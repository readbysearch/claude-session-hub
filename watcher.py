#!/usr/bin/env python3
"""
Claude Session Hub — Daemon

Watches ~/.claude/projects/ for JSONL changes and uploads new lines
to the cloud server. Tracks byte offsets per file so only new content
is uploaded. Never writes to the Claude Code directory.

Usage:
    python watcher.py                    # Use config.yaml in current dir
    python watcher.py --config /path/to/config.yaml
    python watcher.py --scan-once        # One-time scan + upload, then exit
"""
import argparse
import json
import logging
import os
import platform
import signal
import sys
import time
import threading
from pathlib import Path

import yaml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

from parser import find_session_files, read_new_lines
from uploader import Uploader

logger = logging.getLogger("csh-daemon")


# ---------------------------------------------------------------------------
# Offset tracker — persists upload progress per file
# ---------------------------------------------------------------------------

class OffsetTracker:
    """Tracks byte offsets for each JSONL file so we only upload new lines."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.offsets: dict[str, int] = {}
        self._load()

    def _load(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, "r") as f:
                    self.offsets = json.load(f)
            except (json.JSONDecodeError, IOError):
                self.offsets = {}

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(self.offsets, f, indent=2)

    def get(self, filepath: str) -> int:
        return self.offsets.get(filepath, 0)

    def set(self, filepath: str, offset: int):
        self.offsets[filepath] = offset


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Auto-detect claude projects dir if not set
    if not config.get("claude_projects_dir"):
        if platform.system() == "Windows":
            home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        else:
            home = os.path.expanduser("~")
        config["claude_projects_dir"] = str(Path(home) / ".claude" / "projects")

    return config


# ---------------------------------------------------------------------------
# File event handler with debounce
# ---------------------------------------------------------------------------

class SessionFileHandler(FileSystemEventHandler):
    """Watches for .jsonl file changes and triggers uploads after debounce."""

    def __init__(self, process_callback, debounce_seconds: float = 3.0):
        super().__init__()
        self.process_callback = process_callback
        self.debounce_seconds = debounce_seconds
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._schedule(event.src_path)

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._schedule(event.src_path)

    def _schedule(self, filepath: str):
        with self._lock:
            # Cancel existing timer for this file
            if filepath in self._pending:
                self._pending[filepath].cancel()
            # Schedule new debounced call
            timer = threading.Timer(
                self.debounce_seconds,
                self._fire,
                args=[filepath],
            )
            timer.daemon = True
            timer.start()
            self._pending[filepath] = timer

    def _fire(self, filepath: str):
        with self._lock:
            self._pending.pop(filepath, None)
        try:
            self.process_callback(filepath)
        except Exception as e:
            logger.error(f"Error processing {filepath}: {e}")


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

class Daemon:
    def __init__(self, config: dict):
        self.config = config
        self.projects_dir = Path(config["claude_projects_dir"])
        self.uploader = Uploader(
            server_url=config["server_url"],
            api_key=config["api_key"],
            batch_size=config.get("batch_size", 200),
        )

        # State file lives next to config, never in .claude/
        state_dir = Path.home() / ".claude-session-hub"
        self.tracker = OffsetTracker(state_dir / "offsets.json")

        # Build a map of filepath → (project_path, session_uuid)
        self._file_map: dict[str, tuple[str, str]] = {}
        self._refresh_file_map()

    def _refresh_file_map(self):
        """Scan the projects directory and build the file → metadata map."""
        for entry in find_session_files(self.projects_dir):
            key = str(entry["file"])
            self._file_map[key] = (entry["project_path"], entry["session_uuid"])

    def process_file(self, filepath: str):
        """Read new lines from a file and upload them."""
        # If this is a new file we haven't seen, refresh the map
        if filepath not in self._file_map:
            self._refresh_file_map()

        meta = self._file_map.get(filepath)
        if meta is None:
            logger.debug(f"Ignoring unknown file: {filepath}")
            return

        project_path, session_uuid = meta
        offset = self.tracker.get(filepath)
        lines, new_offset = read_new_lines(Path(filepath), offset)

        if not lines:
            return

        logger.info(
            f"Processing {len(lines)} new lines from "
            f"{Path(filepath).name} (project={project_path})"
        )

        success = self.uploader.upload(project_path, session_uuid, lines)
        if success:
            self.tracker.set(filepath, new_offset)
            self.tracker.save()
        else:
            logger.warning(
                f"Upload failed for {filepath}, will retry. "
                f"Offset not advanced."
            )

    def scan_all(self):
        """Full scan of all session files — used for initial sync and --scan-once."""
        logger.info(f"Scanning {self.projects_dir} ...")
        self._refresh_file_map()

        total_files = len(self._file_map)
        total_new_lines = 0

        for filepath in self._file_map:
            offset = self.tracker.get(filepath)
            file_size = Path(filepath).stat().st_size if Path(filepath).exists() else 0
            if file_size > offset:
                lines, new_offset = read_new_lines(Path(filepath), offset)
                if lines:
                    project_path, session_uuid = self._file_map[filepath]
                    success = self.uploader.upload(project_path, session_uuid, lines)
                    if success:
                        self.tracker.set(filepath, new_offset)
                        total_new_lines += len(lines)

        self.tracker.save()
        logger.info(
            f"Scan complete: {total_files} files checked, "
            f"{total_new_lines} new lines uploaded."
        )

    def catchup_scan(self):
        """Lightweight scan of already-known files for unsynced data.

        Unlike scan_all(), this does NOT walk the directory tree to discover
        new files (watchdog handles that). It only stat()s files already in
        _file_map and uploads any data past the saved offset.
        """
        synced = 0
        for filepath, (project_path, session_uuid) in self._file_map.items():
            p = Path(filepath)
            if not p.exists():
                continue
            offset = self.tracker.get(filepath)
            if p.stat().st_size <= offset:
                continue
            lines, new_offset = read_new_lines(p, offset)
            if not lines:
                continue
            logger.info(
                f"Catch-up: {len(lines)} unsynced lines in "
                f"{p.name} (project={project_path})"
            )
            if self.uploader.upload(project_path, session_uuid, lines):
                self.tracker.set(filepath, new_offset)
                synced += len(lines)

        if synced:
            self.tracker.save()
            logger.info(f"Catch-up scan: uploaded {synced} lines.")

    def run(self):
        """Start the file watcher daemon."""
        if not self.projects_dir.exists():
            logger.error(f"Projects directory not found: {self.projects_dir}")
            logger.error("Is Claude Code installed? Check claude_projects_dir in config.")
            sys.exit(1)

        # Initial full scan
        logger.info("Running initial scan...")
        self.scan_all()

        # Start watchdog
        handler = SessionFileHandler(
            process_callback=self.process_file,
            debounce_seconds=self.config.get("debounce_seconds", 3.0),
        )
        observer = Observer()
        observer.schedule(handler, str(self.projects_dir), recursive=True)
        observer.start()
        logger.info(f"Watching {self.projects_dir} for changes...")

        # Graceful shutdown
        stop_event = threading.Event()

        def shutdown(signum, frame):
            logger.info("Shutting down...")
            stop_event.set()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Periodic catch-up scan interval (seconds)
        catchup_interval = self.config.get("catchup_interval", 300)
        last_catchup = time.monotonic()

        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=1)
                now = time.monotonic()
                if now - last_catchup >= catchup_interval:
                    last_catchup = now
                    self.catchup_scan()
        finally:
            observer.stop()
            observer.join()
            self.tracker.save()
            logger.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Claude Session Hub Daemon")
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--scan-once", action="store_true",
        help="Scan all files once and upload, then exit",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, config.get("log_level", "INFO").upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    daemon = Daemon(config)

    if args.scan_once:
        daemon.scan_all()
    else:
        daemon.run()


if __name__ == "__main__":
    main()

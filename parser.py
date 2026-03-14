"""
Parse Claude Code JSONL session files.

Claude Code stores sessions under:
  ~/.claude/projects/<path-encoded>/sessions/<session-uuid>.jsonl

Where <path-encoded> is the project path with separators replaced by dashes.
For example:
  /home/alice/myapp → -home-alice-myapp
  C:\\Users\\alice\\myapp → -C--Users-alice-myapp (varies by version)
"""
import json
import os
import re
from pathlib import Path


def decode_project_path(encoded_dir_name: str) -> str:
    """
    Decode a Claude Code project directory name back to the original path.
    
    Claude encodes paths by replacing '/' with '-'. The leading dash
    represents the root '/'. On Windows paths are encoded differently.
    
    Examples:
        -home-alice-myapp         → /home/alice/myapp
        -Users-alice-myapp        → /Users/alice/myapp
        -C--Users-alice-myapp     → C:/Users/alice/myapp  (approximate)
    """
    # Remove leading dash (represents root /)
    if encoded_dir_name.startswith("-"):
        decoded = encoded_dir_name[1:]
    else:
        decoded = encoded_dir_name

    # Handle Windows drive letter pattern: C- at start
    # Pattern: after removing leading dash, if starts with single letter followed by dash
    win_match = re.match(r"^([A-Z])-(.+)$", decoded)
    if win_match:
        drive = win_match.group(1)
        rest = win_match.group(2).replace("-", "/")
        return f"{drive}:/{rest}"

    # Unix path: replace dashes with slashes
    return "/" + decoded.replace("-", "/")


def find_session_files(projects_dir: Path) -> list[dict]:
    """
    Discover all JSONL session files under the projects directory.
    
    Returns list of dicts:
        {"file": Path, "project_path": str, "session_uuid": str}
    """
    results = []
    if not projects_dir.exists():
        return results

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_path = decode_project_path(project_dir.name)

        # Sessions may be directly in the project dir or in a sessions/ subdir
        session_dirs = [project_dir, project_dir / "sessions"]
        for sdir in session_dirs:
            if not sdir.exists():
                continue
            for jsonl_file in sdir.glob("*.jsonl"):
                session_uuid = jsonl_file.stem
                results.append({
                    "file": jsonl_file,
                    "project_path": project_path,
                    "session_uuid": session_uuid,
                })

    return results


def read_new_lines(filepath: Path, offset: int) -> tuple[list[dict], int]:
    """
    Read JSONL lines from a file starting at the given byte offset.
    
    Returns (lines, new_offset) where each line is:
        {"line_number": int, "raw_json": dict}
    
    Lines that fail to parse are skipped (logged but not fatal).
    """
    lines = []
    try:
        file_size = filepath.stat().st_size
        if file_size <= offset:
            return [], offset

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            # We need to figure out the line number.
            # Count existing lines up to offset to get the starting line number.
            # For efficiency, we store line count in the offset tracker.
            # Here we just count from the new content.
            line_num_start = _count_lines_up_to(filepath, offset)

            line_idx = line_num_start
            while True:
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    line_idx += 1
                    continue
                try:
                    parsed = json.loads(line)
                    lines.append({
                        "line_number": line_idx,
                        "raw_json": parsed,
                    })
                except json.JSONDecodeError:
                    pass  # Skip malformed lines
                line_idx += 1

            new_offset = f.tell()
            return lines, new_offset

    except (OSError, IOError):
        return [], offset


def _count_lines_up_to(filepath: Path, offset: int) -> int:
    """Count the number of newlines in a file up to the given byte offset."""
    if offset == 0:
        return 0
    count = 0
    try:
        with open(filepath, "rb") as f:
            chunk_size = min(offset, 65536)
            remaining = offset
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                data = f.read(to_read)
                if not data:
                    break
                count += data.count(b"\n")
                remaining -= len(data)
    except (OSError, IOError):
        pass
    return count

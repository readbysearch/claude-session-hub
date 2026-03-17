#!/usr/bin/env python3
"""
Step 3: Upload recovered sessions to the Claude Session Hub server.

Reads extracted .jsonl + .meta.json files from the output directory
and uploads them via the daemon's Uploader. Supports an optional
--purge flag to delete existing messages before re-uploading (requires
admin key and the DELETE /api/sessions/{id}/messages endpoint).

Usage:
    python3 upload.py --config ../config.yaml [--input-dir recovered_sessions]
    python3 upload.py --config ../config.yaml --purge --admin-key KEY --server-user USER --server-pass PASS
"""
import argparse
import glob
import json
import os
import sys

import requests
import yaml

# Allow importing uploader from the parent directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from uploader import Uploader


def find_db_ids(server_url: str, username: str, password: str, target_uuids: set) -> dict:
    """Map session UUIDs to their database IDs via the timeline API."""
    db_id_map = {}
    resp = requests.get(
        f"{server_url}/api/timeline",
        params={"days": 90},
        auth=(username, password),
    )
    resp.raise_for_status()

    for machine in resp.json():
        for proj in machine.get("projects", []):
            for session in proj.get("sessions", []):
                if session["uuid"] in target_uuids:
                    db_id_map[session["uuid"]] = session["id"]
    return db_id_map


def purge_messages(server_url: str, admin_key: str, db_id: int) -> bool:
    """Delete all messages for a session via the admin API."""
    resp = requests.delete(
        f"{server_url}/api/sessions/{db_id}/messages",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    return resp.status_code == 200


def main():
    parser = argparse.ArgumentParser(
        description="Upload recovered sessions to Claude Session Hub"
    )
    parser.add_argument(
        "--config",
        default="../config.yaml",
        help="Path to daemon config.yaml (default: ../config.yaml)",
    )
    parser.add_argument(
        "--input-dir",
        default="recovered_sessions",
        help="Directory with recovered .jsonl/.meta.json files (default: recovered_sessions)",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete existing messages before re-uploading (for enriched re-imports)",
    )
    parser.add_argument(
        "--admin-key",
        help="Admin API key (required with --purge)",
    )
    parser.add_argument(
        "--server-user",
        help="Web UI username (required with --purge, for timeline lookup)",
    )
    parser.add_argument(
        "--server-pass",
        help="Web UI password (required with --purge, for timeline lookup)",
    )
    args = parser.parse_args()

    if args.purge and not all([args.admin_key, args.server_user, args.server_pass]):
        parser.error("--purge requires --admin-key, --server-user, and --server-pass")

    with open(args.config) as f:
        config = yaml.safe_load(f)

    uploader = Uploader(
        server_url=config["server_url"],
        api_key=config["api_key"],
        batch_size=config.get("batch_size", 200),
    )

    meta_files = sorted(glob.glob(os.path.join(args.input_dir, "*.meta.json")))
    if not meta_files:
        print(f"No .meta.json files found in {args.input_dir}/")
        return

    # Purge existing messages if requested
    if args.purge:
        target_uuids = set()
        for mf in meta_files:
            with open(mf) as f:
                meta = json.load(f)
            target_uuids.add(meta["session_id"])

        print(f"Looking up {len(target_uuids)} sessions on server...")
        db_id_map = find_db_ids(
            config["server_url"], args.server_user, args.server_pass, target_uuids
        )
        print(f"Found {len(db_id_map)} on server, purging messages...")

        purged = 0
        for uuid, db_id in db_id_map.items():
            if purge_messages(config["server_url"], args.admin_key, db_id):
                purged += 1
            else:
                print(f"  FAILED to purge {uuid[:12]}... (db_id={db_id})")
        print(f"Purged {purged}/{len(db_id_map)} sessions\n")

    # Upload
    ok = 0
    failed = 0
    for meta_file in meta_files:
        with open(meta_file) as f:
            meta = json.load(f)

        jsonl_file = meta_file.replace(".meta.json", ".jsonl")
        lines = []
        with open(jsonl_file) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if line:
                    lines.append({"line_number": i, "raw_json": json.loads(line)})

        success = uploader.upload(meta["cwd"], meta["session_id"], lines)
        status = "OK" if success else "FAILED"
        if success:
            ok += 1
        else:
            failed += 1
        print(f"  {status} | {meta['session_id'][:12]}... | {len(lines):>3} msgs | {meta['cwd']}")

    print(f"\nDone: {ok} uploaded, {failed} failed")


if __name__ == "__main__":
    main()

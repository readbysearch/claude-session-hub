#!/usr/bin/env python3
"""
Step 2: Extract deleted sessions from recall's Tantivy index.

Reads the index, finds sessions whose original .jsonl files have been
deleted, and writes reconstructed .jsonl + .meta.json files to an
output directory.

Usage:
    python3 extract.py [--output-dir recovered_sessions]
    python3 extract.py --deep    # Bypass .del bitmaps for maximum recovery
"""
import argparse
import json
import os
from datetime import datetime, timezone

from scan import scan_index


def extract_deleted(sessions: dict, output_dir: str) -> int:
    """Extract deleted sessions to output_dir. Returns count extracted."""
    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for sid, messages in sessions.items():
        if os.path.exists(messages[0]["file_path"]):
            continue  # still on disk, skip

        messages_sorted = sorted(messages, key=lambda m: m["message_index"])

        # Write JSONL
        with open(os.path.join(output_dir, f"{sid}.jsonl"), "w") as f:
            for msg in messages_sorted:
                ts = msg["timestamp"]
                try:
                    ts_iso = datetime.fromtimestamp(
                        ts, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                except (OSError, ValueError):
                    ts_iso = None

                line = {
                    "type": "message",
                    "timestamp": ts_iso,
                    "message": {
                        "content": [{"type": "text", "text": msg["content"]}],
                    },
                    "recovered_from": "recall_tantivy_index",
                    "recall_metadata": {
                        "source": msg["source"],
                        "cwd": msg["cwd"],
                        "git_branch": msg["git_branch"],
                        "file_path": msg["file_path"],
                        "message_index": msg["message_index"],
                        "timestamp_unix": msg["timestamp"],
                    },
                }
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

        # Write metadata
        with open(os.path.join(output_dir, f"{sid}.meta.json"), "w") as f:
            json.dump(
                {
                    "session_id": sid,
                    "source": messages_sorted[0]["source"],
                    "cwd": messages_sorted[0]["cwd"],
                    "original_file_path": messages_sorted[0]["file_path"],
                    "timestamp": messages_sorted[0]["timestamp"],
                    "message_count": len(messages_sorted),
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

        count += 1
        print(
            f"Recovered: {sid[:12]}... | {len(messages_sorted):>3} msgs "
            f"| {messages_sorted[0]['cwd']}"
        )

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Extract deleted sessions from recall's Tantivy index"
    )
    parser.add_argument(
        "--output-dir",
        default="recovered_sessions",
        help="Directory to write recovered files (default: recovered_sessions)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Bypass Tantivy .del bitmaps to recover soft-deleted docs",
    )
    args = parser.parse_args()

    sessions = scan_index(deep=args.deep)
    count = extract_deleted(sessions, args.output_dir)
    print(f"\nTotal: {count} sessions recovered to {args.output_dir}/")


if __name__ == "__main__":
    main()

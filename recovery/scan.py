#!/usr/bin/env python3
"""
Step 1: Scan recall's Tantivy index and report recoverable sessions.

Lists all sessions in the index, marking each as OK (file exists) or
DELETED (original .jsonl removed by Claude Code). Deleted sessions can
be recovered with extract.py + upload.py.

Usage:
    python3 scan.py              # Normal scan (respects deletion bitmaps)
    python3 scan.py --deep       # Deep scan (bypasses Tantivy .del bitmaps)
"""
import argparse
import json
import os
import shutil
from collections import defaultdict

import tantivy


def scan_index(index_path: str = "~/.cache/recall/index", deep: bool = False) -> dict:
    """Scan the recall Tantivy index and group documents by session.

    If deep=True, temporarily removes Tantivy .del bitmaps to expose
    soft-deleted docs that are still physically present in segment
    .store files. This can recover additional messages from sessions
    that were re-indexed before their .jsonl files were deleted.
    """
    expanded = os.path.expanduser(index_path)
    meta_path = os.path.join(expanded, "meta.json")

    backup_meta = None
    backup_del_dir = None

    if deep:
        # Back up meta.json and .del files
        backup_meta = meta_path + ".bak"
        shutil.copy2(meta_path, backup_meta)

        backup_del_dir = os.path.join(expanded, ".del-backup")
        os.makedirs(backup_del_dir, exist_ok=True)

        # Move .del files aside
        del_files = [f for f in os.listdir(expanded) if f.endswith(".del")]
        for f in del_files:
            shutil.move(os.path.join(expanded, f), os.path.join(backup_del_dir, f))

        # Patch meta.json to remove deletion references
        with open(meta_path) as f:
            meta = json.load(f)

        total_hidden = sum(
            s["deletes"]["num_deleted_docs"]
            for s in meta["segments"]
            if s["deletes"]
        )
        print(f"Deep scan: bypassing deletion bitmaps ({total_hidden} hidden docs)")

        for seg in meta["segments"]:
            seg["deletes"] = None
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

    try:
        index = tantivy.Index.open(expanded)
        index.reload()
        searcher = index.searcher()
        query = index.parse_query("*", ["content"])
        results = searcher.search(query, limit=200000)

        sessions = defaultdict(list)
        for _score, doc_addr in results.hits:
            doc = searcher.doc(doc_addr)
            session_id = doc["session_id"][0]
            sessions[session_id].append({
                "message_index": doc["message_index"][0],
                "content": doc["content"][0],
                "file_path": doc["file_path"][0],
                "source": doc["source"][0],
                "cwd": doc["cwd"][0],
                "git_branch": doc["git_branch"][0] if doc["git_branch"][0] else None,
                "timestamp": doc["timestamp"][0],
            })

        return dict(sessions)
    finally:
        # Always restore original index state
        if deep and backup_meta:
            shutil.copy2(backup_meta, meta_path)
            os.remove(backup_meta)
            if backup_del_dir:
                for f in os.listdir(backup_del_dir):
                    shutil.move(
                        os.path.join(backup_del_dir, f),
                        os.path.join(expanded, f),
                    )
                os.rmdir(backup_del_dir)
            print("Index restored to original state\n")


def main():
    parser = argparse.ArgumentParser(
        description="Scan recall's Tantivy index for recoverable sessions"
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Bypass Tantivy .del bitmaps to recover soft-deleted docs",
    )
    args = parser.parse_args()

    sessions = scan_index(deep=args.deep)

    deleted = 0
    existing = 0

    for sid, msgs in sorted(sessions.items(), key=lambda x: x[1][0]["timestamp"]):
        file_path = msgs[0]["file_path"]
        exists = os.path.exists(file_path)
        status = "OK" if exists else "DELETED"
        if exists:
            existing += 1
        else:
            deleted += 1
        print(
            f"  {status:>7} | {sid[:12]}... | {len(msgs):>3} msgs "
            f"| {msgs[0]['cwd']} | {msgs[0]['timestamp']}"
        )

    print(f"\nTotal: {len(sessions)} sessions ({existing} OK, {deleted} DELETED)")


if __name__ == "__main__":
    main()

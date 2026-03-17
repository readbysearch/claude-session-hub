#!/usr/bin/env python3
"""
Recover docs from orphaned Tantivy segments not referenced in meta.json.

Orphaned segments are produced by segment merges — the merged output replaces
the source segments in meta.json, but the source files remain on disk. Their
data may contain docs not present in the current live index.

Usage:
    python3 recover_orphans.py              # Report-only (no extraction)
    python3 recover_orphans.py --extract    # Extract unique docs to recovered_sessions/
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

import tantivy

from scan import scan_index

INDEX_PATH = os.path.expanduser("~/.cache/recall/index")
SEGMENT_EXTENSIONS = [".store", ".idx", ".pos", ".term", ".fast", ".fieldnorm"]


def discover_orphans(index_path: str = INDEX_PATH) -> list:
    """Return list of segment IDs on disk but not in meta.json."""
    meta_path = os.path.join(index_path, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)

    meta_ids = {s["segment_id"].replace("-", "") for s in meta["segments"]}

    file_ids = set()
    for name in os.listdir(index_path):
        m = re.match(r"^([0-9a-f]{32})\.", name)
        if m:
            file_ids.add(m.group(1))

    return sorted(file_ids - meta_ids)


def estimate_max_doc(orphan_id: str, index_path: str = INDEX_PATH) -> int:
    """Estimate max_doc for an orphan segment from its fieldnorm file size.

    Each field with fieldnorms=true contributes exactly max_doc bytes.
    Total fieldnorm size = header + (num_fieldnorm_fields * max_doc).
    We calibrate the header size from known segments in meta.json.
    """
    meta_path = os.path.join(index_path, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)

    # Count fields with fieldnorms enabled
    num_fn_fields = 0
    for field in meta["schema"]:
        if field["type"] == "text":
            if field["options"].get("indexing", {}).get("fieldnorms", False):
                num_fn_fields += 1
        elif field["type"] in ("i64", "u64"):
            if field["options"].get("fieldnorms", False):
                num_fn_fields += 1

    # Calibrate header from known segments
    headers = []
    for seg in meta["segments"]:
        seg_id = seg["segment_id"].replace("-", "")
        fn_path = os.path.join(index_path, f"{seg_id}.fieldnorm")
        if os.path.exists(fn_path):
            fn_size = os.path.getsize(fn_path)
            header = fn_size - (num_fn_fields * seg["max_doc"])
            headers.append(header)

    if not headers:
        raise RuntimeError("No reference segments found for calibration")

    # Compute max_doc for orphan using each observed header value
    orphan_fn = os.path.join(index_path, f"{orphan_id}.fieldnorm")
    fn_size = os.path.getsize(orphan_fn)

    for h in sorted(set(headers), reverse=True):
        candidate = (fn_size - h) / num_fn_fields
        if candidate == int(candidate) and candidate > 0:
            return int(candidate)

    # Fallback: use most common header value and round
    most_common_header = max(set(headers), key=headers.count)
    return round((fn_size - most_common_header) / num_fn_fields)


def extract_orphan_docs(
    orphan_id: str, max_doc: int, index_path: str = INDEX_PATH
) -> list:
    """Copy orphan segment to temp dir, open as standalone index, extract all docs."""
    meta_path = os.path.join(index_path, "meta.json")
    with open(meta_path) as f:
        meta = json.load(f)

    # Format segment ID with dashes for meta.json
    sid = (
        f"{orphan_id[:8]}-{orphan_id[8:12]}-{orphan_id[12:16]}"
        f"-{orphan_id[16:20]}-{orphan_id[20:]}"
    )

    tmp_dir = tempfile.mkdtemp(prefix=f"tantivy-orphan-{orphan_id[:8]}-")
    print(f"  Temp directory: {tmp_dir}")

    try:
        # Copy segment files
        for ext in SEGMENT_EXTENSIONS:
            src = os.path.join(index_path, f"{orphan_id}{ext}")
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp_dir, f"{orphan_id}{ext}"))

        def try_open(try_max_doc):
            """Try opening the index with the given max_doc. Returns list of docs or None."""
            standalone_meta = {
                "index_settings": meta["index_settings"],
                "segments": [
                    {
                        "segment_id": sid,
                        "max_doc": try_max_doc,
                        "deletes": None,
                    }
                ],
                "schema": meta["schema"],
                "opstamp": 0,
            }
            with open(os.path.join(tmp_dir, "meta.json"), "w") as f:
                json.dump(standalone_meta, f, indent=2)

            try:
                index = tantivy.Index.open(tmp_dir)
                index.reload()
                searcher = index.searcher()
                query = index.parse_query("*", ["content"])
                results = searcher.search(query, limit=200000)

                docs = []
                for _score, doc_addr in results.hits:
                    doc = searcher.doc(doc_addr)
                    docs.append(
                        {
                            "session_id": doc["session_id"][0],
                            "message_index": doc["message_index"][0],
                            "content": doc["content"][0],
                            "file_path": doc["file_path"][0],
                            "source": doc["source"][0],
                            "cwd": doc["cwd"][0],
                            "git_branch": (
                                doc["git_branch"][0]
                                if doc["git_branch"][0]
                                else None
                            ),
                            "timestamp": doc["timestamp"][0],
                        }
                    )
                return docs
            except Exception:
                return None

        # Try estimated max_doc first
        docs = try_open(max_doc)
        if docs is not None:
            return docs

        print(f"  Failed with max_doc={max_doc}, trying nearby values...")
        for delta in range(-10, 11):
            if delta == 0:
                continue
            try_doc = max_doc + delta
            if try_doc <= 0:
                continue
            docs = try_open(try_doc)
            if docs is not None:
                print(f"  Succeeded with max_doc={try_doc}")
                return docs

        print(f"  Could not open orphan segment with any max_doc near {max_doc}")
        return []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def content_key(doc: dict) -> tuple:
    """Create a dedup key from a document."""
    content_hash = hashlib.md5(doc["content"].encode()).hexdigest()
    return (doc["session_id"], doc["message_index"], content_hash)


def extract_unique_to_files(by_session: dict, output_dir: str) -> int:
    """Write unique docs to recovered_sessions/ in the same format as extract.py."""
    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for sid, msgs in by_session.items():
        msgs_sorted = sorted(msgs, key=lambda m: m["message_index"])

        jsonl_path = os.path.join(output_dir, f"{sid}.jsonl")
        meta_path = os.path.join(output_dir, f"{sid}.meta.json")

        # If session already has a recovered file, append unique docs
        mode = "a" if os.path.exists(jsonl_path) else "w"

        with open(jsonl_path, mode) as f:
            for msg in msgs_sorted:
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
                    "recovered_from": "recall_tantivy_orphan_segment",
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

        # Write meta.json only for new sessions (don't overwrite existing)
        if not os.path.exists(meta_path):
            with open(meta_path, "w") as f:
                json.dump(
                    {
                        "session_id": sid,
                        "source": msgs_sorted[0]["source"],
                        "cwd": msgs_sorted[0]["cwd"],
                        "original_file_path": msgs_sorted[0]["file_path"],
                        "timestamp": msgs_sorted[0]["timestamp"],
                        "message_count": len(msgs_sorted),
                        "recovered_at": datetime.now(timezone.utc).isoformat(),
                        "recovered_from": "orphan_segment",
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )

        count += 1
        print(
            f"  Extracted: {sid[:12]}... | {len(msgs_sorted)} msgs → {jsonl_path}"
        )

    return count


def main():
    parser = argparse.ArgumentParser(
        description="Recover docs from orphaned Tantivy segments"
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="Extract unique docs to recovered_sessions/ directory",
    )
    parser.add_argument(
        "--output-dir",
        default="recovered_sessions",
        help="Directory to write recovered files (default: recovered_sessions)",
    )
    args = parser.parse_args()

    # Step 1: Discover orphans
    print("Discovering orphaned segments...")
    orphans = discover_orphans()

    if not orphans:
        print("No orphaned segments found.")
        return

    print(f"Found {len(orphans)} orphaned segment(s):")
    for oid in orphans:
        files = [f for f in os.listdir(INDEX_PATH) if f.startswith(oid)]
        total_size = sum(
            os.path.getsize(os.path.join(INDEX_PATH, f)) for f in files
        )
        print(f"  {oid} ({total_size / 1024:.1f} KB, {len(files)} files)")

    # Step 2: Extract docs from each orphan
    all_orphan_docs = []
    for oid in orphans:
        print(f"\nProcessing orphan {oid}...")
        max_doc = estimate_max_doc(oid)
        print(f"  Estimated max_doc: {max_doc}")

        docs = extract_orphan_docs(oid, max_doc)
        print(f"  Extracted {len(docs)} docs")
        all_orphan_docs.extend(docs)

    if not all_orphan_docs:
        print("\nNo docs extracted from orphaned segments.")
        return

    # Step 3: Diff against live index (deep scan to include soft-deleted docs)
    print("\nScanning live index (deep=True) for comparison...")
    live_sessions = scan_index(deep=True)

    live_keys = set()
    for sid, msgs in live_sessions.items():
        for msg in msgs:
            live_keys.add(content_key({"session_id": sid, **msg}))

    # Find unique docs from orphan
    unique_docs = []
    duplicate_count = 0
    for doc in all_orphan_docs:
        if content_key(doc) not in live_keys:
            unique_docs.append(doc)
        else:
            duplicate_count += 1

    print(f"\nResults:")
    print(f"  Orphan docs total: {len(all_orphan_docs)}")
    print(f"  Already in live index: {duplicate_count}")
    print(f"  Unique (new) docs: {len(unique_docs)}")

    if not unique_docs:
        print(
            "\nAll orphan docs are already in the live index. "
            "Nothing new to recover."
        )
        return

    # Group unique docs by session
    by_session = defaultdict(list)
    for doc in unique_docs:
        by_session[doc["session_id"]].append(doc)

    print(f"\n  Unique docs span {len(by_session)} session(s):")
    for sid, msgs in sorted(by_session.items()):
        print(f"    {sid[:12]}... | {len(msgs)} new msgs")

    # Step 4: Extract if requested
    if args.extract:
        print(f"\nExtracting to {args.output_dir}/...")
        count = extract_unique_to_files(by_session, args.output_dir)
        print(f"\nExtracted {count} session(s) to {args.output_dir}/")
    else:
        print("\nRun with --extract to save unique docs to recovered_sessions/")


if __name__ == "__main__":
    main()

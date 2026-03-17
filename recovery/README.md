# Recovery from Recall's Tantivy Index

When Claude Code deletes session `.jsonl` files (30-day auto-cleanup), the conversation
content may still exist in [recall](https://github.com/zippoxer/recall)'s Tantivy index.

## Prerequisites

- [recall](https://github.com/zippoxer/recall) was installed and running before sessions were deleted
- Python 3 with `tantivy` and `pyyaml` packages: `pip install tantivy pyyaml`
- Claude Session Hub daemon `config.yaml` with valid server_url and api_key

## What gets recovered

| Field | Recovered | Source |
|-------|-----------|--------|
| Message text content | Yes | `content` (TEXT \| STORED) |
| Session ID | Yes | `session_id` (STRING \| STORED) |
| Working directory | Yes | `cwd` (STRING \| STORED) |
| Git branch | Yes | `git_branch` (STRING \| STORED) |
| Timestamp | Yes | `timestamp` (I64 \| STORED) |
| Message ordering | Yes | `message_index` (U64 \| STORED) |
| Original file path | Yes | `file_path` (STRING \| STORED) |
| Source (claude/codex) | Yes | `source` (STRING \| STORED) |
| Message roles (user/assistant) | **No** | Not stored by recall |
| Tool calls & results | **No** | Filtered out during indexing |
| Raw JSON structure | **No** | Discarded during indexing |
| Thinking blocks | **No** | Filtered out during indexing |

## Usage

### Step 1: Scan — see what's recoverable

```bash
python3 scan.py              # Normal scan
python3 scan.py --deep       # Deep scan (see below)
```

### Step 2: Extract — rebuild session files from the index

```bash
python3 extract.py [--output-dir recovered_sessions]
python3 extract.py --deep    # Extract with deep scan for maximum recovery
```

### Step 3: Upload — send to Claude Session Hub

First upload:
```bash
python3 upload.py --config ../config.yaml
```

Re-upload with enriched metadata (purges old messages first):
```bash
python3 upload.py --config ../config.yaml \
    --purge \
    --admin-key YOUR_ADMIN_KEY \
    --server-user USERNAME \
    --server-pass PASSWORD
```

## Deep scan: recovering soft-deleted docs from Tantivy

The `--deep` flag enables a technique that can recover **additional messages** beyond
what a normal scan finds.

### Background

When recall re-indexes a session file (e.g. because new messages were appended), it:
1. Deletes all existing docs for that file from the Tantivy index
2. Re-indexes the file with the updated content

But Tantivy doesn't immediately remove deleted docs. Instead, it writes **`.del` bitmap
files** that mark which docs in each segment are "soft-deleted". The actual data remains
physically present in the segment `.store` files until a segment merge compacts them away.

This means that if recall re-indexed a session while the `.jsonl` file still existed, and
then Claude Code later deleted the file, the **older version of the docs** may still be
in the `.store` files — just hidden by the `.del` bitmaps.

### How `--deep` works

1. **Backs up** the index's `meta.json` and all `.del` files
2. **Patches** `meta.json` to remove all deletion references (`"deletes": null`)
3. **Moves** `.del` files aside so Tantivy can't find them
4. **Searches** the index — all docs, including soft-deleted ones, are now visible
5. **Restores** the original `meta.json` and `.del` files (even if an error occurs)

This is safe and non-destructive: the original index state is always restored via a
`try/finally` block.

### What deep scan can and cannot recover

**Can recover:**
- Messages from sessions that recall indexed at least once before the file was deleted
- Additional messages from sessions where recall re-indexed an updated file (the older
  version's docs may still be in the segments)

**Cannot recover:**
- Docs that were permanently removed by Tantivy segment merges (irreversible)
- Sessions that were never indexed by recall in the first place

### Example

In our testing on weijing-173, deep scan recovered **466 additional hidden docs** from
soft-deleted segments. Several sessions gained extra messages:

```
Session 331c13ea: 30 msgs (normal) → 56 msgs (deep)
Session 5e520fe6:  7 msgs (normal) → 13 msgs (deep)
```

However, sessions from before mid-November 2025 could not be recovered because the
3-month gap between indexing runs (Dec 2025 → Mar 2026) allowed Tantivy segment merges
to permanently compact the oldest segments.

## Orphaned segment recovery

After Tantivy merges segments, the source segments are removed from `meta.json` but
their files may remain on disk. `recover_orphans.py` checks these orphaned segments
for docs not present in the live index.

### How it works

1. **Discovers orphans** — compares segment files on disk against `meta.json` references
2. **Estimates `max_doc`** — calibrates from fieldnorm file sizes of known segments
3. **Opens as standalone index** — copies orphan files to `/tmp`, writes a minimal
   `meta.json` with matching schema, and opens with `tantivy.Index.open()`
4. **Extracts all docs** — wildcard search `*` on the content field
5. **Diffs against live index** — deep scan to include soft-deleted docs, dedup by
   `(session_id, message_index, content_hash)` tuples
6. **Reports** new/unique docs (if any)

### Usage

```bash
python3 recover_orphans.py              # Report only
python3 recover_orphans.py --extract    # Also extract unique docs to recovered_sessions/
```

### Results on weijing-173 (2026-03-16)

The single orphaned segment `43c1c0a5...` (1.93 MB, max_doc=2421) was a merge product
of existing segments. All 2421 docs were already present in the live index (with deep
scan). Zero new data recovered — but worth checking on other machines or after future
segment merges.

## Why purge and re-upload is needed

The server's `ingest.py` uses `ON CONFLICT DO NOTHING` on `(session_id, line_number)` to
ensure idempotent uploads. This means if sessions were first uploaded with bare `raw_json`
(content only, no metadata), a second upload with enriched `raw_json` will be silently
skipped — the old rows win.

To fix this, we need to:

1. **Delete** existing messages for the affected sessions via the admin API:
   ```bash
   curl -s -X DELETE "http://SERVER:8000/api/sessions/{db_id}/messages" \
       -H "Authorization: Bearer ADMIN_KEY"
   ```

2. **Re-upload** with enriched `raw_json` that includes all recall metadata fields
   (`timestamp`, `recall_metadata.source`, `recall_metadata.cwd`, etc.)

The `--purge` flag in `upload.py` automates this: it looks up the database IDs via the
timeline API, deletes existing messages, then re-uploads. The session row itself is
preserved — only its messages are replaced.

### What changed between bare and enriched uploads

| Field in `raw_json` | Bare (first upload) | Enriched (re-upload) |
|----------------------|---------------------|----------------------|
| `type` | `"message"` | `"message"` |
| `timestamp` | missing | ISO 8601 string |
| `message.content` | text only | text only |
| `recovered_from` | `"recall_tantivy_index"` | `"recall_tantivy_index"` |
| `recall_metadata.source` | missing | `"claude"` / `"codex"` |
| `recall_metadata.cwd` | missing | working directory |
| `recall_metadata.git_branch` | missing | branch name |
| `recall_metadata.file_path` | missing | original `.jsonl` path |
| `recall_metadata.message_index` | missing | position in session |
| `recall_metadata.timestamp_unix` | missing | unix timestamp |

With the enriched data, `ingest.py` can extract proper timestamps for `started_at` and
`last_activity_at` on the session, making recovered sessions appear correctly in the
timeline.

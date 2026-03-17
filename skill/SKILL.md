---
name: session-hub
description: Search and retrieve past Claude Code sessions from the Session Hub. Use when the user wants to find, recall, or search past conversations, or asks "when did I...", "which session had...", "find the conversation about...".
---

# Session Hub — Search Past Claude Code Sessions

Search across all your Claude Code sessions stored in the Session Hub server using full-text search with ranked results and snippets.

## Setup

The CLI needs credentials for the Session Hub API. Configure via either:

1. **Environment variables** (recommended for CI/automation):
   ```bash
   export CSH_SERVER="https://your-server:8000"
   export CSH_USER="your-username"
   export CSH_PASS="your-password"
   ```

2. **Config file** (interactive setup):
   ```bash
   python3 scripts/csh.py config
   # Saves to ~/.claude-session-hub/cli.yaml (chmod 600)
   ```

## Commands

IMPORTANT: The script path is relative to this skill's base directory (shown when the skill loads as "Base directory for this skill: ..."). Always use that base directory to resolve `scripts/csh.py`. For example, if the base directory is `/Users/me/.claude/skills/session-hub`, run `python3 /Users/me/.claude/skills/session-hub/scripts/csh.py`.

### Search (most common)
```bash
python3 scripts/csh.py search "<query>"          # FTS with snippets
python3 scripts/csh.py search "<query>" --json   # JSON with rank + snippets
```

### Dump (for grep/jq pipelines)
```bash
python3 scripts/csh.py dump <session_id>                          # Full text dump
python3 scripts/csh.py dump <session_id> --role human --role assistant  # Conversation only
python3 scripts/csh.py dump <session_id> --jsonl                  # JSONL for jq
python3 scripts/csh.py dump <session_id> | grep -i -C3 "pattern" # Extract specific content
```

### Other
```bash
python3 scripts/csh.py show <session_id> --summary   # Condensed session view
python3 scripts/csh.py timeline [--days N]            # Recent sessions by machine/project
python3 scripts/csh.py machines                       # List connected machines
```

## Query syntax

- Multi-word queries use AND by default: `docker layer` matches messages containing both "docker" and "layer"
- Common stop words ("the", "a", "is") are ignored — searching for only stop words returns no results
- Stemming is applied: `running` matches `run`, `runs`, etc.
- Results are ranked by relevance; title matches are boosted over message body matches

## Output format

### search (human-readable)
```
[724] (untitled)  (myproject @ machine-1, 38d ago)
      ...matched text with context showing why this session ranked...
      ...another snippet from a different message in the same session...
[518] Fix deployment pipeline  (infra @ machine-2, 3d ago)
      ...snippet showing the matched content with surrounding context...
```

Each result shows: `[session_id] title (project @ machine, time_ago)` followed by up to 2 text snippets showing *why* the session matched.

### search --json
```json
[
  {
    "session_id": 724,
    "uuid": "abc-123",
    "title": "(untitled)",
    "project_path": "/home/user/myproject",
    "project_name": "myproject",
    "machine_name": "machine-1",
    "last_activity_at": "2026-02-06T16:50:57Z",
    "message_count": 245,
    "rank": 1.23,
    "snippets": [
      "matched text with <b>keyword</b> highlighted",
      "another <b>keyword</b> match from a different message"
    ]
  }
]
```

Key fields: `session_id` (use with dump/show), `rank` (higher = more relevant), `snippets` (contain `<b>` tags around matched terms).

### dump
Plain text, one message per line: `[role] message text`. Designed for piping to `grep`, `awk`, `jq`, `wc`, etc.

## Workflow patterns

### Pattern 1: Find and extract specific content
```bash
# Search broadly to find the session
python3 scripts/csh.py search "deployment nginx config"
# → [42] ... (infra @ prod-machine, 5d ago)
#       ...snippets show this is the right session...

# Dump and grep to extract the exact content
python3 scripts/csh.py dump 42 | grep -i -C5 "server_name\|proxy_pass"
```

### Pattern 2: Narrow across multiple sessions
```bash
# First search returns too many results
python3 scripts/csh.py search "database migration"

# Add more specific terms to narrow
python3 scripts/csh.py search "database migration foreign key"
# → Now just 2-3 results with clear snippets
```

### Pattern 3: Cross-reference sessions
```bash
# Find all sessions about a topic, get JSON for scripting
python3 scripts/csh.py search "authentication OAuth" --json | python3 -c "
import json, sys
for r in json.load(sys.stdin):
    print(f'{r[\"session_id\"]:>5}  {r[\"machine_name\"]:<20} {r[\"rank\"]:.2f}  {r[\"title\"][:60]}')
"
```

### Pattern 4: Conversation-only extraction
```bash
# Get just the human/assistant turns (skip tool use noise)
python3 scripts/csh.py dump 42 --role human --role assistant | head -100
```

## Message roles

| Role | Content |
|---|---|
| human | User messages — the questions/requests |
| assistant | Claude's responses — analysis, code, explanations |
| tool_use | Tool invocations (Bash, Read, Edit, etc.) — usually just the command |
| tool_result | Tool output — file contents, command output |
| progress | Streaming tool output (partial results) |
| system | System prompts and context — rarely useful |

For most use cases, `--role human --role assistant` gives you the conversation without tool noise. Use `--role tool_use` if you need to find specific commands that were run.

## Presenting results

When summarizing sessions to the user, always include these fields for each session mentioned:
- **Session ID** — e.g. [724]
- **Project folder** — `project_path` or `project_name` (e.g. `/home/readbysearch/myproject`)
- **Machine** — which machine it ran on (e.g. `readbysearch-dev-machine`)
- **Date** — when it happened (from `last_activity_at`)

Example: "Session [724] in `/home/readbysearch` on `readbysearch-dev-machine` (Feb 6) — built multi-layer Docker image..."

Never omit the project folder — the user has sessions across many machines and projects, and the folder is key context for identifying the right session.

## Tips

- Snippets in search results show *why* a session matched — use them to decide which session to dump
- Many sessions have unhelpful auto-generated titles — snippets are often more informative than titles
- Use `dump | grep` as the final step to extract exact content; search is for discovery, dump is for extraction
- `--json` output is useful for scripting and chaining with `jq` or Python

## Dependencies

Requires `click`, `requests`, `pyyaml` (standard pip packages).

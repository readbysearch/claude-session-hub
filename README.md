# Claude Session Hub

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

A self-hosted system that archives and searches your Claude Code sessions across multiple machines. Upload conversations from every laptop, desktop, or server to one central place — then browse, search, and visualize your usage.

## Why?

Claude Code **silently deletes session transcripts after 30 days** by default. There is no warning, no confirmation, and no built-in backup. Multiple bugs make this worse — sessions can vanish even when you change the retention setting:

- [`cleanupPeriodDays: 0` disables transcript persistence entirely](https://github.com/anthropics/claude-code/issues/23710) (opposite of documented behavior)
- [Sessions deleted despite `cleanupPeriodDays` set to 1825](https://github.com/anthropics/claude-code/issues/15935)
- [Same-day sessions deleted during startup cleanup](https://github.com/anthropics/claude-code/issues/18881)
- [Sessions vanish from `--resume` due to corrupted index](https://github.com/anthropics/claude-code/issues/18311)
- [Updates change storage paths without migrating data](https://github.com/anthropics/claude-code/issues/29373)

See [why-session-hub.md](why-session-hub.md) for the full list of known issues.

| | Without Session Hub | With Session Hub |
|---|---|---|
| Session retention | 30 days (default), buggy | Permanent |
| Survives local deletion | No | Yes |
| Survives Claude Code updates | No | Yes |
| Multi-machine access | No | Yes |
| Searchable history | Limited (`--resume` picker) | Full-text search across all sessions |
| Backup guarantee | None | Server-side PostgreSQL |

## Features

- **Activity heatmap** — GitHub-style 365-day calendar showing daily prompt counts, with timezone selector
- **Full-text search** — PostgreSQL FTS with ranked results, title boost, and highlighted snippets
- **Multi-machine sync** — Aggregate sessions from macOS, Linux, and Windows machines
- **Session viewer** — Paginated conversation browser with role-based color coding (human, assistant, tool use, tool result)
- **CLI tool** — UNIX-composable commands for searching and dumping sessions (`grep`, `jq` friendly)
- **Skill integration** — Register as a Claude Code skill for in-conversation session search
- **Incremental upload** — Byte-offset tracking ensures only new lines are sent
- **Read-only** — Daemons never write to your `~/.claude/projects/` directory

## Architecture

```
┌─────────────────────┐                      ┌──────────────────────────┐
│  Machine A (macOS)  │                      │   Cloud Server           │
│  daemon (watchdog)  │──── HTTPS POST ────→ │                          │
├─────────────────────┤                      │   FastAPI + PostgreSQL   │
│  Machine B (Linux)  │──── HTTPS POST ────→ │   Web UI                │
│  daemon (watchdog)  │                      │   Full-text search       │
├─────────────────────┤                      │   Activity heatmap       │
│  Machine C (Windows)│──── HTTPS POST ────→ │                          │
│  daemon (watchdog)  │                      └──────────────────────────┘
└─────────────────────┘
```

## Quick Start

### 1. Start the server

```bash
cp .env.example .env         # Set DATABASE_URL and ADMIN_KEY
docker compose up -d         # Starts PostgreSQL + FastAPI on port 8000
```

### 2. Create a user and register machines

```bash
# Create a web UI user
curl -X POST http://your-server:8000/api/users/create \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username": "yourname", "password": "yourpassword"}'

# Register a machine (repeat for each machine)
curl -X POST http://your-server:8000/api/machines/register \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-laptop", "os": "macos"}'
# Returns: {"machine_id": 1, "api_key": "csh_abc123..."}
```

### 3. Start the daemon (on each machine)

```bash
pip install pyyaml watchdog requests
cp config.example.yaml config.yaml   # Set server_url and api_key
python watcher.py                     # Runs continuously, watching for new sessions
python watcher.py --scan-once         # Or: one-time sync then exit
```

### 4. Browse sessions

Open `http://your-server:8000` in a browser. You'll be prompted for HTTP Basic Auth credentials.

## Web UI

- **Timeline** — Sessions grouped by machine and project, with expand/collapse
- **Heatmap** — 365-day activity calendar colored by prompt count (0 / 1-20 / 21-50 / 51-99 / 100+), with timezone selector persisted in localStorage
- **Search** — Full-text search powered by PostgreSQL with ranked results and highlighted snippets
- **Session detail** — Paginated message viewer (500 per page) with color-coded roles

## CLI

The CLI at `skill/scripts/csh.py` provides compact output for terminal use and agent integration.

```bash
pip install click requests pyyaml
python skill/scripts/csh.py config    # Interactive setup (saved to ~/.claude-session-hub/cli.yaml)
```

### Commands

```bash
# Recent sessions grouped by machine/project
csh timeline [--days 14] [--json]

# Full-text search
csh search "docker deploy" [--json]

# Session detail
csh show 42 [--json]
csh show 42 --summary              # Human/assistant only, truncated

# Dump for piping (plain text or JSONL)
csh dump 42 | grep -i -C3 "pytorch"
csh dump 42 --jsonl | jq 'select(.role == "human")'
csh dump 42 --role human --role assistant

# List connected machines
csh machines [--json]
```

## API Endpoints

| Endpoint | Auth | Description |
|----------|------|-------------|
| `POST /api/users/create` | Admin | Create a web UI user |
| `POST /api/machines/register` | Admin | Register a machine, returns API key |
| `POST /api/upload` | Machine | Upload JSONL lines from daemon |
| `GET /api/timeline` | Basic | Sessions grouped by machine/project |
| `GET /api/sessions/{id}` | Basic | Session detail with paginated messages |
| `GET /api/search?q=` | Basic | Full-text search with snippets |
| `GET /api/heatmap?tz=` | Basic | Daily activity data (365 days) |
| `GET /api/machines` | Basic | List registered machines |
| `DELETE /api/sessions/{id}/messages` | Admin | Reset session for reimport |

## Project Structure

```
claude-session-hub/
├── main.py                  # FastAPI server + all API routes
├── models.py                # SQLAlchemy ORM (User, Machine, Project, Session, Message)
├── schemas.py               # Pydantic request/response models
├── database.py              # Async PostgreSQL engine + FTS setup
├── auth.py                  # HTTP Basic Auth + API key auth
├── ingest.py                # JSONL parsing + DB upsert
├── watcher.py               # Daemon: file watcher with offset tracking
├── parser.py                # Daemon: session file discovery + parsing
├── uploader.py              # Daemon: HTTP upload client
├── config.example.yaml      # Daemon config template
├── Dockerfile               # Server container image
├── docker-compose.yml       # PostgreSQL 16 + FastAPI orchestration
├── requirements.txt         # Server dependencies
├── web/
│   └── index.html           # Single-page app (dark theme, heatmap, search)
└── skill/
    ├── SKILL.md             # Skill documentation for Claude Code agents
    └── scripts/
        └── csh.py           # CLI tool
```

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE).

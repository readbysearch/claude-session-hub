# Claude Session Hub

A read-only upload & browse system for Claude Code sessions across multiple machines.

## Architecture

```
┌─────────────────────┐                      ┌──────────────────────────┐
│  Machine A (Win)    │                      │   Cloud Server           │
│  daemon (watchdog)  │──── HTTPS POST ────→ │                          │
├─────────────────────┤                      │   FastAPI                │
│  Machine B (Linux)  │──── HTTPS POST ────→ │   PostgreSQL (FTS)       │
│  daemon (watchdog)  │                      │   Web UI (timeline view) │
├─────────────────────┤                      │                          │
│  Machine C (Linux)  │──── HTTPS POST ────→ │                          │
│  daemon (watchdog)  │                      └──────────────────────────┘
└─────────────────────┘
```

**Key principles:**
- **Read-only**: Daemons only read `~/.claude/projects/`. Nothing is ever written back.
- **Incremental**: Tracks byte offsets per file so only new lines are uploaded.
- **Safe**: If the server is down, the daemon retries later. No data loss.

## Quick Start

### 1. Start the server (cloud machine)

```bash
cp .env.example .env         # Edit with your DB credentials and ADMIN_KEY
docker compose up -d         # Starts PostgreSQL + FastAPI
```

Create a user and register machines:
```bash
# Create a web UI user
curl -X POST http://your-server:8000/api/users/create \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username": "yourname", "password": "yourpassword"}'

# Register a machine to get a daemon API key
curl -X POST http://your-server:8000/api/machines/register \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-laptop", "os": "macos"}'
# Returns: {"machine_id": 1, "api_key": "csh_abc123..."}
```

### 2. Start the daemon (each machine)

```bash
pip install pyyaml watchdog requests
cp config.example.yaml config.yaml   # Edit with server URL + API key
python watcher.py
```

Or install as a systemd service (Linux) / LaunchAgent (macOS).

### 3. Browse sessions

Open `http://your-server:8000` in a browser. HTTP Basic Auth will prompt for your username and password.

## CLI (agent-friendly interface)

The `csh` CLI provides compact, token-efficient output for Claude Code or other agents.

### Setup

```bash
pip install click requests pyyaml
python cli.py config          # Prompts for server URL, username, password
```

Credentials are saved to `~/.claude-session-hub/cli.yaml`.

### Commands

```bash
# Recent sessions grouped by machine/project (default: last 7 days)
python cli.py timeline [--days 14] [--json]

# Search sessions by content or title
python cli.py search "docker" [--json]

# Full session detail
python cli.py show 42 [--json]

# Condensed view — human/assistant text only, truncated to 200 chars
python cli.py show 42 --summary

# Dump session as plain text (pipe to grep/awk)
python cli.py dump 42
python cli.py dump 42 | grep -i -C3 "pytorch"

# Dump as JSONL (pipe to jq)
python cli.py dump 42 --jsonl | jq 'select(.role == "human")'

# Filter by role
python cli.py dump 42 --role human --role assistant

# List connected machines
python cli.py machines [--json]
```

All commands support `--json` for raw JSON output (useful for piping to `jq` or LLM context).
The `dump` command outputs plain text or JSONL for composability with UNIX tools.

## Database Schema

```
machines        projects            sessions              messages
─────────       ──────────          ──────────            ──────────
id              id                  id                    id
name            machine_id (FK)     project_id (FK)       session_id (FK)
os              path_hash           uuid                  role
api_key_hash    original_path       title                 content
created_at      display_name        started_at            tool_name
last_seen_at    created_at          last_activity_at      timestamp
                                    message_count         raw_json
                                                          line_number
```

## Project Structure

```
claude-session-hub/
├── Dockerfile               # Server container image
├── docker-compose.yml       # PostgreSQL + FastAPI orchestration
├── requirements.txt         # Server Python dependencies
├── cli.py                   # CLI tool (csh) for querying sessions
├── main.py                  # FastAPI app + routes
├── database.py              # SQLAlchemy engine + session
├── models.py                # ORM models (User, Machine, Project, Session, Message)
├── schemas.py               # Pydantic request/response schemas
├── auth.py                  # HTTP Basic Auth + API key middleware
├── ingest.py                # JSONL parsing + DB upsert logic
├── watcher.py               # Daemon: file watcher entry point
├── parser.py                # Daemon: JSONL line parser
├── uploader.py              # Daemon: HTTP upload client
├── config.example.yaml      # Daemon config template
└── web/
    └── index.html           # Single-page timeline UI
```

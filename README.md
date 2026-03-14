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
cd server
cp .env.example .env        # Edit with your DB credentials
docker-compose up -d         # Starts PostgreSQL + FastAPI
```

Generate an API key for each machine:
```bash
curl -X POST http://your-server:8000/api/machines/register \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "windows-desktop", "os": "windows"}'
# Returns: {"machine_id": 1, "api_key": "csh_abc123..."}
```

### 2. Start the daemon (each machine)

```bash
cd daemon
pip install -r requirements.txt
cp config.example.yaml config.yaml   # Edit with server URL + API key
python watcher.py
```

Or install as a systemd service (Linux) / Windows Service.

### 3. Browse sessions

Open `http://your-server:8000` in a browser.

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
├── README.md
├── docker-compose.yml
├── server/
│   ├── requirements.txt
│   ├── main.py              # FastAPI app + routes
│   ├── database.py          # SQLAlchemy engine + session
│   ├── models.py            # ORM models
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── auth.py              # API key middleware
│   ├── ingest.py            # JSONL parsing + DB insert logic
│   └── .env.example
├── daemon/
│   ├── requirements.txt
│   ├── config.example.yaml
│   ├── watcher.py           # Main daemon entry point
│   ├── parser.py            # JSONL line parser
│   └── uploader.py          # HTTP upload client
└── web/
    └── index.html           # Single-page timeline UI
```

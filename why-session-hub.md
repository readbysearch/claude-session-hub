# Why Claude Session Hub? — The Session Deletion Problem

## The Problem

Claude Code **silently deletes session transcript files (`.jsonl`) after 30 days** by default. There is no warning, no confirmation, and no built-in backup. Once deleted, sessions cannot be resumed with `claude --resume`, and all conversation history is permanently lost.

This is controlled by the `cleanupPeriodDays` setting (default: 30), but even when users change this setting, multiple bugs cause unexpected data loss.

## Known Issues (as of March 2026)

### 1. Silent 30-Day Auto-Deletion (By Design)

Session `.jsonl` files under `~/.claude/projects/` are automatically deleted after 30 days of inactivity. Most users don't realize this until they try to resume an old session and find it gone.

- Simon Willison's warning (Oct 2025): [Don't let Claude Code delete your session logs](https://simonwillison.net/2025/Oct/22/claude-code-logs/)
- [Issue #22547](https://github.com/anthropics/claude-code/issues/22547) — "Bug: data loss through bad default settings"

### 2. `cleanupPeriodDays: 0` Destroys All Persistence

The documentation states that setting `cleanupPeriodDays` to 0 means "disable cleanup" (retain forever). In reality, it **prevents session transcripts from being written to disk at all** — the exact opposite of what users expect.

- [Issue #23710](https://github.com/anthropics/claude-code/issues/23710) — `cleanupPeriodDays: 0` silently disables all transcript persistence

### 3. Cleanup Ignores User Settings

At least one user set `cleanupPeriodDays` to 1825 (5 years) and still had their project files deleted.

- [Issue #15935](https://github.com/anthropics/claude-code/issues/15935) — Project files deleted despite `cleanupPeriodDays` set to 1825

### 4. Same-Day Session Deletion

Sessions created on the same day were being deleted during startup cleanup, even though they should have been retained.

- [Issue #18881](https://github.com/anthropics/claude-code/issues/18881) — Session cleanup deletes same-day sessions unexpectedly

### 5. Sessions Vanish from `--resume` Picker

Multiple reports of sessions disappearing from `claude --resume` while `.jsonl` files still exist on disk, caused by corrupted or stale `sessions-index.json`.

- [Issue #18311](https://github.com/anthropics/claude-code/issues/18311) — `claude --resume` shows "No conversations found"
- [Issue #25032](https://github.com/anthropics/claude-code/issues/25032) — `sessions-index.json` not updated
- [Issue #14157](https://github.com/anthropics/claude-code/issues/14157) — `/resume` not showing recent sessions

### 6. Updates and Migrations Lose History

Claude Code updates have changed storage directory paths without migrating existing data, making all prior sessions invisible.

- [Issue #29373](https://github.com/anthropics/claude-code/issues/29373) — Desktop update changed directory, no migration performed
- [Issue #12114](https://github.com/anthropics/claude-code/issues/12114) — Session history lost after auto-update
- [Issue #9581](https://github.com/anthropics/claude-code/issues/9581) — All session data lost after logout/login

### 7. Multi-Client Race Conditions

Using VS Code extension and Desktop simultaneously causes sessions to repeatedly disappear from the sidebar.

- [Issue #31787](https://github.com/anthropics/claude-code/issues/31787) — Sessions disappearing with simultaneous clients

## The Workaround (Insufficient)

Adding `"cleanupPeriodDays": 99999` to `~/.claude/settings.json` reduces the risk but does not fully solve the problem — bugs #2, #3, #5, #6, and #7 above can still cause data loss regardless of this setting.

## How Claude Session Hub Solves This

Claude Session Hub runs a lightweight daemon that **uploads session transcripts to a central server in real-time** as you work. This provides:

| Feature | Without Session Hub | With Session Hub |
|---|---|---|
| Session retention | 30 days (default), buggy | Permanent |
| Survives local deletion | No | Yes |
| Survives Claude Code updates | No | Yes |
| Multi-machine access | No | Yes |
| Searchable history | Limited (`--resume` picker) | Full-text search across all sessions |
| Backup guarantee | None | Server-side PostgreSQL with standard backup |

The daemon is **read-only** — it never writes to `~/.claude/projects/`, so it cannot interfere with Claude Code's operation. It tracks byte offsets to upload only new content incrementally, and retries on failure without data loss.

### Quick Setup

```bash
# On the server
docker compose up -d

# On each machine
pip install pyyaml watchdog requests
cp config.example.yaml config.yaml  # edit with server_url and api_key
python3 watcher.py                  # or install as systemd service
```

Once running, every session is preserved on the server regardless of what Claude Code does locally.

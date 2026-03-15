# Session Hub — Technical Decisions & Roadmap

**Status:** Draft
**Date:** March 14, 2026
**Authors:** Core team

---

## 1. Project identity

### What we are building

Session Hub is a self-hosted, multi-provider, agent-consumable platform for uploading, browsing, searching, and sharing AI coding sessions across machines. It captures the *why* behind code changes — the conversations, decisions, and reasoning that happen during AI-assisted development — and makes them searchable, shareable, and reusable.

### What we are not building

We are not building a bidirectional sync tool. The daemon never writes back to the local agent directory. We are not building a Claude-only tool. We support multiple AI coding agents. We are not building a hosted-first SaaS. The open-source self-hosted version is the primary product.

### Core principles

- **Read-only by design.** Daemons only read from agent session directories. Nothing is ever written back. No risk of corrupting the user's local sessions.
- **Multi-provider from day one.** Claude Code, Codex CLI, OpenCode, Gemini CLI, and OpenClaw. If it writes JSONL session logs, we can ingest it.
- **Agent-consumable.** The CLI and Skill are the primary interface, not the web UI. The system is built for agents to query, not just humans to browse.
- **Self-hosted first.** Users own their data. The hosted service (future) is a convenience layer, not a requirement.

---

## 2. Current state (v0.1)

### What exists today

The working prototype consists of three components:

- **Daemon** (Python, watchdog): Monitors `~/.claude/projects/` for JSONL changes on each machine. Tracks byte offsets in a local state file (`~/.claude-session-hub/offsets.json`). Debounces rapid writes (3s window). POSTs new lines to the server in batches of 200. Supports `--scan-once` for one-time upload. Runs on Windows and Linux.
- **Server** (Python, FastAPI + SQLAlchemy + PostgreSQL): Accepts uploads via API key auth. Parses JSONL lines and upserts into structured tables (machines → projects → sessions → messages). Serves a REST API for timeline, session detail, and basic text search.
- **Web UI** (single HTML file, no build step): Timeline view grouped by machine and project. Collapsible session list with auto-generated titles. Session detail modal with full conversation rendering. Search bar with Enter-to-search. Auto-refreshes every 30 seconds.

### Known limitations

- Claude Code only — no parsers for other providers.
- Search uses `ILIKE` pattern matching, which degrades at scale.
- No authentication on the web UI.
- Single-user — no tenant isolation.
- The JSONL parser makes assumptions about Claude Code's directory encoding that may need adjustment for edge cases (especially Windows path encoding).

---

## 3. Decision: Multi-tenant isolation via Option C

### Context

We evaluated three encryption/isolation approaches for multi-user support:

- **Option A (hybrid encryption):** Encrypt message content client-side (AES-256-GCM, user-held key), keep metadata in cleartext. Server cannot read content but can build timelines. Browser decrypts on demand.
- **Option B (full zero-knowledge):** Encrypt everything. Server is a pure blob store. All features (search, timeline, titles) require client-side decryption.
- **Option C (server-side isolation):** No client-side encryption. TLS in transit, PostgreSQL row-level security per user, application-level auth. Server can read all data.

### Decision

**We will implement Option C for all phases.** Option A will be deferred as a potential future opt-in feature if user demand materializes.

### Rationale

Option C preserves full feature parity — server-side search, auto-generated titles, timeline grouping, and the agent-consumable CLI all work without compromise. The primary threat model for a self-hosted tool is cross-user data leakage, which row-level security handles. The secondary threat (server operator reading data) is mitigated by the fact that most users *are* the server operator when self-hosting.

Option A would require building a desktop app for client-side decryption and search, effectively doubling the codebase and maintaining two search implementations. The UX degradation (no server-side content search, no auto-titles for encrypted sessions) is not justified by the threat model of a self-hosted tool.

### Implementation

- Add a `users` table with authentication (username/password, bcrypt-hashed).
- Add `user_id` foreign key to `machines` table. The existing chain (machines → projects → sessions → messages) inherits scoping.
- Enable PostgreSQL row-level security on all tables with policy: `user_id = current_setting('app.current_user_id')`.
- FastAPI sets the session variable after authenticating each request.
- Web UI gets a login page. API endpoints require a session token or API key.
- Add audit logging from day one — log every data access with user ID, timestamp, and query type.

---

## 4. Decision: Multi-provider session ingestion

### Context

Building for Claude Code only creates an existential risk: Anthropic can ship native session sync at any time and do it better. Building for multiple providers creates a category that no single vendor has incentive to build.

### Decision

**The daemon will support pluggable parsers for multiple AI coding agents.** The initial target providers are:

| Provider | Session location | Format |
|---|---|---|
| Claude Code | `~/.claude/projects/` | JSONL |
| Codex CLI | `~/.codex/sessions/` | JSONL |
| OpenCode | `~/.opencode/sessions/` | JSONL |
| OpenClaw | `<workspace>/sessions/` | JSONL |

### Implementation

Refactor `parser.py` into a provider registry pattern:

```
daemon/
├── providers/
│   ├── base.py           # Abstract provider interface
│   ├── claude_code.py    # Claude Code parser
│   ├── codex_cli.py      # Codex CLI parser
│   ├── opencode.py       # OpenCode parser
│   └── openclaw.py       # OpenClaw parser
├── watcher.py            # Provider-agnostic file watcher
└── config.yaml           # Lists enabled providers
```

Each provider implements three methods: `find_session_files()`, `decode_project_path()`, and `extract_fields(raw_json)`. The watcher discovers which providers are available by checking whether their session directories exist, and watches all of them simultaneously.

The `machines` table gets a `provider` column (e.g., "claude_code", "codex_cli") so the web UI and CLI can filter by provider.

---

## 5. Decision: CLI + Skill as primary agent interface

### Context

The Playwright team's 2026 data showed that CLI-based workflows consumed roughly 4x fewer tokens than MCP for equivalent tasks. The industry is converging on CLI + Skill as the standard pattern for agent tooling: Playwright CLI, Vercel's agent-browser, and Google's gws all follow this model.

Our unique value is cross-machine context retrieval — enabling an agent on machine B to access sessions from machine A. This is best served by a CLI that any agent can call, paired with a Skill that teaches the agent when and how to use it.

### Decision

**We will build a CLI (`csh`) as a pip-installable package, paired with a SKILL.md that works across Claude Code, Codex CLI, and OpenClaw.**

### CLI design

```
csh search <query>                     # Full-text search across all sessions
csh timeline [--days N]                # Recent activity summary
csh session show <id> [--summary]      # Full or condensed session view
csh session share <id> [--visibility]  # Generate share link
csh machines list                      # Show connected machines
csh context <project> [--days N]       # Recent decisions for a project
csh config                             # Set server URL and API key
```

Design principles for CLI output:

- Default output is compact and token-efficient. Session titles, timestamps, one-line summaries.
- `--summary` flag returns an LLM-friendly condensed version of a session.
- `--json` flag for structured output that agents can parse.
- Full session content is only returned when explicitly requested with `csh session show <id>`.
- The agent decides what to pull into context, not the tool.

### Skill design

```yaml
---
name: session-hub
description: Search and recall past AI coding sessions across machines and providers
---
When the user references past work, decisions, or sessions on other machines,
use the `csh` CLI to find relevant context.

## Commands
- `csh search "<query>"` — full-text search across all sessions
- `csh timeline` — recent sessions grouped by project and machine
- `csh session show <id> --summary` — condensed session summary
- `csh context <project>` — recent architectural decisions for a project

## When to use
- User says "what did we decide about..." or "on the other server..."
- User references work done on a different machine
- Starting a new session that continues previous work
- User shares a session hub link or token
```

The same SKILL.md works for Claude Code (`~/.claude/skills/`), Codex CLI (`~/.codex/skills/`), and OpenClaw (`<workspace>/skills/`). One file, all providers.

### Distribution

- CLI: `pip install csh-cli` (PyPI)
- Skill for Claude Code: `npx skills add session-hub/csh`
- Skill for OpenClaw: `clawhub install session-hub`
- All three point to the same GitHub repo.

---

## 6. Decision: Shared sessions

### Context

Session transcripts are a form of institutional knowledge that currently evaporates. Git captures what changed; sessions capture why. Making sessions shareable transforms the product from a personal productivity tool into a knowledge layer for AI-assisted development.

### Decision

**Sessions will support three visibility levels: private (default), link-shared, and public.** Sharing is always opt-in and controlled by the session owner.

### Visibility levels

| Level | Who can view | Appears in search | Use case |
|---|---|---|---|
| Private | Owner only | Owner's search only | Default for all sessions |
| Link-shared | Anyone with the URL | No | Sharing with a PR reviewer or teammate |
| Public | Anyone on the instance | Yes (instance-wide search) | Educational content, open-source contributions, team transparency |

### Implementation

- Add `visibility` column to `sessions` table (enum: private, link_shared, public). Default: private.
- Add `share_token` column (random 8-character string, generated on first share).
- New endpoint: `GET /s/{share_token}` — returns session detail without authentication if visibility is link_shared or public.
- New endpoint: `POST /api/sessions/{id}/share` — sets visibility and returns the share URL.
- CLI command: `csh session share <id> --visibility link` — returns the shareable URL.
- Web UI: "Share" button on each session that generates/displays the link with a visibility selector.
- Public sessions are included in search results for all authenticated users on the instance.

### Content safety

Sessions can contain sensitive data (API keys, internal URLs, credentials, proprietary code). Safeguards:

- Default is always private. Sharing requires explicit action.
- The share UI displays a warning: "This session may contain sensitive information. Review before sharing."
- Future: optional automatic redaction of common secret patterns (API keys, tokens, passwords) before sharing. Not in scope for initial implementation.

---

## 7. Decision: Search upgrade to PostgreSQL full-text search

### Context

The current `ILIKE` search works for small datasets but degrades with scale — it performs a sequential scan on every query. With multi-provider ingestion and shared sessions, the search corpus will grow significantly.

### Decision

**Upgrade to PostgreSQL native full-text search using `tsvector` and `tsquery`.**

### Implementation

- Add a `search_vector` column (type `tsvector`) to the `messages` table.
- Create a GIN index on the search vector.
- Populate via trigger on INSERT: `to_tsvector('english', coalesce(content_text, ''))`.
- Add a `search_vector` column to `sessions` table for title search.
- The search API uses `ts_rank()` for relevance scoring and returns results ordered by rank with recency as a tiebreaker.
- The CLI `csh search` command returns results ranked by relevance with session title, machine name, project name, and a text snippet.

---

## 8. Phased roadmap

### Phase 1: Ship the open-source core (weeks 1–3)

**Goal:** Working multi-provider session upload and browse.

- Refactor parser into provider registry (Claude Code + Codex CLI initially).
- Add multi-tenant auth and PostgreSQL row-level security.
- Upgrade search to `tsvector` full-text search.
- Add login page to web UI.
- Write comprehensive README with install instructions for all platforms.
- Publish to GitHub. Target: usable by self-hosters on day one.

**Validation signal:** 50+ GitHub stars, 10+ self-host users.

### Phase 1.5: CLI + Skill (weeks 3–4)

**Goal:** Agents can consume session data.

- Build `csh` CLI as a pip package (thin REST client, ~300 lines).
- Write SKILL.md compatible with Claude Code, Codex CLI, and OpenClaw.
- Publish CLI to PyPI, Skill to ClawHub and awesome-agent-skills repos.
- Add `--summary` output mode optimized for LLM context injection.

**Validation signal:** Agents successfully using `csh search` to retrieve cross-machine context.

### Phase 2: Shared sessions (weeks 4–6)

**Goal:** Sessions become shareable artifacts.

- Implement visibility levels (private/link-shared/public).
- Add share endpoints to API and CLI.
- Add share UI to web frontend.
- Add OpenClaw and OpenCode parsers.

**Validation signal:** Users sharing session links in GitHub PRs or team channels.

### Phase 3: Scale and hosted service (if warranted)

**Goal:** Offer a hosted option for users who don't want to self-host.

**Prerequisites:** Sustained organic demand from Phase 1–2 users asking for a hosted option.

- Move to managed PostgreSQL (Neon, Supabase, or RDS).
- Add data export/import for portability.
- Add GDPR compliance: privacy policy, data export endpoint, account deletion cascade, EU hosting.
- Add monitoring, alerting, and backup infrastructure.
- Define free vs. paid tiers (free tier: full features with storage cap; paid: higher storage, longer retention, priority support).

### Phase 4 (future): Optional hybrid encryption

**Prerequisites:** User demand from enterprise or security-conscious users.

- Add optional client-side content encryption (AES-256-GCM, Argon2id key derivation).
- Browser-side decryption via WebCrypto API (no desktop app).
- Encrypted sessions get reduced functionality: no server-side content search, no auto-titles.
- Implemented as opt-in per-machine flag in daemon config.

---

## 9. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Anthropic ships native cross-machine sync for Claude Code | High | High | Multi-provider support means we serve a category Anthropic won't. Self-hosted angle remains valuable regardless. |
| Claude Code JSONL format changes without notice | Medium | Medium | Pin parser versions to Claude Code versions. Community can contribute fixes quickly for open-source projects. |
| ClawHub security concerns deter users from installing our skill | Low | Medium | Skill is backed by a real GitHub repo with transparent code. Self-hosted architecture is a trust signal. |
| Search quality is poor at scale | Medium | Medium | PostgreSQL FTS with `ts_rank` handles millions of rows. Can add Tantivy or Meilisearch later if needed. |
| Low adoption — nobody uses it | Medium | High | Keep scope tight. If the project doesn't find users in Phase 1 (2–3 weeks of work), the cost of failure is minimal. |
| Shared sessions leak sensitive data | Medium | High | Default is private. Share requires explicit action. Warning displayed before sharing. Future: automatic secret redaction. |

---

## 10. Tech stack summary

| Component | Technology | Rationale |
|---|---|---|
| Daemon | Python 3.11+, watchdog, requests, PyYAML | Cross-platform (Windows + Linux). watchdog uses native OS file events on both platforms. |
| Server | Python 3.11+, FastAPI, SQLAlchemy (async), asyncpg | Async for concurrent uploads from multiple daemons. FastAPI for auto-generated OpenAPI docs. |
| Database | PostgreSQL 16 | Row-level security for multi-tenancy. Native full-text search with tsvector. JSONB for raw session data. |
| Web UI | Single HTML file, vanilla JS | No build step. No framework churn. Ships inside the Docker image. |
| CLI | Python 3.11+, requests, click | Same language as daemon for maintainability. pip-installable. |
| Deployment | Docker Compose (PostgreSQL + FastAPI) | Single `docker-compose up` to run. |
| Skill | SKILL.md (Agent Skills open standard) | Works across Claude Code, Codex CLI, OpenClaw, and others without modification. |

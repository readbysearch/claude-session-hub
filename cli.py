#!/usr/bin/env python3
"""
csh — CLI for querying Claude Session Hub.

Usage:
    python cli.py config
    python cli.py timeline [--days N] [--json]
    python cli.py search <query> [--json]
    python cli.py show <id> [--json] [--summary]
    python cli.py dump <id> [--jsonl] [--role ROLE]
    python cli.py machines [--json]
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import requests
import yaml

CONFIG_DIR = Path.home() / ".claude-session-hub"
CONFIG_FILE = CONFIG_DIR / "cli.yaml"


def load_config():
    if not CONFIG_FILE.exists():
        click.echo(f"Config not found at {CONFIG_FILE}. Run: csh config", err=True)
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def api_get(path, params=None):
    """Make an authenticated GET request to the hub API."""
    cfg = load_config()
    url = cfg["server_url"].rstrip("/") + path
    try:
        resp = requests.get(
            url,
            params=params,
            auth=(cfg["username"], cfg["password"]),
            timeout=30,
        )
    except requests.ConnectionError:
        click.echo(f"Error: cannot connect to {cfg['server_url']}", err=True)
        sys.exit(1)
    if resp.status_code == 401:
        click.echo("Error: authentication failed — check credentials in cli.yaml", err=True)
        sys.exit(1)
    if resp.status_code != 200:
        click.echo(f"Error: {resp.status_code} {resp.text}", err=True)
        sys.exit(1)
    return resp.json()


def time_ago(iso_str):
    """Convert ISO8601 string to a human-readable 'time ago' string."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        if seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        d = seconds // 86400
        return f"{d}d ago"
    except Exception:
        return iso_str


@click.group()
def cli():
    """csh — query Claude Session Hub from the command line."""
    pass


@cli.command()
def config():
    """Set server URL and credentials."""
    click.echo("Claude Session Hub CLI configuration")
    click.echo("=" * 40)

    # Load existing values as defaults
    defaults = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            defaults = yaml.safe_load(f) or {}

    server_url = click.prompt(
        "Server URL",
        default=defaults.get("server_url", "http://localhost:8000"),
    )
    username = click.prompt(
        "Username",
        default=defaults.get("username", ""),
    )
    password = click.prompt(
        "Password",
        default=defaults.get("password", ""),
        hide_input=True,
        show_default=False,
    )

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "server_url": server_url,
        "username": username,
        "password": password,
    }
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    CONFIG_FILE.chmod(0o600)

    click.echo(f"\nSaved to {CONFIG_FILE}")


@cli.command()
@click.option("--days", default=7, help="Number of days to look back (1-90).")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def timeline(days, as_json):
    """Recent sessions grouped by machine and project."""
    data = api_get("/api/timeline", {"days": days})

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    if not data:
        click.echo("No sessions found.")
        return

    for machine in data:
        m = machine["machine"]
        click.echo(f"\n{m['name']} ({m.get('os') or '?'})")
        for proj in machine["projects"]:
            name = proj.get("display_name") or proj["original_path"]
            click.echo(f"  {name}  ({proj['session_count']} sessions)")
            for s in proj["sessions"]:
                title = s.get("title") or "(untitled)"
                if len(title) > 70:
                    title = title[:67] + "..."
                ago = time_ago(s.get("last_activity_at"))
                click.echo(f"    [{s['id']}] {title}  ({ago}, {s['message_count']} msgs)")


@cli.command()
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def search(query, as_json):
    """Search sessions by content or title."""
    data = api_get("/api/search", {"q": query})

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    if not data:
        click.echo("No results.")
        return

    for r in data:
        title = r.get("title") or "(untitled)"
        if len(title) > 50:
            title = title[:47] + "..."
        ago = time_ago(r.get("last_activity_at"))
        click.echo(
            f"[{r['session_id']}] {title}  "
            f"({r['project_name']} @ {r['machine_name']}, {ago})"
        )


@cli.command()
@click.argument("session_id", type=int)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option("--summary", is_flag=True, help="Condensed view (human/assistant text only).")
def show(session_id, as_json, summary):
    """Show full session detail."""
    data = api_get(f"/api/sessions/{session_id}")

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    # Header
    title = data.get("title") or "(untitled)"
    click.echo(f"{title}")
    click.echo(
        f"  project: {data['project_path']}  machine: {data['machine_name']}  "
        f"messages: {data['message_count']}"
    )
    click.echo("-" * 60)

    for msg in data.get("messages", []):
        role = msg.get("role") or msg.get("msg_type") or "?"
        text = msg.get("content_text") or ""

        if summary:
            # In summary mode, only show human and assistant text messages
            if role not in ("human", "assistant"):
                continue
            if not text.strip():
                continue
            if len(text) > 200:
                text = text[:200] + "..."

        ago = time_ago(msg.get("timestamp"))
        click.echo(f"\n[{role}] ({ago})")
        if text:
            click.echo(text)
        elif msg.get("tool_name"):
            click.echo(f"  tool: {msg['tool_name']}")


@cli.command()
@click.argument("session_id", type=int)
@click.option("--jsonl", is_flag=True, help="One JSON object per message (for jq).")
@click.option("--role", multiple=True, help="Filter by role (e.g. --role human --role assistant).")
def dump(session_id, jsonl, role):
    """Dump session content to stdout for piping to grep/jq/awk."""
    data = api_get(f"/api/sessions/{session_id}")
    roles = set(role) if role else None

    for msg in data.get("messages", []):
        msg_role = msg.get("role") or msg.get("msg_type") or "unknown"
        if roles and msg_role not in roles:
            continue

        if jsonl:
            obj = {
                "role": msg_role,
                "text": msg.get("content_text") or "",
                "tool": msg.get("tool_name"),
                "timestamp": msg.get("timestamp"),
                "line": msg.get("line_number"),
            }
            sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        else:
            text = msg.get("content_text") or ""
            if not text and msg.get("tool_name"):
                text = f"[tool: {msg['tool_name']}]"
            if not text:
                continue
            sys.stdout.write(f"[{msg_role}] {text}\n")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
def machines(as_json):
    """List connected machines."""
    data = api_get("/api/machines")

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    if not data:
        click.echo("No machines registered.")
        return

    # Header
    click.echo(f"{'ID':<5} {'Name':<25} {'OS':<10} {'Last Seen'}")
    click.echo("-" * 55)
    for m in data:
        name = m["name"]
        os_name = m.get("os") or "?"
        ago = time_ago(m.get("last_seen_at"))
        click.echo(f"{m['id']:<5} {name:<25} {os_name:<10} {ago}")


if __name__ == "__main__":
    cli()

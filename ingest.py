"""
Ingestion logic: take raw JSONL lines from daemon uploads,
parse them into structured records, and upsert into the database.
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Project, Session, Message


def _hash_path(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()


def _extract_display_name(path: str) -> str:
    """Extract a human-friendly name from a project path.
    /home/alice/projects/myapp  ->  myapp
    C:\\Users\\alice\\myapp      ->  myapp
    """
    clean = path.replace("\\", "/").rstrip("/")
    return clean.rsplit("/", 1)[-1] if "/" in clean else clean


def _extract_role(raw: dict) -> str | None:
    """Extract the role/type from a JSONL line.
    Claude Code JSONL lines vary in structure. Common patterns:
    - {"type": "human", "message": {...}} 
    - {"type": "assistant", "message": {...}}
    - {"type": "tool_use", ...}
    - {"type": "tool_result", ...}
    - {"role": "user", "content": ...}
    - {"role": "assistant", "content": ...}
    """
    return raw.get("role") or raw.get("type")


def _extract_content_text(raw: dict) -> str | None:
    """Pull readable text from a JSONL line, handling nested structures."""
    # Direct content field (string)
    content = raw.get("content")
    if isinstance(content, str):
        return content[:10000]  # Truncate very long content for the text column

    # Nested message.content
    message = raw.get("message")
    if isinstance(message, dict):
        msg_content = message.get("content")
        if isinstance(msg_content, str):
            return msg_content[:10000]
        # Content can be a list of blocks
        if isinstance(msg_content, list):
            parts = []
            for block in msg_content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(block, str):
                    parts.append(block)
            if parts:
                return "\n".join(parts)[:10000]

    # Tool use: input might be interesting
    if raw.get("type") in ("tool_use", "tool_result"):
        inp = raw.get("input") or raw.get("output")
        if isinstance(inp, str):
            return inp[:10000]

    return None


def _extract_tool_name(raw: dict) -> str | None:
    return raw.get("tool_name") or raw.get("name")


def _extract_timestamp(raw: dict) -> datetime | None:
    """Try to extract a timestamp from various possible fields."""
    for field in ("timestamp", "createdAt", "created_at", "ts"):
        val = raw.get(field)
        if val is None:
            # Check nested message
            msg = raw.get("message", {})
            if isinstance(msg, dict):
                val = msg.get(field)
        if val is not None:
            if isinstance(val, (int, float)):
                try:
                    return datetime.fromtimestamp(val / 1000 if val > 1e12 else val, tz=timezone.utc)
                except (OSError, ValueError):
                    continue
            if isinstance(val, str):
                for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
                    try:
                        return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
    return None


async def ingest_lines(
    db: AsyncSession,
    machine_id: int,
    project_path: str,
    session_uuid: str,
    lines: list[dict],  # Each dict has "line_number" and "raw_json"
) -> int:
    """
    Ingest a batch of JSONL lines into the database.
    Returns the number of new messages inserted.
    """
    # 1. Upsert project
    path_hash = _hash_path(project_path)
    display_name = _extract_display_name(project_path)

    stmt = pg_insert(Project).values(
        machine_id=machine_id,
        path_hash=path_hash,
        original_path=project_path,
        display_name=display_name,
    ).on_conflict_do_nothing(index_elements=["machine_id", "path_hash"])
    await db.execute(stmt)
    await db.flush()

    result = await db.execute(
        select(Project).where(
            Project.machine_id == machine_id,
            Project.path_hash == path_hash,
        )
    )
    project = result.scalar_one()

    # 2. Upsert session
    stmt = pg_insert(Session).values(
        project_id=project.id,
        uuid=session_uuid,
        message_count=0,
    ).on_conflict_do_nothing(index_elements=["project_id", "uuid"])
    await db.execute(stmt)
    await db.flush()

    result = await db.execute(
        select(Session).where(
            Session.project_id == project.id,
            Session.uuid == session_uuid,
        )
    )
    session = result.scalar_one()

    # 3. Insert messages (skip duplicates by line_number)
    inserted = 0
    first_user_msg = None
    latest_ts = session.last_activity_at
    earliest_ts = session.started_at

    for line in lines:
        raw = line["raw_json"]
        line_num = line["line_number"]

        role = _extract_role(raw)
        content_text = _extract_content_text(raw)
        tool_name = _extract_tool_name(raw)
        timestamp = _extract_timestamp(raw)

        stmt = pg_insert(Message).values(
            session_id=session.id,
            line_number=line_num,
            role=role,
            msg_type=raw.get("type"),
            content_text=content_text,
            tool_name=tool_name,
            timestamp=timestamp,
            raw_json=raw,
        ).on_conflict_do_nothing(index_elements=["session_id", "line_number"])
        result = await db.execute(stmt)
        if result.rowcount > 0:
            inserted += 1

        # Track session metadata
        if timestamp:
            if earliest_ts is None or timestamp < earliest_ts:
                earliest_ts = timestamp
            if latest_ts is None or timestamp > latest_ts:
                latest_ts = timestamp

        # Auto-title from first human message
        if first_user_msg is None and role in ("human", "user") and content_text:
            first_user_msg = content_text

    # 4. Update session metadata
    if first_user_msg and not session.title:
        session.title = first_user_msg[:200]
    if earliest_ts:
        session.started_at = earliest_ts
    if latest_ts:
        session.last_activity_at = latest_ts
    session.message_count = session.message_count + inserted

    await db.commit()
    return inserted

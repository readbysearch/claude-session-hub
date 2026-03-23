"""
Claude Session Hub — FastAPI server.

Endpoints:
  POST /api/users/create          — Admin: create a user
  POST /api/machines/register     — Admin: register a new machine, get API key
  DELETE /api/sessions/{id}/messages — Admin: reset session for reimport
  POST /api/upload                — Daemon: upload JSONL lines for a session
  GET  /api/timeline              — Web UI (basic auth): recent sessions
  GET  /api/sessions/{id}         — Web UI (basic auth): session detail
  GET  /api/machines              — Web UI (basic auth): list machines
  GET  /api/search                — Web UI (basic auth): search sessions
  GET  /api/heatmap              — Web UI (basic auth): daily activity heatmap
  GET  /api/activity             — Web UI (basic auth): 7-day prompt scatter plot
  GET  /                          — Serve the web UI
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, desc, delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db, init_db
from models import User, Machine, Project, Session, Message
from schemas import (
    MachineRegisterRequest, MachineRegisterResponse, MachineInfo,
    UploadPayload, SessionSummary, ProjectSummary, MachineTimeline,
    SessionDetail, MessageDetail, SearchResult,
    HeatmapDay, HeatmapResponse,
    ActivityPoint, ActivityResponse,
)
from auth import (
    generate_api_key, hash_api_key, require_admin, require_machine,
    require_basic_auth, hash_password,
)
from ingest import ingest_lines

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database ready.")
    yield


app = FastAPI(
    title="Claude Session Hub",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Admin: create user
# ---------------------------------------------------------------------------

@app.post("/api/users/create")
async def create_user(
    request: Request,
    _admin: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"User '{username}' already exists")

    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"Created user: {username} (id={user.id})")
    return {"user_id": user.id, "username": username}


# ---------------------------------------------------------------------------
# Admin: machine registration
# ---------------------------------------------------------------------------

@app.post("/api/machines/register", response_model=MachineRegisterResponse)
async def register_machine(
    req: MachineRegisterRequest,
    _admin: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(Machine).where(Machine.name == req.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Machine '{req.name}' already registered")

    api_key = generate_api_key()
    machine = Machine(
        name=req.name,
        os=req.os,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(machine)
    await db.commit()
    await db.refresh(machine)

    logger.info(f"Registered machine: {req.name} (id={machine.id})")
    return MachineRegisterResponse(machine_id=machine.id, api_key=api_key)


# ---------------------------------------------------------------------------
# Admin: reset session for reimport
# ---------------------------------------------------------------------------

@app.delete("/api/sessions/{session_id}/messages")
async def reset_session(
    session_id: int,
    _admin: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete all messages for a session and reset its metadata,
    so the daemon can re-upload enriched data."""
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    deleted = await db.execute(
        delete(Message).where(Message.session_id == session_id)
    )
    session.title = None
    session.started_at = None
    session.last_activity_at = None
    session.message_count = 0
    await db.commit()

    logger.info(f"Reset session {session_id} (uuid={session.uuid}): deleted {deleted.rowcount} messages")
    return {
        "session_id": session_id,
        "uuid": session.uuid,
        "messages_deleted": deleted.rowcount,
    }


# ---------------------------------------------------------------------------
# Daemon: upload session lines
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_lines(
    payload: UploadPayload,
    machine: Machine = Depends(require_machine),
    db: AsyncSession = Depends(get_db),
):
    if not payload.lines:
        return {"inserted": 0}

    lines_data = [{"line_number": l.line_number, "raw_json": l.raw_json} for l in payload.lines]
    inserted = await ingest_lines(
        db=db,
        machine_id=machine.id,
        project_path=payload.project_path,
        session_uuid=payload.session_uuid,
        lines=lines_data,
    )
    logger.info(
        f"Upload from {machine.name}: project={payload.project_path}, "
        f"session={payload.session_uuid}, lines={len(payload.lines)}, inserted={inserted}"
    )
    return {"inserted": inserted, "total_lines": len(payload.lines)}


# ---------------------------------------------------------------------------
# Web UI API: timeline (basic auth)
# ---------------------------------------------------------------------------

@app.get("/api/timeline", response_model=list[MachineTimeline])
async def get_timeline(
    days: int = Query(default=7, ge=1, le=90),
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
):
    """Return recent sessions grouped by machine → project."""
    machines_result = await db.execute(
        select(Machine).order_by(Machine.name)
    )
    machines = machines_result.scalars().all()

    timelines = []
    for machine in machines:
        projects_result = await db.execute(
            select(Project)
            .where(Project.machine_id == machine.id)
            .order_by(Project.display_name)
        )
        projects = projects_result.scalars().all()

        project_summaries = []
        for project in projects:
            sessions_result = await db.execute(
                select(Session)
                .where(Session.project_id == project.id)
                .order_by(desc(Session.last_activity_at))
                .limit(20)
            )
            sessions = sessions_result.scalars().all()

            if not sessions:
                continue

            latest = max((s.last_activity_at for s in sessions if s.last_activity_at), default=None)
            project_summaries.append(ProjectSummary(
                id=project.id,
                original_path=project.original_path,
                display_name=project.display_name,
                session_count=len(sessions),
                last_activity_at=latest,
                sessions=[
                    SessionSummary(
                        id=s.id,
                        uuid=s.uuid,
                        title=s.title,
                        started_at=s.started_at,
                        last_activity_at=s.last_activity_at,
                        message_count=s.message_count,
                    )
                    for s in sessions
                ],
            ))

        project_summaries.sort(
            key=lambda p: p.last_activity_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        if project_summaries:
            timelines.append(MachineTimeline(
                machine=MachineInfo(
                    id=machine.id,
                    name=machine.name,
                    os=machine.os,
                    last_seen_at=machine.last_seen_at,
                ),
                projects=project_summaries,
            ))

    return timelines


# ---------------------------------------------------------------------------
# Web UI API: session detail (basic auth)
# ---------------------------------------------------------------------------

@app.get("/api/sessions/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=500),
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    project_result = await db.execute(select(Project).where(Project.id == session.project_id))
    project = project_result.scalar_one()
    machine_result = await db.execute(select(Machine).where(Machine.id == project.machine_id))
    machine = machine_result.scalar_one()

    # Total message count (for pagination metadata)
    count_result = await db.execute(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    )
    total = count_result.scalar()

    # Load messages — paginate only if limit is provided
    msg_query = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.line_number)
    )
    if limit is not None:
        msg_query = msg_query.offset(offset).limit(limit)
    msgs_result = await db.execute(msg_query)
    messages = msgs_result.scalars().all()

    return SessionDetail(
        id=session.id,
        uuid=session.uuid,
        title=session.title,
        project_path=project.original_path,
        machine_name=machine.name,
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        message_count=session.message_count,
        total_messages=total,
        offset=offset,
        limit=limit,
        messages=[
            MessageDetail(
                id=m.id,
                line_number=m.line_number,
                role=m.role,
                msg_type=m.msg_type,
                content_text=m.content_text,
                tool_name=m.tool_name,
                timestamp=m.timestamp,
                raw_json=m.raw_json,
            )
            for m in messages
        ],
    )


# ---------------------------------------------------------------------------
# Web UI API: list machines (basic auth)
# ---------------------------------------------------------------------------

@app.get("/api/machines", response_model=list[MachineInfo])
async def list_machines(
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Machine).order_by(Machine.name))
    machines = result.scalars().all()
    return [
        MachineInfo(
            id=m.id, name=m.name, os=m.os, last_seen_at=m.last_seen_at,
        )
        for m in machines
    ]


# ---------------------------------------------------------------------------
# Web UI API: search sessions (basic auth)
# ---------------------------------------------------------------------------

@app.get("/api/search", response_model=list[SearchResult])
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across message content and session titles with ranked results and snippets."""
    # websearch_to_tsquery supports Google-like syntax: "agentic visual" → AND by default
    # It returns NULL for stop-word-only queries, so we coalesce to an empty query
    fts_query = text("""
        WITH query AS (
            SELECT websearch_to_tsquery('english', :q) AS tsq
        ),
        msg_matches AS (
            SELECT
                m.session_id,
                ts_rank(m.search_vector, q.tsq, 1) AS rank,
                ts_headline('english', m.content_text, q.tsq,
                    'MaxFragments=1, MaxWords=25, MinWords=10, StartSel=<b>, StopSel=</b>'
                ) AS snippet
            FROM messages m, query q
            WHERE q.tsq IS NOT NULL AND m.search_vector @@ q.tsq
        ),
        title_matches AS (
            SELECT
                s.id AS session_id,
                ts_rank(s.title_search_vector, q.tsq, 1) * 2 AS rank,
                ts_headline('english', s.title, q.tsq,
                    'MaxFragments=1, MaxWords=25, MinWords=5, StartSel=<b>, StopSel=</b>'
                ) AS snippet
            FROM sessions s, query q
            WHERE q.tsq IS NOT NULL AND s.title_search_vector @@ q.tsq
        ),
        combined AS (
            SELECT session_id, rank, snippet FROM msg_matches
            UNION ALL
            SELECT session_id, rank, snippet FROM title_matches
        ),
        ranked AS (
            SELECT
                session_id,
                SUM(rank) AS total_rank,
                array_agg(snippet ORDER BY rank DESC) AS snippets
            FROM combined
            GROUP BY session_id
            ORDER BY total_rank DESC
            LIMIT :lim
        )
        SELECT
            r.session_id,
            s.uuid,
            s.title,
            p.original_path AS project_path,
            p.display_name AS project_name,
            mach.name AS machine_name,
            s.last_activity_at,
            s.message_count,
            r.total_rank AS rank,
            r.snippets[1:3] AS snippets
        FROM ranked r
        JOIN sessions s ON s.id = r.session_id
        JOIN projects p ON p.id = s.project_id
        JOIN machines mach ON mach.id = p.machine_id
        ORDER BY r.total_rank DESC
    """)

    result = await db.execute(fts_query, {"q": q, "lim": limit})
    rows = result.all()

    return [
        SearchResult(
            session_id=row.session_id,
            uuid=row.uuid,
            title=row.title,
            project_path=row.project_path,
            project_name=row.project_name,
            machine_name=row.machine_name,
            last_activity_at=row.last_activity_at,
            message_count=row.message_count,
            rank=float(row.rank),
            snippets=row.snippets or [],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

@app.get("/api/heatmap", response_model=HeatmapResponse)
async def get_heatmap(
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
    tz: str = Query("UTC"),
):
    query = text("""
        SELECT
            d.date::date::text AS date,
            COALESCE(a.prompt_count, 0) AS prompts,
            COALESCE(a.session_count, 0) AS sessions
        FROM generate_series(
            (NOW() AT TIME ZONE :tz)::date - 364,
            (NOW() AT TIME ZONE :tz)::date,
            '1 day'
        ) AS d(date)
        LEFT JOIN (
            SELECT
                DATE(m.timestamp AT TIME ZONE :tz) AS day,
                COUNT(*) AS prompt_count,
                COUNT(DISTINCT m.session_id) AS session_count
            FROM messages m
            WHERE m.role IN ('human', 'user')
              AND m.timestamp >= (NOW() AT TIME ZONE :tz)::date - 364
              AND (m.raw_json->'message'->'content'->0->>'type') IS DISTINCT FROM 'tool_result'
            GROUP BY DATE(m.timestamp AT TIME ZONE :tz)
        ) a ON d.date = a.day
        ORDER BY d.date
    """)
    result = await db.execute(query, {"tz": tz})
    rows = result.all()

    days = [HeatmapDay(date=r.date, prompts=r.prompts, sessions=r.sessions) for r in rows]
    prompt_counts = [d.prompts for d in days]

    return HeatmapResponse(
        days=days,
        max_prompts=max(prompt_counts) if prompt_counts else 0,
        total_prompts=sum(prompt_counts),
    )


# ---------------------------------------------------------------------------
# 7-day activity scatter plot
# ---------------------------------------------------------------------------

@app.get("/api/activity", response_model=ActivityResponse)
async def get_activity(
    _user: str = Depends(require_basic_auth),
    db: AsyncSession = Depends(get_db),
    tz: str = Query("UTC"),
):
    query = text("""
        SELECT
            TO_CHAR(m.timestamp AT TIME ZONE :tz, 'YYYY-MM-DD"T"HH24:MI:SS') AS local_ts,
            m.session_id,
            s.title AS session_title
        FROM messages m
        JOIN sessions s ON s.id = m.session_id
        WHERE m.role IN ('human', 'user')
          AND m.timestamp >= ((NOW() AT TIME ZONE :tz)::date - 6) AT TIME ZONE :tz
          AND (m.raw_json->'message'->'content'->0->>'type') IS DISTINCT FROM 'tool_result'
        ORDER BY m.timestamp
    """)
    result = await db.execute(query, {"tz": tz})
    rows = result.all()

    points = [
        ActivityPoint(
            timestamp=r.local_ts,
            session_id=r.session_id,
            session_title=r.session_title,
        )
        for r in rows
    ]
    return ActivityResponse(points=points, tz=tz, total=len(points))


# ---------------------------------------------------------------------------
# Serve web UI
# ---------------------------------------------------------------------------

WEB_DIR = Path(__file__).parent / "web"


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    index = WEB_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>Claude Session Hub</h1><p>Web UI not found. Place index.html in /web/</p>")

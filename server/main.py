"""
Claude Session Hub — FastAPI server.

Endpoints:
  POST /api/machines/register     — Admin: register a new machine, get API key
  POST /api/upload                — Daemon: upload JSONL lines for a session
  GET  /api/timeline              — Web UI: get recent sessions grouped by machine+project
  GET  /api/sessions/{id}         — Web UI: get full session with messages
  GET  /api/machines              — Web UI: list all machines
  GET  /                          — Serve the web UI
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db, init_db
from models import Machine, Project, Session, Message
from schemas import (
    MachineRegisterRequest, MachineRegisterResponse, MachineInfo,
    UploadPayload, SessionSummary, ProjectSummary, MachineTimeline,
    SessionDetail, MessageDetail,
)
from auth import generate_api_key, hash_api_key, require_admin, require_machine
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
# Admin: machine registration
# ---------------------------------------------------------------------------

@app.post("/api/machines/register", response_model=MachineRegisterResponse)
async def register_machine(
    req: MachineRegisterRequest,
    _admin: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Check for duplicate name
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
# Web UI API: timeline
# ---------------------------------------------------------------------------

@app.get("/api/timeline", response_model=list[MachineTimeline])
async def get_timeline(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Return recent sessions grouped by machine → project."""
    # Fetch all machines with their projects and sessions
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

        # Sort projects by most recent activity
        project_summaries.sort(
            key=lambda p: p.last_activity_at or "1970-01-01",
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
# Web UI API: session detail
# ---------------------------------------------------------------------------

@app.get("/api/sessions/{session_id}", response_model=SessionDetail)
async def get_session_detail(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Session)
        .options(selectinload(Session.messages))
        .where(Session.id == session_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get project and machine info
    project_result = await db.execute(select(Project).where(Project.id == session.project_id))
    project = project_result.scalar_one()
    machine_result = await db.execute(select(Machine).where(Machine.id == project.machine_id))
    machine = machine_result.scalar_one()

    messages = sorted(session.messages, key=lambda m: m.line_number)
    return SessionDetail(
        id=session.id,
        uuid=session.uuid,
        title=session.title,
        project_path=project.original_path,
        machine_name=machine.name,
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        message_count=session.message_count,
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
# Web UI API: list machines
# ---------------------------------------------------------------------------

@app.get("/api/machines", response_model=list[MachineInfo])
async def list_machines(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Machine).order_by(Machine.name))
    machines = result.scalars().all()
    return [
        MachineInfo(
            id=m.id, name=m.name, os=m.os, last_seen_at=m.last_seen_at,
        )
        for m in machines
    ]


# ---------------------------------------------------------------------------
# Web UI API: search sessions
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search_sessions(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Basic text search across message content and session titles."""
    pattern = f"%{q}%"

    # Search in session titles
    title_results = await db.execute(
        select(Session)
        .where(Session.title.ilike(pattern))
        .order_by(desc(Session.last_activity_at))
        .limit(limit)
    )
    title_sessions = title_results.scalars().all()

    # Search in message content
    msg_results = await db.execute(
        select(Message.session_id)
        .where(Message.content_text.ilike(pattern))
        .group_by(Message.session_id)
        .limit(limit)
    )
    msg_session_ids = [row[0] for row in msg_results.all()]

    all_session_ids = list({s.id for s in title_sessions} | set(msg_session_ids))

    if not all_session_ids:
        return []

    sessions_result = await db.execute(
        select(Session)
        .where(Session.id.in_(all_session_ids))
        .order_by(desc(Session.last_activity_at))
    )
    sessions = sessions_result.scalars().all()

    # Enrich with project/machine names
    results = []
    for s in sessions:
        proj_result = await db.execute(select(Project).where(Project.id == s.project_id))
        project = proj_result.scalar_one()
        machine_result = await db.execute(select(Machine).where(Machine.id == project.machine_id))
        machine = machine_result.scalar_one()
        results.append({
            "session_id": s.id,
            "uuid": s.uuid,
            "title": s.title,
            "project_path": project.original_path,
            "project_name": project.display_name,
            "machine_name": machine.name,
            "last_activity_at": s.last_activity_at.isoformat() if s.last_activity_at else None,
            "message_count": s.message_count,
        })

    return results


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

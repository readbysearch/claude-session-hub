"""
Pydantic schemas for request/response validation.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


# --- Machine registration ---

class MachineRegisterRequest(BaseModel):
    name: str
    os: str | None = None


class MachineRegisterResponse(BaseModel):
    machine_id: int
    api_key: str


class MachineInfo(BaseModel):
    id: int
    name: str
    os: str | None = None
    last_seen_at: datetime | None = None

    model_config = {"from_attributes": True}


# --- Upload from daemon ---

class UploadLine(BaseModel):
    line_number: int
    raw_json: dict[str, Any]


class UploadPayload(BaseModel):
    project_path: str
    session_uuid: str
    lines: list[UploadLine]


# --- Timeline / browsing ---

class SessionSummary(BaseModel):
    id: int
    uuid: str
    title: str | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    message_count: int = 0

    model_config = {"from_attributes": True}


class ProjectSummary(BaseModel):
    id: int
    original_path: str
    display_name: str | None = None
    session_count: int = 0
    last_activity_at: datetime | None = None
    sessions: list[SessionSummary] = []

    model_config = {"from_attributes": True}


class MachineTimeline(BaseModel):
    machine: MachineInfo
    projects: list[ProjectSummary] = []


# --- Session detail ---

class MessageDetail(BaseModel):
    id: int
    line_number: int
    role: str | None = None
    msg_type: str | None = None
    content_text: str | None = None
    tool_name: str | None = None
    timestamp: datetime | None = None
    raw_json: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class SearchResult(BaseModel):
    session_id: int
    uuid: str
    title: str | None = None
    project_path: str
    project_name: str | None = None
    machine_name: str
    last_activity_at: datetime | None = None
    message_count: int = 0
    rank: float = 0.0
    snippets: list[str] = []


class SessionDetail(BaseModel):
    id: int
    uuid: str
    title: str | None = None
    project_path: str
    machine_name: str
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    message_count: int = 0
    messages: list[MessageDetail] = []

    model_config = {"from_attributes": True}

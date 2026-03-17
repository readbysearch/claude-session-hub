import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Index, JSON, func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(128), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    machines = relationship("Machine", back_populates="owner", lazy="selectin")


class Machine(Base):
    __tablename__ = "machines"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), unique=True, nullable=False)
    os = Column(String(32), nullable=True)  # "windows", "linux", "macos"
    api_key_hash = Column(String(256), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    owner = relationship("User", back_populates="machines")
    projects = relationship("Project", back_populates="machine", lazy="selectin")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    machine_id = Column(Integer, ForeignKey("machines.id", ondelete="CASCADE"), nullable=False)
    path_hash = Column(String(64), nullable=False)  # SHA-256 of original_path
    original_path = Column(Text, nullable=False)  # e.g. /home/alice/myapp
    display_name = Column(String(256), nullable=True)  # auto-extracted basename
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    machine = relationship("Machine", back_populates="projects")
    sessions = relationship("Session", back_populates="project", lazy="selectin")

    __table_args__ = (
        Index("ix_project_machine_path", "machine_id", "path_hash", unique=True),
    )


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    uuid = Column(String(128), nullable=False)  # Claude Code session UUID (filename stem)
    title = Column(String(512), nullable=True)  # Auto-generated from first user message
    started_at = Column(DateTime(timezone=True), nullable=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    title_search_vector = Column(TSVECTOR)

    project = relationship("Project", back_populates="sessions")
    messages = relationship("Message", back_populates="session", lazy="noload")

    __table_args__ = (
        Index("ix_session_project_uuid", "project_id", "uuid", unique=True),
        Index("ix_session_last_activity", "last_activity_at"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    line_number = Column(Integer, nullable=False)  # Line index in JSONL file
    role = Column(String(32), nullable=True)  # "human", "assistant", "tool_use", "tool_result", etc.
    msg_type = Column(String(64), nullable=True)  # Claude Code internal type field
    content_text = Column(Text, nullable=True)  # Extracted readable text
    tool_name = Column(String(128), nullable=True)  # For tool_use/tool_result
    timestamp = Column(DateTime(timezone=True), nullable=True)
    raw_json = Column(JSON, nullable=False)  # Full original line — never lose data

    search_vector = Column(TSVECTOR)

    session = relationship("Session", back_populates="messages")

    __table_args__ = (
        Index("ix_message_session_line", "session_id", "line_number", unique=True),
        Index("ix_message_timestamp", "timestamp"),
    )

"""
Database connection and session management.
"""
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://csh_user:changeme@localhost:5432/claude_session_hub",
)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_FTS_STATEMENTS = [
    # tsvector columns
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS search_vector tsvector",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title_search_vector tsvector",
    # GIN indexes
    "CREATE INDEX IF NOT EXISTS ix_messages_search_vector ON messages USING GIN (search_vector)",
    "CREATE INDEX IF NOT EXISTS ix_sessions_title_search_vector ON sessions USING GIN (title_search_vector)",
    # Trigger function: messages
    """CREATE OR REPLACE FUNCTION messages_search_vector_update() RETURNS trigger AS $$
BEGIN
  NEW.search_vector := to_tsvector('english', coalesce(NEW.content_text, ''));
  RETURN NEW;
END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_messages_search_vector ON messages",
    """CREATE TRIGGER trg_messages_search_vector
  BEFORE INSERT OR UPDATE OF content_text ON messages
  FOR EACH ROW EXECUTE FUNCTION messages_search_vector_update()""",
    # Trigger function: sessions.title
    """CREATE OR REPLACE FUNCTION sessions_title_search_vector_update() RETURNS trigger AS $$
BEGIN
  NEW.title_search_vector := to_tsvector('english', coalesce(NEW.title, ''));
  RETURN NEW;
END $$ LANGUAGE plpgsql""",
    "DROP TRIGGER IF EXISTS trg_sessions_title_search_vector ON sessions",
    """CREATE TRIGGER trg_sessions_title_search_vector
  BEFORE INSERT OR UPDATE OF title ON sessions
  FOR EACH ROW EXECUTE FUNCTION sessions_title_search_vector_update()""",
    # Backfill existing rows (no-op once all populated)
    """UPDATE messages SET search_vector = to_tsvector('english', coalesce(content_text, ''))
  WHERE search_vector IS NULL""",
    """UPDATE sessions SET title_search_vector = to_tsvector('english', coalesce(title, ''))
  WHERE title_search_vector IS NULL""",
]


async def init_db():
    """Create all tables if they don't exist, then set up FTS."""
    from models import Machine, Project, Session, Message  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Set up full-text search infrastructure
        logger.info("Setting up FTS indexes, triggers, and backfill...")
        for stmt in _FTS_STATEMENTS:
            await conn.execute(text(stmt))
        logger.info("FTS setup complete.")


async def get_db():
    """FastAPI dependency that yields a DB session."""
    async with async_session() as session:
        yield session

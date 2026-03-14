"""
Authentication helpers for the Claude Session Hub API.
"""
import hashlib
import os
import secrets

from fastapi import Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin_changeme_please")


def generate_api_key() -> str:
    """Generate a random API key for a machine."""
    return f"csh_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


async def require_admin(authorization: str = Header(...)) -> str:
    """Dependency: require a valid admin key in the Authorization header."""
    token = authorization.removeprefix("Bearer ").strip()
    if token != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return token


async def require_machine(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Dependency: require a valid machine API key. Returns the Machine object."""
    from models import Machine

    token = authorization.removeprefix("Bearer ").strip()
    key_hash = hash_api_key(token)

    result = await db.execute(select(Machine).where(Machine.api_key_hash == key_hash))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=403, detail="Invalid API key")

    # Update last_seen
    from sqlalchemy import func
    machine.last_seen_at = func.now()
    await db.commit()

    return machine

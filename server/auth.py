"""
Authentication helpers for the Claude Session Hub API.
"""
import base64
import hashlib
import os
import secrets

import bcrypt
from fastapi import Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db

ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin_changeme_please")


# ---------------------------------------------------------------------------
# API key helpers (for daemon auth)
# ---------------------------------------------------------------------------

def generate_api_key() -> str:
    return f"csh_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Password helpers (for user auth)
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def require_admin(authorization: str = Header(...)) -> str:
    token = authorization.removeprefix("Bearer ").strip()
    if token != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return token


async def require_machine(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    from models import Machine
    from sqlalchemy import func

    token = authorization.removeprefix("Bearer ").strip()
    key_hash = hash_api_key(token)

    result = await db.execute(select(Machine).where(Machine.api_key_hash == key_hash))
    machine = result.scalar_one_or_none()
    if machine is None:
        raise HTTPException(status_code=403, detail="Invalid API key")

    machine.last_seen_at = func.now()
    await db.commit()
    return machine


async def require_basic_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Dependency: require HTTP Basic Auth. Returns username on success."""
    from models import User

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Session Hub\""},
        )

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Session Hub\""},
        )

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Basic realm=\"Claude Session Hub\""},
        )

    return username

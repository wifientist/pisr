"""
What the SPA needs to know about the tenant it is pointed at.

Replaces the part of rtools2's auth surface that PISR actually used: the frontend
read its controller identity out of a login session, and now reads it from here.
Serving it at runtime rather than baking VITE_* vars at build time keeps .env the
single source of truth — flipping R1_EC_TYPE from EC to MSP is a restart, not a
rebuild — and lets one built image serve any tenant.
"""

from fastapi import APIRouter

from config import public_config

router = APIRouter(tags=["Config"])


@router.get("/config")
async def get_config():
    """Identity only. No credentials — see config.public_config()."""
    return public_config()

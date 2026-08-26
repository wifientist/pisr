"""
The RUCKUS ONE client, built from .env instead of from a database row.

Replaces rtools2's `clients/r1_client.py`, which looked a controller up by id,
checked that the requesting user owned it, and Fernet-decrypted its credentials.
Standalone PISR has one controller, no users and no encryption, so all three
collapse into the small functions below.
"""

import logging
from typing import Optional

from fastapi import HTTPException

from config import CONTROLLER, ControllerConfig
from r1api.client import R1Client

logger = logging.getLogger(__name__)


def get_controller(controller_id: int) -> ControllerConfig:
    """
    Replaces validate_controller_access() plus the RuckusONE type check.

    There is exactly one controller and it is RuckusONE by construction, so the
    only thing left to validate is that the URL is asking for the one we have.
    The `{controller_id}` path segment is kept so the router and the frontend
    stay byte-compatible with rtools2 — see README, "Why the URLs have a
    controller id in them".
    """
    if controller_id != CONTROLLER.id:
        raise HTTPException(
            404,
            f"No controller {controller_id}. This deployment serves controller "
            f"{CONTROLLER.id} ({CONTROLLER.name}) only.")
    return CONTROLLER


def resolve_tenant(cfg: ControllerConfig, tenant_id: Optional[str]) -> Optional[str]:
    """
    An MSP controller must be told which MSP-EC to report on — its own tenant id
    addresses the MSP account, which owns no venues. An EC controller addresses
    itself and takes no override.
    """
    if cfg.ec_type == "MSP":
        if not tenant_id:
            raise HTTPException(
                400, "This is an MSP tenant — select an MSP-EC first.")
        return tenant_id
    return None


def build_r1_client(cfg: ControllerConfig = CONTROLLER) -> R1Client:
    """
    A fresh client per request. Replaces create_r1_client_from_controller().

    DO NOT cache this at module level. R1Client authenticates once in __init__
    and never re-authenticates: `_request` sets `Authorization` from
    `self.token` and never inspects the response for a 401. A process-lifetime
    client would serve a token that expires about an hour after start and then
    fail every request until the container was restarted — a bug that passes
    every test on day one.

    Building per request is close to free. __init__ consults the process-wide
    token cache first and does no HTTP at all on a hit; the cache's 60-second
    safety margin makes the once-an-hour miss transparent. Connection pooling is
    preserved where it matters, since a report's twenty-odd concurrent reads all
    share the one requests.Session belonging to that request's client.

    If you ever do need to cache it, the correct guard is
    `not get_cached_token(tenant_id)` — NOT `client.auth_failed`, which stays
    unset when a token merely expires.
    """
    client = R1Client(
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        shared_secret=cfg.shared_secret,
        region=cfg.region,
        ec_type=cfg.ec_type,
    )

    if getattr(client, "auth_failed", False):
        error = getattr(client, "auth_error", {}) or {}
        logger.error("R1 authentication failed for tenant %s: %s",
                     cfg.tenant_id, error)
        raise HTTPException(401, _auth_message(cfg, error))

    return client


def _auth_message(cfg: ControllerConfig, error: dict) -> str:
    """
    Say which .env value is likely wrong, rather than just "auth failed".

    Two distinguishable failures, and the second is not obvious:

      * A real rejection — the token endpoint answers with a 4xx. The
        credentials reached RUCKUS ONE and were refused, so the client id or
        the shared secret is wrong.

      * A non-JSON body on a 200. The tenant id is part of the token URL
        (/oauth2/token/{tenant_id}), and an unrecognised one lands on the
        RUCKUS ONE web app instead of the API — which returns an HTML page with
        a perfectly successful status code. That is a wrong tenant id, or the
        right one on the wrong regional cloud.
    """
    status = error.get("status_code")
    if status:
        return (f"RUCKUS ONE rejected the credentials in .env (HTTP {status}). "
                "Check R1_CLIENT_ID and R1_SHARED_SECRET.")
    return (f"The RUCKUS ONE token endpoint for tenant {cfg.tenant_id} returned "
            f"a web page instead of a token. That usually means R1_TENANT_ID is "
            f"wrong, or that this tenant is not on the {cfg.region} cloud — "
            "try R1_REGION=EU or ASIA.")

"""
PISR — Property Install Status Report.

One process serves both the API and the built single-page app. There is no
database, no user accounts, no Redis, no scheduler and no background work: the
tool reads one RUCKUS ONE venue when someone asks it to, and returns the answer.
"""

import logging
import os
import traceback

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from logging_config import setup_logging

setup_logging(log_level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Imported for effect as much as for use: config validates .env at import and
# raises if anything required is missing or malformed, so a misconfigured
# container fails to start rather than failing every request.
from config import AUTH, CONTROLLER, SESSION_SECRET_IS_EPHEMERAL  # noqa: E402
from auth import (  # noqa: E402
    SecurityHeadersMiddleware, SessionGateMiddleware, proxy_preview,
    router as auth_router)
from routers import config_router, msp_router, pisr_router  # noqa: E402

app = FastAPI(
    title="PISR",
    version="1.0.0",
    description="Property Install Status Report — a read-only poll of one venue.",
)

# Normally empty and normally unnecessary: the SPA is served from this same
# origin, so the browser never makes a cross-origin request. Only needed if you
# front PISR with a separate web server on another origin.
_origins = [o.strip() for o in (os.getenv("CORS_ORIGINS") or "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    logger.info("CORS enabled for %s", _origins)

# Note the ordering, which is the opposite of the intuitive reading:
# Starlette's add_middleware does user_middleware.insert(0, ...), so the LAST
# one added ends up OUTERMOST. The gate is therefore in front of CORS, which is
# the safe direction — nothing reaches a route without passing it — but it does
# mean a CORS preflight to a gated path is answered 401 without CORS headers.
# That costs nothing in the shipped layout, where FastAPI serves the SPA from
# its own origin and no preflight happens. If you ever put the frontend on a
# different origin, move CORS after this line so it becomes the outer one.
app.add_middleware(SessionGateMiddleware)

# Outermost of all, so it also covers the SPA, the 401s and the error pages.
app.add_middleware(SecurityHeadersMiddleware)

if not AUTH.enabled:
    logger.warning(
        "PISR_AUTH_DISABLED=1 — every endpoint is open to anyone who can reach "
        "this port, including the full venue inventory. This is only sane if "
        "the port is bound to localhost.")
else:
    # Printed every start, because the alternative way to find out whether a
    # setting took is to sign in and read the address off a later log line —
    # which needs someone to actually sign in, and says nothing at all if they
    # still hold a valid cookie. An env file edit that never reached the
    # container should be visible here, at the moment it fails to happen.
    logger.info(
        "Gate: mode=%s, trusted_proxies=%s, client_ip_header=%s, cookie_secure=%s",
        AUTH.mode,
        ",".join(str(net) for net in AUTH.trusted_proxies) or "(none)",
        AUTH.client_ip_header or "(none — the TCP peer is used as-is)",
        "forced on" if AUTH.cookie_secure else "per-request (HTTPS only)")

    if AUTH.mode == "proxy":
        # Reported on its own line because it decides what the line above
        # means. Verifying assertions makes the trusted-proxy list advisory —
        # it still scopes the client-IP header, but it no longer decides who
        # gets in. Not verifying them makes that list, and the network it
        # describes, the only thing standing there.
        if AUTH.access_team:
            logger.info(
                "Gate: verifying Cloudflare Access assertions from %s "
                "(aud %s…); the peer address is not consulted for identity.",
                f"https://{AUTH.access_team}.cloudflareaccess.com",
                AUTH.access_aud[:12])
        else:
            logger.warning(
                "Gate: NOT verifying Access assertions — the identity header "
                "%s is trusted on the strength of the peer address alone. "
                "Under rootless podman that address is rewritten and "
                "distinguishes nothing, so the network the port is published "
                "on is the only control. Set PISR_ACCESS_TEAM and "
                "PISR_ACCESS_AUD to verify signatures instead.",
                AUTH.proxy_header)

    if AUTH.client_ip_header and not AUTH.trusted_proxies:
        # The one combination that silently does nothing. Worth a warning
        # rather than a fact, because whoever set the header believed they
        # were fixing the throttle and did not.
        logger.warning(
            "PISR_CLIENT_IP_HEADER=%s is set but PISR_TRUSTED_PROXY_IPS is "
            "empty, so the header is ignored and the login throttle still "
            "counts every caller as one. Set the trusted list to the peer "
            "address PISR actually sees — under rootless podman that is an "
            "address on podman's own network, not the proxy's.",
            AUTH.client_ip_header)

    if SESSION_SECRET_IS_EPHEMERAL:
        logger.info(
            "No PISR_SESSION_SECRET set — a random one was generated, so existing "
            "sessions end at every restart. Set one in .env to keep them.")


@app.get("/healthz")
async def healthz():
    """
    Public, and says nothing. The container healthcheck needs a target that
    does not require a session, and /api/status is not it — that one names the
    tenant, the region and the EC type, which is exactly the reconnaissance an
    unauthenticated caller would like to start with.
    """
    return {"status": "ok"}


# Baked in at image build (see the Dockerfile). Read once here rather than per
# request: it cannot change while the process lives, and that is the point of
# it — this is what the RUNNING container is, which is a different question
# from what the repository on the box says, and they disagree exactly when
# something has gone wrong with a deploy.
BUILD_SHA = os.getenv("PISR_BUILD_SHA") or "unknown"
BUILD_TIME = os.getenv("PISR_BUILD_TIME") or None


@app.get("/api/status")
async def status(request: Request):
    return {
        "status": "ok",
        "controller": CONTROLLER.name,
        "subtype": CONTROLLER.ec_type,
        "region": CONTROLLER.region,
        "build": {
            "sha": BUILD_SHA,
            "short": BUILD_SHA[:12] if BUILD_SHA != "unknown" else "unknown",
            "builtAt": BUILD_TIME,
        },
        # What PISR_AUTH_MODE=proxy would decide about this very request, so a
        # switch to it can be checked before it is made rather than after.
        "proxyPreview": proxy_preview(request),
    }


# No root_path="/api" here. rtools2 needed it because nginx stripped the prefix
# before the request arrived; this process sees the real path, so the prefix
# goes on the routers instead.
app.include_router(auth_router, prefix="/api")
app.include_router(config_router.router, prefix="/api")
app.include_router(msp_router.router, prefix="/api")
app.include_router(pisr_router.router, prefix="/api")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # Both keys, deliberately. PISR.tsx reads `detail`; useSingleEc.tsx reads
    # `error`. rtools2 emitted only `error`, which is why PISR's banner there
    # shows a bare status code where the message should be.
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": exc.detail, "error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "detail": "Validation error",
        "error": "Validation error",
        "details": exc.errors(),
    })


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Unexpected error: %s", traceback.format_exc())
    return JSONResponse(status_code=500, content={
        "detail": "Internal server error", "error": "Internal server error"})


# Mounted LAST, and only if it exists.
#
# Starlette matches routes in order, and a Mount at "/" matches everything — put
# this above the routers and every API call quietly returns index.html with a
# 200, which the frontend reports as a JSON parse error.
#
# The isdir guard lets this same file run under docker-compose.dev.yml, where
# vite serves the SPA and the image has no dist/ in it.
_static = os.getenv("PISR_STATIC_DIR", "/app/static")
if os.path.isdir(_static):
    app.mount("/", StaticFiles(directory=_static, html=True), name="spa")
    logger.info("Serving SPA from %s", _static)
else:
    logger.warning("No SPA found at %s — running API-only. "
                   "(Normal in dev; in production it means the build stage "
                   "did not copy dist/.)", _static)

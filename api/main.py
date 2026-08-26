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
from config import CONTROLLER  # noqa: E402
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


@app.get("/api/status")
async def status():
    return {
        "status": "ok",
        "controller": CONTROLLER.name,
        "subtype": CONTROLLER.ec_type,
        "region": CONTROLLER.region,
    }


# No root_path="/api" here. rtools2 needed it because nginx stripped the prefix
# before the request arrived; this process sees the real path, so the prefix
# goes on the routers instead.
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

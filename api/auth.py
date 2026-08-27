"""
The gate in front of PISR.

Two modes, chosen with PISR_AUTH_MODE. Both gate the same paths; they differ
only in what counts as proof.

PASSPHRASE MODE (default). One shared passphrase, exchanged once for a signed
session cookie, checked on every /api request. No database, no session store,
no new dependency — the cookie carries its own expiry and its own signature, so
the process holds no state and a restart is not a data-loss event.

  POST /api/login    {"passphrase": "..."}  -> 204 + Set-Cookie
  POST /api/logout                          -> 204 + cleared cookie
  GET  /api/auth/status                     -> the mode, and whether this
                                               caller is through the gate

A shared passphrase is not identity. It does not tell you who ran a report, it
cannot be revoked for one person, and over plain HTTP it is readable by anyone
on the same wire. It is the right size for a single-tenant tool on a network
you trust.

PROXY MODE. The SSO story, and deliberately the smaller half of it. An
authenticating reverse proxy in front — oauth2-proxy against Entra, Okta or
Google Workspace — does the whole OIDC dance and forwards the caller's identity
in a header. PISR reads the header. It validates no tokens, fetches no JWKS,
holds no client secret and has no callback route, because implementing OIDC in
a read-only reporting tool is how you acquire a security surface you did not
need. Swapping modes is a compose change, not a code change.

  THE HEADER IS A CLAIM, NOT PROOF. Anyone who can open a socket to PISR can
  send `X-Forwarded-Email: someone.important@corp.example`. Two things stop
  that, and you need both:

    1. PISR_TRUSTED_PROXY_IPS — required in proxy mode, no default. Only these
       source addresses are believed. Checked below in `_proxy_identity`.
    2. The port binding. Publish PISR on loopback or on the proxy's own Docker
       network, never on 0.0.0.0. Compose cannot enforce this from in here,
       which is why it is said twice.

  In proxy mode the passphrase is off entirely and /api/login refuses. Leaving
  both doors open would make the shared secret a way around SSO, which throws
  away the audit trail and the revocation story that were the reasons to adopt
  it.

Everything under /api (and /docs, /redoc, /openapi.json) requires proof in
whichever form the mode calls for. The static SPA bundle is deliberately NOT
gated: it contains no tenant data, and it has to load in order to render the
login form.
"""

import hmac
import ipaddress
import logging
import time
from hashlib import sha256
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from config import AUTH

logger = logging.getLogger(__name__)

COOKIE_NAME = "pisr_session"

# Gated prefixes. The SPA bundle at "/" is not among them on purpose — see the
# module docstring. /docs and friends are: they publish the whole route surface,
# which is a map of what an unauthenticated caller should try next.
_GATED_PREFIXES = ("/api/",)
_GATED_EXACT = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")

# Reachable without a cookie. /healthz is deliberately outside /api so the
# container healthcheck does not need a session; it returns a bare "ok" and
# names neither the tenant nor the region.
_PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/auth/status"}


# ── Session token ────────────────────────────────────────────────────

def _signing_key() -> bytes:
    """
    Passphrase mode only — proxy mode mints no cookies.

    Derived from the session secret AND the passphrase, so that changing the
    passphrase invalidates every outstanding session. Without this, rotating a
    leaked passphrase would lock out the wrong people — the attacker holding a
    valid cookie would keep their access, and only honest users would notice.
    """
    return hmac.new(
        AUTH.session_secret.encode(), AUTH.passphrase.encode(), sha256
    ).digest()


def _mint(now: Optional[float] = None) -> str:
    expires = int((now or time.time()) + AUTH.session_seconds)
    payload = str(expires)
    sig = hmac.new(_signing_key(), payload.encode(), sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid(token: str) -> bool:
    """Signature first, then expiry. Both in constant time where it matters."""
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    expected = hmac.new(_signing_key(), payload.encode(), sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


# ── Brute-force throttle ─────────────────────────────────────────────
#
# A shared passphrase with unlimited guesses is a passphrase with no length.
# One process, no Redis, so this is an in-memory dict — which means it resets on
# restart, and means it counts per source IP as seen by the app. Behind a
# reverse proxy every request would appear to come from the proxy; PISR is meant
# to be reached directly, and if that changes this needs to read a forwarded-for
# header from a trusted proxy rather than request.client.

_attempts: Dict[str, Tuple[int, float]] = {}
_attempts_lock = Lock()
_ATTEMPTS_MAX_TRACKED = 2048


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _locked_until(ip: str) -> float:
    with _attempts_lock:
        count, until = _attempts.get(ip, (0, 0.0))
    return until if count >= AUTH.max_attempts and until > time.time() else 0.0


def _record_failure(ip: str) -> None:
    now = time.time()
    with _attempts_lock:
        # Bounded: a spoofed-source flood should not be able to grow this
        # without limit. Drop everything already expired, then everything at
        # all if that was not enough.
        if len(_attempts) >= _ATTEMPTS_MAX_TRACKED:
            for key in [k for k, (_, u) in _attempts.items() if u <= now]:
                del _attempts[key]
            if len(_attempts) >= _ATTEMPTS_MAX_TRACKED:
                _attempts.clear()

        count, until = _attempts.get(ip, (0, 0.0))
        count = 1 if until and until <= now else count + 1
        _attempts[ip] = (count, now + AUTH.lockout_seconds)


def _clear_failures(ip: str) -> None:
    with _attempts_lock:
        _attempts.pop(ip, None)


# ── Proxy identity ───────────────────────────────────────────────────

def _from_trusted_proxy(ip: str) -> bool:
    """
    Is this connection coming from an address allowed to assert an identity?

    `ip` is the TCP peer as Starlette reports it — the proxy itself, not the
    end user, because the proxy is what opened the socket. One caveat worth
    knowing: uvicorn's own ProxyHeadersMiddleware will rewrite that peer from
    X-Forwarded-For if the real peer is listed in --forwarded-allow-ips (which
    defaults to 127.0.0.1 and should stay narrow). If it ever did rewrite, the
    address seen here becomes the end user's and this check fails — a 401,
    not a bypass. Fail closed either way.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net
               for net in AUTH.trusted_proxies)


def _proxy_identity(request: Request) -> Optional[str]:
    """
    The authenticated identity the proxy vouched for, or None.

    None has two very different causes and they are logged differently: a
    trusted proxy that forwarded no identity is a proxy misconfiguration, while
    an identity header arriving from an untrusted address is either a
    misconfigured trusted-proxy list or someone trying it on.
    """
    ip = _client_ip(request)
    claimed = request.headers.get(AUTH.proxy_header, "").strip()

    if not _from_trusted_proxy(ip):
        if claimed:
            logger.warning(
                "Rejected %s asserted from %s, which is not in "
                "PISR_TRUSTED_PROXY_IPS. Either the proxy moved, or something "
                "is forging identity headers.", AUTH.proxy_header, ip)
        return None

    if not claimed:
        logger.warning(
            "Trusted proxy %s forwarded no %s. Check the proxy is actually "
            "authenticating and is configured to pass the header on.",
            ip, AUTH.proxy_header)
        return None

    return claimed


# ── Middleware ───────────────────────────────────────────────────────

def _denied(message: str) -> JSONResponse:
    # Both keys for the same reason main.py's handler emits both: PISR.tsx
    # reads `detail`, useSingleEc.tsx reads `error`.
    return JSONResponse(status_code=401,
                        content={"detail": message, "error": message})


class SessionGateMiddleware(BaseHTTPMiddleware):
    """
    Rejects un-cookied requests to gated paths with a 401.

    Registered in main.py, so it wraps everything including the StaticFiles
    mount — the path test, not the routing order, is what decides. That is
    deliberate: a new router added later is gated by existing, and someone
    would have to edit this file to open a hole.
    """

    async def dispatch(self, request: Request, call_next):
        if not AUTH.enabled:
            return await call_next(request)

        path = request.url.path
        gated = path.startswith(_GATED_PREFIXES) or path in _GATED_EXACT
        if not gated or path in _PUBLIC_PATHS:
            return await call_next(request)

        if AUTH.mode == "proxy":
            identity = _proxy_identity(request)
            if identity:
                # Read back by pisr_router to name who ran a report. This is
                # the audit trail that a shared passphrase cannot give you.
                request.state.pisr_user = identity
                return await call_next(request)
            return _denied("Not signed in — no identity from the proxy.")

        if _valid(request.cookies.get(COOKIE_NAME, "")):
            return await call_next(request)

        return _denied("Not signed in.")


# ── Routes ───────────────────────────────────────────────────────────

router = APIRouter(tags=["Auth"])


class LoginBody(BaseModel):
    passphrase: str


def _set_session_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _mint(),
        max_age=AUTH.session_seconds,
        httponly=True,     # not reachable from JS, so an XSS cannot exfiltrate it
        samesite="lax",    # a cross-site POST cannot ride the session
        secure=AUTH.cookie_secure,
        path="/",
    )


@router.get("/auth/status")
async def auth_status(request: Request):
    """
    Public. Says which gate is in front of this instance and whether the caller
    is through it — and nothing about the tenant. The SPA needs the mode to
    decide what to render when the answer is "no": a passphrase form is the
    right response in passphrase mode and a useless one in proxy mode, where
    the caller's only route in is back through the proxy.
    """
    if not AUTH.enabled:
        return {"mode": "disabled", "required": False, "authenticated": True,
                "user": None, "logoutUrl": None}

    if AUTH.mode == "proxy":
        identity = _proxy_identity(request)
        return {
            "mode": "proxy",
            "required": True,
            "authenticated": identity is not None,
            "user": identity,
            # oauth2-proxy's own sign-out, if the operator pointed us at it.
            # PISR cannot end an SSO session itself — it never held one.
            "logoutUrl": AUTH.proxy_logout_url or None,
        }

    return {
        "mode": "passphrase",
        "required": True,
        "authenticated": _valid(request.cookies.get(COOKIE_NAME, "")),
        "user": None,
        "logoutUrl": None,
    }


@router.post("/login", status_code=204)
async def login(body: LoginBody, request: Request):
    if not AUTH.enabled:
        return Response(status_code=204)

    if AUTH.mode == "proxy":
        # Not 401: the passphrase is not wrong, it is not a thing here. An
        # operator who lands on this has half-migrated a config and deserves to
        # be told which half.
        return JSONResponse(status_code=400, content={
            "detail": "This instance authenticates through an SSO proxy; "
                      "there is no passphrase to enter.",
            "error": "This instance authenticates through an SSO proxy; "
                     "there is no passphrase to enter.",
        })

    ip = _client_ip(request)
    until = _locked_until(ip)
    if until:
        retry_after = max(1, int(until - time.time()))
        logger.warning("Login rejected: %s is locked out for another %ss", ip, retry_after)
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={
                "detail": f"Too many attempts. Try again in {retry_after}s.",
                "error": f"Too many attempts. Try again in {retry_after}s.",
            },
        )

    if not hmac.compare_digest(body.passphrase.encode(), AUTH.passphrase.encode()):
        _record_failure(ip)
        logger.warning("Failed login from %s", ip)
        return JSONResponse(
            status_code=401,
            content={"detail": "Incorrect passphrase.", "error": "Incorrect passphrase."},
        )

    _clear_failures(ip)
    logger.info("Signed in from %s", ip)

    # Built here rather than mutating an injected `response`: returning a
    # Response object directly bypasses FastAPI's injected one entirely, so a
    # cookie set on that one would silently never be sent.
    signed_in = Response(status_code=204)
    _set_session_cookie(signed_in)
    return signed_in


@router.post("/logout", status_code=204)
async def logout():
    signed_out = Response(status_code=204)
    signed_out.delete_cookie(COOKIE_NAME, path="/")
    return signed_out

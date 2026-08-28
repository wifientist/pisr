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

# Built only when configured, so an unconfigured deployment never imports PyJWT
# or cryptography and never reaches Cloudflare for a key set.
_ACCESS = None
if AUTH.access_team and AUTH.access_aud:
    from cf_access import AccessVerifier
    _ACCESS = AccessVerifier(AUTH.access_team, AUTH.access_aud)

COOKIE_NAME = "pisr_session"

# Cloudflare sends the assertion both ways; the header is canonical and the
# cookie is the fallback for a request that lost it somewhere in between.
_ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
_ACCESS_COOKIE = "CF_Authorization"

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


# ── Who is this request from? ────────────────────────────────────────

def _peer_ip(request: Request) -> str:
    """The other end of the TCP connection, and the only thing not asserted."""
    return request.client.host if request.client else "unknown"


def _client_ip(request: Request) -> str:
    """
    The caller's own address, as well as it can be known.

    The peer address is the truthful answer only when the caller connected
    directly. Behind a reverse proxy the peer is the proxy, and every request
    in the world looks like it came from one address — which quietly turns the
    per-IP login throttle below into a global one: five wrong guesses from
    anyone lock out everyone, repeatably, for as long as the attacker cares to
    keep it up.

    So a forwarded header is read INSTEAD, under two conditions that have to
    hold together:

      * an operator named the header in PISR_CLIENT_IP_HEADER, and
      * the peer is inside PISR_TRUSTED_PROXY_IPS.

    Both, because a header on its own is worth nothing: if any peer could set
    it, an attacker would simply vary it per request and never be throttled at
    all — strictly worse than counting the proxy. Neither set means the peer
    address is used unchanged, which is correct for a direct deployment.

    X-Forwarded-For is a chain the proxies append to, so the entries a client
    sent itself sit at the LEFT and are forgeable. This walks from the right,
    skipping the proxies it already trusts, and takes the first address that
    is not one of them — the last hop nobody in the trusted set vouched for.
    Cf-Connecting-IP and X-Real-IP carry a single value and fall out of the
    same walk unchanged.
    """
    peer = _peer_ip(request)
    if not AUTH.client_ip_header or not _from_trusted_proxy(peer):
        return peer

    chain = request.headers.get(AUTH.client_ip_header, "")
    for candidate in reversed([c.strip() for c in chain.split(",")]):
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            # Garbage in the chain. Stop rather than skip past it: everything
            # further left is behind something unparseable and cannot be
            # reasoned about.
            break
        if not _from_trusted_proxy(candidate):
            return candidate
    return peer


def _request_is_https(request: Request) -> bool:
    """
    Did the CLIENT reach us over TLS?

    request.url.scheme is the scheme of the last hop, which behind a
    terminating proxy is the plaintext one — so on its own it would say "http"
    for a connection the user made over HTTPS, and the cookie would go out
    without Secure on exactly the deployments that most need it. So the
    forwarded scheme wins, on the same terms as the forwarded address: only
    from a peer inside PISR_TRUSTED_PROXY_IPS. An untrusted peer's header is
    ignored, which can only ever err towards marking the cookie Secure less
    often, never towards trusting a plaintext hop.
    """
    if _from_trusted_proxy(_peer_ip(request)):
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
        if forwarded:
            return forwarded.lower() == "https"
    return request.url.scheme == "https"


# ── Brute-force throttle ─────────────────────────────────────────────
#
# A shared passphrase with unlimited guesses is a passphrase with no length.
# One process, no Redis, so this is an in-memory dict — which means it resets on
# restart, and means it counts per client address as resolved above.

_attempts: Dict[str, Tuple[int, float]] = {}
_attempts_lock = Lock()
_ATTEMPTS_MAX_TRACKED = 2048


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

def _from_trusted_proxy(ip: str) -> bool:  # noqa: E302  (used by _client_ip above)
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
    # The PEER, deliberately, not _client_ip(). Who may assert an identity is
    # a question about who opened the socket; resolving a forwarded address
    # first would let a header decide whether that same header is believed.
    # A verified assertion outranks everything below it, and deliberately does
    # not consult the peer address. That is the entire point: the signature
    # proves the request came through this Access application, which is a
    # stronger claim than "it arrived from an address on the allow-list" — and
    # under rootless podman the peer address distinguishes nothing anyway.
    if _ACCESS is not None:
        token = (request.headers.get(_ACCESS_HEADER, "").strip()
                 or request.cookies.get(_ACCESS_COOKIE, "").strip())
        identity = _ACCESS.verify(token)
        if identity:
            return identity
        logger.warning(
            "No valid Cloudflare Access assertion on a request to %s. The "
            "header is %s; check Access is in front of this hostname and that "
            "PISR_ACCESS_AUD matches this application's Audience Tag.",
            request.url.path, _ACCESS_HEADER)
        return None

    ip = _peer_ip(request)
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


def proxy_preview(request: Request) -> dict:
    """
    What proxy mode WOULD make of this request, deciding nothing.

    Switching PISR_AUTH_MODE to proxy turns the passphrase off completely. Get
    it wrong — a proxy not forwarding the header, the wrong header name, a
    trusted-proxy list naming an address that is not really the peer — and
    every caller gets a 401 with no way to authenticate, including whoever
    needs to fix it. The repair is editing the env file on the box.

    So this reports each input to that decision separately, from behind the
    existing gate, while the passphrase still works. Run it before flipping and
    the flip is a formality; run it after and you are reading it over SSH.

    Behind the gate deliberately: it echoes a header value and names the peer,
    neither of which an unauthenticated caller should be handed.
    """
    peer = _peer_ip(request)
    trusted = _from_trusted_proxy(peer)
    claimed = request.headers.get(AUTH.proxy_header, "").strip()

    if _ACCESS is not None:
        token = (request.headers.get(_ACCESS_HEADER, "").strip()
                 or request.cookies.get(_ACCESS_COOKIE, "").strip())
        verified = _ACCESS.verify(token)
        return {
            "peer": peer,
            "verifyingAssertions": True,
            "issuer": _ACCESS.issuer,
            "assertionPresent": bool(token),
            "assertionVerified": verified is not None,
            "identity": verified,
            # The peer no longer decides anything, and saying so stops someone
            # "fixing" a trusted-proxy list that is not being consulted.
            "peerTrusted": trusted,
            "peerMatters": False,
            "wouldAuthenticate": verified is not None,
        }

    return {
        "verifyingAssertions": False,
        "peerMatters": True,
        "peer": peer,
        "peerTrusted": trusted,
        "trustedProxies": [str(net) for net in AUTH.trusted_proxies] or None,
        "header": AUTH.proxy_header,
        "headerPresent": bool(claimed),
        "identity": claimed or None,
        # The whole question, answered the same way the middleware answers it.
        "wouldAuthenticate": bool(trusted and claimed),
    }


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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    The headers a browser needs in order to defend the page it is given.

    Registered outermost in main.py so these land on everything — the SPA, a
    401 from the gate, a 500 from a router — rather than only on the responses
    that happened to reach a route.

    Nothing here is a substitute for the gate. These narrow what a browser will
    do with a response that has already been decided on, which is a different
    job from deciding it.
    """

    # frame-ancestors 'none' rather than X-Frame-Options: same intent, but the
    # CSP directive is the one still specified, and it is what stops the login
    # form being framed invisibly over someone else's page and typed into.
    #
    # The connect-src/img-src/style-src set is what the built SPA actually
    # uses: it talks only to its own origin, and Tailwind ships as one stylesheet
    # with inline styles from React's style props. No 'unsafe-eval', which Vite
    # needs in dev and the production bundle does not.
    _CSP = ("default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; "
            "connect-src 'self'; "
            "font-src 'self' data:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            "frame-ancestors 'none'")

    # PISR asks for none of these, so all of them are denied outright — the
    # point being that a compromised or injected bundle cannot ask either.
    # The list is broad because the app's own use of browser APIs is nil: it
    # renders a report and downloads a PDF, and neither touches a device, a
    # sensor, a payment sheet or a credential store.
    #
    # Two deliberate omissions. `clipboard-write` and `fullscreen` are not
    # denied, because they are the two a reporting tool plausibly grows — a
    # copy-the-serial button, a spectrum chart worth expanding — and a denied
    # feature fails silently in a way that looks like a bug in the feature
    # rather than a line in this list. Everything here is something whose
    # absence nobody will ever have to debug.
    _PERMISSIONS = ", ".join(f"{feature}=()" for feature in (
        "accelerometer", "ambient-light-sensor", "autoplay", "battery",
        "bluetooth", "browsing-topics", "camera", "display-capture",
        "encrypted-media", "gamepad", "geolocation", "gyroscope", "hid",
        "idle-detection", "interest-cohort", "local-fonts", "magnetometer",
        "microphone", "midi", "payment", "picture-in-picture",
        "publickey-credentials-create", "publickey-credentials-get",
        "screen-wake-lock", "serial", "speaker-selection", "sync-xhr", "usb",
        "web-share", "xr-spatial-tracking",
    ))

    _HEADERS = {
        "Content-Security-Policy": _CSP,
        "X-Content-Type-Options": "nosniff",
        # A report URL carries a venue id and a tenant id in the query string.
        # same-origin keeps those out of the Referer on any outbound link.
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": _PERMISSIONS,
        # Belt and braces for the older browsers that never learned
        # frame-ancestors.
        "X-Frame-Options": "DENY",
        # Nothing here is meant to be opened by, or opened into, another
        # origin. same-origin severs window.opener both ways, so a page that
        # somehow opens PISR cannot reach into it.
        "Cross-Origin-Opener-Policy": "same-origin",
        # And nothing here is meant to be embedded elsewhere — no image, no
        # script, no JSON. This is the resource-level counterpart to
        # frame-ancestors, covering the ways a page can pull a resource in
        # without framing it.
        "Cross-Origin-Resource-Policy": "same-origin",
    }

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in self._HEADERS.items():
            response.headers.setdefault(name, value)

        # HSTS only on a connection that was actually HTTPS. Sent over plain
        # HTTP it is ignored by a browser, but sent from a LAN deployment on
        # http://<ip>:8090 it would be a promise PISR cannot keep.
        if _request_is_https(request):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


# ── Routes ───────────────────────────────────────────────────────────

router = APIRouter(tags=["Auth"])


class LoginBody(BaseModel):
    passphrase: str


def _set_session_cookie(response: Response, request: Request) -> None:
    # Secure is set whenever the request arrived over HTTPS, whatever the
    # config says; PISR_COOKIE_SECURE=1 forces it on for the case where a
    # deployment knows it is behind TLS that this process cannot see.
    #
    # Not defaulted to True outright: a browser refuses to store a Secure
    # cookie from http://192.168.1.20:8090, and silently — the login POST
    # succeeds, no cookie is kept, and the user is bounced back to the form
    # with nothing to explain why. The LAN-over-HTTP deployment in
    # docker-compose.yml is a supported one, so it has to keep working.
    secure = AUTH.cookie_secure or _request_is_https(request)
    response.set_cookie(
        COOKIE_NAME,
        _mint(),
        max_age=AUTH.session_seconds,
        httponly=True,     # not reachable from JS, so an XSS cannot exfiltrate it
        samesite="lax",    # a cross-site POST cannot ride the session
        secure=secure,
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
    _set_session_cookie(signed_in, request)
    return signed_in


@router.post("/logout", status_code=204)
async def logout():
    signed_out = Response(status_code=204)
    signed_out.delete_cookie(COOKIE_NAME, path="/")
    return signed_out

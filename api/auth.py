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

ROLES. Both modes resolve one of two: `admin` or `user`. An admin edits the
section visibility policy and is never subject to it; a user sees the report
with the hidden sections removed. This is decoration on top of the gate, not a
second gate — both roles are fully authenticated, and the difference between
them is which cards a report contains, never whether they may have one.

  In PROXY mode the role comes from the verified identity against
  PISR_ADMIN_EMAILS. That is the real deployment, and it is the only one where
  the role means anything durable: revoking someone is an env change, and the
  assertion is signed, so the role cannot be asserted by the caller.

  In PASSPHRASE mode the role comes from WHICH passphrase was used — see
  `_role_keys`, which puts it in the signing key rather than in the cookie.
  This exists so the split can be exercised in development, where there is no
  identity provider. It is not a way to run two tiers of staff on one shared
  secret: a shared secret cannot be revoked for one person, and the user
  passphrase will be in a group chat by Thursday.

  In ACCOUNTS mode the role is stored per account, in the file. It is the
  first mode where an admin can promote and revoke somebody without an env
  change and a restart.

ACCOUNTS MODE. Local per-person logins, added when Cloudflare Access stopped
being usable — its one-time-PIN mail is silently discarded by two of three
customer domains, and every mail-based scheme inherits that failure. An admin
creates an account, PISR mints a single-use enrolment link, and the admin
delivers it OUT OF BAND. Nothing has to arrive by email at the moment somebody
is trying to sign in. See `api/accounts.py` for the store and the argument
about what it is allowed to hold.

  POST /api/login    {"username": "...", "password": "..."} -> 204 + Set-Cookie
  GET  /api/enroll/{token}   is this link good, and whose is it
  POST /api/enroll           redeem it and set a password
  POST /api/account/password change your own password

  THE SESSION KEY INCLUDES THE STORED HASH. `_account_key` derives from the
  session secret, the role, the account id AND the password hash, which buys
  three things for free: changing a password ends that person's other
  sessions, disabling or deleting an account ends them immediately, and a role
  change re-keys rather than being carried in a payload somebody could edit.
  This is `_signing_key`'s trick applied per person instead of per passphrase.

  THE LOGIN PAGE IS NOW THE PERIMETER. Under Access there were two gates and
  this was the inner one. In accounts mode there is one, so the throttle below
  is load-bearing rather than a nicety — read its comment before changing it.

  BREAK-GLASS. PISR_AUTH_ADMIN_PASSPHRASE still signs in as an admin in this
  mode, and that is a deliberate exception to the rule proxy mode states above
  ("leaving both doors open makes the shared secret a way around SSO"). The
  difference is where the identity lives. Proxy mode's IDP is external and is
  still there when PISR's volume is not; accounts mode's identities are a file
  ON that volume, so losing it — or deleting the last admin — would otherwise
  need an SSH session to a box that is deliberately awkward to SSH into. It is
  optional, off unless set, and main.py says so loudly at every startup.

Everything under /api (and /docs, /redoc, /openapi.json) requires proof in
whichever form the mode calls for. The static SPA bundle is deliberately NOT
gated: it contains no tenant data, and it has to load in order to render the
login form. That is also why the role is never a secret the frontend keeps —
`require_admin` on the route is the check, and what the bundle chooses to
render is a convenience for the person using it.
"""

import hmac
import ipaddress
import logging
import time
from hashlib import sha256
from threading import Lock
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, Response
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

# The two roles. Deliberately two: "admin" edits the visibility policy and is
# never subject to it, "user" is everyone else and sees what the policy allows.
# A third would be a new entry in visibility.MANAGED_ROLES and a column in the
# portal — but resist it until something actually needs one. Per-person
# visibility is a permissions system, and a permissions system wants the user
# table this tool deliberately does not have.
ROLE_ADMIN = "admin"
ROLE_USER = "user"

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
_PUBLIC_PATHS = {"/api/login", "/api/logout", "/api/auth/status", "/api/enroll"}

# Enrolment, which has to be reachable by somebody who by definition has no
# session yet — that is what they are here to obtain. The token in the URL is
# the credential.
#
# A PREFIX, because the token is a path segment. Kept deliberately short and
# specific: this is the only place in the file where a prefix opens a hole, and
# a wider one ("/api/enrol", say, or a trailing-slash-less "/api/enroll" that
# also matched "/api/enrollments") would open more than it means to. The routes
# behind it are the only unauthenticated write path in PISR and they carry
# their own throttle.
_PUBLIC_PREFIXES = ("/api/enroll/",)


# ── Session token ────────────────────────────────────────────────────

def _signing_key(passphrase: str) -> bytes:
    """
    Passphrase mode only — proxy mode mints no cookies.

    Derived from the session secret AND the passphrase, so that changing the
    passphrase invalidates every outstanding session. Without this, rotating a
    leaked passphrase would lock out the wrong people — the attacker holding a
    valid cookie would keep their access, and only honest users would notice.
    """
    return hmac.new(
        AUTH.session_secret.encode(), passphrase.encode(), sha256
    ).digest()


def _role_keys() -> Tuple[Tuple[str, bytes], ...]:
    """
    Each role that can hold a session, paired with the key that signs for it.

    THIS IS HOW THE ROLE IS CARRIED, and it is worth understanding rather than
    simplifying. The obvious design puts `role=admin` in the cookie payload and
    signs the lot; this puts the role in the KEY instead, so the cookie says
    only when it expires and there is no role field to tamper with. Verifying
    means trying each key and seeing which one the signature belongs to — a
    user cookie simply does not verify under the admin key, so holding one
    passphrase cannot mint a session for the other.

    It also preserves the property `_signing_key` exists for, per role: rotating
    the admin passphrase ends every admin session and leaves user sessions
    alone, which is what you want at 2am when one of the two has leaked.

    With no admin passphrase set, there is one door and it is the operator's.
    That is the historical behaviour of this mode and the right default for a
    single-operator LAN instance — the alternative, making the lone passphrase
    a *user*, would leave an instance where the admin portal is unreachable and
    nothing can ever be unhidden.
    """
    if AUTH.admin_passphrase:
        return ((ROLE_ADMIN, _signing_key(AUTH.admin_passphrase)),
                (ROLE_USER, _signing_key(AUTH.passphrase)))
    return ((ROLE_ADMIN, _signing_key(AUTH.passphrase)),)


# The account id the break-glass passphrase signs in as. Reserved: real ids are
# "u_" + a random suffix and usernames cannot contain "!", so nothing in the
# accounts file can collide with it.
BREAKGLASS_ID = "!breakglass"
BREAKGLASS_NAME = "break-glass (passphrase)"


def _account_key(account) -> bytes:
    """
    The key that signs one person's session.

    Derived from the session secret, the role, the account id AND the stored
    password hash. That last ingredient is the interesting one: it means the
    key changes whenever the password does, so a password change silently ends
    every other session that person had — the property `_signing_key` gives
    passphrase mode, here given per person.

    It also means a disabled or deleted account cannot present a working
    cookie, because `_valid_account` looks the account up before it can derive
    a key at all. Revocation is immediate rather than "at the next expiry",
    which is the thing a shared passphrase could never offer.

    The role is in the key rather than the payload for the same reason it is in
    passphrase mode: there is then no role field for anyone to edit, and a
    demoted admin's outstanding cookie stops verifying instead of staying
    admin until it expires.
    """
    material = f"{account.role}:{account.id}:{account.hash or ''}"
    return hmac.new(AUTH.session_secret.encode(), material.encode(), sha256).digest()


def _mint_account(account, now: Optional[float] = None) -> str:
    """A session cookie for one account. Payload is `<id>.<expires>`."""
    expires = int((now or time.time()) + AUTH.session_seconds)
    payload = f"{account.id}.{expires}"
    sig = hmac.new(_account_key(account), payload.encode(), sha256).hexdigest()
    return f"{payload}.{sig}"


def _mint_breakglass(now: Optional[float] = None) -> str:
    """A session cookie for the env-configured emergency admin."""
    expires = int((now or time.time()) + AUTH.session_seconds)
    payload = f"{BREAKGLASS_ID}.{expires}"
    key = _signing_key(AUTH.admin_passphrase)
    sig = hmac.new(key, payload.encode(), sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid_account(token: str) -> Optional[Tuple[str, str]]:
    """
    The (identity, role) this cookie proves in accounts mode, or None.

    Every failure is None and none of them are distinguished to the caller: a
    malformed cookie, an unknown id, a deleted account, a disabled one, a
    rotated password and an expired session all mean "sign in again".

    Note that the account is looked up BEFORE the signature can be checked,
    because the account is where the key comes from. That is not a
    verify-then-trust inversion — nothing is trusted on the strength of the
    lookup, and an attacker who names a real id still cannot forge a signature
    without the hash. It does mean the timing differs between a real id and a
    made-up one, which is not worth defending: account ids are opaque random
    strings that the holder of a valid cookie already knows.
    """
    if not token:
        return None
    rest, _, sig = token.rpartition(".")
    uid, _, expires_raw = rest.partition(".")
    if not uid or not expires_raw or not sig:
        return None

    if uid == BREAKGLASS_ID:
        if not AUTH.admin_passphrase:
            # The passphrase was withdrawn while a cookie was outstanding.
            # Withdrawing it has to end those sessions, or it withdraws
            # nothing.
            return None
        key = _signing_key(AUTH.admin_passphrase)
        identity, role = BREAKGLASS_NAME, ROLE_ADMIN
    else:
        import accounts  # local: config is validated before this module loads
        account = accounts.STORE.by_id(uid)
        if account is None or not account.can_sign_in:
            return None
        key = _account_key(account)
        identity, role = account.username, account.role

    expected = hmac.new(key, rest.encode(), sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None

    try:
        if int(expires_raw) <= time.time():
            return None
    except ValueError:
        return None
    return (identity, role)


def _mint(role: str, now: Optional[float] = None) -> str:
    expires = int((now or time.time()) + AUTH.session_seconds)
    payload = str(expires)
    key = dict(_role_keys()).get(role)
    if key is None:
        # Unreachable from login(), which only ever mints a role it just
        # matched a passphrase for. Refusing beats minting an unverifiable
        # cookie that would send someone back to the form with no explanation.
        raise ValueError(f"no signing key for role {role!r}")
    sig = hmac.new(key, payload.encode(), sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid(token: str) -> Optional[str]:
    """
    The role this cookie proves, or None.

    Signature first, then expiry, and every configured key is tried before
    giving up — compare_digest on each, so a near-miss costs the same as a
    wild one. Expiry is checked after the signature because an unsigned token
    has no trustworthy expiry to read.
    """
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")

    matched: Optional[str] = None
    for role, key in _role_keys():
        expected = hmac.new(key, payload.encode(), sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            matched = role
            # No break: with two keys configured the loop is two HMACs either
            # way, and a constant number of them keeps the timing of "admin
            # cookie" and "user cookie" indistinguishable.
    if matched is None:
        return None

    try:
        return matched if int(payload) > time.time() else None
    except ValueError:
        return None


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
# A password with unlimited guesses is a password with no length. One process,
# no Redis, so this is an in-memory dict: it resets on restart, which is a real
# weakness and the reason there is a length floor in config.py at all.
#
# TWO KINDS OF KEY, WITH DIFFERENT POLICIES, and the difference is the point.
#
#   ip:<addr>    the client address as `_client_ip` resolves it
#   user:<name>  the username offered, in accounts mode
#
# The second exists because the first BARELY WORKS HERE. Under rootless podman
# publishing a port rewrites the source address, so every caller arrives from
# inside podman's own network and `ip:` collapses to a single global counter —
# CLAUDE.md documents this at length. Counting per username is the only key
# that distinguishes anything in that deployment.
#
# HARD LOCKOUT vs BACKOFF is then forced by that same fact:
#
#   * PASSPHRASE mode keeps the historical hard lockout — N wrong guesses and
#     that key refuses for `lockout_seconds`. It is one shared secret on a LAN
#     instance and the behaviour is long-standing.
#
#   * ACCOUNTS mode uses EXPONENTIAL BACKOFF on both keys and never refuses
#     outright. A hard lockout here would be an outage switch, twice over: on
#     the `ip:` key, where every caller looks the same, five wrong guesses from
#     anyone would lock out everyone; and on the `user:` key, where anybody
#     could lock out a named person on purpose just by guessing at them. This
#     mode's login page is directly internet-facing, so both are things people
#     would actually do.
#
# Backoff is enough: the delay doubles to a minute, scrypt costs ~60ms a try,
# and the floor is twelve characters. What it will not do is hand a stranger a
# way to take the tool away from the people using it.

_BACKOFF_CAP_SECONDS = 60

# Failures a key may spend before backoff starts biting, by prefix.
#
# `user:` gets none. A delay on one username is paid only by whoever is
# guessing at it, and the real owner avoids it by knowing their own password.
#
# `ip:` and `enroll:` get an allowance, because under rootless podman they are
# shared: every caller arrives from the same apparent address, so a colleague
# mistyping a password would otherwise slow everybody else down. The allowance
# is what keeps ordinary fumbling free while still catching the attack this key
# exists for — spraying one password across many usernames, which the `user:`
# key cannot see because it only ever counts one failure per account.
_FREE_ATTEMPTS = {"ip": 5, "enroll": 3, "user": 0}


def _free_attempts(key: str) -> int:
    return _FREE_ATTEMPTS.get(key.split(":", 1)[0], 0)

# How long a failure is REMEMBERED, as distinct from how long it blocks. These
# must differ for backoff to work at all: if the count reset as soon as the
# (short) block expired, the delay would return to one second after every wait
# and never grow. The count is forgotten after `lockout_seconds`.

# count, blocked-until, forget-at
_attempts: Dict[str, Tuple[int, float, float]] = {}
_attempts_lock = Lock()
_ATTEMPTS_MAX_TRACKED = 2048


def _uses_backoff() -> bool:
    return AUTH.mode == "accounts"


def _locked_until(key: str) -> float:
    """When this key may try again, or 0.0 if it may try now."""
    now = time.time()
    with _attempts_lock:
        count, blocked, _ = _attempts.get(key, (0, 0.0, 0.0))
    if blocked <= now:
        return 0.0
    if _uses_backoff():
        # No count threshold: every failure delays the next attempt, and the
        # delay is capped, so there is no number of failures at which this
        # refuses outright.
        return blocked
    return blocked if count >= AUTH.max_attempts else 0.0


def _record_failure(*keys: str) -> None:
    """
    Count one failure against every key given.

    Variadic because accounts mode always records against BOTH the address and
    the username — recording only one of them would leave the other as an
    unthrottled way to make the same guesses.
    """
    for key in keys:
        _record_one_failure(key)


def _record_one_failure(key: str) -> None:
    now = time.time()
    with _attempts_lock:
        # Bounded: a flood of varied usernames or spoofed sources should not
        # grow this without limit. Drop everything already forgotten, then
        # everything at all if that was not enough.
        if len(_attempts) >= _ATTEMPTS_MAX_TRACKED:
            for stale in [k for k, (_, _, f) in _attempts.items() if f <= now]:
                del _attempts[stale]
            if len(_attempts) >= _ATTEMPTS_MAX_TRACKED:
                _attempts.clear()

        count, _, forget_at = _attempts.get(key, (0, 0.0, 0.0))
        count = 1 if forget_at and forget_at <= now else count + 1
        if _uses_backoff():
            over = count - _free_attempts(key)
            blocked = (now + min(2 ** (over - 1), _BACKOFF_CAP_SECONDS)
                       if over > 0 else 0.0)
        else:
            blocked = now + AUTH.lockout_seconds
        _attempts[key] = (count, blocked, now + AUTH.lockout_seconds)


def _clear_failures(*keys: str) -> None:
    with _attempts_lock:
        for key in keys:
            _attempts.pop(key, None)


def _throttled(keys: Tuple[str, ...]) -> Optional[JSONResponse]:
    """
    A 429 if any of these keys is waiting out a delay, else None.

    The message names the wait but never which key caused it. Saying "this
    username is throttled" would confirm the username exists, which is the one
    thing the login route works to avoid telling anyone.
    """
    until = max((_locked_until(key) for key in keys), default=0.0)
    if not until:
        return None
    retry_after = max(1, int(until - time.time()))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(retry_after)},
        content={
            "detail": f"Too many attempts. Try again in {retry_after}s.",
            "error": f"Too many attempts. Try again in {retry_after}s.",
        },
    )


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


def _role_for_identity(identity: Optional[str]) -> str:
    """
    Admin or user, for a caller PISR actually knows the name of.

    Case-folded on both sides. An identity provider's idea of an address's case
    is not stable — Entra will hand back the casing a user typed at enrolment —
    and losing admin to a capital letter is the kind of failure nobody
    diagnoses quickly.

    An empty admin list means nobody is an admin, which is the safe direction:
    the alternative reading, "unset means everyone", would promote an entire
    corporate directory on the day proxy mode is switched on.
    """
    if not identity:
        return ROLE_USER
    return ROLE_ADMIN if identity.casefold() in AUTH.admin_emails else ROLE_USER


def identity_and_role(request: Request) -> Optional[Tuple[Optional[str], str]]:
    """
    Who this request is and what they may see, or None if it is not signed in.

    The single answer to that question — the middleware, /api/auth/status and
    the admin routes all come through here, so there is no second opinion to
    drift. The identity is None in passphrase mode by definition: a shared
    secret proves someone knew it, never who.
    """
    if not AUTH.enabled:
        # PISR_AUTH_DISABLED. One caller, who is standing at the machine;
        # giving them the user role would hide sections from the only person
        # in a position to unhide them.
        return (None, ROLE_ADMIN)

    if AUTH.mode == "proxy":
        identity = _proxy_identity(request)
        if not identity:
            return None
        return (identity, _role_for_identity(identity))

    if AUTH.mode == "accounts":
        # Names a real person, so the audit trail this returns is worth
        # something for the first time outside proxy mode — `updatedBy` on a
        # policy save, and the line naming who ran a report.
        return _valid_account(request.cookies.get(COOKIE_NAME, ""))

    role = _valid(request.cookies.get(COOKIE_NAME, ""))
    if not role:
        return None
    return (None, role)


def role_of(request: Request) -> str:
    """
    The role the gate decided for this request, for a route to act on.

    Read back from request.state rather than recomputed: the middleware has
    already done the work, and a route that re-derived it could disagree with
    the gate that let the request through. Falls back to `user` — the least
    that can be seen — for the case that should not happen, a gated route
    somehow reached without the middleware having run.
    """
    return getattr(request.state, "pisr_role", None) or ROLE_USER


def require_admin(request: Request) -> str:
    """
    FastAPI dependency: 403 unless this caller is an admin.

    403 and not 404. Hiding the route's existence would be pointless — it is in
    the SPA bundle, which is served unauthenticated — and a plain "you are not
    an admin" is what stops someone spending an afternoon on a bug that is
    actually a missing entry in PISR_ADMIN_EMAILS.
    """
    role = role_of(request)
    if role != ROLE_ADMIN:
        raise HTTPException(
            status_code=403,
            detail="This needs the admin role. In SSO mode that means being "
                   "named in PISR_ADMIN_EMAILS; in accounts mode it means an "
                   "account whose role is admin; in passphrase mode it means "
                   "signing in with PISR_AUTH_ADMIN_PASSPHRASE.")
    return role


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
            # Which role this identity would land in, so "why can I not see
            # the admin portal" is answerable before the switch rather than
            # after. Named separately from the count so a preview against an
            # empty list reads as "no admins configured", not "not you".
            "wouldBeRole": _role_for_identity(verified) if verified else None,
            "adminsConfigured": len(AUTH.admin_emails),
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
        "wouldBeRole": _role_for_identity(claimed) if (trusted and claimed) else None,
        "adminsConfigured": len(AUTH.admin_emails),
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
        if not gated or path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        resolved = identity_and_role(request)
        if resolved is None:
            return _denied("Not signed in — no identity from the proxy."
                           if AUTH.mode == "proxy" else "Not signed in.")

        identity, role = resolved
        # Read back by pisr_router to name who ran a report. This is the audit
        # trail that a shared passphrase cannot give you.
        request.state.pisr_user = identity
        # And read back by every route that renders a report, to decide which
        # sections that report is allowed to contain. Set HERE rather than in
        # each route so that a router added later is role-aware by existing,
        # the same way it is gated by existing.
        request.state.pisr_role = role
        return await call_next(request)


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
    # Optional so that one route serves both modes. Passphrase mode reads
    # `passphrase`; accounts mode reads the other two. A body carrying the
    # wrong pair for the mode is a 400 with the reason, not a 401 — the caller
    # has half-migrated a config, not failed to authenticate.
    passphrase: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class EnrollBody(BaseModel):
    token: str
    password: str


class PasswordBody(BaseModel):
    current: str
    new: str


def _write_session_cookie(response: Response, request: Request, token: str) -> None:
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
        token,
        max_age=AUTH.session_seconds,
        httponly=True,     # not reachable from JS, so an XSS cannot exfiltrate it
        samesite="lax",    # a cross-site POST cannot ride the session
        secure=secure,
        path="/",
    )


def _set_session_cookie(response: Response, request: Request, role: str) -> None:
    """Passphrase mode's cookie, where the role is the whole identity."""
    _write_session_cookie(response, request, _mint(role))


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
                "user": None, "role": ROLE_ADMIN, "logoutUrl": None}

    resolved = identity_and_role(request)
    identity, role = resolved if resolved else (None, None)

    if AUTH.mode == "proxy":
        return {
            "mode": "proxy",
            "required": True,
            "authenticated": resolved is not None,
            "user": identity,
            # The SPA shows the admin portal on the strength of this. It is a
            # convenience, not a control: the bundle is served unauthenticated
            # and anyone can read what it would render. require_admin on the
            # route is what actually decides.
            "role": role,
            # oauth2-proxy's own sign-out, if the operator pointed us at it.
            # PISR cannot end an SSO session itself — it never held one.
            "logoutUrl": AUTH.proxy_logout_url or None,
        }

    if AUTH.mode == "accounts":
        import accounts
        return {
            "mode": "accounts",
            "required": True,
            "authenticated": resolved is not None,
            # A real name, in a mode that has one. This is what the sign-out
            # chip shows and what the audit lines record.
            "user": identity,
            "role": role,
            "logoutUrl": None,
            # So the login form can say "no accounts exist yet, run the CLI"
            # rather than letting somebody guess at a username on a fresh
            # instance forever. Deliberately not a count — the number of
            # accounts is not an unauthenticated caller's business, only
            # whether the instance has been set up at all.
            "setupNeeded": (not accounts.STORE.broken
                            and not accounts.STORE.list()),
            # Whether there is a break-glass door to offer. Not a secret: the
            # form has to know whether to show a passphrase field, and its
            # absence is inferable from the field's absence anyway.
            "breakGlass": bool(AUTH.admin_passphrase),
        }

    return {
        "mode": "passphrase",
        "required": True,
        "authenticated": resolved is not None,
        "user": None,
        "role": role,
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

    if AUTH.mode == "accounts":
        return _login_with_account(body, request, ip)

    if body.passphrase is None:
        return JSONResponse(status_code=400, content={
            "detail": "This instance signs in with a passphrase, not a username.",
            "error": "This instance signs in with a passphrase, not a username.",
        })

    throttled = _throttled((f"ip:{ip}",))
    if throttled is not None:
        logger.warning("Login rejected: %s is locked out", ip)
        return throttled

    # Both compares run unconditionally rather than short-circuiting on the
    # first match, so the time this takes does not say which passphrase was
    # tried. compare_digest against an empty admin passphrase is cheap and
    # never matches a submission, which the length floor in config.py rules out.
    offered = body.passphrase.encode()
    is_admin_pass = bool(AUTH.admin_passphrase) and hmac.compare_digest(
        offered, AUTH.admin_passphrase.encode())
    is_user_pass = hmac.compare_digest(offered, AUTH.passphrase.encode())

    if not (is_admin_pass or is_user_pass):
        _record_failure(f"ip:{ip}")
        logger.warning("Failed login from %s", ip)
        return JSONResponse(
            status_code=401,
            content={"detail": "Incorrect passphrase.", "error": "Incorrect passphrase."},
        )

    # With no admin passphrase configured there is one door and it is the
    # operator's — see _role_keys. With one configured, the ordinary passphrase
    # is a user and this second one is the admin.
    role = ROLE_ADMIN if (is_admin_pass or not AUTH.admin_passphrase) else ROLE_USER

    _clear_failures(f"ip:{ip}")
    logger.info("Signed in from %s as %s", ip, role)

    # Built here rather than mutating an injected `response`: returning a
    # Response object directly bypasses FastAPI's injected one entirely, so a
    # cookie set on that one would silently never be sent.
    signed_in = Response(status_code=204)
    _set_session_cookie(signed_in, request, role)
    return signed_in


# The one message every failed account sign-in returns, whatever went wrong.
#
# "No such user", "wrong password", "never enrolled" and "disabled" are all
# this string, because each of the others would confirm whether a username
# exists — and the login page is now on the open internet. The log line says
# which it was; the response does not.
_SIGNIN_FAILED = ("Incorrect username or password.")


def _login_with_account(body: LoginBody, request: Request, ip: str):
    """
    Accounts mode's sign-in. Also the break-glass passphrase's door.

    Ordered so that every path costs about the same: the throttle is checked
    first, then exactly one scrypt verification happens — against the real hash
    if the account exists, against a dummy one if it does not. Returning early
    on an unknown username would make this route an account enumerator that
    anybody could read with a stopwatch.
    """
    import accounts  # local: config is validated before this module loads

    # Break-glass, checked before the accounts file is consulted at all — the
    # whole point of it is to work when that file is missing or unreadable.
    if AUTH.admin_passphrase and body.passphrase:
        throttled = _throttled((f"ip:{ip}",))
        if throttled is not None:
            return throttled
        if hmac.compare_digest(body.passphrase.encode(),
                               AUTH.admin_passphrase.encode()):
            _clear_failures(f"ip:{ip}")
            logger.warning(
                "BREAK-GLASS sign-in from %s using PISR_AUTH_ADMIN_PASSPHRASE. "
                "This bypasses the accounts file entirely. If this was not you, "
                "rotate that passphrase now.", ip)
            signed_in = Response(status_code=204)
            _write_session_cookie(signed_in, request, _mint_breakglass())
            return signed_in
        _record_failure(f"ip:{ip}")
        logger.warning("Failed break-glass login from %s", ip)
        return JSONResponse(status_code=401,
                            content={"detail": _SIGNIN_FAILED, "error": _SIGNIN_FAILED})

    username = accounts.normalise_username(body.username or "")
    password = body.password or ""
    if not username or not password:
        return JSONResponse(status_code=400, content={
            "detail": "A username and password are required.",
            "error": "A username and password are required.",
        })

    keys = (f"ip:{ip}", f"user:{username}")
    throttled = _throttled(keys)
    if throttled is not None:
        logger.warning("Login rejected: %s / %r is backing off", ip, username)
        return throttled

    account = accounts.STORE.by_username(username)
    if account is None or not account.can_sign_in:
        # Spend the same time a real verification would. Without this the
        # response for an unknown user comes back in microseconds and a real
        # one takes ~60ms, which is a difference measurable over the internet.
        accounts.burn_dummy_hash()
        _record_failure(*keys)
        logger.warning(
            "Failed login from %s for %r (%s)", ip, username,
            "no such account" if account is None
            else "disabled" if account.disabled else "never enrolled")
        return JSONResponse(status_code=401,
                            content={"detail": _SIGNIN_FAILED, "error": _SIGNIN_FAILED})

    if not accounts.verify_password(password, account.hash):
        _record_failure(*keys)
        logger.warning("Failed login from %s for %r (wrong password)", ip, username)
        return JSONResponse(status_code=401,
                            content={"detail": _SIGNIN_FAILED, "error": _SIGNIN_FAILED})

    # The one moment the plaintext exists to rehash from. Best-effort inside,
    # so a read-only volume costs an upgrade and not the sign-in.
    if accounts.needs_rehash(account.hash):
        accounts.STORE.upgrade_hash(account.id, password)
        account = accounts.STORE.by_id(account.id) or account

    _clear_failures(*keys)
    logger.info("Signed in from %s as %s (%s)", ip, account.username, account.role)

    signed_in = Response(status_code=204)
    _write_session_cookie(signed_in, request, _mint_account(account))
    return signed_in


@router.get("/enroll/{token}")
async def enroll_check(token: str, request: Request):
    """
    Public. Is this enrolment link good, and whose is it?

    Returns the username so the form can show whose account is being set up —
    somebody handed a link out of band deserves to see they were given the
    right one. That is not a leak: holding the token already proves far more
    than the username does.

    THE THROTTLE STANDS IN FRONT OF FAILURES ONLY, and that ordering is the
    whole point of it. A valid token is answered whatever anybody else has been
    doing, because under rootless podman every caller shares one apparent
    address — so a throttle checked BEFORE the lookup would let one person
    clicking a stale link delay everybody else's enrolment, up to the cap,
    indefinitely if they kept at it. That is exactly the "off switch for
    strangers" the login throttle is written to avoid.

    Throttling failures is still worth doing, but note how little it is
    defending: the token is 256 random bits, so it cannot be guessed, and an
    invalid one never reaches scrypt — `redeem_invite` refuses before it
    hashes. This is abuse-limiting on an unauthenticated route, not a guard
    against guessing.
    """
    if AUTH.mode != "accounts":
        raise HTTPException(status_code=404, detail="Not found.")

    import accounts

    ip = _client_ip(request)
    account = accounts.STORE.find_by_invite(token)
    if account is None or account.disabled:
        throttled = _throttled((f"enroll:{ip}",))
        if throttled is not None:
            return throttled
        _record_failure(f"enroll:{ip}")
        raise HTTPException(
            status_code=404,
            detail="That enrolment link is not valid. Ask whoever sent it for "
                   "a new one.")
    if account.invite.expired:
        raise HTTPException(
            status_code=410,
            detail="That enrolment link has expired. Ask whoever sent it for a "
                   "new one.")

    return {
        "username": account.username,
        "expiresAt": account.invite.expires_at,
        "minPasswordLength": AUTH.min_password_length,
        # True when this is a reset rather than a first enrolment, so the form
        # can say "choose a new password" instead of "welcome".
        "reset": account.enrolled,
    }


@router.post("/enroll", status_code=204)
async def enroll(body: EnrollBody, request: Request):
    """
    Public. Redeem an enrolment link, set a password, and sign in.

    Signing in on success is deliberate: the alternative bounces somebody who
    has just chosen a password to a form asking for it, which reads as though
    the enrolment failed. The invite is consumed in the same write.

    Throttled on failure only, for the reason `enroll_check` sets out at
    length — and here it matters more, because a rejection is often the
    invitee's own password being too short. Making them wait longer each time
    they get their OWN password wrong, on a shared address, would be a
    self-inflicted lockout on the one flow they have to complete.
    """
    if AUTH.mode != "accounts":
        raise HTTPException(status_code=404, detail="Not found.")

    import accounts

    ip = _client_ip(request)

    # Whether the TOKEN is good, asked separately from whether the password is,
    # so the two failures can be told apart. A password below the floor is the
    # invitee's own slip and must not count as abuse — throttling it would make
    # somebody who mistyped their new password wait longer to fix it, on a
    # shared address, during the one flow they cannot skip. Only a bad token is
    # counted. (`redeem_invite` re-checks the token inside its own write, so
    # this lookup is for classification, not for trust.)
    known = accounts.STORE.find_by_invite(body.token)
    token_is_good = known is not None and not known.disabled and not known.invite.expired

    try:
        account = accounts.STORE.redeem_invite(body.token, body.password)
    except accounts.AccountsError as exc:
        if not token_is_good:
            throttled = _throttled((f"enroll:{ip}",))
            if throttled is not None:
                return throttled
            _record_failure(f"enroll:{ip}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _clear_failures(f"enroll:{ip}", f"user:{account.username}")
    logger.info("Enrolled %s from %s", account.username, ip)

    enrolled = Response(status_code=204)
    _write_session_cookie(enrolled, request, _mint_account(account))
    return enrolled


@router.post("/account/password", status_code=204)
async def change_password(body: PasswordBody, request: Request):
    """
    Behind the gate: change your own password.

    Re-issues the cookie, because changing the password changes the signing key
    — see `_account_key`. Without that the person who just changed it would be
    signed out by their own action while every OTHER session of theirs also
    ended, which is right for the others and baffling for this one.
    """
    if AUTH.mode != "accounts":
        raise HTTPException(
            status_code=400,
            detail="This instance does not use per-person passwords.")

    import accounts

    identity = getattr(request.state, "pisr_user", None)
    if identity == BREAKGLASS_NAME:
        raise HTTPException(
            status_code=400,
            detail="The break-glass session has no account to change. It is "
                   "PISR_AUTH_ADMIN_PASSPHRASE in the environment; change it "
                   "there.")

    account = accounts.STORE.by_username(identity or "")
    if account is None:
        raise HTTPException(status_code=404, detail="No such account.")

    # The current password is required even though the session already proves
    # who this is. A session is not a password: it may be an unlocked laptop,
    # and the whole value of a password change is that it locks out whoever
    # should not have been there.
    if not accounts.verify_password(body.current, account.hash):
        ip = _client_ip(request)
        _record_failure(f"user:{account.username}")
        logger.warning("Failed password change for %s from %s",
                       account.username, ip)
        raise HTTPException(status_code=401, detail="That is not your current password.")

    try:
        updated = accounts.STORE.set_password(account.id, body.new)
    except accounts.AccountsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    changed = Response(status_code=204)
    _write_session_cookie(changed, request, _mint_account(updated))
    return changed


@router.post("/logout", status_code=204)
async def logout():
    signed_out = Response(status_code=204)
    signed_out.delete_cookie(COOKIE_NAME, path="/")
    return signed_out

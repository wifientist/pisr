"""
PISR configuration — the pseudo-controller, read from .env.

In rtools2 this was a `Controller` row in Postgres holding Fernet-encrypted
credentials, selected per-user from a dropdown. Standalone PISR serves exactly
one RUCKUS ONE tenant, so the row becomes a frozen dataclass built from the
environment at import time.

It is loaded eagerly and validated strictly. A container that refuses to start
saying `R1_TENANT_ID is not set` is worth far more than one that boots happily
and 500s on every request.

Every value below can also be supplied out-of-band as `<NAME>_FILE` pointing at
a file to read. That is what lets a secret arrive as a Docker/Compose secret
mounted at /run/secrets/... instead of an environment variable — env vars are
readable via `docker inspect` and /proc/<pid>/environ by anyone with access to
the daemon, a mounted file is not.
"""

import ipaddress
import os
import secrets as _secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

load_dotenv()

# R1Client's vocabulary, not the old Controller model's. `client.py` maps
# EU -> api.eu.ruckus.cloud, ASIA -> api.asia.ruckus.cloud and falls through to
# NA for anything else — so the old model's "APAC" would have silently reported
# on the wrong cloud. Rejecting it here is the point.
REGIONS = {"NA", "EU", "ASIA"}

# Compared as an exact string in three places (fetch._get, fetch._post,
# MspService.get_msp_ecs). A lowercase "msp" would silently drop the
# x-rks-tenantid header and report on the MSP's own venue-less tenant — an
# empty report with no error anywhere. Hence the .upper() and the membership
# check below.
EC_TYPES = {"EC", "MSP"}

# The floor on PISR_AUTH_PASSPHRASE. Low, deliberately: the real brake on
# guessing is the per-IP lockout in auth.py, not length. Its one weakness is
# that it lives in memory and resets when the container restarts — which is why
# there is a floor at all rather than none.
MIN_PASSPHRASE_LENGTH = 10


@dataclass(frozen=True)
class ControllerConfig:
    """What a Controller row used to hold, minus the encryption and the DB."""

    id: int
    name: str
    tenant_id: str
    client_id: str
    shared_secret: str
    region: str   # NA | EU | ASIA
    ec_type: str  # EC | MSP
    controller_type: str = "RuckusONE"


@dataclass(frozen=True)
class AuthConfig:
    """
    The gate in front of the whole app, in one of two modes.

    `passphrase` — the default, and self-contained. One shared passphrase
    exchanges for a signed HttpOnly session cookie; every /api route requires
    that cookie. No accounts, no roles, no registration, no dependency on
    anything outside this process. The right size for one tool on one tenant.

    `proxy` — the SSO story. An authenticating reverse proxy in front (usually
    oauth2-proxy against Entra, Okta or Google) does the OIDC dance and
    forwards the caller's identity in a header. PISR reads that header and
    implements no OIDC of its own. The passphrase is switched OFF entirely in
    this mode: leaving both doors open would mean the shared secret is a way
    around SSO, which would defeat the audit trail and the revocation story
    that were the reasons to adopt SSO.

    `enabled=False` is reachable only by setting PISR_AUTH_DISABLED=1 on
    purpose. There is no accidental path to an open instance.
    """

    enabled: bool
    mode: str  # "passphrase" | "proxy"

    # passphrase mode
    passphrase: str
    session_secret: str
    session_seconds: int
    cookie_secure: bool
    max_attempts: int
    lockout_seconds: int

    # proxy mode
    proxy_header: str
    proxy_logout_url: str

    # Both modes. In proxy mode this list is the whole of the security — only
    # these peers may assert an identity. In passphrase mode it is narrower:
    # it says whose `client_ip_header` may be believed when working out who a
    # request is really from, which is what the login throttle counts against.
    trusted_proxies: Tuple[ipaddress._BaseNetwork, ...]
    client_ip_header: str


def _env(name: str) -> str:
    """
    Read `name`, preferring the contents of the file at `<name>_FILE`.

    The file form wins when both are present: if someone has gone to the
    trouble of mounting a secret, an env var left over from an earlier
    deployment should not silently take precedence over it.
    """
    path = (os.getenv(f"{name}_FILE") or "").strip()
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"{name}_FILE is set to {path!r} but could not be read: {exc}")
    return (os.getenv(name) or "").strip()


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _choice(name: str, default: str, allowed: set) -> str:
    value = (_env(name) or default).strip().upper()
    if value not in allowed:
        raise RuntimeError(
            f"{name} must be one of {sorted(allowed)} — got {value!r}.")
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer — got {raw!r}.")


def _load() -> ControllerConfig:
    raw_id = _env("R1_CONTROLLER_ID") or "1"
    try:
        controller_id = int(raw_id)
    except ValueError:
        raise RuntimeError(f"R1_CONTROLLER_ID must be an integer — got {raw_id!r}.")

    return ControllerConfig(
        id=controller_id,
        name=(_env("R1_CONTROLLER_NAME") or "RUCKUS ONE"),
        tenant_id=_required("R1_TENANT_ID"),
        client_id=_required("R1_CLIENT_ID"),
        shared_secret=_required("R1_SHARED_SECRET"),
        region=_choice("R1_REGION", "NA", REGIONS),
        ec_type=_choice("R1_EC_TYPE", "EC", EC_TYPES),
    )


def _trusted_proxies(required: bool) -> Tuple[ipaddress._BaseNetwork, ...]:
    """
    Which source addresses are allowed to speak for someone other than
    themselves — to assert an identity header in proxy mode, or a
    client-address header in either mode.

    This list is the entire security of proxy mode, so it is required rather
    than defaulted. A header is a claim, not proof: if PISR is reachable from
    anywhere other than the proxy, anyone who can open a socket to it can send
    `X-Forwarded-Email: someone.important@corp.example` and be believed. Bind
    PISR to loopback or to the proxy's own Docker network as well — this check
    is the second lock, not the first.
    """
    raw = _env("PISR_TRUSTED_PROXY_IPS")
    if not raw:
        if not required:
            # Passphrase mode. No proxy declared means no forwarded header is
            # believed and the TCP peer is taken at face value, which is right
            # for a directly-reachable instance and fails closed for any other.
            return ()
        raise RuntimeError(
            "PISR_AUTH_MODE=proxy requires PISR_TRUSTED_PROXY_IPS — the "
            "address or CIDR the authenticating proxy connects from. Without "
            "it, anyone who can reach this port can forge the identity header "
            "and walk straight in. Example: PISR_TRUSTED_PROXY_IPS=172.20.0.0/16")

    nets = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise RuntimeError(
                f"PISR_TRUSTED_PROXY_IPS entry {entry!r} is not an IP or CIDR: {exc}")
    if not nets:
        raise RuntimeError("PISR_TRUSTED_PROXY_IPS is set but contains no usable entries.")
    return tuple(nets)


def _load_auth() -> AuthConfig:
    """
    Fails closed in every mode. A missing passphrase, or a proxy mode with no
    trusted-proxy list, stops the container from starting rather than quietly
    serving one tenant's whole network inventory to anyone who can reach the
    port.
    """
    if _flag("PISR_AUTH_DISABLED"):
        return AuthConfig(
            enabled=False, mode="disabled", passphrase="", session_secret="",
            session_seconds=0, cookie_secure=False, max_attempts=0,
            lockout_seconds=0, proxy_header="", proxy_logout_url="",
            trusted_proxies=(), client_ip_header="",
        )

    mode = (_env("PISR_AUTH_MODE") or "passphrase").lower()
    if mode not in ("passphrase", "proxy"):
        raise RuntimeError(
            f"PISR_AUTH_MODE must be 'passphrase' or 'proxy' — got {mode!r}.")

    if mode == "proxy":
        header = _env("PISR_TRUSTED_PROXY_HEADER") or "X-Forwarded-Email"
        return AuthConfig(
            enabled=True,
            mode="proxy",
            passphrase="", session_secret="", session_seconds=0,
            cookie_secure=_flag("PISR_COOKIE_SECURE", False),
            max_attempts=0, lockout_seconds=0,
            proxy_header=header,
            proxy_logout_url=_env("PISR_PROXY_LOGOUT_URL"),
            trusted_proxies=_trusted_proxies(required=True),
            client_ip_header=_env("PISR_CLIENT_IP_HEADER"),
        )

    passphrase = _env("PISR_AUTH_PASSPHRASE")
    if not passphrase:
        raise RuntimeError(
            "PISR_AUTH_PASSPHRASE is not set. PISR serves a full RUCKUS ONE "
            "venue inventory and will not start without a gate in front of it. "
            "Set one in .env, or set PISR_AUTH_DISABLED=1 if this instance is "
            "genuinely unreachable by anyone else.")
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise RuntimeError(
            f"PISR_AUTH_PASSPHRASE must be at least {MIN_PASSPHRASE_LENGTH} "
            "characters. It is the only thing between the network and the "
            "report.")

    # No secret set means sessions do not survive a restart. That is a safe
    # default — the cost is re-entering the passphrase after `compose up`, and
    # the alternative default (a hardcoded key) is not a real alternative.
    session_secret = _env("PISR_SESSION_SECRET") or _secrets.token_urlsafe(32)

    return AuthConfig(
        enabled=True,
        mode="passphrase",
        passphrase=passphrase,
        session_secret=session_secret,
        session_seconds=_int("PISR_SESSION_HOURS", 12) * 3600,
        cookie_secure=_flag("PISR_COOKIE_SECURE", False),
        max_attempts=_int("PISR_AUTH_MAX_ATTEMPTS", 5),
        lockout_seconds=_int("PISR_AUTH_LOCKOUT_SECONDS", 300),
        proxy_header="", proxy_logout_url="",
        trusted_proxies=_trusted_proxies(required=False),
        client_ip_header=_env("PISR_CLIENT_IP_HEADER"),
    )


CONTROLLER: ControllerConfig = _load()
AUTH: AuthConfig = _load_auth()

# Whether PISR_SESSION_SECRET was supplied rather than generated. main.py logs
# this once at startup so "everyone got logged out again" has a visible cause.
SESSION_SECRET_IS_EPHEMERAL: bool = (
    AUTH.mode == "passphrase" and not _env("PISR_SESSION_SECRET"))


def public_config() -> dict:
    """
    What the SPA is allowed to know. The tenant id is not a secret — it is in
    every URL of the RUCKUS ONE console — but the client id and shared secret
    never leave the process.

    Served behind the session gate: it is not sensitive in the way the shared
    secret is, but it names the tenant, and an unauthenticated caller has no
    business knowing which tenant this instance points at.

    snake_case on purpose: this matches the shape rtools2 served in
    `controllers[]`, so the frontend shim maps it over unchanged.
    """
    return {
        "id": CONTROLLER.id,
        "name": CONTROLLER.name,
        "controller_type": CONTROLLER.controller_type,
        "controller_subtype": CONTROLLER.ec_type,
        "r1_tenant_id": CONTROLLER.tenant_id,
        "r1_region": CONTROLLER.region,
    }

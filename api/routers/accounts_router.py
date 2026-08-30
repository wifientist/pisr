"""
The accounts portal: create people, hand out enrolment links, revoke.

WHAT THIS IS FOR. In accounts mode PISR keeps its own logins, because the
identity provider in front of it stopped being usable — Cloudflare Access's
one-time PINs are silently eaten by two of three customer mail domains. An
admin creates an account here and delivers the enrolment link OUT OF BAND.
Nothing is emailed, by design; see api/accounts.py.

  GET    /api/admin/accounts              everyone, and how this deployment is
                                          configured
  POST   /api/admin/accounts              create one; returns the enrolment
                                          link ONCE
  POST   /api/admin/accounts/{id}/invite  reissue — this is the password reset
  PATCH  /api/admin/accounts/{id}         role, enabled/disabled
  DELETE /api/admin/accounts/{id}         remove

THE ENROLMENT LINK IS RETURNED EXACTLY ONCE, by the two routes that mint one.
Only its SHA-256 goes to disk, so there is no route that can show it again and
no amount of admin privilege recovers it — an admin who loses one reissues it.
That is what makes a stolen accounts.json useless for signing in, and it is
worth keeping even though it costs a support question now and then.

NO ROUTE HERE RETURNS A PASSWORD HASH. `Account.public()` is the only shape
that leaves this module, and it has no `hash` key. Admins are not entitled to
each other's password material either, and a list endpoint that leaked it would
be handing out offline-crackable data to anyone who reached this far.

THE LAST ADMIN CANNOT BE REMOVED, demoted or disabled — `accounts.py` enforces
it and returns a message saying so. Same principle as the visibility portal's
"admins are never subject to the policy": there must be no state an admin can
reach that only an SSH session gets them out of, and this box is deliberately
awkward to SSH into.

Everything is behind `require_admin`, which is the enforcement. The SPA also
hides the portal from non-admins, but the bundle is served unauthenticated and
anyone can read what it would render — so the route check is the real one.
"""

import logging
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import accounts
from auth import require_admin
from config import AUTH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/accounts", tags=["Accounts"],
                   dependencies=[Depends(require_admin)])


class CreateBody(BaseModel):
    username: str = Field(description="Lowercase; see accounts.USERNAME_RULE.")
    role: str = Field(default=accounts.ROLE_USER,
                      description="'admin' or 'user'.")


class UpdateBody(BaseModel):
    """
    A patch, unlike the visibility policy's whole-document PUT.

    The difference is that these fields are independent — changing a role and
    disabling an account are separate decisions by separate people at separate
    times, and a whole-document write would make two admins in two tabs
    silently undo each other. The policy file is one setting changed a few
    times a year; this is a list of people.
    """

    role: Optional[str] = None
    disabled: Optional[bool] = None


def _enrol_path(token: str) -> str:
    """
    The enrolment link, as a PATH — the browser makes it absolute.

    THE SERVER DOES NOT KNOW ITS OWN PUBLIC ADDRESS, and pretending otherwise
    produces a link nobody can open. An earlier version of this built a full
    URL from the request, which in dev came out as `http://backend:8080/...`
    (vite proxies to the container's internal name) and in production would
    rest on whatever Host the tunnel happened to forward. Worse, reading
    X-Forwarded-Host to fix that would believe a header from any peer, which is
    the one thing auth.py is careful never to do.

    The browser knows the address the admin is looking at, by definition. So
    this returns a path and `AdminAccounts.tsx` resolves it against
    window.location.origin. The CLI, which has no browser, prints a visible
    placeholder instead of guessing.

    The SPA has no router — see src/App.tsx — so enrolment is a query parameter
    on the root path rather than a path of its own.
    """
    return f"/?enroll={quote(token, safe='')}"


def _guard_writable() -> None:
    """
    Refuse a write that would be silently lost, and name the actual problem.

    Letting the OSError surface would say "permission denied" and leave the
    reader to work out that the volume was never mounted — which for this file
    means every account disappears at the next deploy.
    """
    if not accounts.STORE.configured:
        raise HTTPException(
            status_code=503,
            detail="PISR_ACCOUNTS_FILE is not set, so there is nowhere to keep "
                   "accounts.")
    if not accounts.STORE.writable:
        raise HTTPException(
            status_code=503,
            detail=f"{accounts.STORE.path} is not writable by the container. "
                   "Mount a writable volume at its directory — without one, "
                   "every account would be lost at the next deploy.")


def _require_accounts_mode() -> None:
    if AUTH.mode != "accounts":
        raise HTTPException(
            status_code=400,
            detail=f"This instance is in {AUTH.mode!r} mode, so local accounts "
                   "decide nothing. Set PISR_AUTH_MODE=accounts to use them.")


def _actor(request: Request) -> str:
    return getattr(request.state, "pisr_user", None) or "admin"


@router.get("")
async def list_accounts():
    """
    Everyone, plus how this deployment is configured.

    Served even when the mode is not `accounts`, deliberately: an operator
    preparing to switch wants to create the accounts BEFORE flipping the mode,
    exactly as they would set PISR_ADMIN_EMAILS before switching to proxy. The
    mode check sits on the writes, where acting on the wrong one would matter.
    """
    return {**accounts.STORE.summary(), "mode": AUTH.mode}


@router.post("", status_code=201)
async def create_account(body: CreateBody, request: Request):
    """Create an account and mint its first enrolment link."""
    _require_accounts_mode()
    _guard_writable()
    try:
        account, token = accounts.STORE.create(
            body.username, body.role, _actor(request))
    except accounts.AccountsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "account": account.public(),
        # ONCE. There is no route that returns this again.
        "enrolPath": _enrol_path(token),
        "expiresAt": account.invite.expires_at if account.invite else None,
    }


@router.post("/{account_id}/invite")
async def reissue_invite(account_id: str, request: Request):
    """
    A fresh enrolment link — the password reset, and the "they never got the
    first one" fix.

    The existing password keeps working until the link is redeemed, so a
    mis-clicked reset is not an outage. See `accounts.issue_invite`.
    """
    _require_accounts_mode()
    _guard_writable()
    try:
        account, token = accounts.STORE.issue_invite(account_id, _actor(request))
    except accounts.AccountsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "account": account.public(),
        "enrolPath": _enrol_path(token),
        "expiresAt": account.invite.expires_at if account.invite else None,
    }


@router.patch("/{account_id}")
async def update_account(account_id: str, body: UpdateBody, request: Request):
    """Change a role, or enable/disable. Refuses to strand the last admin."""
    _require_accounts_mode()
    _guard_writable()
    if body.role is None and body.disabled is None:
        raise HTTPException(status_code=400,
                            detail="Nothing to change — send a role or disabled.")
    try:
        account = accounts.STORE.update(
            account_id, _actor(request), role=body.role, disabled=body.disabled)
    except accounts.AccountsError as exc:
        # 400, not 404: the commonest cause by far is the last-admin guard,
        # which is a refusal with a reason rather than a missing thing.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"account": account.public()}


@router.delete("/{account_id}")
async def delete_account(account_id: str, request: Request):
    """Remove an account. Every session it holds dies with it."""
    _require_accounts_mode()
    _guard_writable()
    try:
        account = accounts.STORE.delete(account_id, _actor(request))
    except accounts.AccountsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deleted": account.public()}

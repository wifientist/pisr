"""
The admin portal's two endpoints: read the section catalogue, write the policy.

WHAT THIS IS FOR. PISR renders about thirty distinct cards, and most readers
want a fraction of them. An admin decides which of those an ordinary user sees;
everything else about the report is unchanged. It is de-cluttering, applied
centrally so that the same decision reaches the screen and the PDF.

WHAT THIS IS NOT. It is not a permissions system and it does not make anything
confidential. Both roles are fully authenticated — they got through the gate in
`auth.py`, which is the control that matters — and the sections hidden from a
user are hidden because someone judged them noise, not because they are
secrets. Read `visibility.py`'s docstring before treating this as a
confidentiality boundary; it fails open by design, and that is a decision, not
an oversight.

ADMINS ARE NEVER SUBJECT TO THE POLICY, which is what makes it safe to edit. A
policy that could hide the portal from the person editing it has a state you
cannot get out of without an SSH session, and this tool is deployed somewhere
that makes an SSH session inconvenient on purpose.

  GET /api/admin/visibility   the catalogue, the current policy, and whether
                              this deployment can save at all
  PUT /api/admin/visibility   replace the policy

The policy has two halves and they are NOT the same kind of thing. `hidden`
decides which report sections a user is shown and fails open; `scope` decides
which MSP-ECs and venues a user may reach at all and fails closed once set. The
portal edits both from one dialog because one person sets both, but see
api/scope.py before touching the second — on an MSP tenant, ECs are different
companies, and it is the only half where a mistake shows one customer another.

The portal builds its EC and venue lists from the ORDINARY endpoints —
/api/r1/{cid}/msp/mspEcs and /api/pisr/{cid}/venues — which return everything
when an admin calls them, because an admin is unrestricted. No admin-only
mirror of those exists, deliberately: a second path to the same R1 data is a
second place for the scope filter to be forgotten.

Both are behind `require_admin`, which is the enforcement. The SPA also hides
the portal from non-admins, but the bundle is served unauthenticated and anyone
can read what it would render — so the route check is the real one and the UI
is a courtesy.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

import access_surface
import grants as grant_catalogue
import roles as role_rules
import scope as scope_rules
import sections as section_catalogue
import visibility
import auth
from auth import grants_of, require_admin
from config import AUTH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"],
                   dependencies=[Depends(require_admin)])


class VisibilityBody(BaseModel):
    """
    A whole policy, not a patch.

    Replacing rather than merging on purpose: a portal that sends a diff has to
    agree with the server about what it was diffing against, and two admins in
    two tabs would silently combine their edits into a policy neither of them
    chose. Sending the whole thing makes last-write-wins visible, which for a
    setting changed a handful of times a year is the honest trade.
    """

    hidden: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Section ids to hide, keyed by role. Only the roles in "
                    "visibility.MANAGED_ROLES are read; anything else, "
                    "including 'admin', is ignored rather than refused.")

    scope: Optional[Dict[str, Any]] = Field(
        default=None,
        description="EC/venue restriction, per role AND per person. Omit or "
                    "send null for no restriction anywhere. Role keys are the "
                    "roles in visibility.MANAGED_ROLES; the reserved key "
                    "'people' (visibility.PEOPLE_KEY) holds a map of identity "
                    "-> the same block, and a person's entry REPLACES their "
                    "role's rather than adding to it. Shape either way: "
                    '{"unrestricted": false, "ecs": {"<tenantId>": "*" | '
                    '["<venueId>", ...]}}. Validated by scope.clean, which '
                    "drops what it cannot read rather than admitting it.")

    roles: Optional[Any] = Field(
        default=None,
        description="Role definitions, keyed by id (or a list carrying "
                    "ids). Each is {label, grants, hidden}. GRANTS ARE A FIXED "
                    "VOCABULARY — grants.ALL_GRANTS — and an unknown one is "
                    "dropped, because a grant no route enforces is a "
                    "permission that silently does nothing. An unknown hidden "
                    "element id is dropped too, for the ordinary reason. See "
                    "api/roles.py.")

    people: Optional[Any] = Field(
        default=None,
        description="Per-person policy, keyed by identity: "
                    '{"<identity>": {"role": "<roleId>|null", '
                    '"scope": <block>|null}}. A role REPLACES the role '
                    "default for both halves; scope replaces the role's scope "
                    "rather than adding to it.")


def _role_authority(request: Request) -> bool:
    """
    Whether this caller may edit the role CATALOGUE.

    True when they hold `admin.roles` — OR when nobody does. The second half
    is the bootstrap rule, and it is the same shape as `scope.py`'s
    "unrestricted is the default": a control nobody has configured must not
    lock everyone out. A fresh instance has no roles, so nobody can hold the
    grant, so the first role could never be made. The moment any role
    confers it and somebody is assigned that role, ordinary admins are shut
    out and the tier is real.

    It also means the tier cannot exist in PASSPHRASE MODE, where there are no
    identities to assign roles to. Correct rather than a gap: a mode that
    cannot tell two people apart cannot put one above the other.
    """
    return (grant_catalogue.GRANT_ADMIN_ROLES in grants_of(request)
            or not visibility.STORE.grant_is_claimed(
                grant_catalogue.GRANT_ADMIN_ROLES))


def _require_role_authority(request: Request, body: "VisibilityBody") -> None:
    """
    The two rules that make the super tier real. Both compare the SUBMITTED
    document against the STORED one, which is why neither can be a dependency.

    RULE 1 — editing the role catalogue needs `admin.roles`. Checked
    against what actually CHANGED rather than against the field being present,
    because the portal sends a whole document every time (last-write-wins is
    visible, and a diff two admins disagreed about is not). An ordinary admin
    saving a scope change re-sends the roles unaltered, and must not be
    refused for it.

    RULE 2 — nobody may add or remove a grant they do not themselves hold.
    Without it rule 1 is decorative: the super role already exists, so an
    ordinary admin would simply pick it from the dropdown for themselves.
    SYMMETRIC, covering removal as well as addition, so that a lesser admin
    cannot quietly demote a super either.

    AND THE LAST-SUPER GUARD, which is rule 2's blind spot. Unassigning the
    last holder of `admin.roles` removes a grant — but it also makes the
    grant unclaimed, which re-opens the bootstrap rule above and hands role
    editing back to every admin. Same failure the last-admin guard in
    accounts.py exists for, reached by a different door.
    """
    stored = visibility.STORE.policy()
    may_edit = _role_authority(request)

    # ── rule 1 ───────────────────────────────────────────────────────
    if body.roles is not None:
        # Compared RESOLVED to RESOLVED — `policy()` emits the built-ins, so a
        # portal echoing the document back unchanged must not read as having
        # deleted them. What is compared is the roles in force before and
        # after, which is the thing an administrator actually changed.
        before = {r["id"]: r for r in stored.get("roles", [])}
        after = {rid: r.public() for rid, r in
                 role_rules.resolve(role_rules.clean(body.roles)).items()}
        if before != after and not may_edit:
            raise HTTPException(
                status_code=403,
                detail="Editing roles needs the 'admin.roles' feature. "
                       "An administrator who has it can change the role, or "
                       "grant it to you.")

    if body.people is None:
        return

    # ── rule 2, per person whose assignment changed ──────────────────
    #
    # ONLY BINDS CALLERS WITHOUT ROLE AUTHORITY, and that is not a loophole.
    # Somebody who may edit the catalogue can put any grant into any role
    # and then assign it, so enforcing this on them would be theatre they
    # bypass with one extra save. It is also what lets a fresh instance make
    # its FIRST super: under the bootstrap rule that admin may edit, and they
    # necessarily do not yet hold the grant they are about to create.
    #
    # So this rule says one thing: an ordinary admin may not exceed themselves.
    if may_edit:
        return _guard_last_super(body, stored)

    held = grants_of(request)
    known = {p["id"]: set(p.get("grants") or []) for p in stored.get("roles", [])}
    if body.roles is not None and may_edit:
        # A super may define and assign a role in ONE save; judge the
        # assignment against what the roles will BE, not what they were.
        known = {pid: set(p.grants)
                 for pid, p in role_rules.clean(body.roles).items()}

    before_map = {identity: (entry or {}).get("role")
                  for identity, entry in (stored.get(visibility.PEOPLE_BLOCK) or {}).items()}
    after_map = {}
    if isinstance(body.people, dict):
        for identity, entry in body.people.items():
            if isinstance(identity, str) and isinstance(entry, dict):
                after_map[identity.strip().casefold()] = entry.get("role")

    for identity in set(before_map) | set(after_map):
        was, now = before_map.get(identity), after_map.get(identity)
        if was == now:
            continue
        changed = known.get(was or "", set()) ^ known.get(now or "", set())
        beyond = sorted(changed - held)
        if beyond:
            raise HTTPException(
                status_code=403,
                detail=f"That change would hand out or take away "
                       f"{', '.join(repr(g) for g in beyond)}, which you do not "
                       "hold yourself. Ask an administrator who does.")

    _guard_last_super(body, stored)


def _guard_last_super(body: "VisibilityBody", stored: dict) -> None:
    """
    Nobody may leave the super tier empty once it exists.

    Unassigning the last holder of `admin.roles` removes a capability, but it
    also makes the capability UNCLAIMED — which re-opens the bootstrap rule and
    hands role editing back to every administrator. A super demoting themselves
    is an escalation for everybody else.

    COUNTS BOTH PLACES A ROLE IS ASSIGNED. In accounts mode the assignment is a
    field on the ACCOUNT, which this document does not touch at all; looking
    only at `body.people` made every role save in that mode look like the tier
    being emptied. `accounts_router` guards its own door separately.

    It guards the ORPHANING, not the DISMANTLING: a document that still DEFINES
    a role carrying the capability but leaves nobody holding it is refused; one
    that defines none is the tier being deliberately turned off, which only a
    super can do and which they are entitled to decide.
    """
    super_grant = grant_catalogue.GRANT_ADMIN_ROLES
    if not visibility.STORE.grant_is_claimed(super_grant):
        return

    after = (role_rules.resolve(role_rules.clean(body.roles))
             if body.roles is not None else visibility.STORE.role_map())
    carries = {rid for rid, role in after.items()
               if super_grant in role_rules.grants_of(role)}
    if not carries:
        return          # deliberately dismantled at the definition level

    # Anybody left holding one of those roles, through either door.
    if isinstance(body.people, dict):
        for entry in body.people.values():
            if isinstance(entry, dict) and entry.get("role") in carries:
                return
    elif body.people is None:
        for entry in (stored.get(visibility.PEOPLE_BLOCK) or {}).values():
            if (entry or {}).get("role") in carries:
                return

    if AUTH.mode == "accounts":
        try:
            import accounts
            if any(a.can_sign_in and a.role in carries
                   for a in accounts.STORE.list()):
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("roles: could not count super holders: %s", exc)
            return      # cannot tell; do not refuse a save on a guess

    raise HTTPException(
        status_code=400,
        detail="That would leave nobody holding 'admin.roles', so no role "
               "could ever be edited again without repairing the policy file "
               "on the box. Assign it to somebody else first.")


def _known_identities() -> List[str]:
    """
    Every identity an admin could scope, or an empty list where PISR cannot
    know. Never an error: the portal must still draw with no names to offer.
    """
    from config import AUTH
    if AUTH.mode != "accounts":
        return []
    try:
        import accounts
        return sorted(a.username for a in accounts.STORE.list())
    except Exception as exc:  # noqa: BLE001
        logger.warning("visibility: could not list identities to scope: %s", exc)
        return []


# `/visibility` IS GONE. It served a role-keyed `hidden` block and a role-keyed
# scope block, both of which moved onto the role itself when roles became the
# one thing that confers anything. `/roles` replaced it whole, and leaving the
# old pair behind would have meant two endpoints describing one policy with
# nothing saying which won.
#
# Its request model survives as `VisibilityBody` because `/roles` takes the
# same document.


@router.get("/roles")
async def get_roles(request: Request):
    """
    Everything the RBAC page needs to draw itself, in one call.

    The CAPABILITY vocabulary ships with the release and the FEATURE list says
    what each tool costs — both emitted rather than hard-coded in the bundle,
    for the reason `sections.catalogue()` exists: two lists of the same ids in
    two languages drift, and only one of them decides.
    """
    return {
        "sections": section_catalogue.catalogue(),
        "grants": [{"id": g, "label": grant_catalogue.label(g),
                    "hint": grant_catalogue.hint(g),
                    "isDefault": g in grant_catalogue.DEFAULT_GRANTS}
                   for g in grant_catalogue.ALL_GRANTS],
        "features": [dict(f) for f in grant_catalogue.features()],
        "people": _known_identities(),
        # Built-in ids always resolve, so deleting one is a reset rather than a
        # removal. The portal says so instead of offering a Delete that
        # silently does nothing.
        "undeletable": list(role_rules.undeletable()),
        "mayEditRoles": _role_authority(request),
        "superGrant": grant_catalogue.GRANT_ADMIN_ROLES,
        "policy": visibility.STORE.policy(),
    }


@router.put("/roles")
async def put_roles(body: VisibilityBody, request: Request):
    """
    Replace the roles and the assignments. Same document, same guards, same
    whole-replace semantics as the policy PUT — this is that endpoint with the
    role half on the front.
    """
    if not visibility.STORE.writable:
        raise HTTPException(
            status_code=503,
            detail=f"{visibility.STORE.policy().get('path')} is not writable by "
                   "the container. Mount a writable volume at its directory.")
    _require_role_authority(request, body)
    actor = getattr(request.state, "pisr_user", None) or "admin (passphrase)"
    try:
        return visibility.STORE.save(body.hidden, body.scope, actor,
                                     roles=body.roles, people=body.people)
    except RuntimeError as exc:
        logger.error("roles: save failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _combinations(policy: dict) -> List[Dict[str, Any]]:
    """
    The columns: one per ROLE, with who holds it.

    ONE CONCEPT MEANS ONE COLUMN PER ROLE. A role is the only answer to "how
    did they get that", so the matrix is simply roles across the top and
    features down the side.

    Every role in force gets a column, including ones nobody holds yet — an
    administrator composing one wants to see what it would give before handing
    it to anybody.
    """
    people = policy.get(visibility.PEOPLE_BLOCK) or {}
    holders: Dict[str, List[str]] = {}
    scoped: Dict[str, int] = {}

    if AUTH.mode == "accounts":
        try:
            import accounts
            for account in accounts.STORE.list():
                holders.setdefault(account.role, []).append(account.username)
                if (people.get(account.username.casefold()) or {}).get("scope"):
                    scoped[account.role] = scoped.get(account.role, 0) + 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("capabilities: could not list accounts: %s", exc)
    else:
        # Proxy mode assigns roles in the policy's per-person map; passphrase
        # mode has no identities, so the matrix shows the shapes without being
        # able to say who is in them.
        for identity, entry in people.items():
            if entry.get("role"):
                holders.setdefault(entry["role"], []).append(identity)

    rows = []
    for role in sorted(visibility.STORE.roles(), key=lambda r: r.label.lower()):
        rows.append({
            "key": role.id,
            "roleId": role.id,
            "roleLabel": role.label,
            "builtin": role.id in role_rules.undeletable(),
            "holders": sorted(holders.get(role.id, [])),
            "scopedHolders": scoped.get(role.id, 0),
            "grants": sorted(role.grants),
            "visibleCount": len(role.visible),
        })

    # Roles nobody built but somebody still names — an account assigned to one
    # that was deleted. Surfaced rather than swept up, because those people
    # hold nothing at all until an administrator notices.
    known = {r["roleId"] for r in rows}
    for role_id, names in sorted(holders.items()):
        if role_id in known:
            continue
        rows.append({
            "key": role_id, "roleId": role_id, "roleLabel": role_id,
            "builtin": False, "missing": True,
            "holders": sorted(names), "scopedHolders": 0,
            "grants": [], "visibleCount": 0,
        })
    return rows


def _features(routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The rows: one per FEATURE, plus a bucket for anything needing no
    capability.

    FEATURES, NOT CAPABILITIES AND NOT ROUTES. An administrator thinks "can
    they run a batch", and a batch needs two capabilities — read the tenant's
    roll-up, and start a run. `grants.features()` is the list both this and the
    SPA gate on, so the matrix and the buttons can never disagree about what a
    tool costs. The routes ride along underneath for whoever is debugging a
    403.
    """
    by_grant: Dict[str, List[Dict[str, Any]]] = {}
    open_routes: List[Dict[str, Any]] = []
    for route in routes:
        if route["public"]:
            continue
        entry = {"path": route["path"], "methods": route["methods"],
                 "narrows": route["checksScope"], "summary": route["summary"]}
        needed = route.get("requiresGrants") or []
        if not needed:
            open_routes.append(entry)
        for grant in needed:
            by_grant.setdefault(grant, []).append(entry)

    rows: List[Dict[str, Any]] = [{
        "id": "_open", "kind": "open", "label": "Available to anyone signed in",
        "hint": "No capability needed. Still narrowed by scope where marked.",
        "requires": [], "wired": bool(open_routes), "routes": open_routes,
    }]

    for feature in grant_catalogue.features():
        needed = list(feature["requires"])
        # A feature's routes are those requiring ALL of its capabilities —
        # anything requiring a subset belongs to a different, smaller feature.
        matched = [r for r in by_grant.get(needed[0], [])
                   if all(r in by_grant.get(g, []) for g in needed)] if needed else []
        rows.append({
            "id": feature["id"],
            "kind": "feature",
            "label": feature["label"],
            "hint": "; ".join(grant_catalogue.hint(g) for g in needed
                              if grant_catalogue.hint(g)),
            "requires": needed,
            # A feature with no route behind it is enforced INSIDE a handler —
            # `admin.roles` is, because its rules compare a submitted document
            # against the stored one — or defined ahead of the view that will
            # use it, like `msp.fleet`. SHOWN EITHER WAY, flagged rather than
            # dropped: a capability an administrator can hand out but cannot
            # find on this screen looks like the matrix lying, and being
            # trusted is the point of it.
            "wired": bool(matched),
            "routes": matched,
        })
    return rows


@router.get("/capabilities")
async def get_capabilities():
    """
    What every role in this deployment can reach — DERIVED from the route
    table, never written down.

    A hand-maintained matrix would be a fourth list beside the three that
    actually decide (the gate's prefixes, the per-route dependencies, the scope
    check inside handlers), and the authoritative-looking one nobody updates.
    This reads `main.app.routes` through `access_surface`, which is also what
    the end-to-end suite generates its expectations from.

    THREE STATES PER CELL, NOT TWO, and that is the thing to preserve if this
    is ever redesigned. "Reachable" and "refused" would say a grant WIDENS
    somebody's reach, which is the exact opposite of the rule it describes: a
    grant is a lens and never a key. `narrowed` is how the matrix says "yes,
    and only over the customers and venues they could already open".

    Admin-only because it is a map of the whole route surface — the same
    reasoning that keeps /docs behind the gate.
    """
    data = access_surface.surface()
    policy = visibility.STORE.policy()
    features = _features(data["routes"])
    combos = _combinations(policy)

    cells: Dict[str, Dict[str, str]] = {}
    for combo in combos:
        held = set(combo["grants"])
        row: Dict[str, str] = {}
        for feature in features:
            narrows = any(r["narrows"] for r in feature["routes"])
            # ALL-OF for a multi-capability feature. Anything missing means
            # refused, and the portal hides the control entirely rather than
            # showing one that 403s.
            allowed = (True if feature["kind"] == "open"
                       else set(feature.get("requires") or [feature["id"]]) <= held)
            row[feature["id"]] = ("refused" if not allowed
                                  else "narrowed" if narrows else "full")
        cells[combo["key"]] = row

    return {
        "features": features,
        "combinations": combos,
        "cells": cells,
        "defaultGrants": list(grant_catalogue.DEFAULT_GRANTS),
        "mode": AUTH.mode,
        # Said in the payload rather than only in the UI, so a reader of the
        # JSON cannot mistake a tick for "entitled to everything this returns".
        "caveat": ("A grant decides WHICH VIEW exists, never which customers "
                   "are in it. Rows marked narrowed are filtered per person by "
                   "scope and by the hidden elements their role carries; "
                   "this matrix cannot show that, because it is per person and "
                   "resolved at request time."),
    }


# ── preview: what one person actually gets ───────────────────────────


def _element_prose() -> Dict[str, Dict[str, str]]:
    """id -> {label, kind, tab} for every hideable element, for readability."""
    prose: Dict[str, Dict[str, str]] = {}
    for section in section_catalogue.catalogue():
        prose[str(section["id"])] = {
            "label": str(section["label"]), "kind": "section",
            "tab": str(section["tabLabel"])}
        for check in section["checks"]:                      # type: ignore[index]
            prose[str(check["id"])] = {
                "label": str(check["label"]), "kind": "check",
                "tab": str(section["tabLabel"])}
        for column in section["columns"]:                    # type: ignore[index]
            prose[str(column["id"])] = {
                "label": str(column["label"]), "kind": "column",
                "tab": str(section["tabLabel"])}
    return prose


@router.get("/preview")
async def get_preview(identity: str = Query(..., max_length=320,
                                            description="The username or "
                                                        "verified address to "
                                                        "resolve.")):
    """
    What ONE PERSON actually gets, and WHERE EACH ANSWER CAME FROM.

    The matrix answers a design question — what does a combination unlock. This
    answers the support question, which is the one that generates calls: "why
    can't Dana see Block B?" Knowing she is scoped is half an answer; knowing
    the restriction comes from HER OWN ENTRY rather than from the user role is
    the half that tells somebody which box to change.

    IT EXPLAINS, IT DOES NOT IMPERSONATE. No session is minted, nothing is
    fetched from RUCKUS ONE, and no report is built. An admin is unrestricted
    anyway, so impersonation would confer nothing they lack — it would only add
    a way to act as somebody else, which is a liability with no upside.

    RESOLVED THROUGH THE SAME FUNCTIONS THE REAL REQUEST PATH USES —
    `scope_for`, `hidden_for`, `grants_for` — never re-derived from the stored
    document. A preview that computed its own answer could disagree with what
    the routes do, and a diagnostic tool that is confidently wrong sends
    somebody to change the wrong box. Same rule as the capability matrix.

    An identity PISR has never heard of is answered rather than refused: on a
    proxy-mode instance there is no account list to check against, and "no
    entry, so they get the defaults" is the useful answer to a typo as well as
    to a real person.
    """
    who = (identity or "").strip()
    folded = who.casefold()
    policy = visibility.STORE.policy()

    entry = (policy.get(visibility.PEOPLE_BLOCK) or {}).get(folded) or {}
    entry_role = entry.get("role")

    role_id = None
    known = False
    disabled = False
    if AUTH.mode == "accounts":
        try:
            import accounts
            account = accounts.STORE.by_username(who)
            if account is not None:
                known, role_id = True, account.role
                disabled = not account.can_sign_in
        except Exception as exc:  # noqa: BLE001
            logger.warning("preview: could not look %r up: %s", who, exc)
    else:
        # Proxy mode assigns roles in the policy's per-person map; the env
        # admin list is the fallback for anybody it does not name.
        role_id = (entry_role or
                   (auth.ROLE_ADMIN if who.casefold() in AUTH.admin_emails
                    else auth.ROLE_USER))
        known = bool(entry_role) or who.casefold() in AUTH.admin_emails

    role = visibility.STORE.role_for(who, role_id)

    # RESOLVED THROUGH THE SAME FUNCTIONS THE REQUEST PATH USES, never worked
    # out again from the stored document. A diagnostic tool that computed its
    # own answer could disagree with the routes, and would send an
    # administrator to change a box that was never the problem.
    held = sorted(visibility.grants_for(who, role_id))
    allowed = visibility.scope_for(who, role_id)
    hidden = visibility.hidden_for(who, role_id)
    prose = _element_prose()

    # WHERE each answer came from. The point of the whole endpoint: knowing
    # somebody is scoped is half an answer, knowing which box to change is the
    # half that ends the support call.
    scope_source = ("person" if entry.get("scope") is not None
                    else "role" if policy.get("scope", {}).get(role_id or "")
                    else "unrestricted")
    hidden_source = "role"

    warnings: List[str] = []
    if role is None:
        warnings.append(
            f"Their account names role {role_id!r}, which no longer exists — "
            "so they hold NOTHING, not even what an ordinary user gets. "
            "Assign them a role that exists.")
    if disabled:
        warnings.append("This account cannot sign in (disabled, or never "
                        "enrolled), so none of the below applies yet.")
    if not known and AUTH.mode == "accounts":
        warnings.append("No account by this name. Shown is what anybody "
                        "unrecognised would get.")
    if grant_catalogue.GRANT_VENUE_REPORT not in held:
        warnings.append(
            "They cannot open a venue report. Deliberate for a role that only "
            "reads estate roll-ups; for anybody else it is probably a role "
            "that forgot to tick it.")
    if not allowed.unrestricted and not allowed.ecs:
        warnings.append(
            "Scoped to no customers at all, so every venue is refused. An "
            "empty selection means nothing, not everything.")

    return {
        "identity": who,
        "known": known,
        "disabled": disabled,
        "mode": AUTH.mode,
        # The id they RESOLVE to, which for somebody with no record at
        # all is the built-in `user` — the answer a typo deserves.
        "roleId": (role.id if role is not None else role_id),
        "role": ({"id": role.id, "label": role.label,
                  "builtin": role.id in role_rules.undeletable()}
                 if role is not None else None),
        "roleMissing": role is None,
        "grants": [{"id": g, "label": grant_catalogue.label(g)} for g in held],
        # WHICH FEATURES they are shown, on the same all-of rule the SPA uses,
        # so this answers "why is the button not there" and not merely "which
        # capabilities do they hold".
        "features": [{"id": f["id"], "label": f["label"],
                      "requires": list(f["requires"]),
                      "has": set(f["requires"]) <= set(held),
                      "missing": sorted(set(f["requires"]) - set(held))}
                     for f in grant_catalogue.features()],
        "scope": {
            "unrestricted": allowed.unrestricted,
            "source": scope_source,
            "ecs": ({} if allowed.unrestricted else
                    {tenant: ("*" if venues == scope_rules.ALL_VENUES
                              else sorted(venues))
                     for tenant, venues in allowed.ecs.items()}),
        },
        "hidden": [{"id": eid, **prose.get(eid, {"label": eid, "kind": "?",
                                                 "tab": "?"})}
                   for eid in hidden],
        "hiddenSource": hidden_source,
        "warnings": warnings,
    }

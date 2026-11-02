"""
Roles: a named bundle of capabilities and visible elements, assigned to a person.

READ `api/grants.py` FIRST. The line this module sits on is the one that makes
"admins build their own roles" safe:

  GRANTS ARE FIXED AND SHIP WITH THE RELEASE. A route names one
  (`require_grants("msp.rollup")`), so a grant an admin could invent would be
  enforced by nothing — a permission that silently does nothing, shown in the
  portal as though it were real. That is worse than no permission at all.

  ROLES ARE DATA AND ADMINS OWN THEM. Any combination of shipped grants,
  any set of hideable element ids, a label of their choosing. Adding a
  CAPABILITY is a release; adding a JOB is a Tuesday.

It is the same relationship `sections.py` already has with the stored policy —
fixed catalogue, dynamic selection — one layer up.

ONE CONCEPT, DELIBERATELY. An earlier design had three ways to hold a
capability — a binary admin/user role, per-account tags, and a bundle called a
"profile" — and the arbitrary split between them was what everybody tripped
over ("I have the MSP tag, so why can I not run a batch?"). There is now
exactly one answer to "how did they get that": the role they were assigned.

ADMIN-NESS IS A CAPABILITY LIKE ANY OTHER (`admin.portal`), so "admin" is a
role somebody builds rather than a flag baked into the auth layer — which is
what makes `install_user` and `tenant_leader` first-class beside it rather
than second-class.

BOTH HALVES FAIL CLOSED, and that is a deliberate change from what came
before. `grants` always did: an unknown id is dropped, a missing role confers
none. `visible` now does too, because it is an ALLOW list — an element nobody
named is withheld.

The earlier model had the visibility half failing OPEN, on the argument that an
app rendering nothing and explaining nothing is worse than a card somebody did
not need. That argument is answered rather than dismissed:

  * the built-in `admin` role's list is COMPUTED from the catalogue, so at
    least one role always sees everything and a release can never go dark for
    everybody at once;
  * the portal and the startup log name elements no role allows, so a card
    added in a release is visibly UNGRANTED rather than quietly missing.

What is bought is the question an administrator actually asks. "What can this
role see" now has a positive answer instead of "everything except these
forty-three", and a list of forty-three exclusions is not something anybody
reads and understands.

IDS ARE GENERATED AND LABELS ARE FREE. `p_7f3a…` goes into the policy file and
is what a person is assigned to; the label is prose an admin will rename the
week after they create it ("Engineer" -> "Field Engineer"). Keying assignment
on the label would orphan every account the moment somebody tidied the wording
— the same rule check ids and section ids already follow, for the same reason.
"""

import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import grants as grant_rules
import sections as section_catalogue

#: Generated, never typed. Long enough not to collide, short enough to read in
#: a file somebody is debugging by eye.
#: A generated id, or one of the BUILT-IN names below. Built-ins are
#: readable on purpose: they are what an accounts file already says
#: ("admin", "user"), so the collapse of the old role field into a role id
#: needs no migration of anybody's account.
_ID = re.compile(r"^(r_[A-Za-z0-9_-]{6,40}|[a-z][a-z0-9_-]{1,30})$")

MAX_LABEL = 64
#: A deployment with more roles than this has a different problem, and the
#: cap stops a malformed body writing a megabyte of them to the volume.
MAX_ROLES = 100


def new_id() -> str:
    return "r_" + secrets.token_urlsafe(8)


@dataclass(frozen=True)
class Role:
    """One job, as the rest of PISR sees it."""

    id: str
    label: str
    #: Grant ids from `grants.ALL_GRANTS`. Anything else was dropped on the way
    #: in — see the module docstring on why this half fails closed.
    grants: Tuple[str, ...]
    #: Element ids this role MAY SEE — a section, a check or a column, from
    #: `sections.is_known_id`.
    #:
    #: AN ALLOW LIST, NOT A DENY LIST. Anything not named here is withheld.
    #: That makes "what can this role see" a positive, auditable answer instead
    #: of "everything except these forty-three", which is the question an
    #: administrator actually has and the one a deny list cannot answer.
    #:
    #: The cost is real and is met rather than argued away: an element added in
    #: a release is in NOBODY's list, so it is withheld until somebody grants
    #: it. Two things keep that from being a silent regression — the built-in
    #: `admin` role's list is COMPUTED from the catalogue, so at least one role
    #: always sees everything, and the portal names elements no role allows.
    visible: Tuple[str, ...]

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "label": self.label,
                "grants": list(self.grants), "visible": list(self.visible)}


def clean_one(raw: Any, fallback_id: Optional[str] = None) -> Optional[Role]:
    """
    One stored or submitted role, or None if it is not usable.

    DROPPED RATHER THAN REPAIRED, like `accounts._account_from_raw`: a record
    with no id cannot be assigned to anybody and inventing one would create a
    job nobody asked for. A record with no LABEL is different — that is
    cosmetic, and falling back to the id keeps the role assignable while
    looking obviously unfinished in the portal, which is the right prompt.
    """
    if not isinstance(raw, dict):
        return None
    ident = raw.get("id") or fallback_id
    if not isinstance(ident, str) or not _ID.match(ident):
        return None

    label = raw.get("label")
    label = label.strip()[:MAX_LABEL] if isinstance(label, str) else ""

    held = raw.get("grants")
    held = held if isinstance(held, (list, tuple)) else ()
    visible = raw.get("visible")
    visible = visible if isinstance(visible, (list, tuple)) else ()

    return Role(
        id=ident,
        label=label or ident,
        # Sorted and de-duplicated so one selection produces one file, and two
        # saves of it do not look like two different policies.
        grants=tuple(sorted({g for g in held
                             if isinstance(g, str) and g in grant_rules.ALL_GRANTS})),
        visible=tuple(sorted({v for v in visible
                              if isinstance(v, str)
                              and section_catalogue.is_known_id(v)})),
    )


def clean(raw: Any) -> Dict[str, Role]:
    """
    A whole `roles` map on its way in, keyed by id.

    Accepts both shapes a portal might send: a map of id -> record, and a list
    of records carrying their own ids. The map is what is written.
    """
    found: List[Role] = []
    if isinstance(raw, dict):
        for ident, record in raw.items():
            role = clean_one(record, fallback_id=ident if isinstance(ident, str) else None)
            if role is not None:
                found.append(role)
    elif isinstance(raw, (list, tuple)):
        for record in raw:
            role = clean_one(record)
            if role is not None:
                found.append(role)
    return {p.id: p for p in found[:MAX_ROLES]}


def grants_of(role: Optional[Role]) -> frozenset:
    """
    What a role confers. NO ROLE CONFERS NOTHING — the fail-closed half.

    A role names its capabilities DIRECTLY. There is no indirection left to
    look through and no second source to union in — which is the whole of what
    collapsing the three mechanisms bought, and the reason this function is
    two lines.
    """
    if role is None:
        return frozenset()
    return frozenset(role.grants)


def hidden_of(role: Optional[Role]) -> Tuple[str, ...]:
    """
    What a role does NOT see: every element minus the ones it allows.

    RESOLVED TO A DENY LIST AT THE BOUNDARY, deliberately. The stored model is
    an allow list because that is the auditable one; `redact.py` and both
    renderers take a hidden set and are untouched by the inversion. One
    subtraction here instead of a rewrite of the enforcement layer.

    NO ROLE SEES NOTHING. A role that was deleted out from under somebody
    withholds everything — which is the fail-CLOSED direction, and the opposite
    of what a missing deny list used to mean. Under an allow model that is the
    only coherent reading: they were given a job, its definition is gone, and
    nobody has said what they may see.
    """
    allowed = frozenset(role.visible) if role is not None else frozenset()
    return tuple(sorted(section_catalogue.ALL_ELEMENT_IDS - allowed))


def visible_of(role: Optional[Role]) -> Tuple[str, ...]:
    """What a role MAY see, as stored. The portal edits this; nothing enforces
    on it directly — `hidden_of` is what reaches `redact`."""
    return role.visible if role is not None else ()


def catalogue(roles: Iterable[Role]) -> List[Dict[str, Any]]:
    """Every role, for the portal. Ordered by label so the list is stable."""
    return [r.public() for r in sorted(roles, key=lambda r: r.label.lower())]


# ── the built-ins ────────────────────────────────────────────────────
#
# COMPILED IN, NOT SEEDED ONTO THE VOLUME, and that is the whole lockout
# story. `admin.portal` is a capability now, so if every capability came from a
# file on a volume, an unreadable file would mean nobody can administer
# anything — the exact failure the old "admins are never subject to the policy"
# short-circuit existed to prevent.
#
# So these two exist in CODE. A policy file may override them by id; a policy
# file that cannot be read falls back to them. An account already says
# `"role": "admin"` or `"role": "user"`, so the collapse of the old admin/user
# field into a role id needs no migration of anybody's account.
#
# The same pattern `baselines/ruckus.json` uses: ships in the repo, overridable
# on the volume.

BUILTIN_ADMIN = "admin"
BUILTIN_USER = "user"

_BUILTINS: Dict[str, Role] = {
    BUILTIN_ADMIN: Role(
        id=BUILTIN_ADMIN,
        label="Administrator",
        # Everything, including the authority to change this. A fresh instance
        # has to be able to reach every part of itself; narrowing from here is
        # an admin's decision, and one they make in the portal rather than one
        # we make for them on their behalf at install time.
        grants=tuple(sorted(grant_rules.ALL_GRANTS)),
        # COMPUTED, NOT LISTED. Under an allow model a stored list would go
        # stale the first time a release added a card, and the role that is
        # supposed to be able to see everything would be the one unable to see
        # the new thing — including unable to grant it to anybody else.
        visible=tuple(sorted(section_catalogue.ALL_ELEMENT_IDS)),
    ),
    BUILTIN_USER: Role(
        id=BUILTIN_USER,
        label="User",
        # The tool as it behaves before anybody configures anything: open a
        # venue report, and nothing else. Widening this is how a capability
        # system quietly becomes decoration.
        grants=tuple(sorted(grant_rules.DEFAULT_GRANTS)),
        # Also everything, and for a different reason: this is the tool BEFORE
        # anybody has configured it. A fresh instance that showed nothing until
        # somebody built a role would look broken rather than empty. Roles an
        # administrator BUILDS start from nothing, which is where deny-by-
        # default belongs.
        visible=tuple(sorted(section_catalogue.ALL_ELEMENT_IDS)),
    ),
}


def builtins() -> Dict[str, Role]:
    """A fresh copy of the compiled-in roles."""
    return dict(_BUILTINS)


def resolve(stored: Dict[str, Role]) -> Dict[str, Role]:
    """
    The roles in force: the built-ins, with any same-id stored role replacing
    one, plus every role an admin has created.

    OVERRIDE RATHER THAN MERGE. An admin editing "Administrator" gets exactly
    what they composed — a half-merged role would hold capabilities they
    deliberately unticked, which is the one thing an authority editor must
    never do.
    """
    resolved = builtins()
    resolved.update(stored)
    return resolved


def undeletable() -> Tuple[str, ...]:
    """
    Ids that always resolve to something, so deleting them is a reset rather
    than a removal. The portal says so instead of offering a Delete that
    silently does nothing.
    """
    return tuple(_BUILTINS)

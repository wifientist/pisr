"""
Resource groups: a named set of customers and venues, assigned to a person.

THE SECOND AXIS, AND IT IS NOT THE ROLE. Two questions decide what somebody
gets, and keeping them apart is the whole design:

    ROLE            what they may RUN and SEE  — capabilities, visible elements
    RESOURCE GROUP  what they may run it AGAINST — customers and venues

A role never carries territory and a group never carries a capability. An
earlier shape had the role carry a default scope; it was removed deliberately,
because "which reports may Dana run" and "whose network may she run them on"
are answered by different people on different days, and a role that silently
decided both meant `tenant_leader_north` and `tenant_leader_south` were only
ever one awkward requirement away.

WHY NAMED GROUPS RATHER THAN A LIST PER PERSON. Scope used to be a bespoke
customer/venue map on every individual, which meant forty people was forty
hand-maintained lists with nothing holding them together — no way to ask "who
is on the North estate", and onboarding meant copying somebody else's ticks by
eye. A group is defined once, reviewed once, and assigned; the same reasoning
that made roles one concept instead of three.

FAIL CLOSED, AND HARDER THAN SCOPE USED TO. `api/scope.py`'s rule was
"unrestricted until an admin names a single EC". A resource group model cannot
keep that: the whole point is that reach is granted by assignment, so somebody
with NO GROUP reaches NOTHING, rather than everything. That is a deliberate
reversal and the one behaviour change an upgrade has to be careful about —
`visibility._migrate_groups` is what stops deploy morning being an outage.

  EXCEPT IN PASSPHRASE MODE, which has no identities at all. A shared secret
  proves somebody knew it, never who, so a per-person assignment can never
  match there and fail-closed would mean the tool's DEFAULT auth mode shows
  every user nothing, for ever, with no way to fix it. Groups therefore apply
  where there are identities to assign them to — accounts and proxy modes —
  exactly as the super tier in `admin_router._role_authority` cannot exist in
  passphrase mode for the same reason. `visibility.scope_for` is where that is
  decided; this module only says what a group IS.

THE BUILT-IN IS COMPILED IN, NOT SEEDED, for the reason `roles._BUILTINS` is:
an unreadable policy file must still leave somebody able to reach the estate
and repair it. `all-access` says yes to every customer and every venue,
including ones added tomorrow, and it is the group an upgrade hands to anybody
who was unrestricted before.

WHAT A GROUP HOLDS IS EXPLICIT IDS, ON PURPOSE. A rule — "every venue tagged
Live", "every venue named North-*" — is expressible: the venue rows PRISM
already fetches carry name, city, country, tags and AP counts. It is not
STORED, and that is the decision rather than an omission. Scope is checked at
the route before any RUCKUS ONE client is built, so a refusal costs no upstream
call and cannot be taken out by R1 being down; a stored rule would have to be
evaluated against the live venue list, which moves the refusal behind a network
call and turns an R1 blip into "you are scoped to nothing" — a lockout that
reads like an authentication bug and is not one. So the portal MATCHES, and
writes the ids it matched: the admin sees which venues a pattern would add
before anybody gains access, and enforcement stays a set membership test.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

import scope as scope_rules

logger = logging.getLogger(__name__)

# Same shape as a role id: a slug an admin can read in a log line, or a
# generated one from the portal. Validated rather than sanitised — an id is a
# key in a stored document and in a person's assignment list, so repairing a
# bad one invents a group nobody asked for.
_ID = re.compile(r"^(g_[A-Za-z0-9_-]{6,40}|[a-z][a-z0-9_-]{1,30})$")

MAX_LABEL = 64
MAX_DESCRIPTION = 240
# Generous, and a bound rather than a budget: the point of groups is that there
# are far fewer of them than there are people.
MAX_GROUPS = 200
# Per group. An MSP with more venues than this in one group wants "all venues
# on this customer" (`ALL_VENUES`), which costs one entry.
MAX_VENUES_PER_EC = 2000

BUILTIN_ALL = "all-access"

ALL_VENUES = scope_rules.ALL_VENUES


@dataclass(frozen=True)
class ResourceGroup:
    """
    One named set of customers and venues.

    `all_access` is NOT "ecs happens to name everything". It means every
    customer and venue including ones that do not exist yet, which no list of
    ids can say — and saying it is the entire job of the built-in. A group with
    `all_access` ignores `ecs`; the portal keeps any ids already there so
    turning the flag off does not silently empty the group.
    """

    id: str
    label: str
    description: str
    all_access: bool
    #: tenant id -> ALL_VENUES | frozenset of venue ids
    ecs: Mapping[str, Any]

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "allAccess": self.all_access,
            "ecs": {tenant: (ALL_VENUES if venues == ALL_VENUES else sorted(venues))
                    for tenant, venues in sorted(self.ecs.items())},
        }

    def venue_count(self) -> Optional[int]:
        """
        How many venues it names, or None where that cannot be counted.

        `ALL_VENUES` and `all_access` are both "however many there are", which
        is a different statement from a number and must not be rendered as one.
        """
        if self.all_access:
            return None
        total = 0
        for venues in self.ecs.values():
            if venues == ALL_VENUES:
                return None
            total += len(venues)
        return total


def clean_one(raw: Any, fallback_id: Optional[str] = None) -> Optional[ResourceGroup]:
    """
    One stored or submitted group, or None if it is not usable.

    DROPPED RATHER THAN REPAIRED, like `roles.clean_one`: a record with no
    usable id cannot be assigned to anybody, and inventing one creates a
    territory nobody drew. A missing LABEL is cosmetic and falls back to the
    id, which keeps the group assignable while looking unfinished in the
    portal — the right prompt rather than a silent drop.
    """
    if not isinstance(raw, dict):
        return None
    ident = raw.get("id") or fallback_id
    if not isinstance(ident, str) or not _ID.match(ident):
        return None

    label = raw.get("label")
    label = label.strip()[:MAX_LABEL] if isinstance(label, str) else ""
    description = raw.get("description")
    description = (description.strip()[:MAX_DESCRIPTION]
                   if isinstance(description, str) else "")

    # `scope.clean` is reused rather than reimplemented: a group's customer and
    # venue map is the same document scope has always validated, and a second
    # validator for one shape is how the two drift into disagreeing about what
    # an empty venue list means.
    block = scope_rules.clean({"unrestricted": False, "ecs": raw.get("ecs") or {}})
    ecs: Dict[str, Any] = {}
    for tenant, venues in ((block or {}).get("ecs") or {}).items():
        ecs[tenant] = (ALL_VENUES if venues == ALL_VENUES
                       else frozenset(venues[:MAX_VENUES_PER_EC]))

    return ResourceGroup(
        id=ident,
        label=label or ident,
        description=description,
        all_access=bool(raw.get("allAccess")),
        ecs=ecs,
    )


def clean(raw: Any) -> Dict[str, ResourceGroup]:
    """
    A whole `resourceGroups` map on its way in, keyed by id.

    Accepts both shapes a portal might send — a map of id -> record and a list
    of records carrying their own ids — for the reason `roles.clean` does. The
    map is what is written.
    """
    found: List[ResourceGroup] = []
    if isinstance(raw, dict):
        for ident, record in raw.items():
            group = clean_one(record,
                              fallback_id=ident if isinstance(ident, str) else None)
            if group is not None:
                found.append(group)
    elif isinstance(raw, (list, tuple)):
        for record in raw:
            group = clean_one(record)
            if group is not None:
                found.append(group)
    return {g.id: g for g in found[:MAX_GROUPS]}


# ── the built-in ─────────────────────────────────────────────────────
#
# COMPILED IN, NOT SEEDED. Same argument as `roles._BUILTINS`: if every way of
# reaching the estate came from a file on a volume, an unreadable file would
# leave nobody able to open a venue and nobody able to repair it. A policy may
# override this by id — an admin who wants "All access" to mean something
# narrower may say so — and a policy that cannot be READ falls back to it.

_BUILTINS: Dict[str, ResourceGroup] = {
    BUILTIN_ALL: ResourceGroup(
        id=BUILTIN_ALL,
        label="All access",
        description=("Every customer and every venue, including ones added "
                     "later. The only group that can say that — a list of ids "
                     "cannot name a venue that does not exist yet."),
        all_access=True,
        ecs={},
    ),
}


def builtins() -> Dict[str, ResourceGroup]:
    return dict(_BUILTINS)


def undeletable() -> tuple:
    """Ids that always resolve, so deleting one is a reset rather than a removal."""
    return tuple(_BUILTINS)


def resolve(stored: Dict[str, ResourceGroup]) -> Dict[str, ResourceGroup]:
    """The groups in force: the built-in, with any stored override by id."""
    merged = builtins()
    merged.update(stored or {})
    return merged


def catalogue(groups: Iterable[ResourceGroup]) -> List[Dict[str, Any]]:
    """Every group, for the portal. Ordered by label so the list is stable."""
    return [g.public() for g in sorted(groups, key=lambda g: g.label.lower())]


def union(groups: Iterable[Optional[ResourceGroup]]) -> scope_rules.Scope:
    """
    What somebody holding these groups can reach.

    UNION, AND THAT IS NOT THE RULE THAT WAS REJECTED ELSEWHERE. `scope_for`
    refuses to union a person's entry with their role's, because that is a
    PRECEDENCE question — an admin typing a territory into a box means "this
    one", not "this one as well as whatever the role already allowed". Groups
    are different in kind: every one of them was deliberately ticked for this
    person, so "North and the Harrogate site" has to mean both. A group model
    where the groups cannot combine is a list of mutually exclusive territories,
    which is the org chart again.

    EMPTY MEANS NOTHING, which is the fail-closed reversal this model is built
    on. `scope.Scope(unrestricted=False, ecs={})` is the same value an admin
    gets today by ticking a customer and then unticking it, and `api/scope.py`
    already treats it as "no venues, not all of them".

    A DELETED GROUP CONTRIBUTES NOTHING rather than being skipped — the caller
    passes None for an id that no longer resolves, and None adds no customers.
    Same rule as a deleted role conferring nothing: somebody was deliberately
    given a territory, and losing its definition must not widen them.
    """
    merged: Dict[str, Any] = {}
    for group in groups:
        if group is None:
            continue
        if group.all_access:
            return scope_rules.UNRESTRICTED
        for tenant, venues in group.ecs.items():
            current = merged.get(tenant)
            # ALL_VENUES ABSORBS A LIST. One group saying "every venue on this
            # customer" and another naming three of them means every venue;
            # intersecting instead would make adding a group take reach away,
            # which is the opposite of what ticking a second box means.
            if current == ALL_VENUES or venues == ALL_VENUES:
                merged[tenant] = ALL_VENUES
            else:
                merged[tenant] = frozenset(current or frozenset()) | frozenset(venues)
    return scope_rules.Scope(unrestricted=False, ecs=merged)


def clean_assignment(raw: Any, known: Optional[Dict[str, ResourceGroup]] = None,
                     ) -> List[str]:
    """
    One person's list of group ids, de-duplicated in first-seen order.

    AN ID FOR A GROUP THAT NO LONGER EXISTS IS KEPT, not dropped, and the
    reason is `_clean_people`'s: the assignment was deliberate, so dropping it
    would quietly change what the person holds the moment somebody deletes a
    group by mistake. It confers nothing (see `union`) and is surfaced to the
    admin instead.
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    out, seen = [], set()
    for item in items:
        if not isinstance(item, str):
            continue
        ident = item.strip()
        if not ident or ident in seen or not _ID.match(ident):
            continue
        seen.add(ident)
        out.append(ident)
    if known is not None:
        missing = [g for g in out if g not in known]
        if missing:
            logger.warning(
                "resource_groups: assignment names %d group(s) that no longer "
                "exist (%s). They confer nothing; fix it in the portal.",
                len(missing), ", ".join(missing))
    return out

"""
Which MSP-ECs and venues a role may report on.

READ THE DIFFERENCE FROM `sections.py` BEFORE CHANGING ANYTHING HERE. They live
in the same policy file and they are administered from the same portal, but
they are not the same kind of control and they do not fail the same way:

  SECTION VISIBILITY is de-cluttering. It decides which cards of a report a
  user is shown, it fails OPEN on every error path, and getting it wrong shows
  someone a card they did not need. Nobody is kept out of anything.

  SCOPE is access. It decides which tenants and venues a user may poll at all,
  it fails CLOSED on every error path, and getting it wrong shows someone
  another customer's site. On an MSP tenant those customers are different
  companies.

So this module refuses by default once a restriction exists, refuses on a
malformed entry rather than skipping past it, and is enforced at the ROUTE with
a 403 — not by filtering a response body. Filtering is also done, because a
picker listing venues a user cannot open is a list of other people's customers,
and the list is itself the disclosure. But the filter is the courtesy and the
route check is the control; never let the two swap places.

  UNRESTRICTED IS THE DEFAULT. A policy with no scope entry lets every role
  reach everything, which is what every existing deployment does today and what
  a fresh one should do. The fail-closed rule starts the moment an admin names
  a single EC: from then on, anything not named is refused.

WHAT IS NOT PROTECTED. A user who knows a venue id can still learn that it
exists by the shape of the refusal — a 403 is not a 404. That is deliberate:
pretending a venue does not exist would make a misconfigured scope
indistinguishable from a deleted venue, and the ids are not secrets, the data
behind them is. If that trade ever stops being right, change it here and say so
in the same breath, because a 404 will send someone hunting for a venue that is
sitting there working.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from config import CONTROLLER

logger = logging.getLogger(__name__)

# Sentinel for "every venue on this EC". A literal rather than an empty list,
# because an empty list has to mean "no venues" — an admin who ticks an EC and
# unticks all of its venues has said something specific, and reading that as
# "all of them" would be the single worst bug this file could have.
ALL_VENUES = "*"


@dataclass(frozen=True)
class Scope:
    """
    One role's reach.

    `unrestricted` is not the same as "ecs is empty". Empty means an admin
    named nothing, which under fail-closed means NOTHING is reachable; that is
    a real and reachable state, and it is different from never having set a
    restriction at all.
    """

    unrestricted: bool
    # tenant id -> allowed venue ids, or ALL_VENUES
    ecs: Mapping[str, Any]

    # ── the two questions the routes ask ─────────────────────────────

    def allows_ec(self, tenant_id: Optional[str]) -> bool:
        if self.unrestricted:
            return True
        return _key(tenant_id) in self.ecs

    def allows_venue(self, tenant_id: Optional[str], venue_id: str) -> bool:
        if self.unrestricted:
            return True
        allowed = self.ecs.get(_key(tenant_id))
        if allowed is None:
            return False
        return allowed == ALL_VENUES or venue_id in allowed

    # ── and the two lists they filter ────────────────────────────────

    def filter_ecs(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Keep only the ECs this role may reach.

        R1 names an EC's id inconsistently across DTOs — `id` on some,
        `tenantId` on others — so both are consulted. A row with neither is
        DROPPED rather than kept: this is the fail-closed side of the house,
        and a row we cannot identify is a row we cannot vouch for.
        """
        if self.unrestricted:
            return rows
        kept = []
        for row in rows:
            ident = row.get("id") or row.get("tenantId")
            if not ident:
                logger.warning("scope: dropped an MSP-EC row with no id: %s",
                               sorted(row)[:6])
                continue
            if ident in self.ecs:
                kept.append(row)
        return kept

    def filter_venues(self, tenant_id: Optional[str],
                      rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.unrestricted:
            return rows
        allowed = self.ecs.get(_key(tenant_id))
        if allowed is None:
            return []
        if allowed == ALL_VENUES:
            return rows
        return [row for row in rows if row.get("id") in allowed]

    # ── for the portal ───────────────────────────────────────────────

    def as_json(self) -> Dict[str, Any]:
        if self.unrestricted:
            return {"unrestricted": True, "ecs": {}}
        return {"unrestricted": False,
                "ecs": {tenant: (ALL_VENUES if venues == ALL_VENUES else sorted(venues))
                        for tenant, venues in self.ecs.items()}}


UNRESTRICTED = Scope(unrestricted=True, ecs={})


def _key(tenant_id: Optional[str]) -> str:
    """
    The tenant a request is really against.

    `resolve_tenant` returns None for an EC-type controller, which addresses
    itself — so None means "the configured tenant", and keying it under its
    real id lets one policy shape serve both controller types. Without this an
    EC deployment would have every scope entry filed under the string "None".
    """
    return tenant_id or CONTROLLER.tenant_id


def parse(raw: Any) -> Scope:
    """
    A stored scope block into a Scope. Anything unrecognisable is UNRESTRICTED.

    That looks like it contradicts fail-closed, and it is the one place it does
    not: a policy file with no scope block, or a corrupt one, describes no
    restriction at all — the same state as a deployment that has never used
    this feature. Refusing everything there would take a running instance
    offline for every user because of a stray character in a config file, which
    is an outage, not a safeguard. Fail-closed applies WITHIN a restriction
    that was successfully read: once an admin has named ECs, anything not named
    is refused, and a malformed entry inside that block is dropped rather than
    admitted.
    """
    if not isinstance(raw, dict) or raw.get("unrestricted") is not False:
        return UNRESTRICTED

    ecs_raw = raw.get("ecs")
    if not isinstance(ecs_raw, dict):
        logger.error("scope: restriction present but 'ecs' is not an object; "
                     "treating as no restriction rather than locking everyone out.")
        return UNRESTRICTED

    ecs: Dict[str, Any] = {}
    for tenant, venues in ecs_raw.items():
        if not isinstance(tenant, str) or not tenant:
            continue
        if venues == ALL_VENUES:
            ecs[tenant] = ALL_VENUES
        elif isinstance(venues, list):
            # frozenset for the membership tests below, which run per row of a
            # venue list that can be in the hundreds.
            ecs[tenant] = frozenset(v for v in venues if isinstance(v, str) and v)
        else:
            logger.warning("scope: EC %s has an unreadable venue list (%s); "
                           "granting no venues on it.", tenant, type(venues).__name__)
            ecs[tenant] = frozenset()
    return Scope(unrestricted=False, ecs=ecs)


def clean(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Validate a scope block on its way IN from the portal, for storage.

    Returns None when the admin asked for no restriction, which is stored as an
    absent key rather than as `{"unrestricted": true}` — one representation of
    "no scope", so a file cannot say it two ways.
    """
    parsed = parse(raw)
    if parsed.unrestricted:
        return None
    return {"unrestricted": False,
            "ecs": {tenant: (ALL_VENUES if venues == ALL_VENUES else sorted(venues))
                    for tenant, venues in parsed.ecs.items()}}

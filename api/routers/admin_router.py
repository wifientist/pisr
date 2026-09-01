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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import sections as section_catalogue
import visibility
from auth import require_admin

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
        description="Per-role EC/venue restriction. Omit or send null for no "
                    "restriction. Shape per role: "
                    '{"unrestricted": false, "ecs": {"<tenantId>": "*" | '
                    '["<venueId>", ...]}}. Validated by scope.clean, which '
                    "drops what it cannot read rather than admitting it.")


@router.get("/visibility")
async def get_visibility():
    """
    Everything the portal needs to draw itself in one call.

    The catalogue ships alongside the policy rather than being fetched
    separately because they are only meaningful together: a policy naming a
    section that no longer exists is invisible without the catalogue to check
    it against, and `visibility.PolicyStore` has already dropped those by the
    time this returns.
    """
    return {
        "sections": section_catalogue.catalogue(),
        "groups": section_catalogue.groups(),
        "tabs": [{"id": tab, "label": label} for tab, label in section_catalogue.TABS],
        "roles": list(visibility.MANAGED_ROLES),
        "policy": visibility.STORE.policy(),
    }


@router.put("/visibility")
async def put_visibility(body: VisibilityBody, request: Request):
    """
    Replace the policy.

    Unknown section ids are dropped rather than refused — see
    `PolicyStore._clean`. A portal built against an older catalogue should
    still be able to save the sections it does know about, instead of being
    told the whole request is invalid because one card was renamed under it.
    """
    if not visibility.STORE.configured:
        raise HTTPException(
            status_code=503,
            detail="This instance has no visibility policy file configured, so "
                   "there is nowhere to save. Set PISR_VISIBILITY_FILE and "
                   "mount a writable volume at its directory.")

    if not visibility.STORE.writable:
        # Checked before writing so the message can name the actual problem.
        # Letting the OSError surface would say "permission denied" and leave
        # the reader to work out that the volume was never mounted.
        raise HTTPException(
            status_code=503,
            detail=f"{visibility.STORE.policy().get('path')} is not writable by "
                   "the container. Mount a writable volume at its directory — "
                   "without one the policy would be lost at the next deploy.")

    # In passphrase mode there is no identity to record, and saying so beats
    # recording a plausible-looking blank. This is the audit trail such as it
    # is; SSO is what makes it worth reading.
    actor = getattr(request.state, "pisr_user", None) or "admin (passphrase)"

    try:
        return visibility.STORE.save(body.hidden, body.scope, actor)
    except RuntimeError as exc:
        logger.error("visibility: save failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

"""
The baseline editor's endpoints: read the org baseline, write the org baseline.

WHAT THIS IS FOR. The Config tab compares each venue setting against two
"recommended" columns — the customer's own (ORG) and RUCKUS's. The ORG column
used to be a hand-edited file mounted read-only; this lets an admin edit it from
the app, set each field to a recommended value, mark it explicitly "not
applicable", or leave it unreviewed, and see the RUCKUS recommendation beside it
as read-only reference. See api/baselines.py for the three-state model and why
the org baseline is a file PISR writes.

  GET /api/admin/baseline   the org baseline (per-level values, notApplicable,
                            meta, whether it can be saved), the RUCKUS values
                            per level as a read-only reference map, and the
                            static field catalogue the editor browses
  PUT /api/admin/baseline   replace the org baseline, all levels at once

LEVELS. A setting is recommended at the level R1 exposes it — venue-wide, per
AP-group, or (later) per network. Both the org baseline and RUCKUS carry a map
per level, and the editor shows one level at a time. See baselines.LEVELS.

RUCKUS IS NEVER WRITTEN HERE. It is vendor guidance, generic across every
customer, and it lives in the repository (api/baselines/ruckus.json) so two
deployments cannot disagree about what RUCKUS recommends. This router only reads
it, to populate the reference column. The one thing an admin changes about it —
placeholder → verified — is a change to that repo file, not something the app
writes.

THE FIELD CATALOGUE IS STATIC. Which settings exist is answered by the committed
`field_catalogue.json` (built from the OpenAPI spec, structured by level), sent
in the GET response — so the editor browses every settable field without an
admin loading a live venue first. Loading a venue is optional and only adds a
"now: <value>" column. See baselines.field_catalogue and the build script.

Both are behind `require_admin`, which is the enforcement. The SPA hides the
editor from non-admins, but the bundle is served unauthenticated, so the route
check is the real one.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import baselines
from auth import require_admin
from config import AUTH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/baseline", tags=["Baseline"],
                   dependencies=[Depends(require_admin)])


class LevelBody(BaseModel):
    """One level's recommendations: values and the reviewed-but-no-recommendation
    set. Keys are `<endpoint>.<dotted path>`; the level they belong to is the
    dimension the enclosing map supplies (see baselines.LEVELS)."""

    values: Dict[str, Any] = Field(
        default_factory=dict,
        description="Recommended value per `<endpoint>.<dotted path>` key.")
    notApplicable: List[str] = Field(
        default_factory=list,
        description="Keys reviewed and deliberately given no recommendation; "
                    "shown as '—' and never flagged as a mismatch.")


class NetworkGroupBody(BaseModel):
    """One network group: a match rule and its own recommendations, which
    override the network default per key for the SSIDs it matches. `match` is
    free-form so a new matcher can ship without a schema change — the server
    only understands `by`/`pattern`/`types`/`ids` today (see baselines)."""

    id: str = Field(description="Stable id a per-group recommendation is filed under.")
    name: str = Field(default="", description="Display name for the group.")
    match: Dict[str, Any] = Field(
        default_factory=dict,
        description="How a network joins: {by:'name',pattern}, {by:'type',types} "
                    "or {by:'ids',ids}.")
    values: Dict[str, Any] = Field(default_factory=dict)
    notApplicable: List[str] = Field(default_factory=list)


class BaselineBody(BaseModel):
    """
    A whole org baseline, ALL LEVELS and network groups, not a patch.

    Replacing rather than merging, for the same reason admin_router replaces the
    visibility policy: a diff has to agree with the server about what it was
    diffing against, and two admins in two tabs would silently combine their
    edits. For a setting changed a handful of times, last-write-wins is the
    honest trade.

    Every level AND every group is sent every save. The editor shows one at a
    time but holds them all, because the save replaces the document — a body
    carrying only what is on screen would delete the rest.
    """

    levels: Dict[str, LevelBody] = Field(
        default_factory=dict,
        description="Per-level recommendations, keyed by level name "
                    "(venue / apgroup / network). Unknown levels are ignored.")
    networkGroups: List[NetworkGroupBody] = Field(
        default_factory=list,
        description="Network groups, each overriding the network default for the "
                    "SSIDs it matches. Order is precedence.")
    status: str = Field(
        default="unverified",
        description="'verified' makes the column read as trustworthy; anything "
                    "else is captioned unverified. See baselines.STATUSES.")
    source: str = Field(
        default="",
        description="Where the values came from, shown in the column header.")
    show: bool = Field(
        default=True,
        description="The global switch: when false, neither recommendation "
                    "column appears in any report. The values are kept, just "
                    "not shown.")


def _actor(request: Request) -> str:
    return getattr(request.state, "pisr_user", None) or "admin"


@router.get("")
async def get_baseline():
    """
    The org baseline to edit, and the RUCKUS values for the reference column.

    `orgName` travels so the editor can title the editable column, and it comes
    from the environment rather than the file — a baseline copied between
    deployments cannot mislabel itself.
    """
    return {
        "org": baselines.org_full(),
        "orgName": AUTH.org_name,
        # Read-only, PER LEVEL. The editor shows these beside the editable org
        # values for whichever level is on screen and never sends them back.
        "ruckus": baselines.ruckus_by_level(),
        "ruckusVerified": baselines.RUCKUS.describe().get("verified", False),
        "levels": list(baselines.LEVELS),
        "statuses": list(baselines.STATUSES),
        # The static field catalogue: every settable field, so the editor can
        # browse them without an admin loading a live venue first. Empty if the
        # catalogue has not been built (the editor then falls back to a venue).
        "catalogue": baselines.field_catalogue(),
    }


@router.put("")
async def put_baseline(body: BaselineBody, request: Request):
    """Replace the org baseline."""
    if not baselines.ORG.path:
        raise HTTPException(
            status_code=503,
            detail="PISR_ORG_BASELINE_FILE is not set, so there is nowhere to "
                   "save recommendations. Set it and mount a writable volume "
                   "at its directory.")
    if not baselines.ORG.writable:
        # Checked before writing so the message names the real problem — a
        # missing volume — rather than surfacing a bare permission error.
        raise HTTPException(
            status_code=503,
            detail=f"{baselines.ORG.path} is not writable by the container. "
                   "Mount a writable volume at its directory — without one the "
                   "baseline would be lost at the next deploy.")
    try:
        levels = {name: {"values": section.values,
                         "notApplicable": section.notApplicable}
                  for name, section in body.levels.items()}
        groups = [{"id": g.id, "name": g.name, "match": g.match,
                   "values": g.values, "notApplicable": g.notApplicable}
                  for g in body.networkGroups]
        return baselines.save_org(
            levels, groups, body.status, body.source, body.show, _actor(request))
    except RuntimeError as exc:
        logger.error("baselines: save failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

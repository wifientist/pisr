"""
The baseline editor's endpoints: read the org baseline, write the org baseline.

WHAT THIS IS FOR. The Config tab compares each venue setting against two
"recommended" columns — the customer's own (ORG) and RUCKUS's. The ORG column
used to be a hand-edited file mounted read-only; this lets an admin edit it from
the app, set each field to a recommended value, mark it explicitly "not
applicable", or leave it unreviewed, and see the RUCKUS recommendation beside it
as read-only reference. See api/baselines.py for the three-state model and why
the org baseline is a file PISR writes.

  GET /api/admin/baseline   the org baseline (values, notApplicable, meta,
                            whether it can be saved) plus the RUCKUS values as
                            a read-only reference map
  PUT /api/admin/baseline   replace the org baseline

RUCKUS IS NEVER WRITTEN HERE. It is vendor guidance, generic across every
customer, and it lives in the repository (api/baselines/ruckus.json) so two
deployments cannot disagree about what RUCKUS recommends. This router only reads
it, to populate the reference column. The one thing an admin changes about it —
placeholder → verified — is a change to that repo file, not something the app
writes.

THE FIELD CATALOGUE IS NOT HERE. Which settings exist is answered by an actual
venue's config, fetched through the ordinary report route, because the field set
is whatever R1 returns for that venue and cannot be enumerated ahead of time —
`config_labels` has a de-camelCase fallback for exactly that reason. The editor
loads a venue's `config.categories` for the field list and uses these routes
only for the recommendations themselves.

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


class BaselineBody(BaseModel):
    """
    A whole org baseline, not a patch.

    Replacing rather than merging, for the same reason admin_router replaces the
    visibility policy: a diff has to agree with the server about what it was
    diffing against, and two admins in two tabs would silently combine their
    edits. For a setting changed a handful of times, last-write-wins is the
    honest trade.
    """

    values: Dict[str, Any] = Field(
        default_factory=dict,
        description="Recommended value per `<endpoint>.<dotted path>` key.")
    notApplicable: List[str] = Field(
        default_factory=list,
        description="Keys reviewed and deliberately given no recommendation; "
                    "shown as '—' and never flagged as a mismatch.")
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
        # Read-only. The editor shows these beside the editable org values and
        # never sends them back.
        "ruckus": baselines.ruckus_values(),
        "ruckusVerified": baselines.RUCKUS.describe().get("verified", False),
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
        return baselines.save_org(
            body.values, body.notApplicable, body.status, body.source,
            body.show, _actor(request))
    except RuntimeError as exc:
        logger.error("baselines: save failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

"""
The MSP-EC list, for the picker the UI shows when R1_EC_TYPE=MSP.

Path-compatible with rtools2's `/r1/{controller_id}/msp/mspEcs` so the frontend
hook that consumes it (src/hooks/useSingleEc.tsx) is carried across unmodified.
Only mspEcs is ported — PISR uses none of the other seven MSP endpoints.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

import visibility
from auth import role_of
from r1_client import build_r1_client, get_controller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/r1", tags=["MSP"])


@router.get("/{controller_id}/msp/mspEcs")
async def get_msp_ecs(request: Request, controller_id: int):
    """
    Every MSP-EC under these credentials that this caller may reach.

    Filtered by role. On an MSP tenant the ECs are different companies, so this
    list is not merely a convenience — it names one customer to another. It is
    the courtesy half of the control, though: `pisr_router._require_scope` is
    what refuses a hand-written tenant_id, and it does so whether or not the id
    ever appeared in this list.

    The explicit 400 below is a deliberate improvement on rtools2. Asking an
    EC-scoped client for MSP-ECs makes MspService return
    `{"success": False, ...}` with a **200**, which useSingleEc reads as neither
    an array nor a `.data` array and turns into an empty list — so the picker
    says "no ECs found", which looks like an empty MSP rather than the
    misconfiguration it is. Say what is actually wrong instead.
    """
    cfg = get_controller(controller_id)
    if cfg.ec_type != "MSP":
        raise HTTPException(
            400,
            f"R1_EC_TYPE is {cfg.ec_type} — these credentials address a single "
            "tenant, so there are no MSP-ECs to list.")

    r1 = build_r1_client(cfg)
    rows = await r1.msp.get_msp_ecs()

    allowed = visibility.scope_for(role_of(request))
    if allowed.unrestricted:
        return rows

    # R1 has returned this as a bare list and as {"data": [...]} depending on
    # the endpoint version, and useSingleEc.tsx accepts both. Filter whichever
    # shape arrived and hand back the same shape, so the frontend contract is
    # unchanged. Anything else is passed through untouched rather than guessed
    # at — but it is logged, because an unfiltered pass-through on THIS list is
    # exactly the failure that matters.
    if isinstance(rows, list):
        return allowed.filter_ecs(rows)
    if isinstance(rows, dict) and isinstance(rows.get("data"), list):
        return {**rows, "data": allowed.filter_ecs(rows["data"])}

    logger.error(
        "scope: the MSP-EC list came back as %s, which this cannot filter, so "
        "a scoped user would see every customer. Refusing instead.",
        type(rows).__name__)
    raise HTTPException(
        502, "The MSP-EC list came back in an unexpected shape and could not "
             "be filtered for this account.")

"""
The MSP-EC list, for the picker the UI shows when R1_EC_TYPE=MSP.

Path-compatible with rtools2's `/r1/{controller_id}/msp/mspEcs` so the frontend
hook that consumes it (src/hooks/useSingleEc.tsx) is carried across unmodified.
Only mspEcs is ported — PISR uses none of the other seven MSP endpoints.
"""

import logging

from fastapi import APIRouter, HTTPException

from r1_client import build_r1_client, get_controller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/r1", tags=["MSP"])


@router.get("/{controller_id}/msp/mspEcs")
async def get_msp_ecs(controller_id: int):
    """
    Every MSP-EC under these credentials.

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
    return await r1.msp.get_msp_ecs()

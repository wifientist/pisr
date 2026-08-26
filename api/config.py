"""
PISR configuration — the pseudo-controller, read from .env.

In rtools2 this was a `Controller` row in Postgres holding Fernet-encrypted
credentials, selected per-user from a dropdown. Standalone PISR serves exactly
one RUCKUS ONE tenant, so the row becomes a frozen dataclass built from the
environment at import time.

It is loaded eagerly and validated strictly. A container that refuses to start
saying `R1_TENANT_ID is not set` is worth far more than one that boots happily
and 500s on every request.
"""

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# R1Client's vocabulary, not the old Controller model's. `client.py` maps
# EU -> api.eu.ruckus.cloud, ASIA -> api.asia.ruckus.cloud and falls through to
# NA for anything else — so the old model's "APAC" would have silently reported
# on the wrong cloud. Rejecting it here is the point.
REGIONS = {"NA", "EU", "ASIA"}

# Compared as an exact string in three places (fetch._get, fetch._post,
# MspService.get_msp_ecs). A lowercase "msp" would silently drop the
# x-rks-tenantid header and report on the MSP's own venue-less tenant — an
# empty report with no error anywhere. Hence the .upper() and the membership
# check below.
EC_TYPES = {"EC", "MSP"}


@dataclass(frozen=True)
class ControllerConfig:
    """What a Controller row used to hold, minus the encryption and the DB."""

    id: int
    name: str
    tenant_id: str
    client_id: str
    shared_secret: str
    region: str   # NA | EU | ASIA
    ec_type: str  # EC | MSP
    controller_type: str = "RuckusONE"


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _choice(name: str, default: str, allowed: set) -> str:
    value = (os.getenv(name) or default).strip().upper()
    if value not in allowed:
        raise RuntimeError(
            f"{name} must be one of {sorted(allowed)} — got {value!r}.")
    return value


def _load() -> ControllerConfig:
    raw_id = (os.getenv("R1_CONTROLLER_ID") or "1").strip()
    try:
        controller_id = int(raw_id)
    except ValueError:
        raise RuntimeError(f"R1_CONTROLLER_ID must be an integer — got {raw_id!r}.")

    return ControllerConfig(
        id=controller_id,
        name=(os.getenv("R1_CONTROLLER_NAME") or "RUCKUS ONE").strip(),
        tenant_id=_required("R1_TENANT_ID"),
        client_id=_required("R1_CLIENT_ID"),
        shared_secret=_required("R1_SHARED_SECRET"),
        region=_choice("R1_REGION", "NA", REGIONS),
        ec_type=_choice("R1_EC_TYPE", "EC", EC_TYPES),
    )


CONTROLLER: ControllerConfig = _load()


def public_config() -> dict:
    """
    What the SPA is allowed to know. The tenant id is not a secret — it is in
    every URL of the RUCKUS ONE console — but the client id and shared secret
    never leave the process.

    snake_case on purpose: this matches the shape rtools2 served in
    `controllers[]`, so the frontend shim maps it over unchanged.
    """
    return {
        "id": CONTROLLER.id,
        "name": CONTROLLER.name,
        "controller_type": CONTROLLER.controller_type,
        "controller_subtype": CONTROLLER.ec_type,
        "r1_tenant_id": CONTROLLER.tenant_id,
        "r1_region": CONTROLLER.region,
    }

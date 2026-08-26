"""
PISR collection — one venue, one poll, one report.

Fans every read out at once (they are independent), shapes what comes back, then
runs the checks over the shaped result. A section that fails to load is recorded
in `errors` and the rest of the report still renders: a half-read venue is worth
more than an error page, as long as the page says which half is missing.

READ-ONLY and HUMAN-TRIGGERED. See services/pisr/fetch.py.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.pisr import checks, fetch, shape

logger = logging.getLogger(__name__)


async def list_venues(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    The venue picker's data: every venue, with whatever counts R1 already
    aggregates. Counts are best-effort — `aggregatedApStatus` is a per-build
    shape, so it is normalised defensively and omitted rather than guessed.
    """
    rows = await asyncio.to_thread(fetch.venue_rows, r1, tenant_id)

    venues = []
    for row in rows:
        address = row.get("address") if isinstance(row.get("address"), dict) else {}
        venues.append({
            "id": row.get("id"),
            "name": row.get("name"),
            "addressLine": row.get("addressLine") or address.get("addressLine"),
            "city": row.get("city") or address.get("city"),
            "country": row.get("country") or address.get("country"),
            "aps": _ap_counts(row.get("aggregatedApStatus")),
            "switches": _int_or_none(row.get("operationalSwitches")),
            "clients": _int_or_none(row.get("clients")),
            "networks": _count_of(row.get("networks")),
            "firmwareUpToDate": row.get("isApFirmwareUpToDate"),
        })
    venues.sort(key=lambda v: (v["name"] or "").lower())
    return venues


def _int_or_none(value) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _count_of(value) -> Optional[int]:
    if isinstance(value, list):
        return len(value)
    return _int_or_none(value)


def _ap_counts(aggregated) -> Optional[Dict[str, int]]:
    """
    `aggregatedApStatus` arrives as a dict of status -> count on the builds we
    have seen, and as a list of {status, count} on others. Anything else is
    dropped: the picker shows no counts rather than wrong ones.
    """
    buckets: Dict[str, int] = {}
    if isinstance(aggregated, dict):
        for key, value in aggregated.items():
            count = _int_or_none(value)
            if count is not None:
                buckets[str(key)] = count
    elif isinstance(aggregated, list):
        for entry in aggregated:
            if not isinstance(entry, dict):
                return None
            key = entry.get("status") or entry.get("name") or entry.get("label")
            count = _int_or_none(entry.get("count") or entry.get("value"))
            if key and count is not None:
                buckets[str(key)] = count
    if not buckets:
        return None

    total = sum(buckets.values())
    online = sum(count for status, count in buckets.items()
                 if shape._state(status) == "online")
    offline = sum(count for status, count in buckets.items()
                  if shape._state(status) == "offline")
    return {"total": total, "online": online, "offline": offline, "byStatus": buckets}


async def build_report(r1, tenant_id: Optional[str], venue_id: str) -> Dict[str, Any]:
    """One poll of one venue. Every read runs concurrently; nothing is cached."""
    started = time.time()

    reads = {
        "venue": (fetch.venue_detail, (r1, tenant_id, venue_id)),
        "property": (fetch.property_config, (r1, tenant_id, venue_id)),
        "units": (fetch.property_units, (r1, tenant_id, venue_id)),
        "mgmtVlan": (fetch.ap_management_vlan, (r1, tenant_id, venue_id)),
        "dhcpPools": (fetch.dhcp_pools, (r1, tenant_id, venue_id)),
        "apGroups": (fetch.ap_groups, (r1, tenant_id, venue_id)),
        "radio": (fetch.radio_settings, (r1, tenant_id, venue_id)),
        "mesh": (fetch.mesh_settings, (r1, tenant_id, venue_id)),
        "aps": (fetch.access_points, (r1, tenant_id, venue_id)),
        "apTotal": (fetch.venue_ap_total, (r1, tenant_id, venue_id)),
        "switches": (fetch.switches, (r1, tenant_id, venue_id)),
        "ports": (fetch.switch_ports, (r1, tenant_id, venue_id)),
        "clients": (fetch.clients, (r1, tenant_id, venue_id)),
        "networks": (fetch.wifi_networks, (r1, tenant_id)),
        "activations": (fetch.venue_activations, (r1, tenant_id, venue_id)),
        "dpskPools": (fetch.dpsk_pools, (r1, tenant_id)),
        "identityGroups": (fetch.identity_groups_all, (r1, tenant_id)),
        "policySets": (fetch.policy_sets, (r1, tenant_id)),
        "policies": (fetch.adaptive_policies, (r1, tenant_id)),
        "radiusGroups": (fetch.radius_attribute_groups, (r1, tenant_id)),
    }

    keys = list(reads)
    results = await asyncio.gather(
        *(asyncio.to_thread(reads[key][0], *reads[key][1]) for key in keys),
        return_exceptions=True,
    )

    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    empty = {"venue": {}, "property": None, "units": {}, "mgmtVlan": None,
             "dhcpPools": [], "apGroups": [], "radio": {}, "mesh": {}, "aps": [],
             "apTotal": None,
             "switches": [], "ports": [], "clients": [], "networks": [], "activations": [],
             "dpskPools": [], "identityGroups": {"rows": [], "total": 0, "complete": True},
             "policySets": [], "policies": [], "radiusGroups": []}
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            logger.warning("pisr: %s read failed for venue %s: %s", key, venue_id, result)
            errors[key] = str(result)
            raw[key] = empty[key]
        else:
            raw[key] = result

    # Passphrase counts are a second, smaller fan-out: they need the pool list
    # from the round above, and only the pools this venue actually activates are
    # worth asking about. One call per pool, count only — never the rows.
    # Same two links shape.dpsk_card scopes by — activated networks, and an
    # identity group whose `propertyId` is this venue's id. Counting only the
    # first left a property-linked pool with an unknown passphrase count, which
    # then reported as "could not be read" rather than the number it has.
    activated_ids = {a.get("networkId") for a in raw["activations"] if a.get("networkId")}
    property_pool_ids = {
        g.get("dpskPoolId")
        for g in (raw["identityGroups"].get("rows") or [])
        if g.get("dpskPoolId") and g.get("propertyId") == venue_id
    }
    venue_pools = [p for p in raw["dpskPools"]
                   if (set(p.get("networkIds") or []) & activated_ids)
                   or p.get("id") in property_pool_ids]
    passphrase_counts: Dict[str, Any] = {}
    if venue_pools:
        counts = await asyncio.gather(
            *(asyncio.to_thread(fetch.dpsk_passphrase_count, r1, tenant_id, p["id"])
              for p in venue_pools),
            return_exceptions=True,
        )
        for pool, count in zip(venue_pools, counts):
            if isinstance(count, Exception):
                logger.warning("pisr: passphrase count failed for pool %s: %s",
                               pool.get("id"), count)
                passphrase_counts[pool["id"]] = None
            else:
                passphrase_counts[pool["id"]] = count

    # The nested AP and client rows are flattened once, here, and every shaper
    # downstream reads the flat views.
    aps = shape.ap_views(raw["aps"], raw["apGroups"])
    clients = shape.client_views(raw["clients"])

    dpsk = shape.dpsk_card(
        raw["dpskPools"], raw["identityGroups"], raw["activations"],
        raw["networks"], venue_id, None, passphrase_counts)

    # Policy detail is a third, smaller round: it needs the scoped policy sets
    # from the DPSK card above, and only those sets' members and RADIUS groups
    # are worth fetching.
    scoped_set_ids = {row["policySetId"] for row in dpsk["pools"] if row.get("policySetId")}
    for group in dpsk.get("otherIdentityGroups") or []:
        if group.get("policySetId"):
            scoped_set_ids.add(group["policySetId"])
    pool_ids = {row["id"] for row in dpsk["pools"]}
    for policy_set in raw["policySets"]:
        for assignment in policy_set.get("externalAssignments") or []:
            # identityId is a LIST on this DTO despite the singular name.
            if pool_ids & set(shape._assignment_identity_ids(assignment)):
                scoped_set_ids.add(policy_set.get("id"))

    set_members: Dict[str, Any] = {}
    group_assignments: Dict[str, Any] = {}
    if scoped_set_ids:
        ordered = sorted(sid for sid in scoped_set_ids if sid)
        results = await asyncio.gather(
            *(asyncio.to_thread(fetch.policy_set_members, r1, tenant_id, sid)
              for sid in ordered),
            return_exceptions=True,
        )
        for sid, result in zip(ordered, results):
            set_members[sid] = [] if isinstance(result, Exception) else result

        wanted_radius = sorted({
            p.get("onMatchResponse")
            for members in set_members.values() for m in members
            for p in raw["policies"]
            if p.get("id") == m.get("policyId") and p.get("onMatchResponse")
        })
        if wanted_radius:
            results = await asyncio.gather(
                *(asyncio.to_thread(fetch.radius_group_assignments, r1, tenant_id, gid)
                  for gid in wanted_radius),
                return_exceptions=True,
            )
            for gid, result in zip(wanted_radius, results):
                group_assignments[gid] = [] if isinstance(result, Exception) else result

    policy = shape.policy_card(
        dpsk["pools"], dpsk.get("otherIdentityGroups") or [], raw["policySets"],
        raw["policies"], raw["radiusGroups"], set_members, group_assignments)

    report: Dict[str, Any] = {
        "venue": shape.venue_card(raw["venue"], raw["property"], raw["units"],
                                  raw["mgmtVlan"], raw["radio"], raw["mesh"]),
        "inventory": shape.inventory_card(aps, raw["switches"]),
        "addressing": shape.addressing_card(aps, raw["switches"], raw["dhcpPools"]),
        "poe": shape.poe_card(raw["switches"], raw["ports"], aps),
        "ports": shape.port_card(raw["ports"]),
        "radios": shape.radio_card(aps),
        "clients": shape.client_card(clients),
        "dpsk": dpsk,
        "policy": policy,
    }
    report["wireless"] = shape.wireless_card(raw["networks"], raw["activations"],
                                             raw["apGroups"], aps, clients)
    # VLANs read from every other section, so it is shaped last.
    report["vlans"] = shape.vlan_card(raw["ports"], report["wireless"]["rows"],
                                      raw["mgmtVlan"], raw["dhcpPools"], aps, clients)
    # Needs the venue card's configured channel list and the observed radio
    # bands, so it is attached once both are built.
    # Did R1 hand over every AP it says the venue has? A short list makes any
    # group whose APs are missing look empty, which reads exactly like a real
    # finding — so it is recorded and the AP-group check declines on it.
    reported_total = raw["apTotal"]
    report["inventory"]["aps"]["reportedTotal"] = reported_total
    report["inventory"]["aps"]["truncated"] = bool(
        isinstance(reported_total, int) and reported_total > len(aps))
    if report["inventory"]["aps"]["truncated"]:
        logger.warning("pisr: venue %s reports %s APs but the query returned %s",
                       venue_id, reported_total, len(aps))

    report["radios"]["plan"] = shape.channel_plan(
        report["venue"].get("radio") or [], aps)

    report["verification"] = checks.run_checks(report)

    report["meta"] = {
        "venueId": venue_id,
        "tenantId": tenant_id,
        "polledAt": datetime.now(timezone.utc).isoformat(),
        "elapsedSeconds": round(time.time() - started, 1),
        "errors": errors,
        "counts": {
            "aps": len(raw["aps"]),
            "switches": len(raw["switches"]),
            "ports": len(raw["ports"]),
            "clients": len(raw["clients"]),
            "networks": len(raw["networks"]),
            "activations": len(raw["activations"]),
        },
        # What PISR read, so the report can show its own sources. Every one is a
        # GET or a query POST.
        "sources": [
            "GET /venues/{venueId}",
            "GET /venues/{venueId}/propertyConfigs",
            "GET /venues/{venueId}/units",
            "GET /venues/{venueId}/apManagementTrafficVlanSettings",
            "GET /venues/{venueId}/dhcpPools",
            "GET /venues/{venueId}/apGroups",
            "GET /venues/{venueId}/apRadioSettings",
            "GET /venues/{venueId}/apMeshSettings",
            "POST /venues/aps/query",
            "POST /venues/switches/query",
            "POST /venues/switches/switchPorts/query",
            "POST /venues/aps/clients/query",
            "POST /wifiNetworks/query",
            "POST /venues/wifiNetworks/query",
        ],
    }
    logger.info("pisr: venue %s report built in %.1fs (%d APs, %d switches, %d ports, "
                "%d clients, %d errors)", venue_id, report["meta"]["elapsedSeconds"],
                len(raw["aps"]), len(raw["switches"]), len(raw["ports"]),
                len(raw["clients"]), len(errors))
    return report

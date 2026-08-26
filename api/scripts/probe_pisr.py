"""
Probe: does a real tenant populate the fields PISR builds its report from?

The OpenAPI spec documents the *surface*. It does not say whether `IP` and
`extIp` come back on an AP row, whether a venue has a propertyConfig, or what a
venue's Wi-Fi activations actually look like. This walks exactly the reads PISR
makes and reports population rates, then builds one real report so the shaping
and the checks are exercised against live data.

Everything here is a GET or a `*/query` POST — the same read-only surface the
tool itself uses. Nothing is created, modified, deleted, rebooted or synced.

Usage:
    docker compose run --rm pisr python scripts/probe_pisr.py [options]

    (or, against the dev stack:
     docker compose -f docker-compose.dev.yml exec backend python scripts/probe_pisr.py)

The tenant comes from .env — there is no controller to name on the command line.

Options:
    --tenant <id>   MSP-EC to probe (when R1_EC_TYPE=MSP; default: first EC with venues)
    --venue <id>    venue to report on (default: the venue with the most APs)
    --json          dump the whole report instead of the summary
"""
import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import CONTROLLER
from r1_client import build_r1_client
from services.pisr import fetch
from services.pisr.collect import build_report, list_venues


def population(rows, fields):
    """How many rows carry a non-empty value for each field."""
    total = len(rows) or 1
    counts = Counter()
    for row in rows:
        for field in fields:
            value = row.get(field)
            if value not in (None, "", [], {}):
                counts[field] += 1
    return {field: f"{counts[field]}/{len(rows)} ({counts[field] * 100 // total}%)"
            for field in fields}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant")
    parser.add_argument("--venue")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    print(f"controller: {CONTROLLER.name} ({CONTROLLER.controller_type}/"
          f"{CONTROLLER.ec_type}) region={CONTROLLER.region} "
          f"tenant={CONTROLLER.tenant_id}")

    r1 = build_r1_client(CONTROLLER)

    tenant = args.tenant
    if CONTROLLER.ec_type == "MSP" and not tenant:
        ecs = (await r1.msp.get_msp_ecs()) or []
        rows = ecs.get("data") if isinstance(ecs, dict) else ecs
        for ec in rows or []:
            candidate = ec.get("id") or ec.get("tenantId")
            if candidate and await list_venues(r1, candidate):
                tenant, ec_name = candidate, ec.get("name")
                print(f"EC: {ec_name} ({tenant})")
                break
        if not tenant:
            sys.exit("No EC under this MSP has a venue.")

    venues = await list_venues(r1, tenant)
    print(f"venues: {len(venues)}")
    if not venues:
        sys.exit("Nothing to report on.")
    print(json.dumps(venues[:5], indent=1))

    venue_id = args.venue
    if not venue_id:
        with_aps = [v for v in venues if (v.get("aps") or {}).get("total")]
        pick = max(with_aps, key=lambda v: v["aps"]["total"]) if with_aps else venues[0]
        venue_id = pick["id"]
        print(f"venue: {pick['name']} ({venue_id})")

    # ── field population, the part the spec cannot answer ──
    aps = await asyncio.to_thread(fetch.access_points, r1, tenant, venue_id)
    print(f"\nAPs: {len(aps)}")
    if aps:
        print(json.dumps(population(aps, fetch.AP_FIELDS), indent=1))
        print("sample AP:", json.dumps(aps[0], indent=1)[:900])

    switches = await asyncio.to_thread(fetch.switches, r1, tenant, venue_id)
    print(f"\nswitches: {len(switches)}")
    if switches:
        print(json.dumps(population(switches, ["ipAddress", "poeTotal", "poeFree",
                                               "poeUtilization", "defaultGateway", "dns",
                                               "staticOrDynamic", "syncedSwitchConfig"]), indent=1))

    activations = await asyncio.to_thread(fetch.venue_activations, r1, tenant, venue_id)
    print(f"\nvenue SSID activations: {len(activations)}")
    print(json.dumps(activations[:3], indent=1))

    prop = await asyncio.to_thread(fetch.property_config, r1, tenant, venue_id)
    print("\npropertyConfig:", "none (not a property)" if prop is None
          else json.dumps(prop, indent=1)[:500])

    print("mgmt VLAN:", await asyncio.to_thread(fetch.ap_management_vlan, r1, tenant, venue_id))
    pools = await asyncio.to_thread(fetch.dhcp_pools, r1, tenant, venue_id)
    print("dhcp pools:", len(pools))

    # ── the real thing ──
    report = await build_report(r1, tenant, venue_id)
    if args.json:
        print(json.dumps(report, indent=1, default=str))
        return

    print(f"\n=== report ({report['meta']['elapsedSeconds']}s) ===")
    print("counts:", json.dumps(report["meta"]["counts"]))
    if report["meta"]["errors"]:
        print("ERRORS:", json.dumps(report["meta"]["errors"], indent=1))
    print("venue:", json.dumps(report["venue"], indent=1)[:700])
    print("addressing:", json.dumps({k: v for k, v in report["addressing"].items()
                                     if k != "dhcpPools"}, indent=1)[:900])
    print("poe:", json.dumps({k: v for k, v in report["poe"].items()
                              if k not in ("apsOnPoe", "topConsumers", "switches")}, indent=1))
    print("vlans:", json.dumps(report["vlans"]["rows"][:12], indent=1))
    print("wireless:", json.dumps([{k: v for k, v in row.items() if k != "scopes"}
                                   for row in report["wireless"]["rows"]], indent=1)[:1500])
    print("clients:", json.dumps(report["clients"], indent=1)[:600])
    print("\nfindings:")
    for finding in report["verification"]["findings"]:
        print(f"  [{finding['severity']:>8}] {finding['title']}: {finding['summary']}")
    print(json.dumps(report["verification"]["counts"]))


if __name__ == "__main__":
    asyncio.run(main())

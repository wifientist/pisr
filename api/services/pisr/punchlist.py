"""
The punch list: everything outstanding, grouped by who fixes it.

WHY THIS EXISTS SEPARATELY FROM `verification`. The rest of the report is
organised by subsystem — wireless here, wired there, addressing over there —
which is the right shape for understanding a venue and the wrong shape for
finishing one. A crew standing in a riser does not want six tabs; they want a
list of what is not done, ordered so the next thing to do is at the top.

So this re-cuts the same findings by TRADE rather than by subsystem. A port
error and an AP that fell back to mesh are the same visit with the same ladder;
a mismatched firmware version is a different person entirely, probably not even
on site. Grouping by subsystem puts the first two on different tabs and the
third next to one of them, which is exactly backwards for the person doing the
work.

IT ADDS NO NEW DATA. Every task here is a finding `checks.py` already produced
or an alarm R1 already raised. If something is wrong with a task, the bug is in
the check, not here. This module decides grouping, ordering and wording, and
nothing else.

WHAT IT DELIBERATELY CANNOT DO. There is no "fixed since yesterday", no ticking
items off, and no history — PISR stores nothing, so every list is built fresh
from the poll in front of it. That is a real limitation for a crew working a
site over days, and the honest workaround today is to export the PDF at the end
of each visit. Making it stateful means giving PISR somewhere to write, which
is a decision about the whole tool rather than a feature of this file.
"""

from typing import Any, Dict, List, Optional, Tuple

# The order a venue actually gets finished in, and therefore the order the list
# is presented in. Devices first because nothing else can be judged until the
# hardware is up; documentation last because it is the only group that does not
# stop the site working.
CATEGORIES: Tuple[Tuple[str, str, str], ...] = (
    ("devices", "Devices not up",
     "Hardware that is missing, offline, or has not finished provisioning. "
     "Nothing below can be trusted until this group is empty."),
    ("cabling", "Cabling & uplinks",
     "Ports, patching and links. One visit with a ladder and a tester."),
    ("power", "Power",
     "PoE budget and anything that looks like a device losing power."),
    ("wireless", "Wireless configuration",
     "SSIDs, AP groups and the channel plan — config, not hardware."),
    ("network", "Addressing & VLANs",
     "Management VLAN, DHCP, subnets and VLANs carried on the wire."),
    ("identity", "Identity & policy",
     "DPSK pools, identity groups, adaptive policy and RADIUS."),
    # RUCKUS ONE alarms are NOT a punch-list group: the platform raises them and
    # they already have their own place on Overview beside Verification. Re-cutting
    # them here duplicated them, so the punch list stays PISR's own findings only.
    ("documentation", "Documentation & handover",
     "Naming placement. Does not stop the site working, and is the group that "
     "never gets done after the crew leaves."),
)

# Checks kept OFF the punch list and shown on Overview only. Floor plans are a
# documentation notice, not a visit a crew plans around — flagged on Overview
# where an admin reviews the venue, not filed as a task with a ladder.
PUNCHLIST_EXCLUDE = frozenset({"floorplans"})

CATEGORY_ORDER = {key: index for index, (key, _, _) in enumerate(CATEGORIES)}
CATEGORY_LABELS = {key: (label, blurb) for key, label, blurb in CATEGORIES}

# check id -> category. A check missing from this map lands in "devices", which
# is the group most likely to be looked at, on the principle that an
# uncategorised problem should be over-reported rather than filed somewhere
# nobody opens. `test_every_check_has_a_category` stops that being the norm.
CHECK_CATEGORY: Dict[str, str] = {
    # Devices not up
    "aps-online": "devices",
    "switches-online": "devices",
    "aps-provisioned": "devices",
    "ap-firmware": "devices",
    "switch-firmware": "devices",
    "switch-config-sync": "devices",
    # Cabling & uplinks
    "port-errors": "cabling",
    "ap-uplink-speed": "cabling",
    "ap-mesh-fallback": "cabling",
    # Power
    "poe-budget": "power",
    "ap-uptime": "power",
    # Wireless configuration
    "ssids-activated": "wireless",
    "ssids-broadcasting": "wireless",
    "ssids-carrying": "wireless",
    "ssid-scope": "wireless",
    "ssid-vlan-carried": "wireless",
    "ap-group-ssid-limit": "wireless",
    "24ghz-channel-plan": "wireless",
    "clients-present": "wireless",
    # Addressing & VLANs
    "mgmt-vlan": "network",
    "ap-addressing": "network",
    "undeclared-vlans": "network",
    "dhcp-pools": "network",
    "external-ip": "network",
    # Identity & policy
    "dpsk-in-use": "identity",
    "dpsk-passphrases": "identity",
    "dpsk-identity-groups": "identity",
    "policy-chain": "identity",
    "radius-group-orphans": "identity",
    # Documentation & handover
    "ap-naming": "documentation",
    "ap-placement": "documentation",
    # "floorplans" is deliberately absent — it is in PUNCHLIST_EXCLUDE, shown on
    # Overview only, not a punch-list task.
}

# Only these ask something of the reader. "ok" is done and "skipped" is neither
# — see below, it gets its own list rather than being silently dropped.
ACTIONABLE = ("critical", "warning", "info")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def _task_from_finding(finding: Dict[str, Any]) -> Dict[str, Any]:
    check_id = finding.get("id") or ""
    return {
        "id": check_id,
        "source": "check",
        "category": CHECK_CATEGORY.get(check_id, "devices"),
        "severity": finding.get("severity"),
        "title": finding.get("title") or finding.get("check") or check_id,
        "summary": finding.get("summary"),
        "detail": finding.get("detail"),
        # The devices to visit. This is what makes a task actionable rather
        # than a statement — "3 APs are offline" is a fact, the list of which
        # three is the work.
        "evidence": finding.get("evidence") or [],
        "count": len(finding.get("evidence") or []),
    }




def build(verification: Dict[str, Any]) -> Dict[str, Any]:
    """
    One list of outstanding work, grouped by trade and ordered by severity.

    PISR's own findings only — R1 alarms live on Overview, not here, and the
    checks in PUNCHLIST_EXCLUDE (floor plans) are Overview notices rather than
    visits a crew plans around.

    Passes are counted, not listed — a punch list of things that are already
    done is a report, not a list. Skipped checks ARE listed, separately: a
    check that could not run is not a pass, and on an install it usually means
    a prerequisite is missing rather than that everything is fine.
    """
    findings = (verification or {}).get("findings") or []

    tasks = [_task_from_finding(f) for f in findings
             if f.get("severity") in ACTIONABLE
             and f.get("id") not in PUNCHLIST_EXCLUDE]

    tasks.sort(key=lambda t: (CATEGORY_ORDER.get(t["category"], 99),
                              SEVERITY_RANK.get(t["severity"], 9),
                              str(t["title"])))

    groups = []
    for key, label, blurb in CATEGORIES:
        in_group = [t for t in tasks if t["category"] == key]
        if not in_group:
            continue
        groups.append({
            "key": key, "label": label, "blurb": blurb,
            "tasks": in_group,
            "counts": {level: sum(1 for t in in_group if t["severity"] == level)
                       for level in ACTIONABLE},
            # Devices to visit for this group, deduplicated across its tasks —
            # the number a crew actually plans around.
            "devices": sorted({
                str(row.get("ap") or row.get("switch") or row.get("device") or "")
                for t in in_group for row in t["evidence"]
                if (row.get("ap") or row.get("switch") or row.get("device"))
            }),
        })

    skipped = [{"id": f.get("id"), "title": f.get("check") or f.get("id"),
                "summary": f.get("summary")}
               for f in findings if f.get("severity") == "skipped"]

    return {
        "total": len(tasks),
        "counts": {level: sum(1 for t in tasks if t["severity"] == level)
                   for level in ACTIONABLE},
        "passed": sum(1 for f in findings if f.get("severity") == "ok"),
        "groups": groups,
        "skipped": skipped,
        # Everything a crew would have to visit, across every group.
        "deviceCount": len({d for g in groups for d in g["devices"]}),
        # Check ids kept off the punch list and shown on Overview instead, so the
        # PDF can render them there even when the punch list has collapsed the
        # rest of the Overview findings.
        "overviewOnly": sorted(PUNCHLIST_EXCLUDE),
    }

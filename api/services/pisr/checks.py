"""
PISR checks — the verification half of the report.

Each check reads the shaped sections and returns one finding, always. A check
that passes says so ("ok"), and a check that could not run says that too
("skipped") rather than quietly disappearing — a report is only evidence if you
can see what was looked at as well as what was wrong.

Severities: critical | warning | info | ok | skipped.

Nothing here talks to RUCKUS ONE, and nothing here fixes anything. PISR reports.
"""

import logging
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# R1 will not activate more than this many SSIDs on one AP Group.
AP_GROUP_SSID_LIMIT = 15

# Chassis PoE allocation thresholds, as a percentage of the switch's budget.
POE_WARN_PCT = 85.0
POE_CRIT_PCT = 95.0

# A DHCP pool this full is about to stop handing out addresses.
DHCP_WARN_PCT = 85.0


def _join(values, empty: str = "—") -> str:
    """
    ", ".join that survives None.

    R1 hands back null names in places that read like they cannot be null — an
    AP group with no name, a radio list with a null entry — and a bare join dies
    on the first one with "sequence item 0: expected str instance, NoneType
    found", taking the whole check with it. Evidence rows are display strings;
    a missing value should print as a dash, not abort the check.
    """
    text = [str(value) for value in (values or []) if value is not None and str(value) != ""]
    return ", ".join(text) if text else empty


def _duration(seconds: Optional[float]) -> str:
    """
    Seconds as something a person reads: "3 days", "2 hours", "14 minutes".

    Its own int() rather than shape._num: this module imports nothing from the
    shapers on purpose, so that a check is only ever a pure reader of the
    assembled report.
    """
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "unknown"
    if total <= 0:
        return "unknown"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if total >= size:
            value = total // size
            return f"{value} {unit}{'s' if value != 1 else ''}"
    return f"{total} second{'s' if total != 1 else ''}"


def _finding(check_id: str, name: str, severity: str, summary: str,
             evidence: Optional[List[Dict[str, Any]]] = None,
             detail: Optional[str] = None,
             headline: Optional[str] = None) -> Dict[str, Any]:
    """
    One finding.

    `name` is the stable thing that was tested, always phrased as the passing
    state ("AP uplinks negotiated at full speed"). `headline` is what this
    particular run found, and it is what gets shown.

    They are separate because they were not always: every branch used to render
    the pass-worded name, so a failure announced "AP uplinks negotiated at full
    speed" in bold and then contradicted itself in the summary underneath. A
    finding should lead with its own verdict, so failing branches state the
    problem and only a pass falls back to the name.
    """
    return {
        "id": check_id,
        "check": name,
        "title": headline or name,
        "severity": severity,
        "summary": summary,
        "detail": detail,
        "evidence": evidence or [],
    }


# ── device presence ──────────────────────────────────────────

def check_aps_online(report: Dict[str, Any]) -> Dict[str, Any]:
    aps = report["inventory"]["aps"]
    rows = report["inventory"]["rows"]["aps"]
    if not aps["total"]:
        return _finding("aps-online", "APs reachable", "skipped",
                        "No APs are assigned to this venue.")
    offline = [r for r in rows if r["state"] == "offline"]
    if offline:
        return _finding("aps-online", "APs reachable", "critical",
                        f"{len(offline)} of {aps['total']} APs are offline.",
                        headline=f"{len(offline)} AP(s) are offline",
                        evidence=[{"ap": r["name"], "serial": r["serial"], "model": r["model"],
                          "status": r["status"], "lastSeen": r["lastSeen"],
                          "switch": r["uplinkSwitch"], "port": r["uplinkPort"]}
                         for r in offline])
    # `offline` alone does not settle reachability. Anything mid-flight or
    # reporting a failed update sits in "other", so a venue can have zero
    # offline APs and still not be fully up. Claiming "All N APs are online"
    # over a smaller online count is the false pass a site review exists to
    # prevent. (Never-contacted APs now count as offline and are caught above.)
    stranded = aps["total"] - aps["online"]
    if stranded > 0:
        named = _join(f"{row['count']} {str(row['label']).lower()}"
                      for row in aps.get("notOnlineByStatus") or [])
        return _finding("aps-online", "APs reachable", "warning",
                        f"{aps['online']} of {aps['total']} APs are online. The other "
                        f"{stranded} are not offline either — {named}. See "
                        f"“APs finished provisioning” for what that state means.",
                        headline=f"{stranded} AP(s) are not online",
                        evidence=[{"ap": r["name"], "serial": r["serial"],
                                   "model": r["model"], "status": r["status"],
                                   "lastSeen": r["lastSeen"]}
                                  for r in rows if r["state"] not in ("online", "offline")])
    return _finding("aps-online", "APs reachable", "ok",
                    f"All {aps['total']} APs are online.")


def check_aps_provisioned(report: Dict[str, Any]) -> Dict[str, Any]:
    rows = report["inventory"]["rows"]["aps"]
    if not rows:
        return _finding("aps-provisioned", "APs finished provisioning", "skipped",
                        "No APs are assigned to this venue.")
    # Gathered independently, not both off state == "other". A failed update
    # counts as ONLINE (the AP is reachable, see shape._state) so keying this
    # check on "other" alone would silently drop the exact fault it exists to
    # report. The two sets cannot overlap: failedUpdate implies online.
    failed = [r for r in rows if r.get("failedUpdate")]
    moving = [r for r in rows if r["state"] == "other"]
    limbo = failed + moving
    if limbo:
        # Two very different things live in this bucket. Initializing /
        # Applying* / Rebooting are in flight and will resolve on their own.
        # *UpdateFailed will not: that AP is reachable and serving, but it is
        # not running the config or firmware you asked for, which is a fault
        # and not a stage. Reporting them under one "still provisioning" label
        # buried the half that needs action.
        if failed and moving:
            headline = (f"{len(failed)} AP(s) failed an update, "
                        f"{len(moving)} still settling")
            summary = (f"{len(failed)} AP(s) are reachable but rejected a firmware or "
                       f"configuration push, and {len(moving)} are mid-flight "
                       f"(initializing, applying, or rebooting).")
        elif failed:
            headline = f"{len(failed)} AP(s) failed a firmware or config update"
            summary = (f"{len(failed)} AP(s) are online and serving, but did not apply "
                       f"the firmware or configuration they were sent — they are not "
                       f"running what this venue is configured for.")
        else:
            headline = f"{len(moving)} AP(s) have not settled yet"
            summary = (f"{len(moving)} AP(s) are neither online nor offline — "
                       f"initializing, applying firmware or configuration, or "
                       f"rebooting. Re-run in a few minutes.")

        return _finding("aps-provisioned", "APs finished provisioning", "warning",
                        summary, headline=headline,
                        evidence=[{"ap": r["name"], "serial": r["serial"],
                                   "status": r["status"],
                                   "kind": ("failed update" if r.get("failedUpdate")
                                            else "in flight")}
                                  for r in limbo])
    return _finding("aps-provisioned", "APs finished provisioning", "ok",
                    "Every AP has reached a settled state.")


def check_switches_online(report: Dict[str, Any]) -> Dict[str, Any]:
    switches = report["inventory"]["switches"]
    rows = report["inventory"]["rows"]["switches"]
    if not switches["total"]:
        return _finding("switches-online", "Switches reachable", "skipped",
                        "No switches are assigned to this venue.")
    bad = [r for r in rows if r["state"] != "online"]
    if bad:
        return _finding("switches-online", "Switches reachable", "critical",
                        f"{len(bad)} of {switches['total']} switches are not online.",
                        headline=f"{len(bad)} switch(es) are not online",
                        evidence=[{"switch": r["name"], "serial": r["serial"], "model": r["model"],
                          "status": r["status"], "ip": r["ip"]} for r in bad])
    return _finding("switches-online", "Switches reachable", "ok",
                    f"All {switches['total']} switches are online.")


# ── firmware ─────────────────────────────────────────────────

def _firmware_check(check_id: str, title: str, tally: List[Dict[str, Any]],
                    noun: str) -> Dict[str, Any]:
    versions = [row for row in tally if row["label"] not in ("Unknown", "None", "")]
    if not versions:
        return _finding(check_id, title, "skipped", f"No {noun} firmware reported.")
    if len(versions) == 1:
        return _finding(check_id, title, "ok",
                        f"Every {noun} runs {versions[0]['label']}.")
    return _finding(check_id, title, "warning",
                    f"{len(versions)} firmware versions across the {noun} estate.",
                    [{"version": row["label"], "devices": row["count"]} for row in versions],
                    headline=f"Mixed {noun} firmware ({len(versions)} versions)")


def check_ap_firmware(report: Dict[str, Any]) -> Dict[str, Any]:
    return _firmware_check("ap-firmware", "AP firmware is consistent",
                           report["inventory"]["aps"]["byFirmware"], "AP")


def check_switch_firmware(report: Dict[str, Any]) -> Dict[str, Any]:
    return _firmware_check("switch-firmware", "Switch firmware is consistent",
                           report["inventory"]["switches"]["byFirmware"], "switch")


def check_switch_config_sync(report: Dict[str, Any]) -> Dict[str, Any]:
    rows = report["inventory"]["rows"]["switches"]
    if not rows:
        return _finding("switch-config-sync", "Switch config is in sync", "skipped",
                        "No switches are assigned to this venue.")
    drifted = [r for r in rows
               if r["configSynced"] is False
               or (r["warning"] and str(r["warning"]).lower() not in ("none", "false"))]
    if drifted:
        return _finding("switch-config-sync", "Switch config is in sync", "warning",
                        f"{len(drifted)} switch(es) report unsynced config or an "
                        "operational warning.",
                        headline=f"{len(drifted)} switch(es) report config drift",
                        evidence=[{"switch": r["name"], "synced": r["configSynced"],
                          "warning": r["warning"]} for r in drifted])
    return _finding("switch-config-sync", "Switch config is in sync", "ok",
                    "No switch reports config drift or an operational warning.")


# ── addressing ───────────────────────────────────────────────

def check_ap_addressing(report: Dict[str, Any]) -> Dict[str, Any]:
    addressing = report["addressing"]
    subnets = addressing["apSubnets"]
    missing = addressing["apsWithoutIp"]
    if not subnets and not missing:
        return _finding("ap-addressing", "APs hold an address", "skipped",
                        "No AP addresses were reported.")
    if missing:
        return _finding("ap-addressing", "APs hold an address", "warning",
                        f"{missing} online AP(s) report no IP address.",
                        headline=f"{missing} online AP(s) have no IP address")
    detail = ", ".join(f"{s['label']} ({s['count']})" for s in subnets[:6])
    if len(subnets) > 1:
        return _finding("ap-addressing", "APs hold an address", "info",
                        f"APs are spread across {len(subnets)} subnets.",
                        headline=f"APs span {len(subnets)} subnets",
                        evidence=[{"subnet": s["label"], "aps": s["count"]} for s in subnets],
                        detail=detail)
    return _finding("ap-addressing", "APs hold an address", "ok",
                    f"Every AP sits in {subnets[0]['label']}.")


def check_external_ip(report: Dict[str, Any]) -> Dict[str, Any]:
    external = report["addressing"]["external"]
    if not external:
        return _finding("external-ip", "Site egress identified", "skipped",
                        "No AP reported an external address.")
    if len(external) > 1:
        return _finding("external-ip", "Site egress identified", "info",
                        f"APs egress via {len(external)} public addresses — more than "
                        "one WAN path, or a NAT pool.",
                        headline=f"Site egresses via {len(external)} public addresses",
                        evidence=[{"ip": row["ip"], "aps": row["count"]} for row in external])
    only = external[0]
    return _finding("external-ip", "Site egress identified", "ok",
                    f"The whole site egresses via {only['ip']}.",
                    [{"ip": only["ip"], "aps": only["count"]}])


def check_dhcp_pools(report: Dict[str, Any]) -> Dict[str, Any]:
    pools = report["addressing"]["dhcpPools"]
    if not pools:
        return _finding("dhcp-pools", "DHCP pools have headroom", "skipped",
                        "This venue runs no R1-managed DHCP pool.")
    tight = [p for p in pools if (p["pct"] or 0) >= DHCP_WARN_PCT]
    if tight:
        return _finding("dhcp-pools", "DHCP pools have headroom", "warning",
                        f"{len(tight)} pool(s) are over {DHCP_WARN_PCT:.0f}% allocated.",
                        headline=f"{len(tight)} DHCP pool(s) are nearly full",
                        evidence=[{"pool": p["name"], "subnet": p["subnet"], "vlan": p["vlan"],
                          "used": p["used"], "total": p["total"], "pct": p["pct"]}
                         for p in tight])
    return _finding("dhcp-pools", "DHCP pools have headroom", "ok",
                    f"All {len(pools)} pool(s) are under {DHCP_WARN_PCT:.0f}% allocated.")


# ── VLANs ────────────────────────────────────────────────────

def check_ssid_vlans_carried(report: Dict[str, Any]) -> Dict[str, Any]:
    """An SSID whose VLAN no switch port carries is configured but landlocked."""
    vlans = report["vlans"]
    if not vlans["portsSeen"]:
        return _finding("ssid-vlan-carried", "SSID VLANs exist on the wire", "skipped",
                        "No switch ports were read, so VLAN reachability cannot be judged.")
    orphans = [row for row in vlans["rows"]
               if row["ssids"] and not row["untaggedPorts"] and not row["taggedPorts"]]
    if orphans:
        return _finding("ssid-vlan-carried", "SSID VLANs exist on the wire", "warning",
                        f"{len(orphans)} SSID VLAN(s) appear on no switch port in this venue.",
                        headline=f"{len(orphans)} SSID VLAN(s) reach no switch port",
                        evidence=[{"vlan": row["vlan"], "ssids": _join(row["ssids"])}
                         for row in orphans])
    carried = [row for row in vlans["rows"] if row["ssids"]]
    if not carried:
        return _finding("ssid-vlan-carried", "SSID VLANs exist on the wire", "skipped",
                        "No SSID declares a VLAN on this venue.")
    return _finding("ssid-vlan-carried", "SSID VLANs exist on the wire", "ok",
                    f"All {len(carried)} SSID VLAN(s) are carried by at least one port.")


def check_undeclared_vlans(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clients sitting on a VLAN that nothing in this venue's config declares.

    Not automatically a fault, and usually not one: dynamic per-identity VLAN
    assignment from DPSK or RADIUS puts clients on VLANs the venue config never
    mentions, which is the whole point of it. What matters is that the VLAN
    table stops presenting these as rows that appear from nowhere, and that the
    reader is told which reading applies.

    Only rated a warning when switch ports WERE read and none of them carry the
    VLAN — that combination means the traffic has nowhere to go northbound.
    """
    vlans = report["vlans"]
    rows = vlans["rows"]
    if not rows:
        return _finding("undeclared-vlans", "Client VLANs are accounted for", "skipped",
                        "No VLAN information was returned.")

    undeclared = [row for row in rows if row["origin"] == "undeclared" and row["clients"]]
    if not undeclared:
        return _finding("undeclared-vlans", "Client VLANs are accounted for", "ok",
                        "Every VLAN carrying clients is declared somewhere in this "
                        "venue's configuration.")

    evidence = [{"vlan": row["vlan"], "clients": row["clients"],
                 "untaggedPorts": row["untaggedPorts"], "taggedPorts": row["taggedPorts"],
                 "seenIn": _join(row["observedBy"])}
                for row in sorted(undeclared, key=lambda r: -r["clients"])]
    total = sum(row["clients"] for row in undeclared)

    if not vlans.get("portsKnown"):
        return _finding("undeclared-vlans", "Client VLANs are accounted for", "info",
                        f"{len(undeclared)} VLAN(s) carry {total} client(s) but are not "
                        f"declared by any SSID, DHCP pool or venue setting here. No "
                        f"switch ports were read, so this cannot be checked against the "
                        f"wire — on a DPSK or RADIUS site this is normally dynamic "
                        f"per-identity VLAN assignment.",
                        headline=f"{len(undeclared)} client VLAN(s) are not declared here",
                        evidence=evidence)

    return _finding("undeclared-vlans", "Client VLANs are accounted for", "warning",
                    f"{len(undeclared)} VLAN(s) carry {total} client(s), are declared by "
                    f"nothing in this venue, and appear on none of the "
                    f"{vlans['portsSeen']} switch ports read. Either they are assigned "
                    f"dynamically by DPSK/RADIUS and trunked from outside this venue, or "
                    f"that traffic has no path northbound.",
                    headline=f"{len(undeclared)} client VLAN(s) are on no port and "
                             f"declared nowhere",
                    evidence=evidence)


def check_management_vlan(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    The venue setting and what the APs actually run on have to agree, and the
    VLAN has to exist on the wire. Either half being wrong strands the APs on
    the next reboot.
    """
    vlans = report["vlans"]
    mgmt = vlans["managementVlan"]
    reported = vlans["apManagementVlans"]

    if len(reported) > 1:
        return _finding("mgmt-vlan", "AP management VLAN is consistent", "warning",
                        f"APs report {len(reported)} different management VLANs.",
                        headline=f"APs disagree on the management VLAN "
                                 f"({len(reported)} in use)",
                        evidence=[{"vlan": row["label"], "aps": row["count"]} for row in reported])

    if mgmt is None:
        if reported:
            return _finding("mgmt-vlan", "AP management VLAN is consistent", "ok",
                            f"No venue-level override; every AP manages on VLAN "
                            f"{reported[0]['label']}.")
        return _finding("mgmt-vlan", "AP management VLAN is consistent", "skipped",
                        "This venue leaves AP management traffic untagged.")

    if reported and str(reported[0]["label"]) != str(mgmt):
        return _finding("mgmt-vlan", "AP management VLAN is consistent", "warning",
                        f"The venue sets management VLAN {mgmt}, but the APs report "
                        f"VLAN {reported[0]['label']}.",
                        headline="AP management VLAN does not match the venue setting",
                        evidence=[{"vlan": row["label"], "aps": row["count"]} for row in reported])

    row = next((r for r in vlans["rows"] if r["vlan"] == int(mgmt)), None)
    if not vlans["portsSeen"]:
        return _finding("mgmt-vlan", "AP management VLAN is consistent", "ok",
                        f"Management VLAN is {mgmt}; no switch ports were read to "
                        "confirm it is carried.")
    if not row or (not row["untaggedPorts"] and not row["taggedPorts"]):
        return _finding("mgmt-vlan", "AP management VLAN is consistent", "warning",
                        f"AP management VLAN {mgmt} appears on no switch port in this venue.",
                        headline=f"AP management VLAN {mgmt} reaches no switch port")
    return _finding("mgmt-vlan", "AP management VLAN is consistent", "ok",
                    f"Management VLAN {mgmt} is on {row['untaggedPorts']} untagged and "
                    f"{row['taggedPorts']} tagged port(s).")


# ── PoE and cabling ──────────────────────────────────────────

def check_poe_budget(report: Dict[str, Any]) -> Dict[str, Any]:
    switches = [s for s in report["poe"]["switches"] if s["capacityWatts"]]
    if not switches:
        return _finding("poe-budget", "PoE budget has headroom", "skipped",
                        "No switch reported a PoE budget.")
    crit = [s for s in switches if (s["allocatedPct"] or 0) >= POE_CRIT_PCT]
    warn = [s for s in switches if POE_WARN_PCT <= (s["allocatedPct"] or 0) < POE_CRIT_PCT]
    if crit or warn:
        rows = [{"switch": s["name"], "model": s["model"],
                 "allocatedW": s["allocatedWatts"], "capacityW": s["capacityWatts"],
                 "pct": s["allocatedPct"], "poweredPorts": s["poweredPorts"]}
                for s in crit + warn]
        return _finding("poe-budget", "PoE budget has headroom",
                        "critical" if crit else "warning",
                        f"{len(crit) + len(warn)} switch(es) have allocated over "
                        f"{POE_WARN_PCT:.0f}% of their PoE budget.", rows,
                        headline=(f"{len(crit)} switch(es) are over "
                                  f"{POE_CRIT_PCT:.0f}% of their PoE budget"
                                  if crit else
                                  f"{len(warn)} switch(es) are near their PoE budget"))
    return _finding("poe-budget", "PoE budget has headroom", "ok",
                    f"Every switch is under {POE_WARN_PCT:.0f}% PoE allocation.")


def check_ap_uplink_speed(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    A gigabit AP on a 100 Mbps link is the classic bad-patch-lead install fault,
    and it never shows up as a fault anywhere else. The speed comes from the AP's
    own PoE port status ("Up 1000Mbps full"), so it works even when the switch
    the AP hangs off is not managed by this tenant.
    """
    # Only online APs: an offline AP's last-known link speed proves nothing.
    linked = [row for row in report["poe"]["apsOnPoe"]
              if row.get("speedMbps") and row.get("state") == "online"]
    if not linked:
        return _finding("ap-uplink-speed", "AP uplinks negotiated at full speed", "skipped",
                        "No online AP reported an uplink link speed.")
    slow = [row for row in linked if row["speedMbps"] < 1000]
    if slow:
        return _finding("ap-uplink-speed", "AP uplinks negotiated at full speed", "warning",
                        f"{len(slow)} AP uplink(s) negotiated below 1 Gbps.",
                        [{"ap": r["ap"], "switch": r["switch"], "port": r["port"],
                          "link": r["link"]} for r in slow],
                        headline=f"{len(slow)} AP uplink(s) negotiated below 1 Gbps")
    return _finding("ap-uplink-speed", "AP uplinks negotiated at full speed", "ok",
                    f"All {len(linked)} AP uplinks are at 1 Gbps or better.")


def check_port_errors(report: Dict[str, Any]) -> Dict[str, Any]:
    ports = report["ports"]
    if not ports["total"]:
        return _finding("port-errors", "Links are clean", "skipped",
                        "No switch ports were read.")
    if ports["erroredCount"]:
        return _finding("port-errors", "Links are clean", "warning",
                        f"{ports['erroredCount']} up port(s) are counting CRC or "
                        "interface errors — suspect cabling or optics.",
                        ports["errored"],
                        headline=f"{ports['erroredCount']} port(s) are counting errors")
    return _finding("port-errors", "Links are clean", "ok",
                    f"None of the {ports['up']} up ports are counting errors.")


def check_port_link_speed(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    A switch port that negotiated below a gigabit. On an infrastructure link
    (switch-to-switch) or an AP uplink it is the classic bad-patch-lead fault
    and a warning; on an access port to an endpoint it may just be a slow device,
    so it is reported as info to confirm rather than as a fault. `shape.port_card`
    does the classification through LLDP — this check only reads the split.

    Distinct from `ap-uplink-speed`, which reads the AP's own PoE port status:
    this is the switch-side view and catches a slow link even when the AP is
    offline or does not report its speed. Overlap on a live AP link is expected.
    """
    ports = report["ports"]
    if not ports["total"]:
        return _finding("port-link-speed", "Links negotiated at full speed", "skipped",
                        "No switch ports were read.")
    slow = ports.get("slowLinks") or []
    if not slow:
        return _finding("port-link-speed", "Links negotiated at full speed", "ok",
                        f"All {ports['up']} up ports negotiated at 1 Gbps or better.")

    infra = [row for row in slow if row["kind"] in ("ap", "switch")]
    # Warning when any infrastructure or AP link is slow; otherwise the slow
    # ports are all endpoints and this is an info-level "confirm these".
    if infra:
        return _finding("port-link-speed", "Links negotiated at full speed", "warning",
                        f"{len(infra)} infrastructure or AP link(s) negotiated below "
                        f"1 Gbps — suspect a patch lead"
                        + (f"; {len(slow) - len(infra)} endpoint port(s) are also below "
                           "a gigabit." if len(slow) > len(infra) else "."),
                        slow,
                        headline=f"{len(infra)} uplink(s) below 1 Gbps")
    return _finding("port-link-speed", "Links negotiated at full speed", "info",
                    f"{len(slow)} access port(s) negotiated below 1 Gbps — confirm the "
                    "attached devices are meant to run at that speed.",
                    slow,
                    headline=f"{len(slow)} access port(s) below 1 Gbps")


# ── wireless ─────────────────────────────────────────────────

def check_ssids_activated(report: Dict[str, Any]) -> Dict[str, Any]:
    wireless = report["wireless"]
    if not wireless["activated"]:
        return _finding("ssids-activated", "SSIDs are activated here", "critical",
                        "No Wi-Fi network is activated on this venue.",
                        headline="No SSID is activated on this venue")
    return _finding("ssids-activated", "SSIDs are activated here", "ok",
                    f"{wireless['activated']} SSID(s) activated on this venue.",
                    [{"ssid": row["ssid"], "security": row["security"],
                      "vlans": _join(row["vlans"]),
                      "radios": _join(row["radios"])}
                     for row in wireless["rows"]])


def check_ssids_carrying(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    The closest thing to proof available read-only: an SSID with clients on it
    is demonstrably working end to end. Silence is not proof of failure — an
    empty guest SSID at 3am is fine — so this never rates worse than a warning.
    """
    rows = report["wireless"]["rows"]
    if not rows:
        return _finding("ssids-carrying", "SSIDs are carrying clients", "skipped",
                        "No SSID is activated on this venue.")
    quiet = [row for row in rows if not row["clientsNow"]]
    if len(quiet) == len(rows):
        return _finding("ssids-carrying", "SSIDs are carrying clients", "warning",
                        "No SSID on this venue has a client on it right now — nothing "
                        "here has been proven to pass traffic.",
                        [{"ssid": row["ssid"], "security": row["security"]} for row in quiet],
                        headline="No SSID has a client on it")
    if quiet:
        return _finding("ssids-carrying", "SSIDs are carrying clients", "info",
                        f"{len(rows) - len(quiet)} of {len(rows)} SSIDs have clients on "
                        "them; the rest are quiet.",
                        headline=f"{len(quiet)} SSID(s) have no clients on them",
                        evidence=[{"ssid": row["ssid"], "clients": row["clientsNow"],
                          "aps": row["apsCarrying"]} for row in rows])
    return _finding("ssids-carrying", "SSIDs are carrying clients", "ok",
                    f"All {len(rows)} SSID(s) have live clients.",
                    [{"ssid": row["ssid"], "clients": row["clientsNow"],
                      "aps": row["apsCarrying"]} for row in rows])


def check_ssids_broadcasting(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Between "configured" and "carrying traffic" sits "on the air". Each online
    AP reports the SSIDs its radios are beaconing, so an activated SSID that no
    AP is broadcasting is a config that never reached the hardware.
    """
    rows = report["wireless"]["rows"]
    online = report["inventory"]["aps"]["online"]
    if not rows:
        return _finding("ssids-broadcasting", "SSIDs reached the air", "skipped",
                        "No SSID is activated on this venue.")
    if not online:
        return _finding("ssids-broadcasting", "SSIDs reached the air", "skipped",
                        "No AP is online to report what it is beaconing.")
    dark = [row for row in rows if not row["apsBroadcasting"]]
    if dark:
        # "No AP is beaconing this" has two very different causes, and the
        # evidence has to separate them: either the config never reached a
        # radio, or there was no online radio in scope for it to reach. Scopes
        # are mutually exclusive in wireless_card — a row carries either its
        # per-group scopes or the single venue-wide one — so these sum cleanly.
        detailed = []
        for row in dark:
            scopes = row["scopes"] or []
            in_scope = sum(scope.get("aps") or 0 for scope in scopes)
            online_in_scope = sum(scope.get("onlineAps") or 0 for scope in scopes)
            detailed.append({
                "ssid": row["ssid"],
                "scopes": _join(scope["group"] for scope in scopes),
                "apsInScope": in_scope,
                "onlineInScope": online_in_scope,
                "radios": _join(row["radios"]),
                "likelyCause": ("no AP in scope" if not in_scope
                                else "no online AP in scope" if not online_in_scope
                                else "reached no radio"),
            })

        unreachable = [row for row in detailed if not row["onlineInScope"]]
        stranded = [row for row in detailed if row["onlineInScope"]]

        if stranded and unreachable:
            summary = (f"{len(stranded)} activated SSID(s) have online APs in scope but "
                       f"are on no radio; a further {len(unreachable)} have no online AP "
                       f"in scope at all.")
            headline = f"{len(stranded)} activated SSID(s) never reached their APs"
        elif stranded:
            summary = (f"{len(stranded)} activated SSID(s) are not being beaconed by any "
                       f"online AP, despite having online APs in scope — the config did "
                       f"not reach the radios.")
            headline = f"{len(stranded)} activated SSID(s) never reached their APs"
        else:
            summary = (f"{len(unreachable)} activated SSID(s) are not on the air because "
                       f"no online AP is in scope for them. Fix AP reachability first — "
                       f"this says nothing about the SSID config itself.")
            headline = f"{len(unreachable)} activated SSID(s) have no online AP in scope"

        return _finding("ssids-broadcasting", "SSIDs reached the air", "warning",
                        summary, headline=headline, evidence=detailed)
    return _finding("ssids-broadcasting", "SSIDs reached the air", "ok",
                    f"All {len(rows)} activated SSID(s) are on the air.",
                    [{"ssid": row["ssid"], "apsBroadcasting": row["apsBroadcasting"]}
                     for row in rows])


def check_ap_group_ssid_limit(report: Dict[str, Any]) -> Dict[str, Any]:
    groups = report["wireless"]["perApGroup"]
    if not groups:
        return _finding("ap-group-ssid-limit", "AP groups are within the SSID limit",
                        "skipped", "No SSID activation scopes were reported.")
    over = [g for g in groups if g["count"] > AP_GROUP_SSID_LIMIT]
    near = [g for g in groups if AP_GROUP_SSID_LIMIT - 2 <= g["count"] <= AP_GROUP_SSID_LIMIT]
    if over:
        return _finding("ap-group-ssid-limit", "AP groups are within the SSID limit",
                        "critical",
                        f"{len(over)} AP group(s) exceed R1's {AP_GROUP_SSID_LIMIT}-SSID limit.",
                        headline=f"{len(over)} AP group(s) are over the "
                                 f"{AP_GROUP_SSID_LIMIT}-SSID limit",
                        evidence=[{"apGroup": g["label"], "ssids": g["count"]} for g in over])
    if near:
        return _finding("ap-group-ssid-limit", "AP groups are within the SSID limit",
                        "warning",
                        f"{len(near)} AP group(s) are within two SSIDs of R1's "
                        f"{AP_GROUP_SSID_LIMIT}-SSID limit.",
                        headline=f"{len(near)} AP group(s) are near the "
                                 f"{AP_GROUP_SSID_LIMIT}-SSID limit",
                        evidence=[{"apGroup": g["label"], "ssids": g["count"]} for g in near])
    return _finding("ap-group-ssid-limit", "AP groups are within the SSID limit", "ok",
                    f"The busiest AP group carries {max(g['count'] for g in groups)} SSIDs.")


def check_empty_ap_groups(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    SSID activations pointing at an AP group that holds no APs.

    Guarded three ways, because the naive version of this check turned one
    broken join into three hundred false findings on a per-unit-SSID site:

    * A venue with NO APs at all cannot have a populated group. That is already
      reported by the AP checks, and repeating it once per activation is noise.
    * If the venue HAS APs but not one activation scope resolved to a single
      AP, the AP-to-group join failed — an id that did not line up, a field the
      AP query omitted. Three hundred groups do not all empty themselves at
      once; the far likelier reading is that the mapping is missing, so the
      check declines rather than accuses.
    * Evidence is capped. A page of identical rows is not more convincing than
      a dozen, and the count in the summary carries the scale.
    """
    wireless = report["wireless"]
    rows = wireless["rows"]
    if not rows:
        return _finding("ssid-scope", "SSID scopes contain APs", "skipped",
                        "No SSID is activated on this venue.")

    aps = report["inventory"]["aps"]
    if aps.get("truncated"):
        return _finding("ssid-scope", "SSID scopes contain APs", "skipped",
                        f"RUCKUS ONE reports {aps.get('reportedTotal')} AP(s) in this "
                        f"venue but returned only {aps.get('total')}. Any group whose "
                        f"APs are among the missing would look empty, so this cannot be "
                        f"judged.",
                        headline="AP list is incomplete — group membership not judged")

    ap_total = aps["total"]
    if not ap_total:
        return _finding("ssid-scope", "SSID scopes contain APs", "skipped",
                        "No APs are assigned to this venue, so no AP group here "
                        "can hold one.")

    # Populated groups, indexed by name, so an empty group can be matched
    # against a similarly-named populated one. A venue left holding two sets of
    # per-unit groups — "1-1001" and "1-1001@The_ross" — produces a finding that
    # looks wrong until you notice the flagged group is not the one you are
    # looking at in the console.
    # Only NAMED populated groups. The default group's name is null, and an
    # empty string is a prefix of everything — matching against it made every
    # flagged group appear to have a look-alike, which is worse than none.
    populated_names = {g["name"]: g for g in wireless.get("groups") or []
                       if g.get("aps") and g.get("name")}

    def lookalike(name: str) -> str:
        if not name:
            return ""
        for other, group in populated_names.items():
            if other == name:
                continue
            longer, shorter = (other, name) if len(other) > len(name) else (name, other)
            # A real look-alike differs only by a suffix, not by being a
            # coincidental substring: "1-1001" vs "1-1001@The_ross".
            if longer.startswith(shorter) and len(shorter) >= 3:
                return f"{other} ({group['aps']} AP(s))"
        return ""

    scoped = [{"ssid": row["ssid"], "apGroup": scope["group"],
               "apGroupId": str(scope.get("groupId") or "")[:8] or "—",
               "similarPopulatedGroup": lookalike(scope["group"]) or "—"}
              for row in rows for scope in row["scopes"]
              if scope.get("aps") == 0 and scope["group"] != "All AP groups"]
    populated = sum(1 for row in rows for scope in row["scopes"]
                    if (scope.get("aps") or 0) > 0)

    if scoped and not populated:
        return _finding("ssid-scope", "SSID scopes contain APs", "skipped",
                        f"{ap_total} AP(s) are assigned to this venue but not one of "
                        f"the {len(scoped)} activation scope(s) resolved to any of "
                        f"them, so the AP-to-group mapping could not be read "
                        f"({wireless.get('apsWithGroup', 0)} of {ap_total} AP(s) "
                        f"reported a group). Reporting every scope as empty on that "
                        f"basis would be a guess, not a finding.",
                        headline="AP group membership could not be resolved",
                        detail="Checked by AP group id, then by group name.")

    if scoped:
        shown = scoped[:15]
        # Say where the APs actually ARE. On a per-unit site the usual cause is
        # that the groups were created and the SSIDs activated on them, but the
        # APs were never moved out of Default — which is a real fault with a
        # specific fix, and unrecognisable from "N groups are empty" alone.
        elsewhere = sorted(((g["name"] or "Default"), g["aps"])
                           for g in wireless.get("groups") or [] if g["aps"])
        where = _join(f"{name} ({count})" for name, count in elsewhere[:6])
        twins = sum(1 for row in scoped if row["similarPopulatedGroup"] != "—")
        note = ""
        if twins:
            note = (f" {twins} of them have a similarly-named group that DOES hold "
                    f"APs — this venue appears to carry two sets of per-unit AP "
                    f"groups, and the SSIDs are activated on the empty set. Check the "
                    f"group id, not the name.")
        ssid_count = len({row["ssid"] for row in scoped})
        group_count = len({row["apGroup"] for row in scoped})
        return _finding("ssid-scope", "SSID scopes contain APs", "warning",
                        f"{ssid_count} SSID(s) are activated on {group_count} AP group(s) "
                        f"that hold no APs, and cannot reach a radio there.{note} "
                        f"This venue's {ap_total} AP(s) sit in: {where}.",
                        headline=f"{ssid_count} SSID(s) activated on {group_count} empty "
                                 f"AP group(s)",
                        evidence=shown,
                        detail=(f"Showing {len(shown)} of {len(scoped)}."
                                if len(scoped) > len(shown) else None))

    return _finding("ssid-scope", "SSID scopes contain APs", "ok",
                    "Every SSID activation lands on a group that holds APs.")


# ── DPSK ─────────────────────────────────────────────────────

def check_dpsk_in_use(report: Dict[str, Any]) -> Dict[str, Any]:
    """Whether this venue is a DPSK site at all. Skipped, not failed, when it is not."""
    dpsk = report.get("dpsk") or {}
    if not dpsk.get("inUse"):
        return _finding("dpsk-in-use", "DPSK is configured", "skipped",
                        f"No DPSK pool backs any SSID activated on this venue "
                        f"({dpsk.get('poolsOnTenant', 0)} pool(s) exist on the tenant, "
                        f"none used here).")
    pools = dpsk["pools"]
    # A pool reached only through the property's identity group backs no SSID
    # here yet: configured, not deployed. Saying it "backs SSIDs on this venue"
    # would be false for exactly the venues where that distinction matters.
    deployed = [row for row in pools if row["networksHere"]]
    configured_only = [row for row in pools if not row["networksHere"]]
    parts = []
    if deployed:
        parts.append(f"{len(deployed)} pool(s) back "
                     f"{sum(r['networksHere'] for r in deployed)} SSID(s) activated here")
    if configured_only:
        parts.append(f"{len(configured_only)} pool(s) are configured for this property "
                     f"but back no SSID activated here yet")
    return _finding("dpsk-in-use", "DPSK is configured", "ok",
                    f"{'; '.join(parts)}. {dpsk['passphraseTotal']} passphrase(s) across "
                    f"{dpsk['identityTotal']} identity/identities.",
                    [{"pool": row["name"], "ssidsHere": row["networksHere"],
                      "linkedBy": _join(row.get("linkedBy")),
                      "passphrases": row["passphraseCount"],
                      "identityCount": row["identityCount"]}
                     for row in pools])


def check_dpsk_pool_has_passphrases(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    A DPSK pool with no passphrases cannot admit anyone.

    On a per-unit property this is the difference between an import that ran and
    one that only created the shell — the SSIDs exist, the pool exists, and not
    one resident can join.
    """
    dpsk = report.get("dpsk") or {}
    if not dpsk.get("inUse"):
        return _finding("dpsk-passphrases", "DPSK pools can admit clients", "skipped",
                        "This venue does not use DPSK.")
    unknown = [row for row in dpsk["pools"] if row["passphraseCount"] is None]
    empty = [row for row in dpsk["pools"] if row["passphraseCount"] == 0]
    if empty:
        return _finding("dpsk-passphrases", "DPSK pools can admit clients", "critical",
                        f"{len(empty)} DPSK pool(s) backing SSIDs here hold no "
                        f"passphrases at all — nothing can authenticate onto those "
                        f"SSIDs.",
                        headline=f"{len(empty)} DPSK pool(s) have no passphrases",
                        evidence=[{"pool": row["name"], "ssidsHere": row["networksHere"],
                                   "identityCount": row["identityCount"]} for row in empty])
    if unknown:
        return _finding("dpsk-passphrases", "DPSK pools can admit clients", "skipped",
                        f"Passphrase counts could not be read for {len(unknown)} pool(s).")
    return _finding("dpsk-passphrases", "DPSK pools can admit clients", "ok",
                    f"Every DPSK pool here holds passphrases "
                    f"({dpsk['passphraseTotal']} in total).")


def check_dpsk_identity_groups(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    A DPSK pool with no identity group behind it — an orphan.

    RUCKUS ONE will not let you CREATE a pool without at least one identity
    group. It will, apparently, let you delete the group afterwards and leave
    the pool behind: a live tenant has four pools and a provably complete list
    of three groups, with the fourth pool matching none of them.

    The pool's own `isReferenced` flag is no help — it is documented as
    "referenced by an identity group and cannot be deleted" and reads `true` on
    that orphan, which is precisely the guard that should have blocked the
    deletion. It is stale, so this check does not reason from it.

    Everything turns on the group list being provably complete, which is why
    fetch.identity_groups_all walks GET /identityGroups rather than the query
    form that silently ignores paging. When the walk is short, the check
    declines rather than accuse a healthy pool.
    """
    dpsk = report.get("dpsk") or {}
    if not dpsk.get("inUse"):
        return _finding("dpsk-identity-groups", "DPSK pools have an identity group",
                        "skipped", "This venue does not use DPSK.")

    pools = dpsk.get("pools") or []
    if not dpsk.get("identityGroupsComplete", True):
        return _finding("dpsk-identity-groups", "DPSK pools have an identity group",
                        "skipped",
                        f"Only {dpsk.get('identityGroupsOnTenant')} of "
                        f"{dpsk.get('identityGroupsTotal')} identity groups could be "
                        f"read, so a pool cannot be shown to be missing one without "
                        f"risking a false accusation.")

    orphans = [row for row in pools if not row.get("identityGroupsResolved")]
    if orphans:
        return _finding("dpsk-identity-groups", "DPSK pools have an identity group",
                        "warning",
                        f"{len(orphans)} DPSK pool(s) used here have no identity group, "
                        f"against a complete list of "
                        f"{dpsk.get('identityGroupsTotal')} group(s) on the tenant. A "
                        f"pool cannot be created this way, so the group was deleted out "
                        f"from under it — the pool still reports isReferenced=true, "
                        f"which is the stale guard that should have prevented that. "
                        f"Passphrases in an orphaned pool cannot be administered through "
                        f"its group.",
                        headline=f"{len(orphans)} DPSK pool(s) are orphaned — identity "
                                 f"group deleted",
                        evidence=[{"pool": row["name"], "passphrases": row["passphraseCount"],
                                   "ssidsHere": row["networksHere"],
                                   "poolClaimsReferenced": row.get("isReferenced")}
                                  for row in orphans])

    return _finding("dpsk-identity-groups", "DPSK pools have an identity group", "ok",
                    f"Every DPSK pool used here has its identity group — "
                    f"{dpsk['identityTotal']} identity/identities in total.",
                    [{"pool": row["name"], "identityCount": row["identityCount"],
                      "passphrases": row["passphraseCount"]} for row in pools])


# ── adaptive policy ──────────────────────────────────────────

def check_policy_chain_resolves(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Every policy in a scoped set resolves to a policy, and every policy to its
    RADIUS attribute group.

    A set lists member policy ids; a policy names its rate tier in
    `onMatchResponse`. Either link can dangle after a delete, and a dangling
    one is silent: the set still shows a policy count and the client still
    matches nothing.
    """
    policy = report.get("policy") or {}
    if not policy.get("inUse"):
        return _finding("policy-chain", "Adaptive policy chain resolves", "skipped",
                        "No adaptive policy set is attached to this venue's DPSK.")

    dangling_policies = policy.get("unresolvedPolicies") or 0
    missing_radius = [
        {"set": row["name"], "policy": member["policy"]}
        for row in policy["sets"] for member in row["policies"]
        if member.get("radiusGroupMissing")
    ]
    if dangling_policies or missing_radius:
        parts = []
        if dangling_policies:
            parts.append(f"{dangling_policies} policy id(s) in a set no longer exist")
        if missing_radius:
            parts.append(f"{len(missing_radius)} policy/policies point at a RADIUS "
                         f"attribute group that is gone")
        return _finding("policy-chain", "Adaptive policy chain resolves", "warning",
                        " and ".join(parts) + ". Traffic matching those rules gets no "
                        "rate tier applied.",
                        headline=f"{dangling_policies + len(missing_radius)} broken link(s) "
                                 f"in the adaptive policy chain",
                        evidence=missing_radius or [{"unresolvedPolicyIds": _join(row["unresolvedPolicyIds"])}
                                                    for row in policy["sets"]
                                                    if row["unresolvedPolicyIds"]])

    total = sum(len(row["policies"]) for row in policy["sets"])
    return _finding("policy-chain", "Adaptive policy chain resolves", "ok",
                    f"All {total} policy/policies across {policy['setCount']} set(s) "
                    f"resolve to a RADIUS attribute group.")


def check_radius_group_orphans(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Assignment rows on a RADIUS attribute group that name a policy that no
    longer exists.

    R1 does not clean these up when a policy is deleted. The group is then
    pinned: the UI shows it with no policies while a delete returns 409, and
    nothing in the interface can clear it. Worth naming explicitly, because
    from the console it looks like a bug with no cause.
    """
    policy = report.get("policy") or {}
    if not policy.get("inUse"):
        return _finding("radius-group-orphans", "RADIUS attribute groups are cleanly "
                        "referenced", "skipped",
                        "No adaptive policy set is attached to this venue's DPSK.")

    pinned = [row for row in policy.get("radiusGroups") or [] if row["orphanedAssignments"]]
    if pinned:
        return _finding("radius-group-orphans", "RADIUS attribute groups are cleanly "
                        "referenced", "warning",
                        f"{len(pinned)} RADIUS attribute group(s) carry assignment rows "
                        f"pointing at policies that no longer exist. R1 leaves these "
                        f"behind when a policy is deleted, which pins the group: it will "
                        f"refuse to delete with a 409 while showing no policies.",
                        headline=f"{len(pinned)} RADIUS attribute group(s) are pinned by "
                                 f"stale assignments",
                        evidence=[{"radiusGroup": row["name"],
                                   "livePolicies": row["policyCount"],
                                   "orphanedAssignments": row["orphanedAssignments"]}
                                  for row in pinned])
    return _finding("radius-group-orphans", "RADIUS attribute groups are cleanly "
                    "referenced", "ok",
                    f"No stale assignment rows on the "
                    f"{len(policy.get('radiusGroups') or [])} RADIUS attribute group(s) "
                    f"used here.")


def check_24ghz_channel_plan(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    2.4 GHz must be restricted to 1, 6 and 11.

    There are only three non-overlapping 20 MHz channels in the 2.4 GHz band
    under an 11-channel plan. Enabling any of the others does not add capacity,
    it subtracts it: a radio on channel 3 overlaps both 1 and 6, so its energy
    raises the noise floor on two clean channels at once and none of the three
    can defer to it properly. Auto-channel algorithms will use whatever they
    are permitted, so leaving the extra channels enabled is enough to cause it
    — nothing has to be manually misconfigured.

    Rated critical on what is ENABLED rather than on what radios happen to be
    using today, because the enabled set is what the next channel selection
    will draw from.
    """
    plan = (report.get("radios") or {}).get("plan") or []
    band = next((b for b in plan if str(b.get("band") or "").startswith("2.4")), None)
    if not band:
        return _finding("24ghz-channel-plan", "2.4 GHz is restricted to 1/6/11",
                        "skipped", "No 2.4 GHz radio configuration was returned.")

    enabled = band.get("enabledOutsidePlan") or []
    in_use = set(band.get("radiosOutsidePlan") or [])
    if not enabled:
        return _finding("24ghz-channel-plan", "2.4 GHz is restricted to 1/6/11", "ok",
                        f"Only 1/6/11 are enabled on 2.4 GHz "
                        f"({band.get('allowedCount', 0)} channel(s) permitted).")

    # Name the APs sitting on an out-of-plan channel; that is the half of this
    # finding somebody has to act on first.
    aps_by_channel: Dict[int, List[str]] = {}
    for row in (report.get("inventory") or {}).get("rows", {}).get("aps") or []:
        for radio in row.get("radios") or []:
            if not str(radio.get("band") or "").startswith("2"):
                continue
            channel = radio.get("channel")
            if channel in in_use:
                aps_by_channel.setdefault(channel, []).append(row.get("name") or "?")

    evidence = [{"channel": channel,
                 "radiosOnIt": len(aps_by_channel.get(channel, [])),
                 "aps": _join(aps_by_channel.get(channel, []), empty="none yet")}
                for channel in enabled]

    summary = (f"{len(enabled)} channel(s) outside 1/6/11 are enabled on 2.4 GHz: "
               f"{_join(enabled)}. Any radio that selects one overlaps two of the "
               f"three clean channels at once, which costs capacity across the whole "
               f"band rather than adding any.")
    if in_use:
        summary += (f" {len(in_use)} of them {'is' if len(in_use) == 1 else 'are'} "
                    f"already in use.")

    return _finding("24ghz-channel-plan", "2.4 GHz is restricted to 1/6/11", "critical",
                    summary,
                    headline=(f"{len(enabled)} non-1/6/11 channel(s) enabled on 2.4 GHz"
                              + (f", {len(in_use)} in use" if in_use else "")),
                    evidence=evidence)


def check_ap_mesh_fallback(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    APs carrying traffic over the air instead of over their cable.

    An install defect that passes every other check in this file. A mesh
    fallback AP is online, provisioned, broadcasting and serving clients — the
    whole report says green — while its drop is dead, its PoE port is dead, or
    it was never patched at all. Nobody notices until someone measures
    throughput, by which time the crew has gone.

    Only APs the shaper positively identified as meshing are counted; an
    unfamiliar meshRole is left alone. See shape._is_meshing.
    """
    rows = report["inventory"]["rows"]["aps"]
    if not rows:
        return _finding("ap-mesh-fallback", "APs are wired, not meshing", "skipped",
                        "No APs are assigned to this venue.")
    meshing = [r for r in rows if r.get("meshing")]
    if meshing:
        return _finding(
            "ap-mesh-fallback", "APs are wired, not meshing", "warning",
            f"{len(meshing)} of {len(rows)} APs are carrying traffic over mesh "
            "rather than their own uplink. Each one is a dead drop, a dead PoE "
            "port or a patch that was never made.",
            headline=f"{len(meshing)} AP(s) fell back to mesh",
            evidence=[{"ap": r["name"], "serial": r["serial"],
                       "role": r.get("meshRole"),
                       "switch": r.get("uplinkSwitch"),
                       "port": r.get("uplinkPort")} for r in meshing])
    return _finding("ap-mesh-fallback", "APs are wired, not meshing", "ok",
                    f"All {len(rows)} APs are on their own uplink.")


# An AP up for less than this, on a venue whose APs are otherwise settled, has
# restarted recently. Loose on purpose — the point is to separate one outlier
# from a fleet, not to measure anything.
RECENT_RESTART_SECONDS = 3600
SETTLED_FLEET_SECONDS = 86400


def check_ap_uptime(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    An AP that has restarted while the rest of the fleet has not.

    RELATIVE, not absolute, and that is the whole design. During an install
    every AP has just booted, so a plain "uptime under an hour" check would
    fire on every AP on the day it is most likely to be run — noise exactly
    when the report needs to be trusted. Comparing each AP against the venue's
    median instead means the check stays silent through commissioning and only
    speaks once there is a settled fleet for an outlier to stand out from.

    That also makes it a power check without any power data: an AP that reboots
    while its neighbours do not is usually a marginal PoE budget, a failing
    injector, or a cable moving in a duct.

    No history is needed, and none is available — PISR stores nothing. One
    poll, one comparison.
    """
    rows = [r for r in report["inventory"]["rows"]["aps"]
            if r["state"] == "online" and r.get("uptimeSeconds")]
    if len(rows) < 3:
        return _finding("ap-uptime", "No AP has restarted on its own", "skipped",
                        "Too few online APs report an uptime to compare them.")

    uptimes = sorted(r["uptimeSeconds"] for r in rows)
    median = uptimes[len(uptimes) // 2]
    if median < SETTLED_FLEET_SECONDS:
        # The whole venue is freshly booted — commissioning, or a site-wide
        # power event. Either way there is no outlier to find.
        return _finding("ap-uptime", "No AP has restarted on its own", "skipped",
                        "Every AP here was started recently, so there is no "
                        "settled fleet to compare against yet.")

    recent = [r for r in rows if r["uptimeSeconds"] < RECENT_RESTART_SECONDS]
    if recent:
        return _finding(
            "ap-uptime", "No AP has restarted on its own", "warning",
            f"{len(recent)} AP(s) restarted within the last hour while the rest "
            f"of this venue has been up for {_duration(median)}. That is usually "
            "power rather than the AP.",
            headline=f"{len(recent)} AP(s) restarted while the fleet did not",
            evidence=[{"ap": r["name"], "serial": r["serial"],
                       "uptime": r.get("uptimeText") or _duration(r["uptimeSeconds"]),
                       "switch": r.get("uplinkSwitch"),
                       "port": r.get("uplinkPort")} for r in recent])
    return _finding("ap-uptime", "No AP has restarted on its own", "ok",
                    f"Every online AP has been up at least "
                    f"{_duration(min(uptimes))}.")


def check_floorplans(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    A floor plan exists, and somebody set a scale on it.

    `check_ap_placement` asks whether APs were dragged onto a plan. This asks
    whether the plan they were dragged onto means anything.

    AN UNSCALED PLAN IS THE QUIET FAILURE. It looks finished — it has an image,
    APs sit on it, placement passes — but every distance derived from it is
    meaningless, so coverage estimates and AP spacing are nonsense and nobody
    finds out until someone surveys the site. Setting the scale is one drag and
    a measurement, and it is skipped precisely because nothing complains.
    """
    plans = (report.get("venue") or {}).get("floorPlans") or []
    if not plans:
        return _finding("floorplans", "Floor plans exist and are scaled", "warning",
                        "This venue has no floor plan, so no AP can be placed on "
                        "one and nothing can be said about coverage or spacing.",
                        headline="No floor plan on this venue")

    unscaled = [p for p in plans if not p.get("scaleSet")]
    if unscaled:
        return _finding(
            "floorplans", "Floor plans exist and are scaled", "warning",
            f"{len(unscaled)} of {len(plans)} floor plan(s) have no scale set. "
            "APs can still be placed on them, and every distance derived from "
            "that placement is meaningless until a scale is drawn.",
            headline=f"{len(unscaled)} floor plan(s) have no scale",
            evidence=[{"floorPlan": p["name"], "floor": p.get("floorNumber"),
                       "image": p.get("imageName"), "scaleSet": False}
                      for p in unscaled])

    missing_image = [p for p in plans if not p.get("hasImage")]
    if missing_image:
        return _finding(
            "floorplans", "Floor plans exist and are scaled", "info",
            f"{len(missing_image)} floor plan(s) are scaled but carry no image.",
            headline=f"{len(missing_image)} floor plan(s) have no image",
            evidence=[{"floorPlan": p["name"], "floor": p.get("floorNumber")}
                      for p in missing_image])

    return _finding("floorplans", "Floor plans exist and are scaled", "ok",
                    f"All {len(plans)} floor plan(s) have an image and a scale "
                    f"({_join(p['scaleDistance'] for p in plans)}).")


def check_ap_placement(report: Dict[str, Any]) -> Dict[str, Any]:
    rows = report["inventory"]["rows"]["aps"]
    if not rows:
        return _finding("ap-placement", "APs are placed on a floor plan", "skipped",
                        "No APs are assigned to this venue.")
    unplaced = [r for r in rows if not r["placed"]]
    if unplaced:
        return _finding("ap-placement", "APs are placed on a floor plan", "info",
                        f"{len(unplaced)} of {len(rows)} APs are not positioned on a "
                        "floor plan.",
                        headline=f"{len(unplaced)} AP(s) are not on a floor plan",
                        evidence=[{"ap": r["name"], "serial": r["serial"]} for r in unplaced])
    return _finding("ap-placement", "APs are placed on a floor plan", "ok",
                    f"All {len(rows)} APs are positioned on a floor plan.")


def check_ap_naming(report: Dict[str, Any]) -> Dict[str, Any]:
    """Duplicate or serial-number AP names mean the install was never labelled."""
    rows = report["inventory"]["rows"]["aps"]
    if not rows:
        return _finding("ap-naming", "APs are named", "skipped",
                        "No APs are assigned to this venue.")
    unnamed = [r for r in rows if not r["name"] or r["name"] == r["serial"]]
    duplicates = [name for name, count in Counter(r["name"] for r in rows).items()
                  if name and count > 1]
    if unnamed or duplicates:
        evidence = [{"ap": r["name"] or "(unnamed)", "serial": r["serial"],
                     "issue": "not named"} for r in unnamed]
        evidence += [{"ap": name, "serial": "—", "issue": "duplicate name"}
                     for name in duplicates]
        if unnamed and duplicates:
            headline = (f"{len(unnamed)} AP(s) unnamed, "
                        f"{len(duplicates)} name(s) duplicated")
        elif unnamed:
            headline = f"{len(unnamed)} AP(s) have no name of their own"
        else:
            headline = f"{len(duplicates)} AP name(s) are duplicated"
        return _finding("ap-naming", "APs are named", "warning",
                        f"{len(unnamed)} AP(s) carry no name of their own"
                        + (f" and {len(duplicates)} name(s) are duplicated" if duplicates else "")
                        + ".", evidence, headline=headline)
    return _finding("ap-naming", "APs are named", "ok",
                    "Every AP has a distinct name.")


def check_clients_present(report: Dict[str, Any]) -> Dict[str, Any]:
    clients = report["clients"]["total"]
    online_aps = report["inventory"]["aps"]["online"]
    if not online_aps:
        return _finding("clients-present", "The venue is carrying clients", "skipped",
                        "No AP is online to carry clients.")
    if not clients:
        return _finding("clients-present", "The venue is carrying clients", "warning",
                        f"{online_aps} AP(s) are online but no client is associated.",
                        headline="No client is associated to any online AP")
    return _finding("clients-present", "The venue is carrying clients", "ok",
                    f"{clients} client(s) associated across {online_aps} online AP(s).")


CHECKS: List[Callable[[Dict[str, Any]], Dict[str, Any]]] = [
    check_aps_online,
    check_switches_online,
    check_aps_provisioned,
    check_ssids_activated,
    check_ssids_carrying,
    check_ssids_broadcasting,
    check_ssid_vlans_carried,
    check_undeclared_vlans,
    check_ap_group_ssid_limit,
    check_empty_ap_groups,
    check_poe_budget,
    check_ap_uplink_speed,
    check_port_errors,
    check_port_link_speed,
    check_ap_firmware,
    check_switch_firmware,
    check_switch_config_sync,
    check_management_vlan,
    check_ap_addressing,
    check_external_ip,
    check_dhcp_pools,
    check_dpsk_in_use,
    check_policy_chain_resolves,
    check_radius_group_orphans,
    check_dpsk_pool_has_passphrases,
    check_dpsk_identity_groups,
    check_24ghz_channel_plan,
    check_ap_placement,
    check_floorplans,
    check_ap_naming,
    check_ap_mesh_fallback,
    check_ap_uptime,
    check_clients_present,
]

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "skipped": 4}


def _check_label(check) -> str:
    """
    "check_ssids_broadcasting" -> "Ssids broadcasting".

    A check that crashed never returned its own title, so the only name we have
    is the function's. Reporting it beats the bare "Check failed to run" this
    used to render, which told you a check broke but not which one.
    """
    name = getattr(check, "__name__", "") or "unknown check"
    words = name.replace("check_", "", 1).replace("_", " ").strip()
    return words.capitalize() if words else name


def run_checks(report: Dict[str, Any]) -> Dict[str, Any]:
    """Run every check against an assembled report. A broken check is a finding, not a 500."""
    findings = []
    for check in CHECKS:
        try:
            findings.append(check(report))
        except Exception as exc:  # a bad row must not cost the whole report
            name = getattr(check, "__name__", "unknown")
            logger.exception("pisr: check %s failed", name)
            findings.append(_finding(
                name,
                f"{_check_label(check)} — check failed to run",
                "skipped",
                f"This check errored and was skipped; every other check still ran. "
                f"{type(exc).__name__}: {exc}",
                detail=f"Check function: {name}()",
            ))
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["title"]))
    tally = Counter(f["severity"] for f in findings)
    return {
        "findings": findings,
        "counts": {level: tally.get(level, 0)
                   for level in ("critical", "warning", "info", "ok", "skipped")},
        "score": {
            "passed": tally.get("ok", 0),
            "ran": len(findings) - tally.get("skipped", 0),
        },
    }

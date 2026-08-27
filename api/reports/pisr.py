"""
PISR report context — an assembled report in, a PDF template context out.

Pure functions, no I/O and no RUCKUS ONE calls. The report handed in is exactly
what the UI renders, so the PDF and the screen cannot disagree about what was
found; anything this module adds is presentation.

Wide tables are SPLIT here rather than in the template. A per-device row has
sixteen columns and no page is wide enough for that, so each device table is
cut into two narrower ones that repeat the device name as the join key — the
reader has to be able to line the halves back up. The device pages are also
marked for landscape (see the template's named @page rules); splitting and
rotating together is what makes them legible rather than either alone.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Mirrors evidenceHeader() in the PISR page: evidence keys are dict keys, and
# naive title-casing turns "ssid" into "Ssid", which is worse than the raw key.
HEADER_WORDS = {
    "ssid": "SSID", "ssids": "SSIDs", "ip": "IP", "ap": "AP", "aps": "APs",
    "vlan": "VLAN", "vlans": "VLANs", "dns": "DNS", "dhcp": "DHCP",
    "poe": "PoE", "snr": "SNR", "rssi": "RSSI", "mac": "MAC", "id": "ID",
    "os": "OS", "w": "W", "pct": "%",
}


def evidence_header(key: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(key)).split(" ")
    out = []
    for index, word in enumerate(words):
        known = HEADER_WORDS.get(word.lower())
        if known:
            out.append(known)
        elif index == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            out.append(word.lower())
    return " ".join(out)




def _fmt_time(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def _dash(value: Any, suffix: str = "", cap: Optional[int] = None) -> str:
    """
    Empty cells read as an em dash, not as a blank that looks like a bug.

    `cap` truncates a long list. On a per-unit-SSID property one VLAN can carry
    fifty-odd SSIDs, and printing every one turns a table row into most of a
    page while telling the reader nothing the count does not.
    """
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        items = [str(v) for v in value if v is not None]
        if not items:
            return "—"
        if cap and len(items) > cap:
            return f"{', '.join(items[:cap])} … +{len(items) - cap} more"
        return ", ".join(items)
    return f"{value}{suffix}"


def _table(columns: List[str], rows: List[List[Any]]) -> Dict[str, Any]:
    """A table the template can render without knowing anything about it."""
    return {"columns": columns, "rows": rows, "count": len(rows)}



# ── chart data ───────────────────────────────────────────────

# Same status colours the screen uses, so a printed report and the tab it came
# from are recognisably the same document.
DONUT_COLOURS = {"online": "#16a34a", "offline": "#dc2626", "other": "#f59e0b"}
BAR_COLOUR = "#3b82f6"


def _donut(segments: List[Dict[str, Any]], centre: Any, centre_label: str) -> Dict[str, Any]:
    """
    Pre-compute a donut as stroke-dash offsets.

    WeasyPrint renders inline SVG, so the chart is drawn as arcs on a circle of
    circumference 100 — each segment's dash length IS its percentage, which
    keeps the arithmetic here and the template free of it.
    """
    total = sum(max(0, int(s.get("value") or 0)) for s in segments)
    arcs, offset = [], 25.0  # 25 puts the first arc at 12 o'clock
    for seg in segments:
        value = max(0, int(seg.get("value") or 0))
        if not total or not value:
            continue
        length = value / total * 100.0
        arcs.append({"label": seg["label"], "value": value,
                     "colour": seg.get("colour", BAR_COLOUR),
                     "dash": f"{length:.2f} {100 - length:.2f}",
                     "offset": f"{offset:.2f}"})
        offset = (offset - length) % 100
    return {"arcs": arcs, "total": total, "centre": centre,
            "centreLabel": centre_label,
            "legend": [{"label": s["label"], "value": int(s.get("value") or 0),
                        "colour": s.get("colour", BAR_COLOUR)}
                       for s in segments if (s.get("value") or 0) > 0]}


def _bars(rows: List[Dict[str, Any]], limit: int = 8) -> List[Dict[str, Any]]:
    """A bar list: label, count, and a width percentage relative to the top row."""
    rows = [r for r in (rows or []) if (r.get("count") or 0) > 0][:limit]
    if not rows:
        return []
    top = max(r["count"] for r in rows) or 1
    return [{"label": r.get("label") or "—", "count": r["count"],
             "pct": round(r["count"] / top * 100.0, 1)} for r in rows]


def _status_donut(group: Dict[str, Any], noun: str) -> Dict[str, Any]:
    return _donut(
        [{"label": "Online", "value": group.get("online"), "colour": DONUT_COLOURS["online"]},
         {"label": "Offline", "value": group.get("offline"), "colour": DONUT_COLOURS["offline"]},
         {"label": "Other", "value": group.get("other"), "colour": DONUT_COLOURS["other"]}],
        group.get("total") or 0, noun)


def _spectrum(band: Dict[str, Any]) -> Dict[str, Any]:
    """
    A band's channel-allocation chart, as final SVG geometry.

    One row per bonding width with the channel numbers along the top and a
    frequency axis underneath — the layout every Wi-Fi channel chart uses.
    Blocks are trapezoids because that is the convention (they read as channel
    masks), not decoration.

    Drawn in a 1000-unit-wide box scaled UNIFORMLY. Stretching a 0-100 box to
    fit smears the axis type horizontally along with the bars.
    """
    low, high = band.get("minMhz") or 0, band.get("maxMhz") or 0
    span = max(1.0, high - low)
    W, GUTTER, REGION, ROW_H, AXIS = 1000.0, 118.0, 15.0, 27.0, 22.0
    plot = W - GUTTER

    # Horizontal channel numbers need ~22 units each. Where the slots are
    # closer than that — 59 channels on a 6 GHz plan — the labels are turned
    # on their side instead of thinned away, so every channel is still named.
    slots_all = band.get("slots") or []
    gaps = [abs(b["centreMhz"] - a["centreMhz"]) / max(1.0, (band.get("maxMhz") or 0)
            - (band.get("minMhz") or 0)) * plot
            for a, b in zip(slots_all, slots_all[1:])]
    tightest = min(gaps) if gaps else 999.0
    vertical = tightest < 22.0
    # Rotated three-digit text needs vertical room the horizontal row does not.
    TOP = 62.0 if vertical else 38.0

    def x(mhz: float) -> float:
        return GUTTER + (mhz - low) / span * plot

    rows_in = band.get("rows") or []
    slots = band.get("slots") or []
    height = TOP + len(rows_in) * ROW_H + AXIS

    # 2.4 GHz slots are 20 MHz wide but only 5 MHz apart, so each one covers
    # three-quarters of its neighbour. Drawn opaque and in order, every shape
    # hid the previous one's slope and the row came out as a strip of leaning
    # parallelograms. Where slots overlap, they are drawn translucent with a
    # toned outline instead — which is how a real 2.4 GHz chart shows that the
    # channels genuinely sit on top of each other — and the ones in use are
    # drawn last, opaque, so they still read clearly.
    OUTLINE = {"#bbf7d0": "#4ade80", "#e8eaed": "#c3c9d0",
               "#2a78d6": "#1d4ed8", "#f59e0b": "#b45309"}

    rows = []
    for index, row in enumerate(rows_in):
        top = TOP + index * ROW_H
        bottom = top + ROW_H - 6
        row_width = row["width"]
        # Do these blocks overlap each other? 1/6/11 sit 25 MHz apart and do
        # not; 2/3/4/5 sit 5 MHz apart and do. Judged per row, not per band.
        centres = sorted((b["loMhz"] + b["hiMhz"]) / 2 for b in row["blocks"])
        gaps = [b - a for a, b in zip(centres, centres[1:])]
        overlapping = bool(gaps) and min(gaps) < (row_width - 0.1)
        shapes = []
        for block in row["blocks"]:
            x0, x1 = x(block["loMhz"]), x(block["hiMhz"])
            # Capped generously rather than at 7: on 2.4 GHz a 20 MHz slot is
            # a fifth of the whole plot, and a 7-unit inset on a 200-unit shape
            # is invisible — thirteen overlapping slots merged into one long
            # slab with a slanted end instead of reading as channel masks.
            inset = min(22.0, max(1.0, (x1 - x0) * 0.18))
            # Three states, deliberately distinct:
            #   blue        permitted and a radio is on it
            #   light green permitted, nothing using it — spare capacity
            #   light grey  not permitted here, i.e. switched off
            #   amber       a radio IS on it and the venue does not permit it
            if block["offPlan"]:
                colour = "#f59e0b"
            elif block["inUse"]:
                colour = "#2a78d6"
            elif block["allowed"]:
                colour = "#bbf7d0"
            else:
                colour = "#e8eaed"
            # Name every BONDED block by its centre channel, not just the ones
            # in use. On a 40/80/160 row the centre channel is the only thing
            # identifying the block, and an unlabelled green box says "some
            # bond is possible here" without saying which. The 20 MHz row is
            # left bare — those channels are already named along the top.
            text = str(block["label"]) if row_width > 20 else ""
            if text and (x1 - x0) < len(text) * 8 + 5:
                text = ""
            shapes.append({
                "points": (f"{x0:.1f},{bottom:.1f} {x0 + inset:.1f},{top:.1f} "
                           f"{x1 - inset:.1f},{top:.1f} {x1:.1f},{bottom:.1f}"),
                "colour": colour,
                "stroke": OUTLINE.get(colour, "#ffffff") if overlapping else "#ffffff",
                "opacity": (1.0 if not overlapping or block["inUse"] or block["offPlan"]
                            else 0.45),
                "inUse": block["inUse"],
                "label": text,
                # White reads on the filled states; the pale ones need ink.
                "labelColour": ("#ffffff" if block["inUse"] or block["offPlan"]
                                else "#15803d" if block["allowed"] else "#9aa1a9"),
                "labelWeight": "700" if block["inUse"] or block["offPlan"] else "600",
                "labelX": round((x0 + x1) / 2, 1),
                "labelY": round(top + (bottom - top) / 2 + 4, 1),
                "count": block["count"],
                # Paint order, least significant first. Sorting only in-use
                # last left every state that differs by colour alone to paint
                # in channel order, which on an overlapping row buries
                # information: a not-permitted slot drawn later lays its
                # 45%-opacity grey across a permitted-but-unused neighbour
                # drawn earlier, and the green reads as grey. 2.4 GHz channel
                # 10 under channel 12 is the case that shows it.
                "rank": (3 if block["offPlan"] else 2 if block["inUse"]
                         else 1 if block["allowed"] else 0),
            })
        # Ranked by how much each state has to say — grey says least, an
        # in-use channel outside the plan says most — so the meaningful
        # colour ends up on top wherever two slots share spectrum.
        shapes.sort(key=lambda sh: sh["rank"])
        rows.append({
            "label": row.get("label") or f"{row['width']} MHz",
            "labelX": GUTTER - 6, "labelY": round(top + (ROW_H - 6) / 2 + 4, 1),
            "muted": row["radios"] == 0,
            "shapes": shapes,
        })

    # Channel numbers along the top, thinned by SPACING rather than by a count
    # or an in-use test. Labelling only in-use channels looked right and was
    # not: a 160 MHz block marks all eight of its channels in use, so a 6 GHz
    # plan lit up nearly every label and they collided into a smear. Dropping
    # any label that would land within 22 units of the last one holds for any
    # band and any plan width.
    # Vertical labels fit every channel; horizontal ones are thinned by spacing.
    channels, last_x = [], -1e9
    for slot in slots:
        pos = x(slot["centreMhz"])
        if not vertical and pos - last_x < 22:
            continue
        channels.append({"x": round(pos, 1), "y": TOP - 6, "text": slot["channel"],
                         "psc": bool(slot.get("psc"))})
        last_x = pos

    step = 200 if span > 800 else 100 if span > 300 else 40 if span > 120 else 20
    ticks, freq = [], (int(low / step) + 1) * step
    axis_y = TOP + len(rows_in) * ROW_H
    while freq <= high:
        pos = x(freq)
        if GUTTER + 12 <= pos <= W - 12:
            ticks.append({"x": round(pos, 1), "label": int(freq)})
        freq += step

    # Regulatory sub-bands as background shading behind everything. Adjacent
    # UNII bands were all one pale grey, so on 6 GHz — four regions in a row —
    # nothing said where one ended and the next began. They alternate between
    # two greys AND carry a divider at each boundary: the alternation reads at
    # a glance, the divider makes it exact.
    #
    # DFS gets no tint of its own. It used to be amber, which is also the
    # in-use-but-not-permitted colour of the blocks painted on top, so a whole
    # sub-band read as a finding at a glance. DFS is named in the label
    # instead — it is a property of the band worth stating, not a warning.
    REGION_FILLS = ["#f8fafc", "#eceff3"]
    REGION_STROKES = ["#eef2f6", "#dfe4ea"]

    regions, dividers = [], []
    for index, region in enumerate(band.get("regions") or []):
        x0, x1 = x(region["clipLoMhz"]), x(region["clipHiMhz"])
        if x1 - x0 < 2:
            continue
        regions.append({
            "x": round(x0, 1), "w": round(x1 - x0, 1),
            "y": REGION, "h": round(height - REGION - AXIS + 4, 1),
            "fill": REGION_FILLS[index % 2],
            "stroke": REGION_STROKES[index % 2],
            "label": region["label"] + (" · DFS" if region["dfs"] else ""),
            "labelX": round((x0 + x1) / 2, 1), "labelY": REGION - 4,
            "labelFill": "#94a3b8",
            "showLabel": (x1 - x0) > 46,
        })
        if len(regions) > 1:
            dividers.append({"x": round(x0, 1)})

    # 6 GHz Preferred Scanning Channels, marked with a dotted drop line so the
    # eye can find them against 59 near-identical slots.
    psc = [{"x": round(x(s["centreMhz"]), 1)}
           for s in slots if s.get("psc")] if band.get("isSixGhz") else []

    return {"width": W, "height": round(height, 1), "gutter": GUTTER,
            "vertical": vertical, "labelSize": 9 if vertical else 12,
            "regions": regions, "dividers": dividers,
            "dividerTop": REGION, "dividerBottom": round(height - AXIS + 4, 1),
            "psc": psc, "pscTop": REGION,
            "pscBottom": round(TOP + len(rows_in) * ROW_H, 1),
            "axisY": round(axis_y, 1), "tickY2": round(axis_y + 4, 1),
            "tickTextY": round(axis_y + 15, 1),
            "rows": rows, "channels": channels, "ticks": ticks}


# ── device tables, split for the page ────────────────────────

def _ap_tables(aps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    APs as two tables: what the device IS, then where it LIVES on the network.

    The split is by subject rather than by arbitrary column count, so each half
    is worth reading on its own. `name` repeats in both because a split table
    is useless without a key to rejoin on.
    """
    identity = _table(
        ["Status", "AP name", "Model", "Serial", "AP group", "Firmware",
         "Clients", "SSIDs on air"],
        [[_dash(ap.get("status")), _dash(ap.get("name")), _dash(ap.get("model")),
          _dash(ap.get("serial")), _dash(ap.get("apGroup")), _dash(ap.get("firmware")),
          _dash(ap.get("clients")), len(ap.get("ssidsBroadcast") or [])]
         for ap in aps])

    addressing = _table(
        ["AP name", "IP", "Mask", "Gateway", "DNS", "Assigned", "External IP",
         "Mgmt VLAN", "Uplink", "Uplink speed"],
        [[_dash(ap.get("name")), _dash(ap.get("ip")), _dash(ap.get("netmask")),
          _dash(ap.get("gateway")), _dash(ap.get("dns")), _dash(ap.get("assignment")),
          _dash(ap.get("externalIp")), _dash(ap.get("mgmtVlan")),
          _dash(ap.get("uplinkStatus")),
          _dash(ap.get("uplinkSpeedMbps"), " Mbps") if ap.get("uplinkSpeedMbps") else "—"]
         for ap in aps])

    return [{"title": "Access points — identity and status", "table": identity},
            {"title": "Access points — addressing and uplink", "table": addressing}]


def _switch_tables(switches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    identity = _table(
        ["Status", "Switch", "Model", "Serial", "Firmware", "Ports", "Clients",
         "Uptime", "Config synced"],
        [[_dash(sw.get("status")), _dash(sw.get("name")), _dash(sw.get("model")),
          _dash(sw.get("serial")), _dash(sw.get("firmware")), _dash(sw.get("ports")),
          _dash(sw.get("clients")), _dash(sw.get("uptime")), _dash(sw.get("configSynced"))]
         for sw in switches])

    addressing = _table(
        ["Switch", "IP", "Mask", "Gateway", "DNS", "Assigned"],
        [[_dash(sw.get("name")), _dash(sw.get("ip")), _dash(sw.get("mask")),
          _dash(sw.get("gateway")), _dash(sw.get("dns")), _dash(sw.get("assignment"))]
         for sw in switches])

    return [{"title": "Switches — identity and status", "table": identity},
            {"title": "Switches — management addressing", "table": addressing}]


# ── the rest of the sections ─────────────────────────────────

def _wireless_table(wireless: Dict[str, Any]) -> Dict[str, Any]:
    return _table(
        ["SSID", "Type", "Security", "VLAN", "Radios", "AP groups", "On air", "Clients"],
        [[_dash(row.get("ssid")) if row.get("resolved", True)
          else f"(no definition) {row.get('networkId')}",
          _dash(row.get("type")), _dash(row.get("security")), _dash(row.get("vlans")),
          _dash(row.get("radios")),
          _dash([s.get("group") for s in row.get("scopes") or []], cap=6),
          _dash(row.get("apsBroadcasting")), _dash(row.get("clientsNow"))]
         for row in wireless.get("rows") or []])


def _vlan_table(vlans: Dict[str, Any]) -> Dict[str, Any]:
    ports_known = vlans.get("portsKnown")
    return _table(
        ["VLAN", "Origin", "Untagged ports", "Tagged ports", "SSIDs", "APs managed",
         "Clients", "DHCP pool", "Declared by"],
        [[str(row.get("vlan")) + (" (mgmt)" if row.get("isManagement") else ""),
          _dash(row.get("origin")),
          _dash(row.get("untaggedPorts")) if ports_known else "unknown",
          _dash(row.get("taggedPorts")) if ports_known else "unknown",
          _dash(row.get("ssids"), cap=8), _dash(row.get("apsManagedOn")),
          _dash(row.get("clients")), _dash(row.get("dhcpPool")),
          _dash(row.get("declaredBy"))]
         for row in vlans.get("rows") or []])


def _subnet_table(rows: List[Dict[str, Any]], noun: str) -> Dict[str, Any]:
    return _table(
        ["Subnet", "Prefix from", noun, "Usable", "Utilisation", "Gateways"],
        [[_dash(row.get("cidr")), _dash(row.get("source")), _dash(row.get("count")),
          _dash(row.get("usable")),
          _dash(row.get("utilisationPct"), "%") if row.get("utilisationPct") is not None else "—",
          _dash(row.get("gateways"))]
         for row in rows or []])


def _poe_tables(poe: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    if poe.get("switches"):
        out.append({"title": "PoE budget by switch", "table": _table(
            ["Switch", "Model", "Capacity", "Allocated", "Allocated %", "Draw",
             "Powered ports"],
            [[_dash(s.get("name")), _dash(s.get("model")),
              _dash(s.get("capacityWatts"), " W"), _dash(s.get("allocatedWatts"), " W"),
              _dash(s.get("allocatedPct"), "%"), _dash(s.get("drawWatts"), " W"),
              _dash(s.get("poweredPorts"))] for s in poe["switches"]])})
    if poe.get("apsOnPoe"):
        out.append({"title": "APs and the port powering them", "table": _table(
            ["AP", "Model", "State", "Switch", "Port", "Draw", "PoE type", "Link"],
            [[_dash(a.get("ap")), _dash(a.get("model")), _dash(a.get("state")),
              _dash(a.get("switch")), _dash(a.get("port")),
              _dash(a.get("watts"), " W") if a.get("watts") else "—",
              _dash(a.get("poeType")), _dash(a.get("link"))]
             for a in poe["apsOnPoe"]])})
    return out


def _dpsk_sections(dpsk: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Identity and policy, counts only.

    The report payload was already built through shape._dpsk_safe, which raises
    on a passphrase, an email, a phone number or an embedded identities array,
    so nothing sensitive can reach this module to be printed. Nothing here
    reintroduces a raw field.
    """
    pools = _table(
        ["Pool", "Linked by", "SSIDs here", "Passphrases", "Identities",
         "Format", "Length", "Devices per key", "Expiration"],
        [[_dash(p.get("name")), _dash(p.get("linkedBy")), _dash(p.get("networksHere")),
          _dash(p.get("passphraseCount")), _dash(p.get("identityCount")),
          _dash(p.get("passphraseFormat")), _dash(p.get("passphraseLength")),
          _dash(p.get("deviceLimitPerPassphrase")), _dash(p.get("expirationType"))]
         for p in dpsk.get("pools") or []])

    groups = _table(
        ["Pool", "Identity group", "Identities", "Networks", "Scope", "Inactive cleanup"],
        [[_dash(p.get("name")), _dash(g.get("name")), _dash(g.get("identityCount")),
          _dash(g.get("networkCount")), "property" if g.get("isProperty") else "tenant",
          f"after {g.get('inactiveAfterDays')} days" if g.get("autoCleanup") else "off"]
         for p in dpsk.get("pools") or [] for g in p.get("identityGroups") or []])

    policy_sets = []
    for row in policy.get("sets") or []:
        policy_sets.append({
            "title": f"Policy set — {row.get('name')}",
            "subtitle": ("Assigned to " + ", ".join(row.get("assignedTo") or [])
                         if row.get("assignedTo") else "Not assigned"),
            "table": _table(
                ["Priority", "Policy", "Type", "Conditions", "RADIUS attribute group",
                 "Rate limit"],
                [[_dash(m.get("priority")), _dash(m.get("policy")), _dash(m.get("policyType")),
                  _dash(m.get("conditions")),
                  "missing — group deleted" if m.get("radiusGroupMissing")
                  else _dash(m.get("radiusGroup")),
                  _dash([f"{r['mbps']} Mbps" for r in m.get("rateLimits") or [] if r.get("mbps")])]
                 for m in row.get("policies") or []]),
        })

    radius = _table(
        ["Group", "Description", "Rate limit", "Policies", "Stale assignments"],
        [[_dash(g.get("name")), _dash(g.get("description")),
          _dash([f"{r['mbps']} Mbps" for r in g.get("rateLimits") or [] if r.get("mbps")]),
          _dash(g.get("policyCount")),
          _dash(g.get("orphanedAssignments")) if g.get("orphanedAssignments") else "—"]
         for g in policy.get("radiusGroups") or []])

    return {"pools": pools, "groups": groups, "policySets": policy_sets, "radius": radius}


def build_context(report: Dict[str, Any], controller_name: str,
                  tenant_label: Optional[str] = None) -> Dict[str, Any]:
    """Everything the PDF template needs, already shaped for printing."""
    venue = report.get("venue") or {}
    inventory = report.get("inventory") or {}
    verification = report.get("verification") or {}
    findings = verification.get("findings") or []

    # The findings list is one section in the PDF, split into the part that
    # asks something of the reader and the part that records what was fine.
    # Both are printed: "we checked 24 things and 18 were clean" is what makes
    # this a review rather than a list of complaints.
    actionable = [f for f in findings
                  if f.get("severity") in ("critical", "warning", "info")]
    clear = [f for f in findings if f.get("severity") in ("ok", "skipped")]

    address = venue.get("address") or {}
    location = ", ".join(part for part in
                         (address.get("line"), address.get("city"), address.get("country"))
                         if part)

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "controller_name": controller_name,
        "tenant_label": tenant_label,
        "venue_name": venue.get("name") or "Unnamed venue",
        "venue_location": location or "No address on record",
        "venue_timezone": address.get("timezone"),
        "is_property": venue.get("isProperty"),
        "property": venue.get("property") or {},
        "venue": venue,

        "counts": verification.get("counts") or {},
        "score": verification.get("score") or {},
        "findings_actionable": actionable,
        "findings_clear": clear,
        "findings_total": len(findings),

        "aps": inventory.get("aps") or {},
        "switches": inventory.get("switches") or {},
        "clients": report.get("clients") or {},
        "wireless": report.get("wireless") or {},
        "radios": report.get("radios") or {},
        "spectrum": [{"band": b, "chart": _spectrum(b)}
                     for b in (report.get("radios") or {}).get("plan") or []],
        "poe": report.get("poe") or {},
        "vlans": report.get("vlans") or {},
        "addressing": report.get("addressing") or {},
        "dpsk": report.get("dpsk") or {},
        "policy": report.get("policy") or {},

        # Charts, restored to match the tab: status donuts, model and firmware
        # bar lists, and the wireless bar charts.
        "ap_donut": _status_donut(inventory.get("aps") or {}, "APs"),
        "switch_donut": _status_donut(inventory.get("switches") or {}, "switches"),
        "ap_models": _bars((inventory.get("aps") or {}).get("byModel"), 6),
        "ap_firmware": _bars((inventory.get("aps") or {}).get("byFirmware"), 6),
        "switch_models": _bars((inventory.get("switches") or {}).get("byModel"), 6),
        "switch_firmware": _bars((inventory.get("switches") or {}).get("byFirmware"), 6),
        "clients_by_band": _bars((report.get("clients") or {}).get("byBand"), 6),
        "clients_by_rssi": _bars((report.get("clients") or {}).get("byRssi"), 4),
        "clients_by_ssid": _bars((report.get("clients") or {}).get("bySsid"), 8),
        "clients_by_health": _bars((report.get("clients") or {}).get("byHealth"), 4),
        "top_aps": _bars((report.get("clients") or {}).get("topAps"), 8),

        "wireless_table": _wireless_table(report.get("wireless") or {}),
        "vlan_table": _vlan_table(report.get("vlans") or {}),
        "ap_subnet_table": _subnet_table((report.get("addressing") or {}).get("apSubnets"), "APs"),
        "switch_subnet_table": _subnet_table(
            (report.get("addressing") or {}).get("switchSubnets"), "Switches"),
        "poe_tables": _poe_tables(report.get("poe") or {}),
        "identity": _dpsk_sections(report.get("dpsk") or {}, report.get("policy") or {}),
        "ap_tables": _ap_tables((inventory.get("rows") or {}).get("aps") or []),
        "switch_tables": _switch_tables((inventory.get("rows") or {}).get("switches") or []),

        "meta": report.get("meta") or {},
        "fmt_time": _fmt_time,
        "evidence_header": evidence_header,
    }

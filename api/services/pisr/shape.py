"""
PISR shaping — raw RUCKUS ONE rows in, card payloads out.

Pure functions, no I/O. Everything here is arithmetic over what fetch.py
collected, which keeps the API calls in one file and the judgement in another.

Written against what a live tenant actually returns, not against the spec:

  * an AP's addressing lives in the nested `networkStatus` object — `IP` and
    `extIp` exist on the DTO but come back empty from `/venues/aps/query`;
  * so does its uplink: `poePort` + `poePortStatus` ("Up 1000Mbps full"),
    with `switchName`/`switchPort` empty and `switchSerialNumber` populated;
  * `radioStatuses[].wifiNetworks[]` names the SSIDs each radio is actually
    beaconing — the closest thing to proof that an SSID reached the air;
  * a client's SSID, VLAN, AP and RSSI live in `networkInformation`,
    `apInformation` and `signalStatus`, not as flat fields;
  * AP status is a coded string: `2_00_Operational`, `3_04_DisconnectedFromCloud`,
    `1_01_NeverContactedCloud` — so state matching is by substring;
  * a venue activation carries BOTH `isAllApGroups` and a full `apGroups` list.

Units note, from the R1 schema: switch `poeTotal`/`poeFree`/`poeUtilization` are
milliwatts, and `poeUtilization` is allocated power, NOT a percentage. Port
`poeUsed`/`poeTotal` are milliwatts too. Everything leaving this module is watts.
"""

import ipaddress
import json

import baselines
import config_labels
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ONLINE_WORDS = ("operational", "online", "connected", "active")
# "nevercontacted" counts as offline: an AP that has never reached the cloud is
# not up, whatever the reason. See _state for why it is not simply "unknown".
OFFLINE_WORDS = ("offline", "disconnected", "down", "nevercontacted")

# R1's AP subState enum, in full (Wi-Fi_Services_Ap.subState):
#   NeverContactedCloud, Initializing, Operational, ApplyingFirmware,
#   ApplyingConfiguration, FirmwareUpdateFailed, ConfigurationUpdateFailed,
#   DisconnectedFromCloud, Rebooting, None
# The two *UpdateFailed states describe a REACHABLE AP whose push failed, so
# _state counts them as online and the provisioning check reports the fault.
FAILED_UPDATE_WORDS = ("updatefailed",)
TRANSITIONAL_WORDS = ("initializing", "applying", "rebooting")


def _is_failed_update(value: Optional[str]) -> bool:
    text = (value or "").strip().lower()
    return any(word in text for word in FAILED_UPDATE_WORDS)


def _is_transitional(value: Optional[str]) -> bool:
    text = (value or "").strip().lower()
    return any(word in text for word in TRANSITIONAL_WORDS)


SPEED_RE = re.compile(r"(\d+)\s*mbps", re.I)

# A switch port's `portSpeed` is a short label, not the "Up 1000Mbps full"
# string _link_speed parses: "1G", "100M", "2.5G", "10G", sometimes a bare
# "1000". A gigabit unit is thousands of Mbps; a bare number is taken as Mbps.
_PORT_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([gm])", re.I)


def _port_speed_mbps(label: Optional[str]) -> Optional[int]:
    """Mbps out of a `portSpeed` label. None when it is absent or unparseable
    ('auto', 'unknown') — a port with no readable speed is not a slow port."""
    if not label:
        return None
    text = str(label).strip().lower()
    if text in ("auto", "unknown", "n/a", "-", ""):
        return None
    match = _PORT_SPEED_RE.search(text)
    if match:
        value = float(match.group(1))
        return int(value * 1000) if match.group(2).lower() == "g" else int(value)
    bare = re.search(r"\d+", text)
    return int(bare.group(0)) if bare else None

# meshRole values that mean "this AP is NOT meshing". Observed live: "DISABLED"
# on a venue with mesh off, and null. The rest are inferred from the vocabulary
# R1 uses elsewhere, which is why `_is_meshing` treats an UNRECOGNISED value as
# not-meshing rather than guessing: a check that accuses an AP of falling back
# to mesh had better be right, and the column shows the raw value regardless.
MESH_WIRED_ROLES = {"", "disabled", "none", "off", "root", "rap", "rootap",
                    "wired", "ethernet"}
MESH_ROLES = {"map", "emap", "mesh", "meshap"}


def _duration_text(seconds: Any) -> Optional[str]:
    """
    Seconds as something a person reads. Shaped here so both renderers and the
    checks' evidence rows print the same string without either of them
    formatting a duration.
    """
    total = int(_num(seconds, 0))
    if total <= 0:
        return None
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if total >= size:
            value = total // size
            return f"{value} {unit}{'s' if value != 1 else ''}"
    return f"{total} seconds"


def _is_meshing(role: Optional[str]) -> bool:
    """
    Is this AP carrying its traffic over the air instead of its cable?

    The install-validation signal nobody looks for. A mesh fallback AP is
    online, provisioned, broadcasting and serving clients — green on every
    other view in this report — while its Ethernet drop is dead, its PoE port
    is dead, or it was never patched. The crew leaves site and the site works,
    badly, until someone measures throughput.

    Deliberately conservative. Only values that clearly mean "meshing" count;
    anything unfamiliar is left alone rather than reported, because the cost of
    a false accusation here is a crew pulling a good cable.
    """
    value = _norm(role)
    if not value or value in MESH_WIRED_ROLES:
        return False
    return value in MESH_ROLES or "mesh" in value


def _epoch_ms_iso(value: Any) -> Optional[str]:
    """
    Epoch milliseconds to an ISO string, or None.

    R1 sends these as ints and sometimes as floats (1785702964230.0), so the
    int() is not decoration — datetime.fromtimestamp is fine with a float but
    the surrounding code compares and sorts these, and a float id sorts oddly
    against an int one.
    """
    ms = _num(value, 0)
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


# ── small helpers ────────────────────────────────────────────

def _num(value, default: float = 0.0) -> float:
    """R1 sends numbers as ints, floats, strings and occasionally '12%'."""
    if value is None or value is True or value is False:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().rstrip("%")
    try:
        return float(text)
    except ValueError:
        return default


def _as_int(value, default: int = 0) -> int:
    """An int for sorting. Non-numeric labels sort first, not crash."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _watts(milliwatts) -> float:
    return round(_num(milliwatts) / 1000.0, 1)


def _pct(part: float, whole: float) -> Optional[float]:
    if not whole:
        return None
    return round(part / whole * 100.0, 1)


def _tally(rows: Iterable[Dict[str, Any]], key: str, unknown: str = "Unknown") -> List[Dict[str, Any]]:
    """[{label, count}] sorted by count desc, then label — a bar list, ready to draw."""
    counts = Counter((row.get(key) or unknown) for row in rows)
    return [{"label": str(label), "count": count}
            for label, count in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]


def _state(value: Optional[str]) -> str:
    """
    Fold R1's status vocabularies into online / offline / other.

    Matching is by substring because AP status is coded — `2_00_Operational`,
    `3_04_DisconnectedFromCloud` — while switches use plain ONLINE / OFFLINE /
    PREPROVISIONED.

    `1_01_NeverContactedCloud` counts as OFFLINE. It used to land in "other" on
    the reasoning that a device never seen is not the same as one that went
    away — true taxonomically, useless operationally. Either way the AP is not
    on the network, and splitting them meant a venue with 107 up and 1 never
    contacted reported zero offline APs and read as healthy.

    Offline is tested before online on purpose: "disconnected" contains
    "connected", so the order is what keeps DisconnectedFromCloud from
    classifying as online.

    The two *UpdateFailed states count as ONLINE. That AP is reachable and
    serving; it simply is not running the config or firmware it was sent. That
    is a real fault, and it is reported by the provisioning check, whose
    subject it actually is — not by pretending the AP is unreachable.

    What stays in "other" is genuinely neither up nor down: Initializing,
    ApplyingFirmware, ApplyingConfiguration and Rebooting are all in flight and
    will resolve on their own.
    """
    text = (value or "").strip().lower()
    if not text:
        return "other"
    if any(word in text for word in OFFLINE_WORDS):
        return "offline"
    if any(word in text for word in ONLINE_WORDS):
        return "online"
    if any(word in text for word in FAILED_UPDATE_WORDS):
        # Reachable, and running the wrong config. Counting it as anything but
        # online would have two checks contradict each other: the provisioning
        # check correctly calls it "online and serving", so the reachability
        # check must not simultaneously call it "not online". The fault is real
        # and is reported — by the check whose subject it actually is.
        return "online"
    return "other"


def _pretty_status(value: Optional[str]) -> Optional[str]:
    """
    `2_00_Operational` is R1's wire format, not something to put on a chart.
    Strips the sort prefix and unpacks the camel case: "Disconnected from cloud".
    """
    if not value:
        return value
    text = re.sub(r"^\d+_\d+_", "", str(value)).strip()
    if not text:
        return str(value)
    if text.isupper():  # switches use ONLINE / PREPROVISIONED
        return text.capitalize()
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", text)
    if not words:
        return text
    return " ".join([words[0].capitalize()] + [w.lower() for w in words[1:]])


def _addr(ip: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    if not ip:
        return None
    try:
        return ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None


def _prefix_from_mask(netmask: Optional[str]) -> Optional[int]:
    """255.255.252.0 -> 22. Accepts a bare prefix length too."""
    if netmask is None:
        return None
    text = str(netmask).strip()
    if not text:
        return None
    if text.isdigit() and 0 <= int(text) <= 32:
        return int(text)
    try:
        return ipaddress.ip_network(f"0.0.0.0/{text}").prefixlen
    except ValueError:
        return None


# Never infer a subnet wider than this from observed addresses alone. A /16 is
# already 65k hosts; anything wider is far likelier to be two sites sharing a
# gateway string than one enormous flat network.
WIDEST_INFERRED_PREFIX = 16


def _smallest_network(addresses: List[ipaddress.IPv4Address]) -> Optional[ipaddress.IPv4Network]:
    """
    The smallest aligned CIDR containing every address given.

    The differing bits between the lowest and highest address bound how long a
    prefix can be: if they agree on the top 22 bits, a /22 holds them both and
    a /23 cannot.
    """
    if not addresses:
        return None
    low = int(min(addresses))
    high = int(max(addresses))
    prefix = 32 - (low ^ high).bit_length()
    prefix = max(WIDEST_INFERRED_PREFIX, min(32, prefix))
    return ipaddress.ip_network(f"{ipaddress.IPv4Address(low)}/{prefix}", strict=False)


def _subnet_groups(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Work out the real subnets an install landed in, rather than assuming /24.

    Two sources, in order of trust:

    1. **The device's own netmask.** APs report `netmask` in `networkStatus`,
       so on a /22 the AP itself says /22 — there is nothing to infer. This was
       being collected already and thrown away in favour of a hardcoded /24,
       which is why a large site full of one /22 rendered as four /24s.

    2. **Shared default gateway.** Devices that answer to the same gateway are
       on the same broadcast domain, so their addresses (and the gateway) can be
       supernetted into the smallest CIDR that holds them all. This *under*
       states the subnet — a /21 with only the bottom half populated looks like
       a /22 — so it is reported as a floor, not a fact.

    Anything with neither mask nor gateway falls back to a /24 bucket and is
    labelled as an assumption — but ONLY after checking it is not already
    inside a subnet some other device reported. Without that check a site on a
    single /21 rendered as the /21 plus a scattering of phantom /24s carved out
    of it: 10.2.0.0/21 with 418 APs, then 10.2.6.0/24 and 10.2.7.0/24 with one
    each, all three describing the same network. Every address is placed in the
    most specific subnet already known to contain it before any assumption is
    made about it.
    """
    exact: Dict[str, Dict[str, Any]] = {}
    by_gateway: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    loose: List[Dict[str, Any]] = []

    for device in devices:
        addr = _addr(device.get("ip"))
        if addr is None or addr.version != 4:
            continue
        prefix = _prefix_from_mask(device.get("netmask"))
        gateway = (device.get("gateway") or "").strip()

        if prefix is not None:
            network = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
            entry = exact.setdefault(str(network), {
                "cidr": str(network), "prefix": prefix, "source": "reported",
                "count": 0, "gateways": set(), "addresses": [],
            })
            entry["count"] += 1
            entry["addresses"].append(addr)
            if gateway:
                entry["gateways"].add(gateway)
        elif gateway:
            by_gateway[gateway].append({"addr": addr})
        else:
            loose.append({"addr": addr})

    rows = list(exact.values())

    def containing(addresses) -> Optional[Dict[str, Any]]:
        """
        The most specific known subnet holding every one of these addresses.

        Most specific, not first: a device inside both a /21 and a /24 that
        somebody reported belongs in the /24, because that is the more precise
        statement about where it actually is.
        """
        best_row, best_prefix = None, -1
        for row in rows:
            network = ipaddress.ip_network(row["cidr"])
            if network.prefixlen > best_prefix and all(a in network for a in addresses):
                best_row, best_prefix = row, network.prefixlen
        return best_row

    for gateway, members in by_gateway.items():
        addresses = [m["addr"] for m in members]
        gw_addr = _addr(gateway)
        if gw_addr is not None and gw_addr.version == 4:
            addresses = addresses + [gw_addr]
        network = _smallest_network(addresses)
        if network is None:
            continue
        # If a subnet somebody actually reported already contains these,
        # trust that over anything supernetted from a gateway. Containment is
        # the test; a shared gateway is corroboration, not a requirement —
        # keying on the gateway meant a device whose gateway no reported row
        # happened to list produced a duplicate subnet inside a real one.
        covered = containing(addresses)
        if covered:
            covered["count"] += len(members)
            if gateway:
                covered["gateways"].add(gateway)
            continue
        rows.append({"cidr": str(network), "prefix": network.prefixlen,
                     "source": "inferred", "count": len(members),
                     "gateways": {gateway}, "addresses": [m["addr"] for m in members]})

    # Loose devices last, so they can land in a subnet inferred above as well
    # as one that was reported.
    bucket = Counter()
    for item in loose:
        covered = containing([item["addr"]])
        if covered:
            # It has no mask of its own, but something else established the
            # subnet it sits in. Assuming a /24 here is what produced the
            # phantom subnets described above.
            covered["count"] += 1
            continue
        bucket[str(ipaddress.ip_network(f"{item['addr']}/24", strict=False))] += 1
    for cidr, count in bucket.items():
        rows.append({"cidr": cidr, "prefix": 24, "source": "assumed",
                     "count": count, "gateways": set(), "addresses": []})

    out = []
    for row in rows:
        network = ipaddress.ip_network(row["cidr"])
        usable = max(network.num_addresses - 2, 0) if network.prefixlen < 31 else network.num_addresses
        out.append({
            "label": row["cidr"],
            "cidr": row["cidr"],
            "prefix": row["prefix"],
            "count": row["count"],
            "source": row["source"],
            "gateways": sorted(row["gateways"]),
            "usable": usable,
            "utilisationPct": _pct(row["count"], usable) if usable else None,
        })
    out.sort(key=lambda r: (-r["count"], r["cidr"]))
    return out


def _is_private(ip: Optional[str]) -> Optional[bool]:
    try:
        return ipaddress.ip_address(str(ip).strip()).is_private
    except (ValueError, TypeError):
        return None


def _link_speed(status: Optional[str]) -> Optional[int]:
    """Mbps out of a link string like 'Up 1000Mbps full'; None when the port is down."""
    if not status or not str(status).strip().lower().startswith("up"):
        return None
    match = SPEED_RE.search(str(status))
    return int(match.group(1)) if match else None


MAX_VLAN_ID = 4094


def _vlan_list(value) -> List[int]:
    """
    Port VLAN membership, whatever shape it arrives in: a list, a bare int, or
    a string. Ranges are expanded; anything unparseable is dropped rather than
    guessed at.

    R1 returns a switch port's `vlanIds` SPACE-separated — "25 21 71 111 35 11"
    — which the previous implementation destroyed. It stripped every space
    first and then split on commas, so that string collapsed into the single
    chunk "2521711113511", which parses as an int perfectly happily and was
    counted as one VLAN with that id. The effect on a report was severe and
    quiet: every real VLAN on a trunk showed zero tagged ports (so core
    uplinks looked like they carried nothing), a nonsense VLAN row appeared
    beside them, and any client on one of those VLANs was then classified
    "undeclared" because nothing on the wire appeared to carry it.

    Separators are now comma, semicolon or any whitespace, and every id — not
    just the ends of a range — has to land inside 1..4094 to be kept.
    """
    if value is None or value == "":
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value] if 0 < value <= MAX_VLAN_ID else []
    if isinstance(value, list):
        out: List[int] = []
        for item in value:
            out.extend(_vlan_list(item))
        return out

    vlans: List[int] = []
    for chunk in re.split(r"[,;\s]+", str(value).strip()):
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                lo_i, hi_i = int(lo), int(hi)
            except ValueError:
                continue
            if 0 < lo_i <= hi_i <= MAX_VLAN_ID and hi_i - lo_i < 512:
                vlans.extend(range(lo_i, hi_i + 1))
            continue
        try:
            vlan = int(chunk)
        except ValueError:
            continue
        # A value outside the 802.1Q range is not a VLAN, it is a parse
        # artefact. Dropping it here is what stops a mangled string from
        # inventing a row in the VLAN table.
        if 0 < vlan <= MAX_VLAN_ID:
            vlans.append(vlan)
    return vlans


def _norm(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _mac(value: Optional[str]) -> str:
    """MACs come back colon-separated in either case, and sometimes hyphenated."""
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def _truthy(value) -> bool:
    """R1 sends booleans as real bools on some fields and as 'true'/'false' on others."""
    if isinstance(value, bool):
        return value
    return _norm(value) in ("true", "yes", "1")


def _port_up(port: Dict[str, Any]) -> bool:
    """Switch ports report a plain Up / Down, unlike every other status in R1."""
    return _norm(port.get("status")) == "up"


# ── flattened views ──────────────────────────────────────────

def ap_views(aps: List[Dict[str, Any]],
             groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    One flat dict per AP, with the nested objects unpacked. Every other AP-facing
    shaper reads these rather than the raw rows, so the nesting is handled once.
    """
    group_names = {g.get("id"): g.get("name") for g in groups}

    views = []
    for ap in aps:
        network = ap.get("networkStatus") or {}
        if not isinstance(network, dict):
            network = {}
        radios_raw = ap.get("radioStatuses") or []
        lan_ports = ap.get("lanPortStatuses") or []

        radios, ssids = [], []
        for radio in radios_raw if isinstance(radios_raw, list) else []:
            names = [n.get("name") for n in (radio.get("wifiNetworks") or []) if n.get("name")]
            ssids.extend(names)
            radios.append({
                "band": radio.get("band"),
                "channel": radio.get("channel"),
                "width": radio.get("channelBandwidth"),
                "power": radio.get("transmitterPower"),
                "chainmask": radio.get("chainmask"),
                "ssids": names,
            })

        uplink_status = ap.get("poePortStatus") or next(
            (p.get("physicalLink") for p in lan_ports
             if isinstance(p, dict) and _link_speed(p.get("physicalLink"))), None)

        views.append({
            "name": ap.get("name"),
            "serial": ap.get("serialNumber"),
            "model": ap.get("model"),
            "status": _pretty_status(ap.get("status")),
            "statusRaw": ap.get("status"),
            "state": _state(ap.get("status")),
            # Classified here so the checks stay pure readers of shaped data.
            # A failed update is a reachable AP running the wrong config; a
            # transitional AP is simply mid-flight. Both sit in state="other".
            "failedUpdate": _is_failed_update(ap.get("status")),
            "transitional": _is_transitional(ap.get("status")),
            "firmware": ap.get("firmwareVersion"),
            "mac": ap.get("macAddress"),
            "apGroupId": ap.get("apGroupId"),
            "apGroup": group_names.get(ap.get("apGroupId")) or ap.get("apGroupName"),
            "clients": int(_num(ap.get("clientCount"))),
            "meshRole": ap.get("meshRole"),
            # Precomputed so `checks.py` stays a pure reader and the mesh
            # vocabulary lives in exactly one place.
            "meshing": _is_meshing(ap.get("meshRole")),
            "lastSeen": ap.get("lastSeenTime"),
            # SECONDS. R1 does not label the unit and this is inferred from the
            # magnitudes a live tenant returns — values clustered at 1.9M-6.5M,
            # which is 22 to 75 days and plausible for a settled fleet. Read as
            # milliseconds the same numbers would be 30 minutes to 2 hours,
            # which no mixed fleet clusters into. If every AP ever starts
            # reporting "32 minutes", this assumption is what to revisit.
            "uptime": ap.get("uptime"),
            "uptimeSeconds": int(_num(ap.get("uptime"), 0)) or None,
            "uptimeText": _duration_text(ap.get("uptime")),
            "placed": bool(ap.get("floorplanId")),
            "tags": [t for t in (ap.get("tags") or []) if t] if isinstance(ap.get("tags"), list)
                    else ap.get("tags"),
            # addressing, from networkStatus
            "ip": network.get("ipAddress") or ap.get("IP"),
            "externalIp": network.get("externalIpAddress") or ap.get("extIp"),
            "netmask": network.get("netmask"),
            "gateway": network.get("gateway"),
            "dns": network.get("primaryDnsServer"),
            "assignment": network.get("ipAddressType"),
            "mgmtVlan": network.get("managementTrafficVlan"),
            # uplink, from the PoE port
            "uplinkSwitchSerial": ap.get("switchSerialNumber"),
            "uplinkSwitch": ap.get("switchName"),
            "uplinkPort": ap.get("switchPort") or ap.get("poePort"),
            "uplinkStatus": uplink_status,
            "uplinkSpeedMbps": _link_speed(uplink_status),
            # air
            "radios": radios,
            "ssidsBroadcast": sorted(set(ssids)),
        })
    return views


def client_views(clients: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One flat dict per associated client; SSID, AP and RSSI come out of nested objects."""
    views = []
    for client in clients:
        network = client.get("networkInformation") or {}
        ap = client.get("apInformation") or {}
        signal = client.get("signalStatus") or {}
        radio = client.get("radioStatus") or {}
        traffic = client.get("trafficStatus") or {}
        views.append({
            "mac": client.get("macAddress"),
            "hostname": client.get("hostname"),
            "ip": client.get("ipAddress"),
            "os": client.get("osType"),
            "band": client.get("band"),
            "ssid": network.get("ssid"),
            "networkId": network.get("id"),
            "vlan": network.get("vlan"),
            "encryption": network.get("encryptionMethod"),
            "apName": ap.get("name"),
            "apSerial": ap.get("serialNumber"),
            "rssi": signal.get("rssi"),
            "snr": signal.get("snr"),
            "health": signal.get("health"),
            "channel": radio.get("channel"),
            "connectedAt": client.get("connectedTime"),
            "traffic": traffic.get("totalTraffic"),
        })
    return views


# ── venue / property ─────────────────────────────────────────

def _channel_list(params: Dict[str, Any]) -> List[str]:
    """
    The channels a band is allowed to use.

    2.4 and 6 GHz report a single `allowedChannels`; 5 GHz splits into indoor
    and outdoor lists, which are unioned here — an install is one or the other
    and the venue config does not say which.
    """
    allowed = params.get("allowedChannels")
    if not allowed:
        indoor = params.get("allowedIndoorChannels") or []
        outdoor = params.get("allowedOutdoorChannels") or []
        allowed = sorted(set(list(indoor) + list(outdoor)), key=lambda c: _num(c))
    return [str(channel) for channel in (allowed or [])]


def floorplan_summary(venue: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Each floor plan, and whether it is actually usable.

    R1 returns a `scales` array of raw coordinate pairs — x1, y1, x2, y2 and a
    real-world distance. Rendered as-is that is eight numbers per plan saying
    nothing; what an install review needs to know is narrower and never stated
    directly: does a floor plan exist, does it have an image, and has anyone
    set a scale on it.

    THE SCALE IS THE PART THAT MATTERS. An unscaled floor plan still lets APs
    be dragged onto it and still satisfies "AP is placed on a plan", but every
    distance derived from it is meaningless — coverage estimates, AP spacing,
    the lot. It is a step that gets skipped precisely because the plan looks
    finished without it.

    A scale counts as set when it names a real-world distance. R1 fills the
    unit that was not used with 0.0, so both are checked and neither is
    trusted to be present.
    """
    out: List[Dict[str, Any]] = []
    for plan in venue.get("floorPlans") or []:
        if not isinstance(plan, dict):
            continue
        scales = [s for s in (plan.get("scales") or []) if isinstance(s, dict)]
        metres = next((_num(s.get("distanceInMeters")) for s in scales
                       if _num(s.get("distanceInMeters")) > 0), 0)
        feet = next((_num(s.get("distanceInFeet")) for s in scales
                     if _num(s.get("distanceInFeet")) > 0), 0)
        distance = (f"{metres:g} m" if metres else
                    f"{feet:g} ft" if feet else None)
        out.append({
            "id": plan.get("id"),
            "name": plan.get("name") or "Unnamed floor plan",
            "floorNumber": plan.get("floorNumber"),
            "imageName": plan.get("imageName") or plan.get("imageId"),
            "hasImage": bool(plan.get("imageId") or plan.get("imageName")),
            "scaleCount": len(scales),
            "scaleSet": bool(distance),
            "scaleDistance": distance,
        })
    out.sort(key=lambda row: (_as_int(row.get("floorNumber")), str(row["name"])))
    return out


def venue_card(venue: Dict[str, Any], prop: Optional[Dict[str, Any]],
               units: Dict[str, Any], mgmt_vlan: Optional[int],
               radio: Dict[str, Any], mesh: Dict[str, Any]) -> Dict[str, Any]:
    address = venue.get("address") or {}
    if not isinstance(address, dict):
        address = {}

    unit_config = (prop or {}).get("unitConfig") or {}
    comms = (prop or {}).get("communicationConfig") or {}

    return {
        "id": venue.get("id"),
        "name": venue.get("name"),
        "description": venue.get("description"),
        "address": {
            "line": address.get("addressLine"),
            "city": address.get("city"),
            "country": address.get("country"),
            "timezone": address.get("timezone") or venue.get("timezone"),
            "latitude": address.get("latitude") or venue.get("latitude"),
            "longitude": address.get("longitude") or venue.get("longitude"),
        },
        # Shaped rather than passed through: the raw form is coordinate pairs.
        # Read by the Config tab and by check_floorplans.
        "floorPlans": floorplan_summary(venue),
        "isProperty": prop is not None,
        # Field names verified against a live property. The previous set was
        # guessed and every one of them missed: `page.totalElements` (the count
        # is top-level), `unitConfig.namingConvention` (no such field) and
        # `communicationConfig.emailEnabled`/`smsEnabled` (they are `sendEmail`
        # and `sendSms`). Three of the five rows on this card were therefore
        # always blank, which is why it looked like it did nothing.
        "property": None if prop is None else {
            "status": prop.get("status"),
            "description": prop.get("description"),
            "residentPortalId": prop.get("residentPortalId"),
            "personaGroupId": prop.get("personaGroupId"),
            "residentPortalAllowed": unit_config.get("residentPortalAllowed"),
            "residentApiAllowed": unit_config.get("residentApiAllowed"),
            "guestAllowed": unit_config.get("guestAllowed"),
            "maxUnitCount": unit_config.get("maxUnitCount") if unit_config.get("useMaxUnitCount") else None,
            "unitCount": units.get("total"),
            "unitsByStatus": units.get("byStatus") or [],
            "unitsWithResident": units.get("withResident"),
            "unitsWithoutResident": units.get("withoutResident"),
            "unitIdentityCount": units.get("identityCount"),
            "communication": {
                "email": comms.get("sendEmail"),
                "sms": comms.get("sendSms"),
                "notifyOnUnitSuspend": comms.get("notifyOnUnitSuspend"),
            } if comms else None,
        },
        "managementVlan": mgmt_vlan,
        "mesh": {
            "enabled": mesh.get("enabled"),
            "radioType": mesh.get("radioType"),
            "zeroTouch": mesh.get("zeroTouchEnabled"),
        } if isinstance(mesh, dict) and mesh else None,
        "meshEnabled": mesh.get("enabled") if isinstance(mesh, dict) else None,
        "meshZeroTouch": mesh.get("zeroTouchEnabled") if isinstance(mesh, dict) else None,
        # Template enforcement: an enforced venue is driven by a config template,
        # so local edits are not the source of truth for it.
        "enforced": venue.get("isEnforced"),
        "isTemplate": venue.get("isTemplate"),
        # 5 GHz split into two independent radios changes what "5 GHz" means in
        # every channel list below it, so it is called out on its own.
        "dual5g": bool(isinstance(radio, dict) and radio.get("radioParamsDual5G")),
        # The configured channel plan, band by band. Sits beside what the APs
        # actually landed on in `radios`. `allowed` is the channel list the
        # venue permits — the gap between that and the channels APs actually
        # picked is where a bad channel plan shows up.
        "radio": [
            {
                "band": label,
                "width": (radio.get(key) or {}).get("channelBandwidth"),
                "power": (radio.get(key) or {}).get("txPower"),
                "method": (radio.get(key) or {}).get("method"),
                "changeInterval": (radio.get(key) or {}).get("changeInterval"),
                "scanInterval": (radio.get(key) or {}).get("scanInterval"),
                "combineChannels": (radio.get(key) or {}).get("combineChannels"),
                "allowed": _channel_list(radio.get(key) or {}),
            }
            for label, key in (("2.4 GHz", "radioParams24G"), ("5 GHz", "radioParams50G"),
                               ("6 GHz", "radioParams6G"))
            if isinstance(radio, dict) and isinstance(radio.get(key), dict)
        ],
    }


# ── inventory ────────────────────────────────────────────────

def inventory_card(aps: List[Dict[str, Any]],
                   switches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`aps` are ap_views; `switches` are raw switch rows."""
    ap_states = Counter(ap["state"] for ap in aps)
    sw_states = Counter(_state(sw.get("deviceStatus")) for sw in switches)

    def not_online(rows: List[Dict[str, Any]], state_of, status_of) -> List[Dict[str, Any]]:
        """
        Every device that is not online, tallied by the status R1 actually gave.

        `online + offline` does not account for a fleet: `_state` folds anything
        it does not recognise — `1_01_NeverContactedCloud` above all — into
        "other" on purpose, so a venue can have zero offline APs and still not
        be fully up. Callers that summarise device health need the real names,
        not a count they have to guess a label for.
        """
        tally = Counter(status_of(row) or "Unknown"
                        for row in rows if state_of(row) != "online")
        return [{"label": str(label), "count": count}
                for label, count in sorted(tally.items(), key=lambda kv: (-kv[1], str(kv[0])))]

    sw_rows = [{
        "name": sw.get("name"),
        "serial": sw.get("serialNumber"),
        "model": sw.get("model"),
        "status": _pretty_status(sw.get("deviceStatus")),
        "statusRaw": sw.get("deviceStatus"),
        "state": _state(sw.get("deviceStatus")),
        "firmware": sw.get("firmwareVersion"),
        "ip": sw.get("ipAddress"),
        "mask": sw.get("subnetMask"),
        "gateway": sw.get("defaultGateway"),
        "dns": sw.get("dns"),
        "assignment": sw.get("staticOrDynamic"),
        "ports": int(_num(sw.get("numOfPorts"))),
        "isStack": bool(sw.get("isStack")),
        "units": int(_num(sw.get("numOfUnits"), 1)),
        "clients": int(_num(sw.get("clientCount"))),
        "uptime": sw.get("uptime"),
        "configSynced": sw.get("syncedSwitchConfig"),
        "warning": sw.get("operationalWarning"),
    } for sw in switches]

    return {
        "aps": {
            "total": len(aps),
            "online": ap_states.get("online", 0),
            "offline": ap_states.get("offline", 0),
            "other": ap_states.get("other", 0),
            # Non-online devices named by their real status, so a summary can
            # say "5 never contacted cloud" instead of miscounting them as up.
            "notOnlineByStatus": not_online(aps, lambda a: a["state"], lambda a: a["status"]),
            "clients": sum(ap["clients"] for ap in aps),
            "byStatus": _tally(aps, "status"),
            "byModel": _tally(aps, "model"),
            "byFirmware": _tally(aps, "firmware"),
            "byGroup": _tally(aps, "apGroup", unknown="(no group)"),
        },
        "switches": {
            "total": len(switches),
            "online": sw_states.get("online", 0),
            "offline": sw_states.get("offline", 0),
            "other": sw_states.get("other", 0),
            "notOnlineByStatus": not_online(
                switches,
                lambda sw: _state(sw.get("deviceStatus")),
                lambda sw: _pretty_status(sw.get("deviceStatus"))),
            "ports": sum(int(_num(sw.get("numOfPorts"))) for sw in switches),
            "stacks": sum(1 for sw in switches if sw.get("isStack")),
            "clients": sum(int(_num(sw.get("clientCount"))) for sw in switches),
            "byStatus": _tally([{"status": _pretty_status(sw.get("deviceStatus"))}
                                for sw in switches], "status"),
            "byModel": _tally(switches, "model"),
            "byFirmware": _tally(switches, "firmwareVersion"),
        },
        "rows": {"aps": aps, "switches": sw_rows},
    }


# ── addressing ───────────────────────────────────────────────

def addressing_card(aps: List[Dict[str, Any]], switches: List[Dict[str, Any]],
                    pools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Where the install actually lives on the network: which subnets the devices
    landed in, and what the site looks like from outside.
    """
    # Real subnets, from each AP's own netmask where it reports one and from
    # shared gateways where it does not — see _subnet_groups.
    ap_subnets = _subnet_groups([
        {"ip": ap["ip"], "netmask": ap.get("netmask"), "gateway": ap.get("gateway")}
        for ap in aps
    ])
    missing_ip = sum(1 for ap in aps if not ap["ip"] and ap["state"] == "online")

    sw_subnets = _subnet_groups([
        {"ip": sw.get("ipAddress"), "netmask": sw.get("subnetMask") or sw.get("netmask"),
         "gateway": sw.get("defaultGateway")}
        for sw in switches
    ])
    external = Counter(ap["externalIp"] for ap in aps if ap["externalIp"])

    gateways = Counter()
    dns_servers = Counter()
    assignment = Counter()
    for ap in aps:
        if ap["gateway"]:
            gateways[ap["gateway"]] += 1
        if ap["dns"]:
            dns_servers[ap["dns"]] += 1
        if ap["assignment"]:
            assignment[str(ap["assignment"]).lower()] += 1
    for sw in switches:
        if sw.get("defaultGateway"):
            gateways[sw["defaultGateway"]] += 1
        raw = sw.get("dns")
        for server in (raw if isinstance(raw, list) else str(raw or "").split(",")):
            server = str(server).strip()
            if server:
                dns_servers[server] += 1
        if sw.get("staticOrDynamic"):
            assignment[str(sw["staticOrDynamic"]).lower()] += 1

    pool_rows = []
    for pool in pools:
        used = _num(pool.get("usedIpCount"))
        total = _num(pool.get("totalIpCount"))
        pool_rows.append({
            "name": pool.get("name"),
            "subnet": pool.get("subnetAddress"),
            "mask": pool.get("subnetMask"),
            "start": pool.get("startIpAddress"),
            "end": pool.get("endIpAddress"),
            "vlan": pool.get("vlanId"),
            "dns": [d for d in (pool.get("primaryDnsIp"), pool.get("secondaryDnsIp")) if d],
            "leaseHours": pool.get("leaseTimeHours"),
            "used": int(used),
            "total": int(total),
            "pct": _pct(used, total),
            "active": pool.get("active"),
        })

    return {
        # Each row carries `source`: "reported" (the device's own netmask),
        # "inferred" (smallest CIDR spanning a shared gateway — a floor, the
        # real subnet may be larger) or "assumed" (a plain /24 bucket).
        "apSubnets": ap_subnets,
        "switchSubnets": sw_subnets,
        "apsWithoutIp": missing_ip,
        "external": [{"ip": ip, "count": n, "private": _is_private(ip)}
                     for ip, n in external.most_common()],
        "gateways": [{"label": gw, "count": n} for gw, n in gateways.most_common()],
        "dns": [{"label": server, "count": n} for server, n in dns_servers.most_common()],
        "assignment": [{"label": kind, "count": n} for kind, n in assignment.most_common()],
        "dhcpPools": pool_rows,
    }


# ── VLANs ────────────────────────────────────────────────────

def vlan_card(ports: List[Dict[str, Any]], ssids: List[Dict[str, Any]],
              mgmt_vlan: Optional[int], pools: List[Dict[str, Any]],
              aps: List[Dict[str, Any]], clients: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    One row per VLAN, with every place that VLAN shows up. The point is the
    cross-check: an SSID pointing at a VLAN no switch port carries is a working
    config that will never pass traffic.
    """
    untagged = Counter()
    tagged = Counter()
    for port in ports:
        native = _vlan_list(port.get("unTaggedVlan"))
        if native:
            untagged[native[0]] += 1
        for vlan in set(_vlan_list(port.get("vlanIds"))):
            tagged[vlan] += 1

    ssid_vlans: Dict[int, List[str]] = defaultdict(list)
    for ssid in ssids:
        for vlan in ssid.get("vlans") or []:
            ssid_vlans[vlan].append(ssid.get("ssid") or ssid.get("name") or "?")

    pool_vlans = {int(_num(p.get("vlanId"))): p.get("name")
                  for p in pools if p.get("vlanId") is not None}
    ap_mgmt = Counter(int(_num(ap["mgmtVlan"])) for ap in aps if ap["mgmtVlan"] is not None)
    client_vlans = Counter(int(_num(c["vlan"])) for c in clients if c["vlan"] is not None)

    every_vlan = (set(untagged) | set(tagged) | set(ssid_vlans) | set(pool_vlans)
                  | set(ap_mgmt) | set(client_vlans))
    if mgmt_vlan:
        every_vlan.add(int(mgmt_vlan))

    rows = []
    for vlan in sorted(every_vlan):
        sources = []
        if untagged.get(vlan):
            sources.append("untagged")
        if tagged.get(vlan):
            sources.append("tagged")
        if vlan in ssid_vlans:
            sources.append("ssid")
        if vlan in pool_vlans:
            sources.append("dhcp")
        if ap_mgmt.get(vlan):
            sources.append("ap-mgmt")
        if client_vlans.get(vlan):
            sources.append("clients")
        # Split where a VLAN comes from. Configuration DECLARES a VLAN (a
        # switch port carries it, an SSID targets it, a DHCP pool serves it,
        # the venue sets it for AP management). Runtime OBSERVES one (clients
        # are on it, APs report managing on it). A VLAN that is observed but
        # never declared is the interesting case: on a DPSK or RADIUS site
        # that is dynamic per-identity VLAN assignment, which is invisible in
        # the venue's own config and looked, wrongly, like a phantom row.
        declared = [name for name in sources
                    if name in ("untagged", "tagged", "ssid", "dhcp")]
        is_mgmt = bool(mgmt_vlan) and vlan == int(mgmt_vlan)
        if is_mgmt:
            declared.append("venue-mgmt")
        observed = [name for name in sources if name in ("clients", "ap-mgmt")]

        rows.append({
            "vlan": vlan,
            "untaggedPorts": untagged.get(vlan, 0),
            "taggedPorts": tagged.get(vlan, 0),
            "ssids": ssid_vlans.get(vlan, []),
            "dhcpPool": pool_vlans.get(vlan),
            "apsManagedOn": ap_mgmt.get(vlan, 0),
            "clients": client_vlans.get(vlan, 0),
            "isManagement": is_mgmt,
            "sources": sources,
            "declaredBy": declared,
            "observedBy": observed,
            # "configured"  — something in this venue's config declares it.
            # "undeclared"  — only seen in live traffic. Not necessarily wrong.
            "origin": "configured" if declared else "undeclared",
        })

    return {
        "managementVlan": mgmt_vlan,
        "apManagementVlans": [{"label": str(vlan), "count": n} for vlan, n in ap_mgmt.most_common()],
        "rows": rows,
        "portsSeen": len(ports),
        # With no switch ports read, the tagged/untagged columns are UNKNOWN,
        # not zero — nothing was looked at. The UI has to say so rather than
        # render a confident 0 that reads as "this VLAN is on no port".
        "portsKnown": bool(ports),
        "distinct": len(every_vlan),
        "undeclaredWithClients": sum(1 for row in rows
                                     if row["origin"] == "undeclared" and row["clients"]),
    }


# ── PoE ──────────────────────────────────────────────────────

def poe_card(switches: List[Dict[str, Any]], ports: List[Dict[str, Any]],
             aps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Chassis budget against real draw, plus which APs are hanging off which port.

    Allocated and consumed are kept apart on purpose: R1's switch-level
    `poeUtilization` is power *committed* to attached devices, while the port
    rows carry what is actually being drawn. A budget that looks tight on
    allocation but idle on draw is a class-negotiation story, not a capacity one.

    APs are matched to ports through LLDP: an AP's `poePort` is its own LAN port
    index and a port row's `switchSerial` is the switch MAC, so neither joins the
    two directly — but the port reports the neighbour it can see.
    """
    # An AP's `poePort` is its own LAN port index ("0"), and a port row's
    # `switchSerial` is the switch MAC, so neither joins AP to port directly.
    # LLDP does: the port records the neighbour it sees.
    ap_by_mac = {_mac(ap["mac"]): ap for ap in aps if ap["mac"]}
    ap_by_name = {_norm(ap["name"]): ap for ap in aps if ap["name"]}

    def neighbour_ap(port: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        found = ap_by_mac.get(_mac(port.get("neighborMacAddress")))
        if found:
            return found
        name = _norm(port.get("neighborName"))
        # LLDP often appends a radio suffix to the AP name.
        return ap_by_name.get(name) or ap_by_name.get(name.rsplit(".", 1)[0])

    port_for_ap: Dict[str, Dict[str, Any]] = {}
    for port in ports:
        found = neighbour_ap(port)
        if found and found["serial"] not in port_for_ap:
            port_for_ap[found["serial"]] = port

    switch_names = {_mac(sw.get("switchMac") or sw.get("id")): sw.get("name")
                    for sw in switches}

    port_draw_by_switch = defaultdict(float)
    powered_ports = 0
    total_draw_mw = 0.0
    class_tally = Counter()
    consumers = []

    for port in ports:
        used_mw = _num(port.get("poeUsed"))
        if used_mw <= 0:
            continue
        powered_ports += 1
        total_draw_mw += used_mw
        name = port.get("switchName") or switch_names.get(_mac(port.get("switchSerial")))
        neighbour = neighbour_ap(port)
        port_draw_by_switch[name] += used_mw
        class_tally[port.get("poeType") or "unknown"] += 1
        identifier = port.get("portIdentifier") or port.get("portIdentifierFormatted")
        consumers.append({
            "switch": name,
            "port": identifier,
            "watts": _watts(used_mw),
            "budgetWatts": _watts(port.get("poeTotal")),
            "poeType": port.get("poeType"),
            "portName": port.get("name"),
            "neighbor": port.get("neighborName"),
            "ap": (neighbour or {}).get("name"),
            "speed": port.get("portSpeed"),
        })
    consumers.sort(key=lambda row: row["watts"], reverse=True)

    switch_rows = []
    total_capacity_mw = allocated_mw = 0.0
    for sw in switches:
        capacity = _num(sw.get("poeTotal"))
        allocated = _num(sw.get("poeUtilization"))
        free = _num(sw.get("poeFree"))
        if not capacity and free and allocated:
            capacity = free + allocated
        if not capacity:
            continue  # a non-PoE switch has no budget to report
        total_capacity_mw += capacity
        allocated_mw += allocated
        mac = _mac(sw.get("switchMac") or sw.get("id"))
        draw = port_draw_by_switch.get(sw.get("name"), 0.0)
        switch_rows.append({
            "name": sw.get("name"),
            "model": sw.get("model"),
            "state": _state(sw.get("deviceStatus")),
            "capacityWatts": _watts(capacity),
            "allocatedWatts": _watts(allocated),
            "freeWatts": _watts(free),
            "drawWatts": _watts(draw),
            "allocatedPct": _pct(allocated, capacity),
            "drawPct": _pct(draw, capacity),
            "poweredPorts": sum(1 for p in ports
                                if _mac(p.get("switchSerial")) == mac and _num(p.get("poeUsed")) > 0),
            "poeCapablePorts": sum(1 for p in ports
                                   if _mac(p.get("switchSerial")) == mac
                                   and _truthy(p.get("isPoeSupported"))),
        })
    switch_rows.sort(key=lambda row: row["allocatedPct"] or 0, reverse=True)

    # Every AP, with the port it is plugged into where LLDP found one. An AP
    # whose port cannot be found is still listed — its own link status is the
    # part that matters for install QA.
    aps_on_poe = []
    for ap in aps:
        port = port_for_ap.get(ap["serial"])
        aps_on_poe.append({
            "ap": ap["name"],
            "model": ap["model"],
            "state": ap["state"],
            "switch": (port or {}).get("switchName")
                      or switch_names.get(_mac((port or {}).get("switchSerial")))
                      or ap["uplinkSwitch"] or ap["uplinkSwitchSerial"],
            "port": (port or {}).get("portIdentifier")
                    or (port or {}).get("portIdentifierFormatted"),
            "watts": _watts((port or {}).get("poeUsed")) if port else None,
            "poeType": (port or {}).get("poeType"),
            "portSpeed": (port or {}).get("portSpeed"),
            "link": ap["uplinkStatus"],
            "speedMbps": ap["uplinkSpeedMbps"],
            "portFound": port is not None,
        })
    aps_on_poe.sort(key=lambda row: str(row["ap"] or ""))

    return {
        # A venue with no PoE-capable switch has no budget, which is not the
        # same fact as a budget of zero. Without this the overview rendered
        # "0.0 W of 0.0 W" at an all-AP site and read like a broken meter.
        "hasPoeBudget": bool(switch_rows),
        "switchCount": len(switches),
        "capacityWatts": _watts(total_capacity_mw),
        "allocatedWatts": _watts(allocated_mw),
        "drawWatts": _watts(total_draw_mw),
        "allocatedPct": _pct(allocated_mw, total_capacity_mw),
        "drawPct": _pct(total_draw_mw, total_capacity_mw),
        "poweredPorts": powered_ports,
        "switches": switch_rows,
        "byType": [{"label": str(label), "count": n} for label, n in class_tally.most_common()],
        "topConsumers": consumers[:25],
        "apsOnPoe": aps_on_poe,
    }


# ── ports / cabling ──────────────────────────────────────────

def port_card(ports: List[Dict[str, Any]],
              aps: Optional[List[Dict[str, Any]]] = None,
              switches: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Link health for the ports that are actually up — install QA, not monitoring."""
    up = [p for p in ports if _port_up(p)]
    speeds = Counter(p.get("portSpeed") or "unknown" for p in up)

    # Ports that came up below a gigabit, split by what is on the other end.
    # An infrastructure link (switch-to-switch) or an AP link at 100 Mbps is a
    # bad patch lead and a warning; an access port to an endpoint may simply be
    # a slow device, so it is reported as info to confirm rather than as a fault.
    # AP links are matched the same way poe_card matches them — through LLDP.
    ap_by_mac = {_mac(ap.get("mac")): ap for ap in (aps or []) if ap.get("mac")}
    ap_by_name = {_norm(ap.get("name")): ap for ap in (aps or []) if ap.get("name")}
    switch_macs = {_mac(sw.get("switchMac") or sw.get("id"))
                   for sw in (switches or []) if (sw.get("switchMac") or sw.get("id"))}
    switch_names = {_norm(sw.get("name")) for sw in (switches or []) if sw.get("name")}

    def link_kind(port: Dict[str, Any]) -> str:
        name = _norm(port.get("neighborName"))
        mac = _mac(port.get("neighborMacAddress"))
        if (mac and mac in ap_by_mac) or ap_by_name.get(name) \
                or ap_by_name.get(name.rsplit(".", 1)[0]):
            return "ap"
        if (mac and mac in switch_macs) or (name and name in switch_names):
            return "switch"
        return "endpoint"

    slow_links = []
    for port in up:
        mbps = _port_speed_mbps(port.get("portSpeed"))
        if mbps is None or mbps >= 1000:
            continue
        slow_links.append({
            "switch": port.get("switchName"),
            "port": port.get("portIdentifierFormatted") or port.get("portIdentifier"),
            "name": port.get("name"),
            "speed": port.get("portSpeed"),
            "speedMbps": mbps,
            "neighbor": port.get("neighborName"),
            # "ap" / "switch" are infrastructure (warning); "endpoint" is info.
            "kind": link_kind(port),
        })
    slow_links.sort(key=lambda row: (row["kind"] == "endpoint", row["speedMbps"]))

    errored = []
    for port in up:
        crc = _num(port.get("crcErr"))
        in_err = _num(port.get("inErr"))
        out_err = _num(port.get("outErr"))
        if crc + in_err + out_err <= 0:
            continue
        errored.append({
            "switch": port.get("switchName"),
            "port": port.get("portIdentifierFormatted") or port.get("portIdentifier"),
            "name": port.get("name"),
            "crc": int(crc), "inErr": int(in_err), "outErr": int(out_err),
            "speed": port.get("portSpeed"),
            "media": port.get("mediaType") or port.get("portConnectorType"),
            "neighbor": port.get("neighborName"),
        })
    errored.sort(key=lambda row: row["crc"] + row["inErr"] + row["outErr"], reverse=True)

    return {
        "total": len(ports),
        "up": len(up),
        "down": len(ports) - len(up),
        "bySpeed": [{"label": str(label), "count": n} for label, n in speeds.most_common()],
        "slowLinks": slow_links[:25],
        "slowCount": len(slow_links),
        "slowInfra": sum(1 for row in slow_links if row["kind"] in ("ap", "switch")),
        "errored": errored[:25],
        "erroredCount": len(errored),
        "errDisabled": [{
            "switch": p.get("switchName"),
            "port": p.get("portIdentifierFormatted") or p.get("portIdentifier"),
            "reason": p.get("errorDisableStatus"),
        } for p in ports if p.get("errorDisableStatus")
            and _norm(p.get("errorDisableStatus")) not in ("none", "false", "normal")][:25],
    }


# R1 alarm severities, most serious first. Anything it sends that is not on
# this list sorts last rather than being dropped — a severity nobody has seen
# before is exactly the one worth showing.
ALARM_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "warning": 3, "info": 4}


def _alarm_text(raw: Any, names: Dict[str, str], fallback: str) -> str:
    """
    R1's alarm message, made readable.

    `message` arrives as a JSON STRING wrapping a template, not as prose:

        '{"message_template":"AP @@apName disconnected from the cloud controller."}'

    The `@@token` placeholders are substituted by the R1 console from its own
    context, which PISR does not have — so left alone they render literally and
    the reader sees "@@apName" where a name should be. Every token is therefore
    replaced with the device's real name where the report knows it, and with
    the serial where it does not, which is still more use than the raw token.

    Anything unparseable falls back to `fallback` (the alarm's `name`), because
    an alarm with an ugly title is worth showing and an alarm swallowed by a
    JSON error is not.
    """
    text = ""
    if isinstance(raw, dict):
        text = raw.get("message_template") or raw.get("message") or ""
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            text = (parsed.get("message_template") or parsed.get("message") or ""
                    if isinstance(parsed, dict) else raw)
        except ValueError:
            text = raw
    text = (text or "").strip()
    if not text:
        return fallback

    def swap(match: "re.Match") -> str:
        return names.get(match.group(1), fallback)

    return re.sub(r"@@(\w+)", swap, text).strip()


def incident_card(alarms: List[Dict[str, Any]], aps: List[Dict[str, Any]],
                  switches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    What RUCKUS ONE is already raising about this venue.

    Deliberately kept apart from `verification`. The checks in `checks.py` are
    PISR's own opinion about whether an install looks finished; this is the
    platform's opinion about whether the venue is healthy right now, and the
    two disagree usefully. A venue can pass every check here and still have R1
    shouting about an AP that dropped off an hour ago — or the reverse, where
    R1 is quiet because everything it monitors is nominal while three SSIDs
    were never activated.

    ACTIVE ALARMS ONLY. The endpoint exposes no status, no cleared time and no
    acknowledgement, so there is nothing to filter and nothing to say about
    history. Do not add a "recently cleared" view without checking that R1 has
    started returning the field for it — see `fetch.ALARM_FIELDS`.
    """
    # Serial and MAC both, because the alarm rows key on serialNumber while
    # some entity types only carry a MAC.
    names: Dict[str, str] = {}
    for ap in aps:
        for key in (ap.get("serial"), _mac(ap.get("mac"))):
            if key and ap.get("name"):
                names[key] = ap["name"]
    for sw in switches:
        for key in (sw.get("serialNumber"), _mac(sw.get("switchMac") or sw.get("id"))):
            if key and sw.get("name"):
                names[key] = sw["name"]

    rows = []
    for alarm in alarms:
        serial = alarm.get("serialNumber") or alarm.get("entityId")
        mac = _mac(alarm.get("apMac"))
        device = names.get(serial) or names.get(mac) or serial or "unknown device"
        # One name for every placeholder in the template. The tokens differ by
        # entity type (@@apName, @@edgeName, @@switchName) and resolving each
        # to the same device is correct — an alarm names one thing.
        token_names = {token: device for token in
                       ("apName", "edgeName", "switchName", "deviceName", "name")}
        severity = str(alarm.get("severity") or "").strip()
        rows.append({
            "id": alarm.get("id"),
            # R1's own type, e.g. ApDisConnected. Kept raw: it is the thing to
            # search their documentation for.
            "type": alarm.get("name"),
            "severity": severity or "Unknown",
            "entityType": alarm.get("entityType"),
            "device": device,
            "serial": serial,
            "model": alarm.get("model"),
            "reason": alarm.get("reason") or None,
            "text": _alarm_text(alarm.get("message"), token_names,
                                alarm.get("name") or "Alarm"),
            # Epoch milliseconds, and occasionally as a float. int() first or
            # the ISO conversion raises on the ones that arrive as 1.78e12.
            "raisedAt": _epoch_ms_iso(alarm.get("startTime")),
            "raisedAtMs": int(_num(alarm.get("startTime"))) or None,
        })

    rows.sort(key=lambda r: (ALARM_SEVERITY_ORDER.get(r["severity"].lower(), 9),
                             -(r["raisedAtMs"] or 0)))

    return {
        "total": len(rows),
        "rows": rows,
        "bySeverity": [{"label": label, "count": n} for label, n in
                       sorted(Counter(r["severity"] for r in rows).items(),
                              key=lambda kv: ALARM_SEVERITY_ORDER.get(kv[0].lower(), 9))],
        "byType": _tally(rows, "type", unknown="Unknown"),
        # So the card can say "3 APs and an Edge" rather than just a number.
        "byEntity": _tally(rows, "entityType", unknown="Unknown"),
        # The oldest one still raised. An alarm from three weeks ago on a fresh
        # install is a different conversation from one raised this morning.
        "oldest": min((r["raisedAt"] for r in rows if r["raisedAt"]), default=None),
    }


# Keys that carry no meaning for a reader and only lengthen the tree. Dropped
# from the config view, never from anything the checks read.
CONFIG_NOISE_KEYS = frozenset({"id", "tenantId", "venueId", "softDeleted",
                               "lastUpdated", "requestId"})


def _config_clean(node: Any, depth: int = 0) -> Any:
    """
    A settings blob, tidied for display and nothing else.

    Drops the identifiers R1 echoes back into every response — they are the
    same value on every branch and they push the interesting fields off the
    screen. Everything else is kept verbatim, including fields nobody
    recognises: this view exists to show what is actually set, and a shaper
    that decides what matters is a shaper that hides the surprise.
    """
    if depth > 12 or node is None:
        return node
    if isinstance(node, dict):
        return {k: _config_clean(v, depth + 1) for k, v in sorted(node.items())
                if k not in CONFIG_NOISE_KEYS}
    if isinstance(node, list):
        return [_config_clean(v, depth + 1) for v in node]
    return node


def _override_flags(node: Any) -> List[str]:
    """
    Every `useVenueSettings: false` in a blob, by path.

    That flag is the one thing in this data that answers the question an
    install review actually asks: is this AP or group running what the venue
    says, or something of its own? Collected here so the UI can lead with it
    instead of making someone open a tree to find out.
    """
    found: List[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                here = f"{path}.{key}" if path else key
                if key == "useVenueSettings" and child is False:
                    found.append(path or "root")
                else:
                    walk(child, here)
        elif isinstance(value, list):
            for item in value:
                walk(item, path)

    walk(node, "")
    return sorted(set(found))


def _config_rows(node: Any, endpoint: str, parts: Tuple[str, ...] = (),
                 depth: int = 0,
                 literals: Optional[set] = None) -> List[Dict[str, Any]]:
    """
    A settings blob flattened into labelled rows, ready to compare.

    The tab started as a raw tree, which showed everything and meant nothing —
    a reader had to know R1's field names to get anything out of it. Rows carry
    a readable label, formatted value, and the path a baseline is keyed on, so
    the same data becomes a table with an "is this right?" column beside it.

    LISTS OF PER-MODEL OBJECTS keep the model as the path segment rather than
    an index: `R550.lanPorts[0].type` survives R1 reordering the list, while
    `[3].lanPorts[0].type` silently starts describing a different AP model.
    Anything else keyed by `id` or `name` gets the same treatment for the same
    reason; a bare index is the last resort.

    THE PATH IS A TUPLE, joined only for display. R1 ships keys with dots in
    them — `2.4GChannels` — so splitting a joined string back apart produced
    a "2" section and a "4GChannels" section. Segments that came from DATA
    rather than from a key (a model name, a profile name) are recorded in
    `literals` so the grouping does not try to de-camelCase them into
    "Radius2 32744".
    """
    if depth > 8 or node is None:
        return []
    if literals is None:
        literals = set()

    rows: List[Dict[str, Any]] = []

    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("_"):
                continue
            here = parts + (str(key),)
            # A list of SCALARS is one setting with several values —
            # `allowedChannels: [1, 6, 11]` is the channel plan, not three
            # separate settings. Recursing into it produced 282 rows for the
            # radio card, most of them a channel number labelled "Allowed
            # channels". format_value joins them instead.
            structured = isinstance(value, (dict, list)) and value and not (
                isinstance(value, list)
                and all(not isinstance(v, (dict, list)) for v in value))
            if structured:
                rows.extend(_config_rows(value, endpoint, here, depth + 1, literals))
                continue
            rows.append(_config_row(endpoint, here, key, value))
        return rows

    if isinstance(node, list):
        for index, item in enumerate(node):
            if isinstance(item, dict):
                # A stable name beats a positional index — see the docstring.
                anchor = (item.get("model") or item.get("name")
                          or item.get("id") or item.get("portId"))
                segment = str(anchor) if anchor else str(index)
                if anchor:
                    literals.add(segment)
                rows.extend(_config_rows(item, endpoint, parts + (segment,),
                                         depth + 1, literals))
            else:
                rows.append(_config_row(endpoint, parts + (f"[{index}]",),
                                        parts[-1] if parts else "", item))
        return rows

    return rows


# A category with fewer rows than this is easier to read flat than split into
# named sections that each hold two lines.
_GROUP_MIN_ROWS = 6
_GROUP_MAX_DEPTH = 2


def _group_config_rows(rows: List[Dict[str, Any]], depth: int = 0,
                       literals: Optional[set] = None
                       ) -> Optional[List[Dict[str, Any]]]:
    """
    Split a category's rows into collapsible sub-sections by path prefix.

    Purely structural — it groups on the shape of the data rather than a
    hand-written list of which settings belong together, so the split survives
    RUCKUS adding a band, a model or a profile. `radioParams24G` /
    `radioParams50G` / `radioParams6G` become 2.4 GHz, 5 GHz and 6 GHz; a
    per-model LAN port list becomes one section per model; `floorPlans` becomes
    Floor plans.

    Returns None when grouping would not help — a short category, or one where
    every prefix holds a single row — because a page of one-line sections is
    worse than a plain list.

    Rows whose path has no prefix at all (a scalar at the root, like the
    venue's `name`) collect into a leading "General" section rather than
    becoming a section each.
    """
    if depth >= _GROUP_MAX_DEPTH or len(rows) < _GROUP_MIN_ROWS:
        return None

    buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    general: List[Dict[str, Any]] = []
    for row in rows:
        parts = row.get("parts") or []
        if len(parts) <= depth + 1:
            general.append(row)
        else:
            buckets[parts[depth]].append(row)

    # A prefix holding one row is not a section; fold it back in.
    for key in [k for k, v in buckets.items() if len(v) < 2]:
        general.extend(buckets.pop(key))

    # A level that holds EVERYTHING says nothing — `apModelCapabilities` wraps
    # its whole payload in one `apModels` key, which as a section heading is
    # just the category's name again. Skip past it to the level that varies.
    if len(buckets) == 1 and not general:
        return _group_config_rows(rows, depth + 1, literals)

    if len(buckets) < 2:
        return None

    groups: List[Dict[str, Any]] = []
    if general:
        groups.append({"key": "_general", "label": "General",
                       "rows": general, "groups": None})
    literals = literals or set()
    for key, members in buckets.items():
        groups.append({
            "key": key,
            # A segment that came from the DATA is a name, not a key: a model
            # or a RADIUS profile. De-camelCasing it produced headings like
            # "Radius2 32744".
            "label": key if key in literals else config_labels.label_for(key),
            "rows": members,
            # One more level, so wifiSettings.radioCustomization splits into
            # its per-band params rather than staying one long section.
            "groups": _group_config_rows(members, depth + 1, literals),
        })
    return groups


def _config_row(endpoint: str, parts: Tuple[str, ...], key: str,
                value: Any) -> Dict[str, Any]:
    """
    One setting, with whatever the baselines say about it.

    The baseline key is `<endpoint>.<dotted path>` — the R1 path, not the
    label. Labels are prose and change; a baseline keyed to prose drifts
    silently, which for a "recommended value" column means quietly comparing
    against nothing.
    """
    path = ".".join(parts)
    baseline_key = f"{endpoint}.{path}"
    recommendations = baselines.lookup(baseline_key)

    row: Dict[str, Any] = {
        "path": path,
        # The un-joined form, so grouping never has to split a string that may
        # contain a dot of its own.
        "parts": list(parts),
        "key": key,
        "label": config_labels.label_for(key),
        "value": value,
        "valueText": config_labels.format_value(key, value),
        "baselineKey": baseline_key,
    }
    for who in ("org", "ruckus"):
        if who not in recommendations:
            continue
        expected = recommendations[who]
        if expected is baselines.NOT_APPLICABLE:
            # Reviewed, deliberately no recommendation (org only). A "—" cell
            # with NO `matches` key, so the row is shown as intentionally blank
            # and the mismatch tallies skip it — different from a value that
            # happens to equal "—".
            row[who] = {"notApplicable": True, "text": "—"}
            continue
        row[who] = {
            "value": expected,
            "text": config_labels.format_value(key, expected),
            # Compared on the raw value, not the formatted text: "Enabled" and
            # "enabled" would match by accident, and 1800 and "1800" would not.
            "matches": value == expected,
        }
    return row


def config_card(venue_raw: Dict[str, Any], venue_config: Dict[str, Any],
                radio: Any, mesh: Any, mgmt_vlan: Any,
                groups: List[Dict[str, Any]], ap_total: int,
                radius_profiles: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Everything R1 will say about how this venue is configured, in three levels.

    ORGANISED BY WHERE IT CAME FROM, not by what it means. Each category is one
    R1 endpoint, which sounds like a leaky abstraction and is deliberate: a
    settings dump has no natural taxonomy, R1's own console groups these
    differently again, and inventing a third grouping would mean a reader
    cannot map what they see here onto either. It also makes the categories a
    unit an admin can hide, because each one is a thing that was pulled.

    Nothing is interpreted. The tree is what R1 returned, minus the identifiers
    it echoes into every response — including fields nobody recognises, because
    the point of a config view is to show what is set rather than what someone
    thought worth keeping.

    VENUE LEVEL ONLY. Group and per-AP config are fetched on demand by
    `/pisr/{cid}/config/detail`, not here, because both are one call per object:
    a 200-unit MDU has a per-unit AP group each, so eagerly loading them would
    put ~600 requests behind every report — most of them for a tab nobody
    opened. This returns the counts so the tab can offer the button and say
    what it would cost.
    """
    categories = []

    # camelCase key -> the kebab slug the visibility catalogue uses. Emitted
    # so the frontend reads `config.<slug>` straight off the payload instead of
    # carrying its own copy of this mapping — one more list to drift.
    SLUGS = {"venue": "venue", "radio": "radio", "mesh": "mesh",
             "mgmtVlan": "mgmt-vlan", "loadBalancing": "load-balancing",
             "availableChannels": "available-channels",
             "clientAdmission": "client-admission",
             "directedMulticast": "directed-multicast",
             "smartMonitor": "smart-monitor", "bandMode": "band-mode",
             "lanPorts": "lan-ports", "modelCapabilities": "model-capabilities",
             "models": "models", "aaa": "aaa",
             "dosProtection": "dos-protection", "rogueAp": "rogue-ap",
             "syslog": "syslog", "snmp": "snmp", "led": "led",
             "bssColoring": "bss-coloring", "cellular": "cellular",
             "radiusProfiles": "radius-profiles",
             "antennaType": "antenna-type", "externalAntenna": "external-antenna",
             "mdnsFencing": "mdns-fencing", "radiusOptions": "radius-options",
             "rebootTimeout": "reboot-timeout", "tlsKey": "tls-key",
             "usbPorts": "usb-ports", "roguePolicy": "rogue-policy",
             "wifiSettings": "wifi-settings",
             "regulatoryChannels": "regulatory-channels",
             "syslogProfile": "syslog-profile",
             "dhcpServiceProfile": "dhcp-service-profile",
             "trustedPorts": "trusted-ports"}

    # The R1 endpoint each category came from, for keying baselines on.
    ENDPOINTS = dict(fetch_module.VENUE_CONFIG_SOURCES)
    ENDPOINTS.update({"venue": "venue", "radio": "apRadioSettings",
                      "mesh": "apMeshSettings",
                      "mgmtVlan": "apManagementTrafficVlanSettings",
                      "radiusProfiles": "radiusServerProfiles"})

    def add(key: str, label: str, source: str, payload: Any, hint: str = "") -> None:
        endpoint = ENDPOINTS.get(key, key)
        # `literals` collects the segments that came from data rather than
        # from a key — model names, profile names — so the grouping below can
        # print them verbatim instead of de-camelCasing them.
        literals: set = set()
        rows = (_config_rows(_config_clean(payload), endpoint, literals=literals)
                if payload is not None else [])
        categories.append({
            "key": key, "slug": SLUGS.get(key, key), "endpoint": endpoint,
            "rows": rows,
            "groups": _group_config_rows(rows, literals=literals),
            "label": label, "source": source, "hint": hint,
            # None means R1 did not answer, which is different from {} meaning
            # it answered with nothing configured. The UI says so.
            "unavailable": payload is None,
            "data": _config_clean(payload) if payload is not None else None,
        })

    # floorPlans is replaced with the shaped summary — the raw form is eight
    # coordinate numbers per plan, which answers nothing a reader is asking.
    venue_for_config = dict(venue_raw)
    venue_for_config["floorPlans"] = floorplan_summary(venue_raw)
    add("venue", "Venue", "GET /venues/{id}", _config_clean(venue_for_config),
        "The venue object itself, including its floor plans and whether a "
        "scale has been set on them.")
    # Second, immediately under the venue: this is R1's own aggregate of the
    # whole Wi-Fi configuration, so it reads as the summary and everything
    # below it as the detail. Added here rather than in the loop below purely
    # to get it into that position.
    add("wifiSettings", "Venue Wi-Fi settings",
        "GET /venues/{id}/wifiSettings", venue_config.get("wifiSettings"),
        "RUCKUS ONE's own aggregate of the venue's Wi-Fi configuration. Much "
        "of it repeats the individual sections below, which is why it leads.")
    add("radio", "Radio", "GET /venues/{id}/apRadioSettings", radio,
        "Per-band channel method, width, power and the permitted channel list.")
    add("mesh", "Mesh", "GET /venues/{id}/apMeshSettings", mesh)
    add("mgmtVlan", "AP management VLAN",
        "GET /venues/{id}/apManagementTrafficVlanSettings",
        {"managementVlan": mgmt_vlan} if mgmt_vlan is not None else None)
    # ORDERED BY WHAT A READER IS LOOKING FOR, not by endpoint name.
    #
    # `wifiSettings` sits near the top because it is R1's own aggregate — it
    # repeats much of what the individual endpoints below return, so it reads
    # as the summary and they read as the detail. Then radio and spectrum,
    # then per-model hardware, then the operational services, then
    # authentication, logging and the rest.
    #
    # Related endpoints are adjacent rather than merged: antenna type and
    # external antenna are two endpoints and stay two sections, so an admin can
    # still hide one without the other. Adjacency gets the reading benefit
    # without coarsening the control.
    for key, label, hint in (
        # radio and spectrum
        ("availableChannels", "Available channels",
         "What the regulatory domain and AFC permit, per band and width."),
        ("regulatoryChannels", "Default regulatory channels",
         "What the regulatory domain permits, before the venue narrows it."),
        ("loadBalancing", "Load balancing & steering",
         "Band balancing, client steering, sticky-client thresholds."),
        ("clientAdmission", "Client admission control",
         "Minimum client counts and radio load before admission is refused."),
        ("bandMode", "Model band mode", "Per AP model, where set."),

        # per-model hardware
        ("antennaType", "Antenna type", "Per AP model — sector or omni, where "
         "the model offers a choice. Reads with External antenna below."),
        ("externalAntenna", "External antenna", "Per AP model — per-band enable "
         "and gain. Mostly an outdoor concern."),
        ("modelCapabilities", "AP model capabilities",
         "What each model supports. Antenna defaults live in here too — there "
         "is no separate endpoint for them."),
        ("lanPorts", "LAN port settings",
         "Per AP MODEL — the venue default each AP inherits. A per-AP "
         "`lanPorts` override shows up in the on-demand detail below."),
        ("usbPorts", "USB ports", "Per AP model."),
        ("led", "AP LEDs", "Per AP model."),
        ("cellular", "Cellular", "Only meaningful on a cellular-capable model."),

        # services on the air
        ("bssColoring", "BSS coloring", ""),
        ("directedMulticast", "Directed multicast", ""),
        ("mdnsFencing", "mDNS fencing", ""),
        ("smartMonitor", "Smart monitor", ""),

        # security
        ("dosProtection", "DoS protection", ""),
        ("rogueAp", "Rogue AP detection", "Whether to look for rogues."),
        ("roguePolicy", "Rogue policy", "What to do about one."),
        ("tlsKey", "TLS enhanced key", ""),

        # authentication — AAA and the two RADIUS sections read together
        ("aaa", "AAA / CLI authentication",
         "Console and SSH authentication order, and command accounting."),
        ("radiusOptions", "RADIUS options",
         "Called-station and NAS id formats sent with RADIUS requests."),
        ("_radiusProfiles", "RADIUS server profiles", ""),

        # logging and management
        ("syslog", "Syslog", ""),
        ("syslogProfile", "Syslog server profile", ""),
        ("snmp", "AP SNMP agent", ""),
        ("dhcpServiceProfile", "DHCP service profile", ""),
        ("rebootTimeout", "Auto-reboot timers",
         "How long an AP tolerates losing its gateway or the cloud before "
         "restarting itself."),
        ("trustedPorts", "Trusted ports", ""),
        ("models", "AP models in use", ""),
    ):
        if key == "_radiusProfiles":
            # Tenant-wide rather than venue-scoped, so it is fetched
            # separately — but it belongs next to the RADIUS options a reader
            # is comparing it against, not stranded at the end of the tab.
            add("radiusProfiles", label, "GET /radiusServerProfiles",
                radius_profiles,
                "Tenant-wide, not scoped to this venue. Shared secrets are "
                "redacted before the report leaves the process.")
            continue
        add(key, label,
            f"GET /venues/{{id}}/{fetch_source(key)}", venue_config.get(key), hint)

    # Tenant-wide, not venue-scoped: R1 offers no venue filter on this one, so
    # it is every profile on the EC. Said in the hint rather than implied, or a
    # reader takes it for this venue's RADIUS configuration.
    #
    # Shared secrets come back in plaintext and api/scrub.py removes them from
    # every report — see the test written against this exact shape.


    # R1 returns the per-model blocks for EVERY model it knows — fifty of them
    # on this tenant, of which three are installed. Left raw, the LAN port
    # settings a crew actually needs are three rows lost in fifty. So models
    # present at the venue are marked and sorted to the front; nothing is
    # dropped, because a venue-level default for a model not yet installed is
    # still the setting the next AP will inherit.
    present = {str(m).lower() for m in
               ((venue_config.get("models") or {}).get("models") or [])}
    if present:
        for category in categories:
            if category["key"] not in ("lanPorts", "led", "usbPorts",
                                       "antennaType", "externalAntenna"):
                continue
            rows = category.get("data")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    row["_atThisVenue"] = str(row.get("model", "")).lower() in present
            category["data"] = sorted(
                rows, key=lambda r: (not (isinstance(r, dict) and r.get("_atThisVenue")),
                                     str(isinstance(r, dict) and r.get("model") or "")))
            category["presentCount"] = sum(
                1 for r in rows if isinstance(r, dict) and r.get("_atThisVenue"))
            category["totalCount"] = len(rows)
            # Rebuilt from the sorted data: `rows` was flattened when the
            # category was added, before the models present here were floated
            # to the front, so without this the table still leads with fifty
            # models nobody has installed.
            model_literals: set = set()
            category["rows"] = _config_rows(category["data"], category["endpoint"],
                                            literals=model_literals)
            category["groups"] = _group_config_rows(category["rows"],
                                                    literals=model_literals)

    return {
        "categories": categories,
        # Whose recommendations the two comparison columns carry, and whether
        # either has been verified. The UI captions an unverified column.
        "baselines": baselines.describe(),
        # Recorded so the tab can say what it looked for and did not find,
        # rather than leaving a reader to wonder whether the setting is absent
        # or the report simply never asked. See fetch.VENUE_CONFIG_NOT_FOUND.
        "notAvailable": list(FETCH_NOT_FOUND),
        # What the on-demand call would cost, so the button can say it rather
        # than the user discovering it.
        "groupTotal": len(groups),
        "apTotal": ap_total,
        "detailCalls": len(groups) * (1 + len(AP_GROUP_SOURCE_COUNT)) + ap_total,
    }


# Kept as a name rather than a literal so the arithmetic above follows
# fetch.AP_GROUP_CONFIG_SOURCES if a source is added.
AP_GROUP_SOURCE_COUNT: Tuple[str, ...] = ("clientAdmission", "bandMode")


def config_detail(groups: List[Dict[str, Any]], group_config: Dict[str, Any],
                  ap_config: Dict[str, Any], ap_total: int) -> Dict[str, Any]:
    """
    Group and per-AP configuration, shaped for the on-demand call.

    Split from `config_card` because it costs one R1 request per group and per
    AP. The thing worth reading here is `overrides`: every `useVenueSettings:
    false` in the blob, which is the only field in this data that answers
    "is this running what the venue says, or something of its own".
    """
    group_rows = []
    for group in groups:
        gid = group.get("id")
        entry = group_config.get(gid) or {}
        blob = {k: v for k, v in entry.items() if v is not None}
        detail = entry.get("detail") or {}
        group_rows.append({
            "id": gid,
            "name": group.get("name") or ("Default" if detail.get("isDefault") else gid),
            "apCount": len(detail.get("apSerialNumbers") or []),
            "isDefault": bool(detail.get("isDefault")),
            "isEnforced": bool(detail.get("isEnforced")),
            "overrides": _override_flags(blob),
            "data": _config_clean(blob),
        })

    ap_rows = []
    for serial, blob in sorted(ap_config.items()):
        if not blob:
            continue
        overrides = _override_flags(blob)
        ap_rows.append({
            "serial": serial,
            "name": blob.get("name") or serial,
            "model": blob.get("model"),
            "apGroupId": blob.get("apGroupId"),
            # An AP with nothing here runs exactly what the venue says, which
            # is what most of them should say.
            "overrides": overrides,
            "overridden": bool(overrides),
            "data": _config_clean(blob),
        })

    return {
        "groups": group_rows,
        "aps": ap_rows,
        "apOverrideCount": sum(1 for row in ap_rows if row["overridden"]),
        "groupOverrideCount": sum(1 for row in group_rows if row["overrides"]),
        # Said out loud rather than implied: "no overrides" on a partial list
        # is a different statement from "no overrides".
        "apTotal": ap_total,
        "apShown": len(ap_rows),
        "apTruncated": ap_total > len(ap_rows),
    }


from services.pisr import fetch as fetch_module  # noqa: E402
from services.pisr.fetch import VENUE_CONFIG_NOT_FOUND as FETCH_NOT_FOUND  # noqa: E402


def fetch_source(key: str) -> str:
    """The R1 path fragment a venue-config category came from, for display."""
    from services.pisr.fetch import VENUE_CONFIG_SOURCES
    return VENUE_CONFIG_SOURCES.get(key, key)


def wired_client_card(mac_rows: List[Dict[str, Any]], ports: List[Dict[str, Any]],
                      aps: List[Dict[str, Any]],
                      switches: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    What is plugged into the switches, from the MAC address table.

    THE COUNTING IS THE WHOLE PROBLEM. R1 hands back one row per address a
    switch has learned, and most of them are not a wired client:

      * an AP's uplink port has learned every wireless client behind it, so a
        busy AP contributes dozens of rows that are already counted on the
        Wireless tab;
      * the APs and switches are in the table as addresses in their own right;
      * a port feeding another switch or a router has learned everything
        behind it, which is a real fact about the port and not a client on it.

    Reporting `len(mac_rows)` as "wired clients" would therefore be several
    times the number of things actually plugged into a wall, and would move
    when someone joined the Wi-Fi. So rows are classified, and the ones set
    aside are counted out loud rather than silently dropped — a number that
    quietly excludes things is worse than one that explains itself.

    The first two classes are separable with data the report already holds: the
    AP-to-port join comes from LLDP the same way `poe_card` builds it, and the
    infrastructure addresses are the APs' and switches' own MACs. The third —
    a downstream switch nobody manages — is NOT separable, and is left in. It
    shows up as a port with an implausible number of addresses on it, which is
    what `topPorts` is for: on a clean install every port has one or two, and a
    port with fifteen is either a hub, an unmanaged switch, or a mislabelled
    uplink.
    """
    ap_macs = {_mac(ap.get("mac")) for ap in aps if ap.get("mac")}
    switch_macs = {_mac(sw.get("switchMac") or sw.get("id"))
                   for sw in switches if sw.get("switchMac") or sw.get("id")}
    infra_macs = {m for m in (ap_macs | switch_macs) if m}

    # Ports with an AP on the far end, keyed the way the MAC rows identify a
    # port. `switchPortId` is R1's own composite ("<switch mac>_<port>") and is
    # the only field the two DTOs share exactly, so the key is rebuilt from the
    # parts rather than parsed out of it.
    ap_names = {_norm(ap.get("name")) for ap in aps if ap.get("name")}
    ap_port_keys = set()
    for port in ports:
        neighbour_mac = _mac(port.get("neighborMacAddress")
                             or port.get("neighborPortMacAddress"))
        neighbour_name = _norm(port.get("neighborName"))
        # LLDP often appends a radio suffix to the AP's name, the same quirk
        # poe_card works around.
        is_ap = (neighbour_mac in ap_macs if neighbour_mac else False) or (
            neighbour_name in ap_names
            or neighbour_name.rsplit(".", 1)[0] in ap_names if neighbour_name else False)
        if is_ap:
            ap_port_keys.add((_mac(port.get("switchMac")),
                              _norm(port.get("portIdentifier"))))

    wired, behind_aps, infrastructure = [], 0, 0
    for row in mac_rows:
        if _mac(row.get("clientMac")) in infra_macs:
            infrastructure += 1
            continue
        if (_mac(row.get("switchMac")), _norm(row.get("switchPort"))) in ap_port_keys:
            behind_aps += 1
            continue
        wired.append(row)

    # VLAN label carries the name where R1 has one — "12 (VOICE)" reads far
    # better than "12" on a site where the numbers mean nothing to the reader.
    def vlan_label(row: Dict[str, Any]) -> str:
        vlan = row.get("clientVlan")
        if vlan in (None, ""):
            return "Unknown"
        name = (row.get("vlanName") or "").strip()
        return f"{vlan} ({name})" if name else str(vlan)

    port_counts = Counter(
        f'{row.get("switchName") or "?"} · {row.get("switchPort") or "?"}'
        for row in wired)

    return {
        "total": len(wired),
        # Every row R1 returned, so the three numbers below add up in public.
        "learned": len(mac_rows),
        "behindAps": behind_aps,
        "infrastructure": infrastructure,
        "switchCount": len({_mac(row.get("switchMac")) for row in wired}),
        "portsInUse": len(port_counts),
        # R1 populates clientIpv4Addr from its own DHCP/ARP snooping and its
        # coverage runs anywhere from a third to most of the table, so the
        # count is reported rather than the absence being read as "no IP".
        "withIp": sum(1 for row in wired if (row.get("clientIpv4Addr") or "").strip()),
        "withName": sum(1 for row in wired
                        if (row.get("clientName") or row.get("dhcpClientHostName") or "").strip()),
        "bySwitch": _tally(wired, "switchName", unknown="Unknown switch"),
        "byVlan": [{"label": label, "count": count}
                   for label, count in sorted(Counter(vlan_label(r) for r in wired).items(),
                                              key=lambda kv: (-kv[1], kv[0]))],
        "byType": _tally(wired, "clientType", unknown="Unclassified"),
        # Capped: this is a summary, and a site with three hundred ports should
        # not push everything else off the card.
        "topPorts": [{"label": label, "count": count}
                     for label, count in port_counts.most_common(10)],
    }


# ── wireless ─────────────────────────────────────────────────

def wireless_card(networks: List[Dict[str, Any]], activations: List[Dict[str, Any]],
                  groups: List[Dict[str, Any]], aps: List[Dict[str, Any]],
                  clients: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Every SSID activated on this venue, joined to what the APs are beaconing and
    the clients riding it.

    Three independent statements per SSID, deliberately kept apart: it is
    *configured* here, N APs are *broadcasting* it, and M clients are *on* it.
    Config alone proves nothing; a beacon proves it reached the air; a client
    proves it works end to end.
    """
    by_id = {n.get("id"): n for n in networks}
    group_names = {g.get("id"): g.get("name") for g in groups}
    default_group_ids = {g.get("id") for g in groups if g.get("isDefault")}

    def group_label(gid: Optional[str]) -> str:
        """
        A printable name for an AP group, never None and never a bare GUID.

        R1 leaves the DEFAULT AP group's `name` null — every unnamed group
        observed across three tenants had `isDefault: true` — and the console
        renders it as "Default". Falling through to the id put a 32-character
        hex string in the SSID table where a name belongs.

        (`group_names.get(gid, gid)` was the original bug here: a null name is
        stored against a real key, so `.get` returns that None rather than the
        default, which then reached a `", ".join(...)` and took a whole check
        out with "sequence item 0: expected str instance, NoneType found".)
        """
        name = group_names.get(gid)
        if name:
            return name
        if gid in default_group_ids:
            return "Default"
        if gid:
            # Not a group this venue knows about at all: say so, and keep a
            # short prefix so it can still be traced.
            return f"Unknown group ({str(gid)[:8]}…)"
        return "Unnamed AP group"

    # Count APs per group by ID, and ALSO by group name as a fallback. The
    # activation names a group by id and the AP reports both id and name; if
    # the ids ever fail to line up — a stale id on one side, a tenant where the
    # AP query omits it — an id-only join silently returns zero for every group
    # and a per-unit site reports all 300 of its groups as empty. Falling back
    # to the name means one broken key cannot produce a page of false findings.
    group_ap_counts = Counter(ap["apGroupId"] for ap in aps if ap["apGroupId"])
    online_by_group = Counter(ap["apGroupId"] for ap in aps
                              if ap["apGroupId"] and ap["state"] == "online")
    by_name = Counter(_norm(ap["apGroup"]) for ap in aps if ap.get("apGroup"))
    online_by_name = Counter(_norm(ap["apGroup"]) for ap in aps
                             if ap.get("apGroup") and ap["state"] == "online")

    def aps_in_group(gid: Optional[str], online_only: bool = False) -> int:
        counts = online_by_group if online_only else group_ap_counts
        names = online_by_name if online_only else by_name
        direct = counts.get(gid, 0)
        if direct:
            return direct
        return names.get(_norm(group_names.get(gid)), 0)

    # How well the AP -> group join actually worked, so a check can tell
    # "these groups are empty" apart from "nothing resolved".
    aps_with_group = sum(1 for ap in aps if ap["apGroupId"] or ap.get("apGroup"))

    clients_by_ssid = Counter(c["ssid"] for c in clients if c["ssid"])
    aps_by_ssid = defaultdict(set)
    for client in clients:
        if client["ssid"] and client["apName"]:
            aps_by_ssid[client["ssid"]].add(client["apName"])

    # What the radios say they are beaconing, by lowercased name. Only online
    # APs count: an offline AP's radio state is whatever it was when it left.
    broadcasting = Counter()
    for ap in aps:
        if ap["state"] != "online":
            continue
        for name in set(_norm(entry) for entry in ap["ssidsBroadcast"]):
            broadcasting[name] += 1

    rows = []
    unresolved = 0
    for activation in activations:
        network_id = activation.get("networkId")
        network = by_id.get(network_id) or {}
        # Whether the activation joined to a network definition at all. When it
        # does not there is no SSID, no security and no type to show, and the
        # id is the only handle we have — so say that plainly rather than
        # printing a GUID in the SSID column and letting it read as a name.
        resolved = bool(network)
        if not resolved:
            unresolved += 1
        ssid = network.get("ssid") or network.get("name") or network_id
        all_groups = bool(activation.get("isAllApGroups"))

        vlans, radios, scopes = [], set(), []
        group_scopes = activation.get("apGroups") or []

        # VLAN and radio settings are carried on the per-group entries even for
        # a venue-wide activation, so harvest them from every entry regardless
        # of how the scope is finally expressed.
        for group in group_scopes:
            if group.get("vlanId") is not None:
                vlans.append(int(_num(group.get("vlanId"), 1)))
            radios.update(group.get("radioTypes") or [])

        # An `isAllApGroups` activation is on the venue, not on a list of
        # groups — but R1 ALSO enumerates every AP group in `apGroups` as a
        # formality. Treating those entries as individually targeted scopes
        # turned three venue-wide SSIDs into 303 scopes on a 203-group
        # property, and every group that happened to be empty was then
        # reported as an SSID pointing at nothing. It is one scope: all of them.
        if all_groups:
            scopes.append({"group": "All AP groups", "groupId": None,
                           "aps": len(aps),
                           "onlineAps": sum(1 for ap in aps if ap["state"] == "online"),
                           "vlan": activation.get("allApGroupsVlanId"),
                           "radios": sorted(radios) or (
                               activation.get("allApGroupsRadioTypes") or [])})
            radios.update(activation.get("allApGroupsRadioTypes") or [])
            group_scopes = []

        for group in group_scopes:
            gid = group.get("apGroupId")
            scopes.append({"group": group_label(gid),
                           # The id, because a name alone is not enough to
                           # identify a group: a venue can hold two groups whose
                           # names differ only by a suffix, one populated and one
                           # not, and a finding that names only "1-1001" is
                           # indistinguishable from "1-1001@The_ross".
                           "groupId": gid,
                           "aps": aps_in_group(gid),
                           "onlineAps": aps_in_group(gid, online_only=True),
                           "vlan": group.get("vlanId"),
                           "radios": group.get("radioTypes") or []})
        if not scopes:
            # Venue-wide activation with no per-group breakdown.
            scopes.append({"group": "All AP groups", "aps": len(aps),
                           "onlineAps": sum(1 for ap in aps if ap["state"] == "online"),
                           "vlan": activation.get("allApGroupsVlanId"),
                           "radios": activation.get("allApGroupsRadioTypes") or []})
            radios.update(activation.get("allApGroupsRadioTypes") or [])
        if activation.get("allApGroupsVlanId") is not None:
            vlans.append(int(_num(activation.get("allApGroupsVlanId"), 1)))
        if not vlans and network.get("vlan") is not None:
            vlans.append(int(_num(network.get("vlan"), 1)))

        names = {_norm(ssid), _norm(network.get("name"))}
        beaconing = max((broadcasting.get(name, 0) for name in names if name), default=0)

        rows.append({
            "networkId": network_id,
            "resolved": resolved,
            "name": network.get("name"),
            "ssid": ssid,
            "security": network.get("securityProtocol"),
            "type": network.get("nwSubType") or network.get("nwType"),
            "captive": network.get("captiveType"),
            "vlans": sorted(set(vlans)),
            "radios": sorted(radios),
            "allApGroups": all_groups,
            "scopes": scopes,
            "scheduled": bool(activation.get("scheduler")),
            "enforced": bool(activation.get("isEnforced")),
            "apsBroadcasting": beaconing,
            "clientsNow": clients_by_ssid.get(ssid, 0),
            "apsCarrying": len(aps_by_ssid.get(ssid, ())),
            "tenantClientCount": int(_num(network.get("clientCount"))),
        })
    rows.sort(key=lambda row: (-row["clientsNow"], -row["apsBroadcasting"], str(row["ssid"])))

    # R1 caps an AP group at 15 SSIDs; count what this venue puts on each.
    #
    # A venue-wide activation is ONE scope but lands on EVERY AP group, and it
    # consumes a slot on each — a unit group carrying one per-unit SSID plus
    # three property-wide ones is at four, not one. Counting only the named
    # scopes would understate every group by the number of venue-wide SSIDs
    # and hide a group approaching the ceiling.
    venue_wide = sum(1 for row in rows if row["allApGroups"])
    per_group = Counter()
    for row in rows:
        if row["allApGroups"]:
            continue
        for scope in row["scopes"]:
            per_group[scope["group"]] += 1
    if venue_wide:
        for group in groups:
            per_group[group_label(group.get("id"))] += venue_wide
        if not groups:
            per_group["All AP groups"] += venue_wide

    return {
        "activated": len(rows),
        "definedOnTenant": len(networks),
        # Activations whose network definition was not in the tenant list. Any
        # non-zero value means the SSID/security/type columns are blank for
        # those rows, so it is surfaced rather than left to look like bad data.
        "unresolved": unresolved,
        "rows": rows,
        "perApGroup": [{"label": str(group), "count": n} for group, n in per_group.most_common()],
        "groups": [{"id": g.get("id"), "name": g.get("name"),
                    "aps": aps_in_group(g.get("id")),
                    "onlineAps": aps_in_group(g.get("id"), online_only=True),
                    "isDefault": bool(g.get("isDefault"))} for g in groups],
        # Join diagnostics — see aps_in_group.
        "apsWithGroup": aps_with_group,
        "apsTotal": len(aps),
    }


def radio_card(aps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    The channel plan as the APs report it — one entry per band, with the channels
    in use and how wide they are. Co-channel APs on a small site are an install
    fault you can only see laid out like this.

    `channels` and `widths` are tallied independently, which answers "which
    channels are in use" and "which widths are in use" but not "which channels
    at which width" — and on a band running more than one width those are
    different questions. A 40 MHz radio and a 20 MHz radio on the same channel
    number are not the same finding. `byWidth` carries the join so the UI can
    bucket them; the two flat tallies above it are untouched, because the
    spectrum chart and the checks read those and neither should change.

    A radio reporting a channel but no width gets its own bucket rather than
    being folded into 20 MHz. Guessing there would put radios on a row they may
    not be on, and the buckets are meant to sum to the band's radio count.
    """
    bands: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"channels": Counter(), "widths": Counter(), "power": Counter(),
                 # (width MHz, or 0 for not reported) -> Counter of channels
                 "byWidth": defaultdict(Counter)})
    radios_seen = 0
    for ap in aps:
        if ap["state"] != "online":
            continue
        for radio in ap["radios"]:
            band = radio.get("band") or "unknown"
            radios_seen += 1
            # Digits only, and NOT _width_mhz — that helper falls back to 20
            # for anything unparseable, which is right for placing a block on
            # the spectrum chart and wrong here, where it would quietly file
            # unknown-width radios under 20 MHz. 0 means the radio reported no
            # width we can read.
            digits = "".join(c for c in str(radio.get("width") or "") if c.isdigit())
            width_mhz = int(digits) if digits else 0

            if radio.get("channel") is not None:
                bands[band]["channels"][str(radio["channel"])] += 1
                bands[band]["byWidth"][width_mhz][str(radio["channel"])] += 1
            if radio.get("width"):
                # Built from the digits rather than interpolated around the raw
                # value. R1's channelBandwidth has appeared as a bare 20 and as
                # "20MHz", and the old `f"{width} MHz"` turned the second into
                # "20MHz MHz" in the widths line under this card. Where R1 does
                # send a bare number this produces the identical string, so the
                # change is only ever visible where it was already wrong.
                # Anything with no digits at all — "AUTO" — is passed through
                # as itself rather than relabelled 0 MHz.
                bands[band]["widths"][
                    f"{width_mhz} MHz" if width_mhz else str(radio["width"])] += 1
            if radio.get("power"):
                bands[band]["power"][str(radio["power"])] += 1

    def _channel_rows(counter: Counter) -> List[Dict[str, Any]]:
        """Busiest first, then by channel NUMBER — the same order as `channels`."""
        return [{"label": channel, "count": n}
                for channel, n in sorted(counter.items(),
                                         key=lambda kv: (-kv[1], _as_int(kv[0])))]

    return {
        "radiosSeen": radios_seen,
        "bands": [{
            "band": band,
            "radios": sum(data["channels"].values()),
            # Busiest first, then by channel NUMBER — a string tiebreak put
            # channel 11 ahead of 6, which is visible now the list is printed
            # in full rather than cut off at the top eight.
            "channels": _channel_rows(data["channels"]),
            # Narrowest first, so the buckets read like a channel chart. The
            # unknown-width bucket sorts to the end rather than to the front,
            # where a 0 would put it: it is the least interesting row and it
            # should not be the first thing read.
            "byWidth": [{
                "width": width or None,
                "label": f"{width} MHz" if width else "width not reported",
                "radios": sum(channels.values()),
                "channels": _channel_rows(channels),
            } for width, channels in sorted(data["byWidth"].items(),
                                            key=lambda kv: (kv[0] == 0, kv[0]))],
            "widths": [{"label": width, "count": n} for width, n in data["widths"].most_common()],
            "power": [{"label": power, "count": n} for power, n in data["power"].most_common()],
        } for band, data in sorted(bands.items())],
    }


def client_card(clients: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Who is on the network right now — the only live proof the install carries traffic."""
    rssi_buckets = Counter()
    for client in clients:
        rssi = _num(client["rssi"], 0)
        if not rssi:
            continue
        if rssi >= -60:
            rssi_buckets["Strong (≥ -60)"] += 1
        elif rssi >= -70:
            rssi_buckets["Good (-60 to -70)"] += 1
        elif rssi >= -80:
            rssi_buckets["Fair (-70 to -80)"] += 1
        else:
            rssi_buckets["Weak (< -80)"] += 1

    order = ["Strong (≥ -60)", "Good (-60 to -70)", "Fair (-70 to -80)", "Weak (< -80)"]
    return {
        "total": len(clients),
        "byBand": _tally(clients, "band"),
        "bySsid": _tally(clients, "ssid", unknown="(none)"),
        "byOs": _tally(clients, "os")[:8],
        "byHealth": _tally([c for c in clients if c["health"]], "health"),
        "topAps": _tally(clients, "apName")[:10],
        "byRssi": [{"label": bucket, "count": rssi_buckets[bucket]}
                   for bucket in order if rssi_buckets.get(bucket)],
        "capped": len(clients) >= 10000,
    }


# ── DPSK / identity ──────────────────────────────────────────

# Keys that must never appear in the DPSK section, checked case-insensitively
# against every dict this module emits. Kept as a hard gate rather than a
# convention: the source DTOs carry the passphrase itself, the resident's
# email, phone and username, and an embedded identities array, so a passthrough
# anywhere would leak credentials and PII into a shareable report.
DPSK_FORBIDDEN_KEYS = {
    "passphrase", "devicepassphrase", "email", "phonenumber", "phone",
    "username", "identities", "mac", "macaddress", "identityid",
    "identityname", "secret", "password",
}


def _dpsk_safe(payload: Any, path: str = "dpsk") -> Any:
    """
    Fail closed on anything sensitive.

    Every dict leaving the DPSK shaper goes through here. A forbidden key is a
    programming error — a new R1 field name folded into a summary by accident —
    so it raises rather than silently dropping, on the same reasoning as
    utils.icx_redact.assert_clean: a redactor that quietly succeeds teaches you
    nothing, and PISR would rather fail a section than publish a passphrase.
    """
    if isinstance(payload, dict):
        for key in payload:
            if str(key).strip().lower() in DPSK_FORBIDDEN_KEYS:
                raise ValueError(
                    f"pisr: refusing to emit sensitive DPSK key {key!r} at {path}"
                )
        return {k: _dpsk_safe(v, f"{path}.{k}") for k, v in payload.items()}
    if isinstance(payload, list):
        return [_dpsk_safe(v, f"{path}[]") for v in payload]
    return payload


def dpsk_card(pools: List[Dict[str, Any]],
              group_result: Dict[str, Any],
              activations: List[Dict[str, Any]],
              networks: List[Dict[str, Any]],
              venue_id: Optional[str],
              property_id: Optional[str],
              passphrase_counts: Dict[str, Optional[int]]) -> Dict[str, Any]:
    """
    Does this venue use DPSK, and if so, how is it set up?

    Scoping uses TWO links, because either alone misses real sites.

    1. **Activated networks.** A pool carries `networkIds`; intersect those
       with what this venue activates.
    2. **Property identity group.** An identity group's `propertyId` IS the
       venue id — not a separate property object id, which is what it looks
       like and what this code first assumed. `/venues/{id}/propertyConfigs`
       has no `id` field at all, so the old comparison was against None and
       never matched anything.

    Link 2 is not a nicety. A property can have DPSK fully configured — pool,
    identity group, residents — while no SSID is activated on the venue yet, or
    while the pool backs no network at all. On link 1 alone such a venue
    reported "DPSK not in use", which is the opposite of the truth.

    Counts only. Never a passphrase, never a resident. See _dpsk_safe.
    """
    # A pool's link to its identity group only ever exists on the GROUP
    # (`dpskPoolId`); R1 leaves the pool's own `identityGroupId` null. If the
    # group list came back short, "this pool has no identity group" is not a
    # fact we are entitled to state — see identityGroupsComplete below.
    groups = group_result.get("rows") or []
    groups_complete = bool(group_result.get("complete", True))

    activated_ids = {a.get("networkId") for a in activations if a.get("networkId")}
    ssid_by_id = {n.get("id"): (n.get("ssid") or n.get("name")) for n in networks}

    # Pools reached through an identity group belonging to THIS venue's
    # property. `propertyId` on the group holds the venue id.
    property_groups = [g for g in groups
                       if venue_id and g.get("propertyId") == venue_id]
    property_pool_ids = {g.get("dpskPoolId") for g in property_groups if g.get("dpskPoolId")}

    pool_rows = []
    for pool in pools:
        pool_id = pool.get("id")
        network_ids = set(pool.get("networkIds") or [])
        here = sorted(network_ids & activated_ids)
        by_property = pool_id in property_pool_ids
        if not here and not by_property:
            continue  # a pool this venue does not use is not this venue's business

        linked_by = []
        if here:
            linked_by.append("activated SSIDs")
        if by_property:
            linked_by.append("property identity group")

        passphrases = passphrase_counts.get(pool_id)
        linked_groups = [g for g in groups if g.get("dpskPoolId") == pool_id]
        identities = sum(int(_num(g.get("identityCount"))) for g in linked_groups)

        pool_rows.append({
            "id": pool_id,
            "name": pool.get("name"),
            # How a passphrase is generated — the recipe, not any passphrase.
            "passphraseFormat": pool.get("passphraseFormat"),
            "passphraseLength": pool.get("passphraseLength"),
            "wordCount": pool.get("wordCount"),
            "numericSuffix": bool(pool.get("numericSuffixEnabled")),
            "deviceLimitPerPassphrase": pool.get("deviceCountLimit"),
            "expirationType": pool.get("expirationType"),
            "expirationDate": pool.get("expirationDate"),
            "expirationOffset": pool.get("expirationOffset"),
            "autoNotifications": bool(pool.get("autoNotificationsEnabled")),
            "policyDefaultAccess": pool.get("policyDefaultAccess"),
            "hasPolicySet": bool(pool.get("policySetId")),
            "policySetId": pool.get("policySetId"),
            "passphraseCount": passphrases,
            "networksTotal": len(network_ids),
            "networksHere": len(here),
            "ssidsHere": sorted(
                name for name in (ssid_by_id.get(nid) for nid in here) if name
            ),
            "identityGroups": [
                {"id": g.get("id"), "name": g.get("name"),
                 "identityCount": int(_num(g.get("identityCount"))),
                 "networkCount": int(_num(g.get("networkCount"))),
                 "autoCleanup": bool(g.get("autoCleanupEnabled")),
                 "inactiveAfterDays": g.get("inactiveAfterDays"),
                 "isProperty": bool(g.get("propertyId")),
                 "hasPolicySet": bool(g.get("policySetId")),
                 "policySetId": g.get("policySetId"),
                 "hasCertificateTemplate": bool(g.get("certificateTemplateId"))}
                for g in linked_groups
            ],
            "identityCount": identities,
            # R1's OWN statement that an identity group references this pool.
            # A DPSK pool cannot be created without at least one identity
            # group, so this is true for every real pool — which is exactly why
            # PISR must not conclude "no identity group" from a failed join.
            "isReferenced": bool(pool.get("isReferenced")),
            # Which link brought this pool into the venue's scope. A pool
            # linked only by its property identity group backs no SSID here
            # yet — configured, not deployed.
            "linkedBy": linked_by,
            # Whether we could actually resolve that group's details. False
            # means the report is thin here, NOT that the group is missing:
            # /identityGroups/query under-returns (a live tenant with four
            # pools reports totalElements=3), and the pool->group link only
            # exists on the group, so an unlisted group is simply invisible.
            "identityGroupsResolved": bool(linked_groups),
        })
    pool_rows.sort(key=lambda row: (-row["networksHere"], str(row["name"] or "")))

    used_pool_ids = {row["id"] for row in pool_rows}
    # Groups attached to this property but to none of the pools above — an
    # identity group with no DPSK pool is a MAC-registration or certificate
    # group, and is worth showing rather than silently dropping.
    other_groups = [
        {"id": g.get("id"), "name": g.get("name"),
         "identityCount": int(_num(g.get("identityCount"))),
         "networkCount": int(_num(g.get("networkCount"))),
         "hasDpskPool": bool(g.get("dpskPoolId")),
         "hasMacRegistrationPool": bool(g.get("macRegistrationPoolId")),
         "hasCertificateTemplate": bool(g.get("certificateTemplateId")),
         "isProperty": bool(g.get("propertyId"))}
        for g in groups
        if g.get("dpskPoolId") not in used_pool_ids
        and venue_id and g.get("propertyId") == venue_id
    ]

    dpsk_ssids = [
        {"ssid": ssid_by_id.get(nid) or nid, "networkId": nid}
        for nid in sorted(activated_ids)
        if any(nid in set(p.get("networkIds") or []) for p in pools)
    ]

    return _dpsk_safe({
        "inUse": bool(pool_rows),
        "poolCount": len(pool_rows),
        "poolsOnTenant": len(pools),
        "identityGroupsOnTenant": len(groups),
        "identityGroupsComplete": groups_complete,
        "identityGroupsTotal": group_result.get("total"),
        "poolsWithUnresolvedGroups": sum(1 for row in pool_rows
                                         if not row["identityGroupsResolved"]),
        "passphraseTotal": sum(row["passphraseCount"] or 0 for row in pool_rows),
        "passphraseCountsKnown": all(row["passphraseCount"] is not None
                                     for row in pool_rows) if pool_rows else True,
        "identityTotal": sum(row["identityCount"] for row in pool_rows),
        "pools": pool_rows,
        "otherIdentityGroups": other_groups,
        "dpskSsids": dpsk_ssids,
    })


# ── adaptive policy / RADIUS attributes ──────────────────────

def _assignment_identity_ids(assignment: Dict[str, Any]) -> List[str]:
    """
    The pool ids an external assignment names.

    `identityId` is a LIST on this DTO — ["642753eb…"] — despite the singular
    name, and a bare string elsewhere in R1. Both are accepted so a shape
    change on either side cannot silently drop the link.
    """
    raw = assignment.get("identityId")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    return []


def _rate_limits(group: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The rate tier a RADIUS attribute group hands back, in readable units.

    Values arrive in bits per second as strings — WISPr-Bandwidth-Max-Down of
    "100000000" is 100 Mbps — so they are converted once here rather than in
    the UI. Non-numeric attributes are kept with their raw value; not every
    attribute is a rate limit.
    """
    out = []
    for attribute in group.get("attributeAssignments") or []:
        name = attribute.get("attributeName")
        raw = attribute.get("attributeValue")
        mbps = None
        if str(attribute.get("dataType") or "").upper() == "INTEGER" and "bandwidth" in str(name).lower():
            value = _num(raw, 0)
            if value:
                mbps = round(value / 1_000_000, 1)
        out.append({
            "attribute": name,
            "operator": attribute.get("operator"),
            "value": raw,
            "mbps": mbps,
        })
    return out


def policy_card(pool_rows: List[Dict[str, Any]],
                other_groups: List[Dict[str, Any]],
                sets: List[Dict[str, Any]],
                policies: List[Dict[str, Any]],
                radius_groups: List[Dict[str, Any]],
                set_members: Dict[str, List[Dict[str, Any]]],
                group_assignments: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    The adaptive policy chain behind this venue's DPSK, resolved end to end.

    Scoped by the policy sets the venue's own pools and identity groups point
    at, so a tenant-wide policy library does not flood a single site's report.

    Two linkage rules matter and are easy to get backwards:

    * A policy names its RADIUS attribute group in `onMatchResponse`. That
      forward link is authoritative and is what reference counts are built
      from.
    * /radiusAttributeGroups/{id}/assignments is the reverse view and
      PAGINATES at 10, so it cannot count anything. It is read only to find
      rows pointing at policies that no longer exist — the orphaned assignment
      that pins a group behind a 409 while the UI claims it has no policies.
    """
    scoped_set_ids = {row.get("policySetId") for row in pool_rows if row.get("policySetId")}
    for group in other_groups:
        if group.get("policySetId"):
            scoped_set_ids.add(group["policySetId"])
    # A set can also name the pool it is assigned to, which catches sets the
    # pool row itself did not carry an id for.
    pool_ids = {row.get("id") for row in pool_rows}
    for policy_set in sets:
        for assignment in policy_set.get("externalAssignments") or []:
            if pool_ids & set(_assignment_identity_ids(assignment)):
                scoped_set_ids.add(policy_set.get("id"))

    policy_by_id = {p.get("id"): p for p in policies}
    radius_by_id = {g.get("id"): g for g in radius_groups}
    known_policy_ids = set(policy_by_id)

    # Forward reference count: policy -> RADIUS group.
    refs_by_radius: Dict[str, List[str]] = defaultdict(list)
    for policy in policies:
        target = policy.get("onMatchResponse")
        if target:
            refs_by_radius[target].append(policy.get("name") or policy.get("id"))

    set_rows = []
    for policy_set in sets:
        set_id = policy_set.get("id")
        if set_id not in scoped_set_ids:
            continue
        members = set_members.get(set_id) or []
        resolved, unresolved = [], []
        for member in members:
            policy = policy_by_id.get(member.get("policyId"))
            if not policy:
                unresolved.append(member.get("policyId"))
                continue
            radius = radius_by_id.get(policy.get("onMatchResponse"))
            resolved.append({
                "policy": policy.get("name"),
                "policyType": policy.get("policyType"),
                "priority": member.get("priority"),
                "conditions": policy.get("conditionsCount"),
                "radiusGroup": (radius or {}).get("name"),
                "radiusGroupMissing": bool(policy.get("onMatchResponse")) and radius is None,
                "rateLimits": _rate_limits(radius) if radius else [],
            })
        resolved.sort(key=lambda row: (row["priority"] if row["priority"] is not None else 9999))
        set_rows.append({
            "id": set_id,
            "name": policy_set.get("name"),
            "description": policy_set.get("description"),
            "policyCount": policy_set.get("mappedPolicyCount"),
            "assignedTo": [a.get("identityName") for a in policy_set.get("externalAssignments") or []],
            "policies": resolved,
            "unresolvedPolicyIds": unresolved,
        })
    set_rows.sort(key=lambda row: str(row["name"] or ""))

    # RADIUS groups reachable from the scoped sets, plus their health.
    used_radius_ids = {
        policy_by_id[member["policyId"]].get("onMatchResponse")
        for row in set_rows for member in (set_members.get(row["id"]) or [])
        if member.get("policyId") in policy_by_id
    }
    radius_rows = []
    for group in radius_groups:
        gid = group.get("id")
        if gid not in used_radius_ids:
            continue
        assignments = group_assignments.get(gid) or []
        orphaned = [a for a in assignments
                    if a.get("externalAssignmentIdentifier")
                    and a["externalAssignmentIdentifier"] not in known_policy_ids]
        radius_rows.append({
            "id": gid,
            "name": group.get("name"),
            "description": group.get("description"),
            "rateLimits": _rate_limits(group),
            "policyCount": len(refs_by_radius.get(gid, [])),
            "policies": sorted(refs_by_radius.get(gid, []))[:12],
            "orphanedAssignments": len(orphaned),
        })
    radius_rows.sort(key=lambda row: (-row["policyCount"], str(row["name"] or "")))

    return {
        "inUse": bool(set_rows),
        "setCount": len(set_rows),
        "setsOnTenant": len(sets),
        "policiesOnTenant": len(policies),
        "radiusGroupsOnTenant": len(radius_groups),
        "sets": set_rows,
        "radiusGroups": radius_rows,
        "orphanedAssignments": sum(row["orphanedAssignments"] for row in radius_rows),
        "unresolvedPolicies": sum(len(row["unresolvedPolicyIds"]) for row in set_rows),
    }



def _channel_centre_mhz(channel: int, band_key: str) -> Optional[float]:
    """
    Centre frequency for a channel, in MHz.

    Channels 1-14 exist in BOTH the 2.4 and 6 GHz plans, so the band decides —
    the number alone is ambiguous and plotting it in the wrong place would put
    a 6 GHz radio on top of the 2.4 GHz spectrum.
    """
    if channel <= 0:
        return None
    if band_key.startswith("6"):
        return 5950 + channel * 5
    if band_key.startswith("5"):
        return 5000 + channel * 5
    if channel == 14:
        return 2484.0
    if 1 <= channel <= 13:
        return 2412 + (channel - 1) * 5
    return None


def _width_mhz(value: Optional[str]) -> float:
    """"80MHz" -> 80. AUTO and anything unparseable fall back to a 20 MHz slot."""
    number = _num("".join(c for c in str(value or "") if c.isdigit()), 0)
    return number if number > 0 else 20.0


# Bonding widths drawn as rows, smallest first — the layout of every standard
# Wi-Fi channel allocation chart. These are shown whether or not the venue is
# configured for them, because the chart's job is to show the spectrum a band
# COULD use against what it does; hiding the 80 MHz row on a venue set to AUTO
# left most bands with a single row and nothing to compare. A width in actual
# use is always drawn on top of these, which is how a Wi-Fi 7 radio at 320 MHz
# still appears.
BAND_WIDTH_ROWS = {"2.4": [20, 40], "5": [20, 40, 80], "6": [20, 40, 80, 160]}
BONDING_WIDTHS = [20, 40, 80, 160, 320]

# The conventional non-overlapping 2.4 GHz plan. Splitting the 20 MHz row into
# these three and everything else makes a radio parked on channel 3 or 8 jump
# out — the most common 2.4 GHz misconfiguration there is, and invisible when
# thirteen overlapping slots share one row. (1/5/9/13 is also mutually
# non-overlapping where 13 channels are permitted, so the second row is named
# "other" rather than claimed to be overlapping.)
CLEAN_24_CHANNELS = {1, 6, 11}


# 6 GHz Preferred Scanning Channels: 5, 21, 37 … every 16th channel. A 6 GHz
# radio only probes these, so a BSS whose primary is not a PSC is far slower to
# discover. Worth marking on the chart.
def _is_psc(channel: int) -> bool:
    return channel % 16 == 5


# Regulatory sub-bands by frequency, so a chart says WHERE in the spectrum a
# channel sits and what rules apply to it. DFS blocks are the ones a radio must
# vacate on radar detection — the practical reason an AP silently moves channel
# — so they are worth seeing behind the channel plan rather than memorising.
UNII_REGIONS = {
    "5": [
        {"label": "UNII-1", "loMhz": 5150, "hiMhz": 5250, "dfs": False},
        {"label": "UNII-2A", "loMhz": 5250, "hiMhz": 5350, "dfs": True},
        {"label": "UNII-2C", "loMhz": 5470, "hiMhz": 5730, "dfs": True},
        {"label": "UNII-3", "loMhz": 5730, "hiMhz": 5850, "dfs": False},
        {"label": "UNII-4", "loMhz": 5850, "hiMhz": 5925, "dfs": False},
    ],
    "6": [
        {"label": "UNII-5", "loMhz": 5925, "hiMhz": 6425, "dfs": False},
        {"label": "UNII-6", "loMhz": 6425, "hiMhz": 6525, "dfs": False},
        {"label": "UNII-7", "loMhz": 6525, "hiMhz": 6875, "dfs": False},
        {"label": "UNII-8", "loMhz": 6875, "hiMhz": 7125, "dfs": False},
    ],
    "2.4": [{"label": "ISM", "loMhz": 2400, "hiMhz": 2500, "dfs": False}],
}


# The full standard channel set per band, so the chart can show what the band
# HAS as well as what this venue permits. Without it a disabled channel is
# simply absent, which reads as "does not exist" rather than "turned off" —
# and the gap where UNII-3 or the DFS range should be is exactly what a site
# review wants to notice.
ALL_CHANNELS = {
    "2.4": list(range(1, 14)),
    "5": list(range(36, 65, 4)) + list(range(100, 145, 4)) + list(range(149, 166, 4)),
    "6": list(range(1, 234, 4)),
}


def _band_channels(band_key: str) -> List[int]:
    key = "6" if band_key.startswith("6") else "5" if band_key.startswith("5") else "2.4"
    return ALL_CHANNELS[key]


def _regions_for(band_key: str, low: float, high: float) -> List[Dict[str, Any]]:
    """Regulatory sub-bands overlapping the plotted range, clipped to it."""
    key = "6" if band_key.startswith("6") else "5" if band_key.startswith("5") else "2.4"
    out = []
    for region in UNII_REGIONS.get(key, []):
        lo, hi = max(region["loMhz"], low), min(region["hiMhz"], high)
        if hi - lo <= 1:
            continue
        out.append({**region, "clipLoMhz": lo, "clipHiMhz": hi})
    return out


def _is_dfs(centre_mhz: float, band_key: str) -> bool:
    if not band_key.startswith("5"):
        return False
    return any(r["dfs"] and r["loMhz"] <= centre_mhz < r["hiMhz"]
               for r in UNII_REGIONS["5"])


def _channel_from_centre(mhz: float, band_key: str) -> Optional[int]:
    """Invert _channel_centre_mhz — the channel number at a centre frequency."""
    if band_key.startswith("6"):
        value = (mhz - 5950) / 5
    elif band_key.startswith("5"):
        value = (mhz - 5000) / 5
    else:
        value = (mhz - 2412) / 5 + 1
    rounded = round(value)
    return int(rounded) if abs(value - rounded) < 0.01 and rounded > 0 else None


def _bond(slots: List[Dict[str, Any]], width: int) -> List[Dict[str, Any]]:
    """
    Group 20 MHz slots into blocks of `width`.

    Channels bond only with neighbours whose centre sits exactly 20 MHz away,
    and only in aligned groups from the start of a contiguous run. That is what
    keeps 2.4 GHz honest: with 1/6/11 permitted, the centres are 25 MHz apart,
    nothing bonds, and no 40 MHz row is drawn — which is correct rather than a
    gap in the data.
    """
    if width == 20:
        return [{"channels": [s["channel"]], "loMhz": s["centre"] - 10,
                 "hiMhz": s["centre"] + 10} for s in slots]

    per_block = width // 20
    runs: List[List[Dict[str, Any]]] = []
    for slot in slots:
        if runs and abs(slot["centre"] - runs[-1][-1]["centre"] - 20) < 0.01:
            runs[-1].append(slot)
        else:
            runs.append([slot])

    blocks = []
    for run in runs:
        for start in range(0, len(run) - per_block + 1, per_block):
            chunk = run[start:start + per_block]
            if len(chunk) < per_block:
                continue
            blocks.append({"channels": [c["channel"] for c in chunk],
                           "loMhz": chunk[0]["centre"] - 10,
                           "hiMhz": chunk[-1]["centre"] + 10})
    return blocks


def channel_plan(venue_radio: List[Dict[str, Any]],
                 aps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The channel plan per band, as a channel-allocation chart.

    One row per bonding width, each showing the blocks that width can form from
    the channels the venue permits, with the block a radio is actually running
    on filled in. This is the shape every Wi-Fi channel chart uses, and it says
    something a flat channel list cannot: an 80 MHz radio occupies four 20 MHz
    channels, so the row it sits on is where its real spectrum cost is visible.

    Radio width comes per-radio from the AP, not from the venue setting — the
    venue asks for a width and a radio may land on another, and a Wi-Fi 7 radio
    on 320 MHz sits on a row the venue never configured.

    Channel 0 is dropped: it is what an AP reports when it has no channel.
    """
    def digits(value: Optional[str]) -> str:
        return "".join(c for c in str(value or "") if c.isdigit() or c == ".").rstrip(".")

    # Every radio in the venue, as (band key, channel, operating width).
    radios: List[Dict[str, Any]] = []
    for ap in aps or []:
        for radio in ap.get("radios") or []:
            channel = int(_num(radio.get("channel"), 0))
            if channel <= 0:
                continue
            radios.append({"key": digits(radio.get("band")), "channel": channel,
                           "width": int(_width_mhz(radio.get("width")))})

    plan = []
    for entry in venue_radio or []:
        key = digits(entry.get("band"))
        band_radios = [r for r in radios if r["key"] == key]

        allowed = sorted({int(_num(v, 0)) for v in entry.get("allowed") or []} - {0})
        in_use_channels = {r["channel"] for r in band_radios}
        off_plan = sorted(in_use_channels - set(allowed))

        # Every channel the band defines, not just the permitted ones — a
        # channel the venue has turned off still occupies spectrum and its
        # absence from the chart would be indistinguishable from it not
        # existing.
        slots = []
        for channel in sorted(set(_band_channels(key)) | set(allowed) | in_use_channels):
            centre = _channel_centre_mhz(channel, key)
            if centre is None:
                continue
            slots.append({"channel": channel, "centre": centre,
                          "allowed": channel in allowed})
        if not slots:
            continue

        band_default = BAND_WIDTH_ROWS.get(key[:3]) or BAND_WIDTH_ROWS.get(key[:1]) or [20]
        widths_in_use = {r["width"] for r in band_radios}

        rows = []
        for width in BONDING_WIDTHS:
            if width not in band_default and width not in widths_in_use:
                continue
            blocks = _bond(slots, width)
            if not blocks:
                continue
            used_here = [r for r in band_radios if r["width"] == width]
            shaped = []
            for block in blocks:
                on_it = [r for r in used_here if r["channel"] in block["channels"]]
                centre_channel = (block["channels"][0] if width == 20 else
                                  _channel_from_centre(
                                      (block["loMhz"] + block["hiMhz"]) / 2, key))
                shaped.append({
                    "channels": block["channels"],
                    # The centre channel IS the channel a bonded BSS is named
                    # by — an 80 MHz BSS on 36-48 is "channel 42" everywhere
                    # else in the industry, so an edge range read as unfamiliar.
                    "label": str(centre_channel) if centre_channel
                             else f"{block['channels'][0]}–{block['channels'][-1]}",
                    "centreChannel": centre_channel,
                    "span": f"{block['channels'][0]}–{block['channels'][-1]}",
                    "loMhz": block["loMhz"], "hiMhz": block["hiMhz"],
                    "count": len(on_it),
                    "inUse": bool(on_it),
                    "allowed": all(c in allowed for c in block["channels"]),
                    "offPlan": bool(on_it) and any(c not in allowed for c in block["channels"]),
                })
            # Inclusion was already decided above, by BAND_WIDTH_ROWS plus any
            # width actually in use. A second gate on the venue's configured
            # width used to live here and silently undid it: a venue set to
            # AUTO reports width 20, so every wider row was dropped and 5 GHz
            # showed a lone 20 MHz row.
            # On 2.4 GHz the 20 MHz row is split in two so a radio outside the
            # 1/6/11 plan is visible at a glance rather than lost among
            # thirteen overlapping slots.
            if key.startswith("2") and width == 20:
                primary = [b for b in shaped
                           if set(b["channels"]) <= CLEAN_24_CHANNELS]
                other = [b for b in shaped
                         if not set(b["channels"]) <= CLEAN_24_CHANNELS]
                rows.append({"width": width, "label": "20 MHz · 1/6/11",
                             "blocks": primary,
                             "radios": sum(b["count"] for b in primary)})
                if other:
                    rows.append({"width": width, "label": "20 MHz · other",
                                 "blocks": other,
                                 "radios": sum(b["count"] for b in other)})
                continue
            rows.append({"width": width, "label": f"{width} MHz",
                         "blocks": shaped, "radios": len(used_here)})

        plan.append({
            "band": entry.get("band"),
            "width": entry.get("width"),
            "method": entry.get("method"),
            "power": entry.get("power"),
            "allowedCount": len(allowed),
            "inUseCount": len(in_use_channels),
            "offPlanCount": len(off_plan),
            "radios": len(band_radios),
            "outsidePlanCount": (sum(1 for r in band_radios
                                     if r["channel"] not in CLEAN_24_CHANNELS)
                                 if key.startswith("2") else 0),
            # Channels the venue PERMITS on 2.4 GHz that are not 1/6/11, and
            # the radios sitting on them. Shaped here so the check stays a pure
            # reader and the 1/6/11 definition lives in one place.
            "enabledOutsidePlan": (sorted(c for c in allowed
                                          if c not in CLEAN_24_CHANNELS)
                                   if key.startswith("2") else []),
            "radiosOutsidePlan": (sorted({r["channel"] for r in band_radios
                                          if r["channel"] not in CLEAN_24_CHANNELS})
                                  if key.startswith("2") else []),
            "rows": rows,
            "isSixGhz": key.startswith("6"),
            "slots": [{"channel": s["channel"], "centreMhz": s["centre"],
                       "allowed": s["allowed"],
                       "psc": key.startswith("6") and _is_psc(s["channel"]),
                       "dfs": _is_dfs(s["centre"], key)}
                      for s in slots],
            "dfsChannels": sorted(s["channel"] for s in slots if _is_dfs(s["centre"], key)),
            "minMhz": round(min(s["centre"] for s in slots) - 15, 1),
            "maxMhz": round(max(s["centre"] for s in slots) + 15, 1),
            "regions": _regions_for(key,
                                    min(s["centre"] for s in slots) - 15,
                                    max(s["centre"] for s in slots) + 15),
        })
    return plan

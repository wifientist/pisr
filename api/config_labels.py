"""
Turning R1's raw configuration keys into something a person reads.

The pattern is lifted from rtools2's bulk WLAN editor, which solved the same
problem for one known object: a label table, an enum table, a units table, and
one `format_value`. What is different here is that the Config tab renders about
thirty-five arbitrary blobs whose shape PISR does not control and which RUCKUS
changes without telling anyone — so this CANNOT be an exhaustive hand-written
map the way the WLAN editor's was.

So it degrades instead:

  1. an explicit label, where the key is worth naming precisely
     ("useVenueSettings" is not "Use venue settings", it is "Inherits from venue")
  2. otherwise a de-camelCased fallback — "serverLossTimeout" becomes
     "Server loss timeout", which reads like English and, more importantly,
     still APPEARS

That second rule is the load-bearing one. A hardcoded layout would silently
drop a field R1 added, and the whole point of this tab is to show what is
actually set, including the surprise. An unlabelled field looks slightly worse
than a labelled one; a missing field looks like a setting that does not exist.

The raw key travels alongside the label everywhere it is shown, because anyone
cross-referencing the R1 console or the OpenAPI spec needs the real name.
"""

import re
from typing import Any, Dict, Optional, Tuple

# Keys where the plain de-camelCased reading is wrong, ambiguous, or loses the
# thing that matters. Matched on the LEAF key at any depth, case-insensitively.
#
# Kept to the ones that earn it. There are several hundred distinct leaves
# across the config payload and labelling all of them by hand would be a
# maintenance tax paid every time RUCKUS ships a field — the fallback handles
# the ordinary ones perfectly well.
LABELS: Dict[str, str] = {
    # inheritance — the single most important field on the tab
    "usevenuesettings": "Inherits from venue",
    "isdefault": "Default group",
    "isenforced": "Enforced by template",
    "istemplate": "Is a template",

    # radio
    "method": "Channel selection method",
    "manualchannel": "Manual channel",
    "operativechannel": "Operating channel",
    "operativetxpower": "Operating Tx power",
    "txpower": "Tx power",
    "channelbandwidth": "Channel width",
    "changeinterval": "Channel change interval",
    "allowedchannels": "Allowed channels",
    "enableafc": "AFC (6 GHz)",
    "afcenabled": "AFC (6 GHz)",
    "bssminrate6g": "BSS minimum rate (6 GHz)",
    "mgmttxrate6g": "Management frame rate (6 GHz)",
    "enableindependentbss": "Independent BSS",
    "snrdb": "Signal-to-noise ratio",
    "dual5genabled": "Dual 5 GHz",

    # load balancing / steering
    "bandbalancingenabled": "Band balancing",
    "bandbalancingclientpercent24g": "2.4 GHz client share target",
    "steeringmode": "Client steering mode",
    "loadbalancingmethod": "Load balancing method",

    # client admission control
    "enable24g": "Enabled on 2.4 GHz",
    "enable50g": "Enabled on 5 GHz",
    "minclientcount24g": "Minimum clients (2.4 GHz)",
    "minclientcount50g": "Minimum clients (5 GHz)",
    "maxradioload24g": "Maximum radio load (2.4 GHz)",
    "maxradioload50g": "Maximum radio load (5 GHz)",
    "minclientthroughput24g": "Minimum client throughput (2.4 GHz)",
    "minclientthroughput50g": "Minimum client throughput (5 GHz)",

    # antenna
    "gain24g": "Antenna gain (2.4 GHz)",
    "gain50g": "Antenna gain (5 GHz)",
    "supportdisable": "Can be disabled",
    "antennatype": "Antenna type",
    "externalantenna": "External antenna",

    # LAN ports
    "lanports": "LAN ports",
    "portid": "Port",
    "untagid": "Untagged VLAN",
    "vlanmembers": "VLAN members",
    "usbportenable": "USB port",

    # services
    "gatewaylosstimeout": "Reboot after gateway loss",
    "serverlosstimeout": "Reboot after cloud loss",
    "enableapsnmp": "AP SNMP agent",
    "tlskeyenhancedmodeenabled": "TLS enhanced key mode",
    "bsscoloringenabled": "BSS coloring",
    "ledenabled": "Status LED",
    "reportthreshold": "Report threshold",
    "networkprotection": "Rogue network protection",
    "networkprotectionstrategy": "Rogue protection strategy",
    "blockingperiod": "Blocking period",
    "failthreshold": "Failure threshold",
    "checkperiod": "Check period",

    # RADIUS
    "calledstationidtype": "Called-Station-Id format",
    "nasidtype": "NAS-Identifier format",
    "nasiddelimiter": "NAS-Identifier delimiter",
    "overrideenabled": "Override venue defaults",
    "sharedsecret": "Shared secret",

    # syslog
    "flowlevel": "Log flow level",
    "secondaryport": "Secondary port",
    "secondaryprotocol": "Secondary protocol",

    # AAA
    "authnenabledssh": "SSH authentication",
    "authnfirstpref": "First authentication method",
    "authzenabledcommand": "Command authorisation",
    "authzenabledexec": "Exec authorisation",
    "acctenabledcommand": "Command accounting",
    "acctenabledexec": "Exec accounting",

    # path segments used as sub-group headings
    "radioparams24g": "2.4 GHz",
    "radioparams50g": "5 GHz",
    "radioparams6g": "6 GHz",
    "radioparamsdual5g": "Dual 5 GHz",
    "radioparamslower5g": "Lower 5 GHz",
    "radioparamsupper5g": "Upper 5 GHz",
    "radiocustomization": "Radio",
    "24gchannels": "2.4 GHz",
    "5gchannels": "5 GHz",
    "6gchannels": "6 GHz",
    "5glowerchannels": "5 GHz lower",
    "5gupperchannels": "5 GHz upper",
    "dfs": "DFS",
    "indoor": "Indoor",
    "outdoor": "Outdoor",
    "floorplans": "Floor plans",
    "apsnmpagent": "AP SNMP agent",
    "denialofserviceprotection": "DoS protection",
    "clientadmissioncontrol": "Client admission control",
    "dhcpservicesetting": "DHCP service",
    "bandbalancing": "Band balancing",
    "loadbalancing": "Load balancing",
    "directedmulticast": "Directed multicast",
    "bsscoloring": "BSS coloring",
    "rogueap": "Rogue AP",

    # misc
    "wiredenabled": "Wired",
    "wirelessenabled": "Wireless",
    "networkenabled": "Network",
    "atthisvenue": "Present at this venue",
}

# Values that read badly as-is. Matched on the VALUE, case-insensitively, and
# only for strings — a code R1 uses in place of prose.
VALUE_LABELS: Dict[str, str] = {
    "channelfly": "ChannelFly",
    "background_scanning": "Background scanning",
    "based_on_client_count": "Based on client count",
    "based_on_throughput": "Based on throughput",
    "keep_original": "Keep original",
    "client_flow": "Client flow",
    "nodelimiter": "No delimiter",
    "conservative": "Conservative",
    "aggressive": "Aggressive",
    "auto": "Auto",
    "trunk": "Trunk",
    "access": "Access",
    "sector": "Sector",
    "omni": "Omnidirectional",
    "disabled": "Disabled",
    "enabled": "Enabled",
}

# Leaf keys whose numeric value carries a unit R1 does not state.
UNITS: Dict[str, str] = {
    "gain24g": "dBi", "gain50g": "dBi",
    "maxradioload24g": "%", "maxradioload50g": "%",
    "bandbalancingclientpercent24g": "%",
    "minclientthroughput24g": "Mbps", "minclientthroughput50g": "Mbps",
    "port": "", "secondaryport": "",
    "reportthreshold": "dBm",
    "interval": "s", "threshold": "",
    "blockingperiod": "s", "checkperiod": "s",
    "changeinterval": "min",
}

# Leaf keys measured in seconds and better read as a duration.
SECONDS: Tuple[str, ...] = ("gatewaylosstimeout", "serverlosstimeout",
                            "uptime_seconds", "clientinactivitytimeout")

# Booleans that answer "is there one" rather than "is it turned on".
# "Has image: Enabled" reads as though the image were a feature someone
# switched on; the question was whether a file is attached.
YES_NO_PREFIXES = ("has", "is", "can", "supports")
YES_NO_KEYS = frozenset({"scaleset", "atthisvenue", "present", "placed",
                         "usevenuesettings", "truncated", "portsknown"})

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYMS = {"ap": "AP", "vlan": "VLAN", "ip": "IP", "dns": "DNS", "id": "ID",
             "ssid": "SSID", "poe": "PoE", "snmp": "SNMP", "usb": "USB",
             "led": "LED", "dhcp": "DHCP", "tls": "TLS", "mdns": "mDNS",
             "aaa": "AAA", "bss": "BSS", "afc": "AFC", "nas": "NAS",
             "ssh": "SSH", "cli": "CLI", "dos": "DoS", "mac": "MAC",
             "rssi": "RSSI", "qos": "QoS", "lan": "LAN", "wan": "WAN",
             "r1": "R1", "sim": "SIM", "apn": "APN", "gps": "GPS",
             "wifi": "Wi-Fi", "bssid": "BSSID", "afc": "AFC", "mlo": "MLO"}


def _normalise(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def label_for(key: str) -> str:
    """
    A readable label for one config key.

    Falls back to de-camelCasing rather than to the raw key, so an unmapped
    field still reads like a setting. Known acronyms are re-capitalised —
    "apSnmpAgent" reading as "Ap snmp agent" looks like a bug, and a reader
    who cannot tell a bug from a label stops trusting the whole page.
    """
    explicit = LABELS.get(_normalise(key))
    if explicit:
        return explicit

    words = _CAMEL.sub(" ", str(key)).replace("_", " ").split()
    if not words:
        return str(key)
    out = []
    for index, word in enumerate(words):
        acronym = _ACRONYMS.get(word.lower())
        if acronym:
            out.append(acronym)
        elif index == 0:
            out.append(word[:1].upper() + word[1:])
        else:
            # Keep a word that is already mixed case — "MHz", "dBm", "GHz" are
            # units, and lowercasing them produced "20 mhz". Only flatten words
            # that are plainly one capitalised English word.
            out.append(word if any(c.isupper() for c in word[1:]) else word.lower())
    return " ".join(out)


def _duration(seconds: float) -> str:
    total = int(seconds)
    if total <= 0:
        return "0"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if total >= size and total % size == 0:
            value = total // size
            return f"{value} {unit}{'s' if value != 1 else ''}"
    if total >= 3600:
        return f"{total // 3600}h {(total % 3600) // 60}m"
    if total >= 60:
        return f"{total // 60}m {total % 60}s"
    return f"{total} seconds"


def format_value(key: str, value: Any) -> str:
    """
    One config value as display text.

    None is "not set" rather than "—": on a settings page the reader is asking
    what something is configured to, and an em dash reads as "no data" when the
    answer is "nothing is configured".
    """
    if value is None:
        return "not set"
    flat = _normalise(key)
    if isinstance(value, bool):
        if flat in YES_NO_KEYS or any(flat.startswith(w) for w in YES_NO_PREFIXES):
            return "Yes" if value else "No"
        return "Enabled" if value else "Disabled"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if flat in SECONDS:
            return _duration(value)
        unit = UNITS.get(flat)
        if unit:
            return f"{value} {unit}"
        return str(value)

    if isinstance(value, str):
        if not value.strip():
            return "not set"
        return VALUE_LABELS.get(value.strip().lower(), value)

    if isinstance(value, list):
        if not value:
            return "none"
        if all(not isinstance(v, (dict, list)) for v in value):
            # NOT truncated. This used to cut at twelve with "(+N more)", which
            # is exactly wrong for the thing these lists are: an allowed-channel
            # plan or a VLAN membership list is only useful in full, and the
            # one channel that got cut off is the one somebody is looking for.
            # The table cell wraps; a long list is long.
            return ", ".join(str(v) for v in value)
        return f"{len(value)} item(s)"

    if isinstance(value, dict):
        return "none" if not value else f"{len(value)} field(s)"

    return str(value)

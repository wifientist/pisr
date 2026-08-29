"""
PISR fetch layer — every read PISR makes against RUCKUS ONE.

READ-ONLY BY CONSTRUCTION. Every function here issues a GET or a `*/query`
POST. Nothing in this module creates, updates, deletes, activates, reboots or
syncs anything, and PISR never writes to local storage either — a report is
assembled in memory, returned once, and forgotten.

HUMAN-TRIGGERED ONLY. Every function runs once per request. PISR registers no
scheduled job and starts no background task; there is deliberately nothing here
for a scheduler to call.

All functions are SYNC (the R1 client is `requests`-based). The collector runs
them through `asyncio.to_thread` so a report fans out without blocking the loop.
Each returns a plain value on success and raises nothing the collector cannot
survive — a section that fails is reported as a section that failed, never as an
empty network.
"""

import logging

import scrub
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# AP fields, chosen against a live tenant rather than the DTO: `IP` and `extIp`
# are on the schema but come back empty, while `networkStatus` carries the real
# addressing, `poePortStatus` the uplink link state, and `radioStatuses` the
# SSIDs each radio is beaconing.
AP_FIELDS = [
    "name", "status", "model", "serialNumber", "macAddress",
    "firmwareVersion", "venueId", "apGroupId", "apGroupName", "clientCount",
    "meshRole", "switchSerialNumber", "poePort", "poePortStatus",
    "lastSeenTime", "uptime", "networkStatus", "radioStatuses", "lanPortStatuses",
    "tags", "floorplanId",
]

# The subset proven in production elsewhere in this codebase. Used only if the
# list above comes back empty, so one unrecognised field name cannot cost us the
# whole AP section.
AP_FIELDS_SAFE = [
    "name", "status", "model", "networkStatus", "macAddress", "venueName",
    "switchName", "meshRole", "clientCount", "apGroupId", "apGroupName",
    "lanPortStatuses", "tags", "serialNumber", "radioStatuses", "venueId",
    "poePort", "firmwareVersion", "uptime",
]

VENUE_FIELDS = [
    "id", "name", "addressLine", "city", "country", "countryCode",
    "latitude", "longitude", "aggregatedApStatus", "clients", "networks",
    "operationalSwitches", "switchClients", "isApFirmwareUpToDate",
    "currentApFirmwares", "mesh", "dhcp", "rogueDetection", "isEnforced",
]

# `venues` and `vlanPool` used to be in this list and are not fields on
# WifiNetworkQueryData — R1 ignores names it does not know rather than
# rejecting the call, so they cost nothing but were never returning anything.
# `description` and `captiveType` only come back when they are set.
NETWORK_FIELDS = [
    "id", "name", "ssid", "description", "nwType", "nwSubType",
    "securityProtocol", "vlan", "captiveType", "apCount", "clientCount",
]

# /wifiNetworks/query is 1-INDEXED: page 0 and page 1 both return the first
# block and page 2 returns the second (verified against four live tenants).
NETWORK_PAGE_SIZE = 500
NETWORK_PAGE_LIMIT = 40  # 20k networks — a runaway backstop, not an expected bound

# Client fields. SSID, VLAN, AP and RSSI live in nested objects — the flat
# `ssid`/`apName`/`rssi` names return nothing on this endpoint.
CLIENT_FIELDS = [
    "macAddress", "hostname", "ipAddress", "osType", "deviceType", "band",
    "connectedTime", "apInformation", "networkInformation", "signalStatus",
    "radioStatus", "trafficStatus",
]


# ── plumbing ─────────────────────────────────────────────────

def _get(r1, path: str, tenant_id: Optional[str], params: Optional[Dict[str, Any]] = None):
    """GET that adds the MSP tenant override only when the client is MSP-scoped."""
    if r1.ec_type == "MSP" and tenant_id:
        return r1.get(path, params=params, override_tenant_id=tenant_id)
    return r1.get(path, params=params)


def _post(r1, path: str, payload: Dict[str, Any], tenant_id: Optional[str]):
    if r1.ec_type == "MSP" and tenant_id:
        return r1.post(path, payload=payload, override_tenant_id=tenant_id)
    return r1.post(path, payload=payload)


def _json(resp, what: str, default=None):
    """Body of a response, or `default` — a bad body is a missing section, not a crash."""
    if resp is None or not resp.ok:
        code = getattr(resp, "status_code", "no response")
        body = getattr(resp, "text", "")[:200]
        logger.warning("pisr: %s -> HTTP %s: %s", what, code, body)
        return default
    try:
        return resp.json()
    except ValueError:
        logger.warning("pisr: %s returned non-JSON", what)
        return default


def _rows(payload) -> List[Dict[str, Any]]:
    """
    R1 hands back three different envelopes; treat them all the same.

      * a bare list
      * {data: [...], totalCount: n}          — the /query endpoints
      * {content: [...], totalElements: n}    — Spring Page, used by
        /identityGroups/query and friends. Reading only `data` on those
        returned nothing at all, which looks exactly like "this tenant has no
        identity groups".
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "content"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return rows
    return []


# ── venue-scope reads ────────────────────────────────────────

def venue_rows(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every venue in the tenant, enriched with the aggregate counts R1 already
    keeps. Falls back to the plain venue list, which is always available — the
    picker has to work even when the query endpoint does not.
    """
    body = {"fields": VENUE_FIELDS, "sortField": "name", "sortOrder": "ASC",
            "page": 0, "pageSize": 500}
    rows = _rows(_json(_post(r1, "/venues/query", body, tenant_id), "/venues/query"))
    if rows:
        return rows
    logger.info("pisr: /venues/query gave nothing, falling back to GET /venues")
    return _rows(_json(_get(r1, "/venues", tenant_id), "GET /venues", []))


def venue_detail(r1, tenant_id: Optional[str], venue_id: str) -> Dict[str, Any]:
    return _json(_get(r1, f"/venues/{venue_id}", tenant_id),
                 f"GET /venues/{venue_id}", {}) or {}


def property_config(r1, tenant_id: Optional[str], venue_id: str) -> Optional[Dict[str, Any]]:
    """
    The venue's Property (MDU) configuration, or None when it is not a property.

    A 404 here is a fact about the venue, not a failure: plenty of venues are
    plain venues. The collector reports "not a property" rather than an error.
    """
    resp = _get(r1, f"/venues/{venue_id}/propertyConfigs", tenant_id)
    if resp is not None and resp.status_code == 404:
        return None
    return _json(resp, f"GET /venues/{venue_id}/propertyConfigs")


UNIT_PAGE_SIZE = 200
UNIT_PAGE_LIMIT = 50


def property_units(r1, tenant_id: Optional[str], venue_id: str) -> Dict[str, Any]:
    """
    Aggregate unit facts for a property venue — counts only.

    Two things this had wrong. It read `page.totalElements`, but this is a
    Spring Page and `totalElements` sits at the TOP level, so the unit count
    was always None and the report rendered a property with "? units". And it
    asked for a single row, which cannot say how many units actually have a
    resident assigned — the number that tells you whether a property is
    provisioned or just configured.

    A unit row carries `resident.name`. That is a real person and it is not
    kept: only whether a resident exists is counted. Nothing identifying leaves
    this function.
    """
    total: Optional[int] = None
    by_status: Dict[str, int] = {}
    with_resident = 0
    identities = 0
    seen = 0

    for page in range(UNIT_PAGE_LIMIT):
        payload = _json(_get(r1, f"/venues/{venue_id}/units", tenant_id,
                             params={"page": page, "size": UNIT_PAGE_SIZE}),
                        f"GET /venues/{venue_id}/units page={page}", {}) or {}
        if not isinstance(payload, dict):
            break
        if isinstance(payload.get("totalElements"), int):
            total = payload["totalElements"]
        rows = payload.get("content") or []
        for row in rows:
            seen += 1
            status = str(row.get("status") or "Unknown")
            by_status[status] = by_status.get(status, 0) + 1
            if (row.get("resident") or {}).get("name"):
                with_resident += 1
            identities += int(row.get("identityCount") or 0)
        if not rows or payload.get("last"):
            break

    return {
        "total": total if total is not None else seen,
        "byStatus": [{"label": k, "count": v}
                     for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])],
        "withResident": with_resident,
        "withoutResident": max(0, (total if total is not None else seen) - with_resident),
        "identityCount": identities,
        "complete": total is None or seen >= total,
    }


def ap_management_vlan(r1, tenant_id: Optional[str], venue_id: str) -> Optional[int]:
    body = _json(_get(r1, f"/venues/{venue_id}/apManagementTrafficVlanSettings", tenant_id),
                 "apManagementTrafficVlanSettings", {}) or {}
    vlan = body.get("vlanId") if isinstance(body, dict) else None
    return vlan


def dhcp_pools(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    return _rows(_json(_get(r1, f"/venues/{venue_id}/dhcpPools", tenant_id),
                       "venue dhcpPools", []))


def ap_groups(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    return _rows(_json(_get(r1, f"/venues/{venue_id}/apGroups", tenant_id),
                       "venue apGroups", []))


def radio_settings(r1, tenant_id: Optional[str], venue_id: str) -> Dict[str, Any]:
    body = _json(_get(r1, f"/venues/{venue_id}/apRadioSettings", tenant_id),
                 "venue apRadioSettings", {}) or {}
    return body if isinstance(body, dict) else {}


def mesh_settings(r1, tenant_id: Optional[str], venue_id: str) -> Dict[str, Any]:
    body = _json(_get(r1, f"/venues/{venue_id}/apMeshSettings", tenant_id),
                 "venue apMeshSettings", {}) or {}
    return body if isinstance(body, dict) else {}


# ── devices ──────────────────────────────────────────────────

def access_points(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    """
    Every AP in the venue. Retries once on the proven-safe field list: an empty
    result usually means a field name this tenant's R1 build does not know, and
    a thin AP section beats no AP section.
    """
    aps = r1.venues.query_all_aps_by_tenant(tenant_id, [venue_id], AP_FIELDS)
    if not aps:
        logger.info("pisr: rich AP field list returned nothing for venue %s, retrying safe",
                    venue_id)
        aps = r1.venues.query_all_aps_by_tenant(tenant_id, [venue_id], AP_FIELDS_SAFE)
    return aps or []


def venue_ap_total(r1, tenant_id: Optional[str], venue_id: str) -> Optional[int]:
    """
    How many APs R1 says the venue has, independent of how many it hands over.

    One cheap call with pageSize 1, purely to read `totalCount`. Comparing it
    against the rows actually returned is the only way to know the AP list was
    truncated — and a truncated AP list makes every group whose APs fell off
    the end look empty, which is indistinguishable from a real finding.
    """
    body = {"fields": ["serialNumber"], "filters": {"venueId": [venue_id]},
            "page": 0, "pageSize": 1}
    payload = _json(_post(r1, "/venues/aps/query", body, tenant_id),
                    "venue AP totalCount", {}) or {}
    total = payload.get("totalCount") if isinstance(payload, dict) else None
    return total if isinstance(total, int) else None


def switches(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    return r1.switches.list_switches(tenant_id, venue_id=venue_id) or []


def switch_ports(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    return r1.switches.crawl_ports(tenant_id, [venue_id]) or []


def clients(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    return r1.clients.query_all_clients_for_venue(tenant_id, venue_id, CLIENT_FIELDS) or []


# Venue-level configuration, one entry per R1 endpoint. The key is the
# category the Config tab groups by, so "how it was pulled" is what the reader
# sees — which is the honest organisation for a settings dump nobody can
# usefully flatten.
#
# `switchSettings` IS NOT HERE, deliberately. It returns six keys, one of which
# is the customer's switch admin password in plaintext, and the other useful
# ones are two booleans. Reading a live credential into this process to display
# `syslogEnabled` is not a trade worth making — api/scrub.py would catch it on
# the way out, but the right answer is not to fetch it. See CLAUDE.md.
VENUE_CONFIG_SOURCES = {
    "loadBalancing": "apLoadBalancingSettings",
    "availableChannels": "wifiAvailableChannels",
    "clientAdmission": "apClientAdmissionControlSettings",
    "directedMulticast": "apDirectedMulticastSettings",
    "smartMonitor": "apSmartMonitorSettings",
    "bandMode": "apModelBandModeSettings",
    # Per AP MODEL, not per AP: a list with one entry per model present, each
    # carrying its own lanPorts array. The venue-level default that a per-AP
    # `lanPorts` override (visible in the on-demand detail) departs from.
    # `apModelLanPortSettings` returns the identical payload; one is enough.
    "lanPorts": "lanPortSettings",
    # Includes externalAntenna gain and per-band enable, which is where the
    # antenna defaults live — there is no separate antenna endpoint.
    "modelCapabilities": "apModelCapabilities",
    "models": "apModels",
    "aaa": "aaaSettings",
    "dosProtection": "apDosProtectionSettings",
    "rogueAp": "rogueApSettings",
    "syslog": "syslogSettings",
    "snmp": "snmpAgentSettings",
    "led": "ledSettings",
    "bssColoring": "apBssColoringSettings",
    "cellular": "apCellularSettings",
    # Found in the OpenAPI spec under spec/ rather than by guessing. The first
    # two sweeps missed every one of these because R1's naming is inconsistent
    # about the `ap` prefix and about `Model` — it is `syslogSettings` but
    # `apModelUsbPortSettings`, `rogueApSettings` but `apRebootTimeoutSettings`.
    # Read the spec before probing for anything else.
    "antennaType": "apModelAntennaTypeSettings",
    "externalAntenna": "apModelExternalAntennaSettings",
    "mdnsFencing": "apMulticastDnsFencingSettings",
    "radiusOptions": "apRadiusOptions",
    "rebootTimeout": "apRebootTimeoutSettings",
    "tlsKey": "apTlsKeyEnhancedSettings",
    "usbPorts": "apModelUsbPortSettings",
    "roguePolicy": "roguePolicySettings",
    "wifiSettings": "wifiSettings",
    "regulatoryChannels": "channels",
    "syslogProfile": "syslogServerProfileSettings",
    "dhcpServiceProfile": "dhcpConfigServiceProfileSettings",
    "trustedPorts": "trustedPorts",
}

# Settings that genuinely have no VENUE-level endpoint, confirmed against the
# OpenAPI spec in spec/ rather than by guessing at names. Both exist, but not
# at this level:
#
#   IoT controller  -> per AP only, /venues/{v}/aps/{serial}/iotSettings
#   Location-based  -> tenant-wide profile objects, /lbsServerProfiles/query
#
# Everything else on the original wanted list turned out to exist and is in
# VENUE_CONFIG_SOURCES above. The lesson is in the naming: two rounds of
# probing missed seven endpoints that were there all along, because R1 is
# inconsistent about the `ap` and `Model` prefixes. Search the spec first.
VENUE_CONFIG_NOT_FOUND = (
    "IoT controller (per-AP only)",
    "Location-based services (tenant-wide profiles, not a venue setting)",
)

# Per AP-group. Each carries a `useVenueSettings` boolean, which is the whole
# point: it says whether the group inherits or overrides.
AP_GROUP_CONFIG_SOURCES = {
    "clientAdmission": "apClientAdmissionControlSettings",
    "bandMode": "apModelBandModeSettings",
    "antennaType": "apModelAntennaTypeSettings",
    "externalAntenna": "apModelExternalAntennaSettings",
    "capabilities": "apModelCapabilities",
    "radio": "radioSettings",
    "availableChannels": "wifiAvailableChannels",
}

# Per-AP config is one call per AP — the bulk `/venues/aps/query` accepts the
# nested field names and returns nothing for them, verified 2026-08-28. So a
# large venue would be hundreds of requests, and this caps it rather than
# letting a report quietly take a minute. The cap is reported in the payload so
# the UI can say the list is partial instead of implying it is complete.
AP_CONFIG_LIMIT = 100


def venue_config_one(r1, tenant_id: Optional[str], venue_id: str,
                     key: str) -> Any:
    """
    ONE venue-level settings block, by category key.

    Deliberately one call per invocation rather than a loop over all nineteen.
    An earlier version fetched them together behind its own ThreadPoolExecutor,
    which was faster than sequential and wrong: `collect.build_report` already
    fans the whole report out through asyncio's default executor, and a second
    uncoordinated pool pushed concurrent requests past the R1 client's
    `pool_maxsize` — urllib3 then logs "Connection pool is full, discarding
    connection" and every request past the tenth pays a fresh TLS handshake
    and is thrown away rather than pooled. See the note in r1api/client.py.

    Registering each of these as its own read in `build_report` keeps ONE
    executor and one bound on how many sockets PISR asks R1 for.
    """
    path = VENUE_CONFIG_SOURCES[key]
    return _json(_get(r1, f"/venues/{venue_id}/{path}", tenant_id),
                 f"venue {path}", None)


def scrub_venue_config(blocks: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scrub the assembled venue config, for the same reason the RADIUS profiles
    are scrubbed at the fetch: a credential should never reach the report
    object, only be caught leaving it. None of these blocks is known to carry
    one today — R1 adding a field to one is the case this is here for.
    """
    cleaned, removed = scrub.scrub(blocks)
    if removed:
        logger.warning("pisr: venue config carried credential-shaped field(s): "
                       "%s. Check whether that block should be fetched at all.",
                       ", ".join(sorted(set(removed))[:8]))
    return cleaned


def radius_server_profiles(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    RADIUS server profiles. TENANT-WIDE, not venue-scoped — R1 offers no venue
    filter, so this is every profile on the EC and the UI says so.

    CARRIES SHARED SECRETS. `primary.sharedSecret` and `secondary.sharedSecret`
    come back in plaintext, so they are removed HERE rather than only at the
    report boundary.

    Why both: `redact.redact` scrubs every report on the way out and is the
    guarantee, but between `build_report` and that boundary the secret would
    sit in the report object — which `checks.run_checks` walks, and which
    copies fields into finding evidence. Scrubbing at the fetch means the
    credential never enters the report at all, and the boundary scrub goes back
    to being what it is meant to be: a backstop that normally finds nothing.
    """
    rows = _rows(_json(_get(r1, "/radiusServerProfiles", tenant_id),
                       "radiusServerProfiles", []))
    cleaned, removed = scrub.scrub(rows)
    if removed:
        logger.debug("pisr: removed %d shared secret(s) from RADIUS profiles at "
                     "the fetch, as expected", len(removed))
    return cleaned


def ap_group_config(r1, tenant_id: Optional[str], venue_id: str,
                    group_ids: List[str]) -> Dict[str, Any]:
    """Group detail plus the sub-resources that carry a useVenueSettings flag."""
    out: Dict[str, Any] = {}
    for group_id in group_ids:
        base = f"/venues/{venue_id}/apGroups/{group_id}"
        entry: Dict[str, Any] = {
            "detail": _json(_get(r1, base, tenant_id), f"apGroup {group_id}", None)}
        for key, path in AP_GROUP_CONFIG_SOURCES.items():
            entry[key] = _json(_get(r1, f"{base}/{path}", tenant_id),
                               f"apGroup {group_id} {path}", None)
        out[group_id] = entry
    return out


def ap_config(r1, tenant_id: Optional[str], serial: str) -> Optional[Dict[str, Any]]:
    """
    One AP's full configuration, including its per-radio parameters and the
    `useVenueSettings` flags that say what it overrides.

    NOTE the path: `/venues/aps/{serial}`, with no venue id. The sibling
    `/venues/{venueId}/aps/{serial}` also exists and returns something quite
    different — a three-key object whose payload is the AP's plaintext
    `loginPassword`. That one is never called from here.
    """
    return _json(_get(r1, f"/venues/aps/{serial}", tenant_id),
                 f"ap config {serial}", None)


# What `/alarms/query` will actually return. Sent explicitly because the
# endpoint echoes back only the names it recognises and silently drops the rest
# — which is how this list was established in the first place, by offering it a
# wide set and reading what came back.
#
# Notably absent, and worth knowing before someone goes looking: there is no
# status, no clearedTime and no acknowledged flag. What this returns is the
# ACTIVE alarm list, not a history, so PISR cannot say when something cleared
# or whether anyone has looked at it.
ALARM_FIELDS = [
    "id", "name", "message", "reason", "severity",
    "entityType", "entityId", "serialNumber", "apMac", "model",
    "venueId", "startTime",
]


def incidents(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    """
    The alarms RUCKUS ONE is currently raising for this venue.

    This is the list behind the bell in the R1 console — what the platform has
    already decided is worth someone's attention, as opposed to what PISR works
    out for itself in `checks.py`. The two are worth showing side by side: a
    venue can pass every check PISR runs and still have R1 shouting about it.

    NOT the AI "Incidents" feature from RUCKUS Analytics, which is a separate
    licensed product. `/events/query` exists as an endpoint but returned
    EVENT-10002 ("something went wrong retrieving events") on every attempt
    against a live tenant, so it is not used here.

    Venue filtering is real, not decorative: `filters.venueId` cut a 16-alarm
    tenant to the 4 that belong to one venue. Verified live 2026-08-28.

    Read-only: a `*/query` POST.
    """
    body = _json(_post(r1, "/alarms/query",
                       {"fields": ALARM_FIELDS, "page": 1, "pageSize": 200,
                        "filters": {"venueId": [venue_id]}},
                       tenant_id),
                 "venue alarms", [])
    return _rows(body)


def wired_clients(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    """
    The switch MAC address table — one row per address a switch has learned.

    This is the only wired-client view R1 offers. `/venues/aps/clients/query`
    above is AP-scoped despite its service being named for both, so without
    this the report can see every wireless association and nothing at all
    plugged into a wall.

    A ROW IS A LEARNED MAC, NOT A CLIENT, and the difference is the whole
    reason `shape.wired_client_card` exists rather than a bare tally. An AP's
    uplink port has learned every wireless client behind it; a port feeding
    another switch has learned everything behind that; the APs and switches
    are in here as addresses themselves. Counting rows would report a number
    several times larger than the number of things actually plugged in.

    `clientIpv4Addr` is the only MAC->IP binding R1 exposes — there is no ARP
    endpoint — and its coverage varies a lot by tenant, so treat a missing
    address as unknown rather than as "no IP".

    Read-only: a `*/query` POST, like every other call in this file.
    """
    return r1.switches.crawl_mac_table(tenant_id, [venue_id]) or []


# ── wireless ─────────────────────────────────────────────────

def wifi_networks(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every Wi-Fi network defined on the tenant. Deliberately a direct query
    rather than NetworkService.get_wifi_networks: that one is `async def` around
    blocking calls, and PISR fans out through threads instead.

    PAGINATED ON PURPOSE. This used to be a single call for page 1 of 500, which
    silently truncated the tenant to its first 500 networks by name. That is not
    a hypothetical limit on the venues PISR is pointed at: a per-unit-SSID
    property defines one network per unit, so a few hundred units already runs
    past a page. Every activation whose network fell off the end failed to join
    in `wireless_card` and rendered as a bare network id with no SSID, no
    security and nothing on the air — which is exactly what it looked like.

    The page sequence starts at 1 because the endpoint is 1-indexed (page 0 is
    an alias for page 1, the same quirk `/venues/aps/clients/query` has).
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()

    for page in range(1, NETWORK_PAGE_LIMIT + 1):
        body = {"fields": NETWORK_FIELDS, "sortField": "name", "sortOrder": "ASC",
                "page": page, "pageSize": NETWORK_PAGE_SIZE}
        rows = _rows(_json(_post(r1, "/wifiNetworks/query", body, tenant_id),
                           f"/wifiNetworks/query page={page}", []))
        if not rows:
            break

        fresh = 0
        for row in rows:
            network_id = row.get("id")
            if network_id:
                if network_id in seen:
                    continue
                seen.add(network_id)
            out.append(row)
            fresh += 1

        if len(rows) < NETWORK_PAGE_SIZE:
            break
        if not fresh:
            # A full page of duplicates means paging stopped advancing; stop
            # rather than spin to the backstop.
            logger.warning("pisr: /wifiNetworks/query page=%s was all duplicates, stopping", page)
            break
    else:
        logger.warning("pisr: /wifiNetworks/query hit the %s-page backstop (%s networks) — "
                       "the tenant may have more", NETWORK_PAGE_LIMIT, len(out))

    return out


def venue_activations(r1, tenant_id: Optional[str], venue_id: str) -> List[Dict[str, Any]]:
    """
    Which networks are activated on this venue, with the VLAN and radios each
    one runs at — venue-wide or per AP group. This is the config side of "is
    the SSID actually deployed here".
    """
    body = {"venueIds": [venue_id]}
    payload = _json(_post(r1, "/venues/wifiNetworks/query", body, tenant_id),
                    "/venues/wifiNetworks/query", {})
    for row in _rows(payload):
        if row.get("venueId") == venue_id:
            return row.get("networks") or []
    rows = _rows(payload)
    return (rows[0].get("networks") or []) if rows else []


# ── DPSK / identity ──────────────────────────────────────────

# /dpskServices/query and /dpskServices/{id}/passphrases/query both return
# HTTP 500 on page=0 — not 400, a genuine server error. They are 1-indexed and
# page 0 is not an alias for page 1 here, unlike the client query. Verified
# live 2026-08-26.
DPSK_FIRST_PAGE = 1

# GET /identityGroups is 0-indexed and honours size; the query form does not.
IDENTITY_GROUP_PAGE_SIZE = 100
IDENTITY_GROUP_PAGE_LIMIT = 50

# Fields that must never reach the report. The pool and group DTOs are broad —
# passphrase DTOs carry the secret itself plus email, phone and username, and an
# identity group embeds an `identities` array. PISR summarises DPSK; it never
# reproduces a credential or a resident's details. See shape._dpsk_safe.
DPSK_FORBIDDEN_KEYS = {
    "passphrase", "devicepassphrase", "email", "phonenumber", "username",
    "identities", "mac", "macaddress", "identityid", "identityname",
}


def dpsk_pools(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every DPSK pool on the tenant, each carrying the network ids it backs.

    `networkIds` on the pool is what makes venue scoping cheap: the alternative,
    GET /wifiNetworks/{id}/dpskServices, is one call per network, and a
    per-unit-SSID property has hundreds.
    """
    body = {"page": DPSK_FIRST_PAGE, "pageSize": 200}
    return _rows(_json(_post(r1, "/dpskServices/query", body, tenant_id),
                       "/dpskServices/query", []))


def identity_groups_all(r1, tenant_id: Optional[str]) -> Dict[str, Any]:
    """
    Every identity group on the tenant, walked page by page.

    Uses GET /identityGroups, not POST /identityGroups/query. The query form
    IGNORES `page` and `pageSize` — it reports `size: 20`, `number: 0` whatever
    you send, and pages 0..3 return identical rows — so on a tenant with more
    groups than its internal default the tail is simply unreachable. The GET
    honours `page`/`size`, is 0-INDEXED, and sets `last` on the final page, so
    walking it yields exactly `totalElements`. Verified live 2026-08-26.

    Completeness is not cosmetic here. The pool->group link exists only on the
    group (`dpskPoolId`); a pool's own `identityGroupId` is null on every
    tenant tested. So a group we failed to fetch makes its pool look orphaned,
    and a pool that IS orphaned looks identical to one we simply under-fetched.
    Only a provably complete walk can tell those apart.

    Returns {"rows": [...], "total": int, "complete": bool}.
    """
    rows: List[Dict[str, Any]] = []
    total: Optional[int] = None
    page = 0
    while page <= IDENTITY_GROUP_PAGE_LIMIT:
        payload = _json(_get(r1, "/identityGroups", tenant_id,
                             params={"page": page, "size": IDENTITY_GROUP_PAGE_SIZE}),
                        f"GET /identityGroups page={page}", {}) or {}
        batch = _rows(payload)
        if isinstance(payload, dict) and isinstance(payload.get("totalElements"), int):
            total = payload["totalElements"]
        rows.extend(batch)
        if not batch or (isinstance(payload, dict) and payload.get("last")):
            break
        page += 1

    if total is None:
        total = len(rows)
    complete = len(rows) >= total
    if not complete:
        logger.warning("pisr: walked /identityGroups and got %s of %s groups — "
                       "DPSK pool linkage cannot be trusted", len(rows), total)
    return {"rows": rows, "total": total, "complete": complete}


def dpsk_passphrase_count(r1, tenant_id: Optional[str], pool_id: str) -> Optional[int]:
    """
    How many passphrases a pool holds — the COUNT only.

    pageSize is 1 on purpose. The rows this endpoint returns contain the
    passphrase itself along with the email, phone number and username attached
    to it, and none of that has any business being in a site report. Only
    totalCount is read; the single row that comes back is discarded here rather
    than carried any further.
    """
    body = {"page": DPSK_FIRST_PAGE, "pageSize": 1}
    payload = _json(_post(r1, f"/dpskServices/{pool_id}/passphrases/query", body, tenant_id),
                    f"passphrases/query {pool_id}", {})
    if not isinstance(payload, dict):
        return None
    for key in ("totalCount", "totalElements"):
        if isinstance(payload.get(key), int):
            return payload[key]
    return None


# ── adaptive policy / RADIUS attributes ──────────────────────

# The hierarchy, as the API actually exposes it:
#
#   DPSK pool ──policySetId──┐
#   identity group ──────────┴─► policy set ──prioritizedPolicies──► policy
#                                     │                                │
#                            externalAssignments[]        onMatchResponse
#                            (back to the pool)                        ▼
#                                                        RADIUS attribute group
#                                                        └─ attributeAssignments[]
#                                                           (WISPr rate limits)
#
# `onMatchResponse` on a policy IS the RADIUS attribute group id — a direct
# forward link, and the one to count by. The reverse view,
# /radiusAttributeGroups/{id}/assignments, PAGINATES at 10: a group with 50
# policies behind it reports 10 assignment rows, so it must never be used as a
# reference count. It is still worth reading for one thing — an assignment row
# pointing at a policy that no longer exists is the orphan that pins a group
# and makes the UI show "0 policies" while refusing to delete it with a 409.

POLICY_PAGE_SIZE = 500


def policy_sets(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Adaptive policy sets, with the counts and names R1 already denormalises.

    GET, not POST /policySets/query — the query form returns zero rows on every
    tenant tested while the GET returns the full list.
    """
    return _rows(_json(_get(r1, "/policySets", tenant_id), "GET /policySets", []))


def adaptive_policies(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every adaptive policy on the tenant, across both policy templates.

    /policyTemplates names a policy TYPE, not the MSP template system: 100 is
    DPSK conditions, 200 is RADIUS conditions, both returning
    RADIUS_ATTRIB_GROUP. This aggregate endpoint spans them and is the only
    shape that carries `onMatchResponse`; the per-template
    /policyTemplates/{id}/policies returns a thinner row whose
    `radiusAttributeGroupId` is always null.

    0-INDEXED and honours `size` (page 1 of 200 returns nothing when there are
    54 policies). Note that is the opposite of /dpskServices/query, which 500s
    on page 0.
    """
    return _rows(_json(_get(r1, "/policyTemplates/policies", tenant_id,
                            params={"page": 0, "size": POLICY_PAGE_SIZE}),
                       "GET /policyTemplates/policies", []))


def radius_attribute_groups(r1, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    """RADIUS attribute groups — the rate tiers a policy hands back on match."""
    return _rows(_json(_get(r1, "/radiusAttributeGroups", tenant_id),
                       "GET /radiusAttributeGroups", []))


def policy_set_members(r1, tenant_id: Optional[str], set_id: str) -> List[Dict[str, Any]]:
    """The policies in one set, with their evaluation priority."""
    return _rows(_json(_get(r1, f"/policySets/{set_id}/prioritizedPolicies", tenant_id),
                       f"prioritizedPolicies {set_id}", []))


def radius_group_assignments(r1, tenant_id: Optional[str], group_id: str) -> List[Dict[str, Any]]:
    """
    Raw assignment rows for a RADIUS attribute group.

    Paginated at 10 and NOT a reference count — see the note above. Read only
    to spot rows whose `externalAssignmentIdentifier` names a policy that no
    longer exists.
    """
    return _rows(_json(_get(r1, f"/radiusAttributeGroups/{group_id}/assignments", tenant_id),
                       f"radiusAttributeGroups/{group_id}/assignments", []))

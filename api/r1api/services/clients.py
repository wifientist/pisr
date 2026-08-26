"""
R1 API Service for Network Clients (Wireless/Wired Clients)
Handles all client-related API calls to RuckusONE

Primary endpoint: POST /venues/aps/clients/query
Historical endpoint: POST /historicalClients/query
"""

from typing import Dict, Any, Optional, List
import logging

logger = logging.getLogger(__name__)


class ClientsService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    def query_all_clients_for_venue(
        self,
        tenant_id: str,
        venue_id: str,
        fields: list = None,
        page_size: int = 1000,
    ):
        """
        Enumerate every client currently visible in a single venue.

        R1's /venues/aps/clients/query has three quirks we have to work around:

        0. **The obvious venue filter does nothing.** `filters.venueId` is
           accepted and silently ignored: a deliberately bogus venue id returns
           the same totalCount as sending no filter at all, so this helper was
           returning every client in the TENANT for years while looking
           per-venue. The nested `filters.venueInformation.id` is honoured (a
           bogus id there returns 0). Verified live 2026-08-26 across four
           tenants. `filters.apInformation.serialNumber` also works if a
           per-AP scope is ever needed; the flat `apSerialNumber` does not.

        1. **Off-by-one on page 0/1.** `page=0` and `page=1` both return the
           same first block — the endpoint is effectively 1-indexed but
           silently accepts page=0 as an alias. Iteration has to skip page 1.
           (Verified empirically 2026-04-15 by walking pages 0..10 on a
           10k-client venue: page 0 and 1 were identical, pages 2-10 advanced
           cleanly by 1000 rows each.)

        2. **Elasticsearch max_result_window = 10000.** The query backend is
           ES; page 11 returns HTTP 400 with the literal ES error
           "Result window is too large, from + size must be less than or
           equal to: [10000] but was [11000]". So even with working
           pagination, we can collect at most 10,000 rows per venue query.
           If a venue genuinely has more than 10k active clients, we'll need
           to partition the query (rangeDateFilter on lastSeenTime is the
           likely path) or use a working scroll/PIT cursor — neither is
           wired up today.

        When this helper returns exactly 10,000 rows, log a warning — the
        venue may have more clients than we're seeing.

        Args:
            tenant_id: Tenant/EC ID. Required for MSP-scoped clients.
            venue_id: Venue ID to query. Required — this helper is per-venue.
            fields: AP fields to request. Defaults to ["macAddress"].
                    macAddress is always appended if missing because it's
                    used as the dedup key.
            page_size: Rows per request. 1000 is the endpoint's ceiling; any
                    higher is silently clamped.

        Returns:
            List of client dicts (deduped by macAddress). Empty list on error.

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        if not venue_id:
            return []

        fields = list(fields) if fields else ["macAddress"]
        if "macAddress" not in fields:
            fields.append("macAddress")
        # Always ask for the venue so the scoping can be re-checked locally
        # rather than trusted — see the row filter below.
        if "venueInformation" not in fields:
            fields.append("venueInformation")

        all_clients: list = []
        seen: set = set()
        off_venue = 0

        # Effective page sequence: 0, 2, 3, 4, ..., 10.
        # Stop at page 10 — page 11 triggers ES max_result_window (10*1000=10000).
        max_window_pages = 10000 // page_size  # e.g. 10 when page_size=1000
        pages = [0] + list(range(2, max_window_pages + 1))

        for page in pages:
            body = {
                "fields": fields,
                # NOT `venueId`. This endpoint silently ignores a flat
                # `venueId` filter — a deliberately bogus venue id returns the
                # same totalCount as no filter at all, so every "per-venue"
                # client query was really returning the whole tenant. The
                # nested dotted path IS honoured (a bogus id there returns 0).
                # Verified against a live tenant 2026-08-26.
                "filters": {"venueInformation.id": [venue_id]},
                "sortField": "macAddress",
                "sortOrder": "ASC",
                "page": page,
                "pageSize": page_size,
            }

            if self.client.ec_type == "MSP":
                resp = self.client.post(
                    "/venues/aps/clients/query",
                    payload=body,
                    override_tenant_id=tenant_id,
                )
            else:
                resp = self.client.post("/venues/aps/clients/query", payload=body)

            if not resp.ok:
                logger.warning(
                    f"[query_all_clients_for_venue] tenant={tenant_id} "
                    f"venue={venue_id} page={page} HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                break

            data = resp.json() or {}
            rows = data.get("data") or []
            if not rows:
                break

            new_in_page = 0
            for row in rows:
                mac = row.get("macAddress")
                if mac:
                    if mac in seen:
                        continue
                    seen.add(mac)
                    new_in_page += 1
                # Belt and braces on the server-side filter. R1 has already
                # been caught ignoring one client filter key; if the nested one
                # ever goes the same way this keeps the result venue-scoped
                # instead of silently widening to the tenant. Rows with no
                # venue reported are kept — dropping them would lose real
                # clients whenever the projection comes back partial.
                venue = row.get("venueInformation")
                if isinstance(venue, dict) and venue.get("id") and venue["id"] != venue_id:
                    off_venue += 1
                    continue
                all_clients.append(row)

            if len(rows) < page_size:
                break

            # Defensive: if a page is 100% duplicates but wasn't page 1, the
            # server behavior changed and we should bail rather than spin.
            if new_in_page == 0 and page > 1:
                logger.warning(
                    f"[query_all_clients_for_venue] tenant={tenant_id} "
                    f"venue={venue_id} page={page} returned {len(rows)} "
                    f"rows but 0 new — aborting"
                )
                break

        if len(all_clients) == 10000:
            logger.warning(
                f"[query_all_clients_for_venue] tenant={tenant_id} "
                f"venue={venue_id} hit ES max_result_window (10000) — venue "
                f"may have additional clients that require time-range "
                f"partitioning to retrieve"
            )

        if off_venue:
            logger.warning(
                f"[query_all_clients_for_venue] tenant={tenant_id} "
                f"venue={venue_id} dropped {off_venue} row(s) belonging to another "
                f"venue — the server-side venue filter is not being honoured"
            )

        logger.info(
            f"[query_all_clients_for_venue] tenant={tenant_id} "
            f"venue={venue_id} fetched {len(all_clients)} unique clients"
        )
        return all_clients

    async def get_active_clients(
        self,
        tenant_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        ssid: Optional[str] = None,
        ap_mac: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Get currently active/connected network clients
        Uses filters to narrow down to specific venue, SSID, or AP

        Args:
            tenant_id: Tenant ID to filter by (for MSP accounts)
            venue_id: Filter by venue ID
            ssid: Filter by SSID name
            ap_mac: Filter by AP MAC address
            limit: Max number of clients to return

        Returns:
            List of active client objects
        """
        match_fields = []
        filters = {}

        # Build match fields for specific criteria
        if venue_id:
            match_fields.append({"field": "venueId", "value": venue_id})

        if ssid:
            match_fields.append({"field": "ssid", "value": ssid})

        if ap_mac:
            match_fields.append({"field": "apMac", "value": ap_mac})

        # Filter for active/connected clients only
        # NOTE: Commenting out status filter to see if it's causing issues
        # Different APIs might use different status values
        # filters["status"] = ["CONNECTED", "ONLINE"]

        fields = [
            "clientMac", "hostname", "ipAddress", "apMac", "apName",
            "ssid", "venueName", "manufacturer", "osType", "deviceType",
            "connectionType", "radio", "channel", "rssi", "snr",
            "txRate", "rxRate", "status"
        ]

        payload = {
            "fields": fields,
            "sortField": "lastSeenTime",
            "sortOrder": "DESC",
            "page": 1,
            "pageSize": limit
        }

        if match_fields:
            payload["matchFields"] = match_fields

        if filters:
            payload["filters"] = filters

        try:
            # Use override_tenant_id for MSP accounts
            if self.client.ec_type == "MSP" and tenant_id:
                response = self.client.post("/venues/aps/clients/query", payload=payload, override_tenant_id=tenant_id)
            else:
                response = self.client.post("/venues/aps/clients/query", payload=payload)

            data = response.json()
            return data.get('data', [])
        except Exception as e:
            logger.error(f"Error fetching active clients: {str(e)}")
            return []


async def query_ap_clients(
    r1_client,
    fields: Optional[List[str]] = None,
    search_string: Optional[str] = None,
    search_target_fields: Optional[List[str]] = None,
    match_fields: Optional[List[Dict[str, str]]] = None,
    filters: Optional[Dict[str, List[str]]] = None,
    sort_field: Optional[str] = None,
    sort_order: str = "ASC",
    page: int = 1,
    page_size: int = 100
) -> Dict[str, Any]:
    """
    Query AP clients from RuckusONE (PRIMARY METHOD)
    Uses POST /venues/aps/clients/query

    Args:
        r1_client: The authenticated R1 API client instance
        fields: List of fields to return
        search_string: Search string to filter clients
        search_target_fields: Fields to search within
        match_fields: List of match field objects [{"field": "fieldName", "value": "value"}]
        filters: Additional filters as key-value pairs with arrays
        sort_field: Field to sort by
        sort_order: ASC or DESC
        page: Page number
        page_size: Number of results per page

    Returns:
        Dictionary with 'data', 'totalCount', 'page', etc.
    """
    try:
        endpoint = "/venues/aps/clients/query"

        # Default fields if none provided - comprehensive list for diagnostics
        if fields is None:
            fields = [
                "clientMac",
                "hostname",
                "ipAddress",
                "apMac",
                "apName",
                "ssid",
                "venueName",
                "venueId",
                "manufacturer",
                "osType",
                "deviceType",
                "userName",
                "connectionType",  # wired/wireless
                "radio",  # 2.4GHz, 5GHz, 6GHz
                "channel",
                "channelWidth",
                "rssi",
                "snr",
                "txRate",
                "rxRate",
                "txBytes",
                "rxBytes",
                "txPackets",
                "rxPackets",
                "txRetries",
                "rxRetries",
                "status",
                "connectTime",
                "lastSeenTime",
                "sessionDuration",
                "vlan",
                "authMethod",
                "encryption"
            ]

        payload = {
            "fields": fields,
            "sortField": sort_field or "lastSeenTime",
            "sortOrder": sort_order,
            "page": page,
            "pageSize": page_size
        }

        # Add optional parameters
        if search_string:
            payload["searchString"] = search_string

        if search_target_fields:
            payload["searchTargetFields"] = search_target_fields

        if match_fields:
            payload["matchFields"] = match_fields

        if filters:
            payload["filters"] = filters

        response = await r1_client.post(endpoint, json=payload)

        if response.status_code == 200:
            data = response.json()
            logger.info(f"Retrieved {len(data.get('data', []))} AP clients")
            return data
        else:
            logger.error(f"Failed to query AP clients: {response.status_code}")
            logger.error(f"Response: {response.text}")
            return {'data': [], 'totalCount': 0, 'page': page}

    except Exception as e:
        logger.error(f"Error querying AP clients: {str(e)}")
        return {'data': [], 'totalCount': 0, 'page': page}


async def query_historical_clients(
    r1_client,
    fields: Optional[List[str]] = None,
    search_string: Optional[str] = None,
    search_target_fields: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    sort_field: Optional[str] = None,
    sort_order: str = "ASC",
    page: int = 1,
    page_size: int = 100,
    detail_level: Optional[str] = None
) -> Dict[str, Any]:
    """
    Query historical network clients from RuckusONE

    Args:
        r1_client: The authenticated R1 API client instance
        fields: List of fields to return
        search_string: Search string to filter clients
        search_target_fields: Fields to search within
        filters: Additional filters as key-value pairs
        sort_field: Field to sort by
        sort_order: ASC or DESC
        page: Page number
        page_size: Number of results per page
        detail_level: Level of detail in response

    Returns:
        Dictionary with 'data', 'totalCount', 'page', etc.
    """
    try:
        endpoint = "/historicalClients/query"

        # Default fields if none provided
        if fields is None:
            fields = [
                "clientMac",
                "hostname",
                "ipAddress",
                "apMac",
                "apName",
                "ssid",
                "venueName",
                "manufacturer",
                "osType",
                "connectionType",  # wired/wireless
                "radio",  # 2.4GHz, 5GHz, 6GHz
                "rssi",
                "snr",
                "channel",
                "channelWidth",
                "txRate",
                "rxRate",
                "status",
                "connectTime",
                "lastSeenTime"
            ]

        payload = {
            "fields": fields,
            "sortField": sort_field or "clientMac",
            "sortOrder": sort_order,
            "page": page,
            "pageSize": page_size
        }

        # Add optional parameters
        if search_string:
            payload["searchString"] = search_string

        if search_target_fields:
            payload["searchTargetFields"] = search_target_fields

        if filters:
            payload["filters"] = filters

        if detail_level:
            payload["detailLevel"] = detail_level

        response = await r1_client.post(endpoint, json=payload)

        if response.status_code == 200:
            data = response.json()
            logger.info(f"Retrieved {len(data.get('data', []))} historical clients")
            return data
        else:
            logger.error(f"Failed to query historical clients: {response.status_code}")
            logger.error(f"Response: {response.text}")
            return {'data': [], 'totalCount': 0, 'page': page}

    except Exception as e:
        logger.error(f"Error querying historical clients: {str(e)}")
        return {'data': [], 'totalCount': 0, 'page': page}


async def get_active_clients(
    r1_client,
    venue_id: Optional[str] = None,
    ssid: Optional[str] = None,
    ap_mac: Optional[str] = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Get currently active/connected network clients
    Uses filters to narrow down to specific venue, SSID, or AP

    Args:
        r1_client: The authenticated R1 API client instance
        venue_id: Filter by venue ID
        ssid: Filter by SSID name
        ap_mac: Filter by AP MAC address
        limit: Max number of clients to return

    Returns:
        List of active client objects
    """
    match_fields = []
    filters = {}

    # Build match fields for specific criteria
    if venue_id:
        match_fields.append({"field": "venueId", "value": venue_id})

    if ssid:
        match_fields.append({"field": "ssid", "value": ssid})

    if ap_mac:
        match_fields.append({"field": "apMac", "value": ap_mac})

    # Filter for active/connected clients only
    # Note: Field name might be "status", "state", or "connectionState"
    # May need to adjust based on actual API response
    filters["status"] = ["CONNECTED", "ONLINE"]  # Try both common values

    result = await query_ap_clients(
        r1_client,
        match_fields=match_fields if match_fields else None,
        filters=filters,
        page_size=limit,
        sort_field="lastSeenTime",
        sort_order="DESC"
    )

    return result.get('data', [])


async def get_client_by_mac(
    r1_client,
    client_mac: str
) -> Optional[Dict[str, Any]]:
    """
    Get a specific network client by MAC address

    Args:
        r1_client: The authenticated R1 API client instance
        client_mac: MAC address of the client

    Returns:
        Client object or None if not found
    """
    result = await query_historical_clients(
        r1_client,
        search_string=client_mac,
        search_target_fields=["clientMac"],
        page_size=1
    )

    clients = result.get('data', [])
    return clients[0] if clients else None


async def get_clients_by_ssid(
    r1_client,
    ssid: str,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Get all clients connected to a specific SSID

    Args:
        r1_client: The authenticated R1 API client instance
        ssid: SSID name
        limit: Max number of clients to return

    Returns:
        List of clients connected to the SSID
    """
    return await get_active_clients(
        r1_client,
        ssid=ssid,
        limit=limit
    )


async def get_clients_by_ap(
    r1_client,
    ap_mac: str,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Get all clients connected to a specific AP

    Args:
        r1_client: The authenticated R1 API client instance
        ap_mac: AP MAC address
        limit: Max number of clients to return

    Returns:
        List of clients connected to the AP
    """
    return await get_active_clients(
        r1_client,
        ap_mac=ap_mac,
        limit=limit
    )

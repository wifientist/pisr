"""
R1 API Service for Floor Plans and AP floor positions.

Backs the Maps tool: floor plan metadata, the signed URL for the plan image,
and the AP-position + live-client data that gets overlaid on it.

Endpoints used:
  GET  /venues/{venueId}/floorplans                 — plan list (id, imageId, scales)
  GET  /venues/{venueId}/signurls/{fileId}/urls     — signed download URL for the image
  POST /venues/aps/query                            — APs incl. floorPosition + radioStatuses
  POST /venues/aps/clients/query                    — live clients incl. signalStatus.rssi
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Fields we need off /venues/aps/query to place an AP and describe its radios.
AP_MAP_FIELDS = [
    "serialNumber",
    "name",
    "model",
    "status",
    "clientCount",
    "apGroupId",
    "venueId",
    "floorPosition",
    "radioStatuses",
    "lastSeenTime",
]

# Fields we need off /venues/aps/clients/query to bucket clients under their AP.
CLIENT_MAP_FIELDS = [
    "macAddress",
    "hostname",
    "ipAddress",
    "osType",
    "deviceType",
    "band",
    "connectedTime",
    "apInformation",
    "signalStatus",
    "radioStatus",
    "networkInformation",
    "trafficStatus",
    "venueInformation",
]


class FloorplanService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    # ── internals ────────────────────────────────────────────

    def _get(self, path: str, tenant_id: Optional[str]):
        """GET that adds the MSP tenant override only when the client is MSP-scoped."""
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(path, override_tenant_id=tenant_id)
        return self.client.get(path)

    def _post(self, path: str, payload: dict, tenant_id: Optional[str]):
        """POST that adds the MSP tenant override only when the client is MSP-scoped."""
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.post(path, payload=payload, override_tenant_id=tenant_id)
        return self.client.post(path, payload=payload)

    # ── floor plans ──────────────────────────────────────────

    def list_floorplans(self, tenant_id: str, venue_id: str) -> List[Dict[str, Any]]:
        """
        All floor plans defined on a venue.

        Each plan carries `imageId` (feed it to get_image_url) and `scales` — a
        list of calibration segments whose x1/y1→x2/y2 span a known real-world
        distance. Scales are optional; a venue admin may never have calibrated
        the plan, in which case distances can't be drawn to scale.

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        resp = self._get(f"/venues/{venue_id}/floorplans", tenant_id)
        if not resp.ok:
            logger.warning(
                f"[list_floorplans] tenant={tenant_id} venue={venue_id} "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return []

        data = resp.json() or []
        # The endpoint returns a bare array, but tolerate a wrapped {data: [...]}.
        if isinstance(data, dict):
            data = data.get("data") or []
        return data

    def get_image_url(self, tenant_id: str, venue_id: str, file_id: str) -> Optional[str]:
        """
        Signed, time-limited download URL for a floor plan image.

        The URL points at R1's object store, not the API host, and it expires —
        so don't cache it. The Maps router streams through it rather than
        handing it to the browser, which keeps the signed URL server-side.

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        resp = self._get(f"/venues/{venue_id}/signurls/{file_id}/urls", tenant_id)
        if not resp.ok:
            logger.warning(
                f"[get_image_url] tenant={tenant_id} venue={venue_id} file={file_id} "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return None
        return (resp.json() or {}).get("signedUrl")

    # ── APs and clients for the overlay ──────────────────────

    def query_aps_with_positions(
        self,
        tenant_id: str,
        venue_id: str,
        page_size: int = 10000,
    ) -> List[Dict[str, Any]]:
        """
        Every AP in a venue, including its floor plan position.

        `floorPosition` is {floorplanId, xPercent, yPercent} and is absent for
        APs the admin never dragged onto a plan — those are returned too, so the
        caller can report how many APs are unplaced.

        Queried per-venue on purpose: the tenant-wide form of /venues/aps/query
        silently caps at ~1000 rows (see VenueService.query_all_aps_by_tenant).

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        body = {
            "fields": AP_MAP_FIELDS,
            "filters": {"venueId": [venue_id]},
            "page": 0,
            "pageSize": page_size,
        }
        resp = self._post("/venues/aps/query", body, tenant_id)
        if not resp.ok:
            logger.warning(
                f"[query_aps_with_positions] tenant={tenant_id} venue={venue_id} "
                f"HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return []

        data = resp.json() or {}
        rows = data.get("data") or []
        reported = data.get("totalCount", len(rows))
        if reported > len(rows):
            logger.warning(
                f"[query_aps_with_positions] tenant={tenant_id} venue={venue_id} "
                f"truncated: got {len(rows)} of reported {reported} APs"
            )
        return rows

    def query_live_clients(
        self,
        tenant_id: str,
        venue_id: str,
        page_size: int = 1000,
        max_pages: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Clients currently associated in a venue, with RSSI/SNR per client.

        Same two quirks as ClientsService.query_all_clients_for_venue: the
        endpoint is effectively 1-indexed (page 0 and page 1 return the same
        block, so page 1 is skipped) and the ES backend refuses page*size beyond
        10000. A venue with more than 10k live clients returns a truncated set;
        the caller surfaces that as a warning rather than silently under-drawing
        the map.

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        all_clients: List[Dict[str, Any]] = []
        seen: set = set()

        window_pages = min(max_pages, 10000 // page_size)
        pages = [0] + list(range(2, window_pages + 1))

        for page in pages:
            body = {
                "fields": CLIENT_MAP_FIELDS,
                # NOT `venueId` — on the CLIENTS endpoint that filter is
                # accepted and ignored, which would put every client in the
                # tenant onto one floor plan. (The AP query above is a
                # different endpoint and does honour `venueId`.) See
                # ClientsService.query_all_clients_for_venue.
                "filters": {"venueInformation.id": [venue_id]},
                "sortField": "macAddress",
                "sortOrder": "ASC",
                "page": page,
                "pageSize": page_size,
            }
            resp = self._post("/venues/aps/clients/query", body, tenant_id)
            if not resp.ok:
                logger.warning(
                    f"[query_live_clients] tenant={tenant_id} venue={venue_id} "
                    f"page={page} HTTP {resp.status_code}: {resp.text[:200]}"
                )
                break

            rows = (resp.json() or {}).get("data") or []
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
                # Re-check scope locally rather than trusting it; a client from
                # another venue drawn on this floor plan would be silent and wrong.
                venue = row.get("venueInformation")
                if isinstance(venue, dict) and venue.get("id") and venue["id"] != venue_id:
                    continue
                all_clients.append(row)

            if len(rows) < page_size:
                break
            if new_in_page == 0 and page > 1:
                logger.warning(
                    f"[query_live_clients] tenant={tenant_id} venue={venue_id} "
                    f"page={page} returned {len(rows)} rows but 0 new — aborting"
                )
                break

        return all_clients

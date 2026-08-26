import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)


class MspService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    @staticmethod
    def _json(response, what: str):
        """
        Decode an R1 response, turning upstream failures into a 502 the UI can
        read. R1's MSP service answers a partial outage with a plain-text
        `503 no healthy upstream`; calling .json() on that raises deep in
        requests and the caller sees an opaque 500 with no hint of the cause.
        """
        if response.status_code >= 500:
            body = (response.text or "").strip()[:200]
            logger.warning(f"{what}: upstream {response.status_code} - {body}")
            raise HTTPException(
                502,
                f"RUCKUS ONE returned {response.status_code} for {what}"
                + (f": {body}" if body else ""),
            )
        try:
            # 4xx bodies are R1's own JSON errors; callers already pass those
            # through, so only an undecodable body is escalated here.
            return response.json()
        except ValueError:
            body = (response.text or "").strip()[:200]
            logger.warning(f"{what}: non-JSON body ({response.status_code}) - {body}")
            raise HTTPException(502, f"RUCKUS ONE sent a non-JSON reply for {what}: {body}")

    async def get_msp_ecs(self):
        logger.debug(f"get_msp_ecs called on client: {self.client}")
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        body = {
            'fields': ['check-all', 'id', 'name', 'tenantType', 'mspAdminCount', 'mspEcAdminCount'],
            'sortField': 'name',
            'sortOrder': 'ASC',
            'filters': {'tenantType': ['MSP_EC']}
        }
        return self._json(self.client.post("/mspecs/query", payload=body), "the MSP-EC list")

    async def get_msp_tech_partners(self):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        body = {
            'fields': ['check-all', 'id', 'name', 'tenantType', 'mspAdminCount', 'mspEcAdminCount'],
            'sortField': 'name',
            'sortOrder': 'ASC',
            'filters': {'tenantType': ['MSP_INSTALLER', 'MSP_INTEGRATOR']}
        }
        return self._json(self.client.post("/techpartners/mspecs/query", payload=body), "the tech-partner list")

    async def get_msp_labels(self):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        logger.debug("Fetching MSP labels")
        response = self.client.get("/mspLabels")
        logger.debug(f"MSP labels response: {response.status_code}")

        if response.ok:
            try:
                return response.json()
            except ValueError:
                logger.warning("Failed to decode JSON from MSP labels response")
                return None
        else:
            logger.warning(f"Failed to fetch MSP labels: {response.status_code}")
            return None

    async def get_entitlements(self): #, r1_client: R1Client = None):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        return self._json(self.client.get("/entitlements"), "entitlements")

    async def get_msp_entitlements(self): #, r1_client: R1Client = None):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        return self._json(self.client.get("/mspEntitlements"), "MSP entitlements")

    async def get_msp_admins(self): #, r1_client: R1Client = None):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        return self._json(self.client.get("/admins"), "MSP admins")

    async def get_msp_customer_admins(self, tenant_id: str): #, r1_client: R1Client = None):
        if self.client.ec_type != "MSP":
            return {"success": False, "error": "Unavailable for non-MSP clients."}
        return self._json(
            self.client.get(f"/mspCustomers/{tenant_id}/admins", override_tenant_id=tenant_id),
            "customer admins",
        )

    def get_inventory_summary(self, page_size: int = 1000, max_pages: int = 200):
        """
        Pull the MSP-wide device inventory via /tenants/inventories/query and
        return a summary for reconciliation against per-tenant fanouts.

        This is a diagnostic: single MSP-level query, follows `subsequentQueries`
        cursor links if present, aggregates counts by connectionStatus /
        deviceType / customer. Not intended as a primary data source — use
        per-venue fanouts for anything the dashboard actually renders.

        Returns a dict of the shape:
            {
                "total_count": int,          # totalCount reported by R1
                "fetched": int,              # unique devices we actually collected
                "pages": int,
                "by_connection_status": {status: count},
                "by_device_type": {type: count},
                "by_customer": {customerName: count},
                "error": str or None,
            }

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        summary = {
            "total_count": 0,
            "fetched": 0,
            "pages": 0,
            "by_connection_status": {},
            "by_device_type": {},
            "by_customer": {},
            "error": None,
        }

        logger.info(
            f"[inventory_summary] start ec_type={self.client.ec_type} "
            f"page_size={page_size}"
        )

        if self.client.ec_type != "MSP":
            summary["error"] = "non-MSP client"
            logger.info("[inventory_summary] skipped: non-MSP client")
            return summary

        # Match the style of the known-working /mspecs/query call:
        # include sortField/sortOrder and an (empty) filters dict. R1's MSP
        # query validator rejects payloads missing these with a generic
        # MSP-10001 error.
        body = {
            "fields": [
                "apMac",
                "deviceType",
                "deviceStatus",
                "connectionStatus",
                "customerName",
            ],
            "sortField": "apMac",
            "sortOrder": "ASC",
            "filters": {},
            "page": 0,
            "pageSize": page_size,
        }

        seen_macs: set = set()

        def absorb(rows: list):
            for d in rows:
                mac = d.get("apMac")
                if mac:
                    if mac in seen_macs:
                        continue
                    seen_macs.add(mac)
                summary["fetched"] += 1

                cs = d.get("connectionStatus") or "Unknown"
                summary["by_connection_status"][cs] = (
                    summary["by_connection_status"].get(cs, 0) + 1
                )

                dt = d.get("deviceType") or "Unknown"
                summary["by_device_type"][dt] = (
                    summary["by_device_type"].get(dt, 0) + 1
                )

                cn = d.get("customerName") or "Unknown"
                summary["by_customer"][cn] = (
                    summary["by_customer"].get(cn, 0) + 1
                )

        try:
            logger.info("[inventory_summary] POST /tenants/inventories/query")
            resp = self.client.post("/tenants/inventories/query", payload=body)
            logger.info(f"[inventory_summary] initial response status={resp.status_code}")
            if not resp.ok:
                summary["error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(
                    f"[inventory_summary] initial query failed: {summary['error']}"
                )
                return summary

            data = resp.json() or {}
            summary["total_count"] = int(data.get("totalCount") or 0)
            absorb(data.get("data") or [])
            summary["pages"] = 1

            # Follow self-describing subsequentQueries links until exhausted.
            while summary["pages"] < max_pages:
                subs = data.get("subsequentQueries") or []
                if not subs:
                    break
                nxt = subs[0] or {}
                url = nxt.get("url") or ""
                method = (nxt.get("httpMethod") or "POST").upper()
                payload = nxt.get("payload")

                if not url:
                    break

                # url may be absolute; strip host so R1Client prepends correctly.
                if url.startswith("http"):
                    from urllib.parse import urlparse
                    url = urlparse(url).path

                if method == "POST":
                    resp = self.client.post(url, payload=payload)
                elif method == "GET":
                    resp = self.client.get(url)
                else:
                    logger.warning(
                        f"[inventory_summary] unsupported subsequent method: {method}"
                    )
                    break

                if not resp.ok:
                    summary["error"] = (
                        f"page {summary['pages'] + 1} HTTP {resp.status_code}"
                    )
                    logger.warning(
                        f"[inventory_summary] {summary['error']}: {resp.text[:200]}"
                    )
                    break

                data = resp.json() or {}
                absorb(data.get("data") or [])
                summary["pages"] += 1

                if summary["fetched"] >= summary["total_count"] > 0:
                    break
        except Exception as exc:
            summary["error"] = str(exc)
            logger.exception("[inventory_summary] unhandled error")

        logger.info(
            f"[inventory_summary] totalCount={summary['total_count']} "
            f"fetched={summary['fetched']} pages={summary['pages']} "
            f"by_connection_status={summary['by_connection_status']}"
        )
        return summary

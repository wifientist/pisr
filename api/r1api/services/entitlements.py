"""
Entitlements Service for RuckusONE API
Handles license and entitlement operations
"""
import asyncio
import logging
from datetime import datetime, timedelta, date

logger = logging.getLogger(__name__)

# R1 hands back dates in at least three shapes across the entitlement
# endpoints — Java's Date.toString(), ISO-8601 with offset, and a bare day.
_DATE_FORMATS = (
    "%a %b %d %H:%M:%S %Z %Y",       # Thu Dec 12 23:59:59 UTC 2030
    "%Y-%m-%dT%H:%M:%S.%f%z",        # 2025-12-12T00:00:00.000+00:00
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d",                      # 2028-02-25
)


def parse_r1_date(value):
    """Parse any of R1's entitlement date formats into a date, or None."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        logger.warning(f"[licensing] unparseable date: {value!r}")
        return None


class EntitlementsService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    def get_msp_compliance_summary(self):
        """
        Fetch MSP-wide license compliance via /entitlements/compliances/query.

        Returns a shaped dict ready for the frontend:
            {
                "license_type": "APSW",
                "total_paid": int,            # total active paid licenses in pool
                "used": int,                  # licenses currently consumed
                "available": int,             # headroom (licenseGap)
                "expiring_soon": int,         # next batch of paid licenses to expire
                "next_expiration_date": str,  # ISO date of next expiration (or None)
                "consolidated_sku_enabled": bool,
                "device_breakdown": [
                    {"device_type": "WIFI", "installed": 48406, "used": 48406},
                    ...
                ],
                "self_licenses": int,         # MSP's own direct licenses (mostly 0)
                "raw": {...}                  # full mspEcSummary for anything not mapped
            }

        Uses {"filters": {"complianceType": "MSP_SUMMARY"}} — complianceType is
        marked deprecated in the spec but is the only filter that returns real
        data. Empty filters / licenseType-only filters return compliances:[null].

        Only applicable to MSP controllers; returns a minimal placeholder for
        non-MSP clients rather than raising so the UI can gracefully degrade.

        Note: sync. Call via asyncio.to_thread from async contexts.
        """
        if self.client.ec_type != "MSP":
            return {
                "license_type": None,
                "error": "non-MSP client",
            }

        body = {"filters": {"complianceType": "MSP_SUMMARY"}}

        try:
            resp = self.client.post("/entitlements/compliances/query", payload=body)
        except Exception as exc:
            logger.exception("[compliance_summary] request raised")
            return {"error": f"{type(exc).__name__}: {exc}"}

        if not resp.ok:
            logger.warning(
                f"[compliance_summary] HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return {"error": f"HTTP {resp.status_code}"}

        payload = resp.json() or {}
        compliances = payload.get("compliances") or []
        # Filter out null entries — some filter combinations return [null].
        compliances = [c for c in compliances if c]

        if not compliances:
            logger.info("[compliance_summary] empty compliances array")
            return {"error": "no compliance data returned"}

        # The endpoint returns one entry per licenseType. Today that's just
        # APSW, but handle multiple gracefully by preferring APSW if present.
        entry = next(
            (c for c in compliances if c.get("licenseType") == "APSW"),
            compliances[0],
        )

        msp_summary = entry.get("mspEcSummary") or {}
        self_summary = entry.get("self") or {}

        device_breakdown = []
        for dev in msp_summary.get("deviceCompliances") or []:
            device_breakdown.append(
                {
                    "device_type": dev.get("deviceType"),
                    "installed": int(dev.get("installedDeviceCount") or 0),
                    "used": int(dev.get("usedLicenseCount") or 0),
                }
            )

        result = {
            "license_type": entry.get("licenseType"),
            "tenant_name": msp_summary.get("tenantName"),
            "total_paid": int(msp_summary.get("totalActivePaidLicenseCount") or 0),
            "used": int(msp_summary.get("licensesUsed") or 0),
            "available": int(msp_summary.get("licenseGap") or 0),
            "expiring_soon": int(
                msp_summary.get("nextTotalPaidExpiringLicenseCount") or 0
            ),
            "next_expiration_date": msp_summary.get("nextPaidExpirationDate"),
            "consolidated_sku_enabled": bool(
                msp_summary.get("consolidatedSkuEnabled")
            ),
            "extended_trial_enabled": bool(msp_summary.get("extendedTrialEnabled")),
            "device_breakdown": device_breakdown,
            "self_licenses": int(
                self_summary.get("totalActivePaidAssignedLicenseCount") or 0
            ),
        }

        logger.info(
            f"[compliance_summary] total_paid={result['total_paid']} "
            f"used={result['used']} available={result['available']} "
            f"expiring_soon={result['expiring_soon']}"
        )
        return result

    async def check_license_availability(
        self,
        license_type: str = "APSW",
        quantity: int = None,
        effective_date: str = None,
        expiration_date: str = None,
        usage_type: str = "SELF",
        tenant_id: str = None
    ):
        """
        Check license availability for AP software licenses

        Args:
            license_type: Type of license (default: "APSW" for AP Software)
            quantity: Number of licenses needed (required for MAX_PERIOD operator)
            effective_date: Start date (format: YYYY-MM-DD)
            expiration_date: End date (format: YYYY-MM-DD, required for MAX_QUANTITY operator)
            usage_type: Usage type - "SELF", "ASSIGNED", or "UNKNOWN"
            tenant_id: Optional tenant ID (required for MSP)

        Returns:
            License availability report with quantity and dates
        """

        # Determine operator based on what we're checking
        if quantity is not None:
            # Check MAX_PERIOD: given a quantity, how long can we use it?
            operator = "MAX_PERIOD"
            # Default expiration date not needed for MAX_PERIOD
        else:
            # Check MAX_QUANTITY: given a date range, how many licenses?
            operator = "MAX_QUANTITY"
            # Ensure expiration_date is provided
            if not expiration_date:
                # Default to 1 year from effective date
                if effective_date:
                    eff_date = datetime.strptime(effective_date, "%Y-%m-%d")
                else:
                    eff_date = datetime.now()
                exp_date = eff_date + timedelta(days=365)
                expiration_date = exp_date.strftime("%Y-%m-%d")

        # Default effective date to today
        if not effective_date:
            effective_date = datetime.now().strftime("%Y-%m-%d")

        # Build request payload.
        # `licenseType` must be a LIST — v2 deserializes it into a Java Set and
        # a bare string 400s with a Jackson "cannot construct HashSet" error.
        # `usageType` is marked deprecated in the spec but is still mandatory;
        # omitting it 400s with "'usageType' is mandatory."
        payload = {
            "effectiveDate": effective_date,
            "operator": operator,
            "filters": {
                "usageType": usage_type,
                "licenseType": (
                    license_type if isinstance(license_type, list) else [license_type]
                ),
            }
        }

        # Add conditional fields based on operator
        if operator == "MAX_QUANTITY":
            payload["expirationDate"] = expiration_date
        elif operator == "MAX_PERIOD":
            payload["quantity"] = quantity

        logger.debug(f"Checking license availability with operator {operator}")
        logger.debug(f"Request Payload: {payload}")

        # Make API call
        if self.client.ec_type == "MSP" and tenant_id:
            logger.debug(f"Making MSP request with tenant_id override: {tenant_id}")
            response = self.client.post(
                "/entitlements/availabilityReports/query",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            logger.debug(f"Making EC request (no tenant_id override)")
            response = self.client.post(
                "/entitlements/availabilityReports/query",
                payload=payload
            )

        logger.debug(f"Response Status Code: {response.status_code}")

        result = response.json()
        logger.debug(f"License availability response: {result}")

        return result

    async def get_license_utilization(self, tenant_id: str = None):
        """
        Get license utilization (allocated vs used) for AP software licenses

        Args:
            tenant_id: Optional tenant ID (required for MSP)

        Returns:
            Dict with license utilization data including allocated and used counts
        """
        logger.debug(f"get_license_utilization called - tenant_id: {tenant_id}")

        # Build request payload for utilization query
        payload = {
            "filters": {
                "licenseType": ["APSW"],
                "isTrial": False,
                "status": ["VALID"],
                "isAssignedLicense": False,
                "usageType": "SELF"
            }
        }

        logger.debug(f"Utilization Request Payload: {payload}")

        # Make API call
        if self.client.ec_type == "MSP" and tenant_id:
            logger.debug(f"Making MSP utilization request with tenant_id override: {tenant_id}")
            response = self.client.post(
                "/entitlements/utilizations/query",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            logger.debug(f"Making EC utilization request (no tenant_id override)")
            response = self.client.post(
                "/entitlements/utilizations/query",
                payload=payload
            )

        logger.debug(f"Response Status Code: {response.status_code}")

        result = response.json()
        logger.debug(f"License utilization response: {result}")

        return result

    async def get_available_ap_licenses(self, tenant_id: str = None):
        """
        Get the current count of available AP software licenses

        Args:
            tenant_id: Optional tenant ID (required for MSP)

        Returns:
            Dict with 'available', 'total', and 'used' license counts
        """
        logger.debug(f"get_available_ap_licenses called - tenant_id: {tenant_id}")

        try:
            # Get utilization data
            result = await self.get_license_utilization(tenant_id=tenant_id)

            # Extract available licenses from utilization data
            # The response should have data array with license info
            if isinstance(result, dict) and 'data' in result:
                data_list = result.get('data', [])
                logger.debug(f"Found {len(data_list)} license entries")

                # Sum up available licenses from all entries
                # The API provides: quantity (total), usedQuantity (in use), remainingQuantity (available)
                total_quantity = 0
                total_used = 0
                total_remaining = 0

                for entry in data_list:
                    logger.debug(f"License entry: {entry}")
                    quantity = entry.get('quantity', 0)
                    used = entry.get('usedQuantity', 0)
                    remaining = entry.get('remainingQuantity', 0)

                    total_quantity += quantity
                    total_used += used
                    total_remaining += remaining

                # Use remainingQuantity if available, otherwise calculate as quantity - usedQuantity
                if total_remaining > 0:
                    available = total_remaining
                    logger.debug(f"Using remainingQuantity: {available} (Total: {total_quantity}, Used: {total_used})")
                else:
                    available = total_quantity - total_used
                    logger.debug(f"Calculated available: {available} (Total: {total_quantity}, Used: {total_used})")

                return {
                    'available': available,
                    'total': total_quantity,
                    'used': total_used
                }
            else:
                logger.warning(f"Unexpected result format, returning 0")
                return {'available': 0, 'total': 0, 'used': 0}
        except Exception as e:
            logger.exception(f"Error in get_available_ap_licenses: {str(e)}")
            raise

    # ------------------------------------------------------------------
    # MSP licensing overview (used by the R1 Details > MSP Licensing tool)
    # ------------------------------------------------------------------

    @staticmethod
    def _capacity_at(blocks, when):
        """Licensed quantity in force on a given day."""
        return sum(
            b["quantity"] for b in blocks
            if b["effective_date"] and b["expiration_date"]
            and b["effective_date"] <= when <= b["expiration_date"]
        )

    @classmethod
    def _build_timeline(cls, blocks, today):
        """
        Turn a set of license blocks into a capacity step function plus the
        list of expiration cliffs, both looking forward from today.

        R1's own summaries collapse a mixed-term pool to a single row carrying
        the *earliest* expiration, which hides longer-dated licenses entirely.
        The step function is what makes that tail visible.
        """
        live = [
            b for b in blocks
            if b["effective_date"] and b["expiration_date"]
            and b["expiration_date"] >= today
        ]
        if not live:
            return [], [], {}

        # Segment boundaries: today, every future start, and the day after
        # every expiration (capacity changes on those days).
        points = {today}
        for b in live:
            if b["effective_date"] > today:
                points.add(b["effective_date"])
            points.add(b["expiration_date"] + timedelta(days=1))
        points = sorted(points)

        segments = []
        for start, nxt in zip(points, points[1:]):
            capacity = cls._capacity_at(live, start)
            segments.append({
                "start": start.isoformat(),
                "end": (nxt - timedelta(days=1)).isoformat(),
                "capacity": capacity,
                "days": (nxt - start).days,
            })

        cliffs = []
        for exp in sorted({b["expiration_date"] for b in live}):
            lost = sum(
                b["quantity"] for b in live
                if b["expiration_date"] == exp and b["effective_date"] <= exp
            )
            after = cls._capacity_at(live, exp + timedelta(days=1))
            cliffs.append({
                "date": exp.isoformat(),
                "days_out": (exp - today).days,
                "quantity_lost": lost,
                "capacity_after": after,
                "skus": sorted({
                    b["sku"] for b in live
                    if b["expiration_date"] == exp and b.get("sku")
                }),
            })

        capacity_today = cls._capacity_at(live, today)
        first = cliffs[0] if cliffs else None
        last_exp = max(b["expiration_date"] for b in live)

        summary = {
            "capacity_today": capacity_today,
            # The headline number: the soonest date capacity drops. This is
            # what R1 reports as "the" expiration date.
            "effective_expiration": first["date"] if first else None,
            "days_to_effective_expiration": first["days_out"] if first else None,
            "capacity_after_first_cliff": first["capacity_after"] if first else None,
            "last_expiration": last_exp.isoformat(),
            "days_to_last_expiration": (last_exp - today).days,
            # How much runway the longest-dated licenses have beyond the cliff
            # — the time R1's summary throws away.
            "tail_days": (
                (last_exp - date.fromisoformat(first["date"])).days if first else 0
            ),
            "cliff_count": len(cliffs),
        }
        return segments, cliffs, summary

    @staticmethod
    def _normalize_pool(raw):
        """Shape /mspEntitlements rows into blocks the timeline can consume."""
        blocks = []
        for e in raw or []:
            if not isinstance(e, dict):
                continue
            eff = parse_r1_date(e.get("effectiveDate"))
            exp = parse_r1_date(e.get("expirationDate"))
            term_days = (exp - eff).days if eff and exp else None
            blocks.append({
                "id": e.get("id"),
                "sku": e.get("sku"),
                "sku_tier": e.get("skuTier"),
                "device_type": e.get("deviceType"),
                "quantity": int(e.get("quantity") or 0),
                "effective_date": eff,
                "expiration_date": exp,
                "term_days": term_days,
                "term_years": round(term_days / 365.25, 1) if term_days else None,
                "status": e.get("status"),
                "is_trial": bool(e.get("isTrial")),
            })
        return blocks

    @staticmethod
    def _serialize_blocks(blocks, today):
        out = []
        for b in blocks:
            eff, exp, rev = (
                b["effective_date"], b["expiration_date"], b.get("revoked_date")
            )
            out.append({
                **{k: v for k, v in b.items()
                   if k not in ("effective_date", "expiration_date", "revoked_date")},
                "effective_date": eff.isoformat() if eff else None,
                "expiration_date": exp.isoformat() if exp else None,
                "revoked_date": rev.isoformat() if rev else None,
                "days_remaining": (exp - today).days if exp else None,
                "expired": bool(exp and exp < today),
            })
        return out

    @classmethod
    def _combined_timeline(cls, pool, committed, today):
        """
        Supply and demand on one shared set of breakpoints.

        A pool cliff only matters if it cuts below what is actually committed to
        end customers, and the reverse gap — pool that outlives every assignment
        — is idle spend. Neither is visible from the pool curve alone.
        """
        live_pool = [b for b in pool
                     if b["effective_date"] and b["expiration_date"]
                     and b["expiration_date"] >= today]
        live_com = [b for b in committed
                    if b["effective_date"] and b["expiration_date"]
                    and b["expiration_date"] >= today]
        if not live_pool and not live_com:
            return []

        points = {today}
        for b in live_pool + live_com:
            if b["effective_date"] > today:
                points.add(b["effective_date"])
            points.add(b["expiration_date"] + timedelta(days=1))
        points = sorted(points)

        segments = []
        for start, nxt in zip(points, points[1:]):
            capacity = cls._capacity_at(live_pool, start)
            com = cls._capacity_at(live_com, start)
            segments.append({
                "start": start.isoformat(),
                "end": (nxt - timedelta(days=1)).isoformat(),
                "capacity": capacity,
                "committed": com,
                "headroom": capacity - com,
            })
        return segments

    @staticmethod
    def _quarter_buckets(committed, today):
        """
        Assigned licenses grouped by the quarter they expire in, with the
        per-EC breakdown behind each bar. Shows where renewal work piles up.
        """
        buckets = {}
        for b in committed:
            exp = b["expiration_date"]
            if not exp or exp < today:
                continue
            key = f"{exp.year}-Q{(exp.month - 1) // 3 + 1}"
            entry = buckets.setdefault(key, {"quarter": key, "total": 0, "by_ec": {}})
            entry["total"] += b["quantity"]
            entry["by_ec"][b["ec_name"]] = (
                entry["by_ec"].get(b["ec_name"], 0) + b["quantity"]
            )

        if not buckets:
            return []

        # Fill the empty quarters between the first and last so the gaps are
        # visible as gaps rather than silently collapsed.
        def parse_q(k):
            y, q = k.split("-Q")
            return int(y), int(q)

        keys = sorted(buckets, key=parse_q)
        (y0, q0), (y1, q1) = parse_q(keys[0]), parse_q(keys[-1])
        out = []
        y, q = y0, q0
        while (y, q) <= (y1, q1):
            key = f"{y}-Q{q}"
            entry = buckets.get(key, {"quarter": key, "total": 0, "by_ec": {}})
            out.append({
                "quarter": key,
                "total": entry["total"],
                "by_ec": sorted(
                    ({"name": n, "quantity": v} for n, v in entry["by_ec"].items()),
                    key=lambda r: -r["quantity"],
                ),
            })
            q += 1
            if q > 4:
                y, q = y + 1, 1
        return out

    @staticmethod
    def _churn_series(rows, today):
        """
        How many licenses this EC held over time, as a step series.

        An assignment is in force from its effective date until it is revoked,
        or until it expires if it never was. Reassignments (the common case —
        one EC had eight rows for one live assignment) show up as the steps.
        """
        spans = []
        for r in rows:
            start = r["effective_date"]
            if not start:
                continue
            end = r["revoked_date"] or (
                r["expiration_date"] + timedelta(days=1)
                if r["expiration_date"] else None
            )
            if end and end < start:
                end = start
            spans.append((start, end, r["quantity"]))

        if not spans:
            return []

        points = sorted({s for s, _, _ in spans} | {e for _, e, _ in spans if e})
        points = [p for p in points if p <= today] or [points[0]]
        if points[-1] < today:
            points.append(today)

        series = []
        for p in points:
            qty = sum(q for s, e, q in spans if s <= p and (e is None or p < e))
            # Collapse runs of equal value — only the steps carry information.
            if series and series[-1]["quantity"] == qty:
                continue
            series.append({"date": p.isoformat(), "quantity": qty})
        return series

    def _fetch_ec_assignments(self, tenant_id, page_size=200, max_pages=10):
        """
        All entitlement assignments held by one MSP_EC.

        Must be called in MSP context with the EC in the path — passing
        override_tenant_id makes R1 treat the EC as the logged-in tenant and it
        rejects the call with ENTITLEMENT-10200.
        """
        rows, page = [], 1
        while page <= max_pages:
            body = {
                "page": page,
                "pageSize": page_size,
                "sortField": "expirationDate",
                "sortOrder": "ASC",
                "filters": {},
            }
            resp = self.client.post(
                f"/tenants/{tenant_id}/entitlements/assignments/query", payload=body
            )
            if not resp.ok:
                logger.warning(
                    f"[licensing] assignments {tenant_id} HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
                return rows, f"HTTP {resp.status_code}"
            payload = resp.json() or {}
            batch = payload.get("data") or []
            rows.extend(batch)
            total = int(payload.get("totalCount") or 0)
            if len(rows) >= total or not batch:
                break
            page += 1
        return rows, None

    def _summarize_ec(self, tenant_id, name, today):
        """Per-EC license position: what's live, when it lapses, what's behind it."""
        raw, error = self._fetch_ec_assignments(tenant_id)

        blocks, historical = [], []
        for a in raw:
            eff = parse_r1_date(a.get("effectiveDate"))
            exp = parse_r1_date(a.get("expirationDate"))
            term_days = (exp - eff).days if eff and exp else None
            row = {
                "id": a.get("id"),
                "sku": a.get("skuTier"),
                "license_type": a.get("licenseType"),
                "quantity": int(a.get("quantity") or 0),
                "effective_date": eff,
                "expiration_date": exp,
                "term_days": term_days,
                "term_years": round(term_days / 365.25, 1) if term_days else None,
                "status": a.get("status"),
                "is_trial": bool(a.get("isTrial")),
                "created_by": a.get("createdBy"),
                "revoked_date": parse_r1_date(a.get("revokedDate")),
            }
            # REVOKED assignments were superseded by a later one; EXPIRED are
            # past. Neither counts toward current capacity, but both are worth
            # keeping as an assignment history for the EC.
            if a.get("status") in ("VALID", "FUTURE"):
                blocks.append(row)
            else:
                historical.append(row)

        segments, cliffs, summary = self._build_timeline(blocks, today)

        return {
            "tenant_id": tenant_id,
            "name": name,
            "error": error,
            "quantity": sum(b["quantity"] for b in blocks),
            "assignment_count": len(blocks),
            "historical_count": len(historical),
            "license_types": sorted({
                b["license_type"] for b in blocks if b.get("license_type")
            }),
            "assignments": self._serialize_blocks(blocks, today),
            "history": self._serialize_blocks(historical, today),
            "timeline": segments,
            "cliffs": cliffs,
            "churn": self._churn_series(blocks + historical, today),
            **summary,
        }

    async def get_msp_licensing_overview(self, include_ecs: bool = True,
                                         concurrency: int = 8):
        """
        Everything the MSP Licensing tool renders, in one shot.

        Combines three sources:
          - GET  /mspEntitlements                 the purchased license pool
          - POST /entitlements/compliances/query  MSP-wide device counts vs licenses
          - POST /mspecs/query + per-EC assignments query

        The value over R1's native view is the capacity *step function*: a pool
        of 40 three-year and 20 five-year licenses is reported by R1 as a single
        60-license line expiring on the earlier date, which silently discards
        almost three years of runway on a third of the pool.
        """
        if self.client.ec_type != "MSP":
            return {"error": "MSP licensing requires an MSP-level controller."}

        today = datetime.utcnow().date()
        result = {"as_of": today.isoformat(), "warnings": []}

        # --- pool -----------------------------------------------------
        try:
            resp = await asyncio.to_thread(self.client.get, "/mspEntitlements")
            pool_raw = resp.json() if resp.ok else []
            if not resp.ok:
                result["warnings"].append(f"mspEntitlements HTTP {resp.status_code}")
        except Exception as exc:
            logger.exception("[licensing] pool fetch failed")
            return {"error": f"Could not load MSP entitlements: {exc}"}

        pool = self._normalize_pool(pool_raw)
        segments, cliffs, summary = self._build_timeline(pool, today)

        result["pool"] = {
            "blocks": self._serialize_blocks(pool, today),
            "timeline": segments,
            "cliffs": cliffs,
            "purchased": sum(b["quantity"] for b in pool),
            "block_count": len(pool),
            "trial_quantity": sum(b["quantity"] for b in pool if b["is_trial"]),
            **summary,
        }

        # --- MSP-level compliance (device counts vs licenses) ---------
        try:
            compliance = await asyncio.to_thread(self.get_msp_compliance_summary)
        except Exception as exc:
            logger.exception("[licensing] compliance fetch failed")
            compliance = {"error": str(exc)}
        if compliance.get("error"):
            result["warnings"].append(f"compliance: {compliance['error']}")
        result["compliance"] = compliance

        # R1 counts courtesy grants in its active total but they never appear
        # as pool rows, so the two numbers legitimately disagree.
        total_active = compliance.get("total_paid")
        if isinstance(total_active, int) and result["pool"]["capacity_today"]:
            courtesy = total_active - result["pool"]["capacity_today"]
            result["pool"]["courtesy"] = courtesy if courtesy > 0 else 0

        if not include_ecs:
            result["ecs"] = []
            return result

        # --- per-EC assignments ---------------------------------------
        try:
            ecs_resp = await self.client.msp.get_msp_ecs()
        except Exception as exc:
            logger.exception("[licensing] EC list fetch failed")
            result["warnings"].append(f"EC list: {exc}")
            ecs_resp = {}

        ecs = (ecs_resp or {}).get("data") or []
        logger.info(f"[licensing] fanning out to {len(ecs)} MSP_ECs")

        semaphore = asyncio.Semaphore(concurrency)

        async def one(ec):
            async with semaphore:
                try:
                    return await asyncio.to_thread(
                        self._summarize_ec, ec.get("id"), ec.get("name"), today
                    )
                except Exception as exc:
                    logger.exception(f"[licensing] EC {ec.get('name')} failed")
                    return {
                        "tenant_id": ec.get("id"),
                        "name": ec.get("name"),
                        "error": str(exc),
                        "quantity": 0,
                        "assignments": [],
                        "history": [],
                        "cliffs": [],
                    }

        summaries = await asyncio.gather(*(one(ec) for ec in ecs))

        # Soonest to lapse first — that's the renewal work queue. ECs holding
        # nothing live sort to the bottom.
        summaries.sort(
            key=lambda e: (e.get("effective_expiration") is None,
                           e.get("effective_expiration") or "")
        )
        result["ecs"] = summaries
        result["assigned_total"] = sum(e.get("quantity") or 0 for e in summaries)

        # --- MSP-wide supply vs demand -------------------------------
        # Re-parse the serialized EC assignments into one flat committed set,
        # tagged with the EC that holds each one.
        committed = []
        for ec in summaries:
            for a in ec.get("assignments") or []:
                if not a.get("effective_date") or not a.get("expiration_date"):
                    continue
                committed.append({
                    "ec_name": ec.get("name"),
                    "tenant_id": ec.get("tenant_id"),
                    "quantity": a.get("quantity") or 0,
                    "effective_date": date.fromisoformat(a["effective_date"]),
                    "expiration_date": date.fromisoformat(a["expiration_date"]),
                })

        result["combined_timeline"] = self._combined_timeline(pool, committed, today)
        result["quarters"] = self._quarter_buckets(committed, today)

        # The pool can outlive every assignment — licenses paid for that no
        # customer is holding. Surface that as its own number.
        idle = next(
            (s for s in reversed(result["combined_timeline"]) if s["capacity"] > 0),
            None,
        )
        result["idle_tail"] = (
            {
                "from": idle["start"],
                "until": idle["end"],
                "quantity": idle["headroom"],
            }
            if idle and idle["committed"] == 0 and idle["capacity"] > 0
            else None
        )

        failed = [e["name"] for e in summaries if e.get("error")]
        if failed:
            result["warnings"].append(
                f"{len(failed)} EC(s) failed to load: {', '.join(failed[:5])}"
            )

        logger.info(
            f"[licensing] pool={result['pool']['purchased']} "
            f"capacity_today={result['pool']['capacity_today']} "
            f"assigned={result['assigned_total']} ecs={len(summaries)}"
        )
        return result

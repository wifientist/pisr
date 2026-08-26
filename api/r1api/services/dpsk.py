import logging

from r1api.constants import DpskPassphraseFormat

logger = logging.getLogger(__name__)


class DpskService:
    """
    Service for managing DPSK (Dynamic Pre-Shared Key) pools and passphrases in RuckusONE.

    DPSK enables unique pre-shared keys for different users/devices without complex
    authentication infrastructure. Each passphrase can be associated with user metadata,
    device limits, expiration, and adaptive policy sets.
    """

    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    # ========== DPSK Pool Management ==========

    async def query_dpsk_pools(
        self,
        tenant_id: str = None,
        search_string: str = None,
        page: int = 1,
        limit: int = 100
    ):
        """
        Search for DPSK pools matching search criteria

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            search_string: Optional search string to filter pools
            page: Page number (1-based, default: 1)
            limit: Number of results per page

        Returns:
            Query response with pools array and pagination info
        """
        # This endpoint is strictly 1-indexed and answers page=0 with a bare
        # HTTP 500 rather than a validation error, which reads like an R1
        # outage. Other query endpoints (e.g. /identityGroups/query) happily
        # accept page=0, so the inconsistency is easy to walk into — normalize
        # here so no caller has to know.
        if page is None or page < 1:
            logger.debug(
                f"query_dpsk_pools: page={page} is invalid for "
                f"/dpskServices/query (1-indexed); using page=1"
            )
            page = 1

        # Build request body matching OpenAPI spec exactly
        # NOTE: Extra fields like defaultPageSize, total cause 500 errors
        body = {
            "page": page,
            "pageSize": limit,
            "sortField": "name",
            "sortOrder": "ASC"
        }

        # Only add optional fields if they have values
        if search_string:
            body["searchString"] = search_string
            body["searchTargetFields"] = ["name"]

        logger.debug(f"query_dpsk_pools request body: {body}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post("/dpskServices/query", payload=body, override_tenant_id=tenant_id)
        else:
            response = self.client.post("/dpskServices/query", payload=body)
        return self.client.safe_json(response)

    async def get_dpsk_pool(self, pool_id: str, tenant_id: str = None):
        """
        Get a specific DPSK pool by ID

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            DPSK pool details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(f"/dpskServices/{pool_id}", override_tenant_id=tenant_id)
        else:
            response = self.client.get(f"/dpskServices/{pool_id}")
        return self.client.safe_json(response)

    async def create_dpsk_pool(
        self,
        identity_group_id: str,
        name: str,
        tenant_id: str = None,
        description: str = None,
        passphrase_length: int = 12,
        passphrase_format: str = None,
        max_devices_per_passphrase: int = 1,
        expiration_days: int = None,
        word_count: int = None,
        numeric_suffix_enabled: bool = None
    ):
        """
        Create a new DPSK pool within an identity group

        Args:
            identity_group_id: Identity group ID to create pool in
            name: Pool name
            tenant_id: Tenant/EC ID (required for MSP)
            description: Optional pool description
            passphrase_length: Length of auto-generated passphrases (default: 12)
            passphrase_format: One of DpskPassphraseFormat.ALL — NUMBERS_ONLY,
                KEYBOARD_FRIENDLY, MOST_SECURED, DICTIONARY_WORDS
            max_devices_per_passphrase: Max devices per passphrase (default: 1)
            expiration_days: Optional expiration in days
            word_count: Number of words for DICTIONARY_WORDS (3-6). Ignored by
                R1 for every other format, so it is only sent when relevant.
            numeric_suffix_enabled: Append a numeric suffix to dictionary-word
                passphrases. Also DICTIONARY_WORDS-only.

        Returns:
            Created DPSK pool response
        """
        payload = {
            "name": name,
            "passphraseLength": passphrase_length  # FIXED: Correct API field name
        }

        # Only set device count limit if > 0 (0 means unlimited, so omit the field)
        if max_devices_per_passphrase and max_devices_per_passphrase > 0:
            payload["deviceCountLimit"] = max_devices_per_passphrase

        if description:
            payload["description"] = description

        if passphrase_format:
            # RuckusONE API passphraseFormat enum values:
            # "NUMBERS_ONLY"      - numeric only (0-9)
            # "KEYBOARD_FRIENDLY" - alphanumeric (a-z, A-Z, 0-9)
            # "MOST_SECURED"      - complex (alphanumeric + symbols)
            # "DICTIONARY_WORDS"  - word-based, e.g. "otter-lantern-copper"
            payload["passphraseFormat"] = passphrase_format

            # wordCount / numericSuffixEnabled apply only to DICTIONARY_WORDS.
            # Sending them with another format is at best ignored, so scope
            # them rather than leaking meaningless fields into the payload.
            if passphrase_format == DpskPassphraseFormat.DICTIONARY_WORDS:
                if word_count is not None:
                    payload["wordCount"] = max(
                        DpskPassphraseFormat.MIN_WORD_COUNT,
                        min(DpskPassphraseFormat.MAX_WORD_COUNT, int(word_count)),
                    )
                if numeric_suffix_enabled is not None:
                    payload["numericSuffixEnabled"] = bool(numeric_suffix_enabled)

        if expiration_days:
            # API uses expirationType and expirationOffset
            payload["expirationType"] = "DAYS_AFTER_TIME"
            payload["expirationOffset"] = expiration_days

        logger.debug(f"create_dpsk_pool payload: {payload}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/identityGroups/{identity_group_id}/dpskServices",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/identityGroups/{identity_group_id}/dpskServices",
                payload=payload
            )

        return self.client.safe_json(response)

    async def update_dpsk_pool(
        self,
        pool_id: str,
        tenant_id: str = None,
        name: str = None,
        description: str = None,
        passphrase_length: int = None,
        max_devices_per_passphrase: int = None,
        expiration_days: int = None
    ):
        """
        Update an existing DPSK pool

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)
            name: Optional new name
            description: Optional new description
            passphrase_length: Optional new passphrase length
            max_devices_per_passphrase: Optional new max devices
            expiration_days: Optional new expiration days

        Returns:
            Updated DPSK pool response
        """
        payload = {}

        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        if passphrase_length is not None:
            # Use same field name as create - passphraseLength
            payload["passphraseLength"] = passphrase_length
        if max_devices_per_passphrase is not None:
            payload["deviceCountLimit"] = max_devices_per_passphrase
        if expiration_days is not None:
            payload["expirationType"] = "DAYS_AFTER_TIME"
            payload["expirationOffset"] = expiration_days

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/dpskServices/{pool_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/dpskServices/{pool_id}",
                payload=payload
            )

        return self.client.safe_json(response)

    async def delete_dpsk_pool(self, pool_id: str, tenant_id: str = None):
        """
        Delete a DPSK pool

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(f"/dpskServices/{pool_id}", override_tenant_id=tenant_id)
        else:
            response = self.client.delete(f"/dpskServices/{pool_id}")

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== DPSK Passphrase Management ==========

    async def get_passphrases(
        self,
        pool_id: str,
        tenant_id: str = None,
        page: int = 0,
        size: int = 100,
        sort: str = None
    ):
        """
        Get all passphrases in a DPSK pool (paginated)

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)
            page: Page number (0-based, Spring Data style)
            size: Page size
            sort: Optional sort criteria (e.g., "userName,asc")

        Returns:
            Passphrases list with pagination info
        """
        params = {
            "page": page,
            "size": size
        }

        if sort:
            params["sort"] = sort

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases",
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases",
                params=params
            )
        return self.client.safe_json(response)

    async def query_passphrases(
        self,
        pool_id: str,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 1,
        limit: int = 100,
        sort_field: str = "createdDate",
        sort_order: str = "DESC",
        search_string: str = None
    ):
        """
        Query passphrases with advanced filtering

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Dictionary of filters (e.g., {"status": ["ACTIVE"]})
            page: Page number (1-based, default: 1)
            limit: Number of results per page
            sort_field: Sort field (e.g., "username", "createdDate")
            sort_order: Sort order ("ASC" or "DESC")
            search_string: Optional search string

        Returns:
            Query response with passphrases
        """
        # Build request body matching OpenAPI spec
        # NOTE: Extra fields like defaultPageSize, total, maxDevicesPerPassphrase cause 500 errors
        body = {
            "page": page,
            "pageSize": limit,
            "sortField": sort_field,
            "sortOrder": sort_order
        }

        # Only add optional fields if they have values
        if search_string:
            body["searchString"] = search_string
            body["searchTargetFields"] = ["username", "mac"]

        # Add filters if provided (e.g., {"status": ["ACTIVE"]})
        if filters:
            body["filters"] = filters

        logger.debug(f"query_passphrases request body: {body}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def get_passphrase(
        self,
        pool_id: str,
        passphrase_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific passphrase by ID

        Args:
            pool_id: DPSK pool ID
            passphrase_id: Passphrase ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Passphrase details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}"
            )
        return self.client.safe_json(response)

    async def create_passphrase(
        self,
        pool_id: str,
        tenant_id: str = None,
        passphrase: str = None,
        user_name: str = None,
        user_email: str = None,
        description: str = None,
        expiration_date: str = None,
        max_devices: int = None,
        vlan_id: str = None
    ):
        """
        Create a new DPSK passphrase

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)
            passphrase: Optional custom passphrase (auto-generated if not provided)
            user_name: Optional username/identifier
            user_email: Optional user email
            description: Optional description
            expiration_date: Optional expiration date (ISO 8601 format)
            max_devices: Optional max devices override
            vlan_id: Optional VLAN ID

        Returns:
            Created passphrase response with id and identityId
        """
        payload = {}

        if passphrase:
            payload["passphrase"] = passphrase
        if user_name:
            payload["username"] = user_name  # FIXED: API uses 'username' not 'userName'
        if user_email:
            payload["email"] = user_email  # FIXED: API uses 'email' not 'userEmail'
        if description:
            payload["description"] = description
        if expiration_date:
            payload["expirationDate"] = expiration_date

        # Device limit: Set numberOfDevicesType explicitly
        if max_devices is not None and max_devices > 0:
            payload["numberOfDevices"] = max_devices  # FIXED: API uses 'numberOfDevices' not 'maxDevices'
            payload["numberOfDevicesType"] = "LIMITED"  # Required when numberOfDevices is set
        else:
            # Explicitly set to UNLIMITED when no device limit
            payload["numberOfDevicesType"] = "UNLIMITED"

        # VLAN ID - normalize for comparison later
        normalized_vlan = None
        if vlan_id is not None and vlan_id != '' and vlan_id != '0':
            try:
                normalized_vlan = int(vlan_id)
                payload["vlanId"] = normalized_vlan
            except (ValueError, TypeError):
                pass  # Skip invalid VLAN IDs

        logger.debug(f"create_passphrase payload: {payload}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases",
                payload=payload
            )

        # Raise exception on HTTP errors so callers can handle failures properly
        result = self.client.safe_json(response)

        # Handle 202 Accepted - async operation that returns requestId
        # We need to poll /activities/{requestId} until completion
        if response.status_code == 202:
            request_id = result.get('requestId')
            if request_id:
                logger.debug(f"create_passphrase returned 202, polling for completion: {request_id}")

                # Wait for the async task to complete
                # Use assume_success_on_timeout=True since POST succeeded - under heavy load
                # the activity may take longer to appear due to R1's eventual consistency
                # Stepped backoff: 1s×5 + 2s×10 + 3s×25 ≈ 100 seconds total
                await self.client.await_task_completion(
                    request_id=request_id,
                    override_tenant_id=tenant_id,
                    max_attempts=40,
                    assume_success_on_timeout=True
                )

                # Task completed - now we need to find the created passphrase
                # Note: The passphrase field is NOT searchable (security) - we must fetch all and filter
                #
                # This lookup is what supplies identityId, and identityId is what
                # lets the import write the Cloudpath GUID onto the identity
                # description afterwards. A single attempt is not enough: the
                # activity can be reported complete (or assumed complete on
                # timeout) before R1 has made the new passphrase readable, and
                # every miss silently drops that identity from the GUID update —
                # which showed up as roughly half of identities missing GUIDs.
                # Retry with backoff so a slow write does not lose the linkage.
                if passphrase:
                    import asyncio

                    lookup_delays = [0, 2, 4, 8]  # first pass immediate
                    found_passphrases = []

                    for attempt, delay in enumerate(lookup_delays):
                        if delay:
                            await asyncio.sleep(delay)

                        # Fetch all passphrases from pool (no search filter)
                        # The passphrase string is unique per pool, so we filter client-side
                        query_result = await self.query_passphrases(
                            pool_id=pool_id,
                            tenant_id=tenant_id,
                            page=1,
                            limit=500  # Fetch enough to find our passphrase
                        )

                        # Find the exact match by passphrase string (+ vlan for extra safety)
                        found_passphrases = query_result.get('data', [])
                        logger.debug(
                            f"Searching {len(found_passphrases)} passphrases for "
                            f"'{passphrase[:8]}...' (attempt {attempt + 1}/{len(lookup_delays)})"
                        )

                        for pp in found_passphrases:
                            pp_passphrase = pp.get('passphrase', '')

                            # Match by passphrase string (primary key)
                            if pp_passphrase == passphrase:
                                # Verify VLAN matches too (extra safety)
                                pp_vlan = pp.get('vlanId')
                                if pp_vlan is not None:
                                    try:
                                        pp_vlan = int(pp_vlan) if pp_vlan != '' and pp_vlan != 0 else None
                                    except (ValueError, TypeError):
                                        pp_vlan = None

                                if pp_vlan != normalized_vlan:
                                    # Passphrase matches but VLAN doesn't - still return it
                                    logger.debug(
                                        f"Found passphrase by string (VLAN mismatch): id={pp.get('id')}"
                                    )
                                elif attempt > 0:
                                    logger.info(
                                        f"Found created passphrase after {attempt + 1} attempts: "
                                        f"id={pp.get('id')}, identityId={pp.get('identityId')}"
                                    )
                                else:
                                    logger.debug(
                                        f"Found created passphrase: id={pp.get('id')}, "
                                        f"identityId={pp.get('identityId')}"
                                    )
                                return pp

                    # Passphrase not found after creation - this shouldn't happen.
                    # The caller gets no identityId, so this identity will not
                    # receive its Cloudpath GUID description.
                    logger.warning(
                        f"Passphrase created but not found in pool after "
                        f"{len(lookup_delays)} attempts: {passphrase[:8]}... "
                        f"(checked {len(found_passphrases)} passphrases) — "
                        f"its identity will be missing the Cloudpath GUID"
                    )
                    return {
                        "requestId": request_id,
                        "status": "created",
                        "passphrase": passphrase,
                        "_note": "Created async but could not fetch details"
                    }
                else:
                    # Auto-generated passphrase - can't easily find it
                    # Return the request info
                    logger.warning("Auto-generated passphrase created async - ID unknown")
                    return {
                        "requestId": request_id,
                        "status": "created",
                        "_note": "Created async with auto-generated passphrase"
                    }

        return result

    async def update_passphrase(
        self,
        pool_id: str,
        passphrase_id: str,
        tenant_id: str = None,
        user_name: str = None,
        user_email: str = None,
        description: str = None,
        expiration_date: str = None,
        max_devices: int = None,
        enabled: bool = None,
        vlan_id: int = None
    ):
        """
        Update an existing DPSK passphrase

        Args:
            pool_id: DPSK pool ID
            passphrase_id: Passphrase ID
            tenant_id: Tenant/EC ID (required for MSP)
            user_name: Optional new username
            user_email: Optional new email
            description: Optional new description
            expiration_date: Optional new expiration date
            max_devices: Optional new max devices
            enabled: Optional enabled/disabled state
            vlan_id: Optional VLAN ID override

        Returns:
            Updated passphrase response
        """
        payload = {}

        if user_name:
            payload["userName"] = user_name
        if user_email:
            payload["userEmail"] = user_email
        if description:
            payload["description"] = description
        if expiration_date:
            payload["expirationDate"] = expiration_date
        if max_devices is not None:
            payload["maxDevices"] = max_devices
        if enabled is not None:
            payload["enabled"] = enabled
        if vlan_id is not None:
            payload["vlanId"] = int(vlan_id)

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}",
                payload=payload
            )

        return self.client.safe_json(response)

    async def delete_passphrase(
        self,
        passphrase_id: str,
        pool_id: str,
        tenant_id: str = None
    ):
        """
        Delete a single passphrase

        Args:
            passphrase_id: Passphrase ID to delete
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        # R1 API uses bulk delete endpoint - single delete is just a one-item array
        return await self.delete_passphrases(
            pool_id=pool_id,
            passphrase_ids=[passphrase_id],
            tenant_id=tenant_id
        )

    async def delete_passphrases(
        self,
        pool_id: str,
        passphrase_ids: list,
        tenant_id: str = None
    ):
        """
        Delete one or more passphrases (bulk delete)

        Args:
            pool_id: DPSK pool ID
            passphrase_ids: List of passphrase IDs to delete
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        # R1 API expects array of passphrase IDs in request body
        payload = passphrase_ids

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/passphrases",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/passphrases",
                payload=payload
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== CSV Import/Export ==========

    async def import_passphrases_from_csv(
        self,
        pool_id: str,
        csv_content: str,
        tenant_id: str = None
    ):
        """
        Import passphrases from CSV file

        Args:
            pool_id: DPSK pool ID
            csv_content: CSV file content as string
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Import result with success/failure counts
        """
        # Note: This endpoint expects multipart/form-data with file upload
        # For now, we'll create a placeholder - actual implementation
        # would need to handle file uploads differently

        payload = {
            "csvContent": csv_content
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/csvFiles",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/csvFiles",
                payload=payload
            )

        return self.client.safe_json(response)

    async def export_passphrases_to_csv(
        self,
        pool_id: str,
        tenant_id: str = None,
        filters: dict = None
    ):
        """
        Export passphrases to CSV format

        Args:
            pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters to apply

        Returns:
            CSV content
        """
        body = {}

        if filters:
            body["filters"] = filters

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/query/csvFiles",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/query/csvFiles",
                payload=body
            )

        # Response is CSV content, not JSON
        return response.text

    # ========== Devices Management ==========

    async def get_passphrase_devices(
        self,
        pool_id: str,
        passphrase_id: str,
        tenant_id: str = None
    ):
        """
        Get all devices associated with a specific passphrase

        Args:
            pool_id: DPSK pool ID
            passphrase_id: Passphrase ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of devices using this passphrase
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices"
            )
        return self.client.safe_json(response)

    async def add_passphrase_device(
        self,
        pool_id: str,
        passphrase_id: str,
        mac_address: str,
        tenant_id: str = None,
        device_name: str = None
    ):
        """
        Add a device to a passphrase (MAC-based device limit)

        Args:
            pool_id: DPSK pool ID
            passphrase_id: Passphrase ID
            mac_address: Device MAC address
            tenant_id: Tenant/EC ID (required for MSP)
            device_name: Optional device name/description

        Returns:
            Created device response
        """
        payload = {
            "macAddress": mac_address
        }

        if device_name:
            payload["deviceName"] = device_name

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices",
                payload=payload
            )

        return self.client.safe_json(response)

    async def delete_passphrase_devices(
        self,
        pool_id: str,
        passphrase_id: str,
        device_ids: list,
        tenant_id: str = None
    ):
        """
        Delete devices from a passphrase

        Args:
            pool_id: DPSK pool ID
            passphrase_id: Passphrase ID
            device_ids: List of device IDs to delete
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        # Note: Actual endpoint might expect device IDs in query params or body
        # Adjust based on actual API behavior

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/passphrases/{passphrase_id}/devices"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== Policy Set Management ==========

    async def attach_policy_set_to_pool(
        self,
        pool_id: str,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Attach an adaptive policy set to a DPSK pool

        Args:
            pool_id: DPSK pool ID
            policy_set_id: Adaptive policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/dpskServices/{pool_id}/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/dpskServices/{pool_id}/policySets/{policy_set_id}"
            )

        if not response.content:
            return {"status": "attached"}
        return self.client.safe_json(response)

    async def remove_policy_set_from_pool(
        self,
        pool_id: str,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Remove an adaptive policy set from a DPSK pool

        Args:
            pool_id: DPSK pool ID
            policy_set_id: Adaptive policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/dpskServices/{pool_id}/policySets/{policy_set_id}"
            )

        if not response.content:
            return {"status": "removed"}
        return self.client.safe_json(response)

    # ========== WiFi Network Integration ==========

    async def activate_dpsk_on_wifi_network(
        self,
        wifi_network_id: str,
        dpsk_service_id: str,
        tenant_id: str = None
    ):
        """
        Activate a DPSK service on a Wi-Fi network

        Args:
            wifi_network_id: Wi-Fi network ID
            dpsk_service_id: DPSK service/pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/wifiNetworks/{wifi_network_id}/dpskServices/{dpsk_service_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/wifiNetworks/{wifi_network_id}/dpskServices/{dpsk_service_id}"
            )

        if not response.content:
            return {"status": "activated"}
        return self.client.safe_json(response)

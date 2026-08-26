import logging

from r1api.constants import (
    WifiNetworkType,
    WlanSecurity,
    SECURITY_TYPE_MAP,
    R1StatusCode
)

logger = logging.getLogger(__name__)


def _merge_advanced_settings(wlan_settings: dict, advanced: dict) -> None:
    """
    Deep-merge advanced settings from field_mappings.build_r1_advanced_settings()
    into the wlan_settings dict for an R1 network creation payload.

    The `advanced` dict has wlan-level keys (e.g. "managementFrameProtection")
    and nested objects (e.g. "advancedCustomization": {"enableFastRoaming": True}).
    This function deep-merges nested dicts rather than overwriting them.
    """
    for key, value in advanced.items():
        if isinstance(value, dict) and isinstance(wlan_settings.get(key), dict):
            # Deep merge nested dicts (e.g. advancedCustomization)
            _merge_advanced_settings(wlan_settings[key], value)
        elif isinstance(value, dict) and key not in wlan_settings:
            # New nested dict — copy it in
            wlan_settings[key] = value
        else:
            # Scalar or new key — set directly
            wlan_settings[key] = value


class NetworksService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    async def get_wifi_networks(self, tenant_id): #, r1_client: R1Client = None):
        """
        Get all WiFi networks for a tenant, handling pagination automatically.

        The query endpoint has pagination that may limit results. This method
        fetches all pages and returns the complete list.
        """
        fields = [
            "check-all",
            "name",
            "description",
            "nwSubType",
            "venues",
            "aps",
            "clients",
            "apCount",
            "clientCount",
            "vlan",
            "cog",
            "ssid",
            "vlanPool",
            "captiveType",
            "id",
            "isOweMaster",
            "owePairNetworkId",
            "dsaeOnboardNetwork",
            "venueApGroups",
            "securityProtocol",
            ]

        # Fetch first page to get totalCount
        body = {
            'fields': fields,
            'sortField': 'name',
            'sortOrder': 'ASC',
            'page': 1,
            'pageSize': 500
        }

        # Use override_tenant_id only for MSP accounts
        if self.client.ec_type == "MSP" and tenant_id:
            first_response = self.client.post("/wifiNetworks/query", payload=body, override_tenant_id=tenant_id).json()
        else:
            first_response = self.client.post("/wifiNetworks/query", payload=body).json()

        all_networks = first_response.get('data', [])
        total_count = first_response.get('totalCount', len(all_networks))

        logger.debug(f"WiFi Networks: first page returned {len(all_networks)}, total count: {total_count}")

        # If there are more pages, fetch them
        if total_count > len(all_networks):
            page_size = len(all_networks) or 100  # Actual page size returned
            pages_needed = (total_count + page_size - 1) // page_size

            logger.debug(f"WiFi Networks pagination: page_size={page_size}, pages_needed={pages_needed}")

            for page_num in range(2, pages_needed + 1):
                body['page'] = page_num
                if self.client.ec_type == "MSP" and tenant_id:
                    page_response = self.client.post("/wifiNetworks/query", payload=body, override_tenant_id=tenant_id).json()
                else:
                    page_response = self.client.post("/wifiNetworks/query", payload=body).json()
                page_data = page_response.get('data', [])

                logger.debug(f"WiFi Networks page {page_num} returned: {len(page_data)} networks")

                all_networks.extend(page_data)

        logger.debug(f"Total WiFi Networks fetched: {len(all_networks)}")

        return {'data': all_networks, 'totalCount': total_count}

    async def get_wifi_network_by_id(self, network_id: str, tenant_id: str = None):
        """
        Get a specific WiFi network by ID via GET.

        NOTE: The GET endpoint does NOT return venueApGroups (venue association data).
        If you need venue/AP group info, use query_wifi_network_by_id() instead.
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(f"/wifiNetworks/{network_id}", override_tenant_id=tenant_id).json()
        else:
            return self.client.get(f"/wifiNetworks/{network_id}").json()

    async def query_wifi_network_by_id(self, network_id: str, tenant_id: str = None):
        """
        Get a specific WiFi network by ID via POST /wifiNetworks/query.

        Unlike get_wifi_network_by_id (GET), this returns venueApGroups
        with full venue association data (isAllApGroups, apGroupIds, etc.).
        """
        body = {
            'filters': {'id': [network_id]},
            'fields': [
                'id', 'name', 'ssid', 'vlan', 'nwSubType',
                'venueApGroups', 'securityProtocol',
            ],
            'page': 1,
            'pageSize': 1,
        }
        if self.client.ec_type == "MSP" and tenant_id:
            resp = self.client.post("/wifiNetworks/query", payload=body, override_tenant_id=tenant_id)
        else:
            resp = self.client.post("/wifiNetworks/query", payload=body)

        data = resp.json() if resp.status_code == 200 else {}
        networks = data.get('data', [])
        return networks[0] if networks else {}

    async def deactivate_from_all_venues(
        self, network_id: str, tenant_id: str = None
    ):
        """
        Deactivate a WiFi network from all venues.

        R1 requires networks to be deactivated from all venues before
        they can be deleted. This fetches the network to get its venue
        list, then DELETEs from each venue individually.

        Args:
            network_id: WiFi network ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            True if deactivated (or already inactive), False on error
        """
        current_network = await self.get_wifi_network_by_id(
            network_id, tenant_id
        )
        if not current_network:
            logger.warning(
                f"Cannot deactivate network {network_id}: not found"
            )
            return False

        venues = current_network.get('venues') or []
        if not venues:
            return True  # Already deactivated

        nw_name = current_network.get('name', network_id)
        logger.info(
            f"Deactivating network '{nw_name}' from "
            f"{len(venues)} venue(s)"
        )

        # Deactivate from each venue using DELETE endpoint
        for venue in venues:
            venue_id = venue.get('id') if isinstance(venue, dict) else venue
            if not venue_id:
                continue

            logger.debug(
                f"Deactivating network '{nw_name}' from venue {venue_id}"
            )

            if self.client.ec_type == "MSP" and tenant_id:
                response = self.client.delete(
                    f"/venues/{venue_id}/wifiNetworks/{network_id}",
                    override_tenant_id=tenant_id,
                )
            else:
                response = self.client.delete(
                    f"/venues/{venue_id}/wifiNetworks/{network_id}",
                )

            if response.status_code in [200, 202, 204]:
                # If async (202), wait for completion
                if response.status_code == 202:
                    result = (
                        response.json() if response.content
                        else {}
                    )
                    request_id = result.get('requestId')
                    if request_id:
                        await self.client.await_task_completion(
                            request_id, override_tenant_id=tenant_id
                        )
                logger.debug(
                    f"Deactivated network '{nw_name}' from venue {venue_id}"
                )
            elif response.status_code == 404:
                # Already deactivated from this venue
                logger.debug(
                    f"Network '{nw_name}' already deactivated "
                    f"from venue {venue_id}"
                )
            else:
                logger.warning(
                    f"Failed to deactivate network '{nw_name}' "
                    f"from venue {venue_id}: "
                    f"{response.status_code} - {response.text}"
                )
                # Continue with other venues even if one fails

        logger.info(f"Deactivated network '{nw_name}' from all venues")
        return True

    async def delete_wifi_network(
        self,
        network_id: str,
        tenant_id: str = None,
        wait_for_completion: bool = True
    ):
        """
        Delete a WiFi network.

        Args:
            network_id: WiFi network ID
            tenant_id: Tenant/EC ID (required for MSP)
            wait_for_completion: If True, wait for async task (default True).
                                 If False, returns requestId for bulk tracking.

        Returns:
            Deletion response (includes requestId if wait_for_completion=False)
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(f"/wifiNetworks/{network_id}", override_tenant_id=tenant_id)
        else:
            response = self.client.delete(f"/wifiNetworks/{network_id}")

        if response.status_code == R1StatusCode.ACCEPTED:
            result = response.json() if response.content else {"status": "accepted"}
            request_id = result.get('requestId')
            if request_id and wait_for_completion:
                await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)
            return result

        return response.json() if response.content else {"status": "deleted"}

    async def find_wifi_network_by_name(self, tenant_id: str, venue_id: str, network_name: str):
        """
        Search for a WiFi network by name (IDEMPOTENT check)

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID to search within
            network_name: Name of the network to find

        Returns:
            Network object if found, None otherwise
        """
        body = {
            'fields': ['id', 'name', 'ssid', 'vlan', 'nwSubType', 'venueApGroups'],
            'filters': {
                'name': [network_name]
            },
            'sortField': 'name',
            'sortOrder': 'ASC',
        }

        if self.client.ec_type == "MSP":
            response = self.client.post("/wifiNetworks/query", payload=body, override_tenant_id=tenant_id).json()
        else:
            response = self.client.post("/wifiNetworks/query", payload=body).json()

        # Response format: {"data": [...], "totalCount": N}
        networks = response.get('data', [])

        if networks and len(networks) > 0:
            # Network found (silent - will be logged by caller)
            return networks[0]

        # Network not found (silent - will be logged by caller)
        return None

    async def find_wifi_network_by_ssid(self, tenant_id: str, venue_id: str, ssid: str):
        """
        Search for a WiFi network by SSID (broadcast name)

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID to search within
            ssid: SSID broadcast name to find

        Returns:
            Network object if found, None otherwise
        """
        body = {
            'fields': ['id', 'name', 'ssid', 'vlan', 'nwSubType', 'venueApGroups'],
            'filters': {
                'ssid': [ssid]
            },
            'sortField': 'name',
            'sortOrder': 'ASC',
        }

        if self.client.ec_type == "MSP":
            response = self.client.post("/wifiNetworks/query", payload=body, override_tenant_id=tenant_id).json()
        else:
            response = self.client.post("/wifiNetworks/query", payload=body).json()

        # Response format: {"data": [...], "totalCount": N}
        networks = response.get('data', [])

        if networks and len(networks) > 0:
            return networks[0]

        return None

    async def create_wifi_network(
        self,
        tenant_id: str,
        venue_id: str,
        name: str,
        ssid: str,
        passphrase: str,
        security_type: str = "WPA3",
        vlan_id: int = 1,
        description: str = None,
        advanced_customization: dict = None,
        wait_for_completion: bool = True
    ):
        """
        Create a new WiFi network (SSID) in RuckusONE

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID where network will be created
            name: Network name (internal identifier)
            ssid: SSID broadcast name
            passphrase: WiFi password (8-64 characters)
            security_type: One of: WPA3, WPA2, WPA2/WPA3 (default: WPA3)
            vlan_id: VLAN ID (1-4094, default: 1)
            description: Optional description
            advanced_customization: Optional dict of advanced settings to merge into wlan
                (from field_mappings.build_r1_advanced_settings)
            wait_for_completion: If True, wait for async task to complete (default: True)

        Returns:
            Created network response from API
        """
        # Map security types to API values using constants
        wlan_security = SECURITY_TYPE_MAP.get(security_type, WlanSecurity.WPA3)

        # Build WLAN settings based on security type
        wlan_settings = {
            "ssid": ssid,
            "wlanSecurity": wlan_security,
            "vlanId": int(vlan_id),
            "enabled": True
        }

        # Add passphrase field(s) based on security type
        # WPA3 uses saePassphrase, WPA2 uses passphrase, Mixed uses both
        if wlan_security == WlanSecurity.WPA3:
            wlan_settings["saePassphrase"] = passphrase
        elif wlan_security == WlanSecurity.WPA2_PERSONAL:
            wlan_settings["passphrase"] = passphrase
        elif wlan_security == WlanSecurity.WPA23_MIXED:
            # Mixed mode needs both
            wlan_settings["passphrase"] = passphrase
            wlan_settings["saePassphrase"] = passphrase
        else:
            # Fallback for other types (WPA, etc.)
            wlan_settings["passphrase"] = passphrase

        # Merge advanced settings (MFP, rate limits, radio, RADIUS options, etc.)
        if advanced_customization:
            _merge_advanced_settings(wlan_settings, advanced_customization)

        # Build payload for PSK network
        payload = {
            "type": WifiNetworkType.PSK,  # Required discriminator field for polymorphic WiFi networks
            "name": name,
            "wlan": wlan_settings
        }

        if description:
            payload["description"] = description

        # Make API call
        if self.client.ec_type == "MSP":
            response = self.client.post(
                "/wifiNetworks",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/wifiNetworks",
                payload=payload
            )

        # API returns 202 Accepted for async operations
        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

                    # After async task completes, fetch the created resource to get its ID
                    created_network = await self.find_wifi_network_by_name(tenant_id, venue_id, name)
                    if created_network:
                        return created_network
                    else:
                        logger.warning(f"Task completed but could not find created network '{name}'")
                        return result

            return result
        else:
            logger.error(f"Failed to create network: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def create_open_wifi_network(
        self,
        tenant_id: str,
        venue_id: str,
        name: str,
        ssid: str,
        vlan_id: int = 1,
        description: str = None,
        advanced_customization: dict = None,
        wait_for_completion: bool = True
    ):
        """
        Create an Open WiFi network (no authentication) in RuckusONE.

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID where network will be created
            name: Network name (internal identifier, 2-32 chars)
            ssid: SSID broadcast name
            vlan_id: VLAN ID (1-4094, default: 1)
            description: Optional description
            advanced_customization: Optional dict of advanced settings to merge into wlan
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Created network object with 'id' field
        """
        wlan_settings = {
            "ssid": ssid,
            "wlanSecurity": "Open",
            "vlanId": int(vlan_id),
            "enabled": True,
        }

        if advanced_customization:
            _merge_advanced_settings(wlan_settings, advanced_customization)

        payload = {
            "type": WifiNetworkType.OPEN,
            "name": name,
            "wlan": wlan_settings,
        }

        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP":
            response = self.client.post("/wifiNetworks", payload=payload, override_tenant_id=tenant_id)
        else:
            response = self.client.post("/wifiNetworks", payload=payload)

        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)
                    created_network = await self.find_wifi_network_by_name(tenant_id, venue_id, name)
                    if created_network:
                        return created_network
                    else:
                        logger.warning(f"Task completed but could not find created open network '{name}'")
                        return result

            return result
        else:
            logger.error(f"Failed to create open network: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def create_aaa_wifi_network(
        self,
        tenant_id: str,
        venue_id: str,
        name: str,
        ssid: str,
        vlan_id: int = 1,
        security_type: str = "WPA2Enterprise",
        description: str = None,
        advanced_customization: dict = None,
        wait_for_completion: bool = True
    ):
        """
        Create an AAA (Enterprise 802.1X) WiFi network in RuckusONE.

        Note: RADIUS profile must be linked separately after creation via
        link_radius_to_network() on the RadiusProfileService.

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID where network will be created
            name: Network name (internal identifier, 2-32 chars)
            ssid: SSID broadcast name
            vlan_id: VLAN ID (1-4094, default: 1)
            security_type: "WPA2Enterprise" or "WPA3" (default: WPA2Enterprise)
            description: Optional description
            advanced_customization: Optional dict of advanced settings to merge into wlan
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Created network object with 'id' field
        """
        wlan_settings = {
            "ssid": ssid,
            "wlanSecurity": security_type,
            "vlanId": int(vlan_id),
            "enabled": True,
        }

        if advanced_customization:
            _merge_advanced_settings(wlan_settings, advanced_customization)

        payload = {
            "type": WifiNetworkType.AAA,
            "name": name,
            "wlan": wlan_settings,
        }

        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP":
            response = self.client.post("/wifiNetworks", payload=payload, override_tenant_id=tenant_id)
        else:
            response = self.client.post("/wifiNetworks", payload=payload)

        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)
                    created_network = await self.find_wifi_network_by_name(tenant_id, venue_id, name)
                    if created_network:
                        return created_network
                    else:
                        logger.warning(f"Task completed but could not find created AAA network '{name}'")
                        return result

            return result
        else:
            logger.error(f"Failed to create AAA network: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def update_wifi_network(
        self,
        network_id: str,
        payload: dict,
        tenant_id: str = None,
        wait_for_completion: bool = False
    ):
        """
        Update a WiFi network with a full object replacement (PUT).

        R1's PUT /wifiNetworks/{id} is async (202 Accepted + requestId).
        Caller should GET the current network first, modify fields, then PUT back.

        Args:
            network_id: WiFi network ID
            payload: Full network object to PUT
            tenant_id: Tenant/EC ID (required for MSP)
            wait_for_completion: If True, poll until the async task finishes

        Returns:
            dict with requestId (if 202) or the response body
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=payload
            )

        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            return result
        else:
            logger.error(f"Failed to update network {network_id}: {response.status_code} - {response.text[:500]}")
            response.raise_for_status()
            return None

    async def update_wifi_network_name(
        self,
        tenant_id: str,
        network_id: str,
        new_name: str,
        wait_for_completion: bool = True
    ):
        """
        Update a WiFi network's internal name.

        Args:
            tenant_id: Tenant/EC ID
            network_id: WiFi Network ID to update
            new_name: New internal name for the network
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Updated network response from API
        """
        # First, get the current network object
        current_network = await self.get_wifi_network_by_id(network_id, tenant_id)

        if not current_network:
            raise Exception(f"Network {network_id} not found")

        old_name = current_network.get('name')
        logger.info(f"Updating network name: '{old_name}' -> '{new_name}'")

        # Update the name
        current_network['name'] = new_name

        # PUT the updated network object
        if self.client.ec_type == "MSP":
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=current_network,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=current_network
            )

        # Handle response
        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            return result
        else:
            logger.error(f"Failed to update network name: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def update_wifi_network_venue_config(
        self,
        tenant_id: str,
        network_id: str,
        venue_id: str,
        ap_group_id: str,
        ap_group_name: str = None,
        radio_types: list = None,
        vlan_id: int = None,
        wait_for_completion: bool = True
    ):
        """
        Update a WiFi network's venue configuration to use specific AP Groups.

        This is the approach the R1 frontend uses - it updates the network object
        directly with the venue configuration including isAllApGroups: false.

        Args:
            tenant_id: Tenant/EC ID
            network_id: WiFi Network ID to update
            venue_id: Venue ID
            ap_group_id: AP Group ID to activate on
            ap_group_name: AP Group name (optional)
            radio_types: List of radio types (default: all radios)
            vlan_id: Optional VLAN ID override
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Updated network response from API
        """
        # Default radio types
        if radio_types is None:
            radio_types = ["2.4-GHz", "5-GHz", "6-GHz"]

        # First, get the current network object
        logger.info(f"    Fetching current network {network_id}...")
        current_network = await self.get_wifi_network_by_id(network_id, tenant_id)

        if not current_network:
            raise Exception(f"Network {network_id} not found")

        # Log current venue config for debugging
        current_venues = current_network.get("venues", [])
        logger.info(f"    Current venues count: {len(current_venues)}")
        for cv in current_venues:
            logger.info(f"      Venue {cv.get('venueId')}: isAllApGroups={cv.get('isAllApGroups')}, apGroups={len(cv.get('apGroups', []))}")

        # Build the AP Group entry
        ap_group_entry = {
            "apGroupId": ap_group_id,
            "radioTypes": radio_types,
            "radio": "Both",
            "isDefault": False
        }
        if ap_group_name:
            ap_group_entry["apGroupName"] = ap_group_name
        if vlan_id is not None:
            ap_group_entry["vlanId"] = int(vlan_id) if isinstance(vlan_id, str) else vlan_id

        # Update or add the venue in the network's venues array
        # MERGE with existing venue config instead of replacing entirely
        venues = current_network.get("venues", [])
        venue_found = False
        for i, v in enumerate(venues):
            if v.get("venueId") == venue_id:
                # Merge: keep existing fields, update AP Group settings
                v["isAllApGroups"] = False
                v["apGroups"] = [ap_group_entry]
                v["scheduler"] = v.get("scheduler", {"type": "ALWAYS_ON"})
                v["allApGroupsRadio"] = "Both"
                v["allApGroupsRadioTypes"] = radio_types
                venues[i] = v
                venue_found = True
                logger.info(f"    Merged venue config: {v}")
                break

        if not venue_found:
            # Create new venue config
            venue_config = {
                "venueId": venue_id,
                "isAllApGroups": False,
                "apGroups": [ap_group_entry],
                "scheduler": {"type": "ALWAYS_ON"},
                "allApGroupsRadio": "Both",
                "allApGroupsRadioTypes": radio_types
            }
            venues.append(venue_config)
            logger.info(f"    Added new venue config: {venue_config}")

        current_network["venues"] = venues

        logger.info(f"    Updating network with venue config: isAllApGroups=False, apGroup={ap_group_name or ap_group_id}")

        # PUT the updated network object
        if self.client.ec_type == "MSP":
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=current_network,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/wifiNetworks/{network_id}",
                payload=current_network
            )

        # Handle response
        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            return result
        else:
            logger.error(f"Failed to update network venue config: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def create_dpsk_wifi_network(
        self,
        tenant_id: str,
        venue_id: str,
        name: str,
        ssid: str,
        dpsk_service_id: str = None,
        vlan_id: int = 1,
        description: str = None,
        advanced_customization: dict = None,
        wait_for_completion: bool = True
    ):
        """
        Create a DPSK WiFi network tied to a DPSK service (pool).

        The network is created as type 'dpsk' and the DPSK service handles authentication.
        Each passphrase in the DPSK pool can optionally have its own VLAN override.

        NOTE: R1 requires a separate PUT call to actually link the DPSK service
        to the network after creation. The caller must call
        activate_dpsk_service_on_network() after this method.

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID where network will be created
            name: Internal network name
            ssid: Broadcast SSID (what clients see)
            dpsk_service_id: DPSK pool/service ID (for logging only - link via PUT)
            vlan_id: Default VLAN (fallback when passphrase has no VLAN)
            description: Optional description
            advanced_customization: Optional dict of advanced settings to merge into wlan
            wait_for_completion: Wait for async task to complete

        Returns:
            Created network data with 'id' field
        """
        # Build WLAN settings for DPSK network
        # Per OpenAPI schema: ssid (required), vlanId, enabled, wlanSecurity
        wlan_settings = {
            "ssid": ssid,
            "vlanId": int(vlan_id),
            "enabled": True,
        }

        if advanced_customization:
            _merge_advanced_settings(wlan_settings, advanced_customization)

        # Build payload for DPSK network
        # useDpskService MUST be true to enable DPSK service linking
        # The actual service link is established via PUT /wifiNetworks/{id}/dpskServices/{serviceId}
        payload = {
            "type": WifiNetworkType.DPSK,  # DPSK type discriminator
            "name": name,
            "wlan": wlan_settings,
            "useDpskService": True,  # Required: enables DPSK service on this network
        }

        if description:
            payload["description"] = description

        logger.info(f"Creating DPSK network '{name}' with SSID '{ssid}' (will link to pool {dpsk_service_id} after creation)")

        # Make API call
        if self.client.ec_type == "MSP":
            response = self.client.post(
                "/wifiNetworks",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/wifiNetworks",
                payload=payload
            )

        # Handle response
        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

                    # After async task completes, fetch the created resource to get its ID
                    created_network = await self.find_wifi_network_by_name(tenant_id, venue_id, name)
                    if created_network:
                        return created_network
                    else:
                        logger.warning(f"Task completed but could not find created DPSK network '{name}'")
                        return result

            return result
        else:
            error_data = response.json() if response.content else {}
            logger.error(f"Failed to create DPSK network: {response.status_code} - {error_data}")
            raise Exception(f"Failed to create DPSK network: {response.status_code} - {error_data.get('message', response.text)}")

    async def activate_dpsk_service_on_network(
        self,
        tenant_id: str,
        network_id: str,
        dpsk_service_id: str,
        wait_for_completion: bool = True
    ):
        """
        Activate/link a DPSK service to a WiFi network.

        This API builds the relationship between DPSK service and WiFi network.
        It must be called AFTER creating the DPSK network and BEFORE activating
        the network on a venue.

        API: PUT /wifiNetworks/{wifiNetworkId}/dpskServices/{dpskServiceId}

        Args:
            tenant_id: Tenant/EC ID
            network_id: WiFi Network ID
            dpsk_service_id: DPSK Pool/Service ID to link
            wait_for_completion: Wait for async task to complete

        Returns:
            API response
        """
        logger.info(f"Activating DPSK service {dpsk_service_id} on network {network_id}")

        # Make API call - PUT with empty body to link the service
        if self.client.ec_type == "MSP":
            response = self.client.put(
                f"/wifiNetworks/{network_id}/dpskServices/{dpsk_service_id}",
                payload={},
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/wifiNetworks/{network_id}/dpskServices/{dpsk_service_id}",
                payload={}
            )

        # Handle response
        if response.status_code in [R1StatusCode.OK, R1StatusCode.CREATED, R1StatusCode.ACCEPTED]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == R1StatusCode.ACCEPTED and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            logger.info(f"DPSK service {dpsk_service_id} linked to network {network_id}")
            return result
        else:
            error_data = response.json() if response.content else {}
            logger.error(f"Failed to activate DPSK service on network: {response.status_code} - {error_data}")
            raise Exception(f"Failed to activate DPSK service on network: {response.status_code} - {error_data.get('message', response.text)}")
import logging

logger = logging.getLogger(__name__)


class IdentityService:
    """
    Service for managing Identity Groups and Identities in RuckusONE.

    Identity Groups are containers for individual identities (users/devices).
    They can have policy sets, DPSK pools, and MAC registration pools attached.
    Individual identities can be associated with units in multi-dwelling units (MDUs).
    """

    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    # ========== Identity Group Management ==========

    async def get_identity_groups(
        self,
        tenant_id: str = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Get all identity groups (paginated)

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            page: Page number (0-based)
            size: Page size

        Returns:
            Identity groups list with pagination
        """
        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                "/identityGroups",
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                "/identityGroups",
                params=params
            )
        return self.client.safe_json(response)

    async def query_identity_groups(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        size: int = 500
    ):
        """
        Query identity groups with advanced filtering

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Dictionary of filters
            search_string: Optional search string
            page: Page number (0-based, Spring Data style)
            size: Page size

        Returns:
            Query response with identity groups (Spring Data paginated)
        """
        # Body contains search/filter criteria
        body = {}
        if filters:
            body["filters"] = filters
        if search_string is not None:
            body["searchString"] = search_string

        # Pagination goes in query params (Spring Data Pageable)
        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/identityGroups/query",
                payload=body,
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/identityGroups/query",
                payload=body,
                params=params
            )
        return self.client.safe_json(response)

    async def get_identity_group(
        self,
        group_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific identity group by ID

        Args:
            group_id: Identity group ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Identity group details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/identityGroups/{group_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/identityGroups/{group_id}"
            )
        return self.client.safe_json(response)

    async def create_identity_group(
        self,
        tenant_id: str,
        name: str,
        description: str = None,
        wait_for_completion: bool = True,
        **kwargs
    ):
        """
        Create a new identity group

        Args:
            tenant_id: Tenant/EC ID
            name: Identity group name
            description: Optional description
            wait_for_completion: Wait for async task to complete (default: True)
            **kwargs: Additional identity group fields

        Returns:
            Created identity group details
        """
        body = {
            "name": name,
            "description": description or f"Identity group {name}",
            **kwargs
        }

        if self.client.ec_type == "MSP":
            response = self.client.post(
                "/identityGroups",
                override_tenant_id=tenant_id,
                payload=body
            )
        else:
            response = self.client.post(
                "/identityGroups",
                payload=body
            )

        # Check for errors before parsing (handles 403, 401, etc.)
        result = self.client.safe_json(response)

        # Handle async pattern (202 Accepted with requestId)
        if response.status_code == 202 and wait_for_completion:
            request_id = result.get('requestId')
            if request_id:
                logger.info(f"Identity group creation is async (requestId: {request_id}), waiting...")
                await self.client.await_task_completion(
                    request_id=request_id,
                    override_tenant_id=tenant_id
                )
                logger.info(f"Identity group '{name}' creation completed")

                # Fetch the created identity group to get full details
                group_id = result.get('id')
                if group_id:
                    return await self.get_identity_group(group_id, tenant_id)

                # The 202 body often carries only a requestId. Returning it as-is
                # leaves callers with no 'id', which reads as "creation produced
                # nothing" even though the group exists. Resolve it by name.
                logger.info(
                    f"Async response had no id for '{name}', resolving by name"
                )
                try:
                    found = await self.query_identity_groups(
                        tenant_id=tenant_id, search_string=name
                    )
                    items = found.get('content', found.get('data', []))
                    match = next(
                        (g for g in items if g.get('name') == name), None
                    )
                    if match:
                        return match
                    logger.warning(
                        f"Identity group '{name}' not found by name after "
                        f"its creation task completed"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to resolve identity group '{name}' by name: {e}"
                    )

        return result

    async def delete_identity_group(
        self,
        group_id: str,
        tenant_id: str = None
    ):
        """
        Delete an identity group

        Args:
            group_id: Identity group ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/identityGroups/{group_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/identityGroups/{group_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    async def export_identity_groups_to_csv(
        self,
        tenant_id: str = None,
        filters: dict = None
    ):
        """
        Export identity groups to CSV format

        Args:
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
                "/identityGroups/csvFile",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/identityGroups/csvFile",
                payload=body
            )

        # Response is CSV content, not JSON
        return response.text

    # ========== Identity Management ==========

    async def get_all_identities(
        self,
        tenant_id: str = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Get all identities across all groups (paginated)

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            page: Page number (0-based)
            size: Page size

        Returns:
            Identities list with pagination
        """
        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                "/identities",
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                "/identities",
                params=params
            )
        return self.client.safe_json(response)

    async def query_identities(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Query identities with advanced filtering

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Dictionary of filters
            search_string: Optional search string
            page: Page number (0-based)
            size: Page size

        Returns:
            Query response with identities
        """
        body = {}

        if filters:
            body["filters"] = filters
        if search_string:
            body["searchString"] = search_string

        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/identities/query",
                payload=body,
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/identities/query",
                payload=body,
                params=params
            )
        return self.client.safe_json(response)

    async def get_identities_in_group(
        self,
        group_id: str,
        tenant_id: str = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Get all identities in a specific group

        Args:
            group_id: Identity group ID
            tenant_id: Tenant/EC ID (required for MSP)
            page: Page number (0-based)
            size: Page size

        Returns:
            Identities list with pagination
        """
        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/identityGroups/{group_id}/identities",
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/identityGroups/{group_id}/identities",
                params=params
            )
        return self.client.safe_json(response)

    async def get_identity(
        self,
        group_id: str,
        identity_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific identity by ID

        Args:
            group_id: Identity group ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Identity details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/identityGroups/{group_id}/identities/{identity_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/identityGroups/{group_id}/identities/{identity_id}"
            )
        return self.client.safe_json(response)

    async def create_identity(
        self,
        group_id: str,
        name: str,
        tenant_id: str = None,
        display_name: str = None,
        description: str = None,
        vlan: int = None,
        phone_number: str = None
    ):
        """
        Create a new identity in an identity group

        Args:
            group_id: Identity group ID
            name: Identity name (required)
            tenant_id: Tenant/EC ID (required for MSP)
            display_name: Display name (optional)
            description: Description (optional) - use for Cloudpath GUID
            vlan: VLAN ID (optional)
            phone_number: Phone number (optional)

        Returns:
            Created identity details (async - returns requestId)
        """
        payload = {
            "name": name,
            "groupId": group_id
        }

        if display_name:
            payload["displayName"] = display_name
        if description:
            payload["description"] = description
        if vlan is not None:
            payload["vlan"] = vlan
        if phone_number:
            payload["phoneNumber"] = phone_number

        logger.info(f"🔍 DEBUG IDENTITY API - create_identity payload: {payload}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/identityGroups/{group_id}/identities",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/identityGroups/{group_id}/identities",
                payload=payload
            )

        return self.client.safe_json(response)

    async def update_identity(
        self,
        group_id: str,
        identity_id: str,
        tenant_id: str = None,
        name: str = None,
        display_name: str = None,
        description: str = None,
        vlan: int = None,
        phone_number: str = None
    ):
        """
        Update an existing identity

        Args:
            group_id: Identity group ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)
            name: New name (optional)
            display_name: New display name (optional)
            description: New description (optional)
            vlan: New VLAN ID (optional)
            phone_number: New phone number (optional)

        Returns:
            Updated identity details
        """
        payload = {}

        if name:
            payload["name"] = name
        if display_name:
            payload["displayName"] = display_name
        if description:
            payload["description"] = description
        if vlan is not None:
            payload["vlan"] = vlan
        if phone_number:
            payload["phoneNumber"] = phone_number

        logger.info(f"🔍 DEBUG IDENTITY API - update_identity payload: {payload}")

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/identityGroups/{group_id}/identities/{identity_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/identityGroups/{group_id}/identities/{identity_id}",
                payload=payload
            )

        return self.client.safe_json(response)

    async def delete_identity(
        self,
        group_id: str,
        identity_id: str,
        tenant_id: str = None
    ):
        """
        Delete a specific identity from an identity group

        Args:
            group_id: Identity group ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response

        Note:
            This operation returns 202 Accepted and must be polled for completion
        """
        # API expects an array of identity IDs in the payload
        payload = [identity_id]

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities",
                payload=payload
            )

        # Check for errors first
        result = self.client.safe_json(response)

        # Handle 202 Accepted (async operation)
        if response.status_code == 202:
            request_id = result.get('requestId')

            if request_id:
                # Wait for async operation to complete
                return await self.client.await_task_completion(
                    request_id=request_id,
                    override_tenant_id=tenant_id
                )
            else:
                # No requestId, just return the 202 response
                return result

        # Handle other responses (200, 204, etc.)
        return result

    async def delete_identities_bulk(
        self,
        group_id: str,
        identity_ids: list,
        tenant_id: str = None,
        wait_for_completion: bool = True
    ):
        """
        Delete multiple identities from an identity group in a single API call

        Args:
            group_id: Identity group ID
            identity_ids: List of identity IDs to delete
            tenant_id: Tenant/EC ID (required for MSP)
            wait_for_completion: If True, wait for async task (default True).
                                 If False, returns requestId for bulk tracking.

        Returns:
            Deletion response (includes requestId if wait_for_completion=False)

        Note:
            This operation returns 202 Accepted and must be polled for completion
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities",
                payload=identity_ids,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities",
                payload=identity_ids
            )

        # Check for errors first
        result = self.client.safe_json(response)

        # Handle 202 Accepted (async operation)
        if response.status_code == 202:
            request_id = result.get('requestId')

            if request_id and wait_for_completion:
                # Wait for async operation to complete
                return await self.client.await_task_completion(
                    request_id=request_id,
                    override_tenant_id=tenant_id
                )
            else:
                # Return requestId for bulk tracking or no requestId
                return result

        # Handle other responses (200, 204, etc.)
        return result

    # ========== Identity Group Associations ==========

    async def attach_policy_set_to_identity_group(
        self,
        group_id: str,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Attach a policy set to an identity group

        Args:
            group_id: Identity group ID
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/identityGroups/{group_id}/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/identityGroups/{group_id}/policySets/{policy_set_id}"
            )

        if not response.content:
            return {"status": "attached"}
        return self.client.safe_json(response)

    async def remove_policy_set_from_identity_group(
        self,
        group_id: str,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Remove a policy set from an identity group

        Args:
            group_id: Identity group ID
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/identityGroups/{group_id}/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/identityGroups/{group_id}/policySets/{policy_set_id}"
            )

        if not response.content:
            return {"status": "removed"}
        return self.client.safe_json(response)

    async def attach_dpsk_pool_to_identity_group(
        self,
        group_id: str,
        dpsk_pool_id: str,
        tenant_id: str = None
    ):
        """
        Attach a DPSK pool to an identity group

        Args:
            group_id: Identity group ID
            dpsk_pool_id: DPSK pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/identityGroups/{group_id}/dpskPools/{dpsk_pool_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/identityGroups/{group_id}/dpskPools/{dpsk_pool_id}"
            )

        if not response.content:
            return {"status": "attached"}
        return self.client.safe_json(response)

    async def attach_mac_registration_pool_to_identity_group(
        self,
        group_id: str,
        pool_id: str,
        tenant_id: str = None
    ):
        """
        Attach a MAC registration pool to an identity group

        Args:
            group_id: Identity group ID
            pool_id: MAC registration pool ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/identityGroups/{group_id}/macRegistrationPools/{pool_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/identityGroups/{group_id}/macRegistrationPools/{pool_id}"
            )

        if not response.content:
            return {"status": "attached"}
        return self.client.safe_json(response)

    # ========== Unit Identity Associations (MDU) ==========

    async def query_unit_identities(
        self,
        venue_id: str,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Query identities associated with units in a venue

        Args:
            venue_id: Venue ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            size: Page size

        Returns:
            Query response with unit identities
        """
        body = {}

        if filters:
            body["filters"] = filters

        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/venues/{venue_id}/units/identities/query",
                payload=body,
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/venues/{venue_id}/units/identities/query",
                payload=body,
                params=params
            )
        return self.client.safe_json(response)

    async def associate_identity_to_unit(
        self,
        venue_id: str,
        unit_id: str,
        identity_id: str,
        tenant_id: str = None
    ):
        """
        Associate an identity to a unit (for MDU scenarios)

        Args:
            venue_id: Venue ID
            unit_id: Unit ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/venues/{venue_id}/units/{unit_id}/identities/{identity_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/venues/{venue_id}/units/{unit_id}/identities/{identity_id}"
            )

        if not response.content:
            return {"status": "associated"}
        return self.client.safe_json(response)

    async def remove_identity_from_unit(
        self,
        venue_id: str,
        unit_id: str,
        identity_id: str,
        tenant_id: str = None
    ):
        """
        Remove an identity association from a unit

        Args:
            venue_id: Venue ID
            unit_id: Unit ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/venues/{venue_id}/units/{unit_id}/identities/{identity_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/venues/{venue_id}/units/{unit_id}/identities/{identity_id}"
            )

        if not response.content:
            return {"status": "removed"}
        return self.client.safe_json(response)

    async def update_identity_ethernet_ports(
        self,
        group_id: str,
        identity_id: str,
        venue_id: str,
        ethernet_ports: list,
        tenant_id: str = None
    ):
        """
        Update ethernet port assignments for an identity

        Args:
            group_id: Identity group ID
            identity_id: Identity ID
            venue_id: Venue ID
            ethernet_ports: List of ethernet port configurations
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        payload = {
            "ethernetPorts": ethernet_ports
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/identityGroups/{group_id}/identities/{identity_id}/venues/{venue_id}/ethernetPorts",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/identityGroups/{group_id}/identities/{identity_id}/venues/{venue_id}/ethernetPorts",
                payload=payload
            )

        return self.client.safe_json(response)

    async def retry_vni_allocation_for_identity(
        self,
        group_id: str,
        identity_id: str,
        tenant_id: str = None
    ):
        """
        Retry VNI (Virtual Network Identifier) allocation for an identity

        Args:
            group_id: Identity group ID
            identity_id: Identity ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities/{identity_id}/vnis",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/identityGroups/{group_id}/identities/{identity_id}/vnis"
            )

        if not response.content:
            return {"status": "retried"}
        return self.client.safe_json(response)

    # ========== External Identities ==========

    async def query_external_identities(
        self,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        size: int = 100
    ):
        """
        Query external identities (from external auth sources)

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            size: Page size

        Returns:
            Query response with external identities
        """
        body = {}

        if filters:
            body["filters"] = filters

        params = {
            "page": page,
            "size": size
        }

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/externalIdentities/query",
                payload=body,
                params=params,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/externalIdentities/query",
                payload=body,
                params=params
            )
        return self.client.safe_json(response)

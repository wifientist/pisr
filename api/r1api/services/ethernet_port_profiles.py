"""
Ethernet Port Profile Service

Manages ethernet port profiles for LAN port VLAN configuration on APs.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EthernetPortProfileService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    async def query_profiles(
        self,
        tenant_id: str,
        fields: list = None,
        filters: dict = None
    ):
        """
        Query ethernet port profiles.

        Args:
            tenant_id: Tenant/EC ID
            fields: List of fields to return
            filters: Optional filters

        Returns:
            Query response with data array
        """
        if fields is None:
            fields = ['id', 'name', 'type', 'untagId', 'vlanMembers', 'isDefault', 'isTemplate']

        body = {
            'fields': fields,
            'sortField': 'name',
            'sortOrder': 'ASC',
        }

        if filters:
            body['filters'] = filters

        if self.client.ec_type == "MSP":
            response = self.client.post("/ethernetPortProfiles/query", payload=body, override_tenant_id=tenant_id)
        else:
            response = self.client.post("/ethernetPortProfiles/query", payload=body)

        if response.ok:
            return response.json()
        else:
            logger.warning(f"Failed to query ethernet port profiles: {response.status_code} - {response.text[:200]}")
            return {'data': [], 'totalCount': 0}

    async def get_profile(self, tenant_id: str, profile_id: str):
        """
        Get a specific ethernet port profile by ID.

        Args:
            tenant_id: Tenant/EC ID
            profile_id: Profile ID

        Returns:
            Profile object or None
        """
        if self.client.ec_type == "MSP":
            response = self.client.get(f"/ethernetPortProfiles/{profile_id}", override_tenant_id=tenant_id)
        else:
            response = self.client.get(f"/ethernetPortProfiles/{profile_id}")

        if response.ok:
            return response.json()
        else:
            logger.debug(f"Profile {profile_id} not found: {response.status_code}")
            return None

    async def find_profile_by_name(self, tenant_id: str, name: str):
        """
        Find an ethernet port profile by exact name match.

        Args:
            tenant_id: Tenant/EC ID
            name: Profile name to find

        Returns:
            Profile object if found, None otherwise
        """
        response = await self.query_profiles(
            tenant_id=tenant_id,
            filters={'name': [name]}
        )

        profiles = response.get('data', [])
        if profiles:
            return profiles[0]
        return None

    async def find_default_access_profile(self, tenant_id: str):
        """
        Find the built-in "Default ACCESS Port" profile that exists in every venue.

        Args:
            tenant_id: Tenant/EC ID

        Returns:
            Profile object with 'id' field, or None if not found
        """
        # Query for the default ACCESS profile
        response = await self.query_profiles(
            tenant_id=tenant_id,
            filters={'isDefault': [True], 'type': ['ACCESS']}
        )

        profiles = response.get('data', [])

        # Look for the default ACCESS profile
        for profile in profiles:
            if profile.get('isDefault') and profile.get('type') == 'ACCESS':
                logger.debug(f"Found default ACCESS profile: {profile.get('id')} (name: {profile.get('name')})")
                return profile

        # Fallback: try by name
        response = await self.query_profiles(
            tenant_id=tenant_id,
            filters={'name': ['Default ACCESS Port']}
        )

        profiles = response.get('data', [])
        if profiles:
            logger.debug(f"Found 'Default ACCESS Port' profile: {profiles[0].get('id')}")
            return profiles[0]

        logger.warning("Could not find default ACCESS profile")
        return None

    async def find_or_create_access_profile(
        self,
        tenant_id: str,
        name: str,
        vlan_id: int,
        wait_for_completion: bool = True
    ):
        """
        Find an existing ACCESS profile with the given name and VLAN, or create one.

        This is the main method to use for LAN port configuration. It ensures
        idempotency by checking for existing profiles first.

        Args:
            tenant_id: Tenant/EC ID
            name: Profile name (e.g., "VLAN-2000-ACCESS")
            vlan_id: Untagged VLAN ID
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Profile object with 'id' field
        """
        # Check for existing profile with this name
        existing = await self.find_profile_by_name(tenant_id, name)

        if existing:
            existing_vlan = existing.get('untagId')
            existing_type = existing.get('type')

            # Verify it matches our requirements
            if existing_type == 'ACCESS' and existing_vlan == vlan_id:
                logger.debug(f"Found existing ACCESS profile '{name}' with VLAN {vlan_id}: {existing.get('id')}")
                return existing
            else:
                logger.warning(
                    f"Found profile '{name}' but type/VLAN mismatch "
                    f"(type={existing_type}, vlan={existing_vlan}, expected ACCESS/{vlan_id})"
                )
                # Use a unique name instead
                name = f"{name}-{vlan_id}"
                logger.info(f"Creating new profile with unique name: {name}")

        # Create new profile
        return await self.create_profile(
            tenant_id=tenant_id,
            name=name,
            profile_type="ACCESS",
            untag_id=vlan_id,
            wait_for_completion=wait_for_completion
        )

    async def create_profile(
        self,
        tenant_id: str,
        name: str,
        profile_type: str = "ACCESS",
        untag_id: int = 1,
        vlan_members: str = None,
        wait_for_completion: bool = True
    ):
        """
        Create a new ethernet port profile.

        Args:
            tenant_id: Tenant/EC ID
            name: Profile name
            profile_type: "ACCESS", "SELECTIVE_TRUNK", or "TRUNK"
            untag_id: Untagged VLAN ID (1-4094)
            vlan_members: VLAN members string for trunk ports (e.g., "100,200-300")
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Created profile object
        """
        payload = {
            "name": name,
            "type": profile_type,
            "untagId": untag_id
        }

        # For ACCESS ports, vlanMembers should match untagId
        if profile_type == "ACCESS":
            payload["vlanMembers"] = str(untag_id)
        elif vlan_members:
            payload["vlanMembers"] = vlan_members

        logger.info(f"Creating ethernet port profile: {name} (type={profile_type}, vlan={untag_id})")

        if self.client.ec_type == "MSP":
            response = self.client.post("/ethernetPortProfiles", payload=payload, override_tenant_id=tenant_id)
        else:
            response = self.client.post("/ethernetPortProfiles", payload=payload)

        if response.status_code in [200, 201, 202]:
            result = response.json() if response.content else {"status": "accepted"}

            # If 202 Accepted and wait_for_completion=True, poll for task completion
            if response.status_code == 202 and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

                    # After async task completes, fetch the created profile
                    created_profile = await self.find_profile_by_name(tenant_id, name)
                    if created_profile:
                        logger.info(f"Created profile '{name}' with ID: {created_profile.get('id')}")
                        return created_profile
                    else:
                        logger.warning(f"Task completed but could not find created profile '{name}'")
                        return result

            return result
        else:
            logger.error(f"Failed to create ethernet port profile: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def activate_profile_on_ap_lan_port(
        self,
        tenant_id: str,
        venue_id: str,
        serial_number: str,
        port_id: str,
        profile_id: str,
        wait_for_completion: bool = True
    ):
        """
        Activate an ethernet port profile on an AP's LAN port.

        This assigns the profile to the port, which applies its VLAN and type settings.
        Note: This also clears any existing VLAN overwrite settings on the port.

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID
            serial_number: AP serial number
            port_id: Port ID (e.g., "1", "2", "LAN1", "LAN2")
            profile_id: Ethernet port profile ID to activate
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Response from API
        """
        # Normalize port_id - API uses just the number (1, 2, 3, 4)
        if port_id.upper().startswith('LAN'):
            port_number = port_id.upper().replace('LAN', '')
        else:
            port_number = port_id

        # Only append _ACCESS suffix if not already present
        # The API expects format: {profile_id}_ACCESS for ACCESS type profiles
        if profile_id.endswith('_ACCESS'):
            profile_id_with_suffix = profile_id
        else:
            profile_id_with_suffix = f"{profile_id}_ACCESS"

        logger.debug(f"Activating profile {profile_id_with_suffix} on AP {serial_number} port {port_number}")

        if self.client.ec_type == "MSP":
            response = self.client.put(
                f"/venues/{venue_id}/aps/{serial_number}/lanPorts/{port_number}/ethernetPortProfiles/{profile_id_with_suffix}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/venues/{venue_id}/aps/{serial_number}/lanPorts/{port_number}/ethernetPortProfiles/{profile_id_with_suffix}"
            )

        if response.status_code in [200, 201, 202]:
            result = response.json() if response.content else {"status": "accepted"}

            if response.status_code == 202 and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            return result
        else:
            logger.error(f"Failed to activate profile on AP LAN port: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

    async def deactivate_profile_on_ap_lan_port(
        self,
        tenant_id: str,
        venue_id: str,
        serial_number: str,
        port_id: str,
        profile_id: str,
        wait_for_completion: bool = True
    ):
        """
        Deactivate an ethernet port profile from an AP's LAN port.

        Args:
            tenant_id: Tenant/EC ID
            venue_id: Venue ID
            serial_number: AP serial number
            port_id: Port ID (e.g., "1", "2", "LAN1", "LAN2")
            profile_id: Ethernet port profile ID to deactivate
            wait_for_completion: If True, wait for async task to complete

        Returns:
            Response from API
        """
        # Normalize port_id
        if port_id.upper().startswith('LAN'):
            port_number = port_id.upper().replace('LAN', '')
        else:
            port_number = port_id

        # Only append _ACCESS suffix if not already present
        if profile_id.endswith('_ACCESS'):
            profile_id_with_suffix = profile_id
        else:
            profile_id_with_suffix = f"{profile_id}_ACCESS"

        logger.debug(f"Deactivating profile {profile_id_with_suffix} from AP {serial_number} port {port_number}")

        if self.client.ec_type == "MSP":
            response = self.client.delete(
                f"/venues/{venue_id}/aps/{serial_number}/lanPorts/{port_number}/ethernetPortProfiles/{profile_id_with_suffix}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/venues/{venue_id}/aps/{serial_number}/lanPorts/{port_number}/ethernetPortProfiles/{profile_id_with_suffix}"
            )

        if response.status_code in [200, 201, 202]:
            result = response.json() if response.content else {"status": "accepted"}

            if response.status_code == 202 and wait_for_completion:
                request_id = result.get('requestId')
                if request_id:
                    await self.client.await_task_completion(request_id, override_tenant_id=tenant_id)

            return result
        else:
            logger.error(f"Failed to deactivate profile from AP LAN port: {response.status_code} - {response.text}")
            response.raise_for_status()
            return None

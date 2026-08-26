"""
RADIUS Attribute Group Management Service

Manages RADIUS attributes and attribute groups for use with
adaptive policies in DPSK and per-unit SSID configurations.
"""
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class RadiusAttributeService:
    """
    Service for managing RADIUS Attributes and Attribute Groups in RuckusONE.

    RADIUS Attribute Groups can be attached to:
    - Adaptive Policies (to return specific RADIUS attributes)
    - DPSK pools
    - Identity configurations

    Typical use cases:
    - Assigning VLANs via RADIUS attributes
    - Setting bandwidth limits
    - Applying QoS policies
    """

    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    # ========== RADIUS Attributes (Read-only lookups) ==========

    async def get_radius_attributes(
        self,
        tenant_id: str = None
    ):
        """
        Get all available RADIUS attributes

        Args:
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of available RADIUS attributes
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                "/radiusAttributes",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get("/radiusAttributes").json()

    async def query_radius_attributes(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query RADIUS attributes with filtering

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            search_string: Optional search string
            page: Page number (0-based)
            limit: Number of results per page

        Returns:
            Query response with RADIUS attributes
        """
        body = {
            "page": page,
            "pageSize": limit  # API uses pageSize, not limit
        }

        if filters:
            body["filters"] = filters
        if search_string:
            body["searchString"] = search_string

        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.post(
                "/radiusAttributes/query",
                payload=body,
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.post(
                "/radiusAttributes/query",
                payload=body
            ).json()

    async def get_radius_attribute(
        self,
        attribute_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific RADIUS attribute by ID

        Args:
            attribute_id: RADIUS attribute ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            RADIUS attribute details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                f"/radiusAttributes/{attribute_id}",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get(
                f"/radiusAttributes/{attribute_id}"
            ).json()

    async def get_radius_attribute_vendors(
        self,
        tenant_id: str = None
    ):
        """
        Get list of RADIUS attribute vendors (Ruckus, Cisco, etc.)

        Args:
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of vendor names/IDs
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                "/radiusAttributes/vendors",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get("/radiusAttributes/vendors").json()

    # ========== RADIUS Attribute Groups ==========

    async def get_radius_attribute_groups(
        self,
        tenant_id: str = None
    ):
        """
        Get all RADIUS attribute groups

        Args:
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of RADIUS attribute groups
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                "/radiusAttributeGroups",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get("/radiusAttributeGroups").json()

    async def query_radius_attribute_groups(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query RADIUS attribute groups with filtering

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            search_string: Optional search string
            page: Page number (0-based)
            limit: Number of results per page

        Returns:
            Query response with RADIUS attribute groups
        """
        body = {
            "page": page,
            "pageSize": limit  # API uses pageSize, not limit
        }

        if filters:
            body["filters"] = filters
        if search_string:
            body["searchString"] = search_string

        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.post(
                "/radiusAttributeGroups/query",
                payload=body,
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.post(
                "/radiusAttributeGroups/query",
                payload=body
            ).json()

    async def get_radius_attribute_group(
        self,
        group_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific RADIUS attribute group

        Args:
            group_id: RADIUS attribute group ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            RADIUS attribute group details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}"
            ).json()

    async def create_radius_attribute_group(
        self,
        name: str,
        attributes: List[Dict[str, Any]],
        tenant_id: str = None,
        description: str = None
    ):
        """
        Create a new RADIUS attribute group

        Args:
            name: Group name
            attributes: List of attribute assignments, e.g.:
                [
                    {
                        "vendorName": "WISPr",
                        "attributeName": "WISPr-Bandwidth-Max-Down",
                        "operator": "ADD",
                        "attributeValue": "1000000000",
                        "dataType": "INTEGER"
                    }
                ]
            tenant_id: Tenant/EC ID (required for MSP)
            description: Optional description

        Returns:
            Created RADIUS attribute group
        """
        payload = {
            "name": name,
            "attributeAssignments": attributes  # API uses attributeAssignments, not attributes
        }

        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/radiusAttributeGroups",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/radiusAttributeGroups",
                payload=payload
            )

        # Raise exception on HTTP errors so callers can handle failures properly
        if not response.ok:
            error_data = response.json()
            error_msg = error_data.get('message', error_data.get('error', response.text[:200]))
            status = error_data.get('status', response.status_code)
            raise Exception(f"Failed to create RADIUS group ({status}): {error_msg}")

        return response.json()

    async def create_bandwidth_group(
        self,
        name: str,
        down_bps: int = 10_000_000_000,
        up_bps: int = 10_000_000_000,
        tenant_id: str = None
    ):
        """
        Create a WISPr bandwidth limit RADIUS attribute group.

        Convenience method for creating bandwidth rate-limit groups.

        Args:
            name: Group name (e.g., "fast", "gigabit")
            down_bps: Download bandwidth in bits per second (default 10Gbps)
            up_bps: Upload bandwidth in bits per second (default 10Gbps)
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Created RADIUS attribute group
        """
        return await self.create_radius_attribute_group(
            name=name,
            attributes=[
                {
                    "vendorName": "WISPr",
                    "attributeName": "WISPr-Bandwidth-Max-Down",
                    "operator": "ADD",
                    "attributeValue": str(down_bps),
                    "dataType": "INTEGER"
                },
                {
                    "vendorName": "WISPr",
                    "attributeName": "WISPr-Bandwidth-Max-Up",
                    "operator": "ADD",
                    "attributeValue": str(up_bps),
                    "dataType": "INTEGER"
                }
            ],
            description=name,
            tenant_id=tenant_id
        )

    async def update_radius_attribute_group(
        self,
        group_id: str,
        tenant_id: str = None,
        name: str = None,
        attributes: List[Dict[str, Any]] = None,
        description: str = None
    ):
        """
        Update a RADIUS attribute group

        Args:
            group_id: RADIUS attribute group ID
            tenant_id: Tenant/EC ID (required for MSP)
            name: Optional new name
            attributes: Optional new attribute list
            description: Optional new description

        Returns:
            Updated RADIUS attribute group
        """
        payload = {}

        if name:
            payload["name"] = name
        if attributes is not None:
            payload["attributes"] = attributes
        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/radiusAttributeGroups/{group_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/radiusAttributeGroups/{group_id}",
                payload=payload
            )

        return response.json()

    async def delete_radius_attribute_group(
        self,
        group_id: str,
        tenant_id: str = None
    ):
        """
        Delete a RADIUS attribute group

        Args:
            group_id: RADIUS attribute group ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/radiusAttributeGroups/{group_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/radiusAttributeGroups/{group_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== RADIUS Attribute Group Assignments ==========

    async def get_group_assignments(
        self,
        group_id: str,
        tenant_id: str = None
    ):
        """
        Get external assignments for a RADIUS attribute group
        (shows where this group is being used)

        Args:
            group_id: RADIUS attribute group ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of assignments (policies, DPSK pools, etc.)
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}/assignments",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}/assignments"
            ).json()

    async def get_group_assignment(
        self,
        group_id: str,
        assignment_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific assignment

        Args:
            group_id: RADIUS attribute group ID
            assignment_id: Assignment ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Assignment details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}/assignments/{assignment_id}",
                override_tenant_id=tenant_id
            ).json()
        else:
            return self.client.get(
                f"/radiusAttributeGroups/{group_id}/assignments/{assignment_id}"
            ).json()

    async def create_group_assignment(
        self,
        group_id: str,
        assignment_data: dict,
        tenant_id: str = None
    ):
        """
        Create an external assignment for a RADIUS attribute group

        Args:
            group_id: RADIUS attribute group ID
            assignment_data: Assignment configuration
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Created assignment
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/radiusAttributeGroups/{group_id}/assignments",
                payload=assignment_data,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/radiusAttributeGroups/{group_id}/assignments",
                payload=assignment_data
            )

        return response.json()

    async def delete_group_assignment(
        self,
        group_id: str,
        assignment_id: str,
        tenant_id: str = None
    ):
        """
        Delete an external assignment

        Args:
            group_id: RADIUS attribute group ID
            assignment_id: Assignment ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/radiusAttributeGroups/{group_id}/assignments/{assignment_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/radiusAttributeGroups/{group_id}/assignments/{assignment_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

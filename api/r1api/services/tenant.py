import logging

logger = logging.getLogger(__name__)


class TenantService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    async def get_tenant_self(self):
        """
        Get tenant self information
        """
        return self.client.safe_json(self.client.get("/tenants/self"))

    async def get_tenant_user_profiles(self):
        """
        Get all user profiles for a tenant
        """
        return self.client.safe_json(self.client.get("/tenants/userProfiles"))

    async def get_tenant_venues(self, tenant_id: str):
        """
        Get all venues for a tenant
        """
        if self.client.ec_type == "MSP":
            return self.client.safe_json(self.client.get("/venues", override_tenant_id=tenant_id))
        else:
            return self.client.safe_json(self.client.get("/venues"))

    async def get_tenant_aps(self, tenant_id: str):
        """
        Get all APs for a venue
        """
        body = {
            'fields': ["name","status","model","networkStatus","macAddress","venueName","switchName","meshRole","clientCount","apGroupId","apGroupName","lanPortStatuses","tags","serialNumber","radioStatuses","venueId","poePort","firmwareVersion","uptime","afcStatus","powerSavingStatus"],
            'sortField': 'name',
            'sortOrder': 'ASC',
        }
        if self.client.ec_type == "MSP":
            return self.client.safe_json(self.client.post("/venues/aps/query", payload=body, override_tenant_id=tenant_id))
        else:
            return self.client.safe_json(self.client.post("/venues/aps/query", payload=body))


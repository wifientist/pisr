import logging

logger = logging.getLogger(__name__)


class ApService:
    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    async def get_ap_overrides(self, tenant_id: str):
        """
        Get all AP for a tenant
        """
        logger.debug(f"getting AP for tenant_id: {tenant_id}, ec_type: {self.client.ec_type}")
        if self.client.ec_type == "MSP":
            logger.debug("Using MSP-specific endpoint overriding tenant_id")
            return self.client.get("/apconfig", override_tenant_id=tenant_id).json()
        else:
            return self.client.get("/apconfig").json()

# TODO - Implement actual AP related methods, /apconfig isn't an actual route
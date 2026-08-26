import asyncio
import logging

logger = logging.getLogger(__name__)


class PolicySetService:
    """
    Service for managing Adaptive Policy Sets in RuckusONE.

    Policy Sets are collections of prioritized policies that define access control
    rules based on device/user attributes. They can be attached to:
    - Identity Groups
    - DPSK Pools
    - MAC Registration Pools
    - Certificate Templates
    """

    def __init__(self, client):
        self.client = client  # back-reference to main R1Client

    # ========== Policy Set Management ==========

    async def query_policy_sets(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query policy sets with advanced filtering

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Dictionary of filters
            search_string: Optional search string
            page: Page number (0-based)
            limit: Number of results per page

        Returns:
            Query response with policy sets
        """
        body = {
            "page": page,
            "limit": limit
        }

        if filters:
            body["filters"] = filters
        if search_string:
            body["searchString"] = search_string

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/policySets/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/policySets/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def get_policy_set(
        self,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific policy set by ID

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Policy set details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policySets/{policy_set_id}"
            )
        return self.client.safe_json(response)

    async def create_policy_set(
        self,
        name: str,
        tenant_id: str = None,
        description: str = None
    ):
        """
        Create a new policy set

        Args:
            name: Policy set name
            tenant_id: Tenant/EC ID (required for MSP)
            description: Optional description

        Returns:
            Created policy set response
        """
        payload = {
            "name": name
        }

        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/policySets",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/policySets",
                payload=payload
            )

        return self.client.safe_json(response)

    async def update_policy_set(
        self,
        policy_set_id: str,
        tenant_id: str = None,
        name: str = None,
        description: str = None
    ):
        """
        Update an existing policy set

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)
            name: Optional new name
            description: Optional new description

        Returns:
            Updated policy set response
        """
        payload = {}

        if name:
            payload["name"] = name
        if description:
            payload["description"] = description

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/policySets/{policy_set_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/policySets/{policy_set_id}",
                payload=payload
            )

        return self.client.safe_json(response)

    async def delete_policy_set(
        self,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Delete a policy set

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/policySets/{policy_set_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/policySets/{policy_set_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== Policy Set Assignments ==========

    async def get_policy_set_assignments(
        self,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Get all assignments (where this policy set is used)

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of assignments
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policySets/{policy_set_id}/assignments",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policySets/{policy_set_id}/assignments"
            )
        return self.client.safe_json(response)

    async def query_policy_set_assignments(
        self,
        policy_set_id: str,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query policy set assignments with filtering

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            limit: Page size

        Returns:
            Query response with assignments
        """
        body = {
            "page": page,
            "limit": limit
        }

        if filters:
            body["filters"] = filters

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policySets/{policy_set_id}/assignments/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policySets/{policy_set_id}/assignments/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def get_policy_set_assignment(
        self,
        policy_set_id: str,
        assignment_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific assignment

        Args:
            policy_set_id: Policy set ID
            assignment_id: Assignment ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Assignment details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policySets/{policy_set_id}/assignments/{assignment_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policySets/{policy_set_id}/assignments/{assignment_id}"
            )
        return self.client.safe_json(response)

    # ========== Prioritized Policies ==========

    async def get_prioritized_policies(
        self,
        policy_set_id: str,
        tenant_id: str = None
    ):
        """
        Get all policies in a policy set with their priorities

        Args:
            policy_set_id: Policy set ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of prioritized policies
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policySets/{policy_set_id}/prioritizedPolicies",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policySets/{policy_set_id}/prioritizedPolicies"
            )
        return self.client.safe_json(response)

    async def get_prioritized_policy(
        self,
        policy_set_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific policy within a policy set

        Args:
            policy_set_id: Policy set ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Policy details with priority
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}"
            )
        return self.client.safe_json(response)

    async def assign_policy_to_policy_set(
        self,
        policy_set_id: str,
        policy_id: str,
        tenant_id: str = None,
        priority: int = None
    ):
        """
        Assign a policy to a policy set

        Args:
            policy_set_id: Policy set ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)
            priority: Optional priority (lower = higher priority)

        Returns:
            Response from API
        """
        payload = {}

        if priority is not None:
            payload["priority"] = priority

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.put(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.put(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}",
                payload=payload
            )

        if not response.content:
            return {"status": "assigned"}
        return self.client.safe_json(response)

    async def remove_policy_from_policy_set(
        self,
        policy_set_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Remove a policy from a policy set

        Args:
            policy_set_id: Policy set ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Response from API
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/policySets/{policy_set_id}/prioritizedPolicies/{policy_id}"
            )

        if not response.content:
            return {"status": "removed"}

        result = self.client.safe_json(response)

        # Unassignment is asynchronous. Returning as soon as the HTTP call
        # completes leaves the policy still assigned server-side, and a delete
        # issued straight afterwards fails with
        #   409 "The policy is still assigned to a policy set".
        # Wait for the task so callers can rely on the link actually being gone.
        if response.status_code == 202 and isinstance(result, dict):
            request_id = result.get('requestId')
            if request_id:
                logger.debug(
                    f"Policy unassignment is async (requestId: {request_id}), waiting..."
                )
                try:
                    await self.client.await_task_completion(
                        request_id=request_id,
                        override_tenant_id=tenant_id,
                    )
                except Exception as e:
                    # Non-fatal: the caller retries the delete on conflict.
                    logger.warning(
                        f"Unassignment task {request_id} did not confirm: {e}"
                    )

        return result

    # ========== Policy Evaluation ==========

    async def evaluate_policy_criteria(
        self,
        policy_set_id: str,
        criteria: dict,
        tenant_id: str = None
    ):
        """
        Evaluate policy criteria to see which policies would match

        Args:
            policy_set_id: Policy set ID
            criteria: Dictionary of criteria to evaluate
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Evaluation report showing matching policies
        """
        payload = criteria

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policySets/{policy_set_id}/evaluationReports",
                payload=payload,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policySets/{policy_set_id}/evaluationReports",
                payload=payload
            )

        return self.client.safe_json(response)

    # ========== Policy Templates ==========

    async def query_policy_templates(
        self,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query policy templates

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            limit: Page size

        Returns:
            Query response with policy templates
        """
        body = {
            "page": page,
            "limit": limit
        }

        if filters:
            body["filters"] = filters

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/policyTemplates/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/policyTemplates/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def get_policy_template(
        self,
        template_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific policy template

        Args:
            template_id: Policy template ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Policy template details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}"
            )
        return self.client.safe_json(response)

    async def get_template_attributes(
        self,
        template_id: str,
        tenant_id: str = None
    ):
        """
        Get attributes for a policy template

        Args:
            template_id: Policy template ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of template attributes
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/attributes",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/attributes"
            )
        return self.client.safe_json(response)

    async def query_template_attributes(
        self,
        template_id: str,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query template attributes with filtering

        Args:
            template_id: Policy template ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            limit: Page size

        Returns:
            Query response with attributes
        """
        body = {
            "page": page,
            "limit": limit
        }

        if filters:
            body["filters"] = filters

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policyTemplates/{template_id}/attributes/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policyTemplates/{template_id}/attributes/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def get_template_attribute(
        self,
        template_id: str,
        attribute_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific template attribute

        Args:
            template_id: Policy template ID
            attribute_id: Attribute ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Attribute details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/attributes/{attribute_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/attributes/{attribute_id}"
            )
        return self.client.safe_json(response)

    # ========== Template Policies ==========

    async def get_template_policies(
        self,
        template_id: str,
        tenant_id: str = None
    ):
        """
        Get all policies for a template

        Args:
            template_id: Policy template ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of policies
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies"
            )
        return self.client.safe_json(response)

    async def query_template_policies(
        self,
        template_id: str,
        tenant_id: str = None,
        filters: dict = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query policies for a template with filtering

        Args:
            template_id: Policy template ID
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            page: Page number
            limit: Page size

        Returns:
            Query response with policies
        """
        # This endpoint reads "pageSize", NOT "limit". Sending "limit" is
        # silently ignored and the server falls back to a default page of 20,
        # so every caller was seeing 20 policies regardless of the value passed
        # (verified against a tenant holding 160). Requests are 0-indexed;
        # page=1 returns an empty set when everything fits on page 0.
        body = {
            "page": page,
            "pageSize": limit
        }

        if filters:
            body["filters"] = filters

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies/query",
                payload=body
            )
        return self.client.safe_json(response)

    async def create_template_policy(
        self,
        template_id: str,
        policy_data: dict,
        tenant_id: str = None
    ):
        """
        Create a new policy in a template

        Args:
            template_id: Policy template ID
            policy_data: Policy configuration data
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Created policy response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies",
                payload=policy_data,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies",
                payload=policy_data
            )

        return self.client.safe_json(response)

    async def await_policy_creation(
        self,
        template_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Poll for policy creation completion with ramping retry intervals.

        The POST /policyTemplates/{templateId}/policies endpoint returns 202
        with the policy object but no requestId for /activities polling.
        This method polls the GET endpoint until the policy is fully created
        with its radius attribute group association complete.

        Retry schedule:
        - 0.5s intervals for first 5s (10 attempts)
        - 1.0s intervals from 5s to 10s (5 attempts)
        - 2.0s intervals from 10s to 30s (10 attempts)

        Args:
            template_id: Policy template ID
            policy_id: Policy ID from the 202 response
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Completed policy data

        Raises:
            TimeoutError: If policy doesn't complete within ~30 seconds
        """
        # Initial delay before first poll — R1 returns 202 but the policy
        # resource isn't immediately available via GET (race condition).
        # Without this, the first GET always returns 404.
        await asyncio.sleep(0.5)
        elapsed = 0.5

        while elapsed < 30.0:
            # Determine sleep interval based on elapsed time
            if elapsed < 5.0:
                sleep_interval = 0.5
            elif elapsed < 10.0:
                sleep_interval = 1.0
            else:
                sleep_interval = 2.0

            if self.client.ec_type == "MSP" and tenant_id:
                response = self.client.get(
                    f"/policyTemplates/{template_id}/policies/{policy_id}",
                    override_tenant_id=tenant_id
                )
            else:
                response = self.client.get(
                    f"/policyTemplates/{template_id}/policies/{policy_id}"
                )

            if response.ok:
                data = self.client.safe_json(response)
                # Check if onMatchResponse is populated (radius attr group associated)
                if data.get("onMatchResponse"):
                    logger.debug(f"Policy {policy_id} creation completed after {elapsed:.1f}s")
                    return data

            await asyncio.sleep(sleep_interval)
            elapsed += sleep_interval

        raise TimeoutError(
            f"Policy {policy_id} did not complete within 30 seconds"
        )

    async def get_template_policy(
        self,
        template_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific policy from a template

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Policy details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}"
            )
        return self.client.safe_json(response)

    async def update_template_policy(
        self,
        template_id: str,
        policy_id: str,
        policy_data: dict,
        tenant_id: str = None
    ):
        """
        Update a policy in a template

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            policy_data: Updated policy configuration
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Updated policy response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/policyTemplates/{template_id}/policies/{policy_id}",
                payload=policy_data,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/policyTemplates/{template_id}/policies/{policy_id}",
                payload=policy_data
            )

        return self.client.safe_json(response)

    async def delete_template_policy(
        self,
        template_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Delete a policy from a template

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/policyTemplates/{template_id}/policies/{policy_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/policyTemplates/{template_id}/policies/{policy_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== Policy Conditions ==========

    async def get_policy_conditions(
        self,
        template_id: str,
        policy_id: str,
        tenant_id: str = None
    ):
        """
        Get all conditions for a policy

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            List of policy conditions
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions"
            )
        result = self.client.safe_json(response)
        logger.debug(f"get_policy_conditions response type={type(result)}, value={result}")
        return result

    async def get_policy_condition(
        self,
        template_id: str,
        policy_id: str,
        condition_id: str,
        tenant_id: str = None
    ):
        """
        Get a specific condition

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            condition_id: Condition ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Condition details
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.get(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}"
            )
        return self.client.safe_json(response)

    async def create_policy_condition(
        self,
        template_id: str,
        policy_id: str,
        condition_data: dict,
        tenant_id: str = None
    ):
        """
        Create a new condition for a policy

        Conditions define the matching criteria for a policy. Common condition types:
        - DPSK Pool membership
        - Device type/OS
        - Time of day
        - Location/Venue

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            condition_data: Condition configuration, e.g.:
                {
                    "attributeId": "...",
                    "operator": "EQUALS",
                    "value": "..."
                }
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Created condition response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions",
                payload=condition_data,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions",
                payload=condition_data
            )

        if not response.ok:
            logger.error(
                f"create_policy_condition failed: status={response.status_code}, "
                f"payload={condition_data}, response={response.text}"
            )

        return self.client.safe_json(response)

    async def create_string_condition(
        self,
        template_id: str,
        policy_id: str,
        attribute_id: int,
        regex_pattern: str,
        tenant_id: str = None
    ):
        """
        Create a string-matching policy condition.

        Convenience method for creating regex-based string conditions.

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            attribute_id: Template attribute ID (e.g., 1012 for username, 1013 for SSID)
            regex_pattern: Regex pattern for matching (e.g., "username" - no anchors needed)
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Created condition response
        """
        # Map attribute IDs to names and types
        attr_names = {
            1012: "DPSK Username",
            1013: "Wireless SSID",
        }

        condition_data = {
            "name": attr_names.get(attribute_id, f"Attribute {attribute_id}"),
            "templateAttributeId": attribute_id,
            "evaluationRule": {
                "criteriaType": "StringCriteria",
                "regexStringCriteria": regex_pattern
            },
            "templateAttribute": {
                "attributeType": "STRING"
            },
            "policyId": policy_id
        }
        logger.info(f"create_string_condition: policy={policy_id}, attr={attribute_id}, pattern='{regex_pattern}'")
        logger.debug(f"create_string_condition full payload: {condition_data}")
        return await self.create_policy_condition(
            template_id=template_id,
            policy_id=policy_id,
            condition_data=condition_data,
            tenant_id=tenant_id
        )

    async def update_policy_condition(
        self,
        template_id: str,
        policy_id: str,
        condition_id: str,
        condition_data: dict,
        tenant_id: str = None
    ):
        """
        Update a policy condition

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            condition_id: Condition ID
            condition_data: Updated condition configuration
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Updated condition response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.patch(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}",
                payload=condition_data,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.patch(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}",
                payload=condition_data
            )

        return self.client.safe_json(response)

    async def delete_policy_condition(
        self,
        template_id: str,
        policy_id: str,
        condition_id: str,
        tenant_id: str = None
    ):
        """
        Delete a policy condition

        Args:
            template_id: Policy template ID
            policy_id: Policy ID
            condition_id: Condition ID
            tenant_id: Tenant/EC ID (required for MSP)

        Returns:
            Deletion response
        """
        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.delete(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}",
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.delete(
                f"/policyTemplates/{template_id}/policies/{policy_id}/conditions/{condition_id}"
            )

        if not response.content:
            return {"status": "deleted"}
        return self.client.safe_json(response)

    # ========== Cross-Template Policy Query ==========

    async def query_policies_across_templates(
        self,
        tenant_id: str = None,
        filters: dict = None,
        search_string: str = None,
        page: int = 0,
        limit: int = 100
    ):
        """
        Query policies across all templates

        Useful for finding policies without knowing which template they belong to.

        Args:
            tenant_id: Tenant/EC ID (required for MSP)
            filters: Optional filters
            search_string: Optional search string
            page: Page number
            limit: Page size

        Returns:
            Query response with policies from all templates
        """
        body = {
            "page": page,
            "limit": limit
        }

        if filters:
            body["filters"] = filters
        if search_string:
            body["searchString"] = search_string

        if self.client.ec_type == "MSP" and tenant_id:
            response = self.client.post(
                "/policyTemplates/policies/query",
                payload=body,
                override_tenant_id=tenant_id
            )
        else:
            response = self.client.post(
                "/policyTemplates/policies/query",
                payload=body
            )
        return self.client.safe_json(response)
import logging
import os
import json
import requests
import time
import asyncio
from r1api.token_cache import get_cached_token, store_token
from r1api.services.msp import MspService

logger = logging.getLogger(__name__)

# See the pool sizing in R1Client.__init__ — logged once, not per instance.
_POOL_LOGGED = False
from r1api.services.venues import VenueService
from r1api.services.networks import NetworksService
from r1api.services.tenant import TenantService
from r1api.services.aps import ApService
from r1api.services.clients import ClientsService
from r1api.services.floorplans import FloorplanService
from r1api.services.entitlements import EntitlementsService
from r1api.services.dpsk import DpskService
from r1api.services.identity import IdentityService
from r1api.services.policy_sets import PolicySetService
from r1api.services.radius_attributes import RadiusAttributeService
from r1api.services.ethernet_port_profiles import EthernetPortProfileService
from r1api.services.radius_profiles import RadiusProfileService
from r1api.services.switches import SwitchService

class R1Client:
    def __init__(self, tenant_id, client_id, shared_secret, ec_type=None, region=None):
        logger.debug(f"Initializing R1Client for tenant_id={tenant_id}, ec_type={ec_type}, region={region}")
        # self.token = None
        # self.token_expiry = None  # optional if you want expiry management
        self.session = requests.Session()

        # DIVERGENCE FROM rtools2. A bare Session gets urllib3's default
        # pool_maxsize of 10, and PISR does not make requests one at a time:
        # collect.py fans a report out with asyncio.gather over
        # asyncio.to_thread, and asyncio's default executor runs
        # min(32, cpu_count + 4) threads. So ~30 requests contend for 10 pooled
        # connections, and every one that misses opens a fresh connection,
        # completes a TCP handshake and a TLS negotiation, and is then thrown
        # away rather than returned to the pool. Production logs it as
        # "Connection pool is full, discarding connection: api.ruckus.cloud".
        #
        # The cost is invisible in a small venue and grows with the venue,
        # which is the wrong way round — the reports that already take longest
        # pay the most handshakes.
        #
        # Sized off the same expression asyncio uses for its default executor,
        # so the pool tracks the concurrency instead of being a second number
        # to keep in step. Floored at urllib3's own default of 10: on a small
        # box that expression comes out BELOW 10 — four cores gives eight — and
        # sizing "correctly" there would shrink the pool and make the very
        # problem this is fixing worse. The floor costs nothing; an unused
        # slot in a pool is not an open connection.
        #
        # This is also why the default bites on bigger hosts and not smaller
        # ones: eight cores gives twelve threads against a pool of ten.
        #
        # R1_POOL_MAXSIZE overrides for anyone who has tuned the executor.
        pool_maxsize = int(os.getenv("R1_POOL_MAXSIZE") or
                           max(10, min(32, (os.cpu_count() or 1) + 4)))
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,   # distinct hosts; there is only ever one
            pool_maxsize=pool_maxsize,
            # Deliberately no retries. Every call here is a GET or a */query
            # POST and would be safe to repeat, but a silent retry turns a
            # failing R1 endpoint into a slow one, and the 404s this tool
            # surfaces are findings rather than faults.
            max_retries=0,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Once per process, at info. An R1Client is built per request — never
        # cached, see build_r1_client — so logging this per instance would put
        # the same line in the log several times per page view. But at debug it
        # is invisible in a container running at INFO, which makes "did the
        # pool sizing take effect" unanswerable without a rebuild. Once, where
        # it can be read, is the useful amount.
        global _POOL_LOGGED
        if not _POOL_LOGGED:
            _POOL_LOGGED = True
            logger.info("R1 connection pool sized to %s (asyncio executor is "
                        "%s threads)", pool_maxsize,
                        min(32, (os.cpu_count() or 1) + 4))

        if region == 'EU':
            self.host = 'api.eu.ruckus.cloud'
        elif region == 'ASIA':
            self.host = 'api.asia.ruckus.cloud'
        else:
            self.host = 'api.ruckus.cloud'

        self.ec_type = ec_type if ec_type else 'EC'

        self.tenant_id = tenant_id
        self.client_id = client_id
        self.shared_secret = shared_secret

        token = get_cached_token(tenant_id)
        if token:
            self.token = token
        else:
            self._authenticate()
        
        # Attach modular services
        self.msp = MspService(self)
        self.networks = NetworksService(self)
        self.venues = VenueService(self)
        self.tenant = TenantService(self)
        self.aps = ApService(self)
        self.clients = ClientsService(self)
        self.floorplans = FloorplanService(self)
        self.entitlements = EntitlementsService(self)
        self.dpsk = DpskService(self)
        self.identity = IdentityService(self)
        self.policy_sets = PolicySetService(self)
        self.radius_attributes = RadiusAttributeService(self)
        self.ethernet_port_profiles = EthernetPortProfileService(self)
        self.radius_profiles = RadiusProfileService(self)
        self.switches = SwitchService(self)

        logger.info(f"R1Client initialized: tenant_id={tenant_id}, ec_type={self.ec_type}, host={self.host}")

    def __repr__(self):
        return f"<R1Client tenant_id={self.tenant_id}, ec_type={self.ec_type}, host={self.host}>"

    def _authenticate(self):
        """Authenticate with R1 API using client_id and shared_secret."""
        logger.debug(f"Authenticating R1Client for tenant_id={self.tenant_id} ec_type={self.ec_type}")
        url = f"https://{self.host}/oauth2/token/{self.tenant_id}"
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'grant_type': 'client_credentials',
            'client_id': self.client_id,
            'client_secret': self.shared_secret
        }
        response = self.session.post(url, headers=headers, data=data, verify=True)
        #print(f"Response status code: {response.status_code}")
        #print(f"Response headers: {response.headers}")
        #print(f"Response content: {response.content[:300]}")  # Print first 300 chars for brevity

        if not response.ok:
            logger.error(f"Auth failed: {response.status_code} - {response.text[:300]}")
            self.token = None
            self.auth_failed = True
            self.auth_error = {
                "success": False,
                "error": "Authentication failed",
                "status_code": response.status_code,
                "raw_response": response.text[:500]
            }
            return self.auth_error

        try:
            data = response.json()
        except ValueError:
            logger.error("Failed to decode JSON during authentication")
            self.token = None
            self.auth_failed = True
            self.auth_error = {
                "success": False,
                "error": "Invalid JSON response from token endpoint",
                "raw_response": response.text[:500]
            }
            return self.auth_error

        self.token = data.get('access_token') or data.get('token')
        expires_in = data.get('expires_in', 3600)  # default to 1hr if not specified
        store_token(self.tenant_id, self.token, expires_in)

        logger.debug(f"Authentication successful, token expires in {expires_in}s")

    def _request(self, method, path, payload=None, params=None, override_tenant_id=None):
        """General request wrapper."""
        url = f"https://{self.host}{path}"
        verbose = os.environ.get('R1_VERBOSE', '').lower() in ('1', 'true', 'yes')

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if override_tenant_id:
            headers["x-rks-tenantid"] = override_tenant_id

        if verbose:
            logger.info(f">>> {method.upper()} {path}")
            if payload:
                logger.info(f">>> PAYLOAD: {json.dumps(payload, indent=2)[:2000]}")
            if params:
                logger.info(f">>> PARAMS: {params}")

        response = self.session.request(
            method,
            url,
            headers=headers,
            json=payload,
            params=params,
            verify=True
        )

        if verbose:
            body = response.text[:3000] if response.text else '(empty)'
            logger.info(f"<<< {response.status_code} {path}\n{body}")
        else:
            logger.debug(f"{method.upper()} {url} --> {response.status_code}")

        if not response.ok:
            # DIVERGENCE FROM rtools2. A 404 is not necessarily a fault, and
            # here it usually is not: fetch.property_config asks every venue for
            # its Property configuration and treats a 404 as "this is a plain
            # venue", which is the answer for most of them. Logging that at
            # warning put two alarming PROPERTY-MANAGEMENT-002 blocks in the log
            # on every single report, for nothing being wrong.
            #
            # A warning that fires when nothing is wrong is worse than no
            # warning: it trains whoever reads the log to skim past the level,
            # and the real ones are in the same colour. 404 drops to info — the
            # caller decides whether it is a finding, which is exactly what
            # property_config does. Everything else stays at warning.
            level = logger.info if response.status_code == 404 else logger.warning
            level(f"Request error: {response.status_code} - {response.text[:500]}")

        return response

    # Basic HTTP verbs
    def get(self, path, params=None, override_tenant_id=None):
        return self._request("get", path, params=params, override_tenant_id=override_tenant_id)

    def post(self, path, payload=None, params=None, override_tenant_id=None):
        return self._request("post", path, payload=payload, params=params, override_tenant_id=override_tenant_id)

    def put(self, path, payload=None, override_tenant_id=None):
        return self._request("put", path, payload=payload, override_tenant_id=override_tenant_id)

    def delete(self, path, payload=None, override_tenant_id=None):
        return self._request("delete", path, payload=payload, override_tenant_id=override_tenant_id)

    def patch(self, path, payload=None, override_tenant_id=None):
        return self._request("patch", path, payload=payload, override_tenant_id=override_tenant_id)

    def safe_json(self, response):
        """
        Safely extract JSON from a response, with proper error handling.

        Args:
            response: requests.Response object

        Returns:
            Parsed JSON data

        Raises:
            Exception with descriptive error message if response is not valid JSON
        """
        if not response.ok:
            try:
                error_data = response.json()
                error_msg = error_data.get('message', error_data.get('error', response.text[:200]))
            except ValueError:
                error_msg = response.text[:200] if response.text else f"HTTP {response.status_code}"
            raise Exception(f"R1 API error ({response.status_code}): {error_msg}")

        try:
            return response.json()
        except ValueError:
            logger.error(f"Failed to parse JSON response: {response.text[:200]}")
            raise Exception(f"Invalid JSON response from R1 API: {response.text[:100]}")

    def post_multipart(self, path, files, override_tenant_id=None):
        """
        POST request with multipart/form-data (for file uploads like CSV import).

        Args:
            path: API path
            files: Dict of files in format {'field_name': ('filename', content, 'content_type')}
            override_tenant_id: Optional tenant ID override for MSP

        Returns:
            Response object
        """
        url = f"https://{self.host}{path}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            # Don't set Content-Type - requests will set it with boundary for multipart
        }
        if override_tenant_id:
            headers["x-rks-tenantid"] = override_tenant_id

        logger.debug(f"R1Client Multipart POST: {path}")

        response = self.session.post(
            url,
            headers=headers,
            files=files,
            verify=True
        )

        logger.debug(f"POST (multipart) {url} --> {response.status_code}")
        if not response.ok:
            logger.warning(f"Multipart request error: {response.status_code} - {response.text[:500]}")

        return response

    def _extract_error_message(self, data: dict) -> str:
        """
        Extract detailed error message from RuckusONE activity response

        RuckusONE error structure can be complex and nested:
        {
            'error': '{"requestId":"...","errors":["{\\"message\\":\\"Validation Error\\",\\"subErrors\\":[...]}"]}'
        }

        This method attempts to parse nested JSON and extract meaningful validation errors.

        Args:
            data: Activity response data from /activities/{requestId}

        Returns:
            str: Detailed error message
        """
        import json

        # Try to get error from 'error' field (most common)
        if 'error' in data:
            error_value = data['error']

            # If it's a string, try to parse it as JSON
            if isinstance(error_value, str):
                try:
                    error_obj = json.loads(error_value)

                    # Check for 'errors' array
                    if isinstance(error_obj, dict) and 'errors' in error_obj:
                        errors_array = error_obj['errors']

                        # Parse each error in the array (they might be JSON strings too)
                        error_messages = []
                        for err in errors_array:
                            if isinstance(err, str):
                                try:
                                    err_obj = json.loads(err)

                                    # Check for subErrors (validation errors)
                                    if isinstance(err_obj, dict) and 'subErrors' in err_obj:
                                        for sub_err in err_obj['subErrors']:
                                            if isinstance(sub_err, dict):
                                                # Format: "DPSK service.name: Name must be unique"
                                                obj = sub_err.get('object', 'Unknown')
                                                field = sub_err.get('field', '')
                                                msg = sub_err.get('message', 'Unknown error')

                                                if field:
                                                    error_messages.append(f"{obj}.{field}: {msg}")
                                                else:
                                                    error_messages.append(f"{obj}: {msg}")

                                    # If no subErrors, use the main message
                                    elif isinstance(err_obj, dict) and 'message' in err_obj:
                                        error_messages.append(err_obj['message'])
                                    else:
                                        error_messages.append(str(err))

                                except json.JSONDecodeError:
                                    # Not JSON, use as-is
                                    error_messages.append(str(err))
                            else:
                                error_messages.append(str(err))

                        if error_messages:
                            return '; '.join(error_messages)

                    # If error_obj has a message field directly
                    elif isinstance(error_obj, dict) and 'message' in error_obj:
                        return error_obj['message']

                    # Otherwise return the stringified object
                    return str(error_obj)

                except json.JSONDecodeError:
                    # Not JSON, return as-is
                    return error_value

            # If error_value is already a dict
            elif isinstance(error_value, dict):
                if 'message' in error_value:
                    return error_value['message']
                return str(error_value)

        # Try 'message' field (fallback)
        if 'message' in data:
            return data['message']

        # Try 'errorMessage' field (another common pattern)
        if 'errorMessage' in data:
            return data['errorMessage']

        # Last resort: return full data as string
        return 'Unknown error'

    def _get_poll_delay(self, attempt: int) -> float:
        """
        Get polling delay with gentle stepped backoff.

        Fast initial checks (most activities complete quickly), then back off
        to reduce API pressure under heavy load.

        Schedule:
          - Attempts 1-5:   1 second  (5 sec cumulative - catch fast completions)
          - Attempts 6-15:  2 seconds (20 sec cumulative - normal ops)
          - Attempts 16+:   3 seconds (for long waits / eventual consistency)

        Returns:
            Sleep duration in seconds
        """
        if attempt <= 5:
            return 1.0
        elif attempt <= 15:
            return 2.0
        else:
            return 3.0

    async def await_task_completion(
        self,
        request_id: str,
        override_tenant_id: str = None,
        max_attempts: int = 40,
        assume_success_on_timeout: bool = False
    ):
        """
        Poll /activities/{requestId} until async task completes

        RuckusONE API returns 202 Accepted for many operations with a requestId.
        This method polls the /activities endpoint to check task status until
        completion (SUCCESS or FAIL).

        Uses stepped backoff: 1s for first 5 attempts, 2s for next 10, then 3s.
        This catches fast completions quickly while reducing API pressure for slow ops.

        Args:
            request_id: The requestId returned from a 202 response
            override_tenant_id: Optional tenant ID for MSP multi-tenant calls
            max_attempts: Maximum number of polling attempts (default: 40 ≈ 100 sec)
            assume_success_on_timeout: If True and activity never appeared (all 404s),
                return a synthetic success response instead of raising TimeoutError.
                Use this when the POST succeeded and you're confident the op went through.

        Returns:
            dict: Final task status response from /activities/{requestId}

        Raises:
            TimeoutError: If task doesn't complete within max_attempts
            Exception: If task status is FAIL
        """
        logger.info(f"Waiting for task {request_id} to complete...")

        activity_found = False  # Track if we ever got past 404
        total_wait_time = 0.0

        for attempt in range(1, max_attempts + 1):
            response = self.get(f"/activities/{request_id}", override_tenant_id=override_tenant_id)

            if not response.ok:
                # Activity might not exist yet - this is normal for the first few attempts
                if attempt == 1:
                    logger.debug(f"Waiting for activity {request_id} to be created...")
                elif attempt % 10 == 0:
                    logger.debug(f"Still waiting for {request_id}... (attempt {attempt}/{max_attempts}, {total_wait_time:.0f}s)")
                delay = self._get_poll_delay(attempt)
                await asyncio.sleep(delay)
                total_wait_time += delay
                continue

            activity_found = True
            data = response.json()
            status = data.get('status')

            # Log status updates periodically
            if attempt == 1 or attempt % 5 == 0:
                logger.debug(f"Poll {attempt}/{max_attempts}: status={status}")

            # Debug: Log full data on first successful poll
            if attempt == 1:
                logger.debug(f"Activity data for {request_id}: {data}")

            if status == 'SUCCESS':
                logger.info(f"Task {request_id} completed successfully ({total_wait_time:.0f}s)")
                logger.debug(f"Success response for {request_id}: {data}")
                return data

            elif status == 'FAIL':
                # Extract detailed error message from RuckusONE response
                error_msg = self._extract_error_message(data)
                logger.error(f"Task {request_id} failed: {error_msg}")
                logger.debug(f"Full failure response for {request_id}: {data}")
                raise Exception(f"Task {request_id} failed: {error_msg}")

            # Status is still PENDING or IN_PROGRESS
            if attempt < max_attempts:
                delay = self._get_poll_delay(attempt)
                await asyncio.sleep(delay)
                total_wait_time += delay

        # Max attempts reached
        if not activity_found and assume_success_on_timeout:
            # Activity never appeared but POST succeeded - assume it went through
            # This handles R1's eventual consistency under heavy load
            logger.warning(
                f"Task {request_id} activity never appeared after {max_attempts} attempts ({total_wait_time:.0f}s), "
                f"but POST succeeded - assuming success (R1 eventual consistency)"
            )
            return {
                'requestId': request_id,
                'status': 'ASSUMED_SUCCESS',
                'message': 'Activity polling timed out but creation request was accepted'
            }

        raise TimeoutError(
            f"Task {request_id} did not complete after {max_attempts} attempts "
            f"({total_wait_time:.0f} seconds)"
        )

    def query_activities_bulk(
        self,
        activity_ids: list[str],
        override_tenant_id: str = None,
    ) -> dict[str, dict]:
        """
        Query multiple activities in a SINGLE API call using POST /activities/query.

        This is the efficient bulk endpoint - ONE request returns status for ALL activities.
        Use this instead of N individual GET /activities/{id} calls.

        Args:
            activity_ids: List of activity/request IDs to query
            override_tenant_id: Optional tenant ID for MSP multi-tenant calls

        Returns:
            Dict mapping activity_id -> activity data (status, resourceId, etc.)
            Activities not found will not be in the result dict.
        """
        if not activity_ids:
            return {}

        # POST /activities/query with filter on activity IDs
        # NOTE: R1 uses direct array format for filters, not {"in": [...]} syntax
        # The {"in": [...]} format causes "bad SQL grammar" 500 errors
        payload = {
            "filters": {
                "id": activity_ids  # Direct array, not {"in": [...]}
            },
            "pageSize": min(len(activity_ids), 500),  # R1 max page size
            "page": 1,
            "sortField": "startDatetime",
            "sortOrder": "DESC",
        }

        response = self.post(
            "/activities/query",
            payload=payload,
            override_tenant_id=override_tenant_id
        )

        if not response.ok:
            logger.warning(f"POST /activities/query failed: {response.status_code}")
            return {}

        data = response.json()
        activities = data.get('data', [])

        # Build lookup dict by activity ID
        result = {}
        for activity in activities:
            aid = activity.get('id')
            if aid:
                result[aid] = activity

        logger.debug(
            f"Bulk query: requested {len(activity_ids)}, "
            f"returned {len(result)} activities"
        )

        return result

    async def await_task_completion_bulk(
        self,
        request_ids: list[str],
        override_tenant_id: str = None,
        max_attempts: int = 60,
        max_concurrent: int = 100,
        progress_callback=None,
        global_timeout_seconds: int = None
    ):
        """
        Poll multiple async tasks in parallel with throttling

        Args:
            request_ids: List of requestId strings to poll
            override_tenant_id: Optional tenant ID for MSP multi-tenant calls
            max_attempts: Maximum polling attempts per task (default: 60)
            max_concurrent: Max concurrent polls (default: 100)
            progress_callback: Optional callback(completed_count, total_count, result)
            global_timeout_seconds: Optional global timeout for all tasks

        Returns:
            dict: {request_id: result_data} for all tasks

        Note:
            - Failed tasks are included in results with their exception
            - Use return_exceptions=True to handle mixed success/failure
            - Progress callback receives updates as tasks complete
            - Uses stepped backoff internally (1s, 2s, 3s)
        """
        if not request_ids:
            return {}

        logger.info(f"Bulk polling {len(request_ids)} async tasks (max_concurrent={max_concurrent})")

        results = {}
        semaphore = asyncio.Semaphore(max_concurrent)
        completed_count = 0
        total_count = len(request_ids)

        async def poll_single_task(request_id: str):
            """Poll a single task with semaphore throttling"""
            nonlocal completed_count

            async with semaphore:
                try:
                    result = await self.await_task_completion(
                        request_id=request_id,
                        override_tenant_id=override_tenant_id,
                        max_attempts=max_attempts
                    )

                    # Store successful result
                    results[request_id] = {
                        "success": True,
                        "data": result,
                        "request_id": request_id
                    }

                except Exception as e:
                    # Store failed result
                    results[request_id] = {
                        "success": False,
                        "error": str(e),
                        "request_id": request_id
                    }
                    logger.warning(f"Task {request_id} failed: {str(e)}")

                # Update progress
                completed_count += 1
                if progress_callback:
                    progress_callback(completed_count, total_count, results[request_id])

                # Log progress periodically
                if completed_count % 10 == 0 or completed_count == total_count:
                    success_count = sum(1 for r in results.values() if r.get("success"))
                    fail_count = completed_count - success_count
                    logger.info(f"Bulk progress: {completed_count}/{total_count} "
                                f"(success={success_count}, failed={fail_count})")

                return results[request_id]

        # Create tasks for all request_ids
        tasks = [poll_single_task(request_id) for request_id in request_ids]

        # Execute with optional global timeout
        try:
            if global_timeout_seconds:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=global_timeout_seconds
                )
            else:
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.TimeoutError:
            logger.warning(f"Global timeout of {global_timeout_seconds}s exceeded")
            # Mark incomplete tasks as timed out
            for request_id in request_ids:
                if request_id not in results:
                    results[request_id] = {
                        "success": False,
                        "error": f"Global timeout of {global_timeout_seconds}s exceeded",
                        "request_id": request_id
                    }

        # Final summary
        success_count = sum(1 for r in results.values() if r.get("success"))
        fail_count = len(results) - success_count
        logger.info(f"Bulk polling complete: {success_count} succeeded, {fail_count} failed")

        return results

    async def await_tasks_bulk_query(
        self,
        request_ids: list[str],
        override_tenant_id: str = None,
        max_poll_seconds: int = 120,
        poll_interval: int = 3,
        assume_success_on_timeout: bool = False,
        progress_callback=None
    ) -> dict:
        """
        Poll multiple activities concurrently using parallel individual GET requests.

        Uses concurrent GET /activities/{id} requests via asyncio thread pool to
        efficiently poll multiple activities in parallel while respecting rate limits.

        Args:
            request_ids: List of activity requestIds to poll
            override_tenant_id: Optional tenant ID for MSP multi-tenant calls
            max_poll_seconds: Maximum total polling time (default: 120s)
            poll_interval: Seconds between polling rounds (default: 3s)
            assume_success_on_timeout: If True, treat activities that never appeared as success
            progress_callback: Optional callback(completed, total, latest_results)

        Returns:
            dict: {request_id: {"status": "SUCCESS"|"FAIL"|"TIMEOUT", "data": {...}}}
        """
        if not request_ids:
            return {}

        logger.info(f"Concurrent polling {len(request_ids)} activities (interval={poll_interval}s, max={max_poll_seconds}s)")

        pending_ids = set(request_ids)
        results = {}
        start_time = asyncio.get_event_loop().time()
        poll_count = 0

        # Helper to fetch a single activity (runs in thread pool since requests is sync)
        def fetch_activity(req_id: str):
            try:
                response = self.get(f"/activities/{req_id}", override_tenant_id=override_tenant_id)
                if response.ok:
                    return req_id, response.json()
                elif response.status_code == 404:
                    # Activity not created yet - normal for first few polls
                    return req_id, None
                else:
                    return req_id, {"error": f"HTTP {response.status_code}"}
            except Exception as e:
                return req_id, {"error": str(e)}

        while pending_ids and (asyncio.get_event_loop().time() - start_time) < max_poll_seconds:
            poll_count += 1

            # Fetch all pending activities concurrently using thread pool
            loop = asyncio.get_event_loop()
            fetch_tasks = [
                loop.run_in_executor(None, fetch_activity, req_id)
                for req_id in list(pending_ids)
            ]
            fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

            # Process results
            newly_completed = []
            for result in fetch_results:
                if isinstance(result, Exception):
                    logger.warning(f"Activity fetch error: {result}")
                    continue

                req_id, activity = result
                if activity is None:
                    # Activity not found yet - keep polling
                    continue
                if "error" in activity and "status" not in activity:
                    # Fetch error - keep polling
                    logger.debug(f"Activity {req_id} fetch error: {activity.get('error')}")
                    continue

                status = activity.get("status")
                if status == "SUCCESS":
                    results[req_id] = {"status": "SUCCESS", "data": activity}
                    pending_ids.discard(req_id)
                    newly_completed.append(req_id)
                elif status == "FAIL":
                    error_msg = activity.get("error") or "Unknown error"
                    results[req_id] = {"status": "FAIL", "data": activity, "error": error_msg}
                    pending_ids.discard(req_id)
                    newly_completed.append(req_id)
                # PENDING/INPROGRESS - keep polling

            # Log progress
            completed = len(results)
            total = len(request_ids)
            if newly_completed or poll_count % 5 == 0:
                logger.debug(f"Concurrent poll #{poll_count}: {completed}/{total} complete, {len(pending_ids)} pending")

            # Progress callback (may be async)
            if progress_callback and newly_completed:
                result = progress_callback(completed, total, {r: results[r] for r in newly_completed})
                if asyncio.iscoroutine(result):
                    await result

            # Wait before next poll (unless we're done)
            if pending_ids:
                await asyncio.sleep(poll_interval)

        # Handle remaining pending IDs
        elapsed = asyncio.get_event_loop().time() - start_time
        if pending_ids:
            logger.warning(f"Bulk polling timed out after {elapsed:.0f}s with {len(pending_ids)} activities still pending")

            for req_id in pending_ids:
                if assume_success_on_timeout:
                    results[req_id] = {
                        "status": "ASSUMED_SUCCESS",
                        "data": {"requestId": req_id},
                        "message": "Activity polling timed out but creation was accepted"
                    }
                else:
                    results[req_id] = {
                        "status": "TIMEOUT",
                        "data": {"requestId": req_id},
                        "error": f"Polling timed out after {elapsed:.0f}s"
                    }

        success_count = sum(1 for r in results.values() if r["status"] in ("SUCCESS", "ASSUMED_SUCCESS"))
        fail_count = sum(1 for r in results.values() if r["status"] == "FAIL")
        timeout_count = sum(1 for r in results.values() if r["status"] == "TIMEOUT")

        logger.info(f"Concurrent polling complete in {elapsed:.0f}s ({poll_count} rounds): "
                   f"{success_count} success, {fail_count} failed, {timeout_count} timeout")

        return results

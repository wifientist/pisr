"""
Access control: what is public, what needs a session, what needs an admin.

WHY THIS EXISTS. The gate is three separate mechanisms that have to agree —
`SessionGateMiddleware` (gates by PATH PREFIX, so a new router is gated by
existing), `require_admin` / `require_grant` (per-route dependencies, which a
new route does NOT get by existing), and the scope check inside the routers (a
call inside the function body, which nothing enforces at all). The first fails
safe; the others fail open, quietly, the moment somebody adds a route and
forgets.

So this file is mostly an INVENTORY. It asserts the exact set of public paths,
the exact set of admin-only routes, the exact set of grant-gated routes AND
which grant each one names, and that every route which reaches RUCKUS ONE — or
reads the batch record — either checks scope or is admin-only. A new route makes it fail, and the
failure says which list to add it to — which is the point: adding a route
should be a decision about who may call it, taken once, in the open.

Runs without pytest, like the others. NEEDS THE APP'S ENVIRONMENT, so run it
in the container rather than on the host:

    docker compose -f docker-compose.dev.yml exec backend python tests/test_access.py
"""

import ast
import asyncio
import sys
import time
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import auth                              # noqa: E402
from config import AUTH                  # noqa: E402
from fastapi import HTTPException        # noqa: E402
from fastapi.routing import APIRoute     # noqa: E402
from starlette.requests import Request   # noqa: E402
from starlette.responses import Response  # noqa: E402


# ── the inventory ───────────────────────────────────────────────────
#
# Both lists are EXACT, not "at least". A route that disappears from one is as
# much a change to who can reach what as a route that appears in it.

# Reachable with no session at all. Everything here has to be: the login form
# cannot ask for a cookie it is trying to obtain.
PUBLIC = {
    "/healthz",              # the container healthcheck; says only "ok"
    "/api/login",
    "/api/logout",
    "/api/auth/status",      # which mode to render, never who the tenant is
    "/api/enroll",           # POST: sets a password FROM a single-use token
    "/api/enroll/{token}",   # GET: what that token is for
}

# Needs the admin role, not merely a session.
# What each gated route requires — EXACT, and the capabilities are part of the
# assertion. A route keeping its path but changing which capability it asks for
# is the quiet change: nothing else in the system would notice it moving to one
# more people hold.
#
# THERE IS NO SEPARATE "ADMIN" CATEGORY ANY MORE. `admin.portal` is a
# capability like every other, so administering PISR appears here beside
# reading a venue — which is the whole point of collapsing roles, tags and
# profiles into one concept.
GRANTED = {
    # Administering PISR: who may sign in, what a report contains, what
    # "recommended" means, and who holds which role.
    # Creating a login is not the same act as curating a report, so the
    # People portal needs its own capability ON TOP of reaching the portals.
    "/api/admin/accounts": ("admin.accounts", "admin.portal"),
    "/api/admin/accounts/{account_id}": ("admin.accounts", "admin.portal"),
    "/api/admin/accounts/{account_id}/invite": ("admin.accounts", "admin.portal"),
    "/api/admin/baseline": ("admin.portal",),
    "/api/admin/capabilities": ("admin.portal",),
    "/api/admin/preview": ("admin.portal",),
    "/api/admin/roles": ("admin.portal",),
    # Reading a venue. In DEFAULT_GRANTS via the built-in `user` role, so
    # everybody has it unless a role says otherwise.
    "/api/pisr/{controller_id}/report": ("venue.report",),
    "/api/pisr/{controller_id}/report.pdf": ("venue.report",),
    # A separate, deeper READ: hundreds of R1 calls, AP-group and per-network
    # settings the report never fetches, and it can carry guest-portal secrets.
    "/api/pisr/{controller_id}/config/detail": ("config.detail",),
    # Names residents' DPSK usernames, which a report will not show at all.
    "/api/pisr/{controller_id}/identity/trace": ("identity.trace",),
    # The two halves of batch, told apart by capability rather than by prefix.
    # The aggregation ladder: one tenant, and the whole estate. Different
    # questions asked by different people, so different capabilities — and
    # separate routes rather than one deciding from a query parameter, because
    # the oracle reads dependencies and a rule nothing can see is a rule
    # nothing can check.
    "/api/batch/state": ("tenant.rollup",),
    "/api/batch/rollup.pdf": ("tenant.rollup",),
    "/api/batch/estate/state": ("msp.rollup",),
    "/api/batch/estate/rollup.pdf": ("msp.rollup",),
    "/api/batch/sweeps": ("batch.run", "msp.batch"),
    "/api/batch/runs": ("batch.run",),
    "/api/batch/runs/{run_id}/venues/{venue_id}": ("batch.run",),
    "/api/batch/runs/{run_id}/finish": ("batch.run",),
}

#: Reachable by anybody with a session. Still narrowed by scope where marked.
UNGATED = {
    "/api/status",
    "/api/config",
    "/api/account/password",
    "/api/pisr/{controller_id}/scope",
    "/api/pisr/{controller_id}/venues",
    "/api/pisr/{controller_id}/checks",
    "/api/r1/{controller_id}/msp/mspEcs",
}


def _app_routes():
    import main
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def _grants_of(route):
    """
    Every capability this route requires — ALL of them, not any.

    UNIONED ACROSS DEPENDENCIES, not first-wins. A route may carry one from its
    router and another of its own — `/batch/sweeps` needs `batch.run` from the
    router AND `msp.batch` from the route — and reporting only the first would
    describe a weaker rule than the app enforces, which is the direction an
    inventory must never be wrong in.
    """
    held: set = set()
    for dep in route.dependant.dependencies:
        if getattr(dep.call, "__name__", "") == "require_grants":
            held.update(getattr(dep.call, "pisr_grants", ()))
    return tuple(sorted(held))


def _grant_of(route):
    """Which grant this route requires, or None. Set by `auth.require_grant`."""
    for dep in route.dependant.dependencies:
        if getattr(dep.call, "__name__", "") == "require_grants":
            return getattr(dep.call, "pisr_grants", "<unnamed>")
    return None


def test_public_paths_are_exactly_these():
    """
    The allowlist, pinned. Adding a path here is the whole of "this endpoint
    needs no session" — there is no second place to check.
    """
    routes = _app_routes()
    gated_by_prefix = {r.path for r in routes
                       if r.path.startswith(auth._GATED_PREFIXES)}
    actually_public = {p for p in gated_by_prefix
                       if p in auth._PUBLIC_PATHS or p == "/api/enroll/{token}"}
    outside_the_gate = {r.path for r in routes
                        if not r.path.startswith(auth._GATED_PREFIXES)}
    assert actually_public | outside_the_gate == PUBLIC, (
        "public surface changed: "
        f"{sorted((actually_public | outside_the_gate) ^ PUBLIC)}")


def test_everything_else_needs_a_session():
    """
    No route may sit outside both the gate's prefix and the allowlist.

    The gate keys on `/api/`, so this is really a test that nothing was mounted
    somewhere else — `/healthz` is the one deliberate exception and it says
    nothing about the tenant.
    """
    for route in _app_routes():
        assert (route.path.startswith(auth._GATED_PREFIXES)
                or route.path in PUBLIC), f"{route.path} is reachable ungated"


def test_the_gated_surface_is_exactly_this():
    """
    Both directions, and the capabilities are part of the assertion.

    A route that appears without being listed fails here, and so does one that
    keeps its path while changing what it asks for — the second is the quiet
    one, because nothing else would notice it moving to a capability more
    people hold.
    """
    found = {r.path: _grants_of(r) for r in _app_routes() if _grants_of(r)}
    assert found == GRANTED, (
        "gated surface changed: "
        f"{sorted(set(found.items()) ^ set(GRANTED.items()))}")


def test_everything_ungated_is_listed_too():
    """
    The other half. A route needing NO capability is a decision as much as one
    that needs three, and it is the decision nobody writes down.
    """
    found = {r.path for r in _app_routes()
             if not _grants_of(r) and r.path.startswith("/api/")
             and r.path not in PUBLIC}
    assert found == UNGATED, f"ungated surface changed: {sorted(found ^ UNGATED)}"


def test_the_admin_namespace_all_needs_the_portal_capability():
    """
    `admin.portal` replaced the admin ROLE when roles became data an
    administrator builds. The namespace rule survives the change: anything
    under /api/admin needs it, whatever else it also needs.
    """
    for route in _app_routes():
        if route.path.startswith("/api/admin"):
            assert "admin.portal" in _grants_of(route), (
                f"{route.path} is in the admin namespace without needing "
                "admin.portal")


def _request(path: str, cookies=None, role=None, tags=()) -> Request:
    headers = []
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", jar.encode()))
    request = Request({"type": "http", "method": "GET", "path": path,
                       "raw_path": path.encode(), "headers": headers,
                       "query_string": b"", "scheme": "http",
                       "client": ("10.89.0.5", 5000), "server": ("test", 80),
                       "state": {}})
    if role:
        request.state.pisr_role = role
    if tags:
        request.state.pisr_tags = tuple(tags)
    request.state.pisr_user = "tester"
    return request


def _through_gate(path: str, cookies=None) -> int:
    """The status the middleware would produce; 200 means it let the request on."""
    gate = auth.SessionGateMiddleware(app=None)

    async def call_next(_request):
        return Response("ok")

    return asyncio.run(gate.dispatch(_request(path, cookies), call_next)).status_code


def test_auth_status_answers_in_every_state():
    """
    /api/auth/status is the SPA's whole picture of who it is talking to, and it
    had NO coverage until a NameError in it took the app down in a throwaway
    stack — three per-mode returns still naming a variable a refactor had
    renamed, which every route test walked straight past because none of them
    called this one.

    So: call it, signed out and signed in, and hold it to the contract the
    bundle reads. `grants` in particular — AuthContext renders chips off it,
    and a missing key silently means "no grants", which looks exactly like a
    feature that was never deployed.
    """
    contract = {"mode", "required", "authenticated", "user", "role",
                "roleLabel", "grants", "features", "logoutUrl"}

    for label, request in (("signed out", _request("/api/auth/status")),
                           ("signed in", _request("/api/auth/status", role="admin"))):
        body = asyncio.run(auth.auth_status(request))
        missing = contract - set(body)
        assert not missing, f"{label}: /api/auth/status lost {sorted(missing)}"
        assert isinstance(body["authenticated"], bool), label
        assert isinstance(body["grants"], list), label
        # WHICH TOOLS to render, all-of resolved server-side so the SPA and
        # the routes cannot disagree about what a feature costs.
        assert isinstance(body["features"], list), label

    # Signed out, nothing is claimed: no identity, no role, and — the one that
    # decides what the header offers — no grants.
    out = asyncio.run(auth.auth_status(_request("/api/auth/status")))
    if AUTH.enabled:
        assert out["authenticated"] is False
        assert out["role"] is None
        assert out["grants"] == [], "a signed-out caller was offered grants"
        assert out["features"] == [], "a signed-out caller was offered features"


def test_gated_paths_refuse_an_uncookied_caller():
    if not AUTH.enabled:
        print("      (skipped: PISR_AUTH_DISABLED=1 in this environment)")
        return
    for path in ("/api/status", "/api/config", "/api/pisr/1/report",
                 "/api/pisr/1/report.pdf", "/api/pisr/1/venues",
                 "/api/admin/visibility", "/api/batch/runs",
                 # Both halves of batch: the gate keys on the /api/ prefix, so
                 # a grant-gated route is refused a session the same way an
                 # admin one is. A 403 for the wrong privilege comes later and
                 # only to a caller who got past this.
                 "/api/batch/state", "/api/batch/estate/state",
                 "/api/r1/1/msp/mspEcs",
                 "/docs", "/openapi.json"):
        assert _through_gate(path) == 401, f"{path} answered without a session"


def test_public_paths_pass_the_gate():
    if not AUTH.enabled:
        return
    for path in ("/api/login", "/api/auth/status", "/healthz", "/",
                 "/assets/index-abc123.js", "/api/enroll/sometoken"):
        assert _through_gate(path) == 200, f"{path} was refused"


def test_the_enrolment_hole_cannot_name_a_sibling_route():
    """
    `/api/enroll/{token}` is public by SHAPE. A bare startswith would also let
    `/api/enroll/../admin/accounts` through the gate — Starlette does not
    normalise `..`, so it 404s today, but the gate must not lean on the router
    staying that way.
    """
    if not AUTH.enabled:
        return
    for path in ("/api/enroll/../admin/accounts", "/api/enroll/token/more",
                 "/api/enroll/..", "/api/enroll/"):
        assert _through_gate(path) == 401, f"{path} slipped through as enrolment"


def test_a_bad_cookie_is_no_cookie():
    if not AUTH.enabled:
        return
    for value in ("", "rubbish", "u_1.9999999999.deadbeef", "..",
                  "!breakglass.9999999999.0000"):
        assert _through_gate("/api/pisr/1/report",
                             {auth.COOKIE_NAME: value}) == 401, value


# ── the role ────────────────────────────────────────────────────────


def test_require_admin_refuses_a_user():
    try:
        auth.require_admin(_request("/api/admin/roles", role="user"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a user role passed require_admin")
    # Returns the capability it checked, not a role name — "admin" is a
    # role somebody builds now, and this dependency does not know about it.
    assert auth.require_admin(
        _request("/api/admin/roles", role="admin")) == "admin.portal"


def test_a_request_with_no_role_is_a_user_not_an_admin():
    """
    `role_of` fails to the LEAST it can be. A route reached without the
    middleware having run (which should not happen) must not be an admin
    session by omission.
    """
    assert auth.role_of(_request("/api/pisr/1/report")) == "user"
    try:
        auth.require_admin(_request("/api/admin/roles"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a request with no role passed require_admin")


# ── the cookie, in accounts mode ────────────────────────────────────


class _Account:
    def __init__(self, uid, username, role, pw_hash, can_sign_in=True):
        self.id, self.username, self.role = uid, username, role
        self.hash, self.can_sign_in = pw_hash, can_sign_in


class _Store:
    def __init__(self, *accounts):
        self.rows = {a.id: a for a in accounts}

    def by_id(self, uid):
        return self.rows.get(uid)


def _with_store(store, fn):
    import accounts as accounts_module
    original = accounts_module.STORE
    accounts_module.STORE = store
    try:
        return fn()
    finally:
        accounts_module.STORE = original


def test_a_user_cookie_cannot_be_edited_into_an_admin_one():
    """
    The role is in the KEY, not the payload, so there is no role field to
    change — and naming the admin's account id in a cookie signed for the user
    does not verify either.
    """
    user = _Account("u_user", "sam", "user", "hash-a")
    admin = _Account("u_admin", "alex", "admin", "hash-b")

    def check():
        token = auth._mint_account(user)
        assert auth._valid_account(token) == ("sam", "user")
        payload, _, sig = token.rpartition(".")
        _, _, expires = payload.partition(".")
        assert auth._valid_account(f"u_admin.{expires}.{sig}") is None
        # And the admin's own cookie is not mintable from the user's key.
        forged = auth._mint_account(_Account("u_admin", "alex", "admin", "hash-a"))
        assert auth._valid_account(forged) is None

    _with_store(_Store(user, admin), check)


def test_promotion_and_password_change_end_outstanding_sessions():
    """
    The stored hash and the role are both in the key, so a cookie minted
    before either changed stops verifying immediately — revocation now rather
    than at the next expiry.
    """
    user = _Account("u_user", "sam", "user", "hash-a")
    token = _with_store(_Store(user), lambda: auth._mint_account(user))

    changed = _Account("u_user", "sam", "user", "hash-b")
    assert _with_store(_Store(changed), lambda: auth._valid_account(token)) is None

    promoted = _Account("u_user", "sam", "admin", "hash-a")
    assert _with_store(_Store(promoted), lambda: auth._valid_account(token)) is None

    disabled = _Account("u_user", "sam", "user", "hash-a", can_sign_in=False)
    assert _with_store(_Store(disabled), lambda: auth._valid_account(token)) is None

    gone = _Store()
    assert _with_store(gone, lambda: auth._valid_account(token)) is None


def test_an_expired_cookie_is_refused():
    user = _Account("u_user", "sam", "user", "hash-a")

    def check():
        stale = auth._mint_account(user, now=time.time() - AUTH.session_seconds - 60)
        assert auth._valid_account(stale) is None
        fresh = auth._mint_account(user)
        assert auth._valid_account(fresh) == ("sam", "user")

    _with_store(_Store(user), check)


# ── scope, which is a call inside a function body ───────────────────


def _routes_in(module_name: str):
    """(function name, decorator source, called names) for each route in a router."""
    source = (API / "routers" / f"{module_name}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = "\n".join(ast.get_source_segment(source, d) or ""
                               for d in node.decorator_list)
        if "router." not in decorators:
            continue
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                target = sub.func
                if isinstance(target, ast.Name):
                    called.add(target.id)
                elif isinstance(target, ast.Attribute):
                    called.add(target.attr)
        out.append((node.name, decorators, called))
    return out


def test_every_route_that_reaches_r1_checks_scope_or_is_admin_only():
    """
    Scope is enforced by CALLING `_require_scope`, which nothing makes a new
    route do. On an MSP tenant the ECs are different companies, so a route that
    builds an R1 client without checking scope serves one customer's venue to
    another — and it fails open, silently, at request time.

    Admin-only routes are exempt because an admin is unrestricted by
    construction (`visibility.STORE.scope_for` short-circuits on the role).
    """
    for name, decorators, called in _routes_in("pisr_router"):
        if "build_r1_client" not in called:
            continue
        admin_only = "require_admin" in decorators
        assert admin_only or "_require_scope" in called, (
            f"pisr_router.{name} reaches RUCKUS ONE without checking scope")


def test_an_id_from_a_caller_cannot_name_another_endpoint():
    """
    `fetch` builds R1 paths by interpolation, so an id with a slash in it is a
    different endpoint, not a venue. The live check that mattered:
    `<venue>/aps/<serial>/passwords` made PISR issue
    `GET /venues/<venue>/aps/<serial>/passwords`, which is "Get AP Password" —
    the one endpoint the tool promises never to call.
    """
    from r1_client import require_id
    assert require_id("9c6f70c0140b49089c3fb92b9d6d8ec6", "venue id")
    assert require_id("a-venue_id-1")
    for bad in ("9c6f70/aps/SERIAL/passwords", "../radiusServerProfiles",
                "..", ".", "", "a/b", "a b", "a\nb", "a%2Fb", "a.b",
                "x" * 65, None, 17, "a\r\nX-Forwarded-Email: root@example"):
        try:
            require_id(bad, "venue id")
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"{bad!r} was accepted as an id")


def test_the_scope_check_validates_the_venue_id_for_every_r1_route():
    """
    Placed in `_require_scope` deliberately: every route that reaches R1 calls
    it (the test below holds them to that), so one check covers all of them
    and a route added later inherits it.
    """
    from routers import pisr_router
    try:
        pisr_router._require_scope(_request("/api/pisr/1/report", role="admin"),
                                   None, "9c6f70/aps/SERIAL/passwords")
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("a path-shaped venue id passed the scope check")
    # A real id still passes, for an unrestricted role.
    pisr_router._require_scope(_request("/api/pisr/1/report", role="admin"),
                               None, "9c6f70c0140b49089c3fb92b9d6d8ec6")


def test_the_msp_tenant_override_is_validated_too():
    """It goes into the x-rks-tenantid header and into scope comparisons."""
    from config import ControllerConfig
    from r1_client import resolve_tenant
    msp = ControllerConfig(id=1, name="t", tenant_id="a" * 32, client_id="c",
                           shared_secret="s", region="NA", ec_type="MSP")
    assert resolve_tenant(msp, "b" * 32) == "b" * 32
    for bad in ("../x", "a/b", "a\r\nHost: evil"):
        try:
            resolve_tenant(msp, bad)
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"{bad!r} was accepted as a tenant id")


def test_every_route_that_reaches_r1_validates_its_ids():
    """
    The companion to the scope test: an admin-only route is exempt from SCOPE
    (an admin is unrestricted) but NOT from id validation, because the id
    still steers the R1 path.
    """
    for module in ("pisr_router", "batch_router"):
        for name, _decorators, called in _routes_in(module):
            if "build_r1_client" not in called:
                continue
            assert "_require_scope" in called or "require_id" in called, (
                f"{module}.{name} interpolates a caller's id into an R1 path "
                "without validating it")


def test_every_batch_read_narrows_the_record():
    """
    The batch record is written by an admin, whose scope is unrestricted, so it
    holds every venue of every tenant ever batched. A route that hands it to a
    grant-holding `user` without narrowing it serves one customer's estate to
    another — and, because the roll-up is COUNTS, it does so as numbers, which
    no amount of hiding names would have caught.

    Held the same way the scope test is held: by the CALL, because nothing
    makes a new route make it. `restrict` is the narrowing and `_readable` is
    the one place that calls it, so a read route must go through `_readable`
    rather than reaching for `STORE.snapshot()` itself.
    """
    for name, _decorators, called in _routes_in("batch_router"):
        path = f"batch_router.{name}"
        if "snapshot" in called:
            assert "_readable" in called or "require_admin" in _decorators, (
                f"{path} reads the batch record without narrowing it; "
                "call _readable(request), not STORE.snapshot()")


def test_the_narrowing_takes_the_denominator_with_it():
    """
    "48 of 58" tells a caller an estate of 58 exists. `restrict` has to narrow
    the numbers, not merely the names — so this checks a venue the scope does
    not name is gone from the record entirely, rather than present and unnamed.
    """
    import scope
    from services.pisr import rollup

    snapshot = {
        "tenants": {"ec1": {"name": "One", "venues": {"v1": "Mine", "v2": "Theirs"}},
                    "ec2": {"name": "Two", "venues": {"v9": "Elsewhere"}}},
        "venues": {
            "ec1/v1": {"venueId": "v1", "lastComplete": {"at": "2026-01-01T00:00:00Z",
                       "summary": {"checks": {"critical": {"aps-online": 3,
                                                           "poe-budget": 1}}}}},
            "ec1/v2": {"venueId": "v2", "lastComplete": {"at": "2026-01-01T00:00:00Z"}},
            "ec2/v9": {"venueId": "v9", "lastComplete": {"at": "2026-01-01T00:00:00Z"}},
        },
        "runs": [{"id": "r1", "tenantId": "ec1", "planned": 58, "venueIds": ["v1", "v2"]}],
    }
    allowed = scope.Scope(unrestricted=False, ecs={"ec1": {"v1"}})
    narrowed = rollup.restrict(snapshot, allowed, {"poe-budget"})

    assert set(narrowed["tenants"]) == {"ec1"}, "another customer survived"
    assert set(narrowed["tenants"]["ec1"]["venues"]) == {"v1"}
    assert set(narrowed["venues"]) == {"ec1/v1"}, narrowed["venues"]
    assert narrowed["runs"] == [], "a run row kept its 58-venue denominator"

    checks = narrowed["venues"]["ec1/v1"]["lastComplete"]["summary"]["checks"]
    assert checks == {"critical": {"aps-online": 3}}, checks
    # Untouched for an admin, and the same object rather than a rebuilt one.
    assert rollup.restrict(snapshot, scope.UNRESTRICTED, ()) is snapshot


def _seeded_batch(tmp):
    """A record holding two customers, one of which the caller may not see."""
    import json
    summary = {"total": 2, "counts": {"critical": 1, "warning": 1, "info": 0},
               "byCategory": {},
               "checks": {"critical": {"aps-online": 3}},
               "skipped": 0, "passed": 5, "deviceCount": 4, "aps": 4,
               "switches": 0, "clients": 0, "alarms": 0, "readErrors": [],
               "devices": {"aps": {"total": 4, "online": 3, "offline": 1, "other": 0},
                           "switches": {"total": 0, "online": 0, "offline": 0,
                                        "other": 0}}}
    done = {"at": "2026-09-20T00:00:00Z", "status": "ok", "summary": summary}
    record = {
        "version": 1,
        "tenants": {
            "ec-mine": {"name": "Mine", "listedAt": "2026-09-01T00:00:00Z",
                        "venues": {"v1": "Block A", "v2": "Block B"}},
            "ec-theirs": {"name": "Theirs", "listedAt": "2026-09-01T00:00:00Z",
                          "venues": {"v9": "Someone Else"}}},
        "venues": {f"{tid}/{vid}": {"venueId": vid, "venueName": vid,
                                    "lastComplete": done, "lastAttempt": done}
                   for tid, vid in (("ec-mine", "v1"), ("ec-mine", "v2"),
                                    ("ec-theirs", "v9"))},
        "runs": [{"id": "r1", "tenantId": "ec-mine", "startedBy": "alex",
                  "planned": ["v1", "v2"], "results": {"v1": "ok", "v2": "ok"},
                  "startedAt": "2026-09-20T00:00:00Z",
                  "updatedAt": "2026-09-20T00:00:00Z"}]}
    path = f"{tmp}/batch.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    return path


def test_a_tagged_user_reads_a_roll_up_of_their_scope_and_no_more():
    """
    THE INVARIANT, end to end: a grant is a lens and never a key.

    The record is written by an admin, whose scope is unrestricted, so it holds
    both customers. A `user` holding `msp.rollup` reaches the same route and
    must come away with one customer, one venue, and a DENOMINATOR to match —
    "1 of 1", never "1 of 2". Asserted through the route rather than through
    `rollup.restrict` alone, because the route is what a browser calls and the
    narrowing is a call inside a function body that nothing enforces.
    """
    import json
    import tempfile
    import batch_runs
    import visibility
    from routers import batch_router

    tmp = tempfile.mkdtemp()
    policy = f"{tmp}/visibility.json"
    with open(policy, "w", encoding="utf-8") as fh:
        json.dump({"version": 2, "hidden": {"user": []},
                   "scope": {"user": {"unrestricted": False,
                                      "ecs": {"ec-mine": ["v1"]}}}}, fh)

    original_batch, original_policy = batch_runs.STORE, visibility.STORE
    batch_runs.STORE = batch_runs.BatchStore(_seeded_batch(tmp))
    visibility.STORE = visibility.PolicyStore(policy)
    try:
        def state(role, tags, tenant="ec-mine"):
            return asyncio.run(batch_router.get_state(
                _request("/api/msp/batch/state", role=role, tags=tags),
                tenant_id=tenant, days=3650))

        admin = state("admin", ("msp",))
        user = state("user", ("msp",))

        assert admin["msp"]["tenantCount"] == 2, admin["msp"]["tenantCount"]
        assert admin["tenant"]["venueCount"] == 2
        assert len(admin["runs"]) == 1

        assert user["msp"]["tenantCount"] == 1, "another customer reached a user"
        assert user["tenant"]["venueCount"] == 1, \
            "the denominator was not narrowed — it discloses the estate size"
        assert user["runs"] == [], "run history reached a scoped user"

        # And a tenant outside the scope is REFUSED, not silently empty: an
        # empty roll-up is indistinguishable from a batch that never ran.
        try:
            state("user", ("msp",), tenant="ec-theirs")
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("a user read a tenant outside their scope")
    finally:
        batch_runs.STORE, visibility.STORE = original_batch, original_policy


def test_both_report_routes_redact():
    """
    The PDF route re-polls rather than rendering what the browser holds, so
    every control has to be applied twice. `redact` is the single enforcement
    point for element visibility AND the credential scrub, so a report route
    that skips it is the way around both.
    """
    for name, _decorators, called in _routes_in("pisr_router"):
        if "build_report" not in called:
            continue
        assert "redact" in called, f"pisr_router.{name} returns an unredacted report"
    # The batch route builds reports too, and only keeps counts — but it goes
    # through `redact` for the credential scrub like everything else.
    for name, _decorators, called in _routes_in("batch_router"):
        if "build_report" in called:
            assert "redact" in called, f"batch_router.{name} skips the scrub"


# ── scope, the fail-closed half of the policy ───────────────────────


def test_scope_fails_closed_once_set():
    """
    Section visibility fails OPEN and scope fails CLOSED; they share a file and
    a dialog and they are not the same kind of control. On an MSP tenant the
    ECs are different companies, so every one of these is a customer boundary.
    """
    import scope

    assert scope.parse(None).unrestricted, "no policy means no restriction"

    # Naming one EC refuses every other, and an empty venue list means NO
    # venues rather than all of them.
    named = scope.parse({"unrestricted": False,
                         "ecs": {"ec1": ["v1"], "ec2": "*", "ec3": []}})
    assert not named.unrestricted
    assert named.allows_ec("ec1") and named.allows_venue("ec1", "v1")
    assert not named.allows_venue("ec1", "v2")
    assert named.allows_venue("ec2", "anything"), "* means every venue"
    assert not named.allows_ec("ec4")
    assert not named.allows_venue("ec3", "v1"), "an empty list is not 'all'"
    assert not named.allows_ec(None)

    # A restriction that names nothing admits nothing.
    nothing = scope.parse({"unrestricted": False, "ecs": {}})
    assert not nothing.allows_ec("ec1") and not nothing.allows_venue("ec1", "v1")

    # An EC row R1 returns with no identifiable id is DROPPED, not passed on.
    rows = [{"id": "ec1", "name": "Kept"}, {"name": "No id at all"},
            {"tenantId": "ec2", "name": "Other"}]
    assert [r["name"] for r in named.filter_ecs(rows)] == ["Kept", "Other"]
    assert named.filter_venues("ec4", [{"id": "v1"}]) == []


def test_the_built_in_admin_role_can_always_repair_a_broken_policy():
    """
    THE SHORT-CIRCUIT IS GONE, AND THIS IS WHAT REPLACED IT.

    `hidden_for`/`scope_for` used to special-case the string "admin" above the
    file read, so an administrator could reach the portal and fix a scope even
    when the policy was the thing that was broken. Admin-ness is a capability
    now and visibility is an allow list, so a special case on a NAME would be
    meaningless — anybody can build a role called anything.

    What guarantees it instead: the built-in `admin` role is COMPILED IN and is
    what an unreadable policy falls back to, its element list is computed from
    the catalogue, and it has no scope entry so it resolves unrestricted the
    ordinary way. Same promise, no special case.
    """
    import grants
    import roles as role_rules
    import sections
    import visibility

    # A store pointed at nothing at all — the unreadable-file case.
    empty = visibility.PolicyStore(None)
    assert empty.scope_for(None, "admin").unrestricted
    assert empty.hidden_for(None, "admin") == ()
    assert grants.GRANT_ADMIN_PORTAL in empty.grants_for(None, "admin")
    assert grants.GRANT_ADMIN_ROLES in empty.grants_for(None, "admin")
    # Computed, so a card added in a release reaches it with no edit at all.
    assert set(role_rules.builtins()["admin"].visible) == set(
        sections.ALL_ELEMENT_IDS)


def test_a_persons_scope_replaces_their_roles():
    """
    Two regional leaders, identical duties, different territories — the case
    per-person scope exists for.

    REPLACES, does not union. Under a union rule, naming Dana's ECs would hand
    her her own PLUS everything the `user` role could already reach, which is
    the opposite of what an admin typing a territory into a box means. It also
    gives "what can Dana see" one answer rather than two that have to be
    combined in the reader's head.
    """
    import json
    import tempfile
    import visibility

    tmp = tempfile.mkdtemp()
    path = f"{tmp}/visibility.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "version": visibility.FORMAT_VERSION,
            "hidden": {"user": []},
            "scope": {
                "user": {"unrestricted": False, "ecs": {"ec-common": "*"}},
                visibility.PEOPLE_KEY: {
                    "dana": {"unrestricted": False, "ecs": {"ec-north": "*"}},
                    # Stored folded; looked up folded. An IDP's idea of an
                    # address's case is not stable.
                    "SAM@Corp.Example": {"unrestricted": False,
                                         "ecs": {"ec-south": ["v1"]}},
                },
            },
        }, fh)

    original = visibility.STORE
    visibility.STORE = visibility.PolicyStore(path)
    try:
        # No entry of their own: the role's scope, unchanged.
        plain = visibility.scope_for("morgan", "user")
        assert plain.allows_ec("ec-common")
        assert not plain.allows_ec("ec-north")

        # Dana's entry replaces it — she does NOT keep the role's ec-common.
        dana = visibility.scope_for("dana", "user")
        assert dana.allows_ec("ec-north")
        assert not dana.allows_ec("ec-common"), \
            "a person's scope was unioned with their role's, not substituted"

        # Case-folded on both sides, and venue lists survive the round trip.
        sam = visibility.scope_for("sam@CORP.example", "user")
        assert sam.allows_venue("ec-south", "v1")
        assert not sam.allows_venue("ec-south", "v2")
        assert not sam.allows_ec("ec-north")

        # No identity at all — passphrase mode — falls back to the role.
        assert visibility.scope_for(None, "user").allows_ec("ec-common")

        assert visibility.STORE.people_scoped() == ("dana", "sam@corp.example")
    finally:
        visibility.STORE = original


def test_the_derived_surface_agrees_with_this_inventory():
    """
    `scripts/access_surface.py` is what the admin capability matrix renders and
    what the end-to-end suite generates its expectations from. If its
    derivation is wrong, the browser suite tests the wrong thing CONFIDENTLY —
    every route green against a table that describes a different app.

    Both sides read `main.app.routes`, so this is not asserting the app agrees
    with itself. It is asserting that two independent readings of it agree:
    the exact sets a person maintains above, and the introspection a machine
    hands to two other consumers.
    """
    sys.path.insert(0, str(API / "scripts"))
    import access_surface

    derived = access_surface.surface()
    by_path: dict = {}
    for row in derived["routes"]:
        by_path.setdefault(row["path"], row)

    assert {p for p, r in by_path.items() if r["public"]} == PUBLIC
    # ALL-OF, so the oracle reports a TUPLE per route and the inventory pins
    # the whole set. A route quietly dropping one of three requirements is the
    # change nothing else would notice.
    assert {p: tuple(r["requiresGrants"]) for p, r in by_path.items()
            if r["requiresGrants"]} == {k: tuple(sorted(v))
                                        for k, v in GRANTED.items()}
    assert set(derived["grants"]) == set(__import__("grants").ALL_GRANTS)
    assert derived["features"], "the oracle published no feature list"

    # The third state, and the one a two-state matrix would lose: reachable
    # AND narrowed. Every route that reaches R1 or reads the batch record has
    # to be in here, or the matrix draws a tick where it owes a caveat.
    narrowing = {p for p, r in by_path.items() if r["checksScope"]}
    for path in ("/api/pisr/{controller_id}/report",
                 "/api/pisr/{controller_id}/report.pdf",
                 "/api/pisr/{controller_id}/venues",
                 "/api/pisr/{controller_id}/config/detail",
                 "/api/batch/state",
                 "/api/batch/rollup.pdf",
                 # New in R4: a scoped user can now hold `batch.run`, so the
                 # WRITE half has to narrow too. It never did before, because
                 # only unrestricted admins could reach it.
                 "/api/batch/runs",
                 "/api/batch/runs/{run_id}/venues/{venue_id}",
                 "/api/batch/runs/{run_id}/finish"):
        assert path in narrowing, f"{path} narrows by scope but the oracle missed it"


def test_a_refusal_says_what_is_missing_and_not_how_it_is_configured():
    """
    A 403 is read by whoever was refused — which for the admin routes is any
    signed-in `user` who clicked something.

    These messages used to recite all three auth modes and name the env var
    behind each, break-glass included. That is a map of the deployment handed
    to the one person who should not have it, and three paragraphs of noise for
    somebody who only needed to know they should ask. So: name what is missing,
    stop there. Where it comes from belongs in the docstrings, `.env.example`
    and the README.

    Asserted on the message the caller actually receives rather than by reading
    the source, so a helpful sentence added anywhere in the chain is caught.
    """
    import grants

    def refusal(call, request):
        try:
            call(request)
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
            return str(exc.detail)
        raise AssertionError("expected a 403")

    messages = {
        "require_admin": refusal(
            auth.require_admin, _request("/api/admin/roles", role="user")),
        "require_grants": refusal(
            auth.require_grants(grants.GRANT_MSP_ROLLUP),
            _request("/api/msp/batch/state", role="user")),
    }

    for where, detail in messages.items():
        assert "PISR_" not in detail, (
            f"{where} names an environment variable to the caller it refused: "
            f"{detail!r}")
        for word in ("passphrase", "SSO", "proxy", "env"):
            assert word.lower() not in detail.lower(), (
                f"{where} describes the deployment's configuration: {detail!r}")
        # Short enough to read in a toast, and it still says what is missing.
        assert len(detail) < 160, f"{where} is {len(detail)} characters: {detail!r}"

    # The grant is named, because that is the thing to ask an admin for.
    # The PROSE name, because that is what somebody asks an administrator
    # for. The id is developer-facing and says nothing to the person refused.
    assert grants.label(grants.GRANT_MSP_ROLLUP) in messages["require_grants"]
    assert grants.label(grants.GRANT_ADMIN_PORTAL) in messages["require_admin"]


def test_a_saved_scope_takes_effect_without_a_restart():
    """
    `save` refreshes the in-memory cache in place and advances `_stamp` so that
    `_load` will not re-read — which means every field `_load` sets has to be
    set there too. One left out is written to disk CORRECTLY and then ignored
    until the container restarts: a save that appears to have done nothing,
    with the value sitting in the file in plain sight.

    `_people` was exactly that, and no unit test could have caught it — the
    store read the file perfectly when constructed fresh. It took a save and a
    read in one process.
    """
    import json
    import tempfile
    import visibility

    tmp = tempfile.mkdtemp()
    path = f"{tmp}/visibility.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": visibility.FORMAT_VERSION, "hidden": {"user": []}}, fh)

    store = visibility.PolicyStore(path)
    assert store.scope_for("dana").unrestricted

    store.save(
        hidden={"user": []},
        scope={"user": {"unrestricted": False, "ecs": {"ec-common": "*"}},
               visibility.PEOPLE_KEY: {
                   "dana": {"unrestricted": False, "ecs": {"ec-north": "*"}}}},
        actor="test")

    # Same process, same store object, no reload.
    dana = store.scope_for("dana")
    assert not dana.unrestricted, "a saved person scope did not take effect"
    assert dana.allows_ec("ec-north")
    assert not dana.allows_ec("ec-common")
    assert store.people_scoped() == ("dana",)
    assert store.policy()[visibility.PEOPLE_BLOCK]["dana"]["scope"]["ecs"] == {
        "ec-north": "*"}


def _role_store(tmp, role_body, assignment, role_hidden=("wireless.ssids",)):
    """
    A store with one role and one person assigned to it.

    `role_hidden` is written as the PRE-ROLES `hidden` block, so every caller
    also exercises the inversion: those ids become everything-but on the role
    of that id. See PolicyStore._migrate_hidden.
    """
    import json
    import visibility
    path = f"{tmp}/visibility.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"version": visibility.FORMAT_VERSION,
                   "hidden": {"user": list(role_hidden)}}, fh)
    store = visibility.PolicyStore(path)
    store.save(hidden={"user": list(role_hidden)}, scope=None, actor="test",
               roles=role_body, people=assignment)
    return store


def test_a_role_carries_both_capabilities_and_visible_cards():
    """
    The engine. A role bundles CAPABILITIES and the ELEMENTS IT MAY SEE.

    THE ELEMENT HALF IS AN ALLOW LIST. Anything the role does not name is
    withheld, so "what can this role see" has a positive answer instead of
    "everything except these forty-three" — the question an administrator
    actually asks, and the one a deny list cannot answer.
    """
    import tempfile
    import grants
    import roles as role_rules
    import sections

    tmp = tempfile.mkdtemp()
    rid = role_rules.new_id()
    store = _role_store(
        tmp,
        {rid: {"label": "Field Engineer", "grants": ["msp.rollup"],
               "visible": ["config.venue"]}},
        {"dana": {"role": rid}})

    assert sorted(store.grants_for("dana")) == ["msp.rollup"]
    assert grants.GRANT_VENUE_REPORT not in store.grants_for("dana")
    # One element allowed means every OTHER element withheld.
    hidden = set(store.hidden_for("dana"))
    assert "config.venue" not in hidden
    assert len(hidden) == len(sections.ALL_ELEMENT_IDS) - 1

    # The built-ins see everything and their list is COMPUTED — so a card added
    # in a release reaches them without anybody editing anything, which is what
    # stops deny-by-default going dark for the whole deployment at once.
    assert store.hidden_for("nobody", "admin") == ()
    assert set(role_rules.builtins()["admin"].visible) == set(sections.ALL_ELEMENT_IDS)


def test_a_deleted_role_withholds_everything():
    """
    BOTH HALVES FAIL CLOSED NOW, and that is what the allow model changes.

    Capabilities always did. Visibility used to fail OPEN — a missing role hid
    nothing — on the argument that an app rendering nothing explains nothing.
    Under an allow list the only coherent reading is the other one: somebody
    was deliberately given a job, its definition is gone, and nobody has said
    what they may see.

    That old argument is answered elsewhere rather than dismissed: the built-in
    roles' lists are computed, and `ungranted_elements` names anything no role
    allows.
    """
    import tempfile
    import roles as role_rules
    import sections

    tmp = tempfile.mkdtemp()
    rid = role_rules.new_id()
    store = _role_store(
        tmp, {rid: {"label": "Gone Soon", "grants": ["msp.rollup"],
                    "visible": ["config.venue"]}},
        {"dana": {"role": rid}})
    assert sorted(store.grants_for("dana")) == ["msp.rollup"]

    store.save(hidden={}, scope=None, actor="test",
               roles={}, people={"dana": {"role": rid}})

    assert store.grants_for("dana") == frozenset(), \
        "a deleted role still conferred capabilities"
    assert set(store.hidden_for("dana")) == set(sections.ALL_ELEMENT_IDS), \
        "a deleted role still let something through"
    assert store.orphaned_assignments() == {"dana": rid}


def test_a_role_drops_ids_it_does_not_recognise():
    """
    Grants are a FIXED vocabulary shipped with the release, because a grant an
    admin could invent would be enforced by nothing — a permission that
    silently does nothing while the portal shows it as real. Element ids follow
    the same rule the stored policy already followed.
    """
    import tempfile
    import roles as role_rules

    tmp = tempfile.mkdtemp()
    pid = role_rules.new_id()
    store = _role_store(
        tmp,
        {pid: {"label": "Optimist",
               "grants": ["msp.rollup", "admin.everything", 7],
               "visible": ["config.venue", "not.a.section", None]}},
        {"dana": {"role": pid}})

    assert sorted(store.grants_for("dana")) == ["msp.rollup"]
    # The unknown ids were dropped on the way in; the known one is allowed and
    # therefore absent from the withheld set.
    assert "config.venue" not in set(store.hidden_for("dana"))
    assert "not.a.section" not in set(store.hidden_for("dana"))

    # A record with no usable id is dropped entirely rather than given one.
    assert role_rules.clean_one({"label": "no id"}) is None
    # A readable lowercase id is VALID — the built-ins use that shape, and an
    # accounts file already says "admin"/"user". Only a shape that could not
    # be either is refused.
    assert role_rules.clean_one({"id": "Not A Role Id"}) is None
    assert role_rules.clean_one({"id": "admin"}) is not None
    # ...but a missing LABEL is cosmetic, so it falls back to the id and stays
    # assignable, looking obviously unfinished in the portal.
    assert role_rules.clean_one({"id": pid}).label == pid


def test_a_policy_written_before_roles_still_reads():
    """
    R1 stored per-person scope under `scope.people`; R2 moved it to a top-level
    `people` map that also carries the role. A file written by the previous
    release has to keep working — a deploy that silently dropped everyone's
    scope would fail open, which is the one direction this control must not.

    Read from both shapes, written in the new one, so the next save tidies it.
    """
    import json
    import tempfile
    import visibility

    tmp = tempfile.mkdtemp()
    path = f"{tmp}/visibility.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "version": visibility.FORMAT_VERSION,
            "hidden": {"user": ["wireless.ssids"]},
            "scope": {
                "user": {"unrestricted": False, "ecs": {"ec-role": "*"}},
                visibility.PEOPLE_KEY: {
                    "dana": {"unrestricted": False, "ecs": {"ec-north": "*"}}},
            },
        }, fh)

    store = visibility.PolicyStore(path)
    dana = store.scope_for("dana")
    assert dana.allows_ec("ec-north"), "an R1 per-person scope was lost"
    assert not dana.allows_ec("ec-role")
    assert store.scope_for("morgan").allows_ec("ec-role")

    # And the next save writes the new shape without losing it.
    policy = store.policy()
    store.save(hidden={"user": ["wireless.ssids"]},
               scope=policy["scope"], actor="test",
               people=policy[visibility.PEOPLE_BLOCK])
    with open(path, encoding="utf-8") as fh:
        written = json.load(fh)
    assert visibility.PEOPLE_BLOCK in written, "the migration did not rewrite"
    assert written[visibility.PEOPLE_BLOCK]["dana"]["scope"]["ecs"] == {"ec-north": "*"}
    assert store.scope_for("dana").allows_ec("ec-north")


def test_grants_come_from_the_role_and_nowhere_else():
    """
    ONE SOURCE, and this is what stops a second one growing back.

    Capabilities used to arrive from three places — a binary admin/user role,
    per-account tags, and a bundle called a "profile" — and the arbitrary split
    between them was what everybody tripped over ("I have the MSP tag, so why
    can I not run a batch?"). `grants_of` must consult the role and nothing
    else.
    """
    import tempfile
    import grants
    import roles as role_rules
    import visibility

    tmp = tempfile.mkdtemp()
    rid = role_rules.new_id()
    store = _role_store(
        tmp, {rid: {"id": rid, "label": "Desk", "grants": ["msp.rollup"],
                    "visible": []}}, {})

    original = visibility.STORE
    visibility.STORE = store
    try:
        request = _request("/api/batch/state", role=rid)
        request.state.pisr_user = "dana"
        assert auth.grants_of(request) == frozenset({grants.GRANT_MSP_ROLLUP})

        # The built-in `user` role, for anybody without one of their own.
        plain = _request("/api/batch/state", role="user")
        plain.state.pisr_user = "morgan"
        assert auth.grants_of(plain) == frozenset(grants.DEFAULT_GRANTS)

        # And a role id naming nothing confers NOTHING — not the defaults.
        # Somebody was deliberately given a job; losing its definition must not
        # promote them back to what everybody else gets.
        gone = _request("/api/batch/state", role="r_deleted000")
        gone.state.pisr_user = "sam"
        assert auth.grants_of(gone) == frozenset()
    finally:
        visibility.STORE = original


def test_every_hidden_lookup_passes_an_identity():
    """
    The companion to the scope guard. Hiding now resolves per person too, so a
    router that called `visibility.hidden_for(role)` would serve the ROLE
    DEFAULT to somebody holding a role — more than they should see, which is
    the wrong direction even for a control that fails open.
    """
    import inspect
    import visibility

    params = inspect.signature(visibility.hidden_for).parameters
    assert params["identity"].default is inspect.Parameter.empty, (
        "visibility.hidden_for grew a default for `identity`")

    # admin_router is exempt, and the exemption is the distinction worth
    # keeping: this rule is about a route resolving for ITS CALLER, where
    # dropping the identity silently serves the role default. `get_preview`
    # resolves for a NAMED SUBJECT — somebody other than the requester — so it
    # passes an identity explicitly and `auth.hidden_of(request)` would be the
    # wrong call, not the right one. A separate test holds it to resolving
    # rather than working the answer out itself.
    for module in ("pisr_router", "msp_router", "batch_router"):
        source = (API / "routers" / f"{module}.py").read_text(encoding="utf-8")
        assert "visibility.hidden_for(" not in source, (
            f"{module} calls visibility.hidden_for directly; use "
            "auth.hidden_of(request), which carries the identity too")


def _visibility_body(**kw):
    """A VisibilityBody with sensible blanks, for the guard tests."""
    from routers.admin_router import VisibilityBody
    return VisibilityBody(**{"hidden": {"user": []}, "scope": None,
                             "roles": None, "people": None, **kw})


class _NoAccounts:
    """An accounts store with nobody in it."""

    broken = False

    @staticmethod
    def list():
        return []

    @staticmethod
    def by_id(_uid):
        return None

    @staticmethod
    def by_username(_name):
        return None


def _with_policy(store, fn, accounts_store=None):
    """
    Run `fn` against a temporary policy — and, by default, an EMPTY accounts
    store.

    `holders_of` consults BOTH places a role can be assigned, so without this
    every super-tier test would be measuring the real /data/accounts.json that
    happens to be mounted in the container. Those accounts hold the built-in
    `admin` role, which carries `admin.roles`, so the tier would read as
    claimed no matter what the test set up.
    """
    import accounts as accounts_module
    import visibility
    original, original_accounts = visibility.STORE, accounts_module.STORE
    visibility.STORE = store
    accounts_module.STORE = accounts_store or _NoAccounts()
    try:
        return fn()
    finally:
        visibility.STORE = original
        accounts_module.STORE = original_accounts


def test_the_super_tier_does_not_exist_until_somebody_holds_it():
    """
    THE BOOTSTRAP RULE, and the same shape as scope.py's "unrestricted is the
    default": a control nobody has configured must not lock everyone out.

    A fresh instance has no roles, so nobody can hold `admin.roles`, so
    the first role could never be made if the grant were required
    unconditionally. Until one exists, every admin may edit; the moment one is
    assigned, ordinary admins are shut out and the tier is real.
    """
    import tempfile
    import grants
    import roles as role_rules
    from routers import admin_router

    tmp = tempfile.mkdtemp()
    pid = role_rules.new_id()
    store = _role_store(tmp, {}, {})           # no roles at all

    admin = _request("/api/admin/roles", role="admin")
    admin.state.pisr_user = "alex"

    def check_open():
        assert admin_router._role_authority(admin), \
            "a fresh instance refused its first role"
        # ...and creating one is allowed.
        admin_router._require_role_authority(admin, _visibility_body(
            roles={pid: {"id": pid, "label": "Super",
                            "grants": [grants.GRANT_ADMIN_ROLES]}},
            people={"alex": {"role": pid}}))
    _with_policy(store, check_open)

    # Now somebody holds it.
    store.save(hidden={"user": []}, scope=None, actor="test",
               roles={pid: {"id": pid, "label": "Super",
                               "grants": [grants.GRANT_ADMIN_ROLES]}},
               people={"alex": {"role": pid}})

    # NOT `role="admin"`: that is the built-in Administrator, which carries
    # `admin.roles` and would therefore be a super. The point of this test is
    # somebody who administers and does not decide who may do what.
    ordinary = role_rules.new_id()
    other = _request("/api/admin/roles", role=ordinary)
    other.state.pisr_user = "sam"

    def check_closed():
        assert not admin_router._role_authority(other), \
            "an ordinary admin kept role authority after a super existed"
        assert admin_router._role_authority(admin), \
            "the super lost their own authority"
    _with_policy(store, check_closed)


def test_an_admin_cannot_assign_a_grant_they_do_not_hold():
    """
    RULE 2, and without it rule 1 is decorative.

    Shutting ordinary admins out of the role EDITOR does not stop them
    picking the super role out of the assignment dropdown for themselves —
    it already exists, and assignment is the half they are allowed to use. So
    the actor must hold every grant an assignment hands out.

    SYMMETRIC, covering removal too, so a lesser admin cannot quietly demote a
    super either.
    """
    import tempfile
    import grants
    import roles as role_rules
    from routers import admin_router

    tmp = tempfile.mkdtemp()
    super_id, desk_id = role_rules.new_id(), role_rules.new_id()
    body = {super_id: {"id": super_id, "label": "Super",
                       "grants": [grants.GRANT_ADMIN_ROLES]},
            desk_id: {"id": desk_id, "label": "MSP Desk",
                      "grants": [grants.GRANT_MSP_ROLLUP]}}
    store = _role_store(tmp, body, {"alex": {"role": super_id}})

    # AN ADMIN WHO IS NOT A SUPER. `role="admin"` would be the built-in
    # Administrator, which carries every capability including `admin.roles` —
    # so testing rule 2 against it would be testing nothing. This is the role
    # an organisation actually builds: administer PISR, but do not decide who
    # may do what.
    plain_admin = role_rules.new_id()
    body[plain_admin] = {"id": plain_admin, "label": "Ordinary admin",
                         "grants": [grants.GRANT_ADMIN_PORTAL], "visible": []}
    # A role carrying only what sam already holds, created UP FRONT —
    # adding one in the body would be a catalogue edit, which rule 1
    # refuses before rule 2 is ever reached.
    allowed = role_rules.new_id()
    body[allowed] = {"id": allowed, "label": "Also ordinary",
                     "grants": [grants.GRANT_ADMIN_PORTAL], "visible": []}
    store = _role_store(tmp, body, {"alex": {"role": super_id},
                                    "sam": {"role": plain_admin}})
    sam = _request("/api/admin/roles", role=plain_admin)
    sam.state.pisr_user = "sam"

    def check():
        # Helping themselves to the super role: refused.
        try:
            admin_router._require_role_authority(sam, _visibility_body(
                people={"alex": {"role": super_id},
                        "sam": {"role": super_id}}))
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
            assert grants.GRANT_ADMIN_ROLES in str(exc.detail)
        else:
            raise AssertionError("an admin assigned themselves the super role")

        # Demoting the super: refused as well — removal counts.
        try:
            admin_router._require_role_authority(sam, _visibility_body(
                people={"alex": {"role": desk_id}}))
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("an admin demoted a super they could not match")

        # Assigning a role carrying only what they already hold is fine —
        # the tier must not make the ordinary job impossible.
        admin_router._require_role_authority(sam, _visibility_body(
            people={"alex": {"role": super_id}, "sam": {"role": allowed}}))
    _with_policy(store, check)


def test_the_last_super_cannot_be_unassigned():
    """
    Rule 2's blind spot, reached by a different door.

    Unassigning the last holder of `admin.roles` removes a grant — but it
    also makes the grant UNCLAIMED, which re-opens the bootstrap rule and hands
    role editing back to every admin. So a super demoting themselves is an
    escalation for everyone else, and the guard has the same shape as the
    last-admin one in accounts.py.
    """
    import tempfile
    import grants
    import roles as role_rules
    from routers import admin_router

    tmp = tempfile.mkdtemp()
    super_id = role_rules.new_id()
    body = {super_id: {"id": super_id, "label": "Super",
                       "grants": [grants.GRANT_ADMIN_ROLES]}}
    store = _role_store(tmp, body, {"alex": {"role": super_id}})

    alex = _request("/api/admin/roles", role="admin")
    alex.state.pisr_user = "alex"

    def check():
        try:
            admin_router._require_role_authority(
                alex, _visibility_body(roles=body, people={}))
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
            assert "admin.roles" in str(exc.detail)
        else:
            raise AssertionError("the last super unassigned themselves")

        # Handing it to somebody else first is allowed.
        admin_router._require_role_authority(alex, _visibility_body(
            roles=body, people={"sam": {"role": super_id}}))

        # AND IT CANNOT BE DISMANTLED, which changed when the built-ins became
        # compiled-in. `admin.roles` is always DEFINED — the built-in
        # Administrator carries it and cannot be deleted away — so the only way
        # to turn the tier off would be to leave nobody holding it, and that is
        # precisely what this guard refuses.
        #
        # One-way, like the last-admin guard: once there is a super there must
        # always be a super. Defensible, and the alternative is a save that
        # quietly hands role authority back to every administrator.
        try:
            admin_router._require_role_authority(
                alex, _visibility_body(roles={}, people={}))
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError("the super tier was dismantled")
    _with_policy(store, check)


def test_an_unchanged_role_list_is_not_an_edit():
    """
    Rule 1 keys on what CHANGED, not on the field being present.

    The portal sends a whole document every time — last-write-wins made
    visible — so an ordinary administrator saving an assignment re-sends the
    roles untouched. Refusing that would make the People tab unusable for
    everybody but supers, which is not what the tier is for.

    The body is built FROM `policy()["roles"]`, the way the portal builds it,
    rather than by hand. A hand-built one omits whatever the store resolved or
    migrated — the built-ins, or a `user` role inverted out of a pre-roles
    hidden block — and those absences read as deletions.
    """
    import tempfile
    import grants
    import roles as role_rules
    from routers import admin_router

    tmp = tempfile.mkdtemp()
    super_id = role_rules.new_id()
    plain_admin = role_rules.new_id()
    body = {
        super_id: {"id": super_id, "label": "Super",
                   "grants": [grants.GRANT_ADMIN_ROLES], "visible": []},
        # AN ADMINISTRATOR WHO IS NOT A SUPER: administers PISR, does not
        # decide who may do what. Testing rule 1 against the built-in `admin`
        # would be testing nothing, since it carries `admin.roles`.
        plain_admin: {"id": plain_admin, "label": "Ordinary admin",
                      "grants": [grants.GRANT_ADMIN_PORTAL], "visible": []},
    }
    store = _role_store(tmp, body, {"alex": {"role": super_id},
                                    "sam": {"role": plain_admin}})
    as_stored = {r["id"]: r for r in store.policy()["roles"]}

    sam = _request("/api/admin/roles", role=plain_admin)
    sam.state.pisr_user = "sam"

    def check():
        # Same roles, different assignment: allowed.
        admin_router._require_role_authority(sam, _visibility_body(
            roles=as_stored,
            people={"alex": {"role": super_id}, "sam": {"role": plain_admin}}))

        # Relabelling one IS an edit, and is refused.
        edited = {rid: dict(r) for rid, r in as_stored.items()}
        edited[super_id]["label"] = "Renamed"
        try:
            admin_router._require_role_authority(sam, _visibility_body(
                roles=edited, people={"alex": {"role": super_id}}))
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("an ordinary admin edited the role catalogue")
    _with_policy(store, check)


def test_the_super_capability_is_never_conferred_by_default():
    """
    `admin.roles` is held only by a role that names it, by a person, in the
    portal.

    This test used to say "belongs to no tag", which is now vacuous — tags are
    gone and `grants.for_tags` with them. The property it was protecting is
    not: the authority to compose everybody else's permissions must not arrive
    from anywhere except a deliberate assignment. So nothing that is conferred
    WITHOUT somebody deciding may carry it — not the default every account
    falls back to, and not the built-in `user` role that default belongs to.

    `admin.portal` is held to the same rule. The built-in `admin` role is the
    one place both are expected, and it is compiled into `roles.py` rather than
    seeded, which is the lockout story.
    """
    import grants
    import roles as role_rules

    super_tier = (grants.GRANT_ADMIN_ROLES, grants.GRANT_ADMIN_PORTAL)
    for capability in super_tier:
        assert capability in grants.ALL_GRANTS, capability
        assert capability not in grants.DEFAULT_GRANTS, capability

    builtins = role_rules.builtins()
    for capability in super_tier:
        assert capability not in role_rules.grants_of(builtins["user"]), (
            f"the built-in `user` role carries {capability}")
        assert capability in role_rules.grants_of(builtins["admin"]), (
            f"the built-in `admin` role is missing {capability}")

    # A role that names nothing confers nothing — not even the defaults.
    assert role_rules.grants_of(None) == frozenset()


def test_the_visibility_put_calls_the_authority_check():
    """
    Both rules are calls inside the handler — they compare a submitted document
    against the stored one, which no dependency can see — so nothing makes a
    future edit keep them. Same reason `_require_scope` is policed this way.
    """
    for name, _decorators, called in _routes_in("admin_router"):
        if name != "put_roles":
            continue
        assert "_require_role_authority" in called, (
            "put_roles no longer checks role authority; an ordinary "
            "admin could rewrite the role catalogue")
        return
    raise AssertionError("admin_router has no put_roles route")


def test_a_deleted_profile_confers_nothing_not_even_the_defaults():
    """
    The one place `grants_for` does NOT fall back to `DEFAULT_GRANTS`.

    Somebody with no role gets the defaults — the tool as it behaves before
    anyone configured anything. Somebody whose role was DELETED is a
    different case: they were deliberately given a job, and losing its
    definition must not silently promote them back to what everyone else gets.
    It fails closed and the portal shows the dangling assignment in red.
    """
    import tempfile
    import grants
    import roles as role_rules

    tmp = tempfile.mkdtemp()
    pid = role_rules.new_id()
    store = _role_store(
        tmp, {pid: {"label": "Gone", "grants": ["msp.rollup"], "visible": []}},
        {"dana": {"role": pid}})
    store.save(hidden={"user": []}, scope=None, actor="test",
               roles={}, people={"dana": {"role": pid}})

    assert store.grants_for("dana") == frozenset(), \
        "a deleted role fell back to the defaults"
    assert store.grants_for("morgan") == frozenset(grants.DEFAULT_GRANTS)


def test_the_default_grants_keep_the_app_from_going_dark():
    """
    `venue.report` is the whole point of PISR, so putting it behind a grant
    without a default would mean the release that introduced roles took the
    only thing most people use away from all of them at once.

    Deliberately just the one. Everything else — config detail, the identity
    trace, running a batch — needs a role or a tag chosen by a person.
    Widening this list is how a grant system quietly becomes decoration.
    """
    import grants
    assert grants.DEFAULT_GRANTS == (grants.GRANT_VENUE_REPORT,), (
        "DEFAULT_GRANTS changed — every signed-in caller now holds "
        f"{list(grants.DEFAULT_GRANTS)} without anybody deciding so")
    for grant in (grants.GRANT_ADMIN_ROLES, grants.GRANT_BATCH_RUN,
                  grants.GRANT_CONFIG_DETAIL, grants.GRANT_IDENTITY_TRACE):
        assert grant not in grants.DEFAULT_GRANTS


def test_the_built_in_roles_are_compiled_in_and_always_resolve():
    """
    `admin.portal` is a capability now, so if every capability came from a file
    on a volume, an unreadable file would mean nobody can administer anything —
    the exact failure the old "admins are never subject to the policy"
    short-circuit existed to prevent.

    So `admin` and `user` live in CODE. A policy file may override them by id;
    a policy file that cannot be read falls back to them. This is the lockout
    story, and it is the one thing in the role system that must not become
    data.
    """
    import grants
    import roles as role_rules
    import visibility

    builtins = role_rules.builtins()
    assert set(builtins) == {"admin", "user"}
    # The administrator role reaches every part of the tool, including the
    # authority to change itself. Narrowing from there is somebody's decision,
    # taken in the portal — not one made for them at install time.
    assert set(builtins["admin"].grants) == set(grants.ALL_GRANTS)
    # And the ordinary one is the tool before anybody configures anything.
    assert set(builtins["user"].grants) == set(grants.DEFAULT_GRANTS)

    # A store pointed at nothing still resolves both.
    empty = visibility.PolicyStore(None)
    assert {r.id for r in empty.roles()} == {"admin", "user"}
    assert grants.GRANT_ADMIN_PORTAL in empty.grants_for(None, "admin")


def test_nobody_can_create_somebody_more_privileged_than_themselves():
    """
    THE HIERARCHY, at the door where it is easiest to forget.

    Rule 2 is usually described as "you may not assign a capability you do not
    hold". Creating an account is the same act reached by a different route —
    if it were unguarded, a restricted administrator would simply make an
    unrestricted colleague and inherit their access by proxy, which is a
    privilege escalation with an extra step and a plausible audit trail.

    Both doors are the accounts router's: POST to make somebody, PATCH to move
    them. This holds both, and holds them for a caller who is a REAL
    administrator — the failure mode is not an outsider, it is somebody
    trusted with part of the job reaching past it.
    """
    import tempfile
    import grants
    import roles as role_rules
    from routers import accounts_router

    tmp = tempfile.mkdtemp()
    desk = role_rules.new_id()
    powerful = role_rules.new_id()
    store = _role_store(tmp, {
        desk: {"id": desk, "label": "Desk admin",
               "grants": [grants.GRANT_ADMIN_PORTAL, grants.GRANT_ADMIN_ACCOUNTS],
               "visible": []},
        powerful: {"id": powerful, "label": "Powerful",
                   "grants": [grants.GRANT_ADMIN_PORTAL,
                              grants.GRANT_ADMIN_ACCOUNTS,
                              grants.GRANT_IDENTITY_TRACE], "visible": []},
    }, {})

    request = _request("/api/admin/accounts", role=desk)
    request.state.pisr_user = "dana"

    def check():
        # Handing out a role carrying only what they hold: fine.
        accounts_router._guard_role_assignment(request, desk)

        # A role carrying one capability more: refused, and the message names
        # the capability so they know what to ask for.
        try:
            accounts_router._guard_role_assignment(request, powerful)
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
            assert grants.GRANT_IDENTITY_TRACE in str(exc.detail)
        else:
            raise AssertionError(
                "a desk administrator created somebody more privileged")

        # The built-in `admin` role carries everything, so it is the obvious
        # thing to reach for and the one that must be refused.
        try:
            accounts_router._guard_role_assignment(request, "admin")
        except HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("a desk administrator handed out the built-in admin")
    _with_policy(store, check)


def test_both_account_doors_call_the_hierarchy_guard():
    """
    `_guard_role_assignment` is a call inside a handler, which nothing makes a
    new route keep — the same reason `_require_scope` is policed this way.

    TWO DOORS: creating somebody and moving them. Guarding one and not the
    other leaves the escalation a single request away, and which one gets
    forgotten is a coin toss.
    """
    for name, _decorators, called in _routes_in("accounts_router"):
        if name not in ("create_account", "update_account"):
            continue
        assert "_guard_role_assignment" in called, (
            f"accounts_router.{name} assigns a role without checking that the "
            "caller could grant it")


def test_every_scope_lookup_passes_an_identity():
    """
    Scope now needs TWO facts about a caller, and a route that composes them
    itself will one day compose only one — silently widening somebody to their
    whole role's reach, which is precisely the direction this control must not
    fail in.

    So no router may call `visibility.scope_for` directly; they ask
    `auth.scope_of(request)`, which cannot be called with half the answer.
    `visibility.scope_for` having no default for `identity` is the other half
    of the same guard: disobeying it is a TypeError, not a widening.
    """
    import inspect
    import visibility

    params = inspect.signature(visibility.scope_for).parameters
    assert params["identity"].default is inspect.Parameter.empty, (
        "visibility.scope_for grew a default for `identity`; a call site that "
        "forgets it would silently get the role's scope")

    # admin_router exempt — see the note in the hidden-lookup test above.
    for module in ("pisr_router", "msp_router", "batch_router"):
        source = (API / "routers" / f"{module}.py").read_text(encoding="utf-8")
        assert "visibility.scope_for(" not in source, (
            f"{module} calls visibility.scope_for directly; use "
            "auth.scope_of(request), which carries the identity too")


def test_both_batch_routers_declare_their_check_at_the_router_level():
    """
    Declared once on each router rather than per route, where a new route
    would have to remember it.

    THREE routers, three capabilities, and the splits are the point:

      router         the writes — starting a run polls every venue of a live
                     customer network
      read_router    ONE tenant's roll-up, the desk's view of one customer
      estate_router  EVERY tenant summed, a management view across companies

    Collapsing the first two would make reading a record and making one the
    same privilege. Collapsing the last two would make "how is this customer
    doing" and "how is the estate doing" the same question, which they are not
    — and on an MSP tenant the ECs are different companies.

    Both name a grant rather than the admin role, which is what took them out
    of /api/admin — a path saying "admin" for a route an ordinary user may
    hold is a lie the next reader has to discover.
    """
    source = (API / "routers" / "batch_router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"router": "GRANT_BATCH_RUN",
              "read_router": "GRANT_TENANT_ROLLUP",
              "estate_router": "GRANT_MSP_ROLLUP"}
    seen = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        name = getattr(node.targets[0], "id", None)
        if name in wanted:
            seen[name] = ast.get_source_segment(source, node.value) or ""
    assert set(seen) == set(wanted), f"batch_router is missing {sorted(set(wanted) - set(seen))}"
    for name, check in wanted.items():
        assert check in seen[name], f"batch_router.{name} does not declare {check}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

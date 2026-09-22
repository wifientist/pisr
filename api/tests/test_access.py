"""
Access control: what is public, what needs a session, what needs an admin.

WHY THIS EXISTS. The gate is three separate mechanisms that have to agree —
`SessionGateMiddleware` (gates by PATH PREFIX, so a new router is gated by
existing), `require_admin` (a per-route dependency, which a new route does NOT
get by existing), and the scope check inside pisr_router (a call inside the
function body, which nothing enforces at all). The first fails safe; the other
two fail open, quietly, the moment somebody adds a route and forgets.

So this file is mostly an INVENTORY. It asserts the exact set of public paths,
the exact set of admin-only routes, and that every route which reaches RUCKUS
ONE either checks scope or is admin-only. A new route makes it fail, and the
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
ADMIN_ONLY = {
    "/api/admin/visibility",
    "/api/admin/baseline",
    "/api/admin/accounts",
    "/api/admin/accounts/{account_id}",
    "/api/admin/accounts/{account_id}/invite",
    "/api/admin/batch/state",
    "/api/admin/batch/runs",
    "/api/admin/batch/runs/{run_id}/venues/{venue_id}",
    "/api/admin/batch/runs/{run_id}/finish",
    "/api/admin/batch/rollup.pdf",
    # NOT under /api/admin, and the one to keep an eye on: it names residents'
    # DPSK usernames, which `shape._dpsk_safe` refuses to put in a report.
    "/api/pisr/{controller_id}/identity/trace",
}


def _app_routes():
    import main
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def _has_require_admin(route) -> bool:
    """Whether `require_admin` runs for this route, router-level or per-route."""
    return any(getattr(dep.call, "__name__", "") == "require_admin"
               for dep in route.dependant.dependencies)


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


def test_admin_routes_are_exactly_these():
    """
    Both directions. A new admin route that forgot the dependency fails here,
    and so does an existing one that lost it.
    """
    with_admin = {r.path for r in _app_routes() if _has_require_admin(r)}
    assert with_admin == ADMIN_ONLY, (
        f"admin surface changed: {sorted(with_admin ^ ADMIN_ONLY)}")


def test_the_admin_namespace_is_all_admin():
    """Anything under /api/admin, whatever it is, needs the role."""
    for route in _app_routes():
        if route.path.startswith("/api/admin"):
            assert _has_require_admin(route), f"{route.path} is not admin-gated"


# ── the gate itself ─────────────────────────────────────────────────


def _request(path: str, cookies=None, role=None) -> Request:
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
    return request


def _through_gate(path: str, cookies=None) -> int:
    """The status the middleware would produce; 200 means it let the request on."""
    gate = auth.SessionGateMiddleware(app=None)

    async def call_next(_request):
        return Response("ok")

    return asyncio.run(gate.dispatch(_request(path, cookies), call_next)).status_code


def test_gated_paths_refuse_an_uncookied_caller():
    if not AUTH.enabled:
        print("      (skipped: PISR_AUTH_DISABLED=1 in this environment)")
        return
    for path in ("/api/status", "/api/config", "/api/pisr/1/report",
                 "/api/pisr/1/report.pdf", "/api/pisr/1/venues",
                 "/api/admin/visibility", "/api/admin/batch/state",
                 "/api/admin/batch/rollup.pdf", "/api/r1/1/msp/mspEcs",
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
        auth.require_admin(_request("/api/admin/visibility", role="user"))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a user role passed require_admin")
    assert auth.require_admin(_request("/api/admin/visibility", role="admin")) == "admin"


def test_a_request_with_no_role_is_a_user_not_an_admin():
    """
    `role_of` fails to the LEAST it can be. A route reached without the
    middleware having run (which should not happen) must not be an admin
    session by omission.
    """
    assert auth.role_of(_request("/api/pisr/1/report")) == "user"
    try:
        auth.require_admin(_request("/api/admin/visibility"))
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


def test_an_admin_is_never_scoped_out_of_the_portal():
    """
    The short-circuit above the file read. An admin has to be able to fix a
    scope even when the policy file is what is broken — and cannot lock
    themselves out of every EC with one bad save.
    """
    import visibility
    assert visibility.scope_for("admin").unrestricted
    assert visibility.hidden_for("admin") == ()


def test_the_batch_router_is_admin_at_the_router_level():
    """
    Every batch route is admin-only, so it is declared once on the router
    rather than per route — where a new route would have to remember it.
    """
    source = (API / "routers" / "batch_router.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", None) == "router"):
            assert "require_admin" in (ast.get_source_segment(source, node.value) or "")
            return
    raise AssertionError("batch_router has no router assignment to check")


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

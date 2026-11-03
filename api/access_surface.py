"""
The access surface, derived from the running app: every route, and what it
takes to reach it.

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" -w /repo/api backend python scripts/access_surface.py

IMPORTED AS WELL AS RUN. `scripts/access_surface.py` is a three-line shim over
this; the admin capability matrix imports `surface()` directly. One derivation,
three consumers — the matrix, the end-to-end suite, and anybody at a terminal.

WHY THIS IS DERIVED AND NOT WRITTEN DOWN. There are three lists that decide who
reaches what — the gate's path prefixes, the per-route dependencies, and the
scope check that is a CALL INSIDE A FUNCTION BODY — and a fourth list
describing them would be the one nobody updates. `test_sections.py` exists only
because three id lists are hand-written in three files; this is the same trap
one layer up, and the answer is to have no fourth list.

TWO CONSUMERS, ONE TRUTH:

  * the admin capability matrix, which draws what a role-and-grant combination
    unlocks — see the note on `narrowed` below before rendering a tick;
  * the end-to-end suite in ~/code/playwright_e2e_testbed, which generates its
    allow/deny expectations from this rather than listing routes by hand.

WHAT THIS CANNOT TELL YOU, and the matrix must say so rather than implying
otherwise: whether a caller is entitled to everything a route returns. Scope
and section visibility are PER PERSON and resolved at request time, so a route
`msp-user` may call still answers with their ECs only and their sections only.
A two-state matrix — reachable or refused — would read as though a grant
widened somebody's reach, which is the exact opposite of the rule it is
describing. `narrowed` is the third state, and it is why this emits
`checksScope` at all.

Read-only, and it starts the app without serving it: importing `main` is what
builds the route table, so this holds whatever the container would.
"""

import ast
import contextlib
import json
import sys
from pathlib import Path

API = Path(__file__).resolve().parent

import auth
import grants
import sections
from fastapi.routing import APIRoute

#: Routes whose scope check lives in a helper this file would otherwise have to
#: follow. `_require_scope` is called by name in prism_router; batch_router's
#: read routes go through `_readable`, which narrows the record instead.
_SCOPE_CALLS = ("_require_scope", "scope_of", "_readable", "_require_ec")


def _dependency_names(route) -> set:
    return {getattr(dep.call, "__name__", "") for dep in route.dependant.dependencies}


def _grants_of(route):
    """
    Every capability a route requires — ALL of them, not any.

    A tuple rather than a single id because a route may name several and the
    caller must hold all; the surface would be lying if it reported only the
    first.
    """
    held: set = set()
    for dep in route.dependant.dependencies:
        if getattr(dep.call, "__name__", "") == "require_grants":
            # UNIONED, not first-wins. A route may carry one from its router
            # and another of its own — `/batch/sweeps` needs `batch.run` from
            # the router AND `msp.batch` from the route — and reporting only
            # one would describe a weaker rule than the app enforces.
            held.update(getattr(dep.call, "prism_grants", ()))
    return tuple(sorted(held))


def _scope_checking_functions() -> set:
    """
    Route handlers that call something which enforces scope.

    An AST scan rather than introspection, because this is a call inside a
    function body — nothing about the route object records it, which is the
    whole reason `test_access.py` polices it separately.
    """
    found = set()
    for path in sorted((API / "routers").glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            called = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    target = sub.func
                    if isinstance(target, ast.Name):
                        called.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        called.add(target.attr)
            if called & set(_SCOPE_CALLS):
                found.add(node.name)
    return found


def surface() -> dict:
    # Importing the app configures logging and announces itself on STDOUT —
    # the gate's mode, the accounts tally, the recovery notice. That is
    # right for a container and fatal for a machine-readable oracle, so the
    # import's output goes to stderr and stdout carries JSON and nothing else.
    # Redirected around the import rather than by reconfiguring logging,
    # because anything printed by any future import is caught the same way.
    with contextlib.redirect_stdout(sys.stderr):
        import main

    scope_checkers = _scope_checking_functions()
    routes = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        handler = getattr(route.endpoint, "__name__", "")
        public = (route.path in auth._PUBLIC_PATHS
                  or route.path == "/api/enroll/{token}"
                  or not route.path.startswith(auth._GATED_PREFIXES))
        routes.append({
            "path": route.path,
            "methods": sorted(route.methods - {"HEAD", "OPTIONS"}),
            "handler": handler,
            # The three checks, reported separately because they are three
            # different questions and a caller can pass one and fail another.
            "public": public,
            # `require_admin` is now an alias for
            # `require_grants("admin.portal")`, so admin-ness shows up as a
            # capability like everything else rather than as a separate flag.
            "requiresGrants": list(_grants_of(route)),
            # Not "is scoped" — "narrows what it returns by the caller's
            # scope". A route can be reachable AND narrowed, which is the
            # third state the matrix has to render.
            "checksScope": handler in scope_checkers,
            "summary": (route.description or "").strip().split("\n")[0][:120],
        })

    routes.sort(key=lambda r: r["path"])
    return {
        # The vocabulary a role may bundle. Fixed, shipped with the release:
        # a grant an admin could invent would be enforced by nothing, which is
        # a permission that silently does nothing. See api/grants.py.
        "grants": list(grants.ALL_GRANTS),
        # What the built-in `user` role carries. Part of the surface because
        # a consumer working out "may this persona reach that route" has to
        # know that most people hold `venue.report` without anybody having
        # decided anything.
        "defaultGrants": list(grants.DEFAULT_GRANTS),
        # Each TOOL and the capabilities it needs — all of them. The
        # end-to-end suite works out who may reach what from this plus the
        # personas, so it has to be the same list the SPA gates on.
        "features": [dict(f) for f in grants.features()],
        "sectionCount": len(sections.ALL_CHECK_IDS),
        "routes": routes,
    }

"""
The drift guard for section visibility.

WHY THIS EXISTS. Section ids are written by hand into three places — the
catalogue in `api/sections.py`, the cards in `src/pages/PISR.tsx`, and the
guards in `api/templates/reports/pisr.html` — because there is no shared module
the frontend build and the Python process can both import (the Dockerfile's web
stage does not copy `api/`, deliberately). Three hand-kept lists drift. This is
the thing that notices.

The failure it is really guarding against is the quiet one: someone adds a card,
adds it to the catalogue, and never touches the PDF. Everything looks correct on
screen for months, and the first time anyone notices is when a user's PDF
contains a section their browser does not — which is exactly the bug the
data-filtering design was chosen to make impossible, undone by a typo.

RUNS WITHOUT PYTEST, on purpose. Adding pytest to requirements.txt would put a
test framework in the production image for one file. Under pytest the `test_*`
functions are collected normally; run directly it executes them itself.

NEEDS THE REPO, NOT THE IMAGE. Half of what this checks lives in
`src/pages/PISR.tsx`, and the runtime image contains only `api/` — so running
it inside the app container finds no frontend and fails on a missing file.
Mount the working tree:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" backend python /repo/api/tests/test_sections.py

That is the command to put in CI, and the reason `_repo_root` below hunts for
the tree rather than assuming a layout.

It reads the frontend and the template as TEXT rather than importing or parsing
them. A real parser would be more precise and would also be a dependency and a
thing to maintain; these are regexes over files whose shape this repo controls,
and a false failure here costs a minute while a false pass costs the feature.
"""

import re
import sys
from pathlib import Path

# api/tests/ -> api/
API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import sections as catalogue          # noqa: E402
import redact                         # noqa: E402

def _repo_root() -> Path:
    """
    The working tree, walking up from this file.

    Not simply `API.parent`: in the dev container `api/` is mounted at `/app`,
    which would make the repo root `/` and the frontend a missing file with a
    confusing error. Walking up and testing for the file we actually need gives
    the same answer on a host checkout and an explicit failure everywhere else.
    """
    for candidate in [API.parent, *API.parents]:
        if (candidate / "src" / "pages" / "PISR.tsx").is_file():
            return candidate
    raise SystemExit(
        "Could not find src/pages/PISR.tsx above " + str(API) + ".\n"
        "This test needs the working tree, not just the runtime image. Run it as:\n"
        "  docker compose -f docker-compose.dev.yml run --rm --no-deps \\\n"
        '    -v "$PWD:/repo" backend python /repo/api/tests/test_sections.py')


REPO = _repo_root()
PISR_TSX = REPO / "src" / "pages" / "PISR.tsx"
TEMPLATE = API / "templates" / "reports" / "pisr.html"
CHECKS_PY = API / "services" / "pisr" / "checks.py"


def _tsx_rendered_ids():
    """Ids actually tagged onto a Card or Section in the React page."""
    text = PISR_TSX.read_text(encoding="utf-8")
    return set(re.findall(r'<(?:Card|Section)[^>]*?\sid="([a-z][a-z.\-]+)"', text, re.S))


def _tsx_declared_ids():
    """The SECTION_IDS array the page registers for tab-emptiness counting."""
    text = PISR_TSX.read_text(encoding="utf-8")
    block = re.search(r"export const SECTION_IDS = \[(.*?)\] as const;", text, re.S)
    assert block, "PISR.tsx no longer exports a SECTION_IDS array"
    return set(re.findall(r'"([a-z][a-z.\-]+)"', block.group(1)))


def _template_guard_ids():
    return set(re.findall(r"visible\(\s*'([^']+)'\s*\)", TEMPLATE.read_text(encoding="utf-8")))


def _template_guard_tabs():
    return set(re.findall(r"visible_tab\(\s*'([^']+)'\s*\)",
                          TEMPLATE.read_text(encoding="utf-8")))


def _check_ids_in_checks_py():
    """
    Every check id `checks.py` can emit.

    Matches any private helper called with a kebab-case string as its first
    argument, not just `_finding(...)` — because not every check calls
    `_finding` directly. `check_ap_firmware` passes "ap-firmware" to
    `_firmware_check`, which forwards it, and a narrower pattern reports those
    two as missing. Over-matching is harmless here: the assertion is only that
    the catalogue's ids are a subset of this, so a stray literal that is not
    really a check id can never cause a false pass.
    """
    text = CHECKS_PY.read_text(encoding="utf-8")
    return set(re.findall(r'\b_[a-z_]+\(\s*"([a-z0-9][a-z0-9\-]*)"', text))


# ── the catalogue is internally consistent ───────────────────────────

def test_ids_are_unique():
    ids = [s.id for s in catalogue.SECTIONS]
    assert len(ids) == len(set(ids)), "duplicate section id in api/sections.py"


def test_id_prefix_matches_tab():
    """
    `<tab>.<thing>`, which is not cosmetic.

    `tabVisible` in src/context/VisibilityContext.tsx decides whether a tab has
    anything left on it by prefix-matching hidden ids. Break the convention and
    it silently answers "visible" for every tab — the safe direction, so nothing
    would look broken, which is why it needs asserting rather than noticing.
    """
    for section in catalogue.SECTIONS:
        assert section.id.startswith(f"{section.tab}."), (
            f"{section.id} is on tab {section.tab!r} but is not prefixed with it")


def test_tabs_are_declared():
    known = {tab for tab, _ in catalogue.TABS}
    for section in catalogue.SECTIONS:
        assert section.tab in known, f"{section.id} names undeclared tab {section.tab!r}"


def test_paths_have_one_owner():
    """
    Two sections owning one path is the subtle catalogue bug.

    Hiding either would empty data the other still renders, so one section
    disappearing takes a second one's contents with it — and the second one
    still draws its heading, because nobody hid it.
    """
    seen = {}
    for section in catalogue.SECTIONS:
        for path in section.paths:
            assert path not in seen, (
                f"path {path!r} is owned by both {seen[path]} and {section.id}")
            seen[path] = section.id


def test_checks_exist():
    """
    A check id the catalogue names but `checks.py` never emits.

    This is the leak: `redact.py` filters findings by id, so a renamed check
    quietly stops being filtered and its finding reappears in a report whose
    section is hidden — a PoE warning on a page with no PoE cards.
    """
    real = _check_ids_in_checks_py()
    for section in catalogue.SECTIONS:
        for check in section.checks:
            assert check in real, (
                f"{section.id} claims check {check!r}, which checks.py does not "
                f"emit. Findings for it will NOT be filtered.")


# ── the three lists agree ────────────────────────────────────────────

def test_frontend_renders_every_section():
    missing = set(catalogue.IDS) - _tsx_rendered_ids()
    assert not missing, (
        f"in api/sections.py but not tagged onto any Card/Section in "
        f"src/pages/PISR.tsx: {sorted(missing)}")


def test_frontend_renders_nothing_extra():
    extra = _tsx_rendered_ids() - set(catalogue.IDS)
    assert not extra, (
        f"tagged in src/pages/PISR.tsx but unknown to api/sections.py "
        f"(so it can never be hidden): {sorted(extra)}")


def test_frontend_declaration_matches():
    """
    SECTION_IDS drives tab-emptiness counting, so a stale one hides a tab too
    eagerly or not eagerly enough — both silent.
    """
    assert _tsx_declared_ids() == set(catalogue.IDS), (
        "SECTION_IDS in src/pages/PISR.tsx does not match api/sections.py: "
        f"missing {sorted(set(catalogue.IDS) - _tsx_declared_ids())}, "
        f"extra {sorted(_tsx_declared_ids() - set(catalogue.IDS))}")


def test_template_guards_are_known():
    """
    Every id the PDF guards on must exist.

    Not the converse: plenty of sections have no PDF counterpart at all, and
    others need no guard because emptying their data already removes the whole
    block (the PoE tables, for one — see the note in the template). A guard on
    an id that does NOT exist, though, is always a bug: it is either a typo or
    a section that was renamed, and it fails open, so the PDF keeps printing a
    section the screen has stopped showing.
    """
    unknown = _template_guard_ids() - set(catalogue.IDS)
    assert not unknown, (
        f"api/templates/reports/pisr.html guards on unknown section id(s): "
        f"{sorted(unknown)}")


def test_template_tab_guards_are_known():
    unknown = _template_guard_tabs() - {tab for tab, _ in catalogue.TABS}
    assert not unknown, (
        f"api/templates/reports/pisr.html guards on unknown tab(s): {sorted(unknown)}")


# ── the redactor does what the catalogue promises ────────────────────

def _sample_report():
    """A report shaped like a real one in the places the catalogue names."""
    return {
        "inventory": {"aps": {"total": 4, "online": 4, "byModel": [{"label": "R650"}]},
                      "switches": {"total": 1},
                      "rows": {"aps": [{"name": "ap-1"}], "switches": [{"name": "sw-1"}]}},
        "poe": {"switches": [{"name": "sw-1"}], "apsOnPoe": [{"ap": "ap-1"}],
                "topConsumers": [{"watts": 12}], "byType": [{"label": "802.3at"}]},
        "ports": {"bySpeed": [{"label": "1 Gbps"}], "errored": [{"port": "1/1/1"}]},
        "clients": {"total": 9, "byBand": [{"label": "5 GHz"}], "byRssi": [],
                    "bySsid": [], "byHealth": [], "topAps": []},
        "addressing": {"apSubnets": [{"cidr": "10.0.0.0/24"}], "apsWithoutIp": 1,
                       "switchSubnets": [], "gateways": [], "dns": [],
                       "external": [{"ip": "203.0.113.4"}], "dhcpPools": [{"name": "p"}]},
        "verification": {
            "findings": [
                {"id": "poe-budget", "severity": "warning"},
                {"id": "aps-online", "severity": "ok"},
                {"id": "dhcp-pools", "severity": "ok"},
            ],
            "counts": {"critical": 0, "warning": 1, "info": 0, "ok": 2, "skipped": 0},
            "score": {"passed": 2, "ran": 3},
        },
        "meta": {"sources": ["GET /venues/{venueId}"], "venueId": "v1"},
    }


def test_redact_empties_owned_paths_and_keeps_the_type():
    report = _sample_report()
    out = redact.redact(report, ["wired.poe-budget"])
    assert out["poe"]["switches"] == [], "the owned path was not emptied"
    assert isinstance(out["poe"]["switches"], list), (
        "emptied to the wrong type — two renderers call .length on this")
    assert out["poe"]["apsOnPoe"], "a path this section does not own was emptied"


def test_redact_does_not_mutate_its_input():
    report = _sample_report()
    redact.redact(report, ["wired.poe-budget"])
    assert report["poe"]["switches"], (
        "redact mutated the report it was given; a second call for a second "
        "role would see an already-emptied payload")


def test_redact_drops_findings_and_retallies():
    out = redact.redact(_sample_report(), ["wired.poe-budget"])
    ids = {f["id"] for f in out["verification"]["findings"]}
    assert "poe-budget" not in ids, (
        "the finding survived its section, so the Verification card reports on "
        "cards that are not there")
    assert out["verification"]["counts"]["warning"] == 0, "counts not recomputed"
    assert out["verification"]["score"]["ran"] == 2, "score not recomputed"


def test_redact_stamps_visibility_even_when_nothing_is_hidden():
    out = redact.redact(_sample_report(), [])
    assert out["visibility"] == {"hidden": [], "redacted": False}


def test_redact_ignores_unknown_ids():
    out = redact.redact(_sample_report(), ["nope.not-a-section"])
    assert out["poe"]["switches"], "an unknown id emptied something"
    assert out["visibility"]["redacted"] is True


def test_template_helpers_fail_open():
    helpers = redact.template_helpers(["wired.poe-budget"])
    assert helpers["visible"]("wired.poe-budget") is False
    assert helpers["visible"]("wired.port-errors") is True
    assert helpers["visible"]("typo.not-real") is True, "guards must fail open"
    assert helpers["visible_tab"]("wired") is True, "one hidden section is not a whole tab"


def test_template_helpers_hide_an_entirely_hidden_tab():
    every_wired = [s.id for s in catalogue.SECTIONS if s.tab == "wired"]
    helpers = redact.template_helpers(every_wired)
    assert helpers["visible_tab"]("wired") is False
    assert helpers["visible_tab"]("wireless") is True


# ── scope: the half that fails CLOSED ────────────────────────────────
#
# Every assertion below is written from the refusing side. The section tests
# above check that things are hidden; these check that things are REFUSED, and
# the two failure modes are not symmetric — a section wrongly shown is clutter,
# an EC wrongly reachable is one customer seeing another's sites.

import scope as scope_rules                        # noqa: E402
from config import CONTROLLER                      # noqa: E402

EC_A, EC_B = "tenant-aaa", "tenant-bbb"
VEN_1, VEN_2 = "venue-111", "venue-222"


def _restricted(ecs):
    return scope_rules.parse({"unrestricted": False, "ecs": ecs})


def test_scope_unset_allows_everything():
    """The state every existing deployment is in, and it must not change."""
    s = scope_rules.parse(None)
    assert s.unrestricted
    assert s.allows_ec(EC_A) and s.allows_venue(EC_A, VEN_1)
    assert s.filter_ecs([{"id": EC_A}]) == [{"id": EC_A}]


def test_scope_refuses_an_unnamed_ec():
    s = _restricted({EC_A: scope_rules.ALL_VENUES})
    assert s.allows_ec(EC_A)
    assert not s.allows_ec(EC_B), "an EC nobody named must be refused"
    assert not s.allows_venue(EC_B, VEN_1)


def test_scope_refuses_an_unnamed_venue():
    s = _restricted({EC_A: [VEN_1]})
    assert s.allows_venue(EC_A, VEN_1)
    assert not s.allows_venue(EC_A, VEN_2)


def test_empty_venue_list_means_none_not_all():
    """
    The single worst bug this file could have.

    An admin who ticks a customer and unticks all of its venues has said
    something specific. Reading that as "all of them" would hand over the whole
    customer at the exact moment someone was trying to lock it down.
    """
    s = _restricted({EC_A: []})
    assert s.allows_ec(EC_A)
    assert not s.allows_venue(EC_A, VEN_1)
    assert s.filter_venues(EC_A, [{"id": VEN_1}, {"id": VEN_2}]) == []


def test_empty_ec_map_means_nothing_reachable():
    s = _restricted({})
    assert not s.unrestricted
    assert not s.allows_ec(EC_A)
    assert s.filter_ecs([{"id": EC_A}, {"id": EC_B}]) == []


def test_filter_drops_rows_it_cannot_identify():
    """Fail closed on a row with no id, rather than passing it through."""
    s = _restricted({EC_A: scope_rules.ALL_VENUES})
    kept = s.filter_ecs([{"id": EC_A}, {"name": "no id here"}, {"tenantId": EC_A}])
    assert len(kept) == 2, "a row with no identifier must be dropped, not kept"


def test_single_tenant_controller_is_keyed_by_its_real_id():
    """
    `resolve_tenant` hands None to an EC-type controller, which addresses
    itself. Keying that under the string "None" would file the policy somewhere
    the portal never writes to, and the restriction would silently do nothing.
    """
    s = _restricted({CONTROLLER.tenant_id: [VEN_1]})
    assert s.allows_ec(None)
    assert s.allows_venue(None, VEN_1)
    assert not s.allows_venue(None, VEN_2)


def test_malformed_venue_list_grants_nothing():
    s = _restricted({EC_A: "not-a-list-and-not-the-star"})
    assert s.allows_ec(EC_A)
    assert not s.allows_venue(EC_A, VEN_1), (
        "an unreadable venue list must grant no venues, not every venue")


def test_corrupt_scope_block_is_no_restriction_not_a_lockout():
    """
    The one place scope deliberately does NOT fail closed, and it is worth
    asserting so nobody "fixes" it: a scope block that cannot be read describes
    no restriction, which is where a deployment that never used the feature
    already is. Refusing everything would be an outage caused by a typo.
    """
    for junk in ({"unrestricted": False, "ecs": "nonsense"}, {"unrestricted": False},
                 "garbage", 42, []):
        assert scope_rules.parse(junk).unrestricted, f"{junk!r} should be no restriction"


def test_clean_round_trips_and_normalises_away_unrestricted():
    assert scope_rules.clean({"unrestricted": True, "ecs": {}}) is None
    assert scope_rules.clean(None) is None
    out = scope_rules.clean({"unrestricted": False,
                             "ecs": {EC_A: scope_rules.ALL_VENUES, EC_B: [VEN_2, VEN_1]}})
    assert out == {"unrestricted": False,
                   "ecs": {EC_A: "*", EC_B: [VEN_1, VEN_2]}}, (
        "clean must sort venue ids so an unchanged policy does not look dirty")


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

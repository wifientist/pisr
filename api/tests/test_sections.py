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
    """
    Checked against SECTION_IDS, not against the tagged occurrences.

    Most cards carry a literal `id="..."`, but the Config tab builds its
    category ids at runtime — `id={`config.${cat.slug}`}` over a list from the
    server — and a regex cannot see those. SECTION_IDS is the page's own
    declaration of what it renders (it is registered at load and drives
    tab-emptiness), so it is the honest thing to compare against.

    The converse check below still runs over the literal tags, so a card
    tagged with an id nobody knows is still caught.
    """
    missing = set(catalogue.IDS) - _tsx_declared_ids()
    assert not missing, (
        f"in api/sections.py but not in SECTION_IDS in src/pages/PISR.tsx: "
        f"{sorted(missing)}")


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
    out = redact.redact(report, ["poe.budget"])
    assert out["poe"]["switches"] == [], "the owned path was not emptied"
    assert isinstance(out["poe"]["switches"], list), (
        "emptied to the wrong type — two renderers call .length on this")
    assert out["poe"]["apsOnPoe"], "a path this section does not own was emptied"


def test_redact_does_not_mutate_its_input():
    report = _sample_report()
    redact.redact(report, ["poe.budget"])
    assert report["poe"]["switches"], (
        "redact mutated the report it was given; a second call for a second "
        "role would see an already-emptied payload")


def test_redact_drops_findings_and_retallies():
    out = redact.redact(_sample_report(), ["poe.budget"])
    ids = {f["id"] for f in out["verification"]["findings"]}
    assert "poe-budget" not in ids, (
        "the finding survived its section, so the Verification card reports on "
        "cards that are not there")
    assert out["verification"]["counts"]["warning"] == 0, "counts not recomputed"
    assert out["verification"]["score"]["ran"] == 2, "score not recomputed"


def test_hiding_a_config_category_removes_its_data_not_just_the_card():
    """
    Config categories are a list, so no dotted path can own one. Without the
    slug filter in redact they would be markup-hidden only, and the categories
    are the unit an admin was given to hand subsets to users.
    """
    report = _sample_report()
    report["config"] = {"categories": [
        {"slug": "radio", "label": "Radio", "data": {"a": 1}},
        {"slug": "mesh", "label": "Mesh", "data": {"b": 2}}]}
    out = redact.redact(report, ["config.radio"])
    slugs = [c["slug"] for c in out["config"]["categories"]]
    assert slugs == ["mesh"], f"hidden category survived: {slugs}"


def test_redact_stamps_visibility_even_when_nothing_is_hidden():
    out = redact.redact(_sample_report(), [])
    assert out["visibility"] == {"hidden": [], "redacted": False}


def test_redact_ignores_unknown_ids():
    out = redact.redact(_sample_report(), ["nope.not-a-section"])
    assert out["poe"]["switches"], "an unknown id emptied something"
    assert out["visibility"]["redacted"] is True


def test_template_helpers_fail_open():
    helpers = redact.template_helpers(["poe.budget"])
    assert helpers["visible"]("poe.budget") is False
    assert helpers["visible"]("wired.port-errors") is True
    assert helpers["visible"]("typo.not-real") is True, "guards must fail open"
    assert helpers["visible_tab"]("poe") is True, "one hidden section is not a whole tab"


def test_template_helpers_hide_an_entirely_hidden_tab():
    every_poe = [s.id for s in catalogue.SECTIONS if s.tab == "poe"]
    helpers = redact.template_helpers(every_poe)
    assert helpers["visible_tab"]("poe") is False
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


# ── renamed section ids ──────────────────────────────────────────────

import visibility                                  # noqa: E402


def test_renames_point_at_real_sections():
    """
    A rename entry naming a section that does not exist migrates a hidden id
    into nothing, and `_clean` then drops it — silently un-hiding a section an
    admin hid. Exactly the failure the map exists to prevent.
    """
    for old, new in visibility.RENAMED.items():
        assert new in catalogue.BY_ID, (
            f"RENAMED maps {old!r} to {new!r}, which is not in the catalogue")
        assert old not in catalogue.BY_ID, (
            f"RENAMED maps {old!r}, but a section with that id still exists")


def test_a_renamed_id_stays_hidden():
    """The whole point: an old policy keeps hiding what it was hiding."""
    if not visibility.RENAMED:
        return
    old, new = next(iter(visibility.RENAMED.items()))
    cleaned = visibility.PolicyStore._clean({"user": [old]})
    assert cleaned.get("user") == [new], (
        f"{old!r} should have migrated to {new!r}, got {cleaned}")


def test_a_deleted_id_is_dropped():
    cleaned = visibility.PolicyStore._clean({"user": ["wired.top-poe-draws"]})
    assert not cleaned.get("user"), "a removed section should not survive in a policy"


# ── wired clients ────────────────────────────────────────────────────

from services.pisr.shape import wired_client_card    # noqa: E402


def _wired_fixture():
    aps = [{"name": "ap-lobby", "mac": "AA:BB:CC:00:00:01", "serial": "S1"}]
    switches = [{"switchMac": "d4:bd:4f:2f:d4:2c", "id": "d4:bd:4f:2f:d4:2c"}]
    ports = [
        {"switchMac": "d4:bd:4f:2f:d4:2c", "portIdentifier": "1/1/5",
         "neighborMacAddress": "aa-bb-cc-00-00-01", "neighborName": "ap-lobby.5G"},
        {"switchMac": "d4:bd:4f:2f:d4:2c", "portIdentifier": "1/1/6",
         "neighborName": "ap-lobby.2G"},
        {"switchMac": "d4:bd:4f:2f:d4:2c", "portIdentifier": "1/1/7",
         "neighborName": "desk-sw"},
    ]

    def row(mac, port, **extra):
        base = {"clientMac": mac, "switchMac": "d4:bd:4f:2f:d4:2c",
                "switchPort": port, "switchName": "Core", "clientType": "OTHER"}
        base.update(extra)
        return base

    rows = [
        row("11:11:11:00:00:01", "1/1/5"),            # behind an AP (MAC match)
        row("11:11:11:00:00:02", "1/1/6"),            # behind an AP (name match)
        row("AA:BB:CC:00:00:01", "1/1/5"),            # the AP itself
        row("d4:bd:4f:2f:d4:2c", "1/1/1"),            # the switch itself
        row("22:22:22:00:00:01", "1/1/7", clientVlan="20", vlanName="VOICE",
            clientIpv4Addr="10.0.20.5", clientType="VOIP"),
        row("22:22:22:00:00:02", "1/1/7", clientVlan="1011", vlanName=""),
    ]
    return wired_client_card(rows, ports, aps, switches)


def test_wired_excludes_clients_learned_through_an_ap():
    """
    The number that would otherwise be wrong, and wrong in a way nobody
    questions: every wireless client is learned on its AP's uplink port, so a
    raw row count moves when someone joins the Wi-Fi.
    """
    card = _wired_fixture()
    assert card["behindAps"] == 2, "both the MAC-matched and name-matched ports"
    assert card["total"] == 2


def test_wired_excludes_the_aps_and_switches_themselves():
    card = _wired_fixture()
    assert card["infrastructure"] == 2


def test_wired_counts_add_up_in_public():
    """The three published figures must reconcile, or the card is lying."""
    card = _wired_fixture()
    assert card["total"] + card["behindAps"] + card["infrastructure"] == card["learned"]


def test_wired_vlan_label_carries_the_name_only_when_there_is_one():
    labels = [r["label"] for r in _wired_fixture()["byVlan"]]
    assert "20 (VOICE)" in labels
    assert "1011" in labels, "a VLAN with no name must not render an empty ()"


def test_wired_ip_coverage_is_counted_not_assumed():
    card = _wired_fixture()
    assert card["withIp"] == 1, "R1's IP binding is partial; the count is the honest form"


def test_wired_handles_an_empty_table():
    card = wired_client_card([], [], [], [])
    assert card["learned"] == 0 and card["total"] == 0
    assert card["bySwitch"] == [] and card["topPorts"] == []


# ── R1 alarms ────────────────────────────────────────────────────────

from services.pisr.shape import incident_card            # noqa: E402


def _alarm(**kw):
    base = {"id": "x", "name": "ApDisConnected", "severity": "Major",
            "entityType": "AP", "entityId": "S1", "serialNumber": "S1",
            "startTime": 1785702964230,
            "message": '{"message_template":"AP @@apName disconnected from the cloud controller."}'}
    base.update(kw)
    return base


def test_alarm_message_template_is_unwrapped_and_named():
    """
    `message` is a JSON string wrapping a template full of @@tokens. Left
    alone the reader sees "@@apName" where a device name belongs.
    """
    card = incident_card([_alarm()], [{"serial": "S1", "name": "ap-lobby"}], [])
    assert card["rows"][0]["text"] == "AP ap-lobby disconnected from the cloud controller."


def test_alarm_falls_back_to_the_serial_when_the_device_is_unknown():
    """An Edge alarm names a device PISR has no inventory for."""
    card = incident_card([_alarm(entityType="EDGE", serialNumber="EDGE9",
                                 entityId="EDGE9")], [], [])
    assert "EDGE9" in card["rows"][0]["text"]
    assert "@@" not in card["rows"][0]["text"], "no placeholder may survive to the UI"


def test_alarm_survives_an_unparseable_message():
    """An ugly title beats an alarm swallowed by a JSON error."""
    card = incident_card([_alarm(message="not json at all")], [], [])
    assert card["rows"][0]["text"] == "not json at all"
    card2 = incident_card([_alarm(message=None)], [], [])
    assert card2["rows"][0]["text"] == "ApDisConnected", "falls back to the alarm type"


def test_alarms_sort_by_severity_then_newest():
    rows = incident_card([
        _alarm(id="a", severity="Minor", startTime=3000),
        _alarm(id="b", severity="Critical", startTime=1000),
        _alarm(id="c", severity="Major", startTime=2000),
        _alarm(id="d", severity="Major", startTime=9000),
    ], [], [])["rows"]
    assert [r["id"] for r in rows] == ["b", "d", "c", "a"]


def test_alarm_float_timestamps_convert():
    """R1 sends startTime as an int and sometimes as a float."""
    card = incident_card([_alarm(startTime=1785702964230.0)], [], [])
    assert card["rows"][0]["raisedAt"].startswith("2026-"), card["rows"][0]["raisedAt"]


def test_alarm_unknown_severity_sorts_last_not_dropped():
    rows = incident_card([_alarm(id="weird", severity="Catastrophic"),
                          _alarm(id="major", severity="Major")], [], [])["rows"]
    assert [r["id"] for r in rows] == ["major", "weird"]
    assert len(rows) == 2, "an unfamiliar severity is the one most worth showing"


def test_no_alarms_is_an_empty_card_not_an_error():
    card = incident_card([], [], [])
    assert card["total"] == 0 and card["rows"] == [] and card["oldest"] is None


# ── punch list ───────────────────────────────────────────────────────

from services.pisr import punchlist as punchlist_builder     # noqa: E402
from services.pisr import checks as check_registry           # noqa: E402


def test_every_check_has_a_category():
    """
    A check missing from CHECK_CATEGORY silently lands in "Devices not up".
    That is the safe default — better over-reported than filed where nobody
    looks — but it should be a fallback, not how checks normally arrive.
    """
    known = set(_check_ids_in_checks_py())
    uncategorised = sorted(known - set(punchlist_builder.CHECK_CATEGORY))
    assert not uncategorised, (
        f"checks with no punch-list category: {uncategorised}")


def test_categories_referenced_are_declared():
    declared = {key for key, _, _ in punchlist_builder.CATEGORIES}
    for check, category in punchlist_builder.CHECK_CATEGORY.items():
        assert category in declared, f"{check} names unknown category {category!r}"


def _verification(*findings):
    return {"findings": list(findings)}


def _f(check_id, severity, evidence=None):
    return {"id": check_id, "severity": severity, "check": check_id,
            "title": check_id, "summary": "s", "evidence": evidence or []}


def test_passes_are_counted_not_listed():
    """A punch list of things already done is a report, not a list."""
    out = punchlist_builder.build(
        _verification(_f("aps-online", "ok"), _f("port-errors", "critical")), {})
    assert out["passed"] == 1
    assert out["total"] == 1
    assert [t["id"] for g in out["groups"] for t in g["tasks"]] == ["port-errors"]


def test_skipped_is_listed_separately_not_as_a_pass():
    out = punchlist_builder.build(_verification(_f("dhcp-pools", "skipped")), {})
    assert out["total"] == 0 and out["passed"] == 0
    assert [r["id"] for r in out["skipped"]] == ["dhcp-pools"]


def test_groups_follow_install_order_and_severity():
    out = punchlist_builder.build(_verification(
        _f("ap-naming", "info"),        # documentation, last group
        _f("port-errors", "warning"),   # cabling
        _f("aps-online", "critical"),   # devices, first group
    ), {})
    assert [g["key"] for g in out["groups"]] == ["devices", "cabling", "documentation"]


def test_alarms_become_tasks_in_their_own_group():
    out = punchlist_builder.build(_verification(), {"rows": [
        {"id": "a1", "severity": "Major", "text": "AP x disconnected",
         "device": "ap-1", "serial": "S1", "entityType": "AP", "type": "ApDisConnected",
         "raisedAt": "2026-08-01T00:00:00+00:00"}]})
    group = next(g for g in out["groups"] if g["key"] == "alarms")
    assert group["tasks"][0]["severity"] == "critical", "R1 Major means go and look"
    assert group["devices"] == ["ap-1"]


def test_devices_to_visit_are_deduplicated():
    """The number a crew plans around, not the sum of the evidence rows."""
    out = punchlist_builder.build(_verification(
        _f("aps-online", "critical", [{"ap": "ap-1"}, {"ap": "ap-2"}]),
        _f("ap-uplink-speed", "warning", [{"ap": "ap-1"}]),
    ), {})
    assert out["deviceCount"] == 2


def test_redact_rebuilds_the_punchlist_from_filtered_findings():
    """
    The leak this guards: a task naming a finding the reader is no longer
    shown. The punch list is rebuilt from the redacted copies, so hiding a
    section must remove its tasks as well as its findings.
    """
    report = _sample_report()
    report["incidents"] = {"rows": []}
    report["punchlist"] = punchlist_builder.build(report["verification"],
                                                  report["incidents"])
    assert any(t["id"] == "poe-budget"
               for g in report["punchlist"]["groups"] for t in g["tasks"])

    out = redact.redact(report, ["poe.budget"])
    ids = {t["id"] for g in out["punchlist"]["groups"] for t in g["tasks"]}
    assert "poe-budget" not in ids, "a hidden section's task survived into the punch list"


def test_hiding_the_punchlist_does_not_repopulate_it():
    report = _sample_report()
    report["incidents"] = {"rows": []}
    report["punchlist"] = punchlist_builder.build(report["verification"],
                                                  report["incidents"])
    out = redact.redact(report, ["punchlist.tasks"])
    assert out["punchlist"] == {}, "rebuilding must not undo the emptying"


# ── credential scrubbing ─────────────────────────────────────────────
#
# The highest-stakes tests in this file. R1 returns live switch and AP admin
# passwords inside ordinary configuration responses, and a report is a document
# that gets emailed to an install crew.

import scrub as secret_scrub                                 # noqa: E402


def test_the_two_credentials_r1_actually_returns_are_removed():
    """
    Both observed live, verbatim key names:
      GET /venues/{id}/switchSettings  -> switchLoginPassword
      GET /venues/{id}/aps/{serial}    -> loginPassword
    """
    payload = {"venue": {"switchSettings": {"switchLoginUsername": "admin",
                                            "switchLoginPassword": "2VsU^Kd4D%"}},
               "aps": [{"name": "ap-1", "loginPassword": "0kzREJ#lp!31C*9*"}]}
    out, removed = secret_scrub.scrub(payload)
    assert out["venue"]["switchSettings"]["switchLoginPassword"] == secret_scrub.REDACTED
    assert out["aps"][0]["loginPassword"] == secret_scrub.REDACTED
    assert out["venue"]["switchSettings"]["switchLoginUsername"] == "admin", (
        "the username is configuration, not a credential")
    assert set(removed) == {"venue.switchSettings.switchLoginPassword",
                            "aps[].loginPassword"}


def test_scrub_reaches_every_depth_and_inside_lists():
    payload = {"a": [{"b": [{"c": {"apiKey": "xyz"}}]}]}
    out, removed = secret_scrub.scrub(payload)
    assert out["a"][0]["b"][0]["c"]["apiKey"] == secret_scrub.REDACTED
    assert removed == ["a[].b[].c.apiKey"]


def test_scrub_matches_regardless_of_casing_or_separators():
    for key in ("Password", "shared_secret", "SharedSecret", "PRE_SHARED_KEY",
                "api-key", "sessionKey", "authToken"):
        out, removed = secret_scrub.scrub({key: "value"})
        assert out[key] == secret_scrub.REDACTED, f"{key} was not redacted"
        assert removed == [key]


def test_scrub_does_not_eat_the_dpsk_card():
    """
    The regression that made this file careful. A bare "psk" substring matched
    "dpsk", redacting the whole DPSK card and breaking the PDF — the guard
    destroying the report it exists to protect.
    """
    payload = {"dpsk": {"inUse": True, "poolCount": 2, "passphraseTotal": 40,
                        "pools": [{"name": "p", "passphrases": 12}]}}
    out, removed = secret_scrub.scrub(payload)
    assert removed == [], f"nothing here is a credential, got {removed}"
    assert out == payload


def test_scrub_still_catches_a_bare_psk_value():
    """The narrowing must not have gone so far that a real one gets through."""
    out, removed = secret_scrub.scrub({"psk": "hunter2", "wepKey": "abc"})
    assert out["psk"] == secret_scrub.REDACTED
    assert out["wepKey"] == secret_scrub.REDACTED


def test_scrub_keeps_configuration_that_merely_mentions_a_secret():
    """
    "Is a PSK in use" is a configuration fact an install review needs. Only the
    value itself is a credential.
    """
    out, removed = secret_scrub.scrub(
        {"pskEnabled": True, "passphraseCount": 42, "keyType": "WPA2"})
    assert removed == []
    assert out == {"pskEnabled": True, "passphraseCount": 42, "keyType": "WPA2"}


def test_scrub_leaves_empty_values_alone():
    """Nothing was leaked, so nothing is reported — the log stays meaningful."""
    out, removed = secret_scrub.scrub({"password": "", "secret": None})
    assert removed == []
    assert out == {"password": "", "secret": None}


def test_scrub_does_not_mutate_its_input():
    payload = {"switchSettings": {"switchLoginPassword": "hunter2"}}
    secret_scrub.scrub(payload)
    assert payload["switchSettings"]["switchLoginPassword"] == "hunter2"


def test_scrub_survives_a_cycle():
    """A cycle must not take down the one function that guarantees safety."""
    node = {"name": "loop"}
    node["self"] = node
    out, _ = secret_scrub.scrub(node)
    assert out["name"] == "loop"


def test_radius_shared_secret_shape_is_caught():
    """
    Written against the exact shape /radiusServerProfiles returns, because that
    endpoint is fetched for the Config tab and hands back working RADIUS shared
    secrets in plaintext. Nested inside primary/secondary, inside a list.
    """
    payload = [{"name": "radius2", "type": "AUTHENTICATION",
                "primary": {"ip": "10.0.0.1", "port": 1812,
                            "sharedSecret": "testing123"},
                "secondary": {"ip": "10.0.0.2", "sharedSecret": "testing123"}}]
    out, removed = secret_scrub.scrub(payload)
    assert out[0]["primary"]["sharedSecret"] == secret_scrub.REDACTED
    assert out[0]["secondary"]["sharedSecret"] == secret_scrub.REDACTED
    assert out[0]["primary"]["ip"] == "10.0.0.1", "the server address is config"
    assert len(removed) == 2


def test_redact_scrubs_before_it_does_anything_else():
    """
    The guarantee: no role, admin included, receives a credential in a report.
    """
    report = _sample_report()
    report["venue"] = {"switchSettings": {"switchLoginPassword": "2VsU^Kd4D%"}}
    for hidden in ([], ["poe.budget"]):
        out = redact.redact(report, hidden)
        assert out["venue"]["switchSettings"]["switchLoginPassword"] == \
            secret_scrub.REDACTED, f"credential survived redact(hidden={hidden})"


# ── config labels and baselines ──────────────────────────────────────

import config_labels                                        # noqa: E402
import baselines as baseline_module                          # noqa: E402


def test_unmapped_keys_still_read_like_english():
    """
    The load-bearing rule. R1 adds fields without warning, and a hardcoded
    label map would make a new one disappear or render as a raw key. It has to
    degrade to something readable so the field still APPEARS.
    """
    assert config_labels.label_for("someBrandNewFieldR1Added") == \
        "Some brand new field R1 added"


def test_known_acronyms_are_not_mangled():
    """
    "Ap snmp agent" looks like a bug, and a reader who cannot tell a bug from
    a label stops trusting the page.
    """
    assert config_labels.label_for("apSnmpAgentEnabled") == "AP SNMP agent enabled"
    assert config_labels.label_for("vlanMembers") == "VLAN members"


def test_mixed_case_units_survive():
    """Regression: the fallback lowercased "MHz" and produced "20 mhz"."""
    assert config_labels.label_for("20MHz") == "20 MHz"
    assert config_labels.label_for("160MHz") == "160 MHz"


def test_labels_with_underscores_are_found():
    """
    Regression: LABELS is looked up through a normaliser that strips
    underscores, so keys written with them never matched.
    """
    assert config_labels.label_for("snr_dB") == "Signal-to-noise ratio"


def test_values_read_as_settings_not_json():
    assert config_labels.format_value("enabled", True) == "Enabled"
    assert config_labels.format_value("enabled", False) == "Disabled"
    assert config_labels.format_value("method", "CHANNELFLY") == "ChannelFly"
    assert config_labels.format_value("gain24G", 3) == "3 dBi"
    assert config_labels.format_value("serverLossTimeout", 7200) == "2 hours"
    assert config_labels.format_value("allowedChannels", [1, 6, 11]) == "1, 6, 11"


def test_null_reads_as_not_set_not_as_missing_data():
    """
    On a settings page the reader is asking what something is configured to.
    An em dash reads as "no data" when the answer is "nothing is configured".
    """
    assert config_labels.format_value("anything", None) == "not set"
    assert config_labels.format_value("anything", "") == "not set"


def test_shipped_ruckus_baseline_is_marked_unverified():
    """
    THE SAFETY PROPERTY. The shipped values are invented placeholders so the
    mechanism can be exercised. A fabricated "RUCKUS recommends" read as
    authoritative by an install crew is worse than an empty column — an empty
    column asks a question, a wrong one answers it. This must not go green
    until somebody sources the real guidance.
    """
    ruckus = baseline_module.RUCKUS.describe()
    assert ruckus["verified"] is False, (
        "the shipped baseline claims to be verified — if the values are now "
        "real, that is fine, but check `source` is filled in too")
    assert ruckus["status"] == "placeholder"
    assert ruckus["count"] > 0, "the placeholder file should still exercise the UI"


def test_a_setting_no_baseline_mentions_gets_no_columns():
    """Two empty columns on every row would drown the ones that matter."""
    assert baseline_module.lookup("nothing.at.all") == {}


def test_missing_is_distinct_from_a_recommended_null():
    assert baseline_module.MISSING is not None


# ── AP subnet grouping ───────────────────────────────────────────────

from services.pisr.shape import _subnet_groups                # noqa: E402


def _dev(ip, netmask=None, gateway=None):
    return {"ip": ip, "netmask": netmask, "gateway": gateway}


def test_maskless_devices_land_in_a_reported_subnet_that_contains_them():
    """
    The bug this guards. A site on a single 10.2.0.0/21 rendered as the /21
    plus phantom /24s carved out of it — 10.2.6.0/24 and 10.2.7.0/24 with one
    AP each — because a device with no netmask and no gateway was bucketed into
    an assumed /24 without checking whether anything already described where it
    was. Three rows, one network, and a subnet count that read as 3.
    """
    devices = [_dev(f"10.2.{i // 254}.{i % 254 + 1}", "255.255.248.0")
               for i in range(418)]
    devices += [_dev("10.2.6.50"), _dev("10.2.7.99")]
    rows = _subnet_groups(devices)
    assert len(rows) == 1, f"expected one subnet, got {[r['cidr'] for r in rows]}"
    assert rows[0]["cidr"] == "10.2.0.0/21"
    assert rows[0]["count"] == 420, "the maskless APs belong to the count"


def test_a_genuinely_separate_subnet_still_gets_its_own_row():
    """The fix must not swallow addresses that are actually elsewhere."""
    rows = _subnet_groups([_dev("10.2.0.5", "255.255.248.0"), _dev("192.168.9.20")])
    assert {r["cidr"] for r in rows} == {"10.2.0.0/21", "192.168.9.0/24"}


def test_the_most_specific_containing_subnet_wins():
    """
    A device inside both a reported /21 and a reported /24 belongs in the /24 —
    that is the more precise statement about where it is.
    """
    rows = _subnet_groups([_dev("10.2.0.5", "255.255.248.0"),
                           _dev("10.2.6.10", "255.255.255.0"),
                           _dev("10.2.6.77")])
    by_cidr = {r["cidr"]: r["count"] for r in rows}
    assert by_cidr["10.2.6.0/24"] == 2, "the maskless AP joined the /24, not the /21"
    assert by_cidr["10.2.0.0/21"] == 1


def test_gateway_inferred_devices_fold_into_a_reported_subnet():
    """
    Containment is the test, not a matching gateway. Keying on the gateway
    meant a device whose gateway no reported row happened to list produced a
    duplicate subnet inside a real one.
    """
    rows = _subnet_groups([_dev("10.2.0.5", "255.255.248.0"),
                           _dev("10.2.4.9", None, "10.2.0.1")])
    assert len(rows) == 1 and rows[0]["cidr"] == "10.2.0.0/21"
    assert rows[0]["count"] == 2


# ── floor plans ──────────────────────────────────────────────────────

from services.pisr.shape import floorplan_summary              # noqa: E402
from services.pisr.checks import check_floorplans               # noqa: E402


def test_floorplan_scale_is_read_from_either_unit():
    """
    R1 fills the unit that was not used with 0.0, so both have to be checked
    and neither can be trusted to be present.
    """
    plans = floorplan_summary({"floorPlans": [
        {"name": "Metres", "scales": [{"distanceInMeters": 30.0,
                                       "distanceInFeet": 0.0}]},
        {"name": "Feet", "scales": [{"distanceInMeters": 0.0,
                                     "distanceInFeet": 98.4}]},
    ]})
    # Keyed by name, not by position: the summary sorts by floor then name so
    # the tab lists plans in a stable order, which is not input order.
    by_name = {p["name"]: p["scaleDistance"] for p in plans}
    assert by_name == {"Metres": "30 m", "Feet": "98.4 ft"}
    assert all(p["scaleSet"] for p in plans)


def test_a_plan_with_coordinates_but_no_distance_is_not_scaled():
    """
    The quiet failure. A scale entry can exist with the coordinate pairs filled
    in and no real-world distance, which looks set and measures nothing.
    """
    plans = floorplan_summary({"floorPlans": [
        {"name": "Unscaled", "scales": [{"x1": 1, "y1": 2, "x2": 3, "y2": 4,
                                         "distanceInMeters": 0.0,
                                         "distanceInFeet": 0.0}]}]})
    assert plans[0]["scaleSet"] is False
    assert plans[0]["scaleCount"] == 1, "the entry exists, it just says nothing"


def test_unscaled_plan_is_a_finding_not_a_pass():
    report = {"venue": {"floorPlans": floorplan_summary({"floorPlans": [
        {"name": "Unscaled", "imageId": "x.jpg", "scales": []}]})}}
    finding = check_floorplans(report)
    assert finding["severity"] == "warning"
    assert "no scale" in finding["title"]


def test_no_floorplan_at_all_is_a_finding():
    finding = check_floorplans({"venue": {"floorPlans": []}})
    assert finding["severity"] == "warning"


def test_a_scaled_plan_with_an_image_passes():
    report = {"venue": {"floorPlans": floorplan_summary({"floorPlans": [
        {"name": "Good", "imageId": "x.jpg",
         "scales": [{"distanceInMeters": 30.0}]}]})}}
    assert check_floorplans(report)["severity"] == "ok"


def test_existence_booleans_read_as_yes_no():
    """"Has image: Enabled" reads as though the image were a feature."""
    assert config_labels.format_value("hasImage", True) == "Yes"
    assert config_labels.format_value("scaleSet", False) == "No"
    assert config_labels.format_value("enabled", True) == "Enabled"


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

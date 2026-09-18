"""
The three-state org baseline, and its round-trip through the writable store.

WHY THIS EXISTS. The org baseline gained a third state — a field can carry a
recommended value, be explicitly "not applicable" (shown "—", never a
mismatch), or be unreviewed (no column). "Not applicable" and "unreviewed" look
identical to a reader, so it is easy to collapse them in code and lose the
distinction the editor exists to record. These lock the three states, the
value-wins-over-N.A. rule, and the atomic write.

RUNS WITHOUT PYTEST, like the other suites:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \\
      -v "$PWD:/repo" backend python /repo/api/tests/test_baselines.py

The org file is pointed at a temp path BEFORE anything reads it, so nothing here
can touch a real /data/org-baseline.json.
"""

import os
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

# config's controller half needs these to import.
for _n, _v in (("R1_TENANT_ID", "t"), ("R1_CLIENT_ID", "c"), ("R1_SHARED_SECRET", "s")):
    os.environ.setdefault(_n, _v)
os.environ.setdefault("PISR_AUTH_PASSPHRASE", "test-passphrase-123")

import baselines                              # noqa: E402
from services.pisr import shape               # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="pisr-baseline-test-"))


def _fresh():
    """An ORG store pointed at its own empty temp file."""
    path = _TMP / f"org-{os.urandom(4).hex()}.json"
    baselines.ORG.path = path
    baselines.ORG._loaded = False
    baselines.ORG._values = {lvl: {} for lvl in baselines.LEVELS}
    baselines.ORG._na = {lvl: set() for lvl in baselines.LEVELS}
    baselines.ORG._groups = []
    baselines.ORG._stamp = None
    return path


def _save_venue(values, na, status, source, show, actor):
    """Save just the venue level — the historical single-level shape, wrapped so
    the existing three-state assertions read as they did."""
    return baselines.save_org(
        {"venue": {"values": values, "notApplicable": na}},
        None, status, source, show, actor)


def _venue_full():
    """The venue level of ORG.full(), where the flat values/na used to live."""
    return baselines.ORG.full()["levels"]["venue"]


def test_three_states_round_trip():
    _fresh()
    _save_venue(
        {"ep.value": True, "ep.num": 1800},
        ["ep.na"], "verified", "internal doc", True, "tester")

    assert baselines.lookup("ep.value") == {"org": True}
    assert baselines.lookup("ep.num") == {"org": 1800}
    assert baselines.lookup("ep.na")["org"] is baselines.NOT_APPLICABLE
    assert baselines.lookup("ep.unreviewed") == {}, "an unlisted key gets no column"


def test_na_is_distinct_from_missing_and_from_a_value():
    _fresh()
    _save_venue({"ep.v": 5}, ["ep.na"], "unverified", "", True, "t")
    assert baselines.ORG.is_na("ep.na")
    assert not baselines.ORG.is_na("ep.v")
    assert not baselines.ORG.is_na("ep.absent")
    assert baselines.ORG.get("ep.na") is baselines.MISSING, "N.A. is not a value"
    assert baselines.ORG.get("ep.v") == 5


def test_value_wins_when_a_key_is_both_value_and_na():
    """A field either has a recommendation or explicitly has none — not both."""
    _fresh()
    _save_venue({"ep.x": 9}, ["ep.x", "ep.y"], "unverified", "", True, "t")
    full = _venue_full()
    assert "ep.x" in full["values"]
    assert "ep.x" not in full["notApplicable"], "the value must win"
    assert "ep.y" in full["notApplicable"]


def test_config_row_renders_each_state():
    """
    The shape the reader consumes: a valued field carries `matches`, an N.A.
    field carries `notApplicable` and NO `matches` (so it is never a diff), an
    unreviewed field has no org cell at all.
    """
    _fresh()
    # endpoint "ep", path "flag" -> baseline key "ep.flag"
    _save_venue({"ep.flag": True}, ["ep.other"], "unverified", "", True, "t")

    valued = shape._config_row("ep", ("flag",), "flag", False)
    assert valued["org"] == {"value": True, "text": "Enabled", "matches": False}

    na = shape._config_row("ep", ("other",), "other", "whatever")
    assert na["org"]["notApplicable"] is True
    assert "matches" not in na["org"], "an N.A. cell must never look like a mismatch"

    plain = shape._config_row("ep", ("unset",), "unset", 1)
    assert "org" not in plain, "an unreviewed field gets no org cell"


def test_verified_gets_a_timestamp_unverified_does_not():
    _fresh()
    _save_venue({}, [], "verified", "", True, "t")
    assert baselines.ORG.full()["verifiedAt"], "verified must be dated"
    _save_venue({}, [], "unverified", "", True, "t")
    assert baselines.ORG.full()["verifiedAt"] is None, "unverified has nothing to date"


def test_bad_status_falls_back_to_unverified():
    _fresh()
    _save_venue({}, [], "totally-made-up", "", True, "t")
    assert baselines.ORG.full()["status"] == "unverified"


def test_save_is_atomic_and_leaves_no_litter():
    path = _fresh()
    _save_venue({"ep.a": 1, "ep.b": 2}, ["ep.c"], "verified", "src", True, "t")
    assert path.exists()
    litter = list(path.parent.glob(".baseline-*.tmp"))
    assert not litter, f"left temp files: {litter}"


def test_no_path_configured_refuses_rather_than_crashes():
    baselines.ORG.path = None
    baselines.ORG._loaded = False
    try:
        _save_venue({"ep.a": 1}, [], "verified", "", True, "t")
    except RuntimeError as exc:
        assert "nowhere to save" in str(exc)
    else:
        raise AssertionError("saving with no path should refuse")


def test_ruckus_is_read_only_reference():
    """RUCKUS ships in the repo and this suite must not have written it."""
    assert baselines.RUCKUS.path and "baselines/ruckus.json" in str(baselines.RUCKUS.path)
    # No save method is exposed for it and it has no notApplicable concept.
    assert not baselines.RUCKUS.is_na("anything")



def test_field_catalogue_covers_endpoints_and_typed():
    """
    The static catalogue the editor browses. It must cover the venue-config
    endpoints, carry usable types (so the editor picks the right input), and be
    a superset of the RUCKUS keys — a recommendation can never key on a field
    the catalogue does not know about. Regenerate it with
    scripts/build_field_catalogue.py when the spec is reshipped.
    """
    cat = baselines.field_catalogue()
    endpoints = (cat.get("levels", {}).get("venue", {}).get("endpoints", {}))
    assert endpoints, "no catalogue built — run build_field_catalogue.py"

    def field(ep, path):
        return endpoints.get(ep, {}).get("fields", {}).get(path)

    assert field("rogueApSettings", "enabled")["type"] == "boolean"
    steering = field("apLoadBalancingSettings", "steeringMode")
    assert steering["type"] == "string" and "BASIC" in steering.get("enum", [])
    assert field("apRebootTimeoutSettings", "gatewayLossTimeout")["type"] == "integer"

    # Every RUCKUS key must exist in the catalogue, AT ITS OWN LEVEL — a
    # recommendation can never key on a field the catalogue for that level does
    # not know about.
    for level, values in baselines.ruckus_by_level().items():
        eps = cat.get("levels", {}).get(level, {}).get("endpoints", {})
        known = {f"{ep}.{path}" for ep, e in eps.items() for path in e["fields"]}
        for key in values:
            assert key in known, (
                f"RUCKUS recommends {key!r} at {level} but that level's "
                "catalogue lacks it")


def test_apgroup_catalogue_is_populated():
    """Phase 2: the AP-group level must carry its settable fields, or the editor
    has nothing to offer and a recommendation there could not be keyed."""
    cat = baselines.field_catalogue()
    eps = cat.get("levels", {}).get("apgroup", {}).get("endpoints", {})
    assert "radioSettings" in eps, "apgroup catalogue missing radioSettings"
    assert "apClientAdmissionControlSettings" in eps, "apgroup catalogue missing client admission"
    txpower = eps["radioSettings"]["fields"].get("radioParams24G.txPower")
    assert txpower and txpower.get("enum"), "expected an enum on an AP-group radio field"


def test_network_catalogue_is_populated():
    """Phase 3: the network level must carry the WifiNetwork settable fields,
    unioned across the polymorphic subtypes, or a network recommendation could
    not be keyed and the editor would have no Network tab."""
    cat = baselines.field_catalogue()
    eps = cat.get("levels", {}).get("network", {}).get("endpoints", {})
    assert "wifiNetworks" in eps, "network catalogue missing the wifiNetworks endpoint"
    fields = eps["wifiNetworks"]["fields"]
    # A field that only exists on one subtype (guest) and a wlan.* field common
    # to all — both must be present, proving the subtypes were unioned.
    assert "wlan.advancedCustomization.hideSsid" in fields, "core wlan field missing"
    assert any(p.startswith("guestPortal.") for p in fields), "guest subtype not unioned"
    # Portal branding is emitted but not curated; a wlan security field is.
    assert fields["wlan.advancedCustomization.hideSsid"]["curated"] is True
    assert fields["guestPortal.redirectUrl"]["curated"] is False


def test_config_detail_compares_networks_at_the_network_level():
    """A network's live config is flattened and compared against the NETWORK
    baseline — the reader half of phase 3."""
    _fresh()
    baselines.save_org({
        "network": {"values": {"wifiNetworks.wlan.advancedCustomization.hideSsid": False},
                    "notApplicable": []},
    }, None, "verified", "", True, "t")
    netcfg = {"n1": {"id": "n1", "name": "Guest", "ssid": "Guest-WiFi", "type": "Guest",
                     "wlan": {"advancedCustomization": {"hideSsid": True}}}}
    detail = shape.config_detail([], {}, {}, 0, netcfg, 1)
    assert detail["networkBaselines"]["org"]["active"], "network column not active"
    net = detail["networks"][0]
    assert net["name"] == "Guest" and net["categories"], "network not shaped"
    row = next(r for c in net["categories"] for r in c["rows"]
               if r["path"] == "wlan.advancedCustomization.hideSsid")
    assert row["org"] == {"value": False, "text": "Disabled", "matches": False}, (
        "the network recommendation did not compare against the live value")


def test_config_detail_networks_are_capped_reporting():
    """The truncation flags reconcile so the tab can say 'showing N of M'."""
    netcfg = {"n1": {"id": "n1", "name": "A"}, "n2": {"id": "n2", "name": "B"}}
    detail = shape.config_detail([], {}, {}, 0, netcfg, 5)
    assert detail["networkShown"] == 2 and detail["networkTotal"] == 5
    assert detail["networkTruncated"] is True


def test_config_detail_groups_are_capped_reporting():
    """The route caps AP groups; the full count still reaches the tab."""
    groups = [{"id": "g1", "name": "A"}, {"id": "g2", "name": "B"}]
    detail = shape.config_detail(groups, {}, {}, 0, group_total=7)
    assert detail["groupShown"] == 2 and detail["groupTotal"] == 7
    assert detail["groupTruncated"] is True
    whole = shape.config_detail(groups, {}, {}, 0)
    assert whole["groupTotal"] == 2 and whole["groupTruncated"] is False


def test_old_flat_file_reads_as_the_venue_level():
    """A baseline written before the level dimension existed is flat at the top
    level; it must migrate to the venue level on read so no mounted file breaks."""
    import json
    path = _fresh()
    path.write_text(json.dumps({
        "status": "verified", "source": "x",
        "values": {"apRadioSettings.foo": "1"},
        "notApplicable": ["apMeshSettings.bar"],
    }), encoding="utf-8")
    baselines.ORG._loaded = False   # force a reload of the flat file
    assert baselines.ORG.get("apRadioSettings.foo", "venue") == "1"
    assert baselines.ORG.is_na("apMeshSettings.bar", "venue")
    # And it is NOT visible at another level.
    assert baselines.ORG.get("apRadioSettings.foo", "apgroup") is baselines.MISSING


def test_same_key_is_independent_across_levels():
    """venue and apgroup share endpoint names, so the level is what disambiguates:
    a recommendation at one level must not read at the other."""
    _fresh()
    baselines.save_org({
        "venue": {"values": {"apClientAdmissionControlSettings.enable24G": True}, "notApplicable": []},
        "apgroup": {"values": {"apClientAdmissionControlSettings.enable24G": False}, "notApplicable": []},
    }, None, "unverified", "", True, "t")
    assert baselines.lookup("apClientAdmissionControlSettings.enable24G", "venue")["org"] is True
    assert baselines.lookup("apClientAdmissionControlSettings.enable24G", "apgroup")["org"] is False


def test_saving_one_level_preserves_the_others():
    """The editor shows one level but sends them all; the store replaces the
    whole document. A save that carried only one level would wipe the rest —
    these assert the round-trip keeps every level the body carried."""
    _fresh()
    baselines.save_org({
        "venue": {"values": {"rogueApSettings.enabled": True}, "notApplicable": []},
        "apgroup": {"values": {"radioSettings.radioParams5G.txPower": "FULL"}, "notApplicable": []},
    }, None, "verified", "src", True, "t")
    full = baselines.ORG.full()
    assert full["levels"]["venue"]["values"]["rogueApSettings.enabled"] is True
    assert full["levels"]["apgroup"]["values"]["radioSettings.radioParams5G.txPower"] == "FULL"


def test_config_row_reads_the_apgroup_level_when_asked():
    """`_config_row` defaults to venue; passing level='apgroup' must consult the
    AP-group recommendations, which is what the Config detail view relies on."""
    _fresh()
    baselines.save_org({
        "apgroup": {"values": {"radioSettings.foo": 42}, "notApplicable": []},
    }, None, "unverified", "", True, "t")
    venue = shape._config_row("radioSettings", ("foo",), "foo", 1)
    assert "org" not in venue, "an apgroup rec must not surface at venue level"
    ap = shape._config_row("radioSettings", ("foo",), "foo", 1, level="apgroup")
    assert ap["org"] == {"value": 42, "text": "42", "matches": False}


# ── network groups: per-group recommendations over a global default ──

def _save_network(default_values, groups):
    baselines.save_org(
        {"network": {"values": default_values, "notApplicable": []}},
        groups, "unverified", "", True, "t")


def test_network_group_matches_by_name_glob_type_and_ids():
    _fresh()
    _save_network({}, [
        {"id": "units", "name": "Per-unit", "match": {"by": "name", "pattern": "Unit-*"},
         "values": {}, "notApplicable": []},
        {"id": "guest", "name": "Guest", "match": {"by": "type", "types": ["Guest"]},
         "values": {}, "notApplicable": []},
        {"id": "pin", "name": "Pinned", "match": {"by": "ids", "ids": ["net-x"]},
         "values": {}, "notApplicable": []},
    ])
    assert baselines.network_group_for({"id": "n", "name": "Unit-101", "type": "Psk"}) == "units"
    assert baselines.network_group_for({"id": "n", "name": "Corp", "type": "Guest"}) == "guest"
    assert baselines.network_group_for({"id": "net-x", "name": "Whatever", "type": "Open"}) == "pin"
    assert baselines.network_group_for({"id": "n", "name": "Corp", "type": "Psk"}) is None


def test_first_matching_group_wins():
    """Order is precedence — a specific rule above a catch-all."""
    _fresh()
    _save_network({}, [
        {"id": "special", "name": "Unit 1", "match": {"by": "ids", "ids": ["u1"]},
         "values": {}, "notApplicable": []},
        {"id": "all-units", "name": "Units", "match": {"by": "name", "pattern": "Unit-*"},
         "values": {}, "notApplicable": []},
    ])
    assert baselines.network_group_for({"id": "u1", "name": "Unit-1"}) == "special"
    assert baselines.network_group_for({"id": "u2", "name": "Unit-2"}) == "all-units"


def test_group_overrides_default_per_key_and_inherits_the_rest():
    _fresh()
    _save_network(
        {"wifiNetworks.a": 1, "wifiNetworks.b": 2},
        [{"id": "g", "name": "G", "match": {"by": "type", "types": ["Guest"]},
          "values": {"wifiNetworks.a": 99},
          "notApplicable": ["wifiNetworks.b"]}])
    # a: group value wins; b: group N.A. shadows the default value; c: default.
    assert baselines.lookup("wifiNetworks.a", "network", "g") == {"org": 99}
    assert baselines.lookup("wifiNetworks.b", "network", "g")["org"] is baselines.NOT_APPLICABLE
    _save_network({"wifiNetworks.c": 7}, [
        {"id": "g", "name": "G", "match": {"by": "type", "types": ["Guest"]},
         "values": {}, "notApplicable": []}])
    assert baselines.lookup("wifiNetworks.c", "network", "g") == {"org": 7}, "group must inherit a default-only key"


def test_group_does_not_leak_into_the_default_or_another_group():
    _fresh()
    _save_network({}, [
        {"id": "g1", "name": "G1", "match": {"by": "ids", "ids": ["a"]},
         "values": {"wifiNetworks.x": 1}, "notApplicable": []},
        {"id": "g2", "name": "G2", "match": {"by": "ids", "ids": ["b"]},
         "values": {}, "notApplicable": []}])
    assert baselines.lookup("wifiNetworks.x", "network") == {}, "group rec leaked to the default"
    assert baselines.lookup("wifiNetworks.x", "network", "g2") == {}, "group rec leaked across groups"


def test_network_column_is_active_when_only_a_group_has_recs():
    """A network in a group must get its columns even when the default is empty."""
    _fresh()
    _save_network({}, [
        {"id": "g", "name": "G", "match": {"by": "type", "types": ["Guest"]},
         "values": {"wifiNetworks.x": 1}, "notApplicable": []}])
    assert baselines.describe("network")["org"]["active"] is True


def test_network_groups_round_trip_and_id_rules():
    _fresh()
    baselines.save_org({}, [
        {"id": "g", "name": "Keep", "match": {"by": "name", "pattern": "K-*"},
         "values": {"wifiNetworks.x": 1}, "notApplicable": []},
        {"id": "g", "name": "Dup dropped", "match": {}, "values": {}, "notApplicable": []},
        {"name": "No id dropped", "match": {}, "values": {}, "notApplicable": []},
    ], "unverified", "", True, "t")
    groups = baselines.ORG.full()["networkGroups"]
    assert [g["id"] for g in groups] == ["g"], "dup id or id-less group survived"
    assert groups[0]["name"] == "Keep" and groups[0]["values"] == {"wifiNetworks.x": 1}


def test_config_detail_clusters_identical_networks_and_keeps_distinct_ones():
    """The MDU case: per-unit SSIDs that differ only by name/SSID/VLAN collapse
    into one cluster in their group; a distinct SSID stands alone in the default;
    the cluster is compared against the group's recs and the standalone against
    the default."""
    _fresh()
    _save_network(
        {"wifiNetworks.wlan.hideSsid": False},
        [{"id": "units", "name": "Per-unit", "match": {"by": "name", "pattern": "Unit-*"},
          "values": {"wifiNetworks.wlan.hideSsid": True}, "notApplicable": []}])

    def unit(n, vlan):
        return {"id": f"u{n}", "name": f"Unit-{n}", "ssid": f"Unit-{n}", "type": "Psk",
                "wlan": {"hideSsid": True}, "vlan": vlan}
    netcfg = {"u1": unit(1, 101), "u2": unit(2, 102), "u3": unit(3, 103),
              "g": {"id": "g", "name": "Guest", "ssid": "Guest-WiFi", "type": "Guest",
                    "wlan": {"hideSsid": False}}}
    detail = shape.config_detail([], {}, {}, 0, netcfg, 4)

    assert detail["networkShown"] == 4, "the count must be of SSIDs, not clusters"
    assert detail["networkClusters"] == 2, "per-unit SSIDs did not collapse"

    by_group = {r["group"]: r for r in detail["networks"]}
    units = by_group["units"]
    assert units["count"] == 3 and {m["name"] for m in units["members"]} == {"Unit-1", "Unit-2", "Unit-3"}
    assert "VLAN" in units["varies"] and "SSID" in units["varies"]
    standalone = by_group[None]
    assert standalone["name"] == "Guest" and standalone["count"] == 1

    def hide(row):
        return next(r["org"] for c in row["categories"] for r in c["rows"]
                    if r["path"] == "wlan.hideSsid")
    # The per-unit cluster is compared against the GROUP rec (hideSsid True);
    # the standalone Guest against the network DEFAULT (hideSsid False).
    assert hide(units) == {"value": True, "text": "Enabled", "matches": True}
    assert hide(standalone) == {"value": False, "text": "Disabled", "matches": True}


def test_networks_differing_in_real_config_do_not_cluster():
    """Two SSIDs that differ in a policy field (not a per-instance one) are
    genuinely distinct and must stay separate rows."""
    _fresh()
    netcfg = {"a": {"id": "a", "name": "A", "ssid": "A", "wlan": {"clientIsolation": True}},
              "b": {"id": "b", "name": "B", "ssid": "B", "wlan": {"clientIsolation": False}}}
    detail = shape.config_detail([], {}, {}, 0, netcfg, 2)
    assert detail["networkClusters"] == 2, "distinct configs must not collapse"


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
        except Exception as exc:
            failures += 1
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

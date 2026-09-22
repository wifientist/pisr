"""
The identity trace's verdicts, on a hand-built venue.

`services/pisr/trace.py` is pure, so every fault it names can be built here
without R1: a clean per-unit resident, a username regex with a typo, an SSID
regex naming a network that is not activated, a tiered set where most users
fall through to the pool default, and a reject-by-default pool.

Runs without pytest, like test_sections.py:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" backend python /repo/api/tests/test_trace.py
"""

import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

from services.pisr import trace  # noqa: E402

SET = "set-1"
POOL = "pool-1"


def _cond(match, regex):
    return {"match": match, "regex": regex, "attribute": match}


def _venue(users, policies, *, default_access=True, activated=("net-101", "net-102"),
           pool_networks=("net-101", "net-102", "net-103")):
    """policies: [(id, name, [conditions])] in priority order."""
    return dict(
        pool_rows=[{"id": POOL, "name": "Residents", "policySetId": SET,
                    "policyDefaultAccess": default_access, "identityGroups": []}],
        raw_pools=[{"id": POOL, "networkIds": list(pool_networks)}],
        usernames={POOL: {"rows": [{"username": u} for u in users], "total": len(users)}},
        sets=[{"id": SET, "name": "Per-unit"}],
        scoped_set_ids={SET},
        set_members={SET: [{"policyId": pid, "priority": i + 1}
                           for i, (pid, _, _) in enumerate(policies)]},
        policies=[{"id": pid, "name": name, "conditionsCount": len(conds)}
                  for pid, name, conds in policies],
        conditions={pid: conds for pid, _, conds in policies},
        conditions_wanted=len(policies),
        networks=[{"id": "net-101", "ssid": "unit101"}, {"id": "net-102", "ssid": "unit102"},
                  {"id": "net-103", "ssid": "unit103"}],
        activations=[{"networkId": nid, "apGroups": [{"apGroupId": f"g-{nid}"}]}
                     for nid in activated],
        aps=[{"serial": "S101", "apGroupId": "g-net-101", "state": "online"},
             {"serial": "S102", "apGroupId": "g-net-102", "state": "online"}],
        ap_groups=[{"id": "g-net-101", "name": "Unit 101"},
                   {"id": "g-net-102", "name": "Unit 102"},
                   {"id": "g-net-103", "name": "Unit 103"}],
    )


def _unit(n, user_rx=None, ssid_rx=None):
    return (f"p{n}", f"unit{n}", [_cond("Dpsk_Username", user_rx or f"^unit{n}$"),
                                  _cond("SSID", ssid_rx or f"^unit{n}$")])


def _vlan(row):
    return (row["vlan"]["vlans"], row["vlan"]["source"])


def _row(out, username):
    return next(r for r in out["rows"] if r["username"] == username)


def test_clean_per_unit_resident():
    out = trace.identity_trace(**_venue(["unit101"], [_unit(101)]))
    row = _row(out, "unit101")
    assert row["status"] == "ok", row
    assert row["policyId"] == "p101"
    assert row["networkIds"] == ["net-101"]
    assert out["apGroups"]["g-net-101"]["apSerials"] == ["S101"]


def test_username_regex_typo_is_named():
    out = trace.identity_trace(**_venue(["unit102"], [_unit(102, user_rx="^unit120$")]))
    row = _row(out, "unit102")
    assert "regex-typo" in row["issues"], row
    assert row["status"] == "error"
    assert out["unclaimedPolicyIds"] == ["p102"]


def test_ssid_not_activated_here():
    out = trace.identity_trace(**_venue(["unit103"], [_unit(103)]))
    assert "ssid-not-here" in _row(out, "unit103")["issues"]


def test_ssid_regex_matching_nothing():
    out = trace.identity_trace(**_venue(["unit101"], [_unit(101, ssid_rx="^unit1O1$")]))
    assert "ssid-no-network" in _row(out, "unit101")["issues"]


def test_activated_on_empty_group():
    venue = _venue(["unit101"], [_unit(101)])
    venue["aps"] = []
    assert "no-aps" in _row(trace.identity_trace(**venue), "unit101")["issues"]


def test_tiered_set_falls_through_as_warning_not_error():
    tier = ("pt", "premium", [_cond("Dpsk_Username", "_premium$")])
    out = trace.identity_trace(**_venue(["a_premium", "b"], [tier]))
    premium = _row(out, "a_premium")
    assert premium["status"] == "ok", premium  # no SSID condition is a NOTE
    assert "no-ssid-condition" in premium["issues"]
    assert _row(out, "b")["issues"] == ["default-access"]
    assert _row(out, "b")["status"] == "warning"


def test_reject_by_default_pool_is_an_error():
    venue = _venue(["b"], [_unit(101)], default_access=False)
    venue["activations"][0]["allApGroupsVlanId"] = 50
    row = _row(trace.identity_trace(**venue), "b")
    assert row["issues"] == ["rejected"]
    # Turned away, so no network default applies.
    assert _vlan(row) == ([], None)


def test_higher_priority_match_shadows():
    broad = ("pb", "catch-units", [_cond("Dpsk_Username", "^unit"), _cond("SSID", "^unit101$")])
    out = trace.identity_trace(**_venue(["unit101"], [broad, _unit(101)]))
    row = _row(out, "unit101")
    assert row["policyId"] == "pb"
    assert row["shadows"] == ["p101"]
    assert "shadowed" in row["issues"]


def test_unreadable_conditions_are_unknown_not_missing():
    venue = _venue(["unit101"], [_unit(101)])
    venue["conditions"] = {"p101": None}
    row = _row(trace.identity_trace(**venue), "unit101")
    assert row["issues"] == ["conditions-unread"], row
    assert row["status"] == "unknown"


def test_invalid_regex_is_unverifiable():
    out = trace.identity_trace(**_venue(["unit101"], [_unit(101, user_rx="^unit(101$")]))
    assert _row(out, "unit101")["status"] == "unknown"


def test_identity_description_group_and_radius_group():
    venue = _venue(["unit101", "unit102"], [_unit(101), _unit(102)])
    venue["pool_rows"][0]["identityGroups"] = [{"id": "ig-1", "name": "Residents IG"}]
    # One joined by identityId, one only by the identity's dpskGuid.
    venue["usernames"][POOL]["rows"] = [
        {"id": "pp-1", "username": "unit101", "identityGroupId": "ig-1", "identityId": "id-1"},
        {"id": "pp-2", "username": "unit102", "identityGroupId": "ig-1"},
    ]
    venue["identities"] = {"ig-1": {"rows": [
        {"id": "id-1", "name": "unit101", "description": "  Smith, 101  "},
        {"id": "id-2", "name": "unit102", "description": "", "dpskGuid": "pp-2"},
    ]}}
    venue["policies"][0]["onMatchResponse"] = "rg-1"
    venue["radius_groups"] = [{"id": "rg-1", "name": "100 Mbps", "attributeAssignments": [
        {"attributeName": "WISPr-Bandwidth-Max-Down", "dataType": "INTEGER",
         "attributeValue": "100000000"}]}]
    out = trace.identity_trace(**venue)
    a, b = _row(out, "unit101"), _row(out, "unit102")
    assert a["identityGroup"] == "Residents IG"
    assert a["description"] == "Smith, 101"
    assert b["identityName"] == "unit102" and b["description"] is None
    policy = out["policies"]["p101"]
    assert policy["radiusGroup"] == "100 Mbps"
    assert policy["rateLimits"][0]["mbps"] == 100.0
    assert out["policies"]["p102"]["radiusGroup"] is None


def test_vlan_precedence_passphrase_then_identity_then_network():
    venue = _venue(["unit101", "unit102", "unit103x"],
                   [_unit(101), _unit(102),
                    ("p3", "unit103x", [_cond("Dpsk_Username", "^unit103x$"),
                                        _cond("SSID", "^unit101$")])])
    venue["networks"][0]["vlan"] = 1          # net-101's own VLAN...
    venue["activations"][0]["apGroups"][0]["vlanId"] = 300  # ...overridden at the venue
    venue["usernames"][POOL]["rows"] = [
        {"id": "a", "username": "unit101", "identityId": "i1", "vlanId": 101,
         "deviceCount": 3, "devicesByNetwork": {"net-101": 3}},
        {"id": "b", "username": "unit102", "identityId": "i2",
         "deviceCount": 2, "devicesByNetwork": {"net-102": 2}},
        {"id": "c", "username": "unit103x", "identityId": "i3",
         "deviceCount": 4, "devicesByNetwork": {"net-101": 4}},
        # A second passphrase on identity i1.
        {"id": "d", "username": "unit101", "identityId": "i1"},
    ]
    venue["identities"] = {"ig": {"rows": [
        {"id": "i1", "vlan": 900}, {"id": "i2", "vlan": 202}, {"id": "i3"}]}}
    out = trace.identity_trace(**venue)
    rows = {r["username"] + r.get("identityId", ""): r for r in out["rows"]}
    a = next(r for r in out["rows"] if r["username"] == "unit101" and r["passphraseVlan"] == 101)
    assert _vlan(a) == ([101], "passphrase")                             # beats identity 900
    assert a["passphraseCount"] == 2
    assert a["devicesByVlan"] == [{"vlans": [101], "source": "passphrase", "count": 3}]
    b = _row(out, "unit102")
    assert _vlan(b) == ([202], "identity")
    c = _row(out, "unit103x")
    assert _vlan(c) == ([300], "network")                                # activation override
    assert c["vlan"]["basis"] == "policy"
    assert c["devicesByVlan"] == [{"vlans": [300], "source": "network", "count": 4}]
    assert rows  # keyed view built without error


def _per_unit_vlans(venue):
    """Give net-101 VLAN 101 and net-102 VLAN 102 at the venue."""
    for activation in venue["activations"]:
        activation["allApGroupsVlanId"] = int(activation["networkId"].split("-")[1])
    return venue


def test_unmatched_user_on_a_single_network_pool_gets_its_default():
    venue = _venue(["stranger"], [_unit(101)], activated=("net-101",),
                   pool_networks=("net-101",))
    venue["activations"][0]["allApGroupsVlanId"] = 50
    row = _row(trace.identity_trace(**venue), "stranger")
    assert row["issues"] == ["default-access"]
    assert _vlan(row) == ([50], "network")
    assert row["vlan"]["basis"] == "only-network"


def test_unmatched_user_never_gets_every_pool_network():
    """The regression: hundreds of per-unit VLANs on one row."""
    row = _row(trace.identity_trace(**_per_unit_vlans(_venue(["stranger"], [_unit(101)]))),
               "stranger")
    assert _vlan(row) == ([], None), row["vlan"]


def test_unmatched_user_network_comes_from_their_devices():
    venue = _per_unit_vlans(_venue(["stranger"], [_unit(101)]))
    venue["usernames"][POOL]["rows"] = [
        {"username": "stranger", "deviceCount": 2, "devicesByNetwork": {"net-102": 2}}]
    row = _row(trace.identity_trace(**venue), "stranger")
    assert _vlan(row) == ([102], "network")
    assert row["vlan"]["basis"] == "devices"


def test_unmatched_user_network_comes_from_the_policy_named_for_them():
    # Username regex typo: the policy "unit102" does not select unit102, but
    # its SSID condition still says which network unit102 was meant to have.
    venue = _per_unit_vlans(_venue(["unit102"], [_unit(102, user_rx="^unit120$")]))
    row = _row(trace.identity_trace(**venue), "unit102")
    assert "regex-typo" in row["issues"]
    assert _vlan(row) == ([102], "network")
    assert row["vlan"]["basis"] == "named-policy"


def test_every_issue_has_a_known_severity():
    for code, (severity, label) in trace.ISSUES.items():
        assert severity in trace.SEVERITY, code
        assert label, code


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

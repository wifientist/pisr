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
    baselines.ORG._values = {}
    baselines.ORG._na = set()
    baselines.ORG._stamp = None
    return path


def test_three_states_round_trip():
    _fresh()
    baselines.save_org(
        {"ep.value": True, "ep.num": 1800},
        ["ep.na"], "verified", "internal doc", True, "tester")

    assert baselines.lookup("ep.value") == {"org": True}
    assert baselines.lookup("ep.num") == {"org": 1800}
    assert baselines.lookup("ep.na")["org"] is baselines.NOT_APPLICABLE
    assert baselines.lookup("ep.unreviewed") == {}, "an unlisted key gets no column"


def test_na_is_distinct_from_missing_and_from_a_value():
    _fresh()
    baselines.save_org({"ep.v": 5}, ["ep.na"], "unverified", "", True, "t")
    assert baselines.ORG.is_na("ep.na")
    assert not baselines.ORG.is_na("ep.v")
    assert not baselines.ORG.is_na("ep.absent")
    assert baselines.ORG.get("ep.na") is baselines.MISSING, "N.A. is not a value"
    assert baselines.ORG.get("ep.v") == 5


def test_value_wins_when_a_key_is_both_value_and_na():
    """A field either has a recommendation or explicitly has none — not both."""
    _fresh()
    baselines.save_org({"ep.x": 9}, ["ep.x", "ep.y"], "unverified", "", True, "t")
    full = baselines.ORG.full()
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
    baselines.save_org({"ep.flag": True}, ["ep.other"], "unverified", "", True, "t")

    valued = shape._config_row("ep", ("flag",), "flag", False)
    assert valued["org"] == {"value": True, "text": "Enabled", "matches": False}

    na = shape._config_row("ep", ("other",), "other", "whatever")
    assert na["org"]["notApplicable"] is True
    assert "matches" not in na["org"], "an N.A. cell must never look like a mismatch"

    plain = shape._config_row("ep", ("unset",), "unset", 1)
    assert "org" not in plain, "an unreviewed field gets no org cell"


def test_verified_gets_a_timestamp_unverified_does_not():
    _fresh()
    baselines.save_org({}, [], "verified", "", True, "t")
    assert baselines.ORG.full()["verifiedAt"], "verified must be dated"
    baselines.save_org({}, [], "unverified", "", True, "t")
    assert baselines.ORG.full()["verifiedAt"] is None, "unverified has nothing to date"


def test_bad_status_falls_back_to_unverified():
    _fresh()
    baselines.save_org({}, [], "totally-made-up", "", True, "t")
    assert baselines.ORG.full()["status"] == "unverified"


def test_save_is_atomic_and_leaves_no_litter():
    path = _fresh()
    baselines.save_org({"ep.a": 1, "ep.b": 2}, ["ep.c"], "verified", "src", True, "t")
    assert path.exists()
    litter = list(path.parent.glob(".baseline-*.tmp"))
    assert not litter, f"left temp files: {litter}"


def test_no_path_configured_refuses_rather_than_crashes():
    baselines.ORG.path = None
    baselines.ORG._loaded = False
    try:
        baselines.save_org({"ep.a": 1}, [], "verified", "", True, "t")
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

    # Every RUCKUS key must exist in the catalogue.
    known = {f"{ep}.{path}"
             for ep, e in endpoints.items() for path in e["fields"]}
    for key in baselines.ruckus_values():
        assert key in known, f"RUCKUS recommends {key!r} but the catalogue lacks it"

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

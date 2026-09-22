"""
Batch runs: the record, the allowlist of what it keeps, and the roll-up.

The test that matters most is `test_summary_holds_only_counts`. The batch
record is the one file PISR writes that holds anything read off a customer's
network, and the argument for allowing it at all is that it holds integers —
no finding text, no device names, no R1 error bodies. If `rollup.summarise`
ever starts passing a string through, that argument is gone, and this is what
says so.

Runs without pytest, like the others:

    docker compose -f docker-compose.dev.yml exec backend python tests/test_batch.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import batch_runs                      # noqa: E402
from services.pisr import punchlist, rollup   # noqa: E402


def _report(findings, errors=None, venue_name="Maple Court"):
    """A report as build_report shapes it, reduced to what summarise reads."""
    verification = {"findings": findings}
    return {
        "venue": {"name": venue_name},
        "verification": verification,
        "punchlist": punchlist.build(verification),
        # One AP offline and one never-contacted: "other" is what stops
        # online + offline accounting for the fleet.
        "inventory": {"aps": {"total": 12, "online": 10, "offline": 1, "other": 1},
                      "switches": {"total": 2, "online": 2, "offline": 0, "other": 0}},
        "meta": {"errors": errors or {}, "elapsedSeconds": 4.2,
                 "counts": {"aps": 12, "switches": 2, "clients": 40, "incidents": 1}},
    }


FINDINGS = [
    {"id": "aps-online", "severity": "critical", "title": "3 APs offline in Unit 4B",
     "evidence": [{"ap": "UNIT-4B-AP1"}, {"ap": "UNIT-4B-AP2"}]},
    {"id": "port-errors", "severity": "warning", "title": "CRC errors on 1/1/7",
     "evidence": [{"switch": "IDF-2"}]},
    {"id": "ap-naming", "severity": "info", "title": "Naming", "evidence": []},
    {"id": "dhcp-pools", "severity": "skipped", "summary": "No DHCP pools"},
    {"id": "mgmt-vlan", "severity": "ok"},
]


def _store():
    tmp = Path(tempfile.mkdtemp()) / "batch-runs.json"
    return batch_runs.BatchStore(str(tmp)), tmp


# ── the allowlist ───────────────────────────────────────────────────


def test_summary_holds_only_counts():
    summary = rollup.summarise(_report(FINDINGS, errors={"ports": "HTTPSConnectionPool(...)"}))
    text = json.dumps(summary)
    # Fragments of the FINDINGS' own text. Note "offline" alone is not one:
    # `devices.aps.offline` is a legitimate key, and a scan loose enough to
    # catch it would have to be weakened later, which is how these rot.
    for leak in ("UNIT-4B", "IDF-2", "CRC errors", "Unit 4B",
                 "HTTPSConnectionPool", "Maple"):
        assert leak not in text, f"summary carries report text: {leak!r}"

    def walk(value, path="summary"):
        if isinstance(value, dict):
            for k, v in value.items():
                # Every KEY is either a field name this module writes or a
                # check id from the shipped catalogue. A key is a string on
                # disk like any other, so it is held to the same rule.
                if path.startswith("summary.checks."):
                    assert k in rollup.KNOWN_CHECKS, f"unknown check id on disk: {k!r}"
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            # The one list: names of R1 reads that failed, never their messages.
            assert path == "summary.readErrors", f"unexpected list at {path}"
            assert all(isinstance(v, str) for v in value)
        else:
            assert value is None or isinstance(value, (int, float)) \
                and not isinstance(value, bool), f"{path} is {type(value).__name__}"
    walk(summary)
    assert summary["readErrors"] == ["ports"]


def test_checks_are_ids_and_device_counts():
    summary = rollup.summarise(_report(FINDINGS))
    assert summary["checks"] == {"critical": {"aps-online": 2},
                                 "warning": {"port-errors": 1},
                                 "info": {"ap-naming": 0}}
    assert rollup.has_checks(summary)


def test_an_unknown_check_id_is_not_written():
    # A check renamed in checks.py but not in sections.py: the count still
    # includes it, the id does not reach the file.
    summary = rollup.summarise(_report(
        FINDINGS + [{"id": "brand-new-check", "severity": "critical", "evidence": []}]))
    assert summary["counts"]["critical"] == 2
    assert "brand-new-check" not in summary["checks"]["critical"]


def test_device_counts_call_everything_not_online_down():
    summary = rollup.summarise(_report(FINDINGS))
    assert summary["devices"]["aps"] == {"total": 12, "online": 10,
                                         "offline": 1, "other": 1}
    # Down is 2, not 1: the never-contacted AP is not up, and the roll-up must
    # not be more optimistic than check_aps_online.
    assert rollup.device_counts(summary, "aps") == {"total": 12, "up": 10, "down": 2}
    assert rollup.device_counts(summary, "switches") == {"total": 2, "up": 2, "down": 0}


def test_device_counts_fall_back_for_an_older_record():
    old_summary = {"aps": 9, "counts": {"critical": 0, "warning": 0, "info": 0}}
    assert rollup.device_counts(old_summary, "aps") == {"total": 9, "up": None, "down": None}
    assert rollup.device_counts(old_summary, "switches") is None
    assert rollup.device_counts(None, "aps") is None


def test_check_detail_is_filtered_and_ordered():
    summary = rollup.summarise(_report(FINDINGS))
    rows = rollup.check_detail(summary, ["warning", "critical"])
    assert [r["id"] for r in rows] == ["aps-online", "port-errors"], "critical first"
    # The label is the catalogue's, and for this id it is deliberately NOT the
    # id de-kebabed: "APs Online" beside a red count reads as good news.
    assert rows[0]["label"] == "APs offline" and rows[0]["count"] == 2
    assert rows[0]["category"] == "Devices not up"
    assert rollup.check_detail(summary, []) == []
    assert [r["id"] for r in rollup.check_detail(summary, ["info"])] == ["ap-naming"]


def test_every_checked_venue_is_named_clean_ones_included():
    store, _ = _store()
    run = store.start_run("t1", "T", _venues("bad", "good", "never"),
                          ["bad", "good"], "admin")
    store.record_venue(run["id"], "bad", "Bad Venue", "ok",
                       rollup.summarise(_report(FINDINGS)), None)
    clean = [{"id": "mgmt-vlan", "severity": "ok"},
             {"id": "dhcp-pools", "severity": "skipped", "summary": "none"}]
    store.record_venue(run["id"], "good", "Good Venue", "ok",
                       rollup.summarise(_report(clean)), None)
    tenant = rollup.attach_detail(
        rollup.tenant_rollup(store.snapshot(), "t1", 7), ["critical", "warning"])

    # The name is the tenant DIRECTORY's, not the one recorded with the run:
    # R1 is the authority on what a venue is called today.
    assert [v["name"] for v in tenant["detailVenues"]] == ["Venue bad"]
    assert [v["name"] for v in tenant["cleanVenues"]] == ["Venue good"]
    assert tenant["unrecordedVenues"] == []
    # The progress number: one of the two checked venues is clean. The venue
    # that was never checked is not clean — it is unknown.
    assert tenant["clean"] == 1 and tenant["checked"] == 2 and tenant["venueCount"] == 3

    from routers.pisr_router import _jinja
    html = _jinja().get_template("reports/batch.html").render(
        title="T", overall=None, tenants=[tenant], days_label="7",
        generated_at="now", has_links=False, detail_severities=["critical", "warning"],
        categories=[{"key": k, "label": rollup.CATEGORY_LABELS[k]}
                    for k in rollup.CATEGORY_KEYS])
    # The heading, not the tile of the same name at the top of the page.
    assert "Clean sites —" in html and "Venue good" in html
    # The clean list sits after the problems, not instead of them.
    assert html.index("Clean sites —") > html.index("Needs attention")


def test_an_info_only_venue_is_clean_but_listed_when_info_is_asked_for():
    store, _ = _store()
    run = store.start_run("t1", "T", _venues("a"), ["a"], "admin")
    store.record_venue(run["id"], "a", "A", "ok", rollup.summarise(_report(
        [{"id": "ap-naming", "severity": "info", "evidence": []}])), None)
    snap = store.snapshot()
    # `clean` is fixed at critical+warning so the tool says one thing
    # everywhere, including the dialog, which has no severity selection.
    assert rollup.tenant_rollup(snap, "t1", 7)["clean"] == 1
    with_info = rollup.attach_detail(rollup.tenant_rollup(snap, "t1", 7), ["info"])
    assert [v["name"] for v in with_info["detailVenues"]] == ["Venue a"]
    assert with_info["cleanVenues"] == []


def test_a_record_written_before_check_ids_still_reads():
    old_summary = {"total": 3, "counts": {"critical": 1, "warning": 1, "info": 1}}
    assert rollup.has_checks(old_summary) is False
    assert rollup.check_detail(old_summary, ["critical"]) == []
    assert rollup.check_detail(None, ["critical"]) == []


def test_severity_parameter_ignores_nonsense():
    assert rollup.parse_severities("warning,critical") == ["critical", "warning"]
    assert rollup.parse_severities("CRITICAL, info") == ["critical", "info"]
    assert rollup.parse_severities("") == []
    assert rollup.parse_severities(None) == []
    assert rollup.parse_severities("wat, <script>") == []


def test_summary_counts_match_the_punch_list():
    summary = rollup.summarise(_report(FINDINGS))
    assert summary["counts"] == {"critical": 1, "warning": 1, "info": 1}
    assert summary["total"] == 3
    assert summary["skipped"] == 1
    assert summary["passed"] == 1
    assert summary["byCategory"]["devices"]["critical"] == 1
    assert summary["byCategory"]["cabling"]["warning"] == 1
    assert rollup.classify(summary) == "ok"
    assert rollup.classify(rollup.summarise(_report(FINDINGS, {"aps": "x"}))) == "partial"


# ── the store ───────────────────────────────────────────────────────


def _venues(*ids):
    return [{"id": v, "name": f"Venue {v}"} for v in ids]


def test_only_ok_moves_last_complete():
    store, _ = _store()
    run = store.start_run("t1", "Tenant One", _venues("a", "b"), ["a"], "admin")
    ok = rollup.summarise(_report(FINDINGS))
    store.record_venue(run["id"], "a", "A", "ok", ok, None)
    first = store.snapshot()["venues"]["t1/a"]["lastComplete"]["at"]

    run2 = store.start_run("t1", "Tenant One", _venues("a", "b"), ["a"], "admin")
    store.record_venue(run2["id"], "a", "A", "failed", None, "ReadTimeout")
    rec = store.snapshot()["venues"]["t1/a"]
    assert rec["lastAttempt"]["status"] == "failed"
    assert rec["lastComplete"]["at"] == first, "a failure must not reset the last good check"
    assert rec["lastComplete"]["summary"]["counts"]["critical"] == 1


def test_run_status():
    store, _ = _store()
    run = store.start_run("t1", None, _venues("a", "b", "c"), ["a", "b", "c"], "admin")
    assert run["status"] == "running" and run["tally"]["notRun"] == 3
    store.record_venue(run["id"], "a", None, "ok", {}, None)
    store.record_venue(run["id"], "b", None, "partial", {}, None)
    done = store.finish_run(run["id"], stopped=True)
    assert done["status"] == "incomplete"
    assert done["tally"] == {"ok": 1, "partial": 1, "failed": 0, "notRun": 1}

    run = store.start_run("t1", None, _venues("a"), ["a"], "admin")
    store.record_venue(run["id"], "a", None, "ok", {}, None)
    assert store.finish_run(run["id"], stopped=False)["status"] == "complete"


def test_quiet_run_reads_interrupted():
    store, _ = _store()
    run = store.start_run("t1", None, _venues("a"), ["a"], "admin")
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=batch_runs.STALE_SECONDS + 60)).isoformat(timespec="seconds")
    store._data["runs"][0]["updatedAt"] = old
    assert store.get_run(run["id"])["status"] == "interrupted"


def test_record_survives_a_restart():
    store, path = _store()
    run = store.start_run("t1", "T", _venues("a"), ["a"], "admin")
    store.record_venue(run["id"], "a", "A", "ok", {"total": 2}, None)
    again = batch_runs.BatchStore(str(path))
    assert again.snapshot()["venues"]["t1/a"]["lastComplete"]["summary"] == {"total": 2}
    assert again.snapshot()["tenants"]["t1"]["venues"] == {"a": "Venue a"}


def test_broken_file_is_never_overwritten():
    store, path = _store()
    path.write_text("{not json", encoding="utf-8")
    broken = batch_runs.BatchStore(str(path))
    assert broken.broken and not broken.writable
    try:
        broken.start_run("t1", None, _venues("a"), ["a"], "admin")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a broken record accepted a write")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_runs_are_capped():
    store, _ = _store()
    for _ in range(batch_runs.MAX_RUNS + 5):
        store._data["runs"].insert(0, {"id": "x", "tenantId": "t", "planned": [], "results": {}})
    store.start_run("t", None, _venues("a"), ["a"], "admin")
    assert len(store._data["runs"]) == batch_runs.MAX_RUNS


# ── the roll-up ─────────────────────────────────────────────────────


def test_tenant_rollup_counts_and_freshness():
    store, _ = _store()
    run = store.start_run("t1", "T", _venues("a", "b", "c"), ["a", "b"], "admin")
    store.record_venue(run["id"], "a", "A", "ok", rollup.summarise(_report(FINDINGS)), None)
    store.record_venue(run["id"], "b", "B", "partial",
                       rollup.summarise(_report(FINDINGS, {"ports": "x"})), None)
    roll = rollup.tenant_rollup(store.snapshot(), "t1", days=7)
    assert roll["venueCount"] == 3
    assert roll["checked"] == 1, "a partial check is not a check"
    assert roll["fresh"] == 1
    assert roll["neverChecked"] == 2
    assert roll["lastAttemptNotOk"] == 1
    assert roll["totals"]["counts"]["critical"] == 1

    # Age the one good check past the window.
    snap = store.snapshot()
    snap["venues"]["t1/a"]["lastComplete"]["at"] = (
        datetime.now(timezone.utc) - timedelta(days=9)).isoformat(timespec="seconds")
    assert rollup.tenant_rollup(snap, "t1", days=7)["fresh"] == 0
    assert rollup.tenant_rollup(snap, "t1", days=10)["fresh"] == 1


def test_unlisted_venue_is_shown_but_not_totalled():
    store, _ = _store()
    run = store.start_run("t1", "T", _venues("a", "gone"), ["gone"], "admin")
    store.record_venue(run["id"], "gone", "Gone", "ok", rollup.summarise(_report(FINDINGS)), None)
    store.note_tenant("t1", "T", _venues("a"))   # R1 no longer lists it
    roll = rollup.tenant_rollup(store.snapshot(), "t1", days=7)
    assert [v["venueId"] for v in roll["venues"]] == ["a", "gone"]
    assert roll["venues"][1]["listed"] is False
    assert roll["totals"]["total"] == 0 and roll["venueCount"] == 1


def test_msp_rollup_sums_tenants_and_names_unbatched_ones():
    store, _ = _store()
    for tid in ("t1", "t2"):
        run = store.start_run(tid, tid.upper(), _venues("a"), ["a"], "admin")
        store.record_venue(run["id"], "a", "A", "ok", rollup.summarise(_report(FINDINGS)), None)
    msp = rollup.msp_rollup(store.snapshot(), 7, names={"t3": "Never batched"},
                            tenant_ids=["t3"])
    assert msp["tenantCount"] == 3 and msp["tenantsBatched"] == 2
    assert msp["totals"]["counts"]["critical"] == 2
    assert msp["checked"] == 2 and msp["venueCount"] == 2


# ── the router's pure helpers and the template ──────────────────────


def test_origin_is_scheme_and_host_only():
    from routers import batch_router
    ok = batch_router._origin
    assert ok("https://pisr.example.com") == "https://pisr.example.com"
    assert ok("http://localhost:4173") == "http://localhost:4173"
    for bad in ("javascript:alert(1)", "https://x.example/path", "file:///etc/passwd",
                "https://x.example\" onmouseover=\"", "", None):
        assert ok(bad) is None, bad


def test_template_renders_and_escapes():
    from routers.pisr_router import _jinja
    store, _ = _store()
    run = store.start_run("t1", "<b>Evil</b>", [{"id": "a", "name": "<img src=x>"}], ["a"], "admin")
    store.record_venue(run["id"], "a", "<img src=x>", "ok", rollup.summarise(_report(FINDINGS)), None)
    snap = store.snapshot()
    for overall in (None, rollup.msp_rollup(snap, 7)):
        tenants = overall["tenants"] if overall else [rollup.tenant_rollup(snap, "t1", 7)]
        for t in tenants:
            for v in t["venues"]:
                v["link"] = "https://pisr.example/?venue=a"
        for severities in ([], ["critical", "warning"]):
            for t2 in tenants:
                rollup.attach_detail(t2, severities)
            html = _jinja().get_template("reports/batch.html").render(
                title="MSP", overall=overall, tenants=tenants, days_label="7",
                generated_at="now", has_links=True, detail_severities=severities,
                categories=[{"key": k, "label": rollup.CATEGORY_LABELS[k]}
                            for k in rollup.CATEGORY_KEYS])
            assert "<img src=x>" not in html and "&lt;img src=x&gt;" in html
            assert "https://pisr.example/?venue=a" in html
            # The named checks appear only when asked for, and as catalogue
            # labels — never the finding's own title.
            assert ("APs offline" in html) is bool(severities)
            assert "Unit 4B" not in html
            # The detail is its own section BELOW the summary table, so the
            # table stays scannable. Both are keyed off the same flag.
            assert ("Needs attention" in html) is bool(severities)
            if severities:
                assert html.index("Needs attention") > html.index("Last checked")
            # Devices read as up/down, not as one number.
            assert "up/down" in html


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

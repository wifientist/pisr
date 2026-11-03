"""
`rollup.restrict`: the batch record as ONE CALLER is entitled to read it.

WHY THIS SUITE EXISTS. The roll-up routes were admin-only when they were
written, so nothing in them narrowed anything; putting them behind a grant
made `restrict` necessary, and it had no test at any level. The property it
protects is the one api/grants.py opens with:

    A CAPABILITY IS A LENS, NEVER A KEY.

    what a caller sees = redact(poll(scope(role) n requested), hidden_for(role))

and the half that is easy to miss is the DENOMINATOR. "48 of 58" handed to
somebody who may reach fourteen venues tells them an estate of 58 exists.
That is disclosure by an INTEGER, so no amount of hiding names catches it,
and it is exactly what an access test asserting status codes cannot see: the
response is a 200 either way.

HOW IT IS CHECKED, and why this is not just `restrict` compared with itself.
The expectation is built from the FIXTURE, not by re-running `restrict`'s
logic: for each case the test prunes the source world down to a declared set
of (tenant, venue) pairs and rolls THAT up. So the claim under test is

    restrict(whole world, scope) rolls up to exactly
    the world that only ever contained the allowed venues

which is the strongest available statement and, crucially, is EXHAUSTIVE OVER
THE FIGURES BY CONSTRUCTION. Whole dicts are compared, so a figure somebody
adds to `tenant_rollup` next month is covered the day it is added, with no
edit here — the same trick `test_role_redaction.py` plays by generating its
fixture from `sections.py`. A figure that `restrict` fails to narrow is a
mismatch, not a silence.

The allowed set in each case is DECLARED DATA, never derived by asking
`scope.allows_venue`. Deriving it would mean a bug in `Scope` producing a
matching bug in the expectation, and the two would agree all the way to
production.

Runs without pytest, like every suite here, and touches no network:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" -w /repo/api backend python tests/test_rollup_restrict.py
"""

import json
import random
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import batch_runs                              # noqa: E402
import scope as scope_rules                    # noqa: E402
from services.prism import punchlist, rollup    # noqa: E402

DAYS = 7.0

# ── the world ────────────────────────────────────────────────────────
#
# THREE TENANTS, deliberately: on an MSP these are different companies, and
# a bug that leaks between two of them looks identical to one that leaks
# between all of them unless there is a third to stay out of it.
#
# The venue mix matters as much as the count. Every figure the roll-up
# publishes has to be able to MOVE when a venue is taken away, or comparing
# dicts proves nothing — so there are clean venues and dirty ones, complete
# checks and partial ones, a never-checked venue and one R1 no longer lists.

WORLD = {
    "t-alpha": ("Alpha Holdings", {
        "v-a1": ("Alpha One", "ok", ["critical", "warning", "info"]),
        "v-a2": ("Alpha Two", "ok", []),                      # clean
        "v-a3": ("Alpha Three", "partial", ["critical"]),     # attempt not ok
        "v-a4": ("Alpha Four", None, None),                   # never checked
    }),
    "t-beta": ("Beta Group", {
        "v-b1": ("Beta One", "ok", ["warning", "warning"]),
        "v-b2": ("Beta Two", "ok", ["critical", "critical", "skipped"]),
    }),
    "t-gamma": ("Gamma Ltd", {
        "v-g1": ("Gamma One", "ok", ["info"]),
        "v-g2": ("Gamma Two", "failed", None),
        "v-g3": ("Gamma Three", "ok", ["critical", "warning"]),
    }),
}

#: Severity -> a finding `summarise` will keep, with a real check id.
_FINDING = {
    "critical": {"id": "aps-online", "severity": "critical",
                 "title": "3 APs offline", "evidence": [{"ap": "AP1"}]},
    "warning": {"id": "port-errors", "severity": "warning",
                "title": "CRC errors", "evidence": [{"switch": "IDF-2"}]},
    "info": {"id": "ap-naming", "severity": "info", "title": "Naming",
             "evidence": []},
    "skipped": {"id": "dhcp-pools", "severity": "skipped",
                "summary": "No DHCP pools"},
}

ALL_PAIRS = [(t, v) for t, (_, venues) in WORLD.items() for v in venues]


def _report(severities, venue_name):
    findings = [dict(_FINDING[s]) for s in severities]
    verification = {"findings": findings}
    return {
        "venue": {"name": venue_name},
        "verification": verification,
        "punchlist": punchlist.build(verification),
        "inventory": {"aps": {"total": 12, "online": 10, "offline": 1, "other": 1},
                      "switches": {"total": 2, "online": 2, "offline": 0, "other": 0}},
        "meta": {"errors": {}, "elapsedSeconds": 4.2,
                 "counts": {"aps": 12, "switches": 2, "clients": 40}},
    }


def _store():
    return batch_runs.BatchStore(str(Path(tempfile.mkdtemp()) / "batch-runs.json"))


def _world_snapshot(pairs=None):
    """
    The record, built through the REAL store so its shape cannot drift from
    what production writes, then frozen so two builds are comparable.

    `pairs` limits it to a declared set of (tenant, venue) — the independent
    expectation. A tenant with nothing allowed is absent entirely, which is
    what `restrict` does to one the caller cannot reach.
    """
    keep = None if pairs is None else set(pairs)
    store = _store()
    for tenant_id, (tenant_name, venues) in WORLD.items():
        wanted = {vid: meta for vid, meta in venues.items()
                  if keep is None or (tenant_id, vid) in keep}
        if not wanted:
            continue
        listing = [{"id": vid, "name": meta[0]} for vid, meta in wanted.items()]
        to_run = [vid for vid, meta in wanted.items() if meta[1]]
        run = store.start_run(tenant_id, tenant_name, listing, to_run, "admin")
        for vid, (name, status, severities) in wanted.items():
            if not status:
                continue
            summary = (rollup.summarise(_report(severities, name))
                       if severities is not None else None)
            store.record_venue(run["id"], vid, name, status, summary,
                               None if status != "failed" else "RequestException")
    return _freeze(store.snapshot())


def _freeze(snapshot):
    """
    Every timestamp replaced with a deterministic one.

    Two builds of the same world happen milliseconds apart, and
    `oldestCheckAt` / `newestCheckAt` are ISO strings that would differ —
    which would make every comparison below fail for a reason that has
    nothing to do with narrowing. Derived from the venue id so the oldest and
    newest are still meaningfully different from each other.
    """
    stamps = {vid: f"2026-10-{(i % 27) + 1:02d}T09:00:00+00:00"
              for i, (_, vid) in enumerate(sorted(ALL_PAIRS))}
    for key, record in (snapshot.get("venues") or {}).items():
        _, _, vid = key.partition("/")
        for field in ("lastComplete", "lastAttempt"):
            block = record.get(field)
            if isinstance(block, dict):
                block["at"] = stamps.get(vid, "2026-10-01T09:00:00+00:00")
    for tenant in (snapshot.get("tenants") or {}).values():
        tenant["listedAt"] = "2026-10-01T08:00:00+00:00"
    for run in snapshot.get("runs") or []:
        for field in ("startedAt", "updatedAt", "finishedAt"):
            if run.get(field):
                run[field] = "2026-10-01T08:00:00+00:00"
    return snapshot


FULL = _world_snapshot()


def _scope_for(pairs):
    """A `Scope` naming exactly these venues, per tenant."""
    ecs = {}
    for tenant_id, venue_id in pairs:
        ecs.setdefault(tenant_id, set()).add(venue_id)
    return scope_rules.Scope(unrestricted=False,
                             ecs={t: frozenset(v) for t, v in ecs.items()})


def _rollups(snapshot):
    """
    Every figure the read routes publish, for one snapshot.

    THE WHOLE SURFACE, not a chosen few — the MSP roll-up, each tenant's own,
    and `attach_detail`'s three venue lists. Comparing these as dicts is what
    makes the suite exhaustive over figures nobody has written yet.
    """
    msp = rollup.msp_rollup(snapshot, DAYS)
    out = {"msp": msp}
    for tenant in msp["tenants"]:
        tid = tenant["tenantId"]
        detail = rollup.attach_detail(rollup.tenant_rollup(snapshot, tid, DAYS),
                                      ["critical", "warning"])
        out[tid] = {
            "rollup": rollup.tenant_rollup(snapshot, tid, DAYS),
            "detailVenues": [v["name"] for v in detail.get("detailVenues") or []],
            "cleanVenues": [v["name"] for v in detail.get("cleanVenues") or []],
            "unrecorded": [v["name"] for v in detail.get("unrecordedVenues") or []],
        }
    return out


def _without_runs(figures):
    """
    Run history is DROPPED for a scoped caller, not narrowed — so `lastRun`
    is the one field the "world that only contained these venues" comparison
    cannot speak to. It gets its own assertion below.
    """
    cleaned = json.loads(json.dumps(figures))
    for key, value in cleaned.items():
        if key == "msp":
            for tenant in value["tenants"]:
                tenant["lastRun"] = None
        else:
            value["rollup"]["lastRun"] = None
    return cleaned


def _cases():
    """
    The scopes to narrow against: every single tenant, every single venue,
    every all-but-one, 60 seeded random subsets, and both extremes.

    SEEDED, so a failure is reproducible. Single-venue and all-but-one are
    the two shapes that catch an off-by-one in a denominator; the random
    middle is what catches a figure that is only wrong for some combinations.
    """
    cases = [("nothing", [])]
    for tenant_id, (_, venues) in WORLD.items():
        cases.append((f"all of {tenant_id}",
                      [(tenant_id, v) for v in venues]))
    for pair in ALL_PAIRS:
        cases.append((f"only {pair[0]}/{pair[1]}", [pair]))
        cases.append((f"all but {pair[0]}/{pair[1]}",
                      [p for p in ALL_PAIRS if p != pair]))
    rng = random.Random(20261103)
    for n in range(80):
        size = rng.randint(1, len(ALL_PAIRS))
        cases.append((f"random {n}", rng.sample(ALL_PAIRS, size)))
    cases.append(("everything", list(ALL_PAIRS)))
    return cases


CASES = _cases()


# ── the harness is not passing vacuously ─────────────────────────────

def test_the_fixture_can_actually_show_a_leak():
    """
    LOAD-BEARING, and the first thing to fix if this suite goes quiet. Every
    assertion below compares two roll-ups; a fixture whose figures are all
    zero, or that has one tenant, compares nothing and passes.
    """
    figures = _rollups(FULL)
    msp = figures["msp"]
    assert msp["tenantCount"] == 3, "fewer than three tenants cannot show a cross-tenant leak"
    assert msp["venueCount"] == len(ALL_PAIRS) == 9
    assert msp["checked"] >= 6 and msp["clean"] >= 1, (
        "the world needs both dirty and clean venues or `clean` never moves")
    for severity in ("critical", "warning", "info"):
        assert msp["totals"]["counts"][severity] > 0, (
            f"no {severity} findings in the fixture, so narrowing them is invisible")
    assert msp["totals"]["skipped"] > 0
    assert any(t["lastAttemptNotOk"] for t in msp["tenants"]), (
        "no partial or failed attempt, so that figure is never exercised")
    assert any(t["neverChecked"] for t in msp["tenants"])
    assert FULL["runs"], "no run history, so dropping it is untested"
    assert len(CASES) > 100, "too few scopes to call this exhaustive"


# ── the property ─────────────────────────────────────────────────────

def test_every_figure_is_narrowed_by_construction():
    """
    THE WHOLE SUITE IN ONE ASSERTION. For every scope, the restricted record
    rolls up to exactly the world that only ever held those venues.

    Whole dicts, so this covers `venueCount`, `checked`, `fresh`, `clean`,
    `neverChecked`, `lastAttemptNotOk`, `venuesWithCritical`, the severity and
    category totals, `deviceCount`, the per-tenant rows, the MSP sums and all
    three of `attach_detail`'s lists — plus whatever is added next, with no
    edit here.
    """
    for label, pairs in CASES:
        got = _without_runs(_rollups(rollup.restrict(FULL, _scope_for(pairs), ())))
        want = _without_runs(_rollups(_world_snapshot(pairs)))
        assert got == want, (
            f"scope {label!r}: the narrowed roll-up is not the roll-up of the "
            f"narrowed world.\n  got  {json.dumps(got, sort_keys=True)[:400]}\n"
            f"  want {json.dumps(want, sort_keys=True)[:400]}")


def test_the_denominator_moves_not_just_the_names():
    """
    The disclosure that hiding names would not catch. `venueCount` is the
    "of 58" in "48 of 58", and a caller allowed four venues must be told
    four — not four of nine.
    """
    for label, pairs in CASES:
        narrowed = rollup.restrict(FULL, _scope_for(pairs), ())
        msp = rollup.msp_rollup(narrowed, DAYS)
        assert msp["venueCount"] == len(pairs), (
            f"scope {label!r}: venueCount is {msp['venueCount']} for a caller "
            f"allowed {len(pairs)} venue(s) — the estate size leaked as a number")
        assert msp["checked"] <= len(pairs)
        assert msp["clean"] <= len(pairs)


def test_no_name_or_id_a_caller_may_not_reach_survives():
    """
    The belt to the braces above. Every figure could be correct while a name
    rode along in a list nothing compared — so the serialised output is
    searched for every id and venue name outside the scope.
    """
    for label, pairs in CASES:
        allowed = set(pairs)
        narrowed = rollup.restrict(FULL, _scope_for(pairs), ())
        blob = json.dumps(_rollups(narrowed))
        for tenant_id, (tenant_name, venues) in WORLD.items():
            for venue_id, (venue_name, _, _) in venues.items():
                if (tenant_id, venue_id) in allowed:
                    continue
                assert venue_id not in blob, (
                    f"scope {label!r}: venue id {venue_id} leaked")
                assert venue_name not in blob, (
                    f"scope {label!r}: venue name {venue_name!r} leaked")
            if not any(t == tenant_id for t, _ in allowed):
                assert tenant_id not in blob, (
                    f"scope {label!r}: tenant id {tenant_id} leaked")
                assert tenant_name not in blob, (
                    f"scope {label!r}: tenant name {tenant_name!r} leaked")


def test_an_unrestricted_caller_is_handed_the_record_untouched():
    """
    The cheap path, and the one that must not quietly start copying: an admin
    with nothing hidden gets the same object back.
    """
    assert rollup.restrict(FULL, scope_rules.UNRESTRICTED, ()) is FULL
    assert rollup.restrict(FULL, scope_rules.UNRESTRICTED, frozenset()) is FULL


def test_run_history_is_dropped_for_a_scoped_caller_and_kept_for_an_admin():
    """
    DROPPED, NOT NARROWED, and deliberately. A run row carries `planned` and
    `results` — counts over venues the caller may not see — so narrowing it
    would be three more chances to get a denominator wrong, for a list a
    scoped caller cannot act on: starting a run is admin-only.
    """
    scoped = rollup.restrict(FULL, _scope_for([("t-alpha", "v-a1")]), ())
    assert scoped["runs"] == [], "run history survived a scoped read"
    assert rollup.tenant_rollup(scoped, "t-alpha", DAYS)["lastRun"] is None
    assert rollup.restrict(FULL, scope_rules.UNRESTRICTED, ())["runs"], (
        "an admin lost the run history they have always had")


def test_a_venue_key_that_cannot_be_attributed_is_dropped():
    """
    Fail-closed on a malformed key. `venues` is keyed `tenant/venue`, and a
    key without a tenant cannot be checked against a scope — so it goes,
    rather than being passed through on the grounds that it looks harmless.
    """
    broken = json.loads(json.dumps(FULL))
    broken["venues"]["orphan-no-tenant"] = {"venueId": "orphan",
                                            "venueName": "Orphan Site"}
    broken["venues"]["t-alpha/"] = {"venueId": "", "venueName": "Blank"}
    narrowed = rollup.restrict(broken, _scope_for(ALL_PAIRS), ())
    assert "orphan-no-tenant" not in narrowed["venues"]
    assert "t-alpha/" not in narrowed["venues"]
    assert "Orphan Site" not in json.dumps(_rollups(narrowed))


# ── hidden checks: a different control, with a different rule ────────

def test_a_hidden_check_loses_its_NAME_but_not_its_COUNT():
    """
    THE ONE ASYMMETRY IN HERE, and the thing most likely to be "fixed" into a
    bug. Section visibility is DE-CLUTTERING: it decides what a reader is
    shown, never whether a problem exists. Subtracting hidden findings from
    `counts.critical` would report a venue as cleaner than it is to the very
    people sent to fix it.
    """
    hidden = frozenset({"aps-online"})
    plain = rollup.restrict(FULL, scope_rules.UNRESTRICTED, ())
    masked = rollup.restrict(FULL, _scope_for(ALL_PAIRS), hidden)

    assert (rollup.msp_rollup(masked, DAYS)["totals"]["counts"]["critical"]
            == rollup.msp_rollup(plain, DAYS)["totals"]["counts"]["critical"]), (
        "hiding a check changed the number of critical tasks, which under-reports "
        "work to the crew sent to do it")

    blob = json.dumps(_rollups(masked))
    assert "aps-online" not in blob, "a hidden check id was still named"
    assert "port-errors" in blob, "hiding one check took an unrelated one with it"


def test_hiding_every_check_leaves_the_counts_standing():
    """The same rule at its extreme, where an off-by-one would be visible."""
    every = frozenset({"aps-online", "port-errors", "ap-naming", "dhcp-pools"})
    masked = rollup.restrict(FULL, _scope_for(ALL_PAIRS), every)
    totals = rollup.msp_rollup(masked, DAYS)["totals"]
    assert totals["counts"]["critical"] > 0 and totals["counts"]["warning"] > 0
    detail = rollup.attach_detail(
        rollup.tenant_rollup(masked, "t-alpha", DAYS), ["critical", "warning"])
    for venue in detail.get("detailVenues") or []:
        assert not venue.get("checks"), (
            "a check survived in the named list while every id was hidden")


def test_hiding_checks_does_not_narrow_the_estate():
    """
    The two controls are orthogonal and must stay so: hiding a check is not a
    reason to drop a venue, and an admin reaching this path (which they do the
    moment any element is hidden from them) must keep every venue they had.
    """
    masked = rollup.restrict(FULL, scope_rules.UNRESTRICTED,
                             frozenset({"aps-online"}))
    assert rollup.msp_rollup(masked, DAYS)["venueCount"] == len(ALL_PAIRS)
    assert masked["runs"], (
        "an unrestricted caller lost the run history because a check was hidden")


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

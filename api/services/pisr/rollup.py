"""
Batch roll-up: a venue's report reduced to counts, and counts summed upward.

    MSP  ->  tenant (MSP-EC)  ->  venue (property)

Each level is a SUM of the one below and nothing more. The roll-up says WHICH
checks failed — by id, from PISR's own fixed catalogue — and how many devices
each names, but never what a finding SAYS. "APs online — 3 devices" is a
catalogue label and two integers; "3 APs offline in Unit 4B" is the report, and
the report is not kept. The detail is one click away in the live report, which
re-polls; that is the whole design.

`summarise` is the boundary of what `batch_runs.BatchStore` persists. If a
field is not produced here, it is not on disk. Every check id is checked
against `sections.ALL_CHECK_IDS` on the way in, so the only strings that can
reach the file are ones this repository already ships — an id PISR does not
recognise is dropped rather than written.
"""

from typing import Any, Dict, Iterable, List, Optional

import sections
from services.pisr import punchlist

SEVERITIES = punchlist.ACTIONABLE          # critical, warning, info
SEVERITY_RANK = {sev: i for i, sev in enumerate(SEVERITIES)}
CATEGORY_KEYS = [key for key, _, _ in punchlist.CATEGORIES]
CATEGORY_LABELS = {key: label for key, label, _ in punchlist.CATEGORIES}

# The vocabulary of check ids that may be written to the batch record. It is
# the section catalogue's, which `test_sections.py::test_checks_exist` already
# holds to `checks.py`'s actual finding ids — so a check renamed in one place
# is caught there rather than quietly becoming an unlabelled id on disk.
KNOWN_CHECKS = sections.ALL_CHECK_IDS


# ── narrowing the record to one caller ──────────────────────────────


def restrict(snapshot: Dict[str, Any], allowed, hidden_checks) -> Dict[str, Any]:
    """
    The record as ONE CALLER is entitled to read it: their tenants, their
    venues, and no check their policy hides.

    APPLIED TO THE SNAPSHOT, BEFORE ANY ROLL-UP READS IT, and that placement is
    the point. Every figure downstream — `venueCount`, `checked`, `fresh`,
    `clean`, the severity totals, the per-tenant rows, the run history and
    `attach_detail`'s three venue lists — is derived from this dict. Filter
    here and all of them are narrowed by construction; filter any of them
    individually and the next figure somebody adds is the one that escapes.

    IT NARROWS THE DENOMINATOR TOO. "48 of 58" handed to a caller who may reach
    fourteen venues tells them an estate of 58 exists, which is exactly the
    disclosure scope.py is there to prevent — and it is disclosure by a NUMBER,
    so no amount of hiding names would have caught it.

    Why the record needs this at all: it is written by an admin, whose scope is
    unrestricted, so it holds every venue of every tenant that has ever been
    batched. The roll-up routes were admin-only when they were written and
    therefore never had to narrow anything. Reaching them behind a grant is
    what makes this function necessary — see the invariant at the top of
    api/grants.py.

    Run history is dropped rather than narrowed — see below for why.

    `allowed` is a `scope.Scope`; `hidden_checks` is the check-id set from
    `sections.checks_for(visibility.hidden_for(role))`. An admin passes
    `scope.UNRESTRICTED` and an empty set, and this returns the snapshot
    unchanged — cheaply, without walking it.
    """
    hidden = frozenset(hidden_checks or ())
    if getattr(allowed, "unrestricted", False) and not hidden:
        return snapshot

    tenants: Dict[str, Any] = {}
    for tenant_id, tenant in (snapshot.get("tenants") or {}).items():
        if not allowed.allows_ec(tenant_id):
            continue
        kept = dict(tenant or {})
        kept["venues"] = {vid: name
                          for vid, name in (kept.get("venues") or {}).items()
                          if allowed.allows_venue(tenant_id, vid)}
        tenants[tenant_id] = kept

    venues: Dict[str, Any] = {}
    for key, record in (snapshot.get("venues") or {}).items():
        tenant_id, _, venue_id = str(key).partition("/")
        if not venue_id or not isinstance(record, dict):
            # A key this function cannot attribute to a tenant is a key it
            # cannot vouch for, and this is the fail-closed side of the house.
            continue
        if not allowed.allows_venue(tenant_id, venue_id):
            continue
        venues[key] = _strip_checks(record, hidden)

    # RUN HISTORY IS NOT NARROWED, IT IS DROPPED. A run row is operational
    # state about the batch itself — who started it, how many venues were
    # planned, how many were never reached — and every one of those is a count
    # over venues the caller may not see. Narrowing it would mean recomputing
    # `planned`, `results` and `tally` so they could not disclose the estate
    # size, which is three chances to get a denominator wrong in return for a
    # list the caller cannot act on: starting a run stays admin-only, so a
    # scoped caller has no use for the history of one.
    #
    # `unrestricted` is exactly "is an admin" here — `visibility.scope_for`
    # returns UNRESTRICTED for the admin role — so this drops nothing an admin
    # sees today.
    # Kept for an unrestricted caller, who reaches this line only when
    # `hidden_checks` is non-empty — a state an admin cannot be in today
    # (`hidden_for("admin")` is empty), but one this function should not
    # silently punish if the policy layer ever changes.
    runs = snapshot.get("runs") or []
    unrestricted = bool(getattr(allowed, "unrestricted", False))
    return {"tenants": tenants, "venues": venues,
            "runs": runs if unrestricted else []}


def _strip_checks(record: Dict[str, Any], hidden: frozenset) -> Dict[str, Any]:
    """
    One venue's record with hidden check ids removed from its summaries.

    THE COUNTS ARE LEFT ALONE, deliberately, and this is the one asymmetry in
    here worth reading twice. `counts.critical` is how many tasks a venue has;
    subtracting the hidden ones would report a venue as cleaner than it is to
    the very people sent to fix it, and section visibility is DE-CLUTTERING —
    it decides what a reader is shown, never whether a problem exists. So the
    number stays honest and the named list loses the rows.

    The consequence to know about: a caller with checks hidden can see that a
    venue has three critical tasks and be listed only two. That is the same
    thing the live report already does, and it is the right way round.
    """
    if not hidden:
        return record
    kept = dict(record)
    for field in ("lastComplete", "lastAttempt"):
        block = kept.get(field)
        if not isinstance(block, dict):
            continue
        summary = block.get("summary")
        if not isinstance(summary, dict) or not isinstance(summary.get("checks"), dict):
            continue
        block = dict(block)
        summary = dict(summary)
        summary["checks"] = {
            sev: {cid: n for cid, n in (ids or {}).items() if cid not in hidden}
            for sev, ids in summary["checks"].items()
        }
        # An empty severity is dropped so `check_detail` reads it the same way
        # it reads a venue that simply had nothing at that severity. `checks`
        # itself stays a dict, because `has_checks` uses its presence to tell
        # "nothing fired" from "recorded before ids were kept" — removing it
        # would relabel a filtered venue as an unrecorded one.
        summary["checks"] = {sev: ids for sev, ids in summary["checks"].items() if ids}
        block["summary"] = summary
        kept[field] = block
    return kept


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _device_states(block: Optional[Dict[str, Any]]) -> Dict[str, int]:
    block = block or {}
    return {"total": _int(block.get("total")), "online": _int(block.get("online")),
            "offline": _int(block.get("offline")), "other": _int(block.get("other"))}


def device_counts(summary: Optional[Dict[str, Any]],
                  kind: str) -> Optional[Dict[str, int]]:
    """
    `{total, up, down}` for "aps" or "switches", or None if never recorded.

    DOWN IS EVERYTHING NOT UP — offline plus `other` — not just `offline`.
    A venue with zero offline APs and five never-contacted ones is not a venue
    with nothing down, and `check_aps_online` already refuses to call that a
    pass. The roll-up must not be more optimistic than the check it summarises.

    Records written before device states were kept fall back to the flat fleet
    size, with `up`/`down` None, so an old row still shows how big the venue is
    rather than showing nothing.
    """
    states = ((summary or {}).get("devices") or {}).get(kind)
    if isinstance(states, dict):
        total, online = _int(states.get("total")), _int(states.get("online"))
        return {"total": total, "up": online, "down": max(total - online, 0)}
    flat = (summary or {}).get(kind)
    if flat is None:
        return None
    return {"total": _int(flat), "up": None, "down": None}


def summarise(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    One venue's punch list as numbers, plus WHICH checks produced them.

    `readErrors` carries the keys of the R1 reads that failed (`"ports"`,
    `"venueConfig:syslog"`) and never their messages — a message is an R1
    response and can quote the request. It is what makes a check `partial`,
    and naming which read failed is what lets someone tell a flaky endpoint
    from a venue that is genuinely unreachable.

    `checks` is `{severity: {checkId: deviceCount}}` — the ids that fired and
    how many devices each task names. An id outside `KNOWN_CHECKS` is DROPPED:
    the counts above already include it, so nothing is lost from the arithmetic,
    and the file is kept to strings this repository ships. That is what makes
    the detail in the roll-up PDF a catalogue label rather than tenant text.
    """
    punch = report.get("punchlist") or {}
    counts = punch.get("counts") or {}
    by_category: Dict[str, Dict[str, int]] = {}
    checks: Dict[str, Dict[str, int]] = {sev: {} for sev in SEVERITIES}
    for group in punch.get("groups") or []:
        key = group.get("key")
        if key not in CATEGORY_KEYS:
            continue
        group_counts = group.get("counts") or {}
        by_category[key] = {sev: _int(group_counts.get(sev)) for sev in SEVERITIES}
        for task in group.get("tasks") or []:
            check_id, severity = task.get("id"), task.get("severity")
            if check_id in KNOWN_CHECKS and severity in checks:
                # The device count, not the evidence: "how many to visit" is a
                # number, and the list of which ones is the report.
                checks[severity][check_id] = _int(task.get("count"))
    inventory = report.get("inventory") or {}
    meta = report.get("meta") or {}
    meta_counts = meta.get("counts") or {}
    return {
        "total": _int(punch.get("total")),
        "counts": {sev: _int(counts.get(sev)) for sev in SEVERITIES},
        "byCategory": by_category,
        "checks": {sev: dict(sorted(ids.items())) for sev, ids in checks.items() if ids},
        "skipped": len(punch.get("skipped") or []),
        "passed": _int(punch.get("passed")),
        "deviceCount": _int(punch.get("deviceCount")),
        "aps": _int(meta_counts.get("aps")),
        "switches": _int(meta_counts.get("switches")),
        # Per-device-type states, so the roll-up can say "11 up, 1 down"
        # rather than a bare fleet size — a count of APs says nothing about
        # whether the venue is working. `other` is carried separately because
        # online + offline does NOT account for a fleet: `shape._state` folds
        # never-contacted and mid-provision devices into "other", and reading
        # those as up is the false pass this tool exists to prevent.
        "devices": {
            "aps": _device_states(inventory.get("aps")),
            "switches": _device_states(inventory.get("switches")),
        },
        "clients": _int(meta_counts.get("clients")),
        "alarms": _int(meta_counts.get("incidents")),
        "readErrors": sorted(str(k) for k in (meta.get("errors") or {})),
        "elapsedSeconds": meta.get("elapsedSeconds"),
    }


def parse_severities(raw: Optional[str]) -> List[str]:
    """
    The `detail=` parameter: which severities the PDF lists per venue.

    Unknown words are ignored rather than refused — this decides how much
    detail a document carries, and a typo should not turn the download into an
    error page. An empty result means counts only, which is the old behaviour.
    """
    wanted = {word.strip().lower() for word in (raw or "").split(",")}
    return [sev for sev in SEVERITIES if sev in wanted]


def check_detail(summary: Optional[Dict[str, Any]],
                 severities: Iterable[str]) -> List[Dict[str, Any]]:
    """
    One venue's checks as rows: `{severity, id, label, count, category}`.

    Ordered critical first, then by label, so the eye lands on the worst thing
    in the venue. Labels come from `sections.check_label` — the same prose the
    visibility portal shows, so the PDF and the portal cannot call one check
    two things.

    An empty list means either nothing fired at those severities or the venue
    was checked before ids were recorded; `has_checks` below tells them apart,
    because "nothing critical here" and "not recorded" must not look the same.
    """
    wanted = [sev for sev in SEVERITIES if sev in set(severities)]
    stored = (summary or {}).get("checks") or {}
    rows = [{
        "severity": sev,
        "id": check_id,
        "label": sections.check_label(check_id),
        "count": _int(count),
        "category": CATEGORY_LABELS.get(punchlist.CHECK_CATEGORY.get(check_id, ""), ""),
    } for sev in wanted for check_id, count in (stored.get(sev) or {}).items()]
    rows.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), r["label"]))
    return rows


def has_checks(summary: Optional[Dict[str, Any]]) -> bool:
    """Whether this venue's record predates check ids being kept."""
    return isinstance((summary or {}).get("checks"), dict)


def attach_detail(tenant: Dict[str, Any], severities: Iterable[str]) -> Dict[str, Any]:
    """
    Decorate one tenant roll-up for rendering: per-venue check rows and device
    tallies, and the tenant's detail buckets.

    Here rather than in the router so the PDF's shape is testable without a
    request, and so the template loops over lists instead of deciding anything.
    The DETAIL IS A SEPARATE BUCKET on purpose — it renders as its own section
    under the summary table, never as rows inside it: the table is there to be
    scanned for which venues need attention, and a venue's checks interleaved
    into it destroy that.
    """
    wanted = list(severities)
    for row in tenant.get("venues") or []:
        row["checkDetail"] = check_detail(row.get("summary"), wanted)
        row["checksRecorded"] = has_checks(row.get("summary"))
        row["aps"] = device_counts(row.get("summary"), "aps")
        row["switches"] = device_counts(row.get("summary"), "switches")
    checked = [r for r in tenant.get("venues") or [] if r.get("summary")]
    tenant["detailVenues"] = [r for r in checked if r["checkDetail"]]
    # EVERY CHECKED VENUE APPEARS SOMEWHERE, including the ones with nothing
    # wrong. A roll-up that lists only problems cannot show an estate arriving
    # at "all clear" — the reader has to infer a venue is fine from its absence,
    # which is indistinguishable from its having been missed.
    tenant["cleanVenues"] = [r for r in checked
                             if r["checksRecorded"] and not r["checkDetail"]]
    # Checked before check ids were recorded. Its own list, never folded in
    # with the clean ones: "nothing failed" and "we did not keep what failed"
    # are different facts.
    tenant["unrecordedVenues"] = [r for r in checked if not r["checksRecorded"]]
    return tenant


def classify(summary: Dict[str, Any]) -> str:
    """ok, or partial when any R1 read failed underneath the punch list."""
    return "partial" if summary.get("readErrors") else "ok"


def _zero() -> Dict[str, Any]:
    return {"total": 0,
            "counts": {sev: 0 for sev in SEVERITIES},
            "byCategory": {key: 0 for key in CATEGORY_KEYS},
            "skipped": 0, "deviceCount": 0}


def _add(into: Dict[str, Any], summary: Optional[Dict[str, Any]]) -> None:
    if not summary:
        return
    into["total"] += _int(summary.get("total"))
    for sev in SEVERITIES:
        into["counts"][sev] += _int((summary.get("counts") or {}).get(sev))
    for key, per_sev in (summary.get("byCategory") or {}).items():
        if key in into["byCategory"] and isinstance(per_sev, dict):
            into["byCategory"][key] += sum(_int(v) for v in per_sev.values())
    into["skipped"] += _int(summary.get("skipped"))
    into["deviceCount"] += _int(summary.get("deviceCount"))


def _age_days(stamp: Optional[str]) -> Optional[float]:
    from batch_runs import _age_seconds
    age = _age_seconds(stamp)
    return None if age is None else age / 86400


def venue_rows(snapshot: Dict[str, Any], tenant_id: str,
               days: float) -> List[Dict[str, Any]]:
    """
    Every venue of one tenant: the ones it is known to have, plus any on record.

    A venue on record but no longer in the tenant's listing is kept and marked
    `listed: False` — it was checked, it may simply have been renamed or moved,
    and dropping it would hide the fact that the numbers above include it. It
    is left out of the tenant's totals, though, which describe the venues the
    tenant has now.
    """
    tenant = (snapshot.get("tenants") or {}).get(tenant_id) or {}
    directory: Dict[str, str] = tenant.get("venues") or {}
    records = {rec.get("venueId"): rec
               for key, rec in (snapshot.get("venues") or {}).items()
               if key.startswith(f"{tenant_id}/") and isinstance(rec, dict)}

    rows = []
    for venue_id in list(dict.fromkeys([*directory, *records])):
        rec = records.get(venue_id) or {}
        complete = rec.get("lastComplete") or {}
        attempt = rec.get("lastAttempt") or {}
        age = _age_days(complete.get("at"))
        rows.append({
            "venueId": venue_id,
            "name": directory.get(venue_id) or rec.get("venueName") or venue_id,
            "listed": venue_id in directory,
            "lastCompleteAt": complete.get("at"),
            "lastAttemptAt": attempt.get("at"),
            "lastAttemptStatus": attempt.get("status"),
            "lastAttemptError": attempt.get("error"),
            "lastAttemptReadErrors": (attempt.get("summary") or {}).get("readErrors") or [],
            # Fresh = checked completely within the window. Everything else —
            # never run, only ever partial, or simply old — is what the
            # "re-run what is stale" button selects.
            "fresh": age is not None and age <= days,
            "ageDays": None if age is None else round(age, 1),
            "summary": complete.get("summary"),
        })
    rows.sort(key=lambda r: (not r["listed"], str(r["name"]).lower()))
    return rows


def tenant_rollup(snapshot: Dict[str, Any], tenant_id: str, days: float,
                  name: Optional[str] = None) -> Dict[str, Any]:
    rows = venue_rows(snapshot, tenant_id, days)
    listed = [r for r in rows if r["listed"]]
    totals = _zero()
    for row in listed:
        _add(totals, row["summary"])
    tenant = (snapshot.get("tenants") or {}).get(tenant_id) or {}
    checked = [r for r in listed if r["lastCompleteAt"]]
    runs = [r for r in snapshot.get("runs") or [] if r.get("tenantId") == tenant_id]
    return {
        "tenantId": tenant_id,
        "name": name or tenant.get("name") or tenant_id,
        "listedAt": tenant.get("listedAt"),
        "venueCount": len(listed),
        "checked": len(checked),
        "fresh": sum(1 for r in listed if r["fresh"]),
        "neverChecked": sum(1 for r in listed if not r["lastCompleteAt"]),
        # Venues whose newest attempt did not come back clean, whatever their
        # last complete check says. The re-run filter already catches them;
        # this is the number that says a batch needs another go.
        "lastAttemptNotOk": sum(1 for r in listed
                                if r["lastAttemptStatus"] in ("partial", "failed")),
        # The number to watch go up. Fixed at critical and warning rather than
        # following the PDF's `detail=` selection, because this is also served
        # to the batch dialog where no selection exists — and because "clean"
        # ought to mean one thing across the tool. Info findings (a naming
        # convention, a placement note) do not stop a site being finished.
        "clean": sum(1 for r in checked
                     if not _int(((r["summary"] or {}).get("counts") or {}).get("critical"))
                     and not _int(((r["summary"] or {}).get("counts") or {}).get("warning"))),
        "oldestCheckAt": min((r["lastCompleteAt"] for r in checked), default=None),
        "newestCheckAt": max((r["lastCompleteAt"] for r in checked), default=None),
        "venuesWithCritical": sum(1 for r in listed
                                  if _int(((r["summary"] or {}).get("counts") or {})
                                          .get("critical"))),
        "totals": totals,
        "lastRun": runs[0] if runs else None,
        "venues": rows,
    }


def msp_rollup(snapshot: Dict[str, Any], days: float,
               names: Optional[Dict[str, str]] = None,
               tenant_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """
    Every tenant on record (plus any the caller names), summed.

    `tenant_ids` lets the caller add the live MSP-EC list, so an EC that has
    never been batched still appears — as zero venues known — rather than
    being absent, which would read as "this MSP has seven customers" when it
    has eight.
    """
    names = names or {}
    known = list(dict.fromkeys([*(tenant_ids or []),
                                *(snapshot.get("tenants") or {})]))
    tenants = [tenant_rollup(snapshot, tid, days, names.get(tid)) for tid in known]
    tenants.sort(key=lambda t: str(t["name"]).lower())
    totals = _zero()
    for t in tenants:
        for sev in SEVERITIES:
            totals["counts"][sev] += t["totals"]["counts"][sev]
        for key in CATEGORY_KEYS:
            totals["byCategory"][key] += t["totals"]["byCategory"][key]
        totals["total"] += t["totals"]["total"]
        totals["skipped"] += t["totals"]["skipped"]
        totals["deviceCount"] += t["totals"]["deviceCount"]
    return {
        "tenantCount": len(tenants),
        "tenantsBatched": sum(1 for t in tenants if t["checked"]),
        "venueCount": sum(t["venueCount"] for t in tenants),
        "checked": sum(t["checked"] for t in tenants),
        "fresh": sum(t["fresh"] for t in tenants),
        "clean": sum(t["clean"] for t in tenants),
        "totals": totals,
        "tenants": tenants,
    }

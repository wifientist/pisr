"""
Batch runs: check every venue of a tenant, one venue per request, and keep the
counts.

TWO ROUTERS, SPLIT BY PRIVILEGE. Reading an estate roll-up and polling every
venue of a live customer network are not the same act, so they are not behind
the same check:

  WRITES need the `batch.run` GRANT (`router`). Starting a run polls every
  venue of a live customer network and writes to the volume — a capability
  worth naming rather than a side effect of being an administrator.

  READS need the `msp.rollup` GRANT (`read_router`). Reading what a previous
  poll recorded is not the same act as making one.

Both live under /api/batch. NEITHER is under /api/admin any more: that
namespace is pinned to `require_admin` in both directions by test_access.py,
and a path saying "admin" for a route an ordinary user may hold is a lie the
next reader has to discover.

WHICH MAKES THE READ ROUTES NARROW THE RECORD, and it is the one thing to get
right in this file. The record is written by an admin, whose scope is
unrestricted, so it holds every venue of every tenant ever batched. A grant is
a lens and never a key: an `msp`-tagged user gets the roll-up of the ECs their
scope already allows, with the checks their policy already hides still hidden,
and the DENOMINATOR narrowed too — "48 of 58" tells a caller an estate of 58
exists. `rollup.restrict` does all of it, applied to the snapshot before any
roll-up reads it, so every figure downstream is narrowed by construction.

HUMAN-TRIGGERED, NOT SCHEDULED. There is no timer, no background task and no
queue here. An admin clicks Run in the browser and the BROWSER walks the venue
list, calling `POST /runs/{id}/venues/{venueId}` once per venue. So:

  * no request outlives Cloudflare's 100-second origin timeout — each is one
    venue's report, the same work `/pisr/{cid}/report` already does;
  * closing the tab stops the batch, and the run reads "interrupted" rather
    than anything finishing it behind the admin's back;
  * a stopped or failed batch is resumed by running the stale venues again,
    which is a filter in the dialog, not state in a worker.

WHAT IS KEPT. Counts only — see `services/pisr/rollup.summarise`, which is the
allowlist, and `batch_runs.py`, which explains why this file is the one place
PISR keeps anything read off a customer's network.

  GET  /api/batch/state                history, roll-ups and runs for the dialog
  GET  /api/batch/rollup.pdf           the roll-up, from the record — no re-poll
  POST /api/batch/runs                 start a run over a list of venues
  POST /api/batch/runs/{id}/venues/{venueId}         check one venue, record it
  POST /api/batch/runs/{id}/finish                   close the run

Every R1 call made here is `build_report`'s, so the read-only guarantee is the
report's own. Nothing in this file talks to R1 except through it and the
MSP-EC listing.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from weasyprint import HTML as WeasyHTML

import batch_runs
import sections as section_catalogue
import visibility
import grants as grant_catalogue
from auth import (generated_by, grants_of, hidden_of, require_admin,
                  require_grants, role_of, scope_of)
from config import CONTROLLER
from grants import (GRANT_BATCH_RUN, GRANT_MSP_BATCH, GRANT_MSP_ROLLUP,
                    GRANT_TENANT_ROLLUP)
from r1_client import build_r1_client, require_id, resolve_tenant
from redact import redact
from services.pisr import rollup
from services.pisr.collect import build_report

logger = logging.getLogger(__name__)

#: The write half. ADMIN-ONLY UNTIL R4; now behind `batch.run`, because
#: "polls every venue of a live customer network" is a capability worth naming
#: rather than a side effect of being an administrator. It is in no default,
#: so nobody holds it until somebody is given a role that names it.
#:
#: NOT under /api/admin any more, and the move is the point rather than
#: tidiness: that namespace is pinned to `require_admin` in both directions by
#: `test_access.py`, and a path that says "admin" for a route an ordinary user
#: may hold is a lie the next reader has to discover. Both halves of batch now
#: sit under /api/batch and are told apart by the grant they need, not by the
#: prefix.
router = APIRouter(prefix="/batch", tags=["Batch"],
                   dependencies=[Depends(require_grants(GRANT_BATCH_RUN))])

#: The read half. Same prefix as the writes, different grant: reading what a
#: batch recorded and starting one that polls a live customer network are
#: different privileges over the same feature, and the grant is what says so.
#: ONE TENANT's roll-up — the desk's view of one customer.
read_router = APIRouter(prefix="/batch", tags=["Batch"],
                        dependencies=[Depends(require_grants(GRANT_TENANT_ROLLUP))])

#: THE ESTATE — every tenant at once, summed.
#:
#: A SEPARATE ROUTER RATHER THAN A CONDITIONAL on the same one, and the reason
#: is the oracle. `api/access_surface.py` reads capabilities off route
#: DEPENDENCIES, so a route that decides which capability it needs by looking
#: at a query parameter is invisible to it — and the capability matrix, the
#: preview and the generated end-to-end expectations all read that. A rule
#: nothing can see is a rule nothing can check.
#:
#: It reads better too: "how is the estate doing" and "how is this customer
#: doing" are different questions asked by different people, and giving them
#: different addresses says so.
estate_router = APIRouter(prefix="/batch/estate", tags=["Batch"],
                          dependencies=[Depends(require_grants(GRANT_MSP_ROLLUP))])

# Generous: the biggest MSP-EC seen has a few hundred venues. The limit exists
# so a malformed body cannot write a megabyte of ids into the run record.
MAX_VENUES = 5000

# The link origin the browser hands the PDF route. Scheme and authority only —
# see `_origin`.
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


def _actor(request: Request) -> str:
    return getattr(request.state, "pisr_user", None) or "admin (passphrase)"


def _tenant_key(tenant_id: Optional[str]) -> str:
    """
    The id a run is filed under. An MSP must name its EC; an EC-scoped
    deployment files everything under its own tenant id, so the record reads
    the same shape either way.
    """
    override = resolve_tenant(CONTROLLER, tenant_id)
    return override or CONTROLLER.tenant_id


def _override_for(tenant_key: str) -> Optional[str]:
    """The inverse: what `build_report` wants as its tenant override."""
    return tenant_key if CONTROLLER.ec_type == "MSP" else None


def _require_store() -> None:
    store = batch_runs.STORE
    if not store.configured:
        raise HTTPException(503, "PISR_BATCH_FILE is not set, so there is "
                                 "nowhere to record a run.")
    if store.broken:
        raise HTTPException(503, f"{store.path} could not be read. It is left "
                                 "untouched so the history can be recovered; "
                                 "repair or move it and restart the container.")
    if not store.writable:
        raise HTTPException(503, f"{store.path} is not writable by the container. "
                                 "Mount a writable volume at its directory.")


def _days(days: float) -> float:
    return max(0.0, min(float(days), 3650.0))


def _readable(request: Request) -> Dict[str, Any]:
    """
    The record as THIS caller may read it, plus the scope that narrowed it.

    Both read routes start here. The scope comes back too because the routes
    still have to REFUSE a tenant outside it — narrowing a response is the
    courtesy, a 403 at the route is the control, and `pisr_router._require_scope`
    makes the same distinction for the same reason. A caller who names a
    tenant they may not reach gets told so, rather than being handed a roll-up
    of nothing and left to wonder whether the batch ever ran.
    """
    role = role_of(request)
    allowed = scope_of(request)
    hidden = section_catalogue.checks_for(hidden_of(request))
    return {
        "scope": allowed,
        "snapshot": rollup.restrict(batch_runs.STORE.snapshot(), allowed, hidden),
    }


def _require_ec(request: Request, allowed, tenant_key: str) -> None:
    """403 if this caller is not scoped to the tenant they named."""
    if not allowed.allows_ec(tenant_key):
        logger.warning("scope: refused batch tenant=%s to user=%s role=%s",
                       tenant_key, getattr(request.state, "pisr_user", "-"),
                       role_of(request))
        raise HTTPException(
            403, "This account is not scoped to that RUCKUS ONE customer.")


# ── reading ─────────────────────────────────────────────────────────


@read_router.get("/state")
async def get_state(request: Request,
                    tenant_id: Optional[str] = Query(None),
                    days: float = Query(7, description="A venue checked completely "
                                                       "within this many days is fresh.")):
    """
    What the dialog draws: the MSP-wide roll-up (tenants only, no venue lists),
    and — when a tenant is named, or always on an EC deployment — that
    tenant's per-venue rows and recent runs.

    Makes NO R1 call. The dialog lists the tenant's venues itself, through the
    ordinary `/pisr/{cid}/venues` route, and merges them; this is only the
    record.
    """
    store = batch_runs.STORE
    readable = _readable(request)
    snap = readable["snapshot"]
    window = _days(days)
    is_msp = CONTROLLER.ec_type == "MSP"

    msp = rollup.msp_rollup(snap, window)
    for tenant in msp["tenants"]:
        tenant.pop("venues", None)

    tenant = None
    runs: List[Dict[str, Any]] = snap["runs"]
    if tenant_id or not is_msp:
        key = _tenant_key(tenant_id)
        _require_ec(request, readable["scope"], key)
        tenant = rollup.tenant_rollup(snap, key, window)
        runs = [r for r in runs if r.get("tenantId") == key]

    return {
        "configured": store.configured,
        "writable": store.writable,
        "broken": store.broken,
        "path": str(store.path) if store.path else None,
        "isMsp": is_msp,
        "days": window,
        "staleSeconds": batch_runs.STALE_SECONDS,
        "categories": [{"key": k, "label": rollup.CATEGORY_LABELS[k]}
                       for k in rollup.CATEGORY_KEYS],
        "msp": msp,
        "tenant": tenant,
        # The list is for display. Each run carries its venue ids so the
        # dialog can show which were not reached, which is a few kilobytes on
        # a big tenant and worth it.
        "runs": runs[:20],
    }


# ── a run ───────────────────────────────────────────────────────────


class VenueRef(BaseModel):
    id: str = Field(..., min_length=1, max_length=128)
    name: Optional[str] = Field(None, max_length=256)


class StartBody(BaseModel):
    tenantId: Optional[str] = Field(None, max_length=128)
    tenantName: Optional[str] = Field(None, max_length=256)
    # The tenant's WHOLE venue list, as the dialog read it — the denominator of
    # "48 of 58". Not only the venues being run.
    venues: List[VenueRef] = Field(default_factory=list)
    venueIds: List[str] = Field(..., min_length=1)


class FinishBody(BaseModel):
    stopped: bool = False


@router.post("/sweeps", dependencies=[Depends(require_grants(GRANT_MSP_BATCH))])
async def plan_sweep(request: Request):
    """
    The tenants a multi-tenant sweep would cover, in order — scope-filtered.

    SWEEPING IS ITS OWN OPERATION, and this is where it is authorised. Polling
    one customer's estate because they asked is a different act from kicking
    off a poll across every company on the account, and an organisation may
    well let its desk do the first and not the second.

    WHICH tenants remains the scope's business, exactly as everywhere else —
    `msp.batch` says the sweep may be started, `filter_ecs` says what is in it.
    A caller scoped to one customer gets a one-tenant sweep, which is the
    correct and unremarkable answer rather than a refusal.

    HONEST ABOUT ITS LIMIT: the browser still starts one run per tenant
    afterwards, and each of those needs only `batch.run`. So somebody holding
    `batch.run` and not `msp.batch` could loop the single-tenant endpoint
    themselves rather than asking here. Closing that means the run endpoint
    accepting a list of tenants and refusing more than one without this
    capability — a change to the record's shape, and worth doing deliberately
    rather than as a side effect of this route.
    """
    if CONTROLLER.ec_type != "MSP":
        raise HTTPException(
            400, "This controller serves one tenant, so there is nothing to "
                 "sweep across. Run it directly.")
    allowed = scope_of(request)
    rows = await _live_ec_names()
    tenants = allowed.filter_ecs(
        [{"id": tid, "name": name} for tid, name in rows.items()])
    logger.info("batch: sweep planned over %d tenant(s) for user=%s",
                len(tenants), getattr(request.state, "pisr_user", "-"))
    return {"tenants": sorted(tenants, key=lambda r: str(r["name"]).lower())}


@router.post("/runs")
async def start_run(body: StartBody, request: Request):
    _require_store()
    if len(body.venues) > MAX_VENUES or len(body.venueIds) > MAX_VENUES:
        raise HTTPException(413, f"At most {MAX_VENUES} venues per run.")
    key = _tenant_key(body.tenantId)
    # These ids are written to the record and later interpolated into R1 paths
    # by `check_venue`, so they are checked on the way in like any other
    # caller id. See r1_client.require_id.
    for venue in body.venues:
        require_id(venue.id, "venue id")

    # SCOPE, AND IT IS NEW HERE FOR A REASON. This route was admin-only until
    # `batch.run` existed, and an admin is unrestricted by construction, so it
    # never needed to narrow anything. A grant can be held by a scoped user,
    # and a grant is a lens and never a key — so a batch may only cover the
    # venues its runner could already open one at a time. Refused rather than
    # filtered: silently dropping venues from a run would report "48 of 58"
    # against a list the caller never chose, and the denominator is the
    # disclosure.
    allowed = scope_of(request)
    _require_ec(request, allowed, key)
    beyond = [vid for vid in body.venueIds if not allowed.allows_venue(key, vid)]
    if beyond:
        logger.warning("scope: refused %d venue(s) in a batch for user=%s",
                       len(beyond), getattr(request.state, "pisr_user", "-"))
        raise HTTPException(
            403, f"{len(beyond)} of those venues are outside this account's "
                 "scope. Run the ones it covers, or ask for wider access.")
    known = {v.id for v in body.venues}
    unknown = [vid for vid in body.venueIds if vid not in known]
    if unknown:
        # Every venue run has to be one the tenant listing named, so the record
        # can never hold a venue id the tenant does not have.
        raise HTTPException(400, f"{len(unknown)} venue id(s) are not in the "
                                 "tenant's venue list.")
    try:
        return batch_runs.STORE.start_run(
            key, body.tenantName,
            [v.model_dump() for v in body.venues], body.venueIds, _actor(request))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/runs/{run_id}/venues/{venue_id}")
async def check_venue(run_id: str, venue_id: str, request: Request):
    """
    One venue: poll it, reduce the report to counts, record the counts.

    The server builds the report itself rather than accepting counts from the
    browser, so the record is PISR's reading of R1 and not whatever a client
    posted. The report is then dropped — only `rollup.summarise`'s integers
    survive this function.

    A failure is RECORDED, not raised. The browser moves on to the next venue
    either way, and the venue stays "not checked" for the re-run filter —
    which is what a failure should mean. The response still says it failed.
    """
    _require_store()
    require_id(venue_id, "venue id")
    # Checked again here and not only in `start_run`: a run id and a venue id
    # both come from the caller, so this is an independent path to polling one
    # venue of one tenant and has to stand on its own. The PDF route taught
    # that lesson already.
    run = batch_runs.STORE.get_run(run_id)
    if run is None:
        raise HTTPException(404, "No such run.")
    if venue_id not in run["venueIds"]:
        raise HTTPException(400, "That venue is not part of this run.")
    if run["finishedAt"]:
        raise HTTPException(409, "This run has already finished. Start another.")
    _require_ec(request, scope_of(request), run.get("tenantId") or "")
    if not scope_of(request).allows_venue(run.get("tenantId"), venue_id):
        logger.warning("scope: refused batch venue=%s to user=%s", venue_id,
                       getattr(request.state, "pisr_user", "-"))
        raise HTTPException(403, "This account is not scoped to that venue.")

    tenant_key = run["tenantId"]
    override = _override_for(tenant_key)
    summary: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    venue_name: Optional[str] = None
    try:
        r1 = build_r1_client(CONTROLLER)
        # Through `redact` like every report that leaves `build_report`, so the
        # credential scrub runs here too. Nothing hidden: the caller is an
        # admin, and admins see everything.
        report = redact(await build_report(r1, override, venue_id), ())
        summary = rollup.summarise(report)
        status = rollup.classify(summary)
        venue_name = (report.get("venue") or {}).get("name")
    except Exception as exc:  # noqa: BLE001 — every failure is a recorded outcome
        # The exception CLASS is recorded, not its text: a requests error can
        # quote the URL, and this file is on disk. The log has the rest.
        logger.warning("batch: run %s venue %s failed: %s", run_id, venue_id, exc)
        status, error = "failed", type(exc).__name__
        if isinstance(exc, HTTPException):
            error = f"HTTP {exc.status_code}"

    logger.info("batch: run %s venue=%s tenant=%s user=%s -> %s%s", run_id,
                venue_id, tenant_key, _actor(request), status,
                f" ({summary['total']} task(s))" if summary else "")
    try:
        described = batch_runs.STORE.record_venue(
            run_id, venue_id, venue_name, status, summary, error)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"venueId": venue_id, "status": status, "summary": summary,
            "error": error, "run": described}


@router.post("/runs/{run_id}/finish")
async def finish_run(run_id: str, body: FinishBody, request: Request):
    _require_store()
    # A run id is a caller's id like any other, so this is a third independent
    # path into the record and checks scope on its own. Closing somebody
    # else's run is a nuisance rather than a disclosure, but "it only does
    # something harmless" is not a reason for a route to skip the check —
    # that is how the one that does something harmful gets added next to it.
    run = batch_runs.STORE.get_run(run_id)
    if run is None:
        raise HTTPException(404, "No such run.")
    _require_ec(request, scope_of(request), run.get("tenantId") or "")
    try:
        return batch_runs.STORE.finish_run(run_id, body.stopped)
    except KeyError:
        raise HTTPException(404, "No such run.")
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


# ── the PDF ─────────────────────────────────────────────────────────


def _origin(value: Optional[str]) -> Optional[str]:
    """
    The address links in the PDF point at, as the BROWSER saw it.

    The server does not know its own public address — the enrolment link
    learned that (see CLAUDE.md), and `X-Forwarded-Host` is a header any peer
    can set. So the dialog passes `window.location.origin` and this accepts
    scheme://host[:port] and nothing else. Anything else means no links, not a
    guessed one: a PDF with no links is less useful, one with wrong links is
    misleading. WeasyPrint does not fetch <a href> targets, so this is not a
    resource the renderer resolves.
    """
    if value and _ORIGIN.match(value.strip()):
        return value.strip().rstrip("/")
    return None


def _report_link(origin: Optional[str], tenant_id: str, tenant_name: Optional[str],
                 venue_id: str) -> Optional[str]:
    """The deep link PISR.tsx reads on load — see `readDeepLink` there."""
    if not origin:
        return None
    params = {"venue": venue_id}
    if CONTROLLER.ec_type == "MSP":
        params = {"ec": tenant_id, **({"ecName": tenant_name} if tenant_name else {}),
                  "venue": venue_id}
    return f"{origin}/?{urlencode(params)}"


async def _live_ec_names() -> Dict[str, str]:
    """Best effort: the roll-up must render even if R1 is unreachable."""
    if CONTROLLER.ec_type != "MSP":
        return {}
    try:
        rows = await build_r1_client(CONTROLLER).msp.get_msp_ecs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch: MSP-EC list unavailable for the roll-up: %s", exc)
        return {}
    if isinstance(rows, dict):
        rows = rows.get("data")
    names = {}
    for row in rows if isinstance(rows, list) else []:
        ident = isinstance(row, dict) and (row.get("id") or row.get("tenantId"))
        if ident:
            names[str(ident)] = str(row.get("name") or ident)
    return names


def _filename(label: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(label))
    safe = "-".join(part for part in safe.split("-") if part)[:60] or "tenant"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"batch-rollup-{safe}-{stamp}.pdf"


@estate_router.get("/state")
async def get_estate_state(request: Request,
                           days: float = Query(7)):
    """The MSP-wide roll-up: every tenant, summed. No tenant named."""
    return await get_state(request, tenant_id=None, days=days)


@estate_router.get("/rollup.pdf")
async def get_estate_pdf(request: Request,
                         days: float = Query(7),
                         detail: Optional[str] = Query(None, max_length=100),
                         origin: Optional[str] = Query(None, max_length=300)):
    """The estate roll-up as a PDF. Every tenant in scope, on its own page."""
    return await get_rollup_pdf(request, tenant_id=None, days=days,
                                detail=detail, origin=origin)


@read_router.get("/rollup.pdf")
async def get_rollup_pdf(request: Request,
                         tenant_id: Optional[str] = Query(None),
                         days: float = Query(7),
                         detail: Optional[str] = Query(
                             None, max_length=100,
                             description="Severities to list per venue as named "
                                         "checks, comma separated: critical, "
                                         "warning, info. Omit for counts only."),
                         origin: Optional[str] = Query(None, max_length=300)):
    """
    The roll-up as a PDF, rendered from the RECORD — it does not re-poll.

    That is the difference from the venue PDF, and the reason the date of
    every row is printed beside it: this says what each venue looked like when
    it was last checked, not now. The live report is what a link opens.

    With `tenant_id` (or on an EC deployment) it is one tenant's venues. On an
    MSP without one it is every tenant, summarised, then each tenant's venues
    on its own page.
    """
    from routers.pisr_router import _jinja

    window = _days(days)
    link_base = _origin(origin)
    readable = _readable(request)
    snap, allowed = readable["snapshot"], readable["scope"]
    is_msp = CONTROLLER.ec_type == "MSP"

    if tenant_id or not is_msp:
        key = _tenant_key(tenant_id)
        _require_ec(request, allowed, key)
        tenants = [rollup.tenant_rollup(snap, key, window)]
        overall = None
        title = tenants[0]["name"]
    else:
        # The live EC list is the OTHER way a tenant id reaches this document —
        # `tenant_ids` makes an EC that has never been batched appear as zero
        # venues rather than not at all. It comes from R1, not the record, so
        # `restrict` never saw it and it has to be filtered here or the
        # narrowing has a second door. `filter_ecs` reads a list of rows.
        names = await _live_ec_names()
        names = {row["id"]: row["name"] for row in allowed.filter_ecs(
            [{"id": tid, "name": name} for tid, name in names.items()])}
        overall = rollup.msp_rollup(snap, window, names=names, tenant_ids=names)
        tenants = overall["tenants"]
        title = CONTROLLER.name

    # Which checks to name under each venue. Resolved here rather than in the
    # template so the template has rows to loop over and no logic of its own —
    # and so `checksRecorded` can say "this venue was checked before ids were
    # kept", which is a different thing from "nothing fired".
    severities = rollup.parse_severities(detail)
    for tenant in tenants:
        # Everything the template loops over — per-venue check rows, device
        # tallies, and the detail buckets — is shaped by `attach_detail`. Only
        # the link is built here, because only this layer knows the origin.
        rollup.attach_detail(tenant, severities)
        for row in tenant["venues"]:
            row["link"] = _report_link(link_base, tenant["tenantId"],
                                       tenant["name"], row["venueId"])

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = _jinja().get_template("reports/batch.html").render(
        title=title, overall=overall, tenants=tenants,
        days_label=f"{window:g}",
        generated_at=generated, generated_by=generated_by(request), categories=[
            {"key": k, "label": rollup.CATEGORY_LABELS[k]} for k in rollup.CATEGORY_KEYS],
        detail_severities=severities, has_links=bool(link_base))
    pdf = WeasyHTML(string=html).write_pdf()
    logger.info("batch: roll-up PDF %s (%d tenant(s), detail=%s, %d bytes)",
                "for " + tenants[0]["tenantId"] if overall is None else "MSP-wide",
                len(tenants), ",".join(severities) or "counts only", len(pdf))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'attachment; filename="{_filename(title)}"'})

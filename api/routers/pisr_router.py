"""
PISR — Property Install Status Report.

A read-only poll of one venue: what was installed, what is online, how it is
addressed, what VLANs and PoE it uses, which SSIDs are activated, and which of
those are demonstrably carrying traffic.

READ-ONLY. Every endpoint issues GETs and `*/query` POSTs against RUCKUS ONE and
nothing else. PISR creates nothing, changes nothing, activates nothing, and
stores nothing — no snapshot files, no database rows. A report exists for the
length of one HTTP response.

HUMAN-TRIGGERED ONLY. Every endpoint runs once per request. PISR registers no
scheduled job, starts no background task, and has no recurring-poll entry point
for a scheduler to call. The page refreshes when someone clicks refresh.

  GET /pisr/{cid}/scope    what tenant this controller acts on
  GET /pisr/{cid}/venues   venues for the picker, with the counts R1 aggregates
  GET /pisr/{cid}/report   one venue's full report
  GET /pisr/{cid}/checks   the check catalogue — what a report verifies

The `{cid}` is a vestige of rtools2, where it selected one of a user's saved
controllers. Here there is only ever one, from .env, and the segment is kept so
this file and the frontend that calls it stay diffable against their origin.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from functools import lru_cache
from pathlib import Path

from fastapi import Response
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML as WeasyHTML

import sections as section_catalogue
import visibility
from auth import role_of
from r1_client import build_r1_client, get_controller, resolve_tenant
from redact import redact, template_helpers as redact_helpers
import scrub as secret_scrub
from services.pisr import fetch as fetch_module
from services.pisr import shape as shape_module
from services.pisr.fetch import ap_groups as fetch_ap_groups
from reports.pisr import build_context as build_pdf_context
from services.pisr import checks as check_registry
from services.pisr.collect import build_report, list_venues

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pisr", tags=["PISR"])


@lru_cache(maxsize=1)
def _jinja() -> Environment:
    """
    Report templates live in api/templates.

    Two parents, not three: rtools2 had this file one level deeper, at
    routers/pisr/pisr_router.py. Getting this wrong fails at PDF-request time,
    not at import, so it is worth a comment.
    """
    return Environment(loader=FileSystemLoader(
        str(Path(__file__).resolve().parent.parent / "templates")))


def _export_name(venue_name: str, extension: str) -> str:
    """A filename that survives a download folder: no spaces, no punctuation."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(venue_name))
    safe = "-".join(part for part in safe.split("-") if part)[:60] or "venue"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"site-review-{safe}-{stamp}.{extension}"


def _require_scope(request: Request, tenant_id, venue_id=None) -> None:
    """
    Refuse a tenant or venue this caller's role may not reach.

    THE CONTROL, as opposed to the list filtering further down. Filtering a
    picker keeps other customers' names off someone's screen; this is what
    stops a hand-written URL. They are enforced separately on purpose — a
    filter that is also the check is a filter somebody will later "optimise"
    into a UI concern.

    403 rather than 404: pretending the venue does not exist would make a
    misconfigured scope indistinguishable from a deleted venue, and send
    someone hunting for a site that is sitting there working. See api/scope.py.
    """
    allowed = visibility.scope_for(role_of(request))
    if not allowed.allows_ec(tenant_id):
        logger.warning("scope: refused tenant=%s to user=%s role=%s",
                       tenant_id, getattr(request.state, "pisr_user", "-"),
                       role_of(request))
        raise HTTPException(
            403, "This account is not scoped to that RUCKUS ONE customer.")
    if venue_id is not None and not allowed.allows_venue(tenant_id, venue_id):
        logger.warning("scope: refused venue=%s on tenant=%s to user=%s role=%s",
                       venue_id, tenant_id,
                       getattr(request.state, "pisr_user", "-"), role_of(request))
        raise HTTPException(403, "This account is not scoped to that venue.")


@router.get("/{controller_id}/scope")
async def get_scope(controller_id: int) -> Dict[str, Any]:
    """Tells the UI whether it has to ask for an MSP-EC before anything else."""
    cfg = get_controller(controller_id)
    return {
        "controllerId": cfg.id,
        "controllerName": cfg.name,
        "subtype": cfg.ec_type,
        "needsEcSelection": cfg.ec_type == "MSP",
        "tenantId": cfg.tenant_id,
        "region": cfg.region,
    }


@router.get("/{controller_id}/venues")
async def get_venues(request: Request,
                     controller_id: int,
                     tenant_id: Optional[str] = Query(None)):
    """
    Every venue on the EC that this caller may reach, for the venue picker.

    Filtered rather than refused when the EC is allowed but only some of its
    venues are — a picker is a list, and a list is allowed to be short. The EC
    itself is still checked first: an EC nobody scoped this caller to is a 403,
    not an empty list, because an empty venue list reads as "this customer has
    no sites" and would have someone chasing R1 for an answer.
    """
    cfg = get_controller(controller_id)
    override = resolve_tenant(cfg, tenant_id)
    _require_scope(request, override)
    r1 = build_r1_client(cfg)
    venues = await list_venues(r1, override)
    allowed = visibility.scope_for(role_of(request))
    return {"tenantId": override or cfg.tenant_id,
            "venues": allowed.filter_venues(override, venues)}


@router.get("/{controller_id}/report")
async def get_report(request: Request,
                     controller_id: int,
                     venue_id: str = Query(..., description="Venue to report on"),
                     tenant_id: Optional[str] = Query(None)):
    """
    One venue, polled once, right now. Roughly a dozen concurrent reads; expect
    a few seconds on a large venue, most of it the switch-port query.
    """
    cfg = get_controller(controller_id)
    override = resolve_tenant(cfg, tenant_id)
    # Before the R1 client is built, so a refused request costs no upstream call.
    _require_scope(request, override, venue_id)
    r1 = build_r1_client(cfg)
    # `user` is set by SessionGateMiddleware in proxy mode and is "-" under a
    # shared passphrase, which cannot tell one person from another. This line
    # is the whole audit trail, and the honest reason to prefer SSO.
    role = role_of(request)
    logger.info("pisr: user=%s role=%s controller=%s tenant=%s venue=%s",
                getattr(request.state, "pisr_user", "-"), role,
                cfg.id, override, venue_id)
    return redact(await build_report(r1, override, venue_id),
                  visibility.hidden_for(role))


@router.get("/{controller_id}/report.pdf")
async def get_report_pdf(request: Request,
                         controller_id: int,
                         venue_id: str = Query(..., description="Venue to report on"),
                         tenant_id: Optional[str] = Query(None),
                         label: Optional[str] = Query(
                             None, description="Human name for the tenant, used in the "
                                               "report header. A tenant id is a hex string "
                                               "and reads badly on a shared document.")):
    """
    The whole review as a PDF.

    PISR stores nothing, so this re-polls the venue rather than rendering a
    saved run — the PDF is its own snapshot and may differ by a few clients
    from a page left open for a while. It is built from exactly the same
    report the UI renders, so the two cannot disagree about what was found.

    Narrative pages are portrait; the device inventory is a landscape named
    page with each table split in two, because sixteen columns do not fit a
    portrait page at a readable size.
    """
    cfg = get_controller(controller_id)
    override = resolve_tenant(cfg, tenant_id)
    # Same check as the JSON route, and for the same reason the redaction below
    # is repeated here: this endpoint re-polls independently, so every control
    # the JSON route applies has to be applied again or the download is the way
    # around it.
    _require_scope(request, override, venue_id)
    r1 = build_r1_client(cfg)

    # Redacted on the same terms as the JSON report, and this is the line that
    # matters most in this file. The PDF re-polls rather than rendering the one
    # the browser already has, so it is a second, independent path to the same
    # data — filter one and not the other and the download is the way around
    # the policy. Both go through `redact` for exactly that reason.
    role = role_of(request)
    hidden = visibility.hidden_for(role)
    report = redact(await build_report(r1, override, venue_id), hidden)
    context = build_pdf_context(report, cfg.name, label or tenant_id)

    template = _jinja().get_template("reports/pisr.html")
    # The section guards, injected here rather than added to build_context:
    # which sections a reader may see is a per-request question, and the
    # context builder shapes one report the same way every time.
    # They only remove headings — the data behind a hidden section was already
    # emptied by `redact` above, which is the part that actually enforces.
    pdf = WeasyHTML(string=template.render(
        **context,
        **redact_helpers(hidden),
        report_visibility=report.get("visibility"),
    )).write_pdf()

    venue_name = (report.get("venue") or {}).get("name") or venue_id
    filename = _export_name(venue_name, "pdf")
    logger.info("pisr: PDF for venue=%s user=%s role=%s (%d findings, %d "
                "sections hidden, %d bytes)",
                venue_id, getattr(request.state, "pisr_user", "-"), role,
                context["findings_total"], len(hidden), len(pdf))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{controller_id}/config/detail")
async def get_config_detail(request: Request,
                            controller_id: int,
                            venue_id: str = Query(..., description="Venue to read"),
                            tenant_id: Optional[str] = Query(None)):
    """
    AP-group and per-AP configuration, on demand.

    SEPARATE FROM THE REPORT ON PURPOSE. This is one R1 request per AP group,
    per group sub-resource, and per AP — a 200-unit MDU with a per-unit AP
    group is several hundred calls. Putting that behind every report would slow
    the common case to serve a tab most readers never open, so the Config tab
    shows the venue level immediately and fetches this when someone asks.

    Everything the report route does, this does too. It is a second path to
    R1 data, so it repeats the scope check and the scrub rather than assuming
    the report route already handled them — the download endpoint taught that
    lesson once already.
    """
    cfg = get_controller(controller_id)
    override = resolve_tenant(cfg, tenant_id)
    _require_scope(request, override, venue_id)

    role = role_of(request)
    hidden = visibility.hidden_for(role)
    if "config.ap-overrides" in hidden and "config.ap-groups" in hidden:
        # Both halves hidden means the button is not rendered for this reader,
        # so a request here is either a stale tab or somebody trying the URL.
        # 403 rather than an empty list: an empty result would read as "this
        # venue has no AP groups", which is a different and untrue statement.
        raise HTTPException(403, "Configuration detail is not shown at this "
                                 "access level.")

    r1 = build_r1_client(cfg)
    # to_thread like every other fetch here: the fetch layer is synchronous
    # requests, and PISR fans out through threads rather than an async client.
    groups = await asyncio.to_thread(fetch_ap_groups, r1, override, venue_id)
    group_ids = [g.get("id") for g in groups if g.get("id")]

    group_config = {}
    if group_ids:
        group_config = await asyncio.to_thread(
            fetch_module.ap_group_config, r1, override, venue_id, group_ids)

    aps = await asyncio.to_thread(fetch_module.access_points, r1, override, venue_id)
    serials = [ap.get("serialNumber") for ap in aps
               if ap.get("serialNumber")][:fetch_module.AP_CONFIG_LIMIT]

    ap_config = {}
    if serials:
        results = await asyncio.gather(
            *(asyncio.to_thread(fetch_module.ap_config, r1, override, serial)
              for serial in serials),
            return_exceptions=True)
        for serial, result in zip(serials, results):
            if not isinstance(result, Exception):
                ap_config[serial] = result
            else:
                logger.warning("pisr: AP config failed for %s: %s", serial, result)

    detail = shape_module.config_detail(groups, group_config, ap_config, len(aps))
    logger.info("pisr: config detail for venue=%s user=%s role=%s "
                "(%d group(s), %d AP(s), %d override(s))",
                venue_id, getattr(request.state, "pisr_user", "-"), role,
                len(detail["groups"]), detail["apShown"],
                detail["groupOverrideCount"] + detail["apOverrideCount"])

    # Scrubbed like everything else. `/venues/aps/{serial}` is the safe path —
    # its sibling `/venues/{venueId}/aps/{serial}` returns a plaintext
    # loginPassword and is never called — but this is a raw config dump and the
    # guarantee should not rest on that staying true.
    return secret_scrub.scrub_report(detail)


@router.get("/{controller_id}/checks")
async def get_checks(request: Request, controller_id: int):
    """
    The check catalogue — what a report verifies, without running one.

    Filtered by role like everything else. A check whose section is hidden
    would otherwise announce itself here — "AP naming follows a convention" —
    for a reader who is never shown the result, which is a worse experience
    than not listing it at all.

    Note the id here is derived from the function name, while the ids a section
    owns come from each check's own `_finding(check_id, ...)`. They agree for
    most checks and not for all, and the mismatch fails open: an id this cannot
    match stays listed. That is the right direction for a catalogue, but it is
    why `redact.py` filters findings by the finding's own id rather than
    reusing this expression.
    """
    get_controller(controller_id)
    hidden_checks = section_catalogue.checks_for(visibility.hidden_for(role_of(request)))
    return {
        "checks": [{"id": check_id,
                    "description": (fn.__doc__ or "").strip()}
                   for fn in check_registry.CHECKS
                   if (check_id := fn.__name__.replace("check_", "").replace("_", "-"))
                   not in hidden_checks],
        "thresholds": {
            "apGroupSsidLimit": check_registry.AP_GROUP_SSID_LIMIT,
            "poeWarnPct": check_registry.POE_WARN_PCT,
            "poeCriticalPct": check_registry.POE_CRIT_PCT,
            "dhcpWarnPct": check_registry.DHCP_WARN_PCT,
        },
    }

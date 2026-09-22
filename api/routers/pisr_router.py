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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from functools import lru_cache
from pathlib import Path

from fastapi import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML as WeasyHTML

import sections as section_catalogue
import visibility
from auth import require_admin, role_of
from r1_client import build_r1_client, get_controller, resolve_tenant
from redact import redact, template_helpers as redact_helpers
import scrub as secret_scrub
from services.pisr import fetch as fetch_module
from services.pisr import shape as shape_module
from services.pisr import trace as trace_module
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

    AUTOESCAPE IS ON, AND MUST STAY ON. Jinja defaults it to OFF, and this
    template interpolates strings that callers control: `label` is a query
    parameter on the PDF route, so ANY authenticated user — including a plain
    `user` — could put markup in it, and venue and device names arrive from the
    RUCKUS ONE tenant. Without escaping, all of that lands in the HTML that
    WeasyPrint renders.

    That is not the usual stored-XSS story, because the output is a PDF and
    WeasyPrint runs no JavaScript. It is worse in one specific way: WeasyPrint
    RESOLVES the resources the document references. An injected
    `<img src="http://...">` makes the container issue that request, and a
    `file://` URL asks it to read a local path — so an unescaped label is a
    `user`-triggered SSRF and a candidate local-file read, from a role that is
    supposed to be able to read one venue's report.

    Safe to turn on: the template uses `|safe` nowhere, so nothing in it was
    relying on markup passing through. `test_sections.py::test_pdf_template_
    autoescapes` is what stops this being switched back off.
    """
    return Environment(
        loader=FileSystemLoader(
            str(Path(__file__).resolve().parent.parent / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )


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
    if ({"config.ap-overrides", "config.ap-groups", "config.networks"} <= set(hidden)):
        # Every section of the detail hidden means the button is not rendered for
        # this reader, so a request here is either a stale tab or somebody trying
        # the URL. 403 rather than an empty list: an empty result would read as
        # "this venue has no AP groups or networks", which is a different and
        # untrue statement.
        raise HTTPException(403, "Configuration detail is not shown at this "
                                 "access level.")

    r1 = build_r1_client(cfg)
    # to_thread like every other fetch here: the fetch layer is synchronous
    # requests, and PISR fans out through threads rather than an async client.
    #
    # TWO WAVES, not six stages. The groups, the APs and the activated networks
    # are independent listings, so they are read together; then every per-object
    # read — each group's sub-resources, each AP, each network — goes out in one
    # gather. Run in sequence, with the group reads serial inside one thread,
    # a per-unit MDU could take minutes — past Cloudflare's 100-second origin
    # timeout, which the browser sees as an HTTP 524. Everything still goes through the one default
    # executor, so the R1 connection pool's bound holds — see the note on
    # venue_config_one before reaching for a pool of your own.
    groups, aps, activations = await asyncio.gather(
        asyncio.to_thread(fetch_ap_groups, r1, override, venue_id),
        asyncio.to_thread(fetch_module.access_points, r1, override, venue_id),
        # The config side of "which SSIDs are deployed here", so it names the
        # networks worth reading rather than every network on the tenant.
        asyncio.to_thread(fetch_module.venue_activations, r1, override, venue_id))

    group_total = len(groups)
    groups = groups[:fetch_module.AP_GROUP_CONFIG_LIMIT]
    group_ids = [g.get("id") for g in groups if g.get("id")]
    serials = [ap.get("serialNumber") for ap in aps
               if ap.get("serialNumber")][:fetch_module.AP_CONFIG_LIMIT]
    network_ids = list(dict.fromkeys(
        a.get("networkId") for a in activations if a.get("networkId")))
    capped_ids = network_ids[:fetch_module.NETWORK_CONFIG_LIMIT]

    reads = {}
    for gid in group_ids:
        for key in ("detail", *fetch_module.AP_GROUP_CONFIG_SOURCES):
            reads[("group", gid, key)] = (fetch_module.ap_group_config_one,
                                          r1, override, venue_id, gid, key)
    for serial in serials:
        reads[("ap", serial)] = (fetch_module.ap_config, r1, override, serial)
    for nid in capped_ids:
        reads[("network", nid)] = (fetch_module.network_config, r1, override, nid)

    results = await asyncio.gather(
        *(asyncio.to_thread(*call) for call in reads.values()),
        return_exceptions=True)

    group_config: Dict[str, Dict[str, Any]] = {gid: {} for gid in group_ids}
    ap_config: Dict[str, Any] = {}
    network_config: Dict[str, Any] = {}
    for tag, result in zip(reads, results):
        if isinstance(result, Exception):
            logger.warning("pisr: config detail read %s failed: %s",
                           " ".join(map(str, tag)), result)
            if tag[0] == "group":
                # A failed sub-resource is a missing block, as a non-2xx already
                # is inside _json; the group itself is still listed.
                group_config[tag[1]][tag[2]] = None
            continue
        if tag[0] == "group":
            group_config[tag[1]][tag[2]] = result
        elif tag[0] == "ap":
            ap_config[tag[1]] = result
        else:
            network_config[tag[1]] = result

    detail = shape_module.config_detail(groups, group_config, ap_config, len(aps),
                                        network_config, len(network_ids),
                                        group_total=group_total)
    logger.info("pisr: config detail for venue=%s user=%s role=%s "
                "(%d group(s), %d AP(s), %d network(s), %d override(s))",
                venue_id, getattr(request.state, "pisr_user", "-"), role,
                len(detail["groups"]), detail["apShown"], detail["networkShown"],
                detail["groupOverrideCount"] + detail["apOverrideCount"])

    # Scrubbed like everything else. `/venues/aps/{serial}` is the safe path —
    # its sibling `/venues/{venueId}/aps/{serial}` returns a plaintext
    # loginPassword and is never called — but this is a raw config dump and the
    # guarantee should not rest on that staying true.
    return secret_scrub.scrub_report(detail)


@router.get("/{controller_id}/identity/trace",
            dependencies=[Depends(require_admin)])
async def get_identity_trace(request: Request,
                             controller_id: int,
                             venue_id: str = Query(..., description="Venue to trace"),
                             tenant_id: Optional[str] = Query(None)):
    """
    Every DPSK username on this venue, followed through its adaptive policy's
    conditions to the network, AP groups and APs it lands on.

    ADMIN ONLY, and that is the control rather than a courtesy. The rows name
    residents' DPSK usernames — the thing `shape._dpsk_safe` refuses to put in
    a report, for a report is handed to install crews. This is a diagnostic an
    admin runs, returned once and never stored; `require_admin` is the gate.

    Separate from the report for the same reason as config/detail: the
    conditions are one R1 call per policy, and a per-unit MDU puts a thousand
    policies in one venue's set. Everything the report route does, this does
    too — scope check and scrub — because it is another path to R1 data.

    THREE WAVES. The listings; then the set members and every scoped pool's
    usernames; then every member policy's conditions. Each wave needs the one
    before, and each is one gather on the default executor, so the R1
    connection pool's bound holds.
    """
    cfg = get_controller(controller_id)
    override = resolve_tenant(cfg, tenant_id)
    _require_scope(request, override, venue_id)

    r1 = build_r1_client(cfg)
    f = fetch_module
    (pools, groups, sets, policies, networks, activations, ap_groups, aps,
     radius_groups) = \
        await asyncio.gather(
            asyncio.to_thread(f.dpsk_pools, r1, override),
            asyncio.to_thread(f.identity_groups_all, r1, override),
            asyncio.to_thread(f.policy_sets, r1, override),
            asyncio.to_thread(f.adaptive_policies, r1, override),
            asyncio.to_thread(f.wifi_networks, r1, override),
            asyncio.to_thread(f.venue_activations, r1, override, venue_id),
            asyncio.to_thread(fetch_ap_groups, r1, override, venue_id),
            asyncio.to_thread(f.access_points, r1, override, venue_id),
            asyncio.to_thread(f.radius_attribute_groups, r1, override))

    # The report's own scoping, so the trace covers exactly the pools and sets
    # the Identity tab shows. Pure; the passphrase counts it would take are not
    # needed here.
    dpsk = shape_module.dpsk_card(pools, groups, activations, networks,
                                  venue_id, None, {})
    scoped_set_ids = shape_module.scoped_policy_set_ids(
        dpsk["pools"], dpsk.get("otherIdentityGroups") or [], sets)
    pool_ids = [row["id"] for row in dpsk["pools"] if row.get("id")]
    set_ids = sorted(scoped_set_ids)
    group_ids = sorted({g.get("id") for row in dpsk["pools"]
                        for g in row.get("identityGroups") or [] if g.get("id")})

    wave2 = await asyncio.gather(
        *(asyncio.to_thread(f.policy_set_members, r1, override, sid) for sid in set_ids),
        *(asyncio.to_thread(f.dpsk_usernames, r1, override, pid) for pid in pool_ids),
        *(asyncio.to_thread(f.identity_details, r1, override, gid) for gid in group_ids),
        return_exceptions=True)
    n_sets, n_pools = len(set_ids), len(pool_ids)
    set_members: Dict[str, Any] = {}
    usernames: Dict[str, Any] = {}
    identities: Dict[str, Any] = {}
    for sid, result in zip(set_ids, wave2[:n_sets]):
        set_members[sid] = [] if isinstance(result, Exception) else result
    for pid, result in zip(pool_ids, wave2[n_sets:n_sets + n_pools]):
        if isinstance(result, Exception):
            logger.warning("pisr: trace usernames failed for pool %s: %s", pid, result)
            result = {"rows": [], "total": 0, "complete": False}
        usernames[pid] = result
    for gid, result in zip(group_ids, wave2[n_sets + n_pools:]):
        if isinstance(result, Exception):
            # Names and descriptions only; the trace itself does not need them.
            logger.warning("pisr: trace identities failed for group %s: %s", gid, result)
            result = {"rows": [], "total": 0, "complete": False}
        identities[gid] = result

    policy_type = {p.get("id"): p.get("policyType") for p in policies}
    wanted = list(dict.fromkeys(
        m.get("policyId") for sid in set_ids for m in set_members[sid]
        if m.get("policyId") in policy_type))
    capped = wanted[:f.POLICY_CONDITION_LIMIT]
    results = await asyncio.gather(
        *(asyncio.to_thread(f.policy_conditions, r1, override, pid, policy_type[pid])
          for pid in capped),
        return_exceptions=True)
    conditions = {pid: (None if isinstance(r, Exception) else r)
                  for pid, r in zip(capped, results)}

    payload = trace_module.identity_trace(
        pool_rows=dpsk["pools"], raw_pools=pools, usernames=usernames,
        identities=identities, radius_groups=radius_groups,
        sets=sets, scoped_set_ids=scoped_set_ids, set_members=set_members,
        policies=policies, conditions=conditions, conditions_wanted=len(wanted),
        networks=networks, activations=activations,
        aps=shape_module.ap_views(aps, ap_groups), ap_groups=ap_groups)

    # Logged without a single username, deliberately: the container log is not
    # somewhere residents' names should accumulate.
    logger.info("pisr: identity trace for venue=%s user=%s (%d username(s), "
                "%d policy/policies, %d condition read(s)) — %s",
                venue_id, getattr(request.state, "pisr_user", "-"),
                payload["summary"]["total"], len(wanted), len(capped),
                payload["summary"]["byStatus"])
    return secret_scrub.scrub_report(payload)


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

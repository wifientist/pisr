"""
The catalogue of report sections that an admin can show or hide.

WHY THIS FILE EXISTS. PISR renders the same report twice — once as React in
`src/pages/PISR.tsx` and once as HTML-for-print in
`api/templates/reports/pisr.html` — and the two are meant to be the same
picture. Hiding a card by editing markup means editing both, remembering both,
and getting them to agree forever. They already drifted once (the spectrum
chart's paint order, noted in CLAUDE.md), and that was a bug nobody could see
without opening a PDF.

So visibility is not a property of markup. It is a property of DATA. Each
section below names the paths of the report payload it owns, and `redact.py`
empties those paths before the report leaves the process. Both renderers then
draw what they are given and neither needs to know a policy exists. The PDF
route re-polls through the same `build_report`, so it is covered by the same
one filter — which is the whole reason for doing it this way rather than
guarding markup.

The markup guards in both renderers are still there, but they are cosmetic:
they remove the empty container that would otherwise say "no rows". The filter
is the enforcement. If you are ever deciding which to trust, trust this file.

EMPTIED, NOT DELETED. A hidden path is replaced by an empty value of the same
type — `[]` for a list, `{}` for a dict, `0`/None for a scalar — never removed.
Deleting keys would mean every `.length`, `.map` and `|length` downstream in
two languages has to be defensive about a key that is normally always present,
and the first one that is not defensive is a blank page rather than a hidden
card.

CHECKS ARE CROSS-CUTTING, which is the trap. Findings are computed from the
whole report, so hiding the PoE cards while leaving the Verification card alone
still announces "switch-3 is at 94% of its PoE budget". Every section therefore
also names the check ids it owns, and `redact.py` drops those findings and
recomputes the tallies. A check id that appears in no section here is never
hidden — see DEFAULTS below.

DEFAULTS ARE OPEN. A section nobody has hidden is visible, and an id that does
not appear in a policy is visible. That matches what this feature is for —
reducing clutter for the ordinary reader, not keeping secrets — and it means
adding a card is never silently blocked on an admin remembering to reveal it.
If a genuinely need-to-know section ever arrives, it needs a deny-by-default
flag here and a deliberate decision, not a quiet reuse of this one.

KEEPING THIS HONEST. The ids below are typed by hand into two other files.
`api/tests/test_sections.py` asserts that every id here appears in both, and
that neither file guards on an id this catalogue does not know. That test is
the only thing standing between this design and the drift it was built to
avoid; do not skip it when adding a section.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Section:
    """
    One hideable piece of the report.

    `id`     stable, dotted, `<tab>.<thing>`. It is written into PISR.tsx and
             pisr.html verbatim, and stored in the policy file, so renaming one
             is a migration, not an edit.
    `paths`  dotted paths into the report payload this section OWNS. Owning
             means: if this section is hidden, this data does not leave the
             process. A path may be owned by exactly one section — the test
             enforces it — because two owners means hiding either one empties
             data the other still renders.
    `checks` finding ids (from `checks.py`'s `_finding(check_id, ...)`) whose
             results only make sense alongside this section.
    """

    id: str
    label: str
    tab: str
    hint: str = ""
    paths: Tuple[str, ...] = ()
    checks: Tuple[str, ...] = ()


# Tab order matches the tab bar in PISR.tsx. The admin portal groups by this,
# and a tab whose every section is hidden is dropped from the bar entirely —
# an empty tab is worse than a missing one.
TABS: List[Tuple[str, str]] = [
    ("overview", "Overview"),
    ("wireless", "Wireless"),
    ("wired", "Wired & PoE"),
    ("addressing", "Addressing"),
    ("identity", "Identity & Policy"),
    ("devices", "Devices"),
    ("report", "Whole report"),
]


SECTIONS: Tuple[Section, ...] = (
    # ── Overview ─────────────────────────────────────────────────────
    Section(
        id="overview.summary",
        label="Headline tiles",
        tab="overview",
        hint="APs online, switches online, SSIDs, clients, PoE allocated.",
        # No paths: every tile is read from data another section owns, so
        # emptying anything here would blank a card somebody else is showing.
        # Markup-guarded only, and that is correct for a derived summary.
        checks=("clients-present",),
    ),
    Section(
        id="overview.verification",
        label="Verification & findings",
        tab="overview",
        hint="Every check PISR ran, and what it found. Hiding this hides the "
             "findings section of the PDF too.",
        paths=("verification",),
    ),
    Section(
        id="overview.access-points",
        label="Access point inventory",
        tab="overview",
        hint="Status donut, models and firmware.",
        paths=("inventory.aps",),
        checks=("aps-online", "aps-provisioned", "ap-firmware"),
    ),
    Section(
        id="overview.switches",
        label="Switch inventory",
        tab="overview",
        hint="Status donut, models and firmware.",
        paths=("inventory.switches",),
        checks=("switches-online", "switch-firmware", "switch-config-sync"),
    ),
    Section(
        id="overview.venue-config",
        label="Venue configuration",
        tab="overview",
        hint="Management VLAN, mesh, and the radio defaults the venue asks for.",
        paths=("venue.managementVlan", "venue.mesh", "venue.meshEnabled",
               "venue.meshZeroTouch", "venue.radio"),
        checks=("mgmt-vlan",),
    ),
    Section(
        id="overview.property",
        label="MDU property features",
        tab="overview",
        hint="Unit counts, residents and communication settings.",
        paths=("venue.property", "venue.isProperty"),
    ),

    # ── Wireless ─────────────────────────────────────────────────────
    Section(
        id="wireless.ssids",
        label="SSIDs activated on this venue",
        tab="wireless",
        hint="The SSID table, with what each one is actually carrying.",
        paths=("wireless.rows",),
        checks=("ssids-activated", "ssids-carrying", "ssids-broadcasting",
                "ssid-scope", "ssid-vlan-carried"),
    ),
    Section(
        id="wireless.clients-by-band",
        label="Clients by band",
        tab="wireless",
        paths=("clients.byBand",),
    ),
    Section(
        id="wireless.signal-quality",
        label="Signal quality",
        tab="wireless",
        hint="RSSI distribution of associated clients.",
        paths=("clients.byRssi",),
    ),
    Section(
        id="wireless.clients-per-ssid",
        label="Clients per SSID",
        tab="wireless",
        paths=("clients.bySsid",),
    ),
    Section(
        id="wireless.connection-health",
        label="Connection health",
        tab="wireless",
        hint="R1's own verdict per client.",
        paths=("clients.byHealth",),
    ),
    Section(
        id="wireless.busiest-aps",
        label="Busiest APs",
        tab="wireless",
        paths=("clients.topAps",),
    ),
    Section(
        id="wireless.channel-plan",
        label="Channel plan",
        tab="wireless",
        hint="The spectrum chart — what the venue asks for against what the "
             "APs landed on.",
        paths=("radios.bands", "radios.plan"),
        checks=("24ghz-channel-plan",),
    ),
    Section(
        id="wireless.ap-groups",
        label="AP groups",
        tab="wireless",
        hint="Groups, their AP counts, and how many SSIDs land on each.",
        paths=("wireless.groups", "wireless.perApGroup"),
        # Only the SSID-limit check. `check_empty_ap_groups` reads AP groups but
        # emits its finding under the id "ssid-scope", which wireless.ssids owns
        # — the function name and the finding id disagree in checks.py, and the
        # finding id is what redact.py filters on. api/tests/test_sections.py is
        # what caught that; do not "fix" it by guessing from the function name.
        checks=("ap-group-ssid-limit",),
    ),

    # ── Wired & PoE ──────────────────────────────────────────────────
    Section(
        id="wired.summary",
        label="PoE headline tiles",
        tab="wired",
        hint="Capacity, allocated, drawn, ports up.",
    ),
    Section(
        id="wired.poe-budget",
        label="PoE budget per switch",
        tab="wired",
        paths=("poe.switches",),
        checks=("poe-budget",),
    ),
    Section(
        id="wired.poe-standard",
        label="PoE standard in use",
        tab="wired",
        paths=("poe.byType",),
    ),
    Section(
        id="wired.link-speeds",
        label="Link speeds",
        tab="wired",
        paths=("ports.bySpeed",),
    ),
    Section(
        id="wired.aps-on-ports",
        label="APs on switch ports",
        tab="wired",
        hint="The LLDP join between an AP and the port powering it.",
        paths=("poe.apsOnPoe",),
        checks=("ap-uplink-speed",),
    ),
    Section(
        id="wired.top-poe-draws",
        label="Biggest PoE draws",
        tab="wired",
        paths=("poe.topConsumers",),
    ),
    Section(
        id="wired.port-errors",
        label="Ports counting errors",
        tab="wired",
        hint="Cabling and optics show up here first.",
        paths=("ports.errored",),
        checks=("port-errors",),
    ),
    Section(
        id="wired.vlans",
        label="VLANs seen in this venue",
        tab="wired",
        hint="Where each VLAN is declared against where it appears in traffic.",
        paths=("vlans",),
        checks=("undeclared-vlans",),
    ),

    # ── Addressing ───────────────────────────────────────────────────
    Section(
        id="addressing.ap-subnets",
        label="Where the APs landed",
        tab="addressing",
        paths=("addressing.apSubnets", "addressing.apsWithoutIp"),
        checks=("ap-addressing",),
    ),
    Section(
        id="addressing.external",
        label="How the site looks from outside",
        tab="addressing",
        hint="The public address APs egress through.",
        paths=("addressing.external",),
        checks=("external-ip",),
    ),
    Section(
        id="addressing.switch-subnets",
        label="Switch subnets",
        tab="addressing",
        paths=("addressing.switchSubnets",),
    ),
    Section(
        id="addressing.gateways",
        label="Gateways",
        tab="addressing",
        paths=("addressing.gateways",),
    ),
    Section(
        id="addressing.dns",
        label="DNS servers",
        tab="addressing",
        paths=("addressing.dns",),
    ),
    Section(
        id="addressing.dhcp-pools",
        label="DHCP pools",
        tab="addressing",
        hint="R1-managed pools, with how full each one is.",
        paths=("addressing.dhcpPools",),
        checks=("dhcp-pools",),
    ),

    # ── Identity & Policy ────────────────────────────────────────────
    Section(
        id="identity.dpsk-summary",
        label="DPSK summary",
        tab="identity",
        hint="Pool and passphrase counts. Never passphrases themselves.",
        paths=("dpsk.inUse", "dpsk.dpskSsids"),
        checks=("dpsk-in-use",),
    ),
    Section(
        id="identity.dpsk-pools",
        label="DPSK pools",
        tab="identity",
        hint="One card per pool, with its identity groups and scope.",
        paths=("dpsk.pools",),
        checks=("dpsk-passphrases",),
    ),
    Section(
        id="identity.other-groups",
        label="Other identity groups",
        tab="identity",
        hint="MAC registration and certificate groups on this property.",
        paths=("dpsk.otherIdentityGroups",),
        checks=("dpsk-identity-groups",),
    ),
    Section(
        id="identity.policy-sets",
        label="Adaptive policy sets",
        tab="identity",
        paths=("policy.sets", "policy.inUse"),
        checks=("policy-chain",),
    ),
    Section(
        id="identity.radius",
        label="RADIUS attribute groups",
        tab="identity",
        paths=("policy.radiusGroups",),
        checks=("radius-group-orphans",),
    ),

    # ── Devices ──────────────────────────────────────────────────────
    Section(
        id="devices.aps",
        label="Access point inventory table",
        tab="devices",
        hint="Full per-AP detail, including addressing and serials.",
        paths=("inventory.rows.aps",),
        checks=("ap-naming", "ap-placement"),
    ),
    Section(
        id="devices.switches",
        label="Switch inventory table",
        tab="devices",
        hint="Full per-switch detail, including addressing and serials.",
        paths=("inventory.rows.switches",),
    ),

    # ── Whole report ─────────────────────────────────────────────────
    Section(
        id="report.sources",
        label="What PISR read",
        tab="report",
        hint="The list of read-only R1 endpoints this report came from.",
        paths=("meta.sources",),
    ),
)


BY_ID: Dict[str, Section] = {section.id: section for section in SECTIONS}
IDS: Tuple[str, ...] = tuple(section.id for section in SECTIONS)


def catalogue() -> List[Dict[str, object]]:
    """
    The catalogue as the admin portal wants it: flat, JSON-safe, tab-ordered.

    Served rather than imported into the bundle. The frontend could hold its
    own copy — it already holds the ids — but then adding a section would be a
    rebuild, and the same reasoning that keeps /api/config a runtime call
    applies here.
    """
    order = {tab: index for index, (tab, _) in enumerate(TABS)}
    labels = dict(TABS)
    rows = sorted(SECTIONS, key=lambda s: (order.get(s.tab, 99), s.id))
    return [{
        "id": section.id,
        "label": section.label,
        "tab": section.tab,
        "tabLabel": labels.get(section.tab, section.tab),
        "hint": section.hint,
        # Shown in the portal so an admin can see what hiding actually removes,
        # rather than trusting a label. "Markup only" is a real answer here and
        # worth saying out loud — those sections are hidden but not withheld.
        "paths": list(section.paths),
        "checks": list(section.checks),
    } for section in rows]


def paths_for(hidden_ids) -> Tuple[str, ...]:
    """Every report path owned by a hidden section. Unknown ids are ignored."""
    return tuple(path
                 for section_id in hidden_ids
                 if (section := BY_ID.get(section_id))
                 for path in section.paths)


def checks_for(hidden_ids) -> frozenset:
    """Every finding id owned by a hidden section. Unknown ids are ignored."""
    return frozenset(check
                     for section_id in hidden_ids
                     if (section := BY_ID.get(section_id))
                     for check in section.checks)

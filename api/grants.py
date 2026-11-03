"""
CAPABILITIES: the fixed vocabulary a route names, and what each tool needs.

A CAPABILITY AND A GRANT ARE THE SAME THING. This module holds them, routes
ask for them through `auth.require_grants`, and the portal calls them
capabilities because that is the word that reads clearly to an administrator
composing a job. If the two names ever diverge in meaning, something has gone
wrong here.

READ THIS APART FROM THE OTHER TWO CONTROLS. All three are administered from
the same portal and they all sound like "permissions", and they are not the
same kind of control:

  CAPABILITIES — this module — decide WHICH TOOLS OF PRISM exist for a caller:
  a venue report, the estate roll-up, the identity trace, the admin portals. A
  route names one in code. Fails CLOSED — an unknown id is dropped, and a
  caller whose role names none holds none.

  A ROLE's `visible` list decides WHICH PIECES OF A REPORT they see — a whole
  section, one individual check, one column. `api/roles.py`, enforced in
  `api/redact.py`. An ALLOW list, so it fails closed too.

  SCOPE decides WHICH TENANTS AND VENUES they may poll at all. `api/scope.py`,
  and it is PER PERSON rather than on the role, because otherwise you need
  `tenant_leader_north` and `tenant_leader_south`. Fails CLOSED.

ONE SOURCE, AND IT IS THE ROLE. A person holds exactly one role and the role
names its capabilities. There are no tags, no per-person capability lists and
no second bundle. An earlier design had three ways to hold a capability — a
binary admin/user role, per-account tags, and a thing called a "profile" — and
the arbitrary split between them was what everybody tripped over ("I have the
MSP tag, so why can I not run a batch?"). `auth.grants_of` therefore has
exactly one lookup in it, and that is deliberate.

CAPABILITIES ARE FIXED AND SHIP WITH THE RELEASE; ROLES ARE DATA AN ADMIN
OWNS. A route names a capability, so one an admin could invent would be
enforced by nothing — a permission that silently does nothing, shown in the
portal as though it were real, which is worse than no permission at all.
Adding a capability is a release; adding a job is a Tuesday.

A GRANT IS A LENS, NEVER A KEY. This is the invariant, and it is the one thing
in this file worth defending in review:

    what a caller sees = redact(poll(scope(role) ∩ requested), hidden_for(role))

A capability decides WHICH LENS is available — one venue, an estate roll-up, a
cross-tenant fleet table — and changes not one term on the right. So somebody
holding `msp.rollup` gets the roll-up OF THE ECs THEIR SCOPE ALREADY ALLOWS,
with the checks their role already hides still hidden, and not one row more.
It adds a SHAPE, not a reach.

Which makes every capability-gated route a route that must apply all three
controls itself — scope, redaction, scrub — exactly like `get_report_pdf`
does, and for the same reason: it is an independent path to the same data. Two
of those are easy to forget here, because the routes this landed on were
ADMIN-ONLY first and so never needed them. Specifically, for anything reading
the batch record:

  * The record is TENANT-WIDE, written by an admin whose scope is unrestricted.
    A scoped caller must be given their venues only — INCLUDING THE DENOMINATOR.
    "48 of 58" handed to a user who may reach fourteen venues discloses the
    size of an estate they were not told about, and discloses it as a number,
    which no amount of hiding names would catch.
  * The record holds CHECK IDS. Listing them by catalogue label to a caller
    whose role hides those checks is a way around `redact._filter_findings`.

Filter the record BEFORE `rollup.attach_detail` sees it. A guard in the
template is cosmetic, the same way the `visible()` guards are cosmetic.

If a route ever holds a capability INSTEAD of calling `_require_scope`, that is
the bug this passage exists to prevent.

FEATURES ARE ALL-OF, NEVER ANY-OF. `_FEATURES` below maps a TOOL to the
capabilities it needs, and both the API and the SPA read that one list, so a
control cannot appear for somebody the server will refuse. Nothing is implied
by holding `admin.portal` either: an administrator without `msp.rollup` does
not get the estate views.
"""

from typing import Any, Dict, Tuple

# ── grants: what a route names ───────────────────────────────────────

#: Read the ESTATE roll-up: every tenant at once, summed. The top of the
#: aggregation ladder.
#:
#: Distinct from reading ONE tenant's roll-up below, because they answer
#: different questions for different people. "How is the estate doing" is a
#: management view across companies; "how is this customer doing" is the desk's
#: view of one. Somebody may well need the second and not the first, and on an
#: MSP tenant the ECs are different companies.
#:
#: Scope still decides WHICH tenants are in it — a capability is a lens and
#: never a key.
GRANT_MSP_ROLLUP = "msp.rollup"

#: Read ONE tenant's roll-up: its venues, when each was checked, how it came
#: back. The rung below `msp.rollup`.
GRANT_TENANT_ROLLUP = "tenant.rollup"

#: Sweep MORE THAN ONE tenant in a single operation.
#:
#: Which tenants is the scope's business, as with everything else; this is
#: whether the operation may be started at all. Polling one customer's estate
#: because they asked is a different act from kicking off a poll across every
#: company on the account, and an organisation may well let its desk do the
#: first and not the second.
GRANT_MSP_BATCH = "msp.batch"

#: A cross-tenant fleet view — device up/down across every EC in scope.
GRANT_MSP_FLEET = "msp.fleet"

#: REACH THE ADMIN PORTALS — accounts, the policy, the baselines, the role
#: editor's read side. What used to be the `admin` ROLE, now a capability like
#: any other, because "admin" is a role somebody builds rather than a flag
#: baked into the auth layer.
#:
#: IT IS THE ONE THAT CAN LOCK EVERYBODY OUT. Two things stop that: the built-in
#: `admin` role is compiled into `roles.py` and is what an unreadable policy
#: file falls back to, and `admin_router` refuses any save that would leave
#: nobody holding this.
GRANT_ADMIN_PORTAL = "admin.portal"

#: BUILD ROLES — create, edit and delete them, and hand out any capability
#: including this one. The super-admin tier, and the only capability here that
#: is about administering PRISM rather than reading a customer's network.
#:
#: Kept separate from `admin.portal` because they are different jobs: curating
#: what a report shows is not the same as deciding who may do what. An MSP can
#: let a customer-facing lead administer without letting them mint authority.
#:
#: IT IS THE ONE CAPABILITY THAT CAN GRANT OTHERS, so it is the one worth being
#: careful with. `admin_router` enforces two rules around it and `api/auth.py`
#: has neither: they are calls inside a handler, because both need to compare a
#: SUBMITTED document against the STORED one, which no dependency can see.
#:
#:   1. Editing the role CATALOGUE needs this capability.
#:   2. Nobody may add or remove a capability they do not themselves hold —
#:      without which rule 1 is decorative, since the super role already exists
#:      and an ordinary administrator could simply pick it from the dropdown
#:      for themselves. Enforced in TWO places, because assignment lives on the
#:      account: `admin_router` for the policy document and `accounts_router`
#:      for an account's `role` field. See `auth.may_grant`.
#:
#: Two more rules live with those. The TIER DOES NOT EXIST until somebody holds
#: this — until then every admin may edit roles, the same shape as `scope.py`'s
#: "unrestricted is the default", which also means the tier cannot exist in
#: passphrase mode, where there are no identities. And the LAST-SUPER GUARD
#: refuses a document that still DEFINES a super role while assigning it to
#: nobody; a document defining none is the tier being deliberately turned off,
#: which only a super may do.
GRANT_ADMIN_ROLES = "admin.roles"

#: MANAGE PEOPLE — create accounts, issue enrolment links, assign roles,
#: disable and delete.
#:
#: Separate from `admin.portal` because creating a login is not the same act as
#: curating a report, and an organisation may well want a desk lead who can
#: administer settings without being able to mint access. It is also the
#: capability the HIERARCHY bites on: whoever holds it may only ever hand out a
#: role whose capabilities they hold themselves, so somebody restricted cannot
#: manufacture an unrestricted colleague. See `auth.may_grant`.
GRANT_ADMIN_ACCOUNTS = "admin.accounts"

#: Open a venue report — the thing this tool is for, and the only grant in
#: DEFAULT_GRANTS below. A leader who reads estate roll-ups and never opens a
#: single venue is a real persona, which is why it is a grant at all; everybody
#: else has it without anyone configuring anything.
GRANT_VENUE_REPORT = "venue.report"

#: The AP-group and per-network configuration behind the Config tab's button.
#: A separate grant from the report because it is a separate READ: hundreds of
#: R1 calls on a per-unit MDU, reaching AP-group and per-network settings the
#: report itself never fetches, and it can carry guest-portal secrets. The
#: report's own config CARDS are not this — those are section visibility, which
#: is de-cluttering and already worked.
GRANT_CONFIG_DETAIL = "config.detail"

#: Follow DPSK usernames to policies, networks and APs.
#:
#: ADMIN-ONLY BEFORE THIS GRANT EXISTED, and turning it into one is a DELIBERATE
#: WIDENING rather than a refactor — it names residents' usernames, which
#: `shape._dpsk_safe` refuses to put in a report at all. It stays out of
#: DEFAULT_GRANTS, so nobody holds it until somebody is given a role that names
#: it, in the portal, by name.
GRANT_IDENTITY_TRACE = "identity.trace"

#: Start a batch for ONE tenant and check its venues into the record. The write
#: half, where the two roll-up capabilities are the read half — polling every
#: venue of a live customer network is not the same act as reading what a
#: previous poll recorded.
GRANT_BATCH_RUN = "batch.run"

#: EVERY capability this release defines. The vocabulary, and the reason an
#: admin can be handed the role editor safely: they compose from this list and
#: cannot extend it.
#:
#: `test_access.py` holds every route naming a grant to naming one from here,
#: so a typo in a dependency is a failed test rather than a route nobody can
#: reach.
#: Ordered by the LEVEL each one operates at — across every tenant, across one
#: tenant, within a venue, then administering PRISM itself. That is the ladder
#: an administrator composes a role down, and the portal renders them in this
#: order for the same reason.
ALL_GRANTS: Tuple[str, ...] = (
    # across every tenant
    GRANT_MSP_BATCH,
    GRANT_MSP_ROLLUP,
    GRANT_MSP_FLEET,
    # one tenant
    GRANT_TENANT_ROLLUP,
    GRANT_BATCH_RUN,
    # one venue
    GRANT_VENUE_REPORT,
    GRANT_CONFIG_DETAIL,
    GRANT_IDENTITY_TRACE,
    # PRISM itself
    GRANT_ADMIN_PORTAL,
    GRANT_ADMIN_ACCOUNTS,
    GRANT_ADMIN_ROLES,
)

#: What the built-in `user` role carries — the tool as it behaves before
#: anybody has composed anything.
#:
#: IT EXISTS SO THE APP DOES NOT GO DARK. `venue.report` is the whole point of
#: PRISM, and putting it behind a capability with no default would have meant
#: the release that introduced them locking every existing user out of the only
#: thing they use. It is a capability at all because "a leader who reads estate
#: roll-ups and never opens a venue" is a real persona — and a ROLE REPLACES
#: this default rather than adding to it, which is how that persona is
#: expressed: a role naming only `msp.rollup` does not carry `venue.report`.
#:
#: DELIBERATELY JUST THE ONE. Everything else — config detail, the identity
#: trace, running a batch — has to be named in a role, by a person, in the
#: portal. Widening this list is how a capability system quietly becomes
#: decoration.
DEFAULT_GRANTS: Tuple[str, ...] = (GRANT_VENUE_REPORT,)


#: Prose for each capability, for the role editor. Like `sections._CHECK_LABELS`
#: this is deliberately hand-written rather than de-kebabed: an admin composing
#: a job needs to know what they are handing somebody, and "Msp Rollup" tells
#: them nothing. The HINT is the sentence that stops a well-meaning admin
#: ticking a box they do not understand.
_GRANT_PROSE: Dict[str, Tuple[str, str]] = {
    GRANT_ADMIN_PORTAL: (
        "Administer PRISM",
        "Reach the admin portals: accounts, the report policy, the baselines. "
        "Not the same as building roles — see below."),
    GRANT_ADMIN_ACCOUNTS: (
        "Manage people",
        "Create accounts, issue enrolment links, assign roles, disable and "
        "delete. Bounded: you may only hand out a role whose capabilities you "
        "hold yourself, so a restricted administrator cannot manufacture an "
        "unrestricted colleague."),
    GRANT_ADMIN_ROLES: (
        "Build roles",
        "Create, edit and delete roles, and hand out any capability including "
        "this one. The super-admin tier. Somebody with 'Administer PRISM' but "
        "not this can still ASSIGN the roles that exist, but only ones whose "
        "capabilities they hold themselves."),
    GRANT_VENUE_REPORT: (
        "Open a venue report",
        "Poll one venue and read its report, on screen or as a PDF. Everybody "
        "has this unless their role says otherwise — a role replaces the "
        "default rather than adding to it, so leaving it unticked takes it "
        "away."),
    GRANT_CONFIG_DETAIL: (
        "AP-group and network configuration",
        "The Config tab's deeper read: per-AP-group and per-network settings, "
        "fetched on a button press. Hundreds of RUCKUS ONE calls on a large "
        "site, and it can carry guest-portal secrets."),
    GRANT_IDENTITY_TRACE: (
        "Trace identities",
        "Follow DPSK usernames to their policy, network and APs. It names "
        "RESIDENTS — the one thing a report will not show — so give it to "
        "people who audit identities, not as a convenience."),
    GRANT_BATCH_RUN: (
        "Run batches",
        "Start a batch and check venues into the record. This polls every "
        "venue of a live customer network; reading what a batch recorded is "
        "the separate 'Estate roll-up' grant."),
    GRANT_MSP_BATCH: (
        "Sweep every tenant",
        "Start a batch covering more than one tenant at a time. Which tenants "
        "is the scope's business; this is whether the sweep may be started."),
    GRANT_TENANT_ROLLUP: (
        "One tenant's roll-up",
        "Read which venues of a named customer were checked, when, and how "
        "they came back."),
    GRANT_MSP_ROLLUP: (
        "Estate roll-up",
        "Read which venues of a customer were checked, when, and how they came "
        "back. Narrowed to the customers this person's scope already allows — "
        "it does not widen their reach."),
    GRANT_MSP_FLEET: (
        "Cross-tenant fleet view",
        "Device up/down across every customer in this person's scope. Same "
        "rule: a view of what they could already open one at a time."),
}


def label(grant: str) -> str:
    """The prose name for a grant, falling back to the id."""
    return _GRANT_PROSE.get(grant, (grant, ""))[0]


def hint(grant: str) -> str:
    """One sentence on what holding it means. Empty if none is written."""
    return _GRANT_PROSE.get(grant, ("", ""))[1]


#: What a TOOL needs, as opposed to what a route needs.
#:
#: ALL-OF, NEVER ANY-OF. A feature listing three capabilities is shown only to
#: somebody holding all three, and hidden entirely otherwise — not greyed, not
#: shown-and-refused. A control that appears and then 403s reads as a broken
#: tool rather than as a privilege somebody does not have, and a half-working
#: screen is worse than an absent one.
#:
#: SERVED TO THE FRONTEND so both sides gate on the same list. The bundle could
#: hold its own copy — it already knows the ids — but then the two would drift,
#: and the direction they drift in is a button that exists for people the
#: server will refuse.
_FEATURES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "report":          ("Venue report", (GRANT_VENUE_REPORT,)),
    "config.detail":   ("AP-group and network settings", (GRANT_CONFIG_DETAIL,)),
    "identity.trace":  ("Identity trace", (GRANT_IDENTITY_TRACE,)),
    "batch.view":      ("One tenant's roll-up", (GRANT_TENANT_ROLLUP,)),
    "batch.estate":    ("Estate roll-up", (GRANT_MSP_ROLLUP,)),
    # The worked ALL-OF example: the Run controls live inside the batch
    # dialog, so reaching them means being able to open it AND to start a run.
    "batch.run":       ("Run one tenant", (GRANT_TENANT_ROLLUP, GRANT_BATCH_RUN)),
    "batch.sweep":     ("Sweep every tenant",
                        (GRANT_MSP_ROLLUP, GRANT_MSP_BATCH, GRANT_BATCH_RUN)),
    "fleet":           ("Cross-tenant fleet", (GRANT_MSP_FLEET,)),
    "admin.portal":    ("Admin portals", (GRANT_ADMIN_PORTAL,)),
    "admin.people":    ("People portal", (GRANT_ADMIN_PORTAL,
                                          GRANT_ADMIN_ACCOUNTS)),
    "admin.roles":     ("Role editor", (GRANT_ADMIN_PORTAL, GRANT_ADMIN_ROLES)),
}


def features() -> Tuple[Dict[str, Any], ...]:
    """Every feature with the capabilities it needs. For the SPA and the portal."""
    return tuple({"id": fid, "label": label, "requires": list(needs)}
                 for fid, (label, needs) in sorted(_FEATURES.items()))


def feature_requires(feature_id: str) -> Tuple[str, ...]:
    return _FEATURES.get(feature_id, ("", ()))[1]

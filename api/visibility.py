"""
The policy file: what a non-admin may reach, and what they are shown of it.

TWO HALVES, WITH DIFFERENT RULES. They share this file because they are set
from one portal and changed by one person, but do not let that make them feel
like one thing:

  `hidden`  which report SECTIONS a role is shown. De-cluttering. Fails OPEN —
            see the note below, and api/sections.py.
  `scope`   which MSP-ECs and VENUES a role may report on at all. Access.
            Fails CLOSED once set — see api/scope.py, which owns that logic and
            explains why the two differ.

Everything below about failing open describes the FIRST of those. The scope
block is parsed by `scope.parse`, which has its own rules and its own reasons.

THIS IS THE ONE PLACE PISR WRITES TO DISK, and it is a deliberate exception to
the rule in CLAUDE.md rather than an erosion of it. That rule — no snapshot
files, no cache, no database — is about TENANT DATA: a report is a live poll
and must not be persisted, cached or served from anything but the RUCKUS ONE
API. This file holds a list of section ids. It contains no venue, no device, no
address and no credential, and it would be equally at home in `.env` if
changing it did not need to be a click rather than an SSH session.

  Guard that distinction. If something ever wants to keep a *report* here, that
  is a different feature with a different argument to make, and it does not get
  to make it by pointing at this file.

SHAPE ON DISK, versioned so a later format can be recognised rather than
guessed at:

    {
      "version": 2,
      "hidden": {"user": ["wired.port-errors", "devices.aps"]},
      "scope": {"user": {"unrestricted": false,
                         "ecs": {"<tenantId>": "*",
                                 "<tenantId>": ["<venueId>", "<venueId>"]}}},
      "updatedAt": "2026-08-28T10:04:00Z",
      "updatedBy": "someone@corp.example"
    }

An OLDER version is read, not rejected: a version-1 file simply has no scope
block, which means no restriction, which is exactly what a version-1
deployment had. A NEWER one is refused, because a file written by a later PISR
may mean something by a key this code would silently ignore — and silently
ignoring a key in the scope block is how you serve one customer's venue to
another.

`hidden` is keyed by role and holds ONE list per role — the sections that role
does not see. Not two lists, not a per-section allow/deny pair: two lists can
disagree with each other, and a section added after the policy was written
would be in neither, with no defensible answer for what to do about it. One
deny list gives a new section exactly one behaviour.

FAIL OPEN, ON PURPOSE. A missing file, an unreadable file, malformed JSON, or
an id naming a section that no longer exists all resolve to "hide nothing".
This feature reduces clutter for the ordinary reader; it is not a
confidentiality boundary, and the gate in `auth.py` is what keeps strangers out
of the report entirely. Failing closed here would turn one corrupt file into an
app that renders nothing and explains nothing — a far more likely event than
the one failing closed would protect against. Everything genuinely sensitive is
either behind the gate or, like DPSK passphrases, never fetched at all
(`shape._dpsk_safe`).

  If a section ever needs to be withheld rather than tidied away, it does not
  belong in this file. It needs its own check at the route, and the argument
  for failing closed made once, explicitly.
"""

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import grants as grant_rules
import resource_groups as group_rules
import roles as role_rules
import scope as scope_rules
import sections as section_catalogue
from config import AUTH

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2

# The only role that can have anything hidden from it. Admins see everything by
# construction — there is no policy entry that could hide a section from the
# person who edits the policy, which removes the "locked myself out" failure
# entirely. A third role would be a new key here and a new column in the portal.
#: RETIRED. Before roles existed this was the one role a policy could hide
#: things from. The list a role hides now lives ON the role, so there is no
#: fixed set of "manageable" ones — an admin creates as many as they need.
#:
#: Kept as a name only because `PEOPLE_KEY`'s assertion below reads better
#: against it, and because a stored `hidden: {"user": [...]}` block is still
#: read forward onto the role of that id. See `_migrate_hidden`.
MANAGED_ROLES: Tuple[str, ...] = ("user",)

# Where PER-PERSON policy lives: a top-level map of identity -> {role,
# scope}. R1 shipped it as `scope.people` (scope only); both are read, and the
# top-level one is written. See `_clean_people`.
#
# ONE PLACE FOR "WHAT IS DANA'S POLICY", deliberately. Her role and her
# scope are two answers to one question an admin asks in one sitting, and
# splitting them across two blocks means two places to look and two places to
# forget when somebody leaves.
PEOPLE_BLOCK = "people"

# The legacy nesting of the same map, inside `scope`, beside the role keys.
#
# NESTED UNDER A RESERVED KEY RATHER THAN IN A NEW TOP-LEVEL SHAPE, and that is
# a rollback decision rather than an aesthetic one. Restructuring `scope` into
# `{"roles": …, "people": …}` would need FORMAT_VERSION bumped, and a PISR one
# version older refuses a file it does not understand OUTRIGHT — which for
# scope means every restriction silently vanishing on a rollback, the one
# direction this control must never fail in. A key that is simply not a
# managed role is invisible to an older reader: it keeps every role
# restriction it already understood and merely does not see the per-person
# ones. Worse than the current binary, better than unrestricted.
#
# The obvious hazard is a future role actually called "people", which would
# make one person's scope look like a role's. `test_sections.py` would not
# catch it and nor would anything else, so it is asserted below.
PEOPLE_KEY = "people"
assert PEOPLE_KEY not in MANAGED_ROLES, (
    f"{PEOPLE_KEY!r} is the reserved per-person scope key and cannot also be "
    "a managed role — see visibility.PEOPLE_KEY")

# Section ids that have been renamed, old -> new.
#
# A stored policy names sections by id, so renaming one in `sections.py` would
# otherwise make every mention of it unknown — and `_clean` drops unknown ids,
# which means a section an admin had HIDDEN silently becomes visible. That is
# the wrong direction to fail in for a rename, and it fails at the next report
# rather than at the rename, so nobody connects the two.
#
# Entries stay until the next save rewrites the file with the new ids; leaving
# one here forever costs a dictionary lookup. An id that was DELETED rather
# than renamed does not belong here — it should drop, and it does.
RENAMED: Dict[str, str] = {
    # The "Wired & PoE" tab became separate Wired and PoE tabs. Ids are
    # <tab>.<thing>, so the sections that moved to PoE had to be renamed.
    #
    # ONLY IDS THAT ACTUALLY SHIPPED belong here — these are the ones deployed
    # in the first role-policy release. Ids that existed only between two
    # uncommitted edits were never in anybody's policy file, and adding them
    # would be dead weight at best. At worst it is a bug: a key here that is
    # ALSO a live section id would migrate an admin's deliberate choice into
    # something else on every load, permanently. test_renames_point_at_real
    # _sections asserts that cannot happen.
    "wired.summary": "poe.summary",
    "wired.poe-budget": "poe.budget",
    "wired.poe-standard": "poe.standard",
    "wired.aps-on-ports": "poe.aps-on-ports",
    # The venue-configuration card moved off Overview onto the Config tab.
    "overview.venue-config": "config.venue-summary",
    # "wired.link-speeds" and "wired.port-errors" are absent because they kept
    # their ids: they briefly moved to the PoE tab and moved back, port health
    # being a wired question rather than a power one.
    #
    # "wired.top-poe-draws" is absent for the other reason — that card was
    # removed, so a policy hiding it should simply forget it.
}


class PolicyStore:
    """
    Reads and writes one small JSON file.

    Cached against the file's mtime and size rather than re-read per request:
    a report is one read, so the saving is small, but the cache also means a
    file that has gone unreadable keeps serving the last good policy instead of
    silently reverting to "show everything" mid-session.
    """

    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._cache: Dict[str, List[str]] = {}
        self._scope: Dict[str, Dict[str, Any]] = {}
        # INITIALISED HERE AND NOT ONLY IN `_load`, because `_load` returns
        # early when there is no path at all — the unconfigured deployment —
        # and every reader would then hit an AttributeError rather than the
        # built-in roles. That is the one case where falling back has to work
        # by construction, since it is also what an unreadable file lands on.
        self._roles: Dict[str, Any] = {}
        #: Stored resource groups, WITHOUT the compiled-in built-in. Emptiness
        #: is load-bearing: see `groups_in_force`.
        self._groups: Dict[str, Any] = {}
        self._people: Dict[str, Dict[str, Any]] = {}
        self._meta: Dict[str, Optional[str]] = {"updatedAt": None, "updatedBy": None}
        self._stamp: Optional[Tuple[float, int]] = None
        self._loaded = False

    # ── state ────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def writable(self) -> bool:
        """
        Can an admin actually save from the portal?

        Answered by testing the DIRECTORY, not the file: the file legitimately
        does not exist until the first save, and a read-only mount is the
        failure this is really looking for — someone who ran the container
        without the volume, whose portal would otherwise accept a change and
        lose it at the next deploy.
        """
        if not self.path:
            return False
        try:
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    # ── reading ──────────────────────────────────────────────────────

    def _stat(self) -> Optional[Tuple[float, int]]:
        try:
            info = self.path.stat()
            return (info.st_mtime, info.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        """Re-read if the file changed. Any failure leaves the cache alone."""
        if not self.path:
            self._loaded = True
            return

        stamp = self._stat()
        if self._loaded and stamp == self._stamp:
            return

        if stamp is None:
            # No file yet — the normal state of a fresh deployment, not an
            # error. Nothing is hidden until an admin hides something.
            self._cache = {}
            self._scope = {}
            self._roles = {}
            self._groups = {}
            self._people = {}
            self._meta = {"updatedAt": None, "updatedBy": None}
            self._stamp = None
            self._loaded = True
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(
                "visibility: could not read %s (%s). Keeping the last known "
                "policy; nothing new is hidden. Fix or delete the file.",
                self.path, exc)
            self._stamp = stamp   # do not re-read a file we know is broken
            self._loaded = True
            return

        version = raw.get("version")
        if not isinstance(version, int) or version > FORMAT_VERSION:
            logger.error(
                "visibility: %s declares version %r, which this PISR does not "
                "understand (it writes version %d). Ignoring the file rather "
                "than acting on half of it — a scope restriction read with a "
                "key missing is worse than no restriction at all.",
                self.path, version, FORMAT_VERSION)
            self._stamp = stamp
            self._loaded = True
            return
        if version < FORMAT_VERSION:
            # Read forward, do not rewrite. The next save from the portal
            # writes the current version; until then an older file is simply an
            # older file, and rewriting one on read would mean a container
            # restart silently modifies a mounted volume.
            logger.info("visibility: %s is version %d; reading it as one with "
                        "no scope restriction.", self.path, version)

        self._cache = self._clean(raw.get("hidden"))
        self._scope = self._clean_scope(raw.get("scope"))
        self._roles = self._migrate_hidden(
            role_rules.clean(raw.get("roles")), self._cache)
        self._groups = group_rules.clean(raw.get("resourceGroups"))
        self._people = self._clean_people(raw, role_rules.resolve(self._roles))
        self._meta = {"updatedAt": raw.get("updatedAt"),
                      "updatedBy": raw.get("updatedBy")}
        self._stamp = stamp
        self._loaded = True

    @staticmethod
    def _clean(hidden) -> Dict[str, List[str]]:
        """
        Keep only known roles and known ELEMENT ids — a section, a check, or a
        column. The `hidden` list holds all three kinds; `is_known_id` accepts
        any of them.

        Unknown ids are dropped here rather than at render time so that the
        portal shows an admin the policy that is actually in force, not the one
        they wrote before something was renamed.
        """
        if not isinstance(hidden, dict):
            return {}
        cleaned: Dict[str, List[str]] = {}
        for role, wanted in hidden.items():
            if not isinstance(role, str) or not isinstance(wanted, list):
                continue
            migrated = {RENAMED.get(sid, sid) for sid in wanted if isinstance(sid, str)}
            renamed = sum(1 for sid in wanted
                          if isinstance(sid, str) and sid in RENAMED)
            if renamed:
                logger.info(
                    "visibility: migrated %d renamed section id(s) for role %r. "
                    "The next save from the portal writes the new ids.",
                    renamed, role)
            known = sorted({sid for sid in migrated
                            if section_catalogue.is_known_id(sid)})
            dropped = len(migrated) - len(known)
            if dropped > 0:
                logger.warning(
                    "visibility: dropped %d unknown element id(s) for role %r. "
                    "They were probably renamed; re-save from the portal to "
                    "tidy the file.", dropped, role)
            if known:
                cleaned[role] = known
        return cleaned

    @staticmethod
    def _migrate_hidden(stored: Dict[str, Any],
                        legacy: Dict[str, List[str]]) -> Dict[str, Any]:
        """
        A pre-roles `hidden` block becomes the matching role's hidden list.

        Before roles existed, the policy held `hidden: {"user": [...]}` — a
        list keyed by the old binary role. Those ids are somebody's deliberate
        curation, often dozens of them, and a release that quietly stopped
        applying them would show every user forty cards an admin had decided
        they should not see. So they are READ FORWARD onto the role of the same
        id, which for the ids that existed ("admin", "user") is exactly the
        built-in they now name.

        INVERTED ON THE WAY, because roles hold an ALLOW list now. A stored
        `hidden` list of forty-three ids becomes a `visible` list of the other
        seventy, so whoever holds that role sees exactly what they saw before
        the model changed. Migrating it as a deny list would have been simpler
        and would have shown them nothing.

        Only when that role has no stored override of its own. An admin who has
        edited the role in the new portal has said something more recent, and
        the old block must not overwrite it.
        """
        if not legacy:
            return stored
        every = frozenset(section_catalogue.ALL_ELEMENT_IDS)
        migrated = dict(stored)
        for role_id, ids in legacy.items():
            if role_id in migrated or not ids:
                continue
            base = role_rules.builtins().get(role_id)
            allowed = sorted(every - {i for i in ids if isinstance(i, str)})
            migrated[role_id] = role_rules.clean_one({
                "id": role_id,
                "label": base.label if base else role_id,
                "grants": list(base.grants) if base else [],
                "visible": allowed,
            })
            logger.info(
                "visibility: inverted %d hidden element(s) from the pre-roles "
                "policy into a %d-element allow list on the %r role. The next "
                "save writes it there.", len(ids), len(allowed), role_id)
        return {k: v for k, v in migrated.items() if v is not None}

    @staticmethod
    def _clean_scope(raw, known_roles=None) -> Dict[str, Dict[str, Any]]:
        """
        Keep only managed roles' scope blocks, each validated by `scope.clean`.

        A role whose block says "no restriction" is dropped rather than stored
        as an explicit unrestricted marker, so the file has exactly one way to
        say it and `scope_for` has exactly one thing to test.
        """
        if not isinstance(raw, dict):
            return {}
        cleaned: Dict[str, Dict[str, Any]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or key == PEOPLE_KEY:
                continue
            # Keyed by ROLE ID now. An id for a role that no longer exists is
            # kept rather than dropped: the restriction was deliberate, and
            # dropping it would widen whoever holds that role if it comes back.
            block = scope_rules.clean(value)
            if block is not None:
                cleaned[key] = block
        return cleaned

    @staticmethod
    def _clean_people(raw, known_roles) -> Dict[str, Dict[str, Any]]:
        """
        Per-person policy — role and scope — keyed by the identity PISR
        knows somebody by.

        That is the username in accounts mode and the verified address in proxy
        mode. PASSPHRASE MODE HAS NO IDENTITY AT ALL — a shared secret proves
        somebody knew it, never who — so nothing here can ever match there and
        every caller falls back to their role's policy. That is not a gap to
        fix; it is what a shared secret means.

        CASE-FOLDED ON THE WAY IN, for the reason `_role_for_identity` folds:
        an identity provider's idea of an address's case is not stable, and
        losing a restriction to a capital letter fails in the wrong direction.

        CARRIES THREE THINGS NOW: the role, the resource groups, and the
        legacy scope map. The last of those is the COMPATIBILITY SHADOW once
        groups are in force — written so that a rollback to a PISR that has
        never heard of groups still enforces the last resolved territory
        rather than reading an absent scope as "unrestricted", which for the
        fail-closed half is the one direction it must never fail in. It is
        write-only from that point: `scope_for` reads the groups.

        READS TWO SHAPES AND WRITES ONE. The top-level `people` map is current;
        `scope.people` is where R1 put the scope half before roles existed.
        Both are merged, top-level winning, so a file written by the previous
        release keeps working and the next save tidies it. An entry with
        neither a role nor a scope is dropped, so the file has one way to
        say "nothing special about this person".

        A ROLE ID THAT NO LONGER EXISTS IS KEPT, NOT DROPPED. Dropping it
        would silently return that person to the role default — more visible
        and more reachable than the job they were assigned — which is the
        wrong direction for the half of a role that fails closed. It is
        surfaced to the admin instead: `orphaned_assignments` below, and a
        warning in the portal.
        """
        if not isinstance(raw, dict):
            return {}

        merged: Dict[str, Dict[str, Any]] = {}

        def note(identity: Any, role_id: Any, block: Any, groups: Any = None) -> None:
            if not isinstance(identity, str) or not identity.strip():
                return
            key = identity.strip().casefold()
            entry = merged.setdefault(key, {})
            if isinstance(role_id, str) and role_id:
                entry["role"] = role_id
            parsed = scope_rules.clean(block)
            if parsed is not None:
                entry["scope"] = parsed
            # AN EMPTY LIST IS NOT THE SAME AS NO LIST, and this is the one
            # place that distinction is made. `[]` is an admin saying "this
            # person reaches nothing" and must survive a round trip; absent
            # means they have not been given a territory yet. Both fail
            # closed once groups are in force, but only one of them is a
            # decision somebody made, and the portal says so differently.
            if isinstance(groups, (list, tuple)):
                entry["groups"] = group_rules.clean_assignment(groups)

        legacy = (raw.get("scope") or {}) if isinstance(raw.get("scope"), dict) else {}
        for identity, block in (legacy.get(PEOPLE_KEY) or {}).items():
            note(identity, None, block)

        current = raw.get(PEOPLE_BLOCK)
        if isinstance(current, dict):
            for identity, entry in current.items():
                if not isinstance(entry, dict):
                    continue
                note(identity, entry.get("role"), entry.get("scope"),
                     entry.get("groups"))

        if known_roles is not None:
            missing = sorted({e["role"] for e in merged.values()
                              if e.get("role")
                              and e["role"] not in known_roles})
            if missing:
                logger.warning(
                    "visibility: %d assignment(s) name role(s) that no "
                    "longer exist (%s). Those people are left WITHOUT the "
                    "grants that role carried rather than silently "
                    "returned to the role default; fix it in the portal.",
                    len(missing), ", ".join(missing))

        return {k: v for k, v in merged.items() if v}

    @staticmethod
    def _shadow_scope(people: Dict[str, Dict[str, Any]],
                      groups: Dict[str, Any]) -> None:
        """
        Write each person's RESOLVED territory into the legacy `scope` field.

        A ROLLBACK GUARD, and the reason groups did not need `FORMAT_VERSION`
        bumped. A PISR one version older refuses a file whose version it does
        not understand OUTRIGHT, which for scope means every restriction
        vanishing at once — the single direction this control must never fail
        in. So the groups go in under a key an older reader simply does not
        see, and the territory they resolve to is ALSO written where that
        older reader already looks.

        What a rolled-back PISR then enforces is the last resolved snapshot:
        correct for a group naming ids, and NEVER WIDER than the live answer,
        because a group cannot resolve to more than the ids it names. The one
        case that loses precision is `all-access`, which resolves to "no
        restriction" — the same thing the older PISR would have done for
        somebody with no entry, so nothing is given away.

        WRITE-ONLY FROM HERE. `scope_for` reads the GROUPS once they are in
        force and never this field, so the two cannot disagree about who is
        in charge. Mutates in place, like the rest of the save path.
        """
        if not groups:
            # Groups are not in force, so `scope` is still the real setting
            # and must not be overwritten by a resolution of nothing.
            return
        known = group_rules.resolve(groups)
        for entry in people.values():
            assigned = entry.get("groups")
            if assigned is None:
                # A person carried forward from before groups: their own scope
                # entry is left exactly as it was. They are unplaced under the
                # new model and `scope_for` says so; silently materialising a
                # territory for them would be this file guessing.
                continue
            resolved = group_rules.union(known.get(g) for g in assigned)
            block = scope_rules.clean(resolved.as_json())
            if block is None:
                entry.pop("scope", None)
            else:
                entry["scope"] = block

    def people_scoped(self) -> Tuple[str, ...]:
        """Every identity with an entry of its own, for the portal to draw."""
        with self._lock:
            self._load()
            return tuple(sorted(self._people))

    def roles(self) -> Tuple[Any, ...]:
        """
        Every role in force: the compiled-in ones, overridden by any stored
        role of the same id, plus every role an admin has created.
        """
        return tuple(self.role_map().values())

    def role_map(self) -> Dict[str, Any]:
        """
        The same, keyed by id. EVERY LOOKUP GOES THROUGH HERE.

        `role_rules.resolve` is what makes an unreadable policy file survivable
        now that `admin.portal` is a capability rather than a flag: the
        built-ins are compiled in, so a file that cannot be read leaves
        `admin` and `user` still resolving instead of leaving nobody able to
        administer anything.
        """
        with self._lock:
            self._load()
            return role_rules.resolve(self._roles)

    def role_for(self, identity: Optional[str], role_id: Optional[str] = None):
        """
        The role in force for this caller, resolved to a `roles.Role`.

        WHERE THE ASSIGNMENT COMES FROM DEPENDS ON THE MODE, and it is one
        place per mode rather than two places arguing:

          ACCOUNTS mode — the account record's `role` field, passed in as
          `role_id`. It was already "admin" or "user" before roles existed, so
          collapsing the old binary into a role id migrated nobody. It is also
          what `_account_key` signs, so reassigning somebody ends their
          sessions rather than waiting for them to expire.

          PASSPHRASE mode — which of the two secrets signed the cookie, again
          passed in as `role_id`. That mode has no identity, so this is the
          only thing there is to key on.

          PROXY mode — the policy's per-person map, looked up by the verified
          identity, because there is no account record to carry it.

        An id naming a role that does not exist resolves to None, and the
        caller treats that as conferring nothing — see `grants_of`. It is NOT
        quietly downgraded to the built-in `user`: somebody was deliberately
        given a job, and losing its definition must not promote them back to
        what everybody else gets.
        """
        resolved = self.role_map()
        if role_id:
            return resolved.get(role_id)
        if not identity:
            return None
        with self._lock:
            self._load()
            entry = self._people.get(identity.strip().casefold()) or {}
        assigned = entry.get("role")
        if not assigned:
            # Nobody named a role for them: the tool as it behaves before
            # anyone configures anything.
            return resolved.get(role_rules.BUILTIN_USER)
        return resolved.get(assigned)

    def ungranted_elements(self) -> Tuple[str, ...]:
        """
        Elements no role allows — nobody in this deployment can see them.

        THE PRICE OF DENY-BY-DEFAULT, PAID IN THE OPEN. An element added in a
        release is in nobody's allow list, so it is withheld; that is correct,
        and it is also exactly how a new feature silently fails to appear. This
        is what turns "missing" into "not granted yet", which is a thing an
        administrator can act on.

        The built-in `admin` role's list is computed, so in practice this is
        only ever non-empty if somebody has overridden that role — which is
        worth surfacing for the same reason.
        """
        with self._lock:
            self._load()
            return self._ungranted()

    def _ungranted(self) -> Tuple[str, ...]:
        """
        The same, WITHOUT taking the lock.

        `self._lock` is a plain `threading.Lock` and is therefore NOT
        reentrant, so a method that holds it cannot call one that takes it —
        `policy()` calling the public version above deadlocked every request
        that touched the policy. Every helper here follows the same split: the
        public one locks and loads, the underscore one assumes both.
        """
        allowed: set = set()
        for role in role_rules.resolve(self._roles).values():
            allowed.update(role.visible)
        return tuple(sorted(section_catalogue.ALL_ELEMENT_IDS - allowed))

    def orphaned_assignments(self) -> Dict[str, str]:
        """identity -> the role id it names, for ids that no longer exist."""
        with self._lock:
            self._load()
            return {identity: entry["role"]
                    for identity, entry in self._people.items()
                    if entry.get("role")
                    and entry["role"] not in role_rules.resolve(self._roles)}

    def holders_of(self, grant: str) -> Tuple[str, ...]:
        """
        Every identity whose ROLE confers `grant`.

        BOTH PLACES A ROLE CAN BE ASSIGNED, because there are two. In accounts
        mode the assignment is a field on the ACCOUNT; the policy's per-person
        map carries it for the modes with no account store. Reading only the
        policy meant the super tier never registered as claimed in the mode
        everybody actually uses — so the bootstrap rule stayed open and every
        administrator kept role authority forever, which is exactly the tier
        not existing.
        """
        with self._lock:
            self._load()
            resolved = role_rules.resolve(self._roles)
            held = {identity for identity, entry in self._people.items()
                    if entry.get("role")
                    and grant in role_rules.grants_of(
                        resolved.get(entry["role"]))}

        if AUTH.mode == "accounts":
            try:
                import accounts
                held.update(
                    a.username for a in accounts.STORE.list()
                    if a.can_sign_in
                    and grant in role_rules.grants_of(resolved.get(a.role)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("visibility: could not count %s holders: %s",
                               grant, exc)
        return tuple(sorted(held))

    def grant_is_claimed(self, grant: str) -> bool:
        """
        Whether anybody holds `grant`, i.e. whether that tier EXISTS yet.

        THE BOOTSTRAP RULE LIVES ON THIS, and it is the same shape as
        `scope.py`'s "unrestricted is the default": a control that nobody has
        configured must not lock everyone out. Until some role confers
        `admin.roles` and somebody is assigned it, every admin may edit
        roles — otherwise a fresh instance could never make its first one.
        The moment a super exists, ordinary admins are shut out.

        Note that in PASSPHRASE MODE this is always False and always will be:
        roles are keyed by identity and that mode has none, so no super tier
        can exist there. That is correct rather than a gap — a mode that cannot
        tell two people apart cannot meaningfully put one above the other.
        """
        return bool(self.holders_of(grant))

    def grants_for(self, identity: Optional[str],
                   role_id: Optional[str] = None) -> frozenset:
        """
        What this person's ROLE confers, and that is the whole of it. There
        is no second source to union in — see `api/grants.py`.

        NO ROLE MEANS THE DEFAULTS, not nothing. `grants.DEFAULT_GRANTS` is
        the tool as it behaves before anybody has configured anything, and it
        holds `venue.report` so that the release introducing roles does not
        take the only thing most people use away from all of them at once.

        A ROLE REPLACES THAT DEFAULT rather than adding to it — the same
        rule `hidden_for` and `scope_for` follow, and the way the "leader who
        never opens a venue" persona is expressed: a role naming only
        `msp.rollup` does not carry `venue.report`, so they do not get one.
        It is also the footgun in this design, so the portal seeds a new
        role with the defaults already ticked.

        Still fails CLOSED where it matters: an unknown grant id is dropped,
        and a DELETED role confers nothing at all — not the defaults, since
        the person was deliberately given a job and losing its definition must
        not silently promote them back to what everyone else gets.
        """
        return role_rules.grants_of(self.role_for(identity, role_id))

    @property
    def groups_in_force(self) -> bool:
        """
        Whether RESOURCE GROUPS decide scope on this deployment.

        THE BOOTSTRAP RULE, and the same shape as `admin_router`'s super tier
        ("the tier does not exist until somebody holds it") and `scope.py`'s
        ("unrestricted is the default"). Groups fail CLOSED — no group means
        no venues — which is a reversal of what every existing deployment
        does, so switching it on cannot be something an upgrade does to
        somebody overnight. It is keyed on whether a group CATALOGUE has been
        saved: until one has, scope resolves the way it always did.

        That also means turning it off is possible and is a deliberate act:
        delete every group and the deployment reverts. `admin_router` refuses
        to delete the last one while anybody is still assigned a group, for
        the same reason the last-super guard exists — a document that turns
        the control off by accident WIDENS everybody, silently.

        The compiled-in `all-access` is deliberately not counted: it always
        resolves, so counting it would mean groups were in force on every
        deployment from the moment this shipped.
        """
        with self._lock:
            self._load()
            return bool(self._groups)

    def groups_for(self, identity: Optional[str]) -> Tuple[str, ...]:
        """The group ids assigned to this person, for the portal."""
        if not identity:
            return ()
        with self._lock:
            self._load()
            entry = self._people.get(identity.strip().casefold()) or {}
            return tuple(entry.get("groups") or ())

    def scope_for(self, identity: Optional[str], role_id: Optional[str] = None):
        """
        Which customers and venues this caller may reach.

        TWO MODELS LIVE HERE, and which one answers depends on
        `groups_in_force`. They are not blended — a caller gets one answer
        from one of them, because a union of the two would mean an admin
        turning groups on could not take anything away.

        RESOURCE GROUPS (once a catalogue exists). The person's assigned
        groups, UNIONED — see `resource_groups.union` for why union is right
        here and wrong between a person and a role. **No group means NO
        VENUES**, which is the deliberate reversal the whole model rests on:
        reach is granted by assignment, so somebody nobody has placed reaches
        nothing rather than everything. The role is not consulted at all; a
        role says what somebody may RUN, never whose network they may run it
        on, and the two were separated on purpose.

        LEGACY (before any group is saved). Person's own entry, else their
        role's default, else unrestricted — what every deployment does today.
        A person's entry REPLACES their role's rather than adding to it, so
        "what can Dana reach" has one answer; under a union rule, naming her
        ECs would have handed her hers PLUS everything her role could already
        reach, which is the opposite of what an admin typing a territory into
        a box means.

        PASSPHRASE MODE NEVER FAILS CLOSED HERE, and that is not a hole. That
        mode has no identities — a shared secret proves somebody knew it,
        never who — so a per-person assignment can never match, and applying
        the fail-closed rule would mean PISR's DEFAULT auth mode shows every
        user nothing, permanently, with no screen that could fix it. Groups
        need somebody to assign them to; where there is nobody, the legacy
        answer stands. Same reasoning as the super tier, which cannot exist
        there either.

        THERE IS NO ADMIN SHORT-CIRCUIT in either model. An administrator with
        no group reaches no venue — and can still reach `/api/admin`, which is
        gated on the role and never on scope, so the screen that fixes it is
        always open. `scope.spec.ts` has a test for exactly that.
        """
        with self._lock:
            self._load()
            folded = identity.strip().casefold() if identity else None
            entry = (self._people.get(folded) or {}) if folded else {}

            if self._groups and folded:
                known = group_rules.resolve(self._groups)
                assigned = entry.get("groups")
                if assigned is None:
                    # Never placed. Under the group model that is nothing, and
                    # it is logged because it presents to the person as "I can
                    # see no venues" with nothing on screen explaining why.
                    logger.info(
                        "scope: %s holds no resource group, so no venue is "
                        "reachable. Assign one in the portal.", folded)
                    return scope_rules.Scope(unrestricted=False, ecs={})
                return group_rules.union(known.get(g) for g in assigned)

            if entry.get("scope") is not None:
                return scope_rules.parse(entry["scope"])
            resolved_id = role_id or entry.get("role")
            return scope_rules.parse(self._scope.get(resolved_id or ""))

    def hidden_for(self, identity: Optional[str],
                   role_id: Optional[str] = None) -> Tuple[str, ...]:
        """
        Element ids this caller does not see — from their ROLE, and nowhere
        else.

        THE FAIL DIRECTION IS THE OPPOSITE OF `scope_for`'s, and that is why
        these stay two methods rather than one lookup returning a bundle.
        Hiding is de-cluttering: an unknown element id, a role that no longer
        exists, an unreadable file all resolve to HIDING NOTHING, because an
        app that renders nothing and explains nothing is worse than a card
        somebody did not need. Scope is access and resolves the other way. They
        live in one record now; do not let a refactor make them agree.

        There is no admin short-circuit any more and there does not need to be:
        the built-in `admin` role hides nothing, and it is compiled in, so the
        old "an admin must be able to fix a broken policy" guarantee now comes
        from `roles.resolve` rather than from a special case here.
        """
        return role_rules.hidden_of(self.role_for(identity, role_id))

    def policy(self) -> Dict[str, object]:
        """The whole policy, for the admin portal."""
        with self._lock:
            self._load()
            return {
                "version": FORMAT_VERSION,
                # NOT EMITTED ANY MORE. Visibility lives on the role, as an
                # allow list, and there is nothing else for a portal to edit.
                # The stored block is still READ — once, to migrate — and
                # `_migrate_hidden` is the only thing that looks at it.
                "scope": {role: scope_rules.parse(block).as_json()
                          for role, block in self._scope.items()},
                "roles": role_rules.catalogue(role_rules.resolve(self._roles).values()),
                # THE SECOND AXIS. Emitted RESOLVED, so the compiled-in
                # `all-access` is in the list the portal draws and an admin
                # can see the thing an unreadable file falls back to.
                "resourceGroups": group_rules.catalogue(
                    group_rules.resolve(self._groups).values()),
                "undeletableGroups": list(group_rules.undeletable()),
                # Whether groups DECIDE scope here yet. The portal has to say
                # which of two very different rules is in force, because
                # "no group" means everything under one and nothing under the
                # other. See `groups_in_force`.
                "groupsInForce": bool(self._groups),
                PEOPLE_BLOCK: {
                    identity: {
                        "role": entry.get("role"),
                        "groups": list(entry["groups"]) if entry.get("groups") is not None
                                  else None,
                        "scope": (scope_rules.parse(entry["scope"]).as_json()
                                  if entry.get("scope") is not None else None),
                    }
                    for identity, entry in sorted(self._people.items())
                },
                # Assignments naming a role that no longer exists. Surfaced
                # rather than swept up, because those people silently hold no
                # grants until somebody notices.
                "ungrantedElements": list(self._ungranted()),
                "orphanedAssignments": {
                    identity: entry["role"]
                    for identity, entry in self._people.items()
                    if entry.get("role") and entry["role"] not in role_rules.resolve(self._roles)
                },
                # Group ids somebody still names that no longer resolve. Kept
                # rather than dropped (see `clean_assignment`) and surfaced
                # here, because they confer nothing and the person simply
                # stops being able to open things with no visible cause.
                "orphanedGroups": {
                    identity: [g for g in (entry.get("groups") or ())
                               if g not in group_rules.resolve(self._groups)]
                    for identity, entry in self._people.items()
                    if any(g not in group_rules.resolve(self._groups)
                           for g in (entry.get("groups") or ()))
                },
                "updatedAt": self._meta.get("updatedAt"),
                "updatedBy": self._meta.get("updatedBy"),
                "writable": self.writable,
                "path": str(self.path) if self.path else None,
            }

    # ── writing ──────────────────────────────────────────────────────

    def save(self, hidden: Dict[str, List[str]], scope: Optional[Dict[str, Any]],
             actor: Optional[str],
             roles: Optional[Any] = None,
             people: Optional[Any] = None,
             resource_groups: Optional[Any] = None) -> Dict[str, object]:
        """
        Replace the policy. Raises RuntimeError if there is nowhere to write.

        Written to a temporary file in the SAME directory and then renamed:
        `os.replace` is atomic within a filesystem, so a reader either sees the
        old policy or the new one and never a half-written file. A tempfile in
        /tmp would not be on the same filesystem as a mounted volume and the
        rename would fall back to a copy, which is exactly the non-atomic write
        this is avoiding.
        """
        if not self.path:
            raise RuntimeError(
                "PISR_VISIBILITY_FILE is not set, so there is nowhere to save a "
                "policy. Set it and mount a writable volume at its directory.")

        # OMITTED MEANS "LEAVE ALONE", NOT "DELETE". The document is replaced
        # whole, but a caller that never mentions a block did not ask for it to
        # go — and the blocks are edited from DIFFERENT SCREENS now that roles
        # have their own page. Reading an absent `roles` as an empty one meant
        # saving the Sections dialog silently deleted every role an
        # administrator had built, which is the worst kind of data loss:
        # invisible, immediate, and caused by an unrelated action.
        #
        # An explicit empty map still clears — that is the role editor deleting
        # the last one, and it says so by sending it.
        with self._lock:
            self._load()
            if hidden is None:
                hidden = dict(self._cache)
            if scope is None and self._scope:
                scope = dict(self._scope)
            if roles is None:
                roles = {rid: r.public() for rid, r in self._roles.items()}
            if people is None:
                people = dict(self._people)
            if resource_groups is None:
                resource_groups = {gid: g.public()
                                   for gid, g in self._groups.items()}

        cleaned = self._clean(hidden)
        cleaned_scope = self._clean_scope(scope)
        # The SAME migration `_load` applies, and it has to be here too: the
        # cache is refreshed in place below rather than re-read, so a step this
        # skipped would be correct on disk and wrong in memory until the
        # container restarted. `_people` was exactly that bug once already.
        cleaned_roles = self._migrate_hidden(role_rules.clean(roles), cleaned)
        # Both shapes are accepted on the way in — a caller may still send the
        # per-person scope nested under `scope`, which is where R1 put it — and
        # only the top-level one is written. `_clean_people` does the merge.
        # Built-ins are NOT written back: `resolve` compiles `all-access` in,
        # so a portal echoing the catalogue would otherwise persist a copy of
        # it and the compiled-in one would stop being the thing that answers.
        # Same rule the roles block follows for `admin` and `user` — except
        # there an override IS meaningful, so only an UNCHANGED built-in is
        # dropped here.
        cleaned_groups = {
            gid: g for gid, g in group_rules.clean(resource_groups).items()
            if g != group_rules.builtins().get(gid)
        }
        cleaned_people = self._clean_people(
            {"scope": scope, PEOPLE_BLOCK: people},
            role_rules.resolve(cleaned_roles))
        self._shadow_scope(cleaned_people, cleaned_groups)
        payload = {
            "version": FORMAT_VERSION,
            # The pre-roles `hidden` block is NOT written back. It is read on
            # load, inverted onto the role of the same id, and then it has done
            # its job — writing it again would leave two descriptions of one
            # thing, and the next reader would have to work out which wins.
            # Each block omitted entirely when empty, so an unrestricted
            # deployment's file stays readable at a glance and a version-1 file
            # round-trips to something that looks the same.
            **({"scope": cleaned_scope} if cleaned_scope else {}),
            **({"roles": {rid: r.public()
                             for rid, r in sorted(cleaned_roles.items())}}
               if cleaned_roles else {}),
            **({"resourceGroups": {gid: g.public()
                                   for gid, g in sorted(cleaned_groups.items())}}
               if cleaned_groups else {}),
            **({PEOPLE_BLOCK: {identity: entry
                               for identity, entry in sorted(cleaned_people.items())}}
               if cleaned_people else {}),
            "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Under a shared passphrase this is "admin (passphrase)", which is
            # the honest answer: that mode cannot tell one admin from another.
            "updatedBy": actor or "unknown",
        }
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".visibility-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                    tmp.write(body)
                    tmp.flush()
                    # The rename is atomic, but only relative to data that has
                    # actually reached the disk. Without this, a power loss
                    # between rename and writeback leaves a correctly-named
                    # file with no contents.
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                # Including KeyboardInterrupt and SIGTERM during a deploy: a
                # dot-prefixed temp file left in a mounted volume is litter
                # that accumulates one per interrupted save.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise RuntimeError(
                f"Could not write the visibility policy to {self.path}: {exc}. "
                "Check the volume is mounted and writable by the container's "
                "user.") from exc

        # The cache is refreshed in place rather than re-read, and `_stamp` is
        # advanced so `_load` will not re-read either. EVERY FIELD `_load`
        # SETS MUST BE SET HERE TOO — one left out is written to disk
        # correctly and then ignored until the container restarts, which
        # presents as a save that silently did nothing and sends somebody
        # looking at the file, where the value is sitting in plain sight.
        # `_people` was exactly that bug.
        with self._lock:
            self._cache = cleaned
            self._scope = cleaned_scope
            self._roles = cleaned_roles
            self._groups = cleaned_groups
            self._people = cleaned_people
            self._meta = {"updatedAt": payload["updatedAt"],
                          "updatedBy": payload["updatedBy"]}
            self._stamp = self._stat()
            self._loaded = True

        logger.info(
            "visibility: policy saved by %s — %s", payload["updatedBy"],
            "; ".join(
                f"{rid}: {len(role.grants)} capability(ies), "
                f"{len(role.visible)} element(s) visible, "
                + ("unrestricted scope" if rid not in cleaned_scope else
                   f"{len(cleaned_scope[rid].get('ecs') or {})} EC(s) allowed")
                for rid, role in sorted(cleaned_roles.items())) or "no roles"
            + (f"; {len(cleaned_roles)} role(s)" if cleaned_roles else "")
            + (f"; {len(cleaned_people)} person(s) with their own entry"
               if cleaned_people else ""))
        return self.policy()


STORE = PolicyStore(AUTH.visibility_file)


def hidden_for(identity: Optional[str],
               role_id: Optional[str] = None) -> Tuple[str, ...]:
    """
    Module-level shorthand — the routers only ever want this.

    Routers ask `auth.hidden_of(request)` rather than calling this, so that no
    fact about a caller can be dropped by accident.
    """
    return STORE.hidden_for(identity, role_id)


def grants_for(identity: Optional[str],
               role_id: Optional[str] = None) -> frozenset:
    """What this person's role confers. See PolicyStore.grants_for."""
    return STORE.grants_for(identity, role_id)


def scope_for(identity: Optional[str], role_id: Optional[str] = None):
    """
    Module-level shorthand for the scope half.

    Routers ask `auth.scope_of(request)`, which carries both facts about a
    caller so neither can be dropped — see
    `test_access.py::test_every_scope_lookup_passes_an_identity`.
    """
    return STORE.scope_for(identity, role_id)

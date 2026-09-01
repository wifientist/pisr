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

import scope as scope_rules
import sections as section_catalogue
from config import AUTH

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2

# The only role that can have anything hidden from it. Admins see everything by
# construction — there is no policy entry that could hide a section from the
# person who edits the policy, which removes the "locked myself out" failure
# entirely. A third role would be a new key here and a new column in the portal.
MANAGED_ROLES: Tuple[str, ...] = ("user",)

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
        for role in MANAGED_ROLES:
            wanted = hidden.get(role)
            if not isinstance(wanted, list):
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
    def _clean_scope(raw) -> Dict[str, Dict[str, Any]]:
        """
        Keep only managed roles' scope blocks, each validated by `scope.clean`.

        A role whose block says "no restriction" is dropped rather than stored
        as an explicit unrestricted marker, so the file has exactly one way to
        say it and `scope_for` has exactly one thing to test.
        """
        if not isinstance(raw, dict):
            return {}
        cleaned: Dict[str, Dict[str, Any]] = {}
        for role in MANAGED_ROLES:
            block = scope_rules.clean(raw.get(role))
            if block is not None:
                cleaned[role] = block
        return cleaned

    def scope_for(self, role: str):
        """
        What this role may reach, as a `scope.Scope`.

        Admins are unrestricted, and that short-circuit sits above the file
        read for the same reason `hidden_for`'s does: an admin has to be able
        to reach the portal and fix a scope even when the policy file is the
        thing that is broken. It is also what stops an admin locking themselves
        out of every EC with one bad save.
        """
        if role == "admin" or role not in MANAGED_ROLES:
            return scope_rules.UNRESTRICTED
        with self._lock:
            self._load()
            return scope_rules.parse(self._scope.get(role))

    def hidden_for(self, role: str) -> Tuple[str, ...]:
        """
        Section ids this role does not see. Admins get an empty tuple always.

        The admin short-circuit is above the file read deliberately: an admin
        must still be able to reach the portal and fix things when the policy
        file is the thing that is broken.
        """
        if role == "admin" or role not in MANAGED_ROLES:
            return ()
        with self._lock:
            self._load()
            return tuple(self._cache.get(role, ()))

    def policy(self) -> Dict[str, object]:
        """The whole policy, for the admin portal."""
        with self._lock:
            self._load()
            return {
                "version": FORMAT_VERSION,
                "hidden": {role: list(self._cache.get(role, []))
                           for role in MANAGED_ROLES},
                "scope": {role: scope_rules.parse(self._scope.get(role)).as_json()
                          for role in MANAGED_ROLES},
                "updatedAt": self._meta.get("updatedAt"),
                "updatedBy": self._meta.get("updatedBy"),
                "writable": self.writable,
                "path": str(self.path) if self.path else None,
            }

    # ── writing ──────────────────────────────────────────────────────

    def save(self, hidden: Dict[str, List[str]], scope: Optional[Dict[str, Any]],
             actor: Optional[str]) -> Dict[str, object]:
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

        cleaned = self._clean(hidden)
        cleaned_scope = self._clean_scope(scope)
        payload = {
            "version": FORMAT_VERSION,
            "hidden": {role: cleaned.get(role, []) for role in MANAGED_ROLES},
            # Omitted entirely when no role is restricted, so an unrestricted
            # deployment's file stays readable at a glance and a version-1 file
            # round-trips to something that looks the same.
            **({"scope": cleaned_scope} if cleaned_scope else {}),
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

        with self._lock:
            self._cache = cleaned
            self._scope = cleaned_scope
            self._meta = {"updatedAt": payload["updatedAt"],
                          "updatedBy": payload["updatedBy"]}
            self._stamp = self._stat()
            self._loaded = True

        logger.info(
            "visibility: policy saved by %s — %s", payload["updatedBy"],
            "; ".join(
                f"{role}: {len(cleaned.get(role, []))} section(s) hidden, "
                + ("unrestricted scope" if role not in cleaned_scope else
                   f"{len(cleaned_scope[role].get('ecs') or {})} EC(s) allowed")
                for role in MANAGED_ROLES))
        return self.policy()


STORE = PolicyStore(AUTH.visibility_file)


def hidden_for(role: str) -> Tuple[str, ...]:
    """Module-level shorthand — the routers only ever want this."""
    return STORE.hidden_for(role)


def scope_for(role: str):
    """Module-level shorthand for the scope half."""
    return STORE.scope_for(role)

"""
Comparing what a venue is configured to against what it ought to be.

Two baselines, and they are kept apart for different reasons:

  RUCKUS   vendor guidance. Generic, the same for every customer, so it lives
           in this repository at api/baselines/ruckus.json.

  ORG      the customer's own agreed configuration. Specific to one company,
           so it is a MOUNTED FILE and its name is an environment variable.
           Neither the values nor the customer's name belong in a repository
           that is not theirs — see config._org_name.

UNVERIFIED UNTIL SOMEBODY SAYS OTHERWISE. A baseline file carries a `status`,
and anything other than "verified" is surfaced to the reader as such. The
shipped RUCKUS file is `"status": "placeholder"` with invented values, so the
mechanism can be exercised end to end before the real guidance is sourced.

  That flag is not decoration. This tab is read by an install crew deciding
  whether a site is finished, and a fabricated "RUCKUS recommends" is worse
  than an empty column — an empty column asks a question, a wrong one answers
  it. Do not remove the flag; set it to "verified" when the values are real,
  and put where they came from in `source`.

KEYED BY `<endpoint>.<dotted path>`, matching the R1 path a value came from
rather than a label — labels change, and a baseline keyed to prose would drift
silently. `apRebootTimeoutSettings.gatewayLossTimeout`, not "Reboot after
gateway loss".

THREE STATES, NOT TWO, FOR THE ORG BASELINE. A setting is in one of three
states, and the third is the reason this file is writable:

  a recommended value   in `values`        — compared, may match or mismatch
  not applicable        in `notApplicable` — shown as "—", NEVER a mismatch
  not reviewed          in neither         — no column at all (the default)

"Not applicable" and "not reviewed" look the same to a reader — both mean "no
opinion here" — but they are different to the admin maintaining the baseline:
one is "we looked and there is nothing to recommend", the other is "we have not
looked yet". Collapsing them would make the baseline un-auditable, so they are
stored distinctly and the admin editor lets an admin move a field between all
three. RUCKUS has no `notApplicable` — it is vendor guidance, read-only, and a
field it says nothing about is simply absent.

THE ORG BASELINE IS NOW A FILE PISR WRITES, joining the visibility policy and
the accounts file on the pisr-config volume. That is not a contradiction of
"PISR stores nothing" for the same reason those two are not: it holds
recommended values keyed by R1 path — operator configuration, no venue data, no
device, no credential. See visibility.py for the fuller version of the
argument. RUCKUS stays repo-sourced and is never written.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import AUTH

logger = logging.getLogger(__name__)

_RUCKUS_FILE = Path(__file__).resolve().parent / "baselines" / "ruckus.json"

# Sentinel for "this baseline says nothing about this setting", which is
# different from "it recommends null".
MISSING = object()

# Sentinel for "this baseline was reviewed and deliberately recommends nothing
# here" — the org column shows "—" and no mismatch is ever computed. Distinct
# from MISSING (never reviewed), which shows no column at all.
NOT_APPLICABLE = object()

# The statuses a baseline file may carry. Only "verified" makes the column
# read as trustworthy; everything else is captioned unverified, and the editor
# offers the two an admin sets by hand.
STATUSES = ("verified", "placeholder", "unverified")


class Baseline:
    """One set of recommended values, loaded from JSON and cached by mtime."""

    def __init__(self, path: Optional[Path], fallback_name: str):
        self.path = path
        self.fallback_name = fallback_name
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = {}
        self._na: set = set()
        self._meta: Dict[str, Any] = {}
        self._stamp: Optional[Tuple[float, int]] = None
        self._loaded = False

    def _stat(self):
        try:
            info = self.path.stat()
            return (info.st_mtime, info.st_size)
        except (OSError, AttributeError):
            return None

    def _load(self) -> None:
        if not self.path:
            self._loaded = True
            return
        stamp = self._stat()
        if self._loaded and stamp == self._stamp:
            return
        if stamp is None:
            # No file is the ordinary state for the org baseline: most
            # deployments have not written one, and the column is simply empty.
            self._values, self._na, self._meta = {}, set(), {}
            self._stamp, self._loaded = None, True
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("baselines: could not read %s (%s). The column will be "
                         "empty rather than wrong.", self.path, exc)
            self._stamp, self._loaded = stamp, True
            return

        values = raw.get("values")
        self._values = values if isinstance(values, dict) else {}
        na = raw.get("notApplicable")
        self._na = {k for k in na if isinstance(k, str)} if isinstance(na, list) else set()
        self._meta = {k: raw.get(k) for k in ("name", "status", "note", "source",
                                              "verifiedAt")}
        # The global "show recommendations at all" switch. Default ON when the
        # key is absent — an old file, or a deployment that never touched it,
        # keeps showing the columns. The admin turns it OFF to suppress both
        # columns everywhere without deleting the values.
        show = raw.get("show")
        self._meta["show"] = show if isinstance(show, bool) else True
        self._stamp, self._loaded = stamp, True

    def get(self, key: str) -> Any:
        """The recommended value, or MISSING. N.A. reads as MISSING here — a
        caller wanting to tell them apart asks `is_na` too (see `lookup`)."""
        with self._lock:
            self._load()
            return self._values.get(key, MISSING)

    def is_na(self, key: str) -> bool:
        with self._lock:
            self._load()
            return key in self._na

    def describe(self) -> Dict[str, Any]:
        """Header material for the column: whose it is and whether to trust it."""
        with self._lock:
            self._load()
            status = self._meta.get("status") or ("empty" if not self._values
                                                  else "unverified")
            return {
                "name": self._meta.get("name") or self.fallback_name,
                "status": status,
                # The single thing the UI keys off. Anything not explicitly
                # verified is captioned as such.
                "verified": status == "verified",
                "note": self._meta.get("note") or "",
                "source": self._meta.get("source") or "",
                "count": len(self._values),
                # Whether this baseline gets a COLUMN in the Config tab, and the
                # reason the reader shows it consistently rather than per-
                # category. `some(row has a rec)` per category made the column
                # blink in and out between settings; keyed on whether the
                # baseline has any content at all, the column is present for
                # every setting (with "—" where this field has no rec) or absent
                # entirely. Values OR not-applicable count — an admin who marked
                # a field N.A. has an opinion worth a column.
                "active": bool(self._values) or bool(self._na),
            }

    # ── writing (org baseline only) ──────────────────────────────────

    @property
    def writable(self) -> bool:
        """
        Can an admin save from the editor?

        Tests the DIRECTORY, not the file — the file legitimately does not
        exist until the first save, and a read-only mount is the failure this
        is really looking for: an editor that accepts a change and loses it at
        the next deploy. Same reasoning as visibility.PolicyStore.writable.
        """
        if not self.path:
            return False
        try:
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    @property
    def show(self) -> bool:
        """The global show-recommendations switch. Default ON."""
        with self._lock:
            self._load()
            return bool(self._meta.get("show", True))

    def full(self) -> Dict[str, Any]:
        """The whole baseline, for the admin editor to render and round-trip."""
        with self._lock:
            self._load()
            return {
                "values": dict(self._values),
                "notApplicable": sorted(self._na),
                "status": self._meta.get("status") or "",
                "source": self._meta.get("source") or "",
                "note": self._meta.get("note") or "",
                "verifiedAt": self._meta.get("verifiedAt"),
                "show": bool(self._meta.get("show", True)),
                "writable": self.writable,
                "path": str(self.path) if self.path else None,
            }

    def save(self, values: Dict[str, Any], not_applicable: List[str],
             status: str, source: str, show: bool, actor: Optional[str]) -> Dict[str, Any]:
        """
        Replace the baseline. Raises RuntimeError if there is nowhere to write.

        Atomic write copied from visibility.PolicyStore.save: a temp file in the
        SAME directory, fsync, then os.replace — a reader sees the old file or
        the new one, never a half-written one, and the rename stays atomic
        because it is on the same filesystem as the mounted volume.

        A key cannot be in both `values` and `notApplicable` — a field either
        has a recommendation or explicitly has none. When both arrive for one
        key (a UI race), the explicit value wins and the N.A. entry is dropped,
        because a stored value is the more specific statement.
        """
        if not self.path:
            raise RuntimeError(
                "PISR_ORG_BASELINE_FILE is not set, so there is nowhere to save "
                "the baseline. Set it and mount a writable volume at its "
                "directory.")

        clean_values = {k: v for k, v in values.items() if isinstance(k, str)}
        clean_na = sorted({k for k in not_applicable
                           if isinstance(k, str) and k not in clean_values})
        status = status if status in STATUSES else "unverified"

        payload = {
            # The name travels with the file for a human reading it, but the UI
            # always labels the column from PISR_ORG_NAME (see describe()), so a
            # file copied between deployments cannot mislabel itself.
            "name": AUTH.org_name,
            "status": status,
            "source": source or "",
            # Only a verified baseline gets a timestamp; an unverified one has
            # nothing to date. This is what the header caption reads.
            "verifiedAt": (datetime.now(timezone.utc).isoformat(timespec="seconds")
                           if status == "verified" else None),
            "show": bool(show),
            "values": clean_values,
            **({"notApplicable": clean_na} if clean_na else {}),
            "updatedBy": actor or "unknown",
        }
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".baseline-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                    tmp.write(body)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise RuntimeError(
                f"Could not write the baseline to {self.path}: {exc}. Check the "
                "volume is mounted and writable by the container's user.") from exc

        with self._lock:
            self._values = clean_values
            self._na = set(clean_na)
            self._meta = {k: payload.get(k) for k in
                          ("name", "status", "note", "source", "verifiedAt", "show")}
            self._stamp = self._stat()
            self._loaded = True

        logger.info("baselines: org baseline saved by %s — %d value(s), %d "
                    "not-applicable, status=%s", payload["updatedBy"],
                    len(clean_values), len(clean_na), status)
        return self.full()


RUCKUS = Baseline(_RUCKUS_FILE, "RUCKUS")
ORG = Baseline(Path(AUTH.org_baseline_file) if AUTH.org_baseline_file else None,
               AUTH.org_name)


def describe() -> Dict[str, Any]:
    """Both column headers plus the global show switch, for the Config tab."""
    org = ORG.describe()
    # The org column is named from the environment, never from the file, so a
    # baseline copied between deployments cannot mislabel itself.
    org["name"] = AUTH.org_name
    # The single flag the reader keys the whole recommendation feature off:
    # both columns appear only when this is on. Lives with the org baseline
    # because that is the file an admin edits from the recommendations portal.
    return {"org": org, "ruckus": RUCKUS.describe(), "show": ORG.show}


def lookup(key: str) -> Dict[str, Any]:
    """
    What each baseline says about one setting, or nothing.

    Returns only the halves that have an opinion. A setting neither baseline
    mentions gets no columns rather than two empty ones, which keeps the
    common case — most settings, most of the time — from becoming a wall of
    dashes.
    """
    out: Dict[str, Any] = {}
    org = ORG.get(key)
    if org is not MISSING:
        out["org"] = org
    elif ORG.is_na(key):
        # Reviewed, deliberately no recommendation. shape._config_row turns
        # this into a "—" cell with no mismatch, distinct from the key being
        # absent entirely (which yields no org column at all).
        out["org"] = NOT_APPLICABLE
    ruckus = RUCKUS.get(key)
    if ruckus is not MISSING:
        out["ruckus"] = ruckus
    return out


def org_full() -> Dict[str, Any]:
    """The whole org baseline, for the admin editor. RUCKUS is fetched via its
    own `values` map in the router — it is read-only reference, not editable."""
    return ORG.full()


def save_org(values: Dict[str, Any], not_applicable: List[str],
             status: str, source: str, show: bool,
             actor: Optional[str]) -> Dict[str, Any]:
    return ORG.save(values, not_applicable, status, source, show, actor)


def ruckus_values() -> Dict[str, Any]:
    """RUCKUS's recommendations as a flat map, for the editor's read-only
    reference column. Read straight off the loaded baseline."""
    with RUCKUS._lock:
        RUCKUS._load()
        return dict(RUCKUS._values)

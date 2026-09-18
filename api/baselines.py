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

STRUCTURED BY LEVEL — venue / apgroup / network. A setting is recommended at the
level R1 exposes it, and venue and AP-group share several endpoint NAMES
(`apClientAdmissionControlSettings`, the `apModel*` set), so the key alone cannot
say which level a value belongs to. The level is therefore a separate dimension
of the store, NOT baked into the key: the same key at two levels is two different
settings. A baseline written before this dimension existed is a flat
`values`/`notApplicable` at the top level; it is read AS the venue level, so no
mounted file needs migrating (see `Baseline._read_levels`). `network` is scaffold
for the next phase and round-trips empty.

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

import fnmatch
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

# The static field catalogue the editor browses — every settable field, keyed
# like the baseline, with type/enum/label. Built from the OpenAPI spec by
# scripts/build_field_catalogue.py and committed; the editor no longer has to
# poll a live venue to discover fields. See that script's docstring.
_CATALOGUE_FILE = Path(__file__).resolve().parent / "baselines" / "field_catalogue.json"

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

# The recommendation LEVELS. A setting is recommended at the level R1 exposes it:
# venue-wide, per AP-group, or per network/SSID. Venue and AP-group share several
# endpoint NAMES (apClientAdmissionControlSettings and the apModel* set live at
# both), so the key alone cannot say which level it belongs to — the level is a
# separate dimension of the store, not encoded into the key. `network` is scaffold
# for the next phase; the store round-trips it so nothing is lost meanwhile.
LEVELS = ("venue", "apgroup", "network")
DEFAULT_LEVEL = "venue"


def _level(level: Optional[str]) -> str:
    """A known level, defaulting to venue — so an old caller that passes none,
    and a corrupt level from disk, both resolve to the historical behaviour."""
    return level if level in LEVELS else DEFAULT_LEVEL


# ── network groups ───────────────────────────────────────────────────
#
# A network-level recommendation applies to EVERY SSID, but an MDU runs hundreds
# of per-unit SSIDs that share 99% of their config plus a few genuinely distinct
# ones (guest, corporate). A GROUP lets an admin carve out a set of SSIDs and
# give them their own recommendations, which OVERRIDE the network default per
# key. A group is matched to a network by a rule — name glob, network type, or an
# explicit id list (which doubles as "pin this one SSID out on its own"). Groups
# exist only at the network level and only in the ORG baseline: RUCKUS guidance
# is generic and knows nothing about a customer's SSID naming. See the reader for
# the separate, display-only auto-clustering that collapses identical SSIDs.
NETWORK_GROUP_LEVEL = "network"


def _group_matches(match: Dict[str, Any], meta: Dict[str, Any]) -> bool:
    """Whether a network (its `meta`: id/name/ssid/type) satisfies a group rule.

    Case-insensitive throughout, and a malformed rule matches NOTHING rather
    than everything — an unmatched network simply falls to the network default,
    which is the safe direction for a recommendation comparison."""
    by = (match or {}).get("by")
    if by == "ids":
        return meta.get("id") in set(match.get("ids") or [])
    if by == "type":
        wanted = {str(t).lower() for t in (match.get("types") or [])}
        return str(meta.get("type") or "").lower() in wanted
    if by == "name":
        pattern = str(match.get("pattern") or "")
        if not pattern:
            return False
        return any(fnmatch.fnmatchcase(str(v).lower(), pattern.lower())
                   for v in (meta.get("name"), meta.get("ssid")) if v)
    return False


class Baseline:
    """One set of recommended values, loaded from JSON and cached by mtime."""

    def __init__(self, path: Optional[Path], fallback_name: str):
        self.path = path
        self.fallback_name = fallback_name
        self._lock = threading.Lock()
        # Per level: {level -> {key -> value}} and {level -> set(keys)}. The meta
        # (status, source, show, …) stays global — it is a property of the whole
        # baseline document, not of one level.
        self._values: Dict[str, Dict[str, Any]] = {lvl: {} for lvl in LEVELS}
        self._na: Dict[str, set] = {lvl: set() for lvl in LEVELS}
        # Network groups: an ordered list of {id, name, match, values, na}. Only
        # the ORG baseline ever populates it; RUCKUS leaves it empty.
        self._groups: List[Dict[str, Any]] = []
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
            self._values = {lvl: {} for lvl in LEVELS}
            self._na = {lvl: set() for lvl in LEVELS}
            self._groups = []
            self._meta = {}
            self._stamp, self._loaded = None, True
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("baselines: could not read %s (%s). The column will be "
                         "empty rather than wrong.", self.path, exc)
            self._stamp, self._loaded = stamp, True
            return

        self._values, self._na = self._read_levels(raw)
        self._groups = self._read_groups(raw)
        self._meta = {k: raw.get(k) for k in ("name", "status", "note", "source",
                                              "verifiedAt")}
        # The global "show recommendations at all" switch. Default ON when the
        # key is absent — an old file, or a deployment that never touched it,
        # keeps showing the columns. The admin turns it OFF to suppress both
        # columns everywhere without deleting the values.
        show = raw.get("show")
        self._meta["show"] = show if isinstance(show, bool) else True
        self._stamp, self._loaded = stamp, True

    @staticmethod
    def _read_levels(raw: Dict[str, Any]):
        """
        (values-by-level, na-by-level) out of a baseline document, of EITHER
        shape.

        New shape: `{"levels": {"venue": {"values": {...}, "notApplicable":
        [...]}, "apgroup": {...}}}`. Old shape (before the level dimension
        existed): a flat top-level `values`/`notApplicable`, which was always
        venue-scoped — so it is read AS the venue level. That migration is why a
        baseline written by the old code keeps working untouched, and is applied
        on read so no separate migration step has to run against a mounted file.
        """
        values = {lvl: {} for lvl in LEVELS}
        na = {lvl: set() for lvl in LEVELS}

        def _section(vals, na_list):
            v = vals if isinstance(vals, dict) else {}
            n = ({k for k in na_list if isinstance(k, str)}
                 if isinstance(na_list, list) else set())
            return v, n

        levels_raw = raw.get("levels")
        if isinstance(levels_raw, dict):
            for lvl in LEVELS:
                section = levels_raw.get(lvl) or {}
                if isinstance(section, dict):
                    values[lvl], na[lvl] = _section(
                        section.get("values"), section.get("notApplicable"))
        else:
            # Back-compat: a flat file is the venue level.
            values[DEFAULT_LEVEL], na[DEFAULT_LEVEL] = _section(
                raw.get("values"), raw.get("notApplicable"))
        return values, na

    @staticmethod
    def _read_groups(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        The `networkGroups` list, normalised. A group needs a string `id` or it
        is dropped — the id is what a recommendation is filed under, so a group
        with none can hold nothing. Duplicate ids keep the first, for the same
        reason the reader resolves the FIRST matching group: order is the
        admin's precedence, and a later duplicate would silently shadow it.
        """
        groups: List[Dict[str, Any]] = []
        seen: set = set()
        for g in (raw.get("networkGroups") or []):
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            if not isinstance(gid, str) or not gid or gid in seen:
                continue
            seen.add(gid)
            vals = g.get("values") if isinstance(g.get("values"), dict) else {}
            vals = {k: v for k, v in vals.items() if isinstance(k, str)}
            na_raw = g.get("notApplicable")
            na = ({k for k in na_raw if isinstance(k, str) and k not in vals}
                  if isinstance(na_raw, list) else set())
            groups.append({
                "id": gid,
                "name": g.get("name") if isinstance(g.get("name"), str) else gid,
                "match": g.get("match") if isinstance(g.get("match"), dict) else {},
                "values": vals,
                "na": na,
            })
        return groups

    def _group_by_id(self, gid: str) -> Optional[Dict[str, Any]]:
        for g in self._groups:
            if g["id"] == gid:
                return g
        return None

    def _group_opinion(self, gid: str, key: str):
        """('value', v) | ('na', None) | None — what a group says about a key.

        `None` means the group has NO opinion, and the caller falls back to the
        network default; the whole point of a group is to override PER KEY, not
        to shadow the default wholesale."""
        g = self._group_by_id(gid)
        if g is None:
            return None
        if key in g["values"]:
            return ("value", g["values"][key])
        if key in g["na"]:
            return ("na", None)
        return None

    def match_group(self, meta: Dict[str, Any]) -> Optional[str]:
        """The id of the FIRST network group this network matches, or None. Order
        is precedence — an admin puts the specific group above the catch-all."""
        with self._lock:
            self._load()
            for g in self._groups:
                if _group_matches(g["match"], meta):
                    return g["id"]
        return None

    def get(self, key: str, level: Optional[str] = DEFAULT_LEVEL,
            group: Optional[str] = None) -> Any:
        """The recommended value at `level` (and, for the network level, within
        `group`), or MISSING. N.A. reads as MISSING here — a caller wanting to
        tell them apart asks `is_na` too (see `lookup`)."""
        with self._lock:
            self._load()
            if group and _level(level) == NETWORK_GROUP_LEVEL:
                opinion = self._group_opinion(group, key)
                if opinion is not None:
                    return opinion[1] if opinion[0] == "value" else MISSING
            return self._values[_level(level)].get(key, MISSING)

    def is_na(self, key: str, level: Optional[str] = DEFAULT_LEVEL,
              group: Optional[str] = None) -> bool:
        with self._lock:
            self._load()
            if group and _level(level) == NETWORK_GROUP_LEVEL:
                opinion = self._group_opinion(group, key)
                if opinion is not None:
                    return opinion[0] == "na"
            return key in self._na[_level(level)]

    def network_groups(self) -> List[Dict[str, Any]]:
        """The groups as the editor and reader want them: id, name, match rule
        and per-group counts. Values travel in `full()` for the editor to edit;
        the reader only needs the rule (to match a network) and the counts."""
        with self._lock:
            self._load()
            return [{
                "id": g["id"], "name": g["name"], "match": dict(g["match"]),
                "count": len(g["values"]), "naCount": len(g["na"]),
            } for g in self._groups]

    def _any_values(self) -> bool:
        return any(self._values[lvl] for lvl in LEVELS)

    def describe(self, level: Optional[str] = DEFAULT_LEVEL) -> Dict[str, Any]:
        """
        Header material for the column at `level`: whose it is, whether to trust
        it, and whether it earns a column here.

        The trust half (name, status, verified, source) is GLOBAL — a baseline is
        verified as a document, not per level. The `count`/`active` half is
        per-level, so the Config tab's venue view and the AP-group view each show
        a column only when THIS level has something to say.
        """
        lvl = _level(level)
        with self._lock:
            self._load()
            status = self._meta.get("status") or ("empty" if not self._any_values()
                                                  else "unverified")
            return {
                "name": self._meta.get("name") or self.fallback_name,
                "status": status,
                # The single thing the UI keys off. Anything not explicitly
                # verified is captioned as such.
                "verified": status == "verified",
                "note": self._meta.get("note") or "",
                "source": self._meta.get("source") or "",
                "count": len(self._values[lvl]),
                # Whether this baseline gets a COLUMN at this level, and the
                # reason the reader shows it consistently rather than per-
                # category. `some(row has a rec)` per category made the column
                # blink in and out between settings; keyed on whether the level
                # has any content at all, the column is present for every setting
                # (with "—" where this field has no rec) or absent entirely.
                # Values OR not-applicable count — an admin who marked a field
                # N.A. has an opinion worth a column. At the network level a
                # GROUP's recommendations count too: a network in a group gets a
                # column even when the default is empty.
                "active": (bool(self._values[lvl]) or bool(self._na[lvl])
                           or (lvl == NETWORK_GROUP_LEVEL
                               and any(g["values"] or g["na"] for g in self._groups))),
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
        """
        The whole baseline, all levels, for the admin editor to render and
        round-trip. The editor MUST get every level and send them all back:
        saving is a whole-document replace, so a save that carried only the
        level on screen would delete the recommendations set at the others.
        """
        with self._lock:
            self._load()
            return {
                "levels": {lvl: {
                    "values": dict(self._values[lvl]),
                    "notApplicable": sorted(self._na[lvl]),
                } for lvl in LEVELS},
                # Groups travel with their values so the editor round-trips them
                # whole, exactly like the levels — a save that dropped them would
                # delete every per-group recommendation.
                "networkGroups": [{
                    "id": g["id"], "name": g["name"], "match": dict(g["match"]),
                    "values": dict(g["values"]), "notApplicable": sorted(g["na"]),
                } for g in self._groups],
                "status": self._meta.get("status") or "",
                "source": self._meta.get("source") or "",
                "note": self._meta.get("note") or "",
                "verifiedAt": self._meta.get("verifiedAt"),
                "show": bool(self._meta.get("show", True)),
                "writable": self.writable,
                "path": str(self.path) if self.path else None,
            }

    def save(self, levels: Dict[str, Any], network_groups: Optional[List[Dict[str, Any]]],
             status: str, source: str, show: bool, actor: Optional[str]) -> Dict[str, Any]:
        """
        Replace the baseline, all levels at once. Raises RuntimeError if there is
        nowhere to write.

        Atomic write copied from visibility.PolicyStore.save: a temp file in the
        SAME directory, fsync, then os.replace — a reader sees the old file or
        the new one, never a half-written one, and the rename stays atomic
        because it is on the same filesystem as the mounted volume.

        `levels` is `{level -> {"values": {...}, "notApplicable": [...]}}`. A key
        cannot be in both `values` and `notApplicable` within a level — a field
        either has a recommendation or explicitly has none. When both arrive for
        one key (a UI race), the explicit value wins and the N.A. entry is
        dropped, because a stored value is the more specific statement. The same
        key MAY appear at two different levels: those are different settings.
        """
        if not self.path:
            raise RuntimeError(
                "PISR_ORG_BASELINE_FILE is not set, so there is nowhere to save "
                "the baseline. Set it and mount a writable volume at its "
                "directory.")

        clean_levels: Dict[str, Dict[str, Any]] = {}
        for lvl in LEVELS:
            section = levels.get(lvl) if isinstance(levels, dict) else None
            section = section or {}
            raw_values = section.get("values") if isinstance(section, dict) else None
            raw_na = section.get("notApplicable") if isinstance(section, dict) else None
            vals = {k: v for k, v in (raw_values or {}).items() if isinstance(k, str)}
            na = sorted({k for k in (raw_na or [])
                         if isinstance(k, str) and k not in vals})
            clean_levels[lvl] = {"values": vals, "notApplicable": na}
        status = status if status in STATUSES else "unverified"

        # Groups are cleaned the same way as a level — value wins over N.A. per
        # key — and re-read through `_read_groups` so the stored shape and the
        # in-memory shape can never diverge (dedupe of ids, drop of id-less).
        clean_groups: List[Dict[str, Any]] = []
        for g in (network_groups or []):
            if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not g["id"]:
                continue
            raw_v = g.get("values") if isinstance(g.get("values"), dict) else {}
            gv = {k: v for k, v in raw_v.items() if isinstance(k, str)}
            raw_n = g.get("notApplicable")
            gn = sorted({k for k in (raw_n or [])
                         if isinstance(k, str) and k not in gv})
            clean_groups.append({
                "id": g["id"],
                "name": g.get("name") if isinstance(g.get("name"), str) else g["id"],
                "match": g.get("match") if isinstance(g.get("match"), dict) else {},
                "values": gv,
                **({"notApplicable": gn} if gn else {}),
            })

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
            "levels": {lvl: {
                "values": clean_levels[lvl]["values"],
                **({"notApplicable": clean_levels[lvl]["notApplicable"]}
                   if clean_levels[lvl]["notApplicable"] else {}),
            } for lvl in LEVELS},
            **({"networkGroups": clean_groups} if clean_groups else {}),
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
            self._values = {lvl: dict(clean_levels[lvl]["values"]) for lvl in LEVELS}
            self._na = {lvl: set(clean_levels[lvl]["notApplicable"]) for lvl in LEVELS}
            # Re-read through the same normaliser the loader uses, so the cached
            # groups match exactly what a fresh load of this file would produce.
            self._groups = self._read_groups(payload)
            self._meta = {k: payload.get(k) for k in
                          ("name", "status", "note", "source", "verifiedAt", "show")}
            self._stamp = self._stat()
            self._loaded = True

        totals = ", ".join(
            f"{lvl}={len(clean_levels[lvl]['values'])}v/"
            f"{len(clean_levels[lvl]['notApplicable'])}na"
            for lvl in LEVELS if clean_levels[lvl]["values"] or clean_levels[lvl]["notApplicable"])
        logger.info("baselines: org baseline saved by %s — %s, status=%s",
                    payload["updatedBy"], totals or "empty", status)
        return self.full()


RUCKUS = Baseline(_RUCKUS_FILE, "RUCKUS")
ORG = Baseline(Path(AUTH.org_baseline_file) if AUTH.org_baseline_file else None,
               AUTH.org_name)


def describe(level: Optional[str] = DEFAULT_LEVEL) -> Dict[str, Any]:
    """Both column headers plus the global show switch, for the Config tab at
    `level`. Venue callers pass nothing and get the historical behaviour."""
    org = ORG.describe(level)
    # The org column is named from the environment, never from the file, so a
    # baseline copied between deployments cannot mislabel itself.
    org["name"] = AUTH.org_name
    # The single flag the reader keys the whole recommendation feature off:
    # both columns appear only when this is on. Lives with the org baseline
    # because that is the file an admin edits from the recommendations portal.
    return {"org": org, "ruckus": RUCKUS.describe(level), "show": ORG.show}


def lookup(key: str, level: Optional[str] = DEFAULT_LEVEL,
           group: Optional[str] = None) -> Dict[str, Any]:
    """
    What each baseline says about one setting AT ONE LEVEL, or nothing.

    Returns only the halves that have an opinion. A setting neither baseline
    mentions gets no columns rather than two empty ones, which keeps the
    common case — most settings, most of the time — from becoming a wall of
    dashes. The `level` is what tells a venue-wide `apClientAdmissionControl…`
    recommendation from a per-AP-group one keyed identically.

    `group` (network level only) resolves the ORG half against that group's
    recommendations first, per key, falling back to the network default — so a
    per-unit SSID group can override the default for the few keys it cares about
    and inherit the rest. RUCKUS is never grouped: its guidance is generic.
    """
    out: Dict[str, Any] = {}
    org = ORG.get(key, level, group)
    if org is not MISSING:
        out["org"] = org
    elif ORG.is_na(key, level, group):
        # Reviewed, deliberately no recommendation. shape._config_row turns
        # this into a "—" cell with no mismatch, distinct from the key being
        # absent entirely (which yields no org column at all).
        out["org"] = NOT_APPLICABLE
    ruckus = RUCKUS.get(key, level)
    if ruckus is not MISSING:
        out["ruckus"] = ruckus
    return out


def network_group_for(meta: Dict[str, Any]) -> Optional[str]:
    """The org network-group id a network belongs to, or None for the default.
    `meta` carries the network's id/name/ssid/type — see `_group_matches`."""
    return ORG.match_group(meta)


def network_groups() -> List[Dict[str, Any]]:
    """The org network groups (id, name, match rule, counts), for the reader to
    label a network's group and the editor to list them."""
    return ORG.network_groups()


def org_full() -> Dict[str, Any]:
    """The whole org baseline, all levels and network groups, for the admin
    editor. RUCKUS is fetched via its own per-level maps in the router —
    read-only reference, not editable."""
    return ORG.full()


def save_org(levels: Dict[str, Any], network_groups: Optional[List[Dict[str, Any]]],
             status: str, source: str, show: bool,
             actor: Optional[str]) -> Dict[str, Any]:
    return ORG.save(levels, network_groups, status, source, show, actor)


_catalogue_cache: Optional[Dict[str, Any]] = None


def field_catalogue() -> Dict[str, Any]:
    """
    The static field catalogue, loaded once and cached for the process.

    A missing or unreadable file yields an empty catalogue rather than an
    error: the editor falls back to loading a venue's fields, so an unbuilt
    catalogue degrades to the old behaviour instead of breaking. Rebuild it
    with scripts/build_field_catalogue.py when the spec is reshipped.
    """
    global _catalogue_cache
    if _catalogue_cache is None:
        try:
            _catalogue_cache = json.loads(_CATALOGUE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("baselines: no field catalogue (%s); the editor will "
                           "fall back to loading a venue's fields.", exc)
            _catalogue_cache = {}
    return _catalogue_cache


def ruckus_values(level: Optional[str] = DEFAULT_LEVEL) -> Dict[str, Any]:
    """RUCKUS's recommendations at `level` as a flat map, for the editor's
    read-only reference column. Read straight off the loaded baseline."""
    lvl = _level(level)
    with RUCKUS._lock:
        RUCKUS._load()
        return dict(RUCKUS._values[lvl])


def ruckus_by_level() -> Dict[str, Dict[str, Any]]:
    """RUCKUS's recommendations for every level, for the editor to switch
    between without a round-trip per level."""
    with RUCKUS._lock:
        RUCKUS._load()
        return {lvl: dict(RUCKUS._values[lvl]) for lvl in LEVELS}

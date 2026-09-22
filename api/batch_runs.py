"""
The batch-run record: when each venue was last checked, and what it found.

THIS IS THE FOURTH FILE PISR WRITES, AND THE FIRST ONE THAT HOLDS SOMETHING
READ OFF A CUSTOMER'S NETWORK. The other three are configuration. This one
keeps, per venue, the COUNTS from the last check — how many punch-list items,
at which severity, in which trade — plus the venue's name and when it was run.
It is a deliberate, narrow exception to "PISR stores nothing of the tenant",
made because the feature it serves (re-run only what has not been checked in X
days; show a roll-up across every venue without re-polling every venue) cannot
exist without it. See CLAUDE.md.

  WHAT IT MAY HOLD: ids, names, timestamps, run status, who started a run, and
  integer counts. WHAT IT MUST NOT: a finding's text, its evidence, a device
  name, a serial, an address, a config value, an error body. Those are the
  report, and the report is still not persisted. `services/pisr/rollup.
  summarise` is the allowlist that decides what crosses into here, and
  `test_batch.py::test_summary_holds_only_counts` is what keeps it that way. If
  you want a finding's title in the roll-up, re-poll the venue — that is what
  the live report is for.

HUMAN-TRIGGERED, STILL. Nothing here runs by itself. The browser drives a batch
one venue per request (so no request outlives Cloudflare's 100-second origin
timeout), and this file only records what those requests found. A run whose tab
was closed simply stops; `_status` reads it as "interrupted" once it has been
quiet for `STALE_SECONDS`, rather than anything trying to finish it.

FAILS OPEN, LIKE THE VISIBILITY POLICY. An unreadable file means "no history":
every venue reads as never checked, which errs toward re-running a venue rather
than skipping one. It is NOT overwritten while broken — the next save would
otherwise replace a recoverable file with an empty one, and the history is the
only thing in it worth keeping.

One writer, one process: uvicorn runs a single worker (see the Dockerfile), and
nothing else touches this file, so an in-process lock is the whole of the
concurrency story. If PISR ever runs more than one worker, this needs the
`st_mtime_ns` re-read that `accounts.AccountStore` does.
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

# Runs kept in the file, newest first. A run record lists its venue ids, so an
# MSP-wide batch is a few kilobytes; two hundred of them is well under a
# megabyte and still years of use at one batch a day.
MAX_RUNS = 200

# A run nobody has reported on for this long is taken to be abandoned — the tab
# was closed, the laptop slept, the session expired. Longer than any single
# venue poll (those are bounded by Cloudflare at 100s) with room to spare.
STALE_SECONDS = 15 * 60

# What a venue's check can come back as. `ok` is the only one that counts as
# checked: `partial` means some R1 reads failed and the punch list was built on
# an incomplete picture, and `failed` means there is no punch list at all. Both
# leave the venue "not checked" for the re-run filter, which is the point.
VENUE_STATES = ("ok", "partial", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(stamp: Optional[str]) -> Optional[float]:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _venue_key(tenant_id: str, venue_id: str) -> str:
    return f"{tenant_id}/{venue_id}"


class BatchStore:
    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._empty()
        self.broken = False
        self._load()

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {"version": FORMAT_VERSION, "tenants": {}, "venues": {}, "runs": []}

    # ── state ────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def writable(self) -> bool:
        """Tested on the directory, as visibility.PolicyStore does and for the same reason."""
        if not self.path or self.broken:
            return False
        try:
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(
                "batch: could not read %s (%s). Every venue will read as never "
                "checked, and the file will NOT be overwritten until it is "
                "repaired or moved.", self.path, exc)
            self.broken = True
            return
        version = raw.get("version") if isinstance(raw, dict) else None
        if not isinstance(version, int) or version > FORMAT_VERSION:
            logger.error(
                "batch: %s declares version %r; this PISR writes %d. Leaving it "
                "alone rather than rewriting a newer file with older rules.",
                self.path, version, FORMAT_VERSION)
            self.broken = True
            return
        data = self._empty()
        for key in ("tenants", "venues"):
            if isinstance(raw.get(key), dict):
                data[key] = raw[key]
        if isinstance(raw.get("runs"), list):
            data["runs"] = [r for r in raw["runs"] if isinstance(r, dict)][:MAX_RUNS]
        self._data = data

    def _save(self) -> None:
        """Atomic replace in the same directory — see visibility.PolicyStore.save."""
        if not self.path:
            raise RuntimeError(
                "PISR_BATCH_FILE is not set, so there is nowhere to record a run.")
        if self.broken:
            raise RuntimeError(
                f"{self.path} could not be read, so it is not being overwritten. "
                "Repair or move it and restart the container.")
        body = json.dumps(self._data, indent=1, sort_keys=True) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".batch-", suffix=".tmp")
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
                f"Could not write the batch record to {self.path}: {exc}. Check "
                "the volume is mounted and writable by the container's user.") from exc

    # ── the venue directory ──────────────────────────────────────────

    def note_tenant(self, tenant_id: str, name: Optional[str],
                    venues: Iterable[Dict[str, Any]]) -> None:
        """
        Remember which venues a tenant HAS, not only which were checked.

        Without this a roll-up can only say "48 venues checked" and never "of
        58", which is the half of the sentence that matters. Replaced wholesale
        each time the batch dialog lists the tenant: a venue deleted in R1 drops
        out of the denominator, while its last check stays on record under
        `venues` in case it comes back.
        """
        directory = {str(v.get("id")): str(v.get("name") or v.get("id"))
                     for v in venues if isinstance(v, dict) and v.get("id")}
        with self._lock:
            self._data["tenants"][tenant_id] = {
                "name": name or (self._data["tenants"].get(tenant_id) or {}).get("name"),
                "venues": directory,
                "listedAt": _now(),
            }

    # ── runs ─────────────────────────────────────────────────────────

    def start_run(self, tenant_id: str, tenant_name: Optional[str],
                  venues: List[Dict[str, Any]], planned: List[str],
                  actor: str) -> Dict[str, Any]:
        self.note_tenant(tenant_id, tenant_name, venues)
        run = {
            "id": uuid.uuid4().hex[:12],
            "tenantId": tenant_id,
            "tenantName": tenant_name,
            "startedAt": _now(),
            "startedBy": actor,
            "updatedAt": _now(),
            "finishedAt": None,
            "stopped": False,
            "planned": list(dict.fromkeys(planned)),
            "results": {},
        }
        with self._lock:
            self._data["runs"].insert(0, run)
            del self._data["runs"][MAX_RUNS:]
            self._save()
        logger.info("batch: run %s started by %s on tenant %s — %d venue(s)",
                    run["id"], actor, tenant_id, len(run["planned"]))
        return self._describe(run)

    def _run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return next((r for r in self._data["runs"] if r.get("id") == run_id), None)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._run(run_id)
            return self._describe(run) if run else None

    def record_venue(self, run_id: str, venue_id: str, venue_name: Optional[str],
                     status: str, summary: Optional[Dict[str, Any]],
                     error: Optional[str]) -> Dict[str, Any]:
        """
        One venue's outcome. `lastComplete` moves only on `ok`.

        That asymmetry is the feature: a venue whose poll failed or came back
        partial keeps its previous complete check (and its date), so "not
        checked in 7 days" still selects it, and the roll-up still has the last
        trustworthy numbers to show beside a note that the latest attempt did
        not finish.
        """
        if status not in VENUE_STATES:
            raise ValueError(f"unknown venue state {status!r}")
        with self._lock:
            run = self._run(run_id)
            if run is None:
                raise KeyError(run_id)
            tenant_id = run["tenantId"]
            at = _now()
            attempt = {"at": at, "runId": run_id, "status": status,
                       "summary": summary,
                       # A short classification only — never an R1 response body.
                       "error": (error or None) and str(error)[:160]}
            key = _venue_key(tenant_id, venue_id)
            entry = self._data["venues"].get(key) or {
                "tenantId": tenant_id, "venueId": venue_id}
            entry["venueName"] = venue_name or entry.get("venueName") or venue_id
            entry["lastAttempt"] = attempt
            if status == "ok":
                entry["lastComplete"] = {"at": at, "runId": run_id, "summary": summary}
            self._data["venues"][key] = entry
            run["results"][venue_id] = status
            run["updatedAt"] = at
            self._save()
            return self._describe(run)

    def finish_run(self, run_id: str, stopped: bool) -> Dict[str, Any]:
        with self._lock:
            run = self._run(run_id)
            if run is None:
                raise KeyError(run_id)
            if not run.get("finishedAt"):
                run["finishedAt"] = _now()
                run["stopped"] = bool(stopped)
                run["updatedAt"] = run["finishedAt"]
                self._save()
            described = self._describe(run)
        logger.info("batch: run %s %s — %s", run_id, described["status"],
                    described["tally"])
        return described

    @staticmethod
    def _status(run: Dict[str, Any], tally: Dict[str, int]) -> str:
        """
        complete     every planned venue came back ok
        incomplete   finished (or stopped) with venues failed, partial or unrun
        running      still being reported on
        interrupted  nobody has reported on it for STALE_SECONDS

        Only `complete` is a clean bill. "48/58" is `incomplete` whether the
        other ten failed or were never reached; the tally says which.
        """
        planned = len(run.get("planned") or [])
        if tally["ok"] == planned and planned:
            return "complete"
        if run.get("finishedAt"):
            return "incomplete"
        age = _age_seconds(run.get("updatedAt"))
        if age is not None and age > STALE_SECONDS:
            return "interrupted"
        return "running"

    def _describe(self, run: Dict[str, Any]) -> Dict[str, Any]:
        results = run.get("results") or {}
        planned = run.get("planned") or []
        tally = {state: sum(1 for v in results.values() if v == state)
                 for state in VENUE_STATES}
        tally["notRun"] = sum(1 for vid in planned if vid not in results)
        return {
            "id": run.get("id"),
            "tenantId": run.get("tenantId"),
            "tenantName": run.get("tenantName"),
            "startedAt": run.get("startedAt"),
            "startedBy": run.get("startedBy"),
            "updatedAt": run.get("updatedAt"),
            "finishedAt": run.get("finishedAt"),
            "stopped": bool(run.get("stopped")),
            "planned": len(planned),
            "venueIds": list(planned),
            "results": dict(results),
            "tally": tally,
            "status": self._status(run, tally),
        }

    # ── reading, for the roll-up ─────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """A deep-enough copy for the roll-up to read without holding the lock."""
        with self._lock:
            return json.loads(json.dumps({
                "tenants": self._data["tenants"],
                "venues": self._data["venues"],
                "runs": [self._describe(r) for r in self._data["runs"]],
            }))


def _batch_file() -> Optional[str]:
    # Read here rather than added to config.AuthConfig: it is not an auth
    # setting, and threading it through the four AuthConfig constructors would
    # be four places to forget it. Same `_FILE` indirection as everything else.
    from config import _env
    return _env("PISR_BATCH_FILE") or "/data/batch-runs.json"


STORE = BatchStore(_batch_file())

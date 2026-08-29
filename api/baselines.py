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
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config import AUTH

logger = logging.getLogger(__name__)

_RUCKUS_FILE = Path(__file__).resolve().parent / "baselines" / "ruckus.json"

# Sentinel for "this baseline says nothing about this setting", which is
# different from "it recommends null".
MISSING = object()


class Baseline:
    """One set of recommended values, loaded from JSON and cached by mtime."""

    def __init__(self, path: Optional[Path], fallback_name: str):
        self.path = path
        self.fallback_name = fallback_name
        self._lock = threading.Lock()
        self._values: Dict[str, Any] = {}
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
            self._values, self._meta, self._stamp, self._loaded = {}, {}, None, True
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
        self._meta = {k: raw.get(k) for k in ("name", "status", "note", "source",
                                              "verifiedAt")}
        self._stamp, self._loaded = stamp, True

    def get(self, key: str) -> Any:
        with self._lock:
            self._load()
            return self._values.get(key, MISSING)

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
            }


RUCKUS = Baseline(_RUCKUS_FILE, "RUCKUS")
ORG = Baseline(Path(AUTH.org_baseline_file) if AUTH.org_baseline_file else None,
               AUTH.org_name)


def describe() -> Dict[str, Any]:
    """Both column headers, for the Config tab."""
    org = ORG.describe()
    # The org column is named from the environment, never from the file, so a
    # baseline copied between deployments cannot mislabel itself.
    org["name"] = AUTH.org_name
    return {"org": org, "ruckus": RUCKUS.describe()}


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
    ruckus = RUCKUS.get(key)
    if ruckus is not MISSING:
        out["ruckus"] = ruckus
    return out

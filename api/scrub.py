"""
The last thing every report passes through: strip anything secret.

WHY THIS EXISTS. RUCKUS ONE hands back live credentials in ordinary
configuration responses, with no marking and no opt-out:

    GET /venues/{id}/switchSettings   -> switchLoginPassword: "2VsU^Kd4D%"
    GET /venues/{id}/aps/{serial}     -> loginPassword: "0kzREJ#lp!31C*9*"

Those are the working admin passwords for a customer's switches and APs. A
report that carried them would put them in a JSON response, in a PDF that gets
emailed around, and in anyone's browser cache — for a tool whose whole job is
to be handed to an install crew. Nobody would notice until it mattered.

BELT AND BRACES, DELIBERATELY. The shapers already allowlist what they emit, so
in principle nothing secret reaches a report at all. This runs anyway, over the
whole payload, at the single point every report passes through, because:

  * the shapers are an allowlist maintained by people, and the next person to
    add a config block will pass through a dict they have not read every key of;
  * R1 adds fields to existing endpoints without warning, so a block that is
    clean today can carry a credential after a platform release;
  * the cost is one walk over a payload that is already being deep-copied.

An allowlist that is enforced twice is not redundant. It is an allowlist plus a
guarantee, and only the second one survives someone being in a hurry.

FAIL LOUD. Every removal is logged with its path. A silent scrub would hide the
fact that a shaper started leaking, which is the thing worth knowing — the
report being safe is the minimum, not the goal.

NOT A SUBSTITUTE FOR NOT FETCHING IT. If a config block exists only to be
scrubbed, do not fetch it. This catches what slips through; it does not make
reading credentials into the process acceptable.
"""

import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Two kinds of match, because one alone is either too crude or too narrow.
#
# The first version of this file matched a bare substring list containing
# "psk", and it redacted the whole `dpsk` card — "dpsk" contains "psk" — which
# emptied a section of the report and made the PDF fail to render. So a false
# positive is NOT a harmless trade here: it silently removes real content, and
# the guard meant to protect the report becomes the thing that breaks it.
#
# COMPOUND terms are specific enough to match anywhere in the flattened key,
# which they have to be: "switchLoginPassword" buries the credential word in
# the middle of a longer name.
SECRET_COMPOUNDS = (
    "password", "passwd", "passphrase", "secret", "credential",
    "apikey", "privatekey", "presharedkey", "sharedkey", "wepkey",
    "authkey", "sessionkey", "bearer", "signature",
)

# STANDALONE terms are short enough to appear inside innocent words, so they
# match only as a whole token, after splitting camelCase and separators.
# "psk" is a credential; "dpsk" is a product feature.
SECRET_TOKENS = frozenset({"psk", "pmk", "pwd", "token", "cookie"})

# Keys that trip the rules above but are configuration, not credentials.
# Matched on the FULL flattened key and checked first.
#
# Counts especially: "passphrases" on a DPSK evidence row is how many exist,
# which is exactly what an install review reports. `shape._dpsk_safe` already
# guarantees no passphrase VALUE is ever fetched, so a key named for them here
# can only be a number.
SAFE_KEYS = frozenset({
    "pskenabled", "haspsk", "passphraseenabled", "passphrasecount",
    "passphrases", "passphrasetotal", "passphrasecountsknown",
    "secretconfigured", "tokenexpiry", "keytype", "keymanagement",
    "keyexchange", "dpsk", "dpskenabled", "dpskpoolid", "dpskssids",
})

REDACTED = "«redacted»"

# How deep to walk before giving up. A report is a handful of levels; anything
# deeper is a cycle or a pathological payload, and recursing forever inside the
# one function that guarantees safety would be a poor way to fail.
_MAX_DEPTH = 40


def _tokens(key: str) -> List[str]:
    """`switchLoginPassword` -> [switch, login, password]. camelCase or any separator."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _is_secret(key: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if flat in SAFE_KEYS:
        return False
    if any(word in flat for word in SECRET_COMPOUNDS):
        return True
    # Plural stripped so "tokens" is caught with "token", without letting a
    # substring match reach inside an unrelated word.
    return any(token in SECRET_TOKENS or token.rstrip("s") in SECRET_TOKENS
               for token in _tokens(key))


def scrub(payload: Any, _path: str = "", _depth: int = 0,
          _removed: List[str] = None) -> Tuple[Any, List[str]]:
    """
    A copy of `payload` with every credential-shaped value replaced.

    Returns the scrubbed value and the list of paths that were redacted, so the
    caller can log or assert on them. Values are REPLACED rather than deleted,
    for the same reason redact.py empties rather than deletes: a renderer that
    reads a key which is normally present should find something there.

    Keys are matched, never values. Matching on value shape — "this looks like a
    password" — would redact hostnames, serials and hashes at random, and would
    still miss a weak password. The key name is what R1 actually tells us.
    """
    if _removed is None:
        _removed = []

    if _depth > _MAX_DEPTH:
        logger.error("scrub: hit max depth at %s; refusing to walk further and "
                     "dropping the branch rather than returning it unchecked.", _path)
        return None, _removed

    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            here = f"{_path}.{key}" if _path else str(key)
            if _is_secret(key):
                out[key] = REDACTED if value not in (None, "", [], {}) else value
                if value not in (None, "", [], {}):
                    _removed.append(here)
                continue
            out[key], _ = scrub(value, here, _depth + 1, _removed)
        return out, _removed

    if isinstance(payload, list):
        return [scrub(item, f"{_path}[]", _depth + 1, _removed)[0]
                for item in payload], _removed

    return payload, _removed


def scrub_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """
    Scrub a whole report and say so in the log if anything was found.

    A hit here is not routine. It means a shaper emitted a key it should not
    have, or R1 added one — either way somebody should look, so it is logged at
    WARNING with the paths and repeated on every report until it is fixed.
    """
    cleaned, removed = scrub(report)
    if removed:
        logger.warning(
            "scrub: removed %d credential-shaped field(s) from a report before "
            "it was served: %s. This should be empty — a shaper is emitting a "
            "key it should not, or R1 added one to an endpoint PISR reads.",
            len(removed), ", ".join(sorted(set(removed))[:12]))
    return cleaned

"""
Applying a visibility policy to an assembled report.

This is the enforcement point, and it is deliberately the ONLY one. Both
renderers — the React page and the PDF template — are handed the output of this
function and draw whatever they are given, so there is no path by which the PDF
can disagree with the screen about what a reader is allowed to see. That
matters more than it sounds: `/pisr/{cid}/report.pdf` re-polls the venue and
rebuilds the report from scratch rather than rendering the one the browser
already has, so a filter applied in the JSON route only would leave the PDF as
an unauthenticated-by-role copy of everything.

WHY EMPTY AND NOT DELETE. A hidden path is replaced with an empty value of the
same type, never removed. Two renderers in two languages read this payload with
several hundred `.length`, `.map`, `|length` and `.get(...)` calls between them,
most of them written when the key was guaranteed to exist. Deleting a key turns
every one of those into a potential blank page; emptying one turns them all
into an empty list, which every renderer here already handles because a venue
with no switches produces exactly that.

FINDINGS ARE THE PART PEOPLE FORGET. Checks read the whole report and are
rendered in one place, so hiding a section without dropping its findings leaves
the Verification card cheerfully reporting on the cards that are gone. Findings
owned by hidden sections are removed and the tallies recomputed, so the header
counts and the "18 of 24 checks passed" line stay true to what is shown rather
than to what was run.

Nothing here mutates its input. `build_report` is expensive and shared, and a
future caller that redacts twice for two different roles must not get a report
that has been emptied twice over.
"""

import logging
from collections import Counter
from typing import Any, Dict, Iterable, List

import scrub as secret_scrub
import sections as section_catalogue
from services.pisr import punchlist as punchlist_builder

logger = logging.getLogger(__name__)

# The five severities `checks.run_checks` tallies, in its order. Recomputed
# here rather than imported so `checks.py` stays a pure reader over the report
# and gains no knowledge that a visibility policy exists — findings are
# filtered by id from the outside, which is why a check can be renamed without
# anything here noticing (and why the drift test checks that it was not).
_SEVERITIES = ("critical", "warning", "info", "ok", "skipped")


def _blank_like(value: Any) -> Any:
    """
    An empty value of the same shape.

    Type-preserving on purpose — see the module docstring. `None` stays `None`
    rather than becoming `0`, because a null in this payload already means
    "not known" everywhere it appears and both renderers print it as an em dash.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    if isinstance(value, (list, tuple)):
        return []
    if isinstance(value, dict):
        return {}
    if isinstance(value, str):
        return ""
    if isinstance(value, (int, float)):
        return 0
    return None


def _blank_path(tree: Dict[str, Any], dotted: str) -> bool:
    """
    Empty one dotted path in place. Returns whether it found anything.

    A path that does not resolve is not an error at runtime — a report from a
    venue with no property config genuinely has no `venue.property` — but it IS
    worth a debug line, because the other reason a path stops resolving is that
    someone renamed a key in `shape.py` and this catalogue now silently
    protects nothing.
    """
    parts = dotted.split(".")
    node: Any = tree
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    leaf = parts[-1]
    if not isinstance(node, dict) or leaf not in node:
        return False
    node[leaf] = _blank_like(node[leaf])
    return True


def _blank_column(tree: Dict[str, Any], path: str, field: str) -> int:
    """
    Blank one FIELD in every row of the list at `path`. Returns rows touched.

    The column-level primitive: hide the VLAN column of the SSID table without
    hiding the table, by emptying `vlan` in each row while the rest of the row
    stays. Type-preserving like `_blank_path`, so a renderer indexing the field
    still finds a key of the same shape rather than a hole. A path that does not
    resolve to a list, or rows without the field, are left alone — the same
    fail-quiet as `_blank_path`, and the drift test is what catches a rename.
    """
    node: Any = tree
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return 0
        node = node[part]
    if not isinstance(node, list):
        return 0
    touched = 0
    for row in node:
        if isinstance(row, dict) and field in row:
            row[field] = _blank_like(row[field])
            touched += 1
    return touched


def _filter_findings(verification: Dict[str, Any], hidden_checks) -> None:
    """
    Drop findings owned by hidden sections and recompute both tallies in place.

    The counts are rebuilt rather than decremented so that this stays correct
    if `run_checks` ever emits a severity not in `_SEVERITIES`: a level nobody
    counted would simply not appear, which is the same thing `run_checks` does.
    """
    findings: List[Dict[str, Any]] = verification.get("findings") or []
    kept = [f for f in findings if f.get("id") not in hidden_checks]
    if len(kept) == len(findings):
        return

    verification["findings"] = kept
    tally = Counter(f.get("severity") for f in kept)
    verification["counts"] = {level: tally.get(level, 0) for level in _SEVERITIES}
    verification["score"] = {
        "passed": tally.get("ok", 0),
        "ran": len(kept) - tally.get("skipped", 0),
    }


def template_helpers(hidden_ids: Iterable[str]) -> Dict[str, Any]:
    """
    The two predicates the PDF template guards on.

    Passed into `template.render()` at the call site rather than added to
    `build_context`. The guards are a rendering concern: which sections a
    reader may see is decided per request, while the context builder shapes
    one report the same way every time. Keeping the two apart means a change
    to the policy never touches the shaping code.

    `visible(id)`      one section.
    `visible_tab(tab)` any section on that tab, for the <h2> that would
                       otherwise print a heading over nothing.

    Both answer True for an id the catalogue does not know, matching the
    fail-open rule everywhere else: a template guarding on a typo renders its
    section rather than silently dropping it, which is the failure you notice.
    """
    hidden = frozenset(hidden_ids)

    def visible(section_id: str) -> bool:
        return section_id not in hidden

    def visible_tab(tab: str) -> bool:
        on_tab = [s.id for s in section_catalogue.SECTIONS if s.tab == tab]
        return any(section_id not in hidden for section_id in on_tab) if on_tab else True

    return {"visible": visible, "visible_tab": visible_tab}


def redact(report: Dict[str, Any], hidden_ids: Iterable[str]) -> Dict[str, Any]:
    """
    A copy of `report`, scrubbed of credentials and with hidden sections emptied.

    Two different jobs, deliberately in one place because it is the one place
    every report passes through on its way out — the JSON route and the PDF
    route both call it, and neither can be served without it.

      * Credential scrubbing is unconditional and role-independent. Nobody
        gets a customer's switch password.
      * Section hiding is the visibility policy, and depends on the role.

    The resolved id list is stamped onto the result as `visibility.hidden` so
    the renderers can drop the containers as well as the contents — a card that
    would otherwise render its own "no rows" empty state, which reads as "there
    are none" rather than "you are not being shown these". That stamp is
    presentation; the emptying above it is the control.
    """
    hidden = sorted({sid for sid in hidden_ids if sid})

    # BEFORE anything else, and regardless of role. RUCKUS ONE returns live
    # switch and AP admin passwords inside ordinary configuration responses
    # (see api/scrub.py), and no role in this tool has any business receiving
    # one in a report. This is not part of the visibility policy — an admin is
    # not more entitled to a customer's switch password than a user is.
    #
    # It also deep-copies, which is why there is no separate copy below: scrub
    # rebuilds every dict and list it walks.
    redacted = secret_scrub.scrub_report(report)

    # Stamped even when nothing is hidden. A renderer that has to test for the
    # key's existence before reading it is a renderer with two code paths, and
    # the one that never runs in development is the one that breaks.
    redacted["visibility"] = {
        "hidden": hidden,
        # So a reader can tell "this venue has no DHCP pools" from "you are not
        # shown DHCP pools". The UI says so; the PDF prints it in the footer.
        "redacted": bool(hidden),
    }

    if not hidden:
        return redacted

    unknown = [vid for vid in hidden if not section_catalogue.is_known_id(vid)]
    if unknown:
        # Left over from an element (section, check or column) that was renamed
        # or removed. Ignored rather than refused: a stale id in the policy file
        # must not take the report down, and the alternative — treating it as a
        # wildcard — would hide more than the admin asked for.
        logger.warning("visibility: policy names %d unknown element(s), ignored: %s",
                       len(unknown), ", ".join(unknown))

    for path in section_catalogue.paths_for(hidden):
        if not _blank_path(redacted, path):
            logger.debug("visibility: path %s did not resolve in this report", path)

    # Config categories live in a LIST, so no dotted path can own one and the
    # generic path-emptying above cannot reach them. Filtered by slug instead,
    # which makes hiding a config category withhold the data rather than only
    # un-draw the card — the categories are the unit an admin was given to
    # hand subsets to users, so they should mean something in the payload.
    config = redacted.get("config")
    if isinstance(config, dict) and isinstance(config.get("categories"), list):
        config["categories"] = [
            category for category in config["categories"]
            if f"config.{category.get('slug')}" not in set(hidden)]

    # Findings dropped by id, from BOTH sources: a check owned by a hidden
    # section, and a check hidden INDIVIDUALLY in the portal tree. Same filter,
    # same tally recompute — the finding id is all `_filter_findings` needs.
    hidden_checks = (section_catalogue.checks_for(hidden)
                     | section_catalogue.loose_checks_for(hidden))
    if hidden_checks and isinstance(redacted.get("verification"), dict):
        _filter_findings(redacted["verification"], hidden_checks)

    # Column-level: blank a field in every row of a table, for a column hidden
    # on its own (the SSID table's VLAN, say). This withholds the data — the
    # column is emptied in the payload, not merely un-drawn — so the JSON route
    # and the PDF cannot disagree, and a `user` cannot read it back.
    for column in section_catalogue.columns_for(hidden):
        _blank_column(redacted, column.path, column.field)

    # The punch list is a re-cut of `verification` and `incidents`, so it is
    # REBUILT from the redacted copies rather than filtered alongside them.
    # Filtering would mean two implementations of the same rule and one of them
    # eventually falling behind — and the failure mode is a task naming a
    # finding the reader is no longer shown, which is exactly the leak this
    # module exists to prevent.
    #
    # Skipped when the punch list is itself hidden: its path has just been
    # emptied above, and rebuilding would quietly fill it back in.
    punchlist_hidden = any(path.split(".")[0] == "punchlist"
                           for path in section_catalogue.paths_for(hidden))
    if not punchlist_hidden and isinstance(redacted.get("punchlist"), dict):
        redacted["punchlist"] = punchlist_builder.build(
            redacted.get("verification") or {})

    return redacted

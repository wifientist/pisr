"""
Differential validation: every role's report against the administrator's.

WHY THIS EXISTS. Roles are data now, so the interesting question stopped being
"does redaction work" and became "does it work for ANY role somebody composes".
Four hand-written roles in the end-to-end suite prove the mechanism; they
cannot prove the 113-element space, and the bugs live in the space — a column
that is not blanked, a check that is not filtered, a path that is emptied when
it should not have been.

THREE THINGS MAKE THIS CHEAP:

  `redact.redact(report, hidden)` IS A PURE FUNCTION. Dict in, dict out, and it
  is the one place both the JSON route and the PDF route pass through. So this
  polls RUCKUS ONE zero times and runs in a second.

  THE FIXTURE IS GENERATED FROM `sections.py`, not captured. A real report
  cannot live in this repository — PRISM stores no report, and a captured one is
  a customer's network in git. Building it from the catalogue means every
  declared path carries a sentinel, the coverage is exhaustive by
  construction, and a section added next release is covered the day it is
  added rather than the day somebody remembers this file.

  THE ADMINISTRATOR'S REPORT IS THE ORACLE. The built-in `admin` role allows
  every element, so `redact(report, hidden_for("admin"))` is the 100%-assured
  variant every other role is measured against.

WHAT IT CANNOT PROVE. That `sections.py` declares the RIGHT paths for a real
payload — a section pointing at a key the shapers never emit would pass here
and hide nothing in production. `test_sections.py` holds the id lists together
and a live PDF render is what catches the rest.

Runs without pytest, like the others:

    docker compose -f docker-compose.dev.yml run --rm --no-deps \\
      -v "$PWD:/repo" -w /repo/api backend python tests/test_role_redaction.py
"""

import random
import sys
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import roles as role_rules          # noqa: E402
import sections                     # noqa: E402
from redact import redact           # noqa: E402

#: What a sentinel looks like at a leaf. A non-empty value of each kind
#: redaction has to recognise — `_blank_like` replaces a hidden path with an
#: EMPTY VALUE OF THE SAME TYPE, so the fixture has to offer it types to
#: preserve.
_MARKERS = ("kept", ["kept"], {"kept": True}, 7)


def _plant(tree: dict, dotted: str, value) -> None:
    """Put `value` at a dotted path, making the intermediate dicts."""
    parts = dotted.split(".")
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):        # two sections, one prefix
            return
    node[parts[-1]] = value


def _fixture() -> dict:
    """
    A report-shaped dict with a distinguishable value at every declared path.

    Not a real report and not pretending to be. It exists so that "was this
    path emptied" is a question with a yes/no answer for all 47 of them at
    once.
    """
    report: dict = {}
    for index, path in enumerate(sorted(
            {p for s in sections.SECTIONS for p in s.paths})):
        _plant(report, path, _MARKERS[index % len(_MARKERS)])

    # The verification card, shaped enough for `_filter_findings` to work on:
    # findings carry the check ids a role may or may not be allowed.
    report["verification"] = {
        "findings": [{"id": check, "severity": "warning", "title": check}
                     for check in sorted(sections.ALL_CHECK_IDS)],
        "counts": {"critical": 0, "warning": len(sections.ALL_CHECK_IDS),
                   "info": 0},
        "passed": 0,
    }
    # A table with a blankable column in every row — the SSID VLAN is the
    # worked example, and `_blank_column` is the primitive nothing else covers.
    for column in {c for s in sections.SECTIONS for c in s.columns}:
        rows = [{column.field: "kept", "other": "kept"} for _ in range(3)]
        _plant(report, column.path, rows)
    return report


def _hidden_for(visible) -> tuple:
    """The deny list a role allowing `visible` resolves to."""
    return role_rules.hidden_of(role_rules.Role(
        id="r_probe0001", label="Probe", grants=(), visible=tuple(visible)))


def _leaf_at(tree, dotted):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _empty(value) -> bool:
    return value in ("", [], {}, 0, None) or value is False


# ── the properties ──────────────────────────────────────────────────


def _check_role(report, visible, label):
    """
    One role: what it allows must survive, what it does not must not.

    Both directions, because they fail in opposite ways and only one of them
    is loud. Under-redaction is a LEAK — somebody sees a card they were not
    granted. Over-redaction is a BLANK PAGE — and under an allow model that is
    the likelier of the two, because an element nobody named is withheld by
    default and a coupling bug takes its neighbours with it.
    """
    hidden = set(_hidden_for(visible))
    out = redact(report, hidden)
    allowed = set(visible)

    for section in sections.SECTIONS:
        owns = {section.id, *section.checks, *(c.id for c in section.columns)}
        if section.id in hidden:
            for path in section.paths:
                # Emptied, NOT deleted. Two renderers call `.length`, `.map`
                # and `|length` on this payload assuming the keys exist;
                # deleting one turns a withheld card into a blank page.
                assert _leaf_at(out, path) is not None, (
                    f"{label}: {section.id} deleted {path!r} instead of "
                    "emptying it")
                assert _empty(_leaf_at(out, path)), (
                    f"{label}: {section.id} is withheld but {path!r} still "
                    "carries data — this is the leak direction")
        elif section.id in allowed and not (owns & hidden) - {section.id}:
            for path in section.paths:
                assert not _empty(_leaf_at(out, path)), (
                    f"{label}: {section.id} is allowed but {path!r} came back "
                    "empty — this is the blank-page direction")

    # A CHECK APPEARS ONLY IF ITS OWNING SECTION DOES, and that is not a bug
    # in either direction — it is the rule `redact` has always had, stated the
    # other way up. Findings are cross-cutting: hiding the PoE cards without
    # dropping their findings leaves the Verification card reporting on cards
    # that are not there, so a section takes its checks with it.
    #
    # Under an allow list that reads as: ticking a check on its own does
    # nothing until the card it belongs to is ticked too. The exhaustive pass
    # below found this on its first role, which is what it is for.
    owner = {check: section.id
             for section in sections.SECTIONS for check in section.checks}
    shown = {f["id"] for f in (out.get("verification") or {}).get("findings", [])}
    verification_shown = not _empty(_leaf_at(out, "verification"))
    for check in sections.ALL_CHECK_IDS:
        visible_here = (verification_shown
                        and check in allowed
                        and owner.get(check) in allowed)
        if visible_here:
            assert check in shown, (
                f"{label}: check {check} and its card {owner.get(check)!r} are "
                "both allowed, but it was filtered out")
        else:
            assert check not in shown, (
                f"{label}: check {check} is not reachable (allowed="
                f"{check in allowed}, card={owner.get(check)!r} allowed="
                f"{owner.get(check) in allowed}) but is still in the findings")

    for column in {c for s in sections.SECTIONS for c in s.columns}:
        rows = _leaf_at(out, column.path)
        if not isinstance(rows, list) or not rows:
            continue
        for row in rows:
            assert column.field in row, (
                f"{label}: column {column.id} was deleted from a row rather "
                "than blanked")
            if column.id in hidden:
                assert _empty(row[column.field]), (
                    f"{label}: column {column.id} is withheld but a row still "
                    "carries it")


def test_the_harness_is_not_passing_vacuously():
    """
    THE FAILURE MODE THAT WOULD MAKE EVERYTHING BELOW WORTHLESS.

    Every exhaustive pass here is a loop over `sections.ALL_ELEMENT_IDS`, and a
    loop over an empty set passes. So does a loop over a fixture where nothing
    was ever planted — the "is it withheld" assertions all hold trivially when
    there was nothing to withhold.

    Neither is far-fetched. The catalogue is derived, the fixture is derived
    from the catalogue, and a refactor that quietly broke either would turn
    this file into four hundred green ticks asserting nothing. So the floor is
    checked explicitly, and the fixture is checked against the catalogue it
    claims to cover.

    The numbers are floors, not counts: they must not need editing when a card
    is added, only when something has gone badly wrong.
    """
    assert len(sections.ALL_ELEMENT_IDS) > 80, (
        f"only {len(sections.ALL_ELEMENT_IDS)} elements in the catalogue — "
        "the exhaustive passes below are looping over almost nothing")
    assert len(sections.ALL_CHECK_IDS) > 20
    assert len(sections.SECTIONS) > 50

    report = _fixture()
    # EVERY declared path carries something. `_plant` gives up when two
    # sections' paths collide on a non-dict prefix, and a path it gave up on is
    # one this file silently stops testing.
    unplanted = sorted(path for section in sections.SECTIONS
                       for path in section.paths
                       if _empty(_leaf_at(report, path))
                       or _leaf_at(report, path) is None)
    assert not unplanted, (
        f"the fixture never planted {unplanted} — those paths are declared in "
        "sections.py but nothing here would notice if they leaked")

    # And every check is in the findings to begin with, or filtering them
    # would be filtering an empty list.
    assert len(report["verification"]["findings"]) == len(sections.ALL_CHECK_IDS)


def test_the_administrator_variant_is_the_full_report():
    """
    The oracle. The built-in `admin` role allows every element, so its variant
    must lose nothing — if it did, every comparison below would be measured
    against something already wrong.
    """
    report = _fixture()
    every = role_rules.builtins()["admin"].visible
    assert set(every) == set(sections.ALL_ELEMENT_IDS)

    out = redact(report, _hidden_for(every))
    for path in {p for s in sections.SECTIONS for p in s.paths}:
        assert not _empty(_leaf_at(out, path)), (
            f"the administrator's own report lost {path!r}")
    shown = {f["id"] for f in out["verification"]["findings"]}
    assert shown == set(sections.ALL_CHECK_IDS)


def test_a_role_allowing_nothing_keeps_the_shape():
    """
    The far end. A role that names nothing withholds all 113 elements — and
    must still return something both renderers can walk, because "empties
    rather than deletes" is what stops a withheld report being a crash.
    """
    report = _fixture()
    out = redact(report, _hidden_for(()))
    for path in {p for s in sections.SECTIONS for p in s.paths}:
        assert _leaf_at(out, path) is not None, f"{path!r} was deleted outright"
        assert _empty(_leaf_at(out, path)), f"{path!r} survived a role allowing nothing"
    # `verification` is itself an element, so withholding everything empties
    # the whole card to `{}` rather than leaving an empty findings list inside
    # it. Same-type, which is the contract — a dict becomes a dict.
    assert out["verification"] == {}


def test_every_element_alone():
    """
    EXHAUSTIVE AT THE SINGLE-ELEMENT LEVEL, allowing side.

    One role per element, allowing exactly that one. Catches the coupling bug
    this design is most exposed to: allowing X quietly requiring Y, so a role
    composed of one card renders nothing.
    """
    report = _fixture()
    for element in sorted(sections.ALL_ELEMENT_IDS):
        _check_role(report, {element}, f"only {element}")


def test_every_element_withheld_alone():
    """
    EXHAUSTIVE AT THE SINGLE-ELEMENT LEVEL, withholding side.

    One role per element, allowing everything EXCEPT that one. This is the
    direction that leaks: a path that is never emptied, a column that is
    never blanked, a check that is never filtered — each of which looks
    perfectly fine until the one role that withholds it exists.
    """
    report = _fixture()
    every = set(sections.ALL_ELEMENT_IDS)
    for element in sorted(every):
        _check_role(report, every - {element}, f"all but {element}")


def test_random_combinations():
    """
    Interactions, sampled. Pairs alone would be 113² roles; a seeded sample of
    mixed sizes finds the same class of bug for a hundredth of the work.

    SEEDED, so a failure is reproducible. An unseeded fuzz that fails once and
    never again is worse than no fuzz, because it trains people to re-run.
    """
    report = _fixture()
    every = sorted(sections.ALL_ELEMENT_IDS)
    rng = random.Random(20260923)
    for trial in range(200):
        size = rng.randint(1, len(every))
        _check_role(report, set(rng.sample(every, size)), f"random #{trial}")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n        {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

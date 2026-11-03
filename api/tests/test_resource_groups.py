"""
Resource groups: the second axis, and the one that fails closed.

WHAT THIS IS REALLY GUARDING. Scope used to answer "unrestricted" to every
question it could not otherwise answer — a missing file, an unknown role, a
person nobody had configured. The group model reverses that: reach is granted
by assignment, so an unplaced person reaches NOTHING. A reversal of a default
is the most dangerous kind of change, because every path that used to fall
through to "everything" now falls through to "nothing", and the two failures
look completely different from the outside:

  too open   somebody sees another customer's venues. Loud, and the thing the
             reversal exists to prevent.
  too shut   somebody sees an empty picker and files a bug about logging in.
             Quiet, blamed on auth, and what an upgrade would cause on every
             existing deployment at once if the bootstrap rule were missing.

So both directions are asserted here, and the bootstrap rule — groups do not
decide anything until a catalogue has been saved — gets more tests than the
happy path does.

NO PYTEST, like every suite here: a plain script counting its own failures.
It touches no network and no RUCKUS ONE tenant; `PolicyStore` is pointed at a
temporary file.

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" -w /repo/api backend python tests/test_resource_groups.py
"""

import json
import sys
import tempfile
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

import resource_groups as rg          # noqa: E402
import scope as scope_rules           # noqa: E402
import visibility                     # noqa: E402

NORTH = {"label": "North", "ecs": {"tenantA": "*", "tenantB": ["v1", "v2"]}}
SOUTH = {"label": "South", "ecs": {"tenantC": "*"}}


def _store(**policy):
    """A PolicyStore over a throwaway file holding `policy`."""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump({"version": visibility.FORMAT_VERSION, **policy}, handle)
    handle.close()
    return visibility.PolicyStore(handle.name)


# ── the module itself ────────────────────────────────────────────────

def test_union_combines_rather_than_replacing():
    """
    Two ticked groups mean both. The union rule rejected between a person and
    their ROLE is a precedence question; between two groups somebody chose it
    is the entire point, and a group model whose groups cannot combine is just
    the org chart with extra steps.
    """
    groups = rg.clean({"north": NORTH, "south": SOUTH})
    both = rg.union([groups["north"], groups["south"]])
    assert both.allows_ec("tenantA"), "lost the first group's customer"
    assert both.allows_ec("tenantC"), "lost the second group's customer"
    assert both.allows_venue("tenantB", "v1")
    assert not both.allows_venue("tenantB", "v9"), (
        "unioning two groups admitted a venue neither of them names")


def test_all_venues_absorbs_a_venue_list():
    """
    One group saying "every venue here" beside another naming three means
    every venue. Intersecting would make ticking a SECOND group take reach
    away, which is the opposite of what adding a box means.
    """
    groups = rg.clean({
        "wide": {"label": "Wide", "ecs": {"t": "*"}},
        "narrow": {"label": "Narrow", "ecs": {"t": ["v1"]}},
    })
    for order in ([groups["wide"], groups["narrow"]],
                  [groups["narrow"], groups["wide"]]):
        merged = rg.union(order)
        assert merged.allows_venue("t", "v-anything"), (
            "a narrow group cancelled a wide one, and order mattered")


def test_no_group_reaches_nothing():
    """The reversal, at its source. Empty is NONE, never all."""
    nothing = rg.union([])
    assert not nothing.unrestricted
    assert not nothing.allows_ec("tenantA")


def test_a_deleted_group_confers_nothing():
    """
    Same rule as a deleted ROLE. Somebody was deliberately given a territory;
    losing the definition must not widen them, and must not quietly drop the
    assignment either — the id is kept so an admin can see what broke.
    """
    assert not rg.union([None]).allows_ec("tenantA")
    kept = rg.clean_assignment(["north", "ghost"], known=rg.clean({"north": NORTH}))
    assert kept == ["north", "ghost"], (
        "an assignment naming a deleted group was silently dropped, which "
        "hides the mistake instead of surfacing it")


def test_the_builtin_is_compiled_in_not_stored():
    """
    `all-access` resolves even against an empty — or unreadable — policy, for
    the reason `roles._BUILTINS` does: if every route to the estate came from
    a file on a volume, a damaged file would leave nobody able to repair it.
    """
    assert rg.BUILTIN_ALL in rg.resolve({})
    assert rg.union([rg.builtins()[rg.BUILTIN_ALL]]).unrestricted
    assert rg.BUILTIN_ALL in rg.undeletable()


def test_all_access_cannot_be_expressed_as_ids():
    """
    The built-in's whole job: a venue that does not exist yet. A group of ids
    says no to it; `allAccess` says yes. If these ever answer the same, the
    flag has become decoration.
    """
    ids = rg.clean({"named": {"label": "Named", "ecs": {"t": "*"}}})["named"]
    assert not rg.union([ids]).allows_ec("tenant-created-tomorrow")
    assert rg.union([rg.builtins()[rg.BUILTIN_ALL]]).allows_ec("tenant-created-tomorrow")


def test_venue_count_refuses_to_guess():
    """
    "All venues" is not a number and must not render as one. A count beside a
    group is read as "this is how much reach it grants", and printing 0 for
    "however many there are" is the worst available answer.
    """
    groups = rg.clean({"north": NORTH, "exact": {"label": "E", "ecs": {"t": ["a", "b"]}}})
    assert groups["north"].venue_count() is None, "counted a wildcard"
    assert groups["exact"].venue_count() == 2
    assert rg.builtins()[rg.BUILTIN_ALL].venue_count() is None


def test_a_group_with_no_usable_id_is_dropped():
    """Dropped rather than repaired — an invented id is a territory nobody drew."""
    assert rg.clean({"": NORTH}) == {}
    assert rg.clean({"Bad Id!": NORTH}) == {}
    assert rg.clean({"x": NORTH}) == {}, "a one-character id is not a slug"
    assert rg.clean_one({"id": "ok-id", "label": "x"}) is not None


# ── the bootstrap rule ───────────────────────────────────────────────

def test_groups_decide_nothing_until_a_catalogue_is_saved():
    """
    THE UPGRADE GUARD, and the most important test here. Every existing
    deployment has people with no group. If the model switched on the moment
    this code shipped, all of them would see nothing on deploy morning — an
    outage that reads as an authentication failure. So scope keeps answering
    the old way until somebody saves a group.
    """
    store = _store(people={"dana": {"role": "user"}})
    assert not store.groups_in_force
    assert store.scope_for("dana").unrestricted, (
        "a pre-groups policy stopped being unrestricted, which turns an "
        "upgrade into an outage")


def test_the_legacy_rules_still_answer_while_groups_are_off():
    """Person entry beats role default beats unrestricted, exactly as before."""
    store = _store(
        scope={"user": {"unrestricted": False, "ecs": {"roleTenant": "*"}}},
        people={"dana": {"role": "user",
                         "scope": {"unrestricted": False,
                                   "ecs": {"danaTenant": "*"}}},
                "sam": {"role": "user"}})
    assert store.scope_for("dana").allows_ec("danaTenant")
    assert not store.scope_for("dana").allows_ec("roleTenant"), (
        "a person's entry was unioned with their role's instead of replacing it")
    assert store.scope_for("sam").allows_ec("roleTenant"), (
        "somebody with no entry of their own lost their role's default")


def test_saving_a_group_turns_the_model_on():
    store = _store()
    store.save({}, None, "tester", roles={}, people={},
               resource_groups={"north": NORTH})
    assert store.groups_in_force


def test_the_role_is_not_consulted_once_groups_are_on():
    """
    Roles say what somebody may RUN; groups say whose network they may run it
    on. A role default silently surviving into the group model would be the
    two axes quietly recoupling — the thing this split exists to prevent.
    """
    store = _store(
        resourceGroups={"north": NORTH},
        scope={"user": {"unrestricted": False, "ecs": {"roleTenant": "*"}}},
        people={"sam": {"role": "user", "groups": []}})
    assert store.groups_in_force
    assert not store.scope_for("sam").allows_ec("roleTenant"), (
        "a role's scope default still applied after groups took over")


# ── resolution, once they are on ─────────────────────────────────────

def test_an_unplaced_person_reaches_nothing():
    store = _store(resourceGroups={"north": NORTH},
                   people={"dana": {"role": "user", "groups": ["north"]}})
    reached = store.scope_for("nobody-placed-them")
    assert not reached.unrestricted and not reached.ecs, (
        "somebody with no group was not refused, which is the whole reversal")


def test_an_assigned_person_reaches_their_groups():
    store = _store(resourceGroups={"north": NORTH, "south": SOUTH},
                   people={"dana": {"groups": ["north", "south"]}})
    dana = store.scope_for("dana")
    assert dana.allows_ec("tenantA") and dana.allows_ec("tenantC")
    assert not dana.allows_ec("tenantZ")


def test_an_explicit_empty_list_is_not_the_same_as_no_list():
    """
    Both refuse everything, so this is about what the PORTAL can say. `[]` is
    an admin deciding somebody reaches nothing; absent is somebody nobody has
    got to yet. Collapsing them loses the difference between a decision and
    an omission, which is the same reason `baselines` keeps N.A. apart from
    unreviewed.
    """
    store = _store(resourceGroups={"north": NORTH},
                   people={"decided": {"groups": []}, "forgotten": {"role": "user"}})
    policy = store.policy()
    assert policy["people"]["decided"]["groups"] == []
    assert policy["people"]["forgotten"]["groups"] is None


def test_an_admin_with_no_group_is_refused_venues():
    """
    No short-circuit, deliberately. The portal that fixes it is gated on the
    ROLE and never on scope, so this is recoverable rather than a lockout —
    `scope.spec.ts` covers that half end to end.
    """
    store = _store(resourceGroups={"north": NORTH},
                   people={"boss": {"role": "admin"}})
    assert not store.scope_for("boss").allows_ec("tenantA")


def test_passphrase_mode_never_fails_closed():
    """
    That mode has no identities, so no assignment can ever match. Applying the
    reversal there would leave PRISM's DEFAULT auth mode showing every user
    nothing, for ever, with no screen that could repair it.
    """
    store = _store(resourceGroups={"north": NORTH})
    assert store.scope_for(None).unrestricted, (
        "a caller with no identity was scoped out, which breaks passphrase mode")


# ── writing ──────────────────────────────────────────────────────────

def test_a_saved_group_takes_effect_without_a_restart():
    """
    `save` refreshes the cache IN PLACE and advances `_stamp`, so a field it
    forgets is correct on disk and wrong in memory until the container
    restarts. `_people` was that bug once and `_migrate_hidden` was it twice;
    `_groups` is the third field with the same shape.
    """
    store = _store()
    store.save({}, None, "tester", roles={}, people={"dana": {"groups": ["north"]}},
               resource_groups={"north": NORTH})
    assert store.scope_for("dana").allows_ec("tenantA"), (
        "a group saved in this process was not in force until a restart")


def test_the_builtin_is_not_written_back():
    """
    A portal echoing the resolved catalogue must not persist a copy of
    `all-access`, or the compiled-in one stops being the thing that answers
    and an unreadable file has no fallback left.
    """
    store = _store()
    store.save({}, None, "tester", roles={}, people={},
               resource_groups={**{"north": NORTH},
                                rg.BUILTIN_ALL: rg.builtins()[rg.BUILTIN_ALL].public()})
    written = json.loads(Path(store.path).read_text())
    assert rg.BUILTIN_ALL not in written.get("resourceGroups", {}), (
        "the compiled-in group was persisted onto the volume")
    assert "north" in written["resourceGroups"]


def test_the_rollback_shadow_is_written_and_is_never_wider():
    """
    Groups ride under a key an older PRISM cannot see, so the territory they
    resolve to is ALSO written where that older PRISM already looks. Without
    it a rollback reads "no scope entry" as unrestricted, which is the one
    direction this control must never fail in.
    """
    store = _store()
    store.save({}, None, "tester", roles={},
               people={"dana": {"groups": ["north"]},
                       "wide": {"groups": [rg.BUILTIN_ALL]},
                       "none": {"groups": []}},
               resource_groups={"north": NORTH})
    written = json.loads(Path(store.path).read_text())["people"]

    shadow = written["dana"]["scope"]
    assert shadow["ecs"]["tenantA"] == "*", "the shadow lost a customer"
    assert sorted(shadow["ecs"]["tenantB"]) == ["v1", "v2"]

    # all-access resolves to "no restriction", which is what an older PRISM
    # would already have done for somebody with no entry. Nothing is given
    # away by it, and writing an id list instead would be NARROWER than the
    # group and would go stale.
    assert "scope" not in written["wide"]

    # And the fail-closed case survives the rollback as itself.
    assert written["none"]["scope"] == {"unrestricted": False, "ecs": {}}


def test_the_shadow_leaves_pre_groups_people_alone():
    """
    Somebody carried forward with a scope entry and no groups has not been
    placed yet. Resolving a territory for them would be the store guessing,
    and the guess would be "nothing".
    """
    store = _store(people={"legacy": {"scope": {"unrestricted": False,
                                                "ecs": {"old": "*"}}}})
    store.save({}, None, "tester", roles={},
               people={"legacy": {"scope": {"unrestricted": False,
                                            "ecs": {"old": "*"}}}},
               resource_groups={"north": NORTH})
    written = json.loads(Path(store.path).read_text())["people"]
    assert written["legacy"]["scope"]["ecs"] == {"old": "*"}, (
        "a person who predates groups had their scope overwritten")


def test_the_policy_names_what_is_broken():
    """
    An assignment to a deleted group confers nothing and has no visible cause
    from the user's seat, so the portal is told rather than left to guess.
    """
    store = _store(resourceGroups={"north": NORTH},
                   people={"dana": {"groups": ["north", "ghost"]}})
    policy = store.policy()
    assert policy["orphanedGroups"] == {"dana": ["ghost"]}
    assert policy["groupsInForce"] is True
    assert {g["id"] for g in policy["resourceGroups"]} == {"north", rg.BUILTIN_ALL}


def test_scope_clean_and_groups_agree_about_an_empty_venue_list():
    """
    A group reusing `scope.clean` is the point: one validator, so the two
    cannot drift about what an empty venue list means. Empty is NO venues.
    """
    group = rg.clean({"empty": {"label": "Empty", "ecs": {"t": []}}})["empty"]
    reached = rg.union([group])
    assert reached.allows_ec("t"), "the customer itself should still be named"
    assert not reached.allows_venue("t", "anything"), (
        "an empty venue list was read as every venue")
    assert scope_rules.ALL_VENUES == "*"


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

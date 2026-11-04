import { useMemo, useState, type ReactNode } from "react";
import { Globe, Layers, TriangleAlert } from "lucide-react";
import {
  ALL_VENUES, type EcRow, type Resolved, type ResourceGroup,
  type ScopeCatalogue,
} from "@/hooks/useScopeCatalogue";

/**
 * ONE CUSTOMER's people — the by-customer direction.
 *
 * WHY BOTH DIRECTIONS EXIST. An administrator asks two questions about reach
 * and only one of them was answerable. "Dana changed territories" is
 * per-person. "We just won this customer, who works on it?" and "who can see
 * this customer?" are per-customer, and answering either used to mean opening
 * all forty people in turn and remembering what you saw. The second question
 * is also the one an audit asks.
 *
 * IT IS A TRANSPOSITION, NOT A SECOND STORE. Reach is people[x].groups against
 * the group catalogue, and this view reads and writes exactly that — so there
 * is nothing to keep in step.
 *
 * NOT A GRID, DELIBERATELY. The obvious rendering of "people × customers" is a
 * matrix, and it was the first design. Twenty customers against forty people
 * is eight hundred cells and some 3,500px of name columns, so the one screen
 * that is supposed to let you scan the estate is the one you read by
 * scrolling sideways. Two lists and a direction toggle keep every axis
 * vertical at any size.
 *
 * THREE ANSWERS TO "CAN THEY OPEN THIS?", and each row NAMES THE GROUP that
 * lets them in — because "how do I stop them" is never "untick this person"
 * under groups: it is taking a group off them, or the customer out of a
 * group, and those have very different blast radii.
 *
 *   reaches    one of their groups covers this customer.
 *   not here   they hold groups, none of which covers it.
 *   unplaced   they hold no group at all, so they reach nothing anywhere.
 *
 * Only rendered once groups are switched on; before that everybody reaches
 * every customer and there is nothing to read by customer.
 */

export default function AdminScopeByCustomer({
  ec, catalogue, roster, resolvedOf, groupsOf, groupById, allGroups,
  roleLabelOf, onChangeGroups, onPickPerson, onPickGroup,
}: {
  ec: EcRow;
  catalogue: ScopeCatalogue;
  /** Every identity this portal knows about. */
  roster: string[];
  /** What they actually reach and why — `resolvedOf` in AdminRoles. */
  resolvedOf: (identity: string) => Resolved;
  /** Their assigned groups: null means never placed, [] means nothing. */
  groupsOf: (identity: string) => string[] | null;
  groupById: Record<string, ResourceGroup>;
  allGroups: ResourceGroup[];
  roleLabelOf: (identity: string) => string | null;
  onChangeGroups: (identity: string, next: string[] | null) => void;
  /** Jump to this person in the by-person direction. */
  onPickPerson: (identity: string) => void;
  /** Jump to this group's definition. */
  onPickGroup: (groupId: string) => void;
}) {
  const { venues, loadVenues } = catalogue;

  /** The groups that cover this customer at all. */
  const covering = useMemo(
    () => allGroups.filter((g) => g.allAccess || ec.id in (g.ecs || {})),
    [allGroups, ec.id]);
  const coveringIds = useMemo(() => new Set(covering.map((g) => g.id)), [covering]);

  const groups = useMemo(() => {
    const reaches: string[] = [];
    const not: string[] = [];
    const unplaced: string[] = [];
    for (const identity of roster) {
      const held = groupsOf(identity);
      if (held === null) { unplaced.push(identity); continue; }
      if (held.some((g) => coveringIds.has(g))) reaches.push(identity);
      else not.push(identity);
    }
    return { reaches, not, unplaced };
  }, [roster, groupsOf, coveringIds]);

  const rowsForEc = venues[ec.id];
  const venueCount = Array.isArray(rowsForEc) ? rowsForEc.length : null;

  /**
   * WHICH GROUP LETS THEM IN, named on the row.
   *
   * The question this screen answers is "who can open this customer", and
   * half of a useful answer is "and how do I stop them". Under a group model
   * that is never "untick this person" — it is either taking the group off
   * them or taking the customer out of the group, and those have very
   * different blast radii. Naming the group is what lets an admin tell.
   */
  const via = (identity: string) =>
    (groupsOf(identity) || []).filter((g) => coveringIds.has(g));

  const venueSummary = (identity: string) => {
    const { scope } = resolvedOf(identity);
    if (scope.unrestricted) return "all venues";
    const current = scope.ecs[ec.id];
    if (current === ALL_VENUES) return "all venues";
    const n = Array.isArray(current) ? current.length : 0;
    if (!n) return "no venues";
    return venueCount === null ? `${n} venue(s)` : `${n} of ${venueCount} venues`;
  };

  const roleChip = (identity: string) => {
    const label = roleLabelOf(identity);
    return label
      ? <span className="shrink-0 text-[11px] text-gray-400">{label}</span>
      : null;
  };

  /** Add one covering group to somebody who does not reach this customer. */
  const [adding, setAdding] = useState<string | null>(null);

  return (
    <div className="space-y-4">
      <p className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-600">
        Who can open <b>{ec.name}</b> — <b>{groups.reaches.length}</b> of{" "}
        {roster.length} {roster.length === 1 ? "person" : "people"}, through{" "}
        <b>{covering.length}</b> resource group(s). Nobody reaches a customer
        individually here: they hold a group that covers it.
      </p>

      {/*
        THE GROUPS FIRST, BECAUSE THEY ARE THE LEVER. Showing only people
        would invite the question this model does not answer — "untick
        them" — when the two real edits are take the group off the person,
        or take the customer out of the group.
      */}
      <Group title={`Groups covering ${ec.name}`}
             count={covering.length}
             hint="Edit one of these and everybody holding it moves."
             empty={"No group covers this customer, so nobody can open it. "
                    + "That is correct if it is deliberate — it is also what a "
                    + "newly onboarded customer looks like."}>
        {covering.map((group) => {
          const holders = groups.reaches.filter((i) => via(i).includes(group.id));
          return (
            <div key={group.id}
                 className="flex min-w-0 flex-wrap items-center gap-2 rounded-md
                            border border-gray-200 px-3 py-2">
              {group.allAccess
                ? <Globe size={13} className="shrink-0 text-gray-400" />
                : <Layers size={13} className="shrink-0 text-gray-400" />}
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-gray-900">
                {group.label}
              </span>
              <span className="shrink-0 text-xs text-gray-400">
                {group.allAccess ? "every customer" : "all venues here"}
                {` · ${holders.length} person(s)`}
              </span>
              <button type="button" onClick={() => onPickGroup(group.id)}
                      className="shrink-0 text-xs text-blue-700 underline-offset-2
                                 hover:underline">
                Edit group
              </button>
            </div>
          );
        })}
      </Group>

      <Group title={`Can open ${ec.name}`}
             count={groups.reaches.length}
             hint="Through the group named on their row."
             empty="Nobody holds a group that covers this customer.">
        {groups.reaches.map((identity) => (
          <div key={identity}
               className="flex min-w-0 flex-wrap items-center gap-2 rounded-md
                          border border-gray-200 px-3 py-2">
            <span className="min-w-0 flex-1 truncate text-sm text-gray-900">
              {identity}
            </span>
            {roleChip(identity)}
            <span className="shrink-0 text-[11px] text-gray-500">
              via <b>{via(identity).map((g) => groupById[g]?.label ?? g).join(", ")}</b>
            </span>
            <span className="shrink-0 text-xs text-gray-400">
              {venueSummary(identity)}
            </span>
            <button type="button" onClick={() => onPickPerson(identity)}
                    className="shrink-0 text-xs text-blue-700 underline-offset-2
                               hover:underline">
              Open
            </button>
          </div>
        ))}
      </Group>

      <Group title="Cannot open it"
             count={groups.not.length}
             hint={covering.length
               ? "Give them a group that covers this customer."
               : "No group covers this customer, so there is nothing to give."}
             empty={null}>
        {groups.not.map((identity) => (
          <div key={identity}
               className="flex min-w-0 flex-wrap items-center gap-2 rounded-md
                          border border-gray-200 px-3 py-2">
            <span className="min-w-0 flex-1 truncate text-sm text-gray-900">
              {identity}
            </span>
            {roleChip(identity)}
            {adding === identity && covering.length > 0 ? (
              <select autoFocus defaultValue=""
                      onChange={(e) => {
                        if (!e.target.value) return;
                        onChangeGroups(identity,
                                       [...(groupsOf(identity) || []), e.target.value]);
                        setAdding(null);
                        loadVenues(ec.id);
                      }}
                      onBlur={() => setAdding(null)}
                      className="min-w-0 shrink-0 rounded-md border border-gray-300
                                 px-2 py-1 text-xs">
                <option value="">Add a group…</option>
                {covering
                  .filter((g) => !(groupsOf(identity) || []).includes(g.id))
                  .map((g) => (
                    <option key={g.id} value={g.id}>{g.label}</option>
                  ))}
              </select>
            ) : (
              <button type="button" disabled={!covering.length}
                      onClick={() => setAdding(identity)}
                      className="shrink-0 text-xs text-blue-700 underline-offset-2
                                 hover:underline disabled:text-gray-300
                                 disabled:no-underline">
                Give them a group
              </button>
            )}
          </div>
        ))}
      </Group>

      {/*
        UNPLACED IS ITS OWN GROUP, not folded into "cannot open it". Both
        refuse, but one of them is somebody nobody has got to yet — the state
        a newly created account lands in, and the one that gets reported as
        "I log in and see nothing". Folding it away would hide the backlog.
      */}
      <Group title="Not placed yet"
             count={groups.unplaced.length}
             hint={"They hold no resource group at all, so they can open "
                   + "nothing anywhere — not just here."}
             empty={null}>
        {groups.unplaced.map((identity) => (
          <div key={identity}
               className="flex min-w-0 flex-wrap items-center gap-2 rounded-md
                          border border-amber-200 bg-amber-50 px-3 py-2">
            <TriangleAlert size={13} className="shrink-0 text-amber-600" />
            <span className="min-w-0 flex-1 truncate text-sm text-gray-900">
              {identity}
            </span>
            {roleChip(identity)}
            <button type="button" onClick={() => onPickPerson(identity)}
                    className="shrink-0 text-xs text-blue-700 underline-offset-2
                               hover:underline">
              Assign groups
            </button>
          </div>
        ))}
      </Group>
    </div>
  );
}

function Group({ title, count, hint, empty, children }: {
  title: string;
  count: number;
  hint?: string;
  empty: string | null;
  children: ReactNode;
}) {
  // An empty group with no `empty` line is three lines of heading and
  // explanation about nobody. The groups that CAN be empty and still matter —
  // "nobody is selected for this customer" — say so through `empty`.
  if (count === 0 && !empty) return null;
  return (
    <div className="min-w-0">
      <p className="text-xs font-medium text-gray-700">
        {title} <span className="font-normal text-gray-400">· {count}</span>
      </p>
      {hint && <p className="mt-0.5 text-[11px] text-gray-500">{hint}</p>}
      {count === 0
        ? (empty && <p className="mt-1.5 text-xs text-gray-500">{empty}</p>)
        : <div className="mt-1.5 space-y-1">{children}</div>}
    </div>
  );
}

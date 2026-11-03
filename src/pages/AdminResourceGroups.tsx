import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Globe, Layers, TriangleAlert } from "lucide-react";
import AdminScope from "@/pages/AdminScope";
import {
  asScope, type EcRow, type ResourceGroup, type ScopeCatalogue,
} from "@/hooks/useScopeCatalogue";

/**
 * The resource-group CATALOGUE: define the territories, once.
 *
 * THE SECOND AXIS, AND IT IS NOT THE ROLE. A role says what somebody may RUN
 * and SEE; a group says whose network they may run it on. Neither carries the
 * other, deliberately — an earlier shape had the role carry a default
 * territory, and it was removed because "which reports may Dana run" and
 * "whose sites may she run them on" are answered by different people on
 * different days.
 *
 * WHY NAMED GROUPS AT ALL. Scope used to be a bespoke customer/venue map on
 * every individual: forty people meant forty hand-kept lists with nothing
 * holding them together, no way to ask "who is on the North estate", and
 * onboarding done by copying somebody else's ticks by eye. Define it once,
 * review it once, assign it — the same argument that collapsed roles, tags
 * and profiles into one concept.
 *
 * THE CATALOGUE FAILS CLOSED AND THE MODEL SWITCHES ON WITH IT. Until the
 * first group is saved, scope answers the way it always did (person entry,
 * else role default, else unrestricted). The moment a catalogue exists,
 * groups decide — and somebody with no group reaches NOTHING. That reversal
 * is why the banner below exists and why saving the first group is called out
 * rather than being an ordinary save: it changes what every unassigned person
 * on the deployment can see, at once.
 *
 * `all-access` IS COMPILED IN, not stored, for the reason the built-in roles
 * are: an unreadable policy file must still leave somebody able to reach the
 * estate and repair it. It is the only group that can say "and ones added
 * later", which no list of ids can.
 */

export function groupSummary(
  group: ResourceGroup, nameOf: (id: string) => string,
): string {
  if (group.allAccess) return "every customer and venue";
  const ids = Object.keys(group.ecs || {});
  if (!ids.length) return "nothing selected";
  const partial = ids.some((id) => Array.isArray(group.ecs[id]));
  const named = ids.length <= 2
    ? ids.map(nameOf).join(", ")
    : `${nameOf(ids[0])} + ${ids.length - 1} more`;
  return partial ? `${named} · some venues` : named;
}

export default function AdminResourceGroups({
  groups, onChange, catalogue, undeletable, holdersOf, inForce, readOnly,
  onBuildFromScopes, carryOverCount,
}: {
  groups: ResourceGroup[];
  onChange: (next: ResourceGroup[]) => void;
  catalogue: ScopeCatalogue;
  /** Ids that always resolve, so Delete is a reset rather than a removal. */
  undeletable: string[];
  holdersOf: (groupId: string) => string[];
  /** Whether groups already decide scope here. False before the first save. */
  inForce: boolean;
  readOnly: boolean;
  /** Propose a catalogue from the per-person lists in force today. */
  onBuildFromScopes: () => void;
  /** How many people that would place. */
  carryOverCount: number;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const { rows, nameOf } = catalogue;

  const setGroup = (id: string, patch: Partial<ResourceGroup>) =>
    onChange(groups.map((g) => (g.id === id ? { ...g, ...patch } : g)));

  const add = () => {
    const id = `g_${Math.random().toString(36).slice(2, 12)}`;
    // STARTS EMPTY, which is where deny-by-default belongs: a new group
    // should cover nothing until somebody says what it is for. The amber
    // warning inside the tree says so rather than letting it look finished.
    onChange([...groups, {
      id, label: "New group", description: "", allAccess: false, ecs: {},
    }]);
    setOpen(id);
  };

  const shell = "rounded-md border border-gray-300 px-2.5 py-1.5 text-xs " +
                "font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50";

  /**
   * Customers no group names.
   *
   * THE DRIFT CHECK, and the counterpart to the stale-id warning inside the
   * tree. A customer in no group is one nobody can open — which is the
   * correct behaviour for a fail-closed model and is indistinguishable, from
   * a user's seat, from PISR being broken. Onboarding a customer and
   * forgetting this screen is the obvious way to arrive there, so the screen
   * says it rather than waiting for the support call.
   */
  const uncovered = useMemo(() => {
    if (!inForce || !rows.length) return [];
    if (groups.some((g) => g.allAccess)) return [];
    const named = new Set<string>();
    for (const g of groups) for (const id of Object.keys(g.ecs || {})) named.add(id);
    return rows.filter((r: EcRow) => !named.has(r.id));
  }, [inForce, rows, groups]);

  return (
    <div className="space-y-3">
      <p className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                    text-xs text-gray-600">
        A resource group is a <b>territory</b>: the customers and venues it
        covers. Define it once here, then assign it to people on the{" "}
        <b>Customer access</b> tab — somebody holding several gets all of them
        together.
        <br />
        This is <b>not</b> a role. A role decides which reports and tools
        somebody has; a group decides whose network they may point them at.
      </p>

      {!inForce && (
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2">
          <p className="flex items-start gap-1.5 text-xs text-amber-900">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            <span>
              <b>Groups are not deciding anything yet.</b> Until the first one
              is saved, access works the old way — each person's own customer
              list, and everybody else unrestricted. Saving a group{" "}
              <b>switches that over</b>: from then on anybody with no group
              assigned can open <b>no venue at all</b>.
            </span>
          </p>
          {/*
            THE ON-RAMP. Fail-closed plus an empty catalogue is an outage, so
            the screen that switches the model on is also the screen that
            offers to carry today's access across. Nothing is written until
            Save, so this is a proposal an administrator reads.
          */}
          <button type="button" onClick={onBuildFromScopes} disabled={readOnly}
                  className="mt-2 rounded-md border border-amber-400 bg-white px-2.5
                             py-1 text-xs font-medium text-amber-900
                             hover:bg-amber-100 disabled:opacity-50">
            Carry the current access across
          </button>
          <p className="mt-1 text-[11px] text-amber-800">
            Builds a group for each distinct territory people already have —
            identical lists become <b>one</b> group, which is usually the first
            time that duplication is visible — and gives everybody who was
            unrestricted <b>All access</b>. {carryOverCount} person(s) would be
            placed. Review and Save, or close the dialog to discard.
          </p>
        </div>
      )}

      {uncovered.length > 0 && (
        <p className="flex items-start gap-1.5 rounded-md border border-amber-200
                      bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          <span>
            <b>{uncovered.length} customer(s) are in no group</b>, so nobody can
            open them: {uncovered.slice(0, 4).map((r) => r.name).join(", ")}
            {uncovered.length > 4 ? ", …" : ""}. That is correct if it is
            deliberate — it is also what a newly onboarded customer looks like
            before anyone has been given it.
          </span>
        </p>
      )}

      <button onClick={add} disabled={readOnly} className={shell}>
        New group
      </button>

      <div className="space-y-2">
        {groups.map((group) => {
          const isOpen = open === group.id;
          const builtin = undeletable.includes(group.id);
          const holders = holdersOf(group.id);
          return (
            <div key={group.id} className="min-w-0 rounded-md border border-gray-200">
              <div className="flex min-w-0 items-center gap-2 px-3 py-2">
                <button onClick={() => setOpen(isOpen ? null : group.id)}
                        className="shrink-0 text-gray-500 hover:text-gray-800">
                  {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                {group.allAccess
                  ? <Globe size={13} className="shrink-0 text-gray-400" />
                  : <Layers size={13} className="shrink-0 text-gray-400" />}
                <input value={group.label} disabled={readOnly}
                       onChange={(e) => setGroup(group.id, { label: e.target.value })}
                       className="min-w-0 flex-1 rounded border border-transparent px-1
                                  py-0.5 text-sm font-medium text-gray-900
                                  hover:border-gray-300 focus:border-blue-500
                                  focus:outline-none disabled:bg-transparent" />
                {builtin && (
                  <span className="shrink-0 rounded bg-gray-100 px-1.5 text-[10px]
                                   text-gray-600">built in</span>
                )}
                <span className="shrink-0 text-xs text-gray-500">
                  {groupSummary(group, nameOf)}
                  {holders.length ? ` · ${holders.length} person(s)` : " · nobody yet"}
                </span>
                {!builtin && (
                  <button
                    onClick={() => onChange(groups.filter((g) => g.id !== group.id))}
                    disabled={readOnly}
                    title={holders.length
                      ? `${holders.length} person(s) hold this — they will lose that territory entirely, not fall back to anything`
                      : "Delete this group"}
                    className="shrink-0 rounded border border-gray-300 px-1.5 py-0.5
                               text-xs text-gray-600 hover:bg-red-50 hover:text-red-700
                               disabled:opacity-50">
                    Delete
                  </button>
                )}
              </div>

              {isOpen && (
                <div className="border-t border-gray-100 px-3 py-3">
                  <label className="block text-xs font-medium text-gray-700">
                    What it is for
                  </label>
                  <input
                    value={group.description} disabled={readOnly}
                    placeholder="Sites handed over and live, for example"
                    onChange={(e) => setGroup(group.id, { description: e.target.value })}
                    className="mt-1 w-full min-w-0 rounded-md border border-gray-300
                               px-2 py-1.5 text-sm disabled:bg-gray-50" />
                  <p className="mt-1 text-[11px] text-gray-500">
                    Read by whoever assigns it, months later. "North" tells them
                    nothing a week after you wrote it.
                  </p>

                  {holders.length > 0 && (
                    <p className="mt-2 text-[11px] text-gray-500">
                      Held by {holders.slice(0, 6).join(", ")}
                      {holders.length > 6 ? `, +${holders.length - 6} more` : ""} —
                      a change here moves all of them.
                    </p>
                  )}

                  <div className="mt-3 border-t border-gray-100 pt-3">
                    <fieldset disabled={readOnly} className="min-w-0">
                      <AdminScope
                        kind="group"
                        subject={group.label}
                        catalogue={catalogue}
                        scope={asScope(group)}
                        onChange={(next) => setGroup(group.id, {
                          allAccess: next.unrestricted,
                          // THE IDS ARE KEPT when "everything" is chosen, so
                          // flipping the mode to look at it does not empty a
                          // list somebody spent ten minutes drawing. The
                          // backend ignores `ecs` while `allAccess` is set.
                          ecs: next.ecs,
                        })} />
                    </fieldset>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

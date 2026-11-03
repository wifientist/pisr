import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import {
  ALL_VENUES, type EcRow, type ScopeCatalogue, type ScopeState,
} from "@/hooks/useScopeCatalogue";

/**
 * ONE PERSON's customers and venues — the by-person direction, and the only
 * place the venue level can be edited in full.
 *
 * A DIFFERENT KIND OF SETTING FROM THE SECTIONS TAB, and the dialog says so
 * rather than letting them look alike. Hiding a section tidies a report. This
 * decides whether someone can open a customer's data at all — and on an MSP
 * tenant those customers are separate companies, so a mistake here is the one
 * that shows one of them another.
 *
 * Consequently it FAILS CLOSED: the moment "only what I select" is chosen,
 * anything not selected is refused. The empty selection is a real, reachable
 * state meaning "nothing", not a shorthand for "everything" — see api/scope.py,
 * which is where that is enforced. The banner below exists to make sure nobody
 * discovers that by locking a team out on a Friday.
 *
 * THE MODE IS TWO RADIOS AND THE TREE IS ALWAYS LIVE, which is a deliberate
 * reversal. It was one "can reach everything" tick box, and while that was on
 * — its default — the customer list below rendered greyed out and
 * `pointer-events-none`. So an admin looking for where to scope somebody
 * arrived at a list of their own customers that could not be clicked, which
 * reads as a feature that was never finished rather than as a mode. Now
 * narrowing is a choice you can see, and a click in the tree makes it: see
 * `narrow` below.
 *
 * IT NAMES THE PERSON. The old label said "Users can reach every customer and
 * venue" — plural and generic, left over from when this control belonged to
 * the `user` role rather than to one person. Scope is per person now
 * (visibility.scope_for), so the labels say whose it is; a control that talks
 * about "users" while sitting under one person's name is the kind of thing
 * that gets read as a global switch and left alone.
 *
 * THE CUSTOMER LIST ARRIVES AS A PROP (`useScopeCatalogue`), because the
 * by-customer direction needs the same one. See that hook for why there is
 * only one copy.
 *
 * IT ALSO EDITS A ROLE'S DEFAULT (`kind="role"`), which is the same control
 * over the same stored shape — `visibility` keys scope blocks by role id
 * beside the per-person ones, and `scope.clean` validates both identically.
 * Only the prose differs, so a second component would have been this file
 * twice, with the fail-closed rule stated once in each and no test holding
 * them together.
 *
 * AND IT RESOLVES INHERITANCE, which is the part worth reading before
 * changing anything. `visibility.scope_for` answers person → role default →
 * unrestricted, so somebody with no entry of their own under a scoped role
 * already reaches a definite list of customers. Reading only their own entry
 * would draw an empty tree beside a radio claiming they reach everything —
 * both halves wrong, on the screen whose entire job is to say who can open
 * what. See `base` below.
 */

export type { ScopeState };
export { ALL_VENUES };

export default function AdminScope(
  { scope, onChange, subject, catalogue, kind = "person" }: {
    scope: ScopeState;
    onChange: (next: ScopeState) => void;
    /** Whose scope this is, named in the labels. Omitted where it is not known. */
    subject?: string;
    catalogue: ScopeCatalogue;
    /**
     * Whether `scope` is a PERSON's own territory (the legacy model, still in
     * force until a resource group exists) or the CONTENTS OF A GROUP.
     *
     * One component for both because they are one document: a group holds
     * exactly the `{unrestricted, ecs}` shape scope has always held, validated
     * by the same `scope.clean`, and `allAccess` is that `unrestricted` flag.
     * Only the prose differs, so a second component would be this file twice,
     * with the fail-closed rule stated once in each and nothing holding them
     * together.
     */
    kind?: "person" | "group";
  },
) {
  const { ecs, ecError, rows, venues, loadVenues } = catalogue;
  const [open, setOpen] = useState<Set<string>>(new Set());

  // Named in a label, so blank is handled rather than rendering "  can reach".
  const who = (subject || "").trim();
  const whoName = who
    ? <b>{who}</b>
    : (kind === "group" ? "This group" : "This person");
  // Unique per subject AND per kind: the dialog can hold two of these at once,
  // and a shared name would make one radio move the other.
  const radioName = `scope-mode-${kind}-${who || "unassigned"}`;

  /**
   * What every tick box, counter and warning below reads.
   *
   * It was briefly the RESOLVED scope, blending in a role's default so a
   * person following one did not see an empty tree. The role axis is gone —
   * a role says what somebody may RUN, never whose network — so this is
   * simply the document being edited. See api/resource_groups.py.
   */
  const base: ScopeState = scope;

  /**
   * Apply a change from the TREE, which always means narrowing.
   *
   * Every tree interaction is taken at face value: ticking a customer selects
   * it AND switches the mode above away from "every customer", because that is
   * unambiguously what the click meant. The alternative was the greyed-out
   * tree described in the header — a dead end at exactly the moment somebody
   * has found the right screen. Nothing is lost by guessing: the mode is two
   * radios directly above, so putting it back is one click and visible.
   */
  const narrow = (next: ScopeState) => onChange({ ...next, unrestricted: false });

  const toggleOpen = (tenant: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(tenant)) next.delete(tenant);
      else { next.add(tenant); loadVenues(tenant); }
      return next;
    });
  };

  const ecAllowed = (tenant: string) => tenant in base.ecs;

  const setEcAllowed = (tenant: string, allowed: boolean) => {
    const ecsNext = { ...base.ecs };
    // Defaults to every venue on the customer. The alternative — an empty list,
    // meaning no venues — would make ticking a customer appear to do nothing,
    // and the admin would reasonably conclude the feature was broken.
    if (allowed) ecsNext[tenant] = ALL_VENUES; else delete ecsNext[tenant];
    narrow({ ...base, ecs: ecsNext });
    if (allowed) { setOpen((o) => new Set(o).add(tenant)); loadVenues(tenant); }
  };

  const setAllVenues = (tenant: string, all: boolean) => {
    const rowsForEc = venues[tenant];
    narrow({
      ...base,
      ecs: {
        ...base.ecs,
        // Switching off "all venues" seeds the explicit list with everything
        // rather than nothing, so the admin narrows from a working state
        // instead of starting from a customer nobody can open.
        [tenant]: all
          ? ALL_VENUES
          : (Array.isArray(rowsForEc) ? rowsForEc.map((r) => r.id) : []),
      },
    });
  };

  const toggleVenue = (tenant: string, venueId: string) => {
    const current = base.ecs[tenant];
    const rowsForEc = venues[tenant];
    const list = current === ALL_VENUES
      ? (Array.isArray(rowsForEc) ? rowsForEc.map((r) => r.id) : [])
      : ([...(current as string[] | undefined) || []]);
    const next = list.includes(venueId)
      ? list.filter((id) => id !== venueId)
      : [...list, venueId];
    narrow({ ...base, ecs: { ...base.ecs, [tenant]: next } });
  };

  const venueAllowed = (tenant: string, venueId: string) => {
    const current = base.ecs[tenant];
    return current === ALL_VENUES || (Array.isArray(current) && current.includes(venueId));
  };

  /**
   * Set every venue on one customer at once.
   *
   * "None" leaves the customer TICKED with an empty venue list, which is a real
   * and reachable state — the customer is named, and no venue within it is
   * allowed. It is not the same as unticking the customer, and api/scope.py
   * treats an empty list as none rather than as all, so this is honest rather
   * than a shortcut for removal.
   */
  const setEveryVenue = (tenant: string, on: boolean) => {
    const rowsForEc = venues[tenant];
    if (!Array.isArray(rowsForEc)) return;
    narrow({
      ...base,
      ecs: { ...base.ecs, [tenant]: on ? rowsForEc.map((v) => v.id) : [] },
    });
  };

  /** Tick or untick every customer in the live list at once. */
  const setEveryEc = (on: boolean) => {
    if (!on) { narrow({ ...base, ecs: {} }); return; }
    const next: Record<string, string | string[]> = { ...base.ecs };
    for (const ec of rows) if (!(ec.id in next)) next[ec.id] = ALL_VENUES;
    narrow({ ...base, ecs: next });
  };

  const emptySelection = useMemo(
    () => !base.unrestricted && Object.keys(base.ecs).length === 0,
    [base]);

  /**
   * Ids in the stored policy that this tenant does not contain.
   *
   * THE LIST BELOW IS BUILT FROM THE LIVE TENANT, so a stored id that no
   * longer resolves is not rendered anywhere — it is filtering for users and
   * invisible to the admin who would have to fix it. That is the state a
   * deployment lands in the moment the RUCKUS ONE credentials are repointed at
   * a different tenant: every id in the policy becomes meaningless at once,
   * and because scope FAILS CLOSED, users stop being able to open anything
   * while admins see nothing wrong. It reads like an authentication bug and
   * is not one.
   *
   * CHECKED AGAINST `rows`, NOT `ecs`. On a single-tenant deployment `ecs` is
   * the empty list and the one real tenant lives in `rows` alone, so comparing
   * against `ecs` called that tenant's own id stale and offered to delete the
   * only scope entry that worked.
   *
   * Venues are only checkable for customers whose venue list has been loaded,
   * so this reports what it knows rather than pretending to be exhaustive.
   */
  // Load the venue list for every customer the policy names an explicit venue
  // list for, without waiting to be expanded. The orphan check below cannot
  // see a stale VENUE id until that customer's venues are known, and asking
  // the admin to expand each one in turn to discover a problem they do not yet
  // know exists is most of the way back to not reporting it at all. Bounded by
  // the number of allowed customers, which is small by construction.
  useEffect(() => {
    if (base.unrestricted || !ecs) return;
    for (const [tenant, allowed] of Object.entries(base.ecs)) {
      if (allowed === ALL_VENUES || !Array.isArray(allowed) || !allowed.length) continue;
      loadVenues(tenant);
    }
  }, [ecs, base.unrestricted, base.ecs, loadVenues]);

  const orphanEcs = useMemo(() => {
    if (!ecs || base.unrestricted) return [];
    const live = new Set(rows.map((e) => e.id));
    return Object.keys(base.ecs).filter((id) => !live.has(id));
  }, [ecs, rows, base]);

  const orphanVenues = useMemo(() => {
    if (base.unrestricted) return [];
    const out: { tenant: string; tenantName: string; ids: string[] }[] = [];
    for (const [tenant, allowedIds] of Object.entries(base.ecs)) {
      if (allowedIds === ALL_VENUES || !Array.isArray(allowedIds)) continue;
      const rowsForEc = venues[tenant];
      if (!rowsForEc || rowsForEc === "loading" || rowsForEc === "error") continue;
      const live = new Set(rowsForEc.map((v) => v.id));
      const missing = allowedIds.filter((id) => !live.has(id));
      if (missing.length) {
        out.push({
          tenant,
          tenantName: rows.find((e) => e.id === tenant)?.name || tenant,
          ids: missing,
        });
      }
    }
    return out;
  }, [rows, venues, base]);

  // True while some allowed customer's venue list has not arrived, which is the
  // only remaining reason the venue check above could be incomplete. Customers
  // set to "all venues" have no list to check, so their absence is not a gap.
  const venueCheckPending = useMemo(() => {
    if (base.unrestricted) return false;
    return Object.entries(base.ecs).some(([tenant, allowed]) => {
      if (allowed === ALL_VENUES || !Array.isArray(allowed) || !allowed.length) return false;
      const rowsForEc = venues[tenant];
      return !rowsForEc || rowsForEc === "loading" || rowsForEc === "error";
    });
  }, [venues, base]);

  const dropOrphanEcs = () => {
    const next = { ...base.ecs };
    for (const id of orphanEcs) delete next[id];
    onChange({ ...base, ecs: next });
  };

  const dropOrphanVenues = () => {
    const next = { ...base.ecs };
    for (const { tenant, ids } of orphanVenues) {
      const current = next[tenant];
      if (!Array.isArray(current)) continue;
      const gone = new Set(ids);
      next[tenant] = current.filter((id) => !gone.has(id));
    }
    onChange({ ...base, ecs: next });
  };

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
        <div className="space-y-1.5">
          {/*
            "EVERYTHING" IS A STATEMENT NO LIST OF IDS CAN MAKE, which is why
            it is a mode rather than a full set of ticked boxes. It means
            every customer and venue INCLUDING ONES THAT DO NOT EXIST YET;
            ticking all eight customers today says something different, and
            quietly stops being true the next time one is onboarded. That
            distinction is the entire job of the built-in `all-access` group.
          */}
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="radio" name={radioName} className="mt-1 shrink-0"
                   checked={scope.unrestricted}
                   onChange={() => onChange({ ...scope, unrestricted: true })} />
            <span className="min-w-0 text-sm font-medium text-gray-900">
              {kind === "group"
                ? <>{whoName} covers <b>every</b> customer and venue, including
                    ones added later</>
                : <>{whoName} can reach <b>every</b> customer and venue</>}
            </span>
          </label>
          <label className="flex items-start gap-2 cursor-pointer">
            <input type="radio" name={radioName} className="mt-1 shrink-0"
                   checked={!scope.unrestricted}
                   onChange={() => onChange({ ...scope, unrestricted: false })} />
            <span className="min-w-0 text-sm font-medium text-gray-900">
              {kind === "group"
                ? <>{whoName} covers <b>only</b> what is selected below</>
                : <>{whoName} can reach <b>only</b> what is selected below</>}
            </span>
          </label>
        </div>
        <p className="mt-2 text-xs text-gray-500">
          {kind === "group" ? (
            <>
              Everybody assigned this group can open what is selected here —
              on the screen and in the PDF. Somebody holding <b>several</b>{" "}
              groups gets all of them together, so a group only ever adds.
            </>
          ) : (
            <>
              Unlike the Sections tab, this decides what they can open <b>at
              all</b> — anything not selected is refused, on the screen and in
              the PDF.
            </>
          )}
        </p>
      </div>


      {emptySelection && (
        <p className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50
                      px-3 py-2 text-sm text-amber-900">
          <TriangleAlert size={15} className="mt-0.5 shrink-0" />
          <span>
            Nothing is selected, so{" "}
            {kind === "group" ? "anybody assigned this group"
                              : (who || "this person")}{" "}
            will be able to open <b>no venue at all</b>. That is a real
            setting, not an oversight — an empty selection means none, never
            all — so pick at least one customer, or choose the first option
            above.
          </span>
        </p>
      )}

      {(orphanEcs.length > 0 || orphanVenues.length > 0) && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2">
          <p className="flex items-start gap-2 text-sm text-amber-900">
            <TriangleAlert size={15} className="mt-0.5 shrink-0" />
            <span className="min-w-0">
              <b>This policy names things that are not in this tenant.</b> They
              still count: scope refuses anything not selected, so a user
              allowed <i>only</i> these can open nothing at all. The usual cause
              is the RUCKUS ONE credentials being repointed at a different
              tenant after the policy was written.
            </span>
          </p>

          {orphanEcs.length > 0 && (
            <div className="mt-2 min-w-0">
              <p className="text-xs font-medium text-amber-900">
                {orphanEcs.length} customer id(s) not in this tenant:
              </p>
              <ul className="mt-1 space-y-0.5">
                {orphanEcs.map((id) => (
                  <li key={id} className="break-all font-mono text-[11px] text-amber-800">
                    {id}
                    {base.ecs[id] === ALL_VENUES
                      ? " · all venues"
                      : ` · ${(base.ecs[id] as string[]).length} venue(s)`}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {orphanVenues.map(({ tenant, tenantName, ids }) => (
            <div key={tenant} className="mt-2 min-w-0">
              <p className="text-xs font-medium text-amber-900">
                {ids.length} venue id(s) not in {tenantName}:
              </p>
              <ul className="mt-1 space-y-0.5">
                {ids.map((id) => (
                  <li key={id} className="break-all font-mono text-[11px] text-amber-800">{id}</li>
                ))}
              </ul>
            </div>
          ))}

          {venueCheckPending && (
            <p className="mt-2 text-[11px] text-amber-800">
              Still loading venues for one or more customers, so there may be
              more than this.
            </p>
          )}

          <div className="mt-2 flex flex-wrap gap-2">
            {orphanEcs.length > 0 && (
              <button type="button" onClick={dropOrphanEcs}
                      className="rounded-md border border-amber-400 bg-white px-2.5 py-1
                                 text-xs font-medium text-amber-900 hover:bg-amber-100">
                Remove the {orphanEcs.length} customer id(s)
              </button>
            )}
            {orphanVenues.length > 0 && (
              <button type="button" onClick={dropOrphanVenues}
                      className="rounded-md border border-amber-400 bg-white px-2.5 py-1
                                 text-xs font-medium text-amber-900 hover:bg-amber-100">
                Remove the stale venue id(s)
              </button>
            )}
          </div>
          <p className="mt-1 text-[11px] text-amber-800">
            Removing them changes nothing about what users can reach — these
            ids match nothing. It only takes them out of the saved policy.
            Save to apply.
          </p>
        </div>
      )}

      {ecError && (
        <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          Could not load the customer list: {ecError}
        </p>
      )}

      {!ecs && !ecError && (
        <p className="flex items-center gap-2 py-4 text-sm text-gray-500">
          <Loader2 size={15} className="animate-spin" /> Loading customers…
        </p>
      )}

      {base.unrestricted && rows.length > 0 && (
        <p className="text-xs text-gray-500">
          Every customer below is allowed at the moment. Tick one to narrow
          {kind === "group"
            ? " this group"
            : (who ? ` ${who}` : " this person")} to just what you select —
          that switches the choice above for you.
        </p>
      )}

      {/* Only in the narrowed mode: "0 of 7 selected" directly contradicts
          the line above it while every customer is allowed. */}
      {!base.unrestricted && rows.length > 1 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-gray-500">
            {Object.keys(base.ecs).length} of {rows.length} customer(s) selected
          </p>
          <span className="flex items-center gap-2 text-xs">
            <button type="button" onClick={() => setEveryEc(true)}
                    className="text-blue-700 underline-offset-2 hover:underline">
              Select all
            </button>
            <span className="text-gray-300">·</span>
            <button type="button" onClick={() => setEveryEc(false)}
                    className="text-blue-700 underline-offset-2 hover:underline">
              Select none
            </button>
          </span>
        </div>
      )}

      <div className="space-y-1">
        {rows.map((ec: EcRow) => {
          const allowed = ecAllowed(ec.id);
          const all = base.ecs[ec.id] === ALL_VENUES;
          const expanded = open.has(ec.id);
          const rowsForEc = venues[ec.id];
          return (
            <div key={ec.id} className="min-w-0 rounded-md border border-gray-200">
              <div className="flex items-center gap-2 px-3 py-2">
                <input type="checkbox" checked={allowed} className="shrink-0"
                       onChange={(e) => setEcAllowed(ec.id, e.target.checked)} />
                <button onClick={() => toggleOpen(ec.id)}
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left">
                  {expanded ? <ChevronDown size={14} className="shrink-0 text-gray-400" />
                            : <ChevronRight size={14} className="shrink-0 text-gray-400" />}
                  <span className="min-w-0 truncate text-sm font-medium text-gray-900">
                    {ec.name}
                  </span>
                  {/* "no access" would be a lie while every customer is
                      allowed, so an unticked row says nothing in that mode and
                      the line above explains it. */}
                  <span className="shrink-0 text-xs text-gray-400">
                    {allowed ? (all ? "all venues" : `${(base.ecs[ec.id] as string[]).length} venue(s)`)
                             : (base.unrestricted ? "" : "no access")}
                  </span>
                </button>
              </div>

              {expanded && (
                <div className="border-t border-gray-100 px-3 py-2">
                  {!allowed && (
                    <p className="text-xs text-gray-500">
                      Tick this customer to choose venues within it.
                    </p>
                  )}
                  {allowed && (
                    <>
                      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
                        <label className="flex items-center gap-2 text-xs text-gray-700 cursor-pointer">
                          <input type="checkbox" checked={all}
                                 onChange={(e) => setAllVenues(ec.id, e.target.checked)} />
                          Every venue on this customer, including ones added later
                        </label>
                        {!all && Array.isArray(rowsForEc) && rowsForEc.length > 1 && (
                          <span className="flex items-center gap-2 text-xs">
                            <button type="button" onClick={() => setEveryVenue(ec.id, true)}
                                    className="text-blue-700 underline-offset-2 hover:underline">
                              Select all
                            </button>
                            <span className="text-gray-300">·</span>
                            <button type="button" onClick={() => setEveryVenue(ec.id, false)}
                                    className="text-blue-700 underline-offset-2 hover:underline">
                              Select none
                            </button>
                          </span>
                        )}
                      </div>
                      {rowsForEc === "loading" && (
                        <p className="flex items-center gap-2 text-xs text-gray-500">
                          <Loader2 size={13} className="animate-spin" /> Loading venues…
                        </p>
                      )}
                      {rowsForEc === "error" && (
                        <p className="text-xs text-red-700">Could not load venues.</p>
                      )}
                      {Array.isArray(rowsForEc) && !rowsForEc.length && (
                        <p className="text-xs text-gray-500">This customer has no venues.</p>
                      )}
                      {Array.isArray(rowsForEc) && rowsForEc.length > 0 && (
                        <div className={`grid gap-1 sm:grid-cols-2 ${all ? "opacity-50 pointer-events-none" : ""}`}>
                          {rowsForEc.map((v) => (
                            <label key={v.id}
                                   className="flex min-w-0 items-center gap-2 text-xs text-gray-700 cursor-pointer">
                              <input type="checkbox" className="shrink-0"
                                     checked={venueAllowed(ec.id, v.id)}
                                     onChange={() => toggleVenue(ec.id, v.id)} />
                              <span className="min-w-0 truncate">{v.name}</span>
                            </label>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

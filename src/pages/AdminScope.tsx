import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import { useAuth } from "@/context/AuthContext";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * Which RUCKUS ONE customers and venues an ordinary user may report on.
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
 * THE LISTS COME FROM THE ORDINARY ENDPOINTS. An admin is unrestricted, so
 * /api/r1/{cid}/msp/mspEcs and /api/pisr/{cid}/venues return everything for
 * them. There is no admin-only mirror of either, deliberately: a second path to
 * the same R1 data is a second place for the scope filter to be forgotten.
 */

export const ALL_VENUES = "*";

export interface ScopeState {
  unrestricted: boolean;
  /** tenantId -> "*" | venueId[] */
  ecs: Record<string, string | string[]>;
}

interface EcRow { id: string; name: string }
interface VenueRow { id: string; name: string }

export default function AdminScope(
  { scope, onChange }: { scope: ScopeState; onChange: (next: ScopeState) => void },
) {
  const { activeControllerId, activeControllerSubtype } = useAuth();
  const isMsp = (activeControllerSubtype || "").toUpperCase() === "MSP";

  const [ecs, setEcs] = useState<EcRow[] | null>(null);
  const [ecError, setEcError] = useState<string | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [venues, setVenues] = useState<Record<string, VenueRow[] | "loading" | "error">>({});

  // The single-tenant case still needs a key to file venues under, and the
  // backend uses the configured tenant id for it. The frontend does not know
  // that id, so it asks /scope for it rather than inventing one — getting this
  // wrong would write a policy keyed under a tenant that never matches.
  const [soleTenant, setSoleTenant] = useState<string | null>(null);

  useEffect(() => {
    if (activeControllerId === null) return;
    if (isMsp) {
      fetch(`${API_BASE_URL}/r1/${activeControllerId}/msp/mspEcs`,
            { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((body) => {
          const rows = Array.isArray(body) ? body : (body?.data || []);
          setEcs(rows.map((r: any) => ({ id: r.id || r.tenantId, name: r.name || r.id })));
        })
        .catch((e: Error) => setEcError(e.message));
    } else {
      fetch(`${API_BASE_URL}/pisr/${activeControllerId}/scope`, { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((body) => { setSoleTenant(body.tenantId); setEcs([]); })
        .catch((e: Error) => setEcError(e.message));
    }
  }, [activeControllerId, isMsp]);

  const loadVenues = useCallback((tenant: string) => {
    if (venues[tenant]) return;
    setVenues((v) => ({ ...v, [tenant]: "loading" }));
    fetch(`${API_BASE_URL}/pisr/${activeControllerId}/venues?tenant_id=${encodeURIComponent(tenant)}`,
          { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => setVenues((v) => ({
        ...v,
        [tenant]: (body.venues || []).map((row: any) => ({ id: row.id, name: row.name })),
      })))
      .catch(() => setVenues((v) => ({ ...v, [tenant]: "error" })));
  }, [activeControllerId, venues]);

  const toggleOpen = (tenant: string) => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(tenant)) next.delete(tenant);
      else { next.add(tenant); loadVenues(tenant); }
      return next;
    });
  };

  const ecAllowed = (tenant: string) => tenant in scope.ecs;

  const setEcAllowed = (tenant: string, allowed: boolean) => {
    const ecsNext = { ...scope.ecs };
    // Defaults to every venue on the customer. The alternative — an empty list,
    // meaning no venues — would make ticking a customer appear to do nothing,
    // and the admin would reasonably conclude the feature was broken.
    if (allowed) ecsNext[tenant] = ALL_VENUES; else delete ecsNext[tenant];
    onChange({ ...scope, ecs: ecsNext });
    if (allowed) { setOpen((o) => new Set(o).add(tenant)); loadVenues(tenant); }
  };

  const setAllVenues = (tenant: string, all: boolean) => {
    const rows = venues[tenant];
    onChange({
      ...scope,
      ecs: {
        ...scope.ecs,
        // Switching off "all venues" seeds the explicit list with everything
        // rather than nothing, so the admin narrows from a working state
        // instead of starting from a customer nobody can open.
        [tenant]: all ? ALL_VENUES : (Array.isArray(rows) ? rows.map((r) => r.id) : []),
      },
    });
  };

  const toggleVenue = (tenant: string, venueId: string) => {
    const current = scope.ecs[tenant];
    const rows = venues[tenant];
    const list = current === ALL_VENUES
      ? (Array.isArray(rows) ? rows.map((r) => r.id) : [])
      : ([...(current as string[] | undefined) || []]);
    const next = list.includes(venueId)
      ? list.filter((id) => id !== venueId)
      : [...list, venueId];
    onChange({ ...scope, ecs: { ...scope.ecs, [tenant]: next } });
  };

  const venueAllowed = (tenant: string, venueId: string) => {
    const current = scope.ecs[tenant];
    return current === ALL_VENUES || (Array.isArray(current) && current.includes(venueId));
  };

  const rows: EcRow[] = isMsp
    ? (ecs || [])
    : (soleTenant ? [{ id: soleTenant, name: "This RUCKUS ONE tenant" }] : []);

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
    const rows = venues[tenant];
    if (!Array.isArray(rows)) return;
    onChange({
      ...scope,
      ecs: { ...scope.ecs, [tenant]: on ? rows.map((v) => v.id) : [] },
    });
  };

  /** Tick or untick every customer in the live list at once. */
  const setEveryEc = (on: boolean) => {
    if (!on) { onChange({ ...scope, ecs: {} }); return; }
    const next: Record<string, string | string[]> = { ...scope.ecs };
    for (const ec of rows) if (!(ec.id in next)) next[ec.id] = ALL_VENUES;
    onChange({ ...scope, ecs: next });
  };

  const emptySelection = useMemo(
    () => !scope.unrestricted && Object.keys(scope.ecs).length === 0,
    [scope]);

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
    if (scope.unrestricted || !ecs) return;
    for (const [tenant, allowed] of Object.entries(scope.ecs)) {
      if (allowed === ALL_VENUES || !Array.isArray(allowed) || !allowed.length) continue;
      loadVenues(tenant);
    }
  }, [ecs, scope.unrestricted, scope.ecs, loadVenues]);

  const orphanEcs = useMemo(() => {
    if (!ecs || scope.unrestricted) return [];
    const live = new Set(ecs.map((e) => e.id));
    return Object.keys(scope.ecs).filter((id) => !live.has(id));
  }, [ecs, scope]);

  const orphanVenues = useMemo(() => {
    if (scope.unrestricted) return [];
    const out: { tenant: string; tenantName: string; ids: string[] }[] = [];
    for (const [tenant, allowedIds] of Object.entries(scope.ecs)) {
      if (allowedIds === ALL_VENUES || !Array.isArray(allowedIds)) continue;
      const rows = venues[tenant];
      if (!rows || rows === "loading" || rows === "error") continue;
      const live = new Set(rows.map((v) => v.id));
      const missing = allowedIds.filter((id) => !live.has(id));
      if (missing.length) {
        out.push({
          tenant,
          tenantName: ecs?.find((e) => e.id === tenant)?.name || tenant,
          ids: missing,
        });
      }
    }
    return out;
  }, [ecs, venues, scope]);

  // True while some allowed customer's venue list has not arrived, which is the
  // only remaining reason the venue check above could be incomplete. Customers
  // set to "all venues" have no list to check, so their absence is not a gap.
  const venueCheckPending = useMemo(() => {
    if (scope.unrestricted) return false;
    return Object.entries(scope.ecs).some(([tenant, allowed]) => {
      if (allowed === ALL_VENUES || !Array.isArray(allowed) || !allowed.length) return false;
      const rows = venues[tenant];
      return !rows || rows === "loading" || rows === "error";
    });
  }, [venues, scope]);

  const dropOrphanEcs = () => {
    const next = { ...scope.ecs };
    for (const id of orphanEcs) delete next[id];
    onChange({ ...scope, ecs: next });
  };

  const dropOrphanVenues = () => {
    const next = { ...scope.ecs };
    for (const { tenant, ids } of orphanVenues) {
      const current = next[tenant];
      if (!Array.isArray(current)) continue;
      const gone = new Set(ids);
      next[tenant] = current.filter((id) => !gone.has(id));
    }
    onChange({ ...scope, ecs: next });
  };

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
        <label className="flex items-start gap-2 cursor-pointer">
          <input type="checkbox" className="mt-1 shrink-0" checked={scope.unrestricted}
                 onChange={(e) => onChange({ ...scope, unrestricted: e.target.checked })} />
          <span className="min-w-0 text-sm">
            <span className="font-medium text-gray-900">
              Users can reach every customer and venue
            </span>
            <span className="block text-xs text-gray-500">
              Untick to restrict them to what you select below. Unlike the
              Sections tab, this decides what a user can open at all — anything
              not selected is refused, on the screen and in the PDF.
            </span>
          </span>
        </label>
      </div>

      {emptySelection && (
        <p className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50
                      px-3 py-2 text-sm text-amber-900">
          <TriangleAlert size={15} className="mt-0.5 shrink-0" />
          <span>
            Nothing is selected, so users will be able to open <b>no venue at
            all</b>. That is a real setting, not an oversight — an empty
            selection means none, never all — so pick at least one customer or
            tick the box above.
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
                    {scope.ecs[id] === ALL_VENUES
                      ? " · all venues"
                      : ` · ${(scope.ecs[id] as string[]).length} venue(s)`}
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
            Removing them changes nothing about what users can reach — these ids
            match nothing. It only takes them out of the saved policy. Save to apply.
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

      {!scope.unrestricted && rows.length > 1 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-gray-500">
            {Object.keys(scope.ecs).length} of {rows.length} customer(s) selected
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

      <div className={`space-y-1 ${scope.unrestricted ? "opacity-50 pointer-events-none" : ""}`}>
        {rows.map((ec) => {
          const allowed = ecAllowed(ec.id);
          const all = scope.ecs[ec.id] === ALL_VENUES;
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
                  <span className="shrink-0 text-xs text-gray-400">
                    {allowed ? (all ? "all venues" : `${(scope.ecs[ec.id] as string[]).length} venue(s)`)
                             : "no access"}
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

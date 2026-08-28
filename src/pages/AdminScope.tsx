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

  const emptySelection = useMemo(
    () => !scope.unrestricted && Object.keys(scope.ecs).length === 0,
    [scope]);

  const rows: EcRow[] = isMsp
    ? (ecs || [])
    : (soleTenant ? [{ id: soleTenant, name: "This RUCKUS ONE tenant" }] : []);

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
                      <label className="mb-2 flex items-center gap-2 text-xs text-gray-700 cursor-pointer">
                        <input type="checkbox" checked={all}
                               onChange={(e) => setAllVenues(ec.id, e.target.checked)} />
                        Every venue on this customer, including ones added later
                      </label>
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

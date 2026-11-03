import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@/context/AuthContext";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * The customers and venues a scope can name, fetched ONCE for the whole
 * portal.
 *
 * LIFTED OUT OF AdminScope BECAUSE THERE ARE NOW TWO DIRECTIONS. Scope used
 * to be edited one way only — pick a person, tick their customers — so the
 * customer list could live inside that one component. It is now also edited
 * the other way round (pick a customer, tick the people), and both directions
 * need the same list plus the same lazily-loaded venue lists. Two copies would
 * mean two sets of R1 calls and, worse, two caches that disagree about which
 * venues a customer has while one of them is still loading.
 *
 * THE LISTS COME FROM THE ORDINARY ENDPOINTS. An admin is unrestricted, so
 * /api/r1/{cid}/msp/mspEcs and /api/pisr/{cid}/venues return everything for
 * them. There is no admin-only mirror of either, deliberately: a second path
 * to the same R1 data is a second place for the scope filter to be forgotten.
 */

export const ALL_VENUES = "*";

export interface ScopeState {
  unrestricted: boolean;
  /** tenantId -> "*" | venueId[] */
  ecs: Record<string, string | string[]>;
}

export interface EcRow { id: string; name: string }
export interface VenueRow { id: string; name: string }
export type VenueList = VenueRow[] | "loading" | "error";

export interface ScopeCatalogue {
  /** The live customer list, or null while it is still arriving. */
  ecs: EcRow[] | null;
  ecError: string | null;
  /**
   * What to RENDER as the customer list: the live ECs on an MSP tenant, or the
   * one configured tenant otherwise. Use this for rows and for deciding
   * whether a stored id is stale — never `ecs`, which is empty in the
   * single-tenant case and would call that tenant's own id an orphan.
   */
  rows: EcRow[];
  venues: Record<string, VenueList>;
  loadVenues: (tenant: string) => void;
  /** A customer's name for a summary line, falling back to its id. */
  nameOf: (tenantId: string) => string;
}

export function useScopeCatalogue(): ScopeCatalogue {
  const { activeControllerId, activeControllerSubtype } = useAuth();
  const isMsp = (activeControllerSubtype || "").toUpperCase() === "MSP";

  const [ecs, setEcs] = useState<EcRow[] | null>(null);
  const [ecError, setEcError] = useState<string | null>(null);
  const [venues, setVenues] = useState<Record<string, VenueList>>({});

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

  /**
   * Which venue lists have been asked for, kept in a ref rather than read back
   * out of `venues`.
   *
   * It is what makes `loadVenues` STABLE. Guarding on `venues[tenant]` put the
   * cache in the callback's dependencies, so the callback changed identity
   * every time a venue list arrived — and an effect that calls it then re-runs
   * on its own result, which is one edit away from a fetch loop. Both
   * directions now call this from effects, so stability stopped being a
   * nicety.
   */
  const requested = useRef<Set<string>>(new Set());
  useEffect(() => { requested.current = new Set(); setVenues({}); },
           [activeControllerId]);

  const loadVenues = useCallback((tenant: string) => {
    if (!tenant || requested.current.has(tenant)) return;
    requested.current.add(tenant);
    setVenues((v) => ({ ...v, [tenant]: "loading" }));
    fetch(`${API_BASE_URL}/pisr/${activeControllerId}/venues?tenant_id=${encodeURIComponent(tenant)}`,
          { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => setVenues((v) => ({
        ...v,
        [tenant]: (body.venues || []).map((row: any) => ({ id: row.id, name: row.name })),
      })))
      // Dropped from `requested` so expanding the row again retries, rather
      // than leaving a customer permanently unexpandable after one blip.
      .catch(() => {
        requested.current.delete(tenant);
        setVenues((v) => ({ ...v, [tenant]: "error" }));
      });
  }, [activeControllerId]);

  const rows: EcRow[] = useMemo(
    () => (isMsp
      ? (ecs || [])
      : (soleTenant ? [{ id: soleTenant, name: "This RUCKUS ONE tenant" }] : [])),
    [isMsp, ecs, soleTenant]);

  const nameOf = useCallback(
    (tenantId: string) => rows.find((r) => r.id === tenantId)?.name || tenantId,
    [rows]);

  return { ecs, ecError, rows, venues, loadVenues, nameOf };
}

/**
 * One line describing a person's scope, for the roster.
 *
 * THE ROSTER EXISTS TO BE SCANNED, so "nothing selected" has to be as loud as
 * it is consequential — scope fails closed, and a person with an empty
 * selection can open no venue at all. That state used to be reachable only by
 * opening the one person it applied to.
 */
export function scopeSummary(
  scope: ScopeState | null, nameOf: (id: string) => string,
): { text: string; tone: "plain" | "narrowed" | "empty" } {
  if (!scope || scope.unrestricted) return { text: "every customer", tone: "plain" };
  const ids = Object.keys(scope.ecs);
  if (!ids.length) return { text: "nothing selected", tone: "empty" };
  const partial = ids.some((id) => Array.isArray(scope.ecs[id]));
  const named = ids.length <= 2
    ? ids.map(nameOf).join(", ")
    : `${nameOf(ids[0])} + ${ids.length - 1} more`;
  return { text: partial ? `${named} · some venues` : named, tone: "narrowed" };
}

/**
 * Where a person's reach came from, and the one place the portal works it out.
 *
 * MIRRORS `visibility.scope_for` DELIBERATELY AND EXACTLY: a person's own
 * entry, else their ROLE's default, else unrestricted. The portal cannot ask
 * the server this per person — it would be one request per row of a forty-row
 * roster — so it resolves locally, which means there are now two
 * implementations of one rule. Keeping it to this one function is what makes
 * that survivable: every screen reads it, nothing re-derives it, and
 * `/admin/preview` is the server's own answer for checking one person against.
 *
 * A ROLE DEFAULT IS NOT A FLOOR OR A CEILING, it is a FALLBACK. The person's
 * entry REPLACES it rather than narrowing or widening it — see scope.spec.ts,
 * "a person's scope REPLACES their role's".
 */
export interface ResourceGroup {
  id: string;
  label: string;
  description: string;
  /** Every customer and venue, INCLUDING ones added later. See resource_groups.py. */
  allAccess: boolean;
  ecs: Record<string, string | string[]>;
}

/**
 * A group's contents as the scope tree edits them — they are one document.
 * `allAccess` is the `unrestricted` flag; `api/scope.py` validates both.
 */
export const asScope = (group: ResourceGroup): ScopeState =>
  ({ unrestricted: Boolean(group.allAccess), ecs: group.ecs || {} });

/**
 * What somebody holding these groups reaches — the UNION, mirroring
 * `resource_groups.union`.
 *
 * A deleted group (undefined here) contributes NOTHING rather than being
 * skipped over: somebody was deliberately given a territory, and losing the
 * definition must not widen them. `ALL_VENUES` absorbs an explicit list,
 * because adding a second group must never take reach away.
 */
export function unionGroups(
  groups: (ResourceGroup | undefined)[],
): ScopeState {
  const ecs: Record<string, string | string[]> = {};
  for (const group of groups) {
    if (!group) continue;
    if (group.allAccess) return { unrestricted: true, ecs: {} };
    for (const [tenant, venues] of Object.entries(group.ecs || {})) {
      const current = ecs[tenant];
      if (current === ALL_VENUES || venues === ALL_VENUES) {
        ecs[tenant] = ALL_VENUES;
      } else {
        ecs[tenant] = [...new Set([
          ...(Array.isArray(current) ? current : []),
          ...(Array.isArray(venues) ? venues : []),
        ])];
      }
    }
  }
  return { unrestricted: false, ecs };
}

export type ScopeSource = "person" | "role" | "unrestricted" | "groups" | "unplaced";

export interface Resolved {
  /** What they can actually reach. `unrestricted: true` means everything. */
  scope: ScopeState;
  source: ScopeSource;
  /** The role default, when there is one — for showing what they fall back to. */
  roleDefault: ScopeState | null;
}

export const UNRESTRICTED: ScopeState = { unrestricted: true, ecs: {} };

/**
 * A scope state as a RESTRICTION, or null if it is not one.
 *
 * `{unrestricted: true}` and "no entry at all" are the SAME STORED STATE —
 * `scope.clean` returns None for an unrestricted block, so the file has one
 * way to say "no restriction here". Which means a person cannot be stored as
 * explicitly unrestricted in order to escape their role's default: an empty
 * entry falls back to the role, by construction. The portal must not offer
 * what the file cannot hold, so `AdminScope` renders the first radio as
 * "uses the <role> default" rather than "every customer" whenever one exists.
 */
export function restrictionOf(scope: ScopeState | null | undefined): ScopeState | null {
  return scope && !scope.unrestricted ? scope : null;
}

export function resolveScope(
  own: ScopeState | null | undefined,
  roleDefault: ScopeState | null | undefined,
): Resolved {
  const mine = restrictionOf(own);
  const theirs = restrictionOf(roleDefault);
  if (mine) return { scope: mine, source: "person", roleDefault: theirs };
  if (theirs) return { scope: theirs, source: "role", roleDefault: theirs };
  return { scope: UNRESTRICTED, source: "unrestricted", roleDefault: null };
}

/**
 * What somebody reaches and WHY, once resource groups decide it.
 *
 * MIRRORS `visibility.scope_for`'s group branch. The role is not consulted:
 * a role says what somebody may RUN, never whose network. No assignment at
 * all is `unplaced` and reaches NOTHING — the deliberate reversal the model
 * rests on — and that is kept distinct from an assignment of `[]`, which is
 * somebody deciding the same thing on purpose.
 */
export function resolveGroups(
  assigned: string[] | null | undefined,
  byId: Record<string, ResourceGroup>,
): Resolved {
  if (assigned === null || assigned === undefined) {
    return { scope: { unrestricted: false, ecs: {} },
             source: "unplaced", roleDefault: null };
  }
  return { scope: unionGroups(assigned.map((id) => byId[id])),
           source: "groups", roleDefault: null };
}

/** Does this scope let somebody open one customer at all? */
export function reaches(scope: ScopeState, ecId: string): boolean {
  return scope.unrestricted || ecId in scope.ecs;
}

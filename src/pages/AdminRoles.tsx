import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown, ChevronRight, Loader2, ShieldCheck, TriangleAlert, X,
} from "lucide-react";
import AdminScope from "@/pages/AdminScope";
import AdminScopeByCustomer from "@/pages/AdminScopeByCustomer";
import AdminResourceGroups, { groupSummary } from "@/pages/AdminResourceGroups";
import {
  UNRESTRICTED, resolveGroups, resolveScope, restrictionOf, scopeSummary,
  useScopeCatalogue, type Resolved, type ResourceGroup, type ScopeState,
} from "@/hooks/useScopeCatalogue";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * RBAC: roles, what they can do, and who holds them.
 *
 * ONE CONCEPT ON PURPOSE. An earlier design had three ways to hold a
 * capability — a binary admin/user role, per-account tags, and a bundle called
 * a "profile" — and the arbitrary split between them was what everybody
 * tripped over ("I have the MSP tag, so why can I not run a batch?"). There is
 * now one answer to "how did they get that": the role they were assigned.
 *
 * FOUR TABS, FOUR QUESTIONS:
 *
 *   Roles              what does each job allow, and what does it hide
 *   Customer access    which customers and venues each person may open
 *   Capabilities       what does each capability actually unlock
 *   Who can do what    the derived matrix, plus "why can this person not…"
 *
 * THE SECOND TAB IS NAMED FOR WHAT IT SETS. It was "Who sees what", which is
 * the same question as "Who can do what" three tabs along and says nothing
 * about customers — so the one screen where somebody is scoped to a tenant
 * was the one nobody could find. It is scope, and the label says so.
 *
 * SCOPE IS RESOLVED, NOT READ. Three things decide what somebody can open —
 * the role they hold, that role's DEFAULT territory, and their own entry —
 * and `visibility.scope_for` picks between them in that order. The portal
 * used to read only the last of the three: role defaults were in the GET
 * payload, accepted by the PUT and enforced on every request, with no control
 * anywhere and nothing on screen naming them. So anyone holding a scoped role
 * appeared unrestricted in the roster, in the per-customer counts and in the
 * by-customer groups, while the server refused them. The defaults are now
 * edited on the Roles tab, and every list on the Customer access tab reads
 * `resolveScope` — one function, mirroring the server's order, never
 * re-derived per screen.
 *
 * THAT TAB READS BOTH WAYS, and the roster is why. It was a `<select>` of
 * identities: the roster was hidden until you opened it, you could not see who
 * had no scope set, and "who can open this customer?" was forty visits to a
 * dropdown. It is now a visible filterable list with each person's scope on
 * their row, plus a direction toggle — see `AdminScopeByCustomer` for what the
 * other direction can and cannot safely store.
 *
 * The matrix and the preview are DERIVED FROM THE LIVE ROUTE TABLE, never
 * written down here — a hand-maintained copy would be a fourth list beside
 * the three that actually decide, and the authoritative-looking one nobody
 * updates. See api/access_surface.py.
 *
 * THE CONTROLS ARE NOT THE CONTROL. This bundle is served unauthenticated, so
 * anyone can read that a role editor exists; `require_grants` on the routes is
 * what refuses them. What the portal buys is the difference between "you may
 * not" and a save that fails for reasons nobody can see.
 */

interface Role {
  id: string;
  label: string;
  grants: string[];
  /**
   * Element ids this role MAY SEE — an ALLOW list. Anything not named is
   * withheld, so "what can this role see" has a positive answer instead of
   * "everything except these forty-three".
   */
  visible: string[];
}

interface Capability { id: string; label: string; hint: string; isDefault: boolean }

interface Feature { id: string; label: string; requires: string[] }

interface Person {
  identity: string;
  role: string | null;
  /** Assigned resource groups. null = never placed; [] = deliberately none. */
  groups: string[] | null;
  scope: ScopeState | null;
}

interface Column {
  key: string;
  roleId: string;
  roleLabel: string;
  builtin?: boolean;
  missing?: boolean;
  holders: string[];
  scopedHolders: number;
  grants: string[];
  visibleCount: number;
}

interface MatrixFeature {
  id: string;
  kind: string;
  label: string;
  hint: string;
  requires: string[];
  wired: boolean;
  routes: { path: string; methods: string[]; narrows: boolean; summary: string }[];
}

interface Capabilities {
  features: MatrixFeature[];
  combinations: Column[];
  cells: Record<string, Record<string, string>>;
  mode: string;
  caveat: string;
}

interface Preview {
  identity: string;
  known: boolean;
  disabled: boolean;
  roleId: string | null;
  role: { id: string; label: string; builtin: boolean } | null;
  roleMissing: boolean;
  grants: { id: string; label: string }[];
  features: { id: string; label: string; requires: string[]; has: boolean; missing: string[] }[];
  scope: { unrestricted: boolean; source: string; ecs: Record<string, string | string[]> };
  hidden: { id: string; label: string; kind: string; tab: string }[];
  hiddenSource: string;
  warnings: string[];
}

interface SectionRow {
  id: string; label: string; tab: string; tabLabel: string;
  checks: { id: string; label: string }[];
  columns: { id: string; label: string }[];
}

interface Payload {
  sections: SectionRow[];
  grants: Capability[];
  features: Feature[];
  people: string[];
  undeletable: string[];
  /** The role a caller falls back to when nothing names one — `auth.role_id_of`. */
  defaultRole: string;
  mayEditRoles: boolean;
  superGrant: string;
  policy: {
    roles: Role[];
    /**
     * Each role's DEFAULT scope, keyed by role id. LEGACY AND NOT EDITABLE
     * HERE: a role says what somebody may run, never whose network, and the
     * two axes were split for good. It is still ENFORCED while resource
     * groups are off, so the portal surfaces it as something to clear rather
     * than pretending it is not there.
     */
    scope: Record<string, ScopeState>;
    /** The territories. See api/resource_groups.py. */
    resourceGroups: ResourceGroup[];
    undeletableGroups: string[];
    /** Whether groups DECIDE scope here yet — false until the first is saved. */
    groupsInForce: boolean;
    people: Record<string, {
      role: string | null;
      /** null means never placed, [] means deliberately nothing. */
      groups: string[] | null;
      scope: ScopeState | null;
    }>;
    orphanedGroups: Record<string, string[]>;
    orphanedAssignments: Record<string, string>;
    writable: boolean;
    path: string | null;
  };
}

/**
 * The groups EXACTLY AS THE SERVER STORES THEM: built-ins dropped, tenant
 * keys and venue lists sorted, `ecs` cleared on an all-access group.
 *
 * Canonical because it is what `dirty` compares AND what the PUT sends.
 * `resource_groups.clean` drops an unchanged built-in and `Scope.as_json`
 * sorts the venues, so a local map differing only in key order would read as
 * an unsaved change and the Save button would never go out — the bug this
 * file already had once with `people`.
 */
function storedGroups(
  groups: ResourceGroup[], undeletable: string[],
): Record<string, ResourceGroup> {
  const out: Record<string, ResourceGroup> = {};
  for (const group of [...groups].sort((a, b) => a.id.localeCompare(b.id))) {
    if (undeletable.includes(group.id)) continue;
    const ecs: Record<string, string | string[]> = {};
    if (!group.allAccess) {
      for (const tenant of Object.keys(group.ecs || {}).sort()) {
        const venues = group.ecs[tenant];
        ecs[tenant] = Array.isArray(venues) ? [...venues].sort() : venues;
      }
    }
    out[group.id] = { ...group, ecs };
  }
  return out;
}

const TABS = [
  ["roles", "Roles"],
  ["groups", "Resource groups"],
  ["people", "Customer access"],
  ["capabilities", "Capabilities"],
  ["matrix", "Who can do what"],
] as const;

export default function AdminRoles({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<typeof TABS[number][0]>("roles");
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [roles, setRoles] = useState<Role[]>([]);
  const [people, setPeople] = useState<Record<string, Person>>({});
  const [groups, setGroups] = useState<ResourceGroup[]>([]);
  const [openRole, setOpenRole] = useState<string | null>(null);
  /** Which parts of the element tree are expanded. Independent of what is
   *  ticked — see the note in the tree itself. */
  const [openTabs, setOpenTabs] = useState<Set<string>>(new Set());
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());
  const [subject, setSubject] = useState<string>("");
  /**
   * Which way round the Customer access tab is read: pick a person and see
   * their customers, or pick a customer and see its people. Both are views of
   * the same per-person policy — see AdminScopeByCustomer.
   */
  const [direction, setDirection] = useState<"person" | "customer">("person");
  const [customer, setCustomer] = useState<string>("");
  /** Narrows whichever list is on the left. Cleared when the direction flips,
   *  because it means a different thing on each side. */
  const [rosterFilter, setRosterFilter] = useState<string>("");
  /** identity -> role id, as the ACCOUNTS say it. Read-only here. */
  const [accountRoles, setAccountRoles] = useState<Record<string, string>>({});

  // Fetched once for the whole dialog and handed to both scope directions, so
  // they cannot disagree about which venues a customer has.
  const catalogue = useScopeCatalogue();

  const [caps, setCaps] = useState<Capabilities | null>(null);
  const [previewOf, setPreviewOf] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [openFeature, setOpenFeature] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    fetch(`${API_BASE_URL}/admin/roles`, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail
                                   || `HTTP ${r.status}`);
        return r.json();
      })
      .then((payload: Payload) => {
        setData(payload);
        setRoles(payload.policy.roles);
        setGroups(payload.policy.resourceGroups || []);
        setPeople(Object.fromEntries(
          Object.entries(payload.policy.people).map(([identity, e]) => [
            identity, { identity, role: e.role ?? null,
                        groups: e.groups ?? null, scope: e.scope ?? null },
          ])));
      })
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(load, [load]);

  // Who holds what, read from the accounts portal's own endpoint rather than
  // inferred — the assignment lives there in this mode, and showing a guess
  // would be worse than showing nothing.
  useEffect(() => {
    fetch(`${API_BASE_URL}/admin/accounts`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (!body) return;
        setAccountRoles(Object.fromEntries(
          body.users.map((u: any) => [u.username, u.role])));
      })
      .catch(() => { /* proxy mode has none; the picker copes */ });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Re-fetched whenever its tab opens or a save lands: it is DERIVED from the
  // policy the other tabs edit, so a stale matrix is worse than a slow one.
  useEffect(() => {
    if (tab !== "matrix") return;
    fetch(`${API_BASE_URL}/admin/capabilities`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setCaps)
      .catch((e: Error) => setError(e.message));
  }, [tab, saved]);

  const readOnly = !data?.policy.writable;
  const mayEdit = Boolean(data?.mayEditRoles) && !readOnly;

  const dirty = useMemo(() => {
    if (!data) return false;
    if (JSON.stringify(roles) !== JSON.stringify(data.policy.roles)) return true;
    // Compared as the SERVER stores it — only roles carrying a restriction —
    // or every role the editor has merely looked at would read as a change
    // and the Save button would never go out.
    if (JSON.stringify(storedGroups(groups, data.policy.undeletableGroups || []))
        !== JSON.stringify(storedGroups(data.policy.resourceGroups || [],
                                        data.policy.undeletableGroups || []))) return true;
    const now = Object.fromEntries(Object.entries(people)
      .map(([i, p]) => [i, { role: p.role, groups: p.groups, scope: p.scope }]));
    const was = Object.fromEntries(Object.entries(data.policy.people)
      .map(([i, p]) => [i, { role: p.role ?? null, groups: p.groups ?? null,
                             scope: p.scope ?? null }]));
    return JSON.stringify(now) !== JSON.stringify(was);
  }, [data, roles, groups, people]);

  const save = async () => {
    setBusy(true); setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/admin/roles`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        // EVERY role and EVERY assignment, every time. The PUT is a
        // whole-document replace, so a body carrying only what is on screen
        // would delete the rest.
        body: JSON.stringify({
          roles: Object.fromEntries(roles.map((r) => [r.id, r])),
          // EVERY group, every time, for the same reason as the roles
          // themselves: the PUT is a whole-document replace. Sending `{}` is
          // how the editor deletes the last one — which also switches the
          // model OFF, so the server refuses it while anybody is still
          // assigned a group (`_guard_last_group`).
          resourceGroups: storedGroups(groups, data?.policy.undeletableGroups || []),
          // AN EMPTY GROUP LIST IS WORTH SENDING, unlike an unrestricted
          // scope block: `[]` is an admin deciding somebody reaches nothing,
          // and dropping the entry would turn that decision back into "never
          // placed". Both refuse everything; only one of them is a record of
          // a choice, and the portal shows them differently.
          people: Object.fromEntries(Object.entries(people)
            .filter(([, p]) => p.role || p.groups
                               || (p.scope && !p.scope.unrestricted))
            .map(([i, p]) => [i, {
              role: p.role,
              groups: p.groups,
              scope: p.scope && !p.scope.unrestricted ? p.scope : null,
            }])),
        }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      setData((c) => (c ? { ...c, policy: body } : c));
      setRoles(body.roles);
      setGroups(body.resourceGroups || []);
      // RESEEDED FROM THE RESPONSE, exactly as `load` does it. The PUT drops
      // entries that carry neither a role nor a restriction, so a person
      // narrowed and then put back to "every customer" is absent from the
      // saved document while still sitting in local state — and `dirty`
      // compares the two, so the Save button stayed lit for a change that had
      // already landed, forever.
      setPeople(Object.fromEntries(
        Object.entries(body.people as Record<string, any>).map(([identity, e]) => [
          identity, { identity, role: e.role ?? null,
                      groups: e.groups ?? null, scope: e.scope ?? null },
        ])));
      setSaved(`Saved. ${body.roles.length} role(s).`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const runPreview = useCallback((identity: string) => {
    if (!identity.trim()) { setPreview(null); return; }
    fetch(`${API_BASE_URL}/admin/preview?identity=${encodeURIComponent(identity.trim())}`,
          { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setPreview)
      .catch((e: Error) => { setPreview(null); setError(e.message); });
  }, []);

  /** Every element a role could allow: the section, its checks, its columns. */
  const elementIds = useCallback(
    (section: SectionRow) => [section.id,
                              ...section.checks.map((c) => c.id),
                              ...section.columns.map((c) => c.id)], []);
  const allElements = useMemo(
    () => (data?.sections ?? []).flatMap(elementIds), [data, elementIds]);
  /** Sections grouped by tab, in catalogue order. */
  const byTab = useMemo(() => {
    const groups = new Map<string, SectionRow[]>();
    for (const section of data?.sections ?? []) {
      groups.set(section.tab, [...(groups.get(section.tab) ?? []), section]);
    }
    return [...groups.entries()];
  }, [data]);

  /**
   * The role somebody holds, from whichever place assigns it in this mode:
   * the ACCOUNT where there is one, the policy map otherwise.
   */
  const assignedRole = useCallback(
    (identity: string) => accountRoles[identity] ?? people[identity]?.role ?? null,
    [accountRoles, people]);

  const roleById = useMemo(
    () => Object.fromEntries(roles.map((r) => [r.id, r])), [roles]);
  const heldByMe = useMemo(
    () => new Set(data?.grants.filter((g) => false).map((g) => g.id) ?? []), [data]);
  void heldByMe;

  /** A Set with one member flipped. */
  const toggled = (set: Set<string>, id: string) => {
    const next = new Set(set);
    if (!next.delete(id)) next.add(id);
    return next;
  };

  const setRole = (id: string, patch: Partial<Role>) =>
    setRoles((c) => c.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const everyone = useMemo(() => {
    const names = new Set<string>([...(data?.people ?? []), ...Object.keys(people)]);
    return [...names].sort();
  }, [data, people]);

  /** Somebody's stored scope, or null for "no entry" — which means inherit. */
  const scopeOf = useCallback(
    (identity: string) => people[identity]?.scope ?? null, [people]);

  const roleLabelOf = useCallback((identity: string) => {
    const id = assignedRole(identity);
    return id ? (roleById[id]?.label ?? id) : null;
  }, [assignedRole, roleById]);

  const groupById = useMemo(
    () => Object.fromEntries(groups.map((g) => [g.id, g])), [groups]);

  /**
   * Whether RESOURCE GROUPS decide scope here — the bootstrap rule, mirrored.
   *
   * The server keys it on a stored catalogue existing (`visibility.
   * groups_in_force`), and the compiled-in `all-access` does not count or
   * every deployment would have switched over the moment this shipped. Read
   * from the LOCAL list as well as the stored flag, so that adding the first
   * group shows the consequence — everybody unassigned drops to nothing —
   * before it is saved rather than after.
   */
  const groupsInForce = Boolean(data?.policy.groupsInForce)
    || groups.some((g) => !(data?.policy.undeletableGroups || []).includes(g.id));

  /** Their assigned groups: null means never placed, [] means nothing. */
  const groupsOf = useCallback(
    (identity: string) => people[identity]?.groups ?? null, [people]);

  const setGroupsFor = useCallback(
    (identity: string, next: string[] | null) => setPeople((c) => ({
      ...c,
      [identity]: { identity, role: c[identity]?.role ?? null,
                    groups: next, scope: c[identity]?.scope ?? null },
    })), []);

  /** The LEGACY role default, read-only — see the Payload note. */
  const roleDefaultOf = useCallback((identity: string) => {
    const id = assignedRole(identity) || data?.defaultRole;
    return id ? restrictionOf((data?.policy.scope || {})[id]) : null;
  }, [assignedRole, data?.policy.scope, data?.defaultRole]);

  /**
   * WHAT THEY ACTUALLY REACH, AND WHY — the one answer every list on this tab
   * reads, resolved the way `visibility.scope_for` resolves it: their own
   * entry, else their role's default, else unrestricted.
   *
   * Nothing here may read `scopeOf` on its own to decide whether somebody can
   * open a customer. That was the whole defect: a person under a scoped role
   * has no entry of their own, so every count, roster line and group on this
   * tab called them unrestricted while the server was refusing them.
   */
  const resolvedOf = useCallback(
    (identity: string): Resolved => (
      groupsInForce
        ? resolveGroups(groupsOf(identity), groupById)
        : resolveScope(scopeOf(identity), roleDefaultOf(identity))),
    [groupsInForce, groupsOf, groupById, scopeOf, roleDefaultOf]);

  /**
   * Turn the per-person lists that are in force TODAY into groups.
   *
   * THE ON-RAMP, and the reason switching models is not a morning of typing.
   * Fail-closed means every unassigned person drops to nothing the moment a
   * catalogue exists, so the realistic alternative to this button is an
   * administrator hand-drawing forty territories while their colleagues
   * cannot open anything.
   *
   * DEDUPED BY TERRITORY, which is the whole insight groups offer: people
   * with byte-identical customer maps were already one territory maintained
   * in several places, and this is the first time that is visible. Everybody
   * who was unrestricted gets `all-access`, because that is precisely what
   * they had.
   *
   * It only PROPOSES — nothing is written until Save, so the diff is on
   * screen and reversible by closing the dialog.
   */
  const buildGroupsFromScopes = useCallback(() => {
    const byTerritory = new Map<string, { ecs: ScopeState["ecs"]; who: string[] }>();
    const unrestricted: string[] = [];
    for (const identity of everyone) {
      const own = restrictionOf(scopeOf(identity));
      if (!own) { unrestricted.push(identity); continue; }
      const key = JSON.stringify(
        Object.fromEntries(Object.keys(own.ecs).sort().map((t) => [
          t, Array.isArray(own.ecs[t]) ? [...(own.ecs[t] as string[])].sort()
                                       : own.ecs[t]])));
      const seen = byTerritory.get(key);
      if (seen) seen.who.push(identity);
      else byTerritory.set(key, { ecs: own.ecs, who: [identity] });
    }

    const made: ResourceGroup[] = [];
    const assign: Record<string, string[]> = {};
    let n = 0;
    for (const { ecs, who } of byTerritory.values()) {
      n += 1;
      const id = `g_mig${n}${Math.random().toString(36).slice(2, 8)}`;
      made.push({
        id,
        label: who.length === 1 ? `${who[0]}'s sites` : `Territory ${n}`,
        description: `Carried over from the per-person lists of ${who.join(", ")}.`,
        allAccess: false,
        ecs,
      });
      for (const identity of who) assign[identity] = [id];
    }
    for (const identity of unrestricted) {
      assign[identity] = [data?.policy.undeletableGroups?.[0] || "all-access"];
    }

    setGroups((current) => [...current, ...made]);
    setPeople((current) => {
      const next = { ...current };
      for (const [identity, held] of Object.entries(assign)) {
        next[identity] = {
          identity,
          role: current[identity]?.role ?? null,
          groups: held,
          scope: current[identity]?.scope ?? null,
        };
      }
      return next;
    });
  }, [everyone, scopeOf, data?.policy.undeletableGroups]);

  const holdersOf = useCallback(
    (groupId: string) => everyone.filter(
      (i) => (groupsOf(i) || []).includes(groupId)),
    [everyone, groupsOf]);

  const setScopeFor = useCallback(
    (identity: string, scope: ScopeState | null) => setPeople((c) => ({
      ...c,
      [identity]: { identity, role: c[identity]?.role ?? null,
                    groups: c[identity]?.groups ?? null, scope },
    })), []);

  /**
   * How many people can open one customer — the number on its roster row.
   *
   * COUNTS EVERY ROUTE TO IT, because each one genuinely opens the customer:
   * their own entry, their role's default, or being unrestricted. A count of
   * only the people explicitly selected would read as an answer to "who can
   * see this customer" while being wrong by however many admins exist plus
   * everyone under a scoped role — and this is the one reading of the screen
   * that has to be trustworthy.
   */
  const openerCount = useCallback((ecId: string) => everyone.filter((identity) => {
    const { scope } = resolvedOf(identity);
    return scope.unrestricted || ecId in scope.ecs;
  }).length, [everyone, resolvedOf]);

  const rosterRows = useMemo(() => {
    const q = rosterFilter.trim().toLowerCase();
    if (!q) return everyone;
    return everyone.filter((identity) => identity.toLowerCase().includes(q)
      || (roleLabelOf(identity) || "").toLowerCase().includes(q));
  }, [everyone, rosterFilter, roleLabelOf]);

  const customerRows = useMemo(() => {
    const q = rosterFilter.trim().toLowerCase();
    if (!q) return catalogue.rows;
    return catalogue.rows.filter((r) => r.name.toLowerCase().includes(q)
      || r.id.toLowerCase().includes(q));
  }, [catalogue.rows, rosterFilter]);

  const flip = (next: "person" | "customer") => {
    setDirection(next);
    setRosterFilter("");
  };

  const shell = "rounded-md border border-gray-300 px-2.5 py-1.5 text-xs " +
                "font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50";

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto
                    bg-black/40 p-4 sm:p-8">
      <div className="min-w-0 w-full max-w-5xl rounded-lg bg-white shadow-xl">
        <div className="flex min-w-0 items-center gap-2 border-b border-gray-200 px-4 py-3">
          <ShieldCheck size={17} className="shrink-0 text-gray-400" />
          <h2 className="min-w-0 flex-1 truncate text-sm font-semibold text-gray-900">
            Roles &amp; access
          </h2>
          {dirty && (
            <button onClick={save} disabled={busy || readOnly}
                    className="shrink-0 rounded-md bg-blue-600 px-3 py-1.5 text-xs
                               font-medium text-white hover:bg-blue-700
                               disabled:cursor-not-allowed disabled:bg-gray-300">
              {busy ? "Saving…" : "Save changes"}
            </button>
          )}
          <button onClick={onClose} aria-label="Close"
                  className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100">
            <X size={16} />
          </button>
        </div>

        <div className="flex gap-1 border-b border-gray-200 px-4">
          {TABS.map(([id, label]) => (
            <button key={id} onClick={() => setTab(id)}
                    className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${
                      tab === id ? "border-blue-600 text-blue-700"
                                 : "border-transparent text-gray-500 hover:text-gray-800"}`}>
              {label}
            </button>
          ))}
        </div>

        {error && (
          <p className="m-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
        )}
        {saved && !dirty && (
          <p className="m-4 rounded-md bg-green-50 px-3 py-2 text-sm text-green-800">{saved}</p>
        )}
        {readOnly && data && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2
                        text-sm text-amber-900">
            <b>Read-only.</b> {data.policy.path} is not writable by the container.
          </p>
        )}
        {data && !data.mayEditRoles && (
          <p className="m-4 flex items-start gap-1.5 rounded-md border border-amber-200
                        bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <TriangleAlert size={14} className="mt-0.5 shrink-0" />
            <span>
              Building roles needs <code>{data.superGrant}</code>, which somebody
              else holds. You can still assign the roles below — but only ones
              whose capabilities you hold yourself.
            </span>
          </p>
        )}
        {!data && !error && (
          <p className="flex items-center gap-2 p-8 text-sm text-gray-500">
            <Loader2 size={15} className="animate-spin" /> Loading roles…
          </p>
        )}

        {data && tab === "roles" && (
          <div className="max-h-[62vh] overflow-y-auto p-4">
            <p className="mb-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                          text-xs text-gray-600">
              A role is a job: the <b>capabilities</b> somebody holds and the{" "}
              <b>cards they are not shown</b>. It is the only thing that confers
              either — there is no second mechanism. <b>admin</b> and <b>user</b>{" "}
              ship with PISR and always resolve, so an unreadable policy file
              cannot lock everybody out; you can edit them, and deleting one
              resets it rather than removing it.
            </p>
            <button
              onClick={() => {
                const id = `r_${Math.random().toString(36).slice(2, 12)}`;
                setRoles((c) => [...c, {
                  id, label: "New role",
                  // Capabilities seeded with what an ordinary user already
                  // gets, because a role REPLACES the default rather than
                  // adding to it — an empty one creates somebody who cannot
                  // open a report.
                  grants: [...(data.grants.filter((g) => g.isDefault).map((g) => g.id))],
                  // Elements start EMPTY, which is where deny-by-default
                  // belongs: a new role should show nothing until somebody
                  // says what it is for.
                  visible: [],
                }]);
                setOpenRole(id);
              }}
              disabled={!mayEdit} className={shell}>
              New role
            </button>

            <div className="mt-3 space-y-2">
              {roles.map((role) => {
                const open = openRole === role.id;
                const builtin = (data.undeletable || []).includes(role.id);
                const holders = everyone.filter((i) => assignedRole(i) === role.id);
                return (
                  <div key={role.id} className="min-w-0 rounded-md border border-gray-200">
                    <div className="flex min-w-0 items-center gap-2 px-3 py-2">
                      <button onClick={() => setOpenRole(open ? null : role.id)}
                              className="shrink-0 text-gray-500 hover:text-gray-800">
                        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </button>
                      <input value={role.label} disabled={!mayEdit}
                             onChange={(e) => setRole(role.id, { label: e.target.value })}
                             className="min-w-0 flex-1 rounded border border-transparent px-1
                                        py-0.5 text-sm font-medium text-gray-900
                                        hover:border-gray-300 focus:border-blue-500
                                        focus:outline-none disabled:bg-transparent" />
                      {builtin && (
                        <span className="shrink-0 rounded bg-gray-100 px-1.5 text-[10px]
                                         text-gray-600">built in</span>
                      )}
                      <span className="shrink-0 text-xs text-gray-500">
                        {role.grants.length} capability(ies) · {role.visible.length} card(s)
                        {holders.length ? ` · ${holders.length} person(s)` : ""}
                      </span>
                      {/* A LEGACY SCOPE DEFAULT, flagged rather than edited.
                          It still applies while resource groups are off, and
                          then stops — so leaving it invisible would mean a
                          restriction nobody can see today and a silent
                          widening the day groups go on. */}
                      {restrictionOf((data.policy.scope || {})[role.id]) && (
                        <span className="shrink-0 rounded bg-amber-50 px-1.5 text-[10px]
                                         text-amber-800"
                              title="An older policy gave this role a scope default. It still applies until resource groups are switched on, then it is ignored. Clear it by giving its holders a resource group.">
                          legacy scope: {scopeSummary(
                            (data.policy.scope || {})[role.id],
                            catalogue.nameOf).text}
                        </span>
                      )}
                      {!builtin && (
                        <button
                          onClick={() => setRoles((c) => c.filter((r) => r.id !== role.id))}
                          disabled={!mayEdit}
                          title={holders.length
                            ? `${holders.length} person(s) hold this — they will hold NOTHING until reassigned`
                            : "Delete this role"}
                          className="shrink-0 rounded border border-gray-300 px-1.5 py-0.5
                                     text-xs text-gray-600 hover:bg-red-50 hover:text-red-700
                                     disabled:opacity-50">
                          Delete
                        </button>
                      )}
                    </div>

                    {open && (
                      <div className="border-t border-gray-100 px-3 py-3">
                        <p className="text-xs font-medium text-gray-700">Capabilities</p>
                        <div className="mt-1.5 space-y-1.5">
                          {data.grants.map((cap) => (
                            <label key={cap.id}
                                   className="flex min-w-0 items-start gap-2 text-xs text-gray-700">
                              <input type="checkbox" className="mt-0.5 shrink-0"
                                     disabled={!mayEdit}
                                     checked={role.grants.includes(cap.id)}
                                     onChange={() => setRole(role.id, {
                                       grants: role.grants.includes(cap.id)
                                         ? role.grants.filter((g) => g !== cap.id)
                                         : [...role.grants, cap.id],
                                     })} />
                              <span className="min-w-0">
                                <b>{cap.label}</b>
                                <span className="ml-1 text-gray-400">{cap.id}</span>
                                {cap.isDefault && (
                                  <span className="ml-1 rounded bg-blue-50 px-1 text-[10px]
                                                   text-blue-700">
                                    everyone has this unless a role says otherwise
                                  </span>
                                )}
                                {cap.hint && (
                                  <span className="block text-gray-500">{cap.hint}</span>
                                )}
                              </span>
                            </label>
                          ))}
                        </div>

                        {/*
                          NO SCOPE CONTROL HERE, DELIBERATELY. A role says
                          what somebody may RUN and SEE; a resource group says
                          whose network they may run it on. They are separate
                          axes and the portal keeps them on separate tabs,
                          because a role that quietly decided territory is how
                          `tenant_leader_north` and `tenant_leader_south` get
                          invented. See the Resource groups tab.
                        */}
                        <div className="mt-4 border-t border-gray-100 pt-3">
                          <p className="text-xs font-medium text-gray-700">
                            What this role sees ({role.visible.length} of {allElements.length})
                          </p>
                          <p className="mt-1 text-xs text-gray-500">
                            An <b>allow</b> list: anything not ticked is withheld.
                            Tick a tab to take everything under it.
                          </p>
                          <div className="mt-1.5 flex gap-2">
                            <button onClick={() => setRole(role.id, { visible: allElements })}
                                    disabled={!mayEdit} className={shell}>
                              Everything
                            </button>
                            <button onClick={() => setRole(role.id, { visible: [] })}
                                    disabled={!mayEdit} className={shell}>
                              Nothing
                            </button>
                          </div>

                          <div className="mt-2 space-y-1.5">
                            {byTab.map(([tab, rows]) => {
                              const ids = rows.flatMap(elementIds);
                              const on = ids.filter((i) => role.visible.includes(i)).length;
                              const open = openTabs.has(tab);
                              return (
                                <div key={tab} className="min-w-0 rounded border border-gray-200">
                                  <div className="flex min-w-0 items-center gap-2 px-2 py-1.5">
                                    {/*
                                      EXPANDING IS INDEPENDENT OF SELECTING.
                                      The tree used to open only when a tab was
                                      PARTLY ticked, which made "all on" and
                                      "all off" both dead ends: no way to drop
                                      one card from a full tab, and no way to
                                      pick one out of an empty one.
                                    */}
                                    <button onClick={() => setOpenTabs(toggled(openTabs, tab))}
                                            aria-label={open ? "Collapse" : "Expand"}
                                            className="shrink-0 text-gray-400 hover:text-gray-700">
                                      {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                                    </button>
                                    <label className="flex min-w-0 flex-1 items-center gap-2
                                                      text-xs text-gray-800">
                                      <input type="checkbox" className="shrink-0"
                                             disabled={!mayEdit}
                                             checked={on === ids.length && ids.length > 0}
                                             ref={(el) => { if (el) el.indeterminate =
                                               on > 0 && on < ids.length; }}
                                             onChange={() => setRole(role.id, {
                                               visible: on === ids.length
                                                 ? role.visible.filter((v) => !ids.includes(v))
                                                 : [...new Set([...role.visible, ...ids])],
                                             })} />
                                      <b className="min-w-0 truncate">{rows[0].tabLabel}</b>
                                    </label>
                                    <span className="shrink-0 text-xs text-gray-500">
                                      {on}/{ids.length}
                                    </span>
                                  </div>

                                  {open && (
                                    <div className="border-t border-gray-100 px-2 py-1">
                                      {rows.map((section) => {
                                        const kids = [...section.checks, ...section.columns];
                                        const checkIds = new Set(section.checks.map((c) => c.id));
                                        const sids = elementIds(section);
                                        const sOn = sids.filter(
                                          (i) => role.visible.includes(i)).length;
                                        const sOpen = openSections.has(section.id);
                                        return (
                                          <div key={section.id} className="min-w-0">
                                            <div className="flex min-w-0 items-center gap-2 py-0.5">
                                              {kids.length ? (
                                                <button
                                                  onClick={() => setOpenSections(
                                                    toggled(openSections, section.id))}
                                                  aria-label={sOpen ? "Collapse" : "Expand"}
                                                  className="shrink-0 text-gray-300
                                                             hover:text-gray-600">
                                                  {sOpen ? <ChevronDown size={12} />
                                                         : <ChevronRight size={12} />}
                                                </button>
                                              ) : <span className="w-3 shrink-0" />}
                                              <label className="flex min-w-0 flex-1 items-center
                                                                gap-2 text-xs text-gray-700">
                                                <input type="checkbox" className="shrink-0"
                                                       disabled={!mayEdit}
                                                       checked={sOn === sids.length}
                                                       ref={(el) => { if (el) el.indeterminate =
                                                         sOn > 0 && sOn < sids.length; }}
                                                       onChange={() => setRole(role.id, {
                                                         visible: sOn === sids.length
                                                           ? role.visible.filter(
                                                               (v) => !sids.includes(v))
                                                           : [...new Set(
                                                               [...role.visible, ...sids])],
                                                       })} />
                                                <span className="min-w-0 truncate">
                                                  {section.label}
                                                </span>
                                              </label>
                                              {kids.length > 0 && (
                                                <span className="shrink-0 text-[10px] text-gray-400">
                                                  {sOn}/{sids.length}
                                                </span>
                                              )}
                                            </div>

                                            {/* The third level: one check, or one
                                                column of one table. This is where
                                                "grant exactly that element" lives,
                                                and it is why the VLAN column can be
                                                withheld without losing the table. */}
                                            {sOpen && kids.length > 0 && (
                                              <div className="ml-8 border-l border-gray-100 pl-2">
                                                {kids.map((kid) => (
                                                  <label key={kid.id}
                                                         className="flex min-w-0 items-center gap-2
                                                                    py-0.5 text-xs text-gray-600">
                                                    <input type="checkbox" className="shrink-0"
                                                           disabled={!mayEdit}
                                                           checked={role.visible.includes(kid.id)}
                                                           onChange={() => setRole(role.id, {
                                                             visible: role.visible.includes(kid.id)
                                                               ? role.visible.filter(
                                                                   (v) => v !== kid.id)
                                                               : [...role.visible, kid.id],
                                                           })} />
                                                    <span className="min-w-0 truncate">
                                                      {kid.label}
                                                    </span>
                                                    <span className="shrink-0 text-[10px]
                                                                     text-gray-400">
                                                      {checkIds.has(kid.id) ? "check" : "column"}
                                                    </span>
                                                  </label>
                                                ))}
                                              </div>
                                            )}
                                          </div>
                                        );
                                      })}
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {data && tab === "groups" && (
          <div className="max-h-[62vh] overflow-y-auto p-4">
            <AdminResourceGroups
              groups={groups}
              onChange={setGroups}
              catalogue={catalogue}
              undeletable={data.policy.undeletableGroups || []}
              holdersOf={holdersOf}
              inForce={groupsInForce}
              readOnly={readOnly}
              onBuildFromScopes={buildGroupsFromScopes}
              carryOverCount={everyone.length} />
          </div>
        )}

        {data && tab === "people" && (
          <div className="max-h-[62vh] overflow-y-auto p-4">
            <p className="mb-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                          text-xs text-gray-600">
              <b>Which customers and venues</b> each person may open. What KIND
              of work they do there is their role, set on the account in the
              People portal.
              <br />
              Territory is assigned by <b>resource group</b>, never by role: a
              role says what somebody may run, a group says whose network they
              may run it on. Groups <b>add up</b> — somebody holding two
              reaches both — and somebody holding <b>none reaches nothing</b>.
            </p>

            {groupsInForce && (() => {
              const unplaced = everyone.filter((i) => groupsOf(i) === null);
              if (!unplaced.length) return null;
              return (
                <p className="mb-3 flex items-start gap-1.5 rounded border
                              border-amber-200 bg-amber-50 px-2 py-1.5 text-xs
                              text-amber-900">
                  <TriangleAlert size={13} className="mt-0.5 shrink-0" />
                  <span>
                    <b>{unplaced.length} person(s) hold no resource group</b>,
                    so they can open no venue at all:{" "}
                    {unplaced.slice(0, 5).join(", ")}
                    {unplaced.length > 5 ? ", …" : ""}. That is the rule here,
                    not a fault — but it is also what gets reported as "I can
                    log in and see nothing".
                  </span>
                </p>
              );
            })()}

            {Object.keys(data.policy.orphanedGroups || {}).length > 0 && (
              <p className="mb-3 flex items-start gap-1.5 rounded border border-red-200
                            bg-red-50 px-2 py-1.5 text-xs text-red-800">
                <TriangleAlert size={13} className="mt-0.5 shrink-0" />
                <span>
                  Somebody is assigned a resource group that no longer exists.
                  A deleted group confers nothing and does not fall back, so
                  that part of their territory is simply gone.
                </span>
              </p>
            )}

            {Object.keys(data.policy.orphanedAssignments || {}).length > 0 && (
              <p className="mb-3 flex items-start gap-1.5 rounded border border-red-200
                            bg-red-50 px-2 py-1.5 text-xs text-red-800">
                <TriangleAlert size={13} className="mt-0.5 shrink-0" />
                <span>
                  Somebody is assigned a role that no longer exists, so they hold
                  nothing at all. Pick a role for them below.
                </span>
              </p>
            )}

            {/*
              THE DIRECTION TOGGLE. Two questions, one stored policy: "which
              customers does this person work on" and "who works on this
              customer". The second is the onboarding and the audit question,
              and before this it meant opening every person in turn.
            */}
            <div className="mb-3 flex min-w-0 flex-wrap items-center gap-2">
              <div className="flex shrink-0 rounded-md border border-gray-300 p-0.5">
                {([["person", "By person"], ["customer", "By customer"]] as const)
                  .map(([id, label]) => (
                  <button key={id} type="button" onClick={() => flip(id)}
                          className={`rounded px-2.5 py-1 text-xs font-medium ${
                            direction === id
                              ? "bg-gray-900 text-white"
                              : "text-gray-600 hover:bg-gray-50"}`}>
                    {label}
                  </button>
                ))}
              </div>
              <input value={rosterFilter}
                     onChange={(e) => setRosterFilter(e.target.value)}
                     placeholder={direction === "person"
                       ? "Filter people…" : "Filter customers…"}
                     className="min-w-0 flex-1 rounded-md border border-gray-300
                                px-2 py-1.5 text-sm" />
            </div>

            <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,13rem)_minmax(0,1fr)]">
              {/* THE ROSTER IS VISIBLE, which is the whole point of it. A
                  dropdown hid both the list and every person's current scope,
                  so "who has nothing set" — the state that fails closed —
                  could not be seen without forty clicks. */}
              <div className="min-w-0 max-h-[46vh] overflow-y-auto rounded-md
                              border border-gray-200">
                {direction === "person" && rosterRows.map((identity) => {
                  // THE EFFECTIVE SCOPE, not the stored one. A roster showing
                  // "every customer" beside somebody their role has scoped to
                  // two is worse than no roster: it is the answer an admin
                  // would act on.
                  const { scope, source } = resolvedOf(identity);
                  const sum = scopeSummary(scope, catalogue.nameOf);
                  const held = groupsOf(identity);
                  // UNPLACED IS THE LOUD ONE. Under the group model it is the
                  // commonest way somebody ends up seeing an empty picker,
                  // and it is invisible from their seat — so the roster has
                  // to make it scannable rather than something you find by
                  // opening forty people.
                  const line = groupsInForce
                    ? (source === "unplaced"
                        ? "no group assigned"
                        : held && held.length
                          ? held.map((g) => groupById[g]?.label ?? `${g} (deleted)`)
                              .join(", ")
                          : "no group assigned")
                    : sum.text;
                  const tone = groupsInForce
                    ? (source === "unplaced" || !held?.length ? "empty" : "narrowed")
                    : sum.tone;
                  const on = subject === identity;
                  return (
                    <button key={identity} type="button"
                            onClick={() => setSubject(identity)}
                            className={`block w-full min-w-0 border-b border-gray-100
                                        px-2.5 py-1.5 text-left last:border-b-0 ${
                                          on ? "bg-blue-50" : "hover:bg-gray-50"}`}>
                      {/* The role sits on the SECOND line beside the scope,
                          not beside the name. Competing for one line truncated
                          the identity, and an address or a long username is
                          exactly what you are scanning for. */}
                      <span className="block min-w-0 truncate text-sm text-gray-900">
                        {identity}
                      </span>
                      <span className="block min-w-0 truncate text-[11px]">
                        <span className="text-gray-400">
                          {roleLabelOf(identity) ?? "no role"}
                        </span>
                        <span className="text-gray-300"> · </span>
                        <span className={
                          tone === "empty" ? "text-amber-700"
                            : tone === "narrowed" ? "text-blue-700"
                            : "text-gray-500"}>
                          {line}
                        </span>
                        {!groupsInForce && source === "role" && (
                          <span className="text-gray-400"> (role)</span>
                        )}
                      </span>
                    </button>
                  );
                })}

                {direction === "customer" && customerRows.map((ec) => {
                  const on = customer === ec.id;
                  return (
                    <button key={ec.id} type="button" onClick={() => setCustomer(ec.id)}
                            className={`block w-full min-w-0 border-b border-gray-100
                                        px-2.5 py-1.5 text-left last:border-b-0 ${
                                          on ? "bg-blue-50" : "hover:bg-gray-50"}`}>
                      <span className="block min-w-0 truncate text-sm text-gray-900">
                        {ec.name}
                      </span>
                      <span className="block text-[11px] text-gray-500">
                        {openerCount(ec.id)} can open it
                      </span>
                    </button>
                  );
                })}

                {direction === "customer" && !catalogue.ecs && !catalogue.ecError && (
                  <p className="flex items-center gap-2 px-2.5 py-3 text-xs text-gray-500">
                    <Loader2 size={13} className="animate-spin" /> Loading customers…
                  </p>
                )}
                {direction === "customer" && catalogue.ecError && (
                  <p className="px-2.5 py-3 text-xs text-red-700">
                    Could not load customers: {catalogue.ecError}
                  </p>
                )}
                {((direction === "person" && !rosterRows.length)
                  || (direction === "customer" && catalogue.ecs
                      && !customerRows.length)) && (
                  <p className="px-2.5 py-3 text-xs text-gray-500">
                    {rosterFilter.trim() ? "Nothing matches that filter."
                      : "Nothing to show."}
                  </p>
                )}
              </div>

              <div className="min-w-0">
                {direction === "person" && !subject && (
                  <p className="rounded-md border border-dashed border-gray-300 px-3 py-6
                                text-center text-xs text-gray-500">
                    Pick somebody on the left to set which customers and venues
                    they may open.
                  </p>
                )}

                {direction === "person" && subject && (() => {
                  const resolved = resolvedOf(subject);
                  const roleId = assignedRole(subject);
                  const roleLabel = roleId
                    ? (roleById[roleId]?.label ?? roleId) : null;
                  // The role whose DEFAULT applies, which for somebody nothing
                  // names is the built-in one the request path floors at.
                  const fellBack = !roleId;
                  const effRoleId = roleId || data.defaultRole;
                  const effRoleLabel = roleById[effRoleId]?.label ?? effRoleId;
                  const own = restrictionOf(scopeOf(subject));
                  const held = groupsOf(subject);
                  const effective = scopeSummary(resolved.scope, catalogue.nameOf);
                  return (
                  <div className="space-y-3">
                    {/*
                      THE ROLE IS SHOWN, NOT SET — in this mode. It is a field on
                      the ACCOUNT, chosen when the account is created and changed
                      in the People portal, which is where a person is
                      administered. A second dropdown here would be two places to
                      change one thing with no answer to which wins.
                    */}
                    {/*
                      THE WHOLE RESOLUTION, IN ORDER, ON ONE CARD. Three things
                      decide what somebody reaches — the role they hold, that
                      role's default, and their own entry — and before this they
                      were on three different screens, so "why can Dana not open
                      Acme?" meant holding all three in your head and knowing
                      which wins. The order here is `visibility.scope_for`'s
                      order, and the last line is the answer.
                    */}
                    <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
                      <dl className="space-y-1 text-xs">
                        <div className="flex min-w-0 gap-2">
                          <dt className="w-28 shrink-0 text-gray-500">Role</dt>
                          <dd className="min-w-0 flex-1 text-gray-900">
                            {roleLabel ?? (
                              <>
                                <i className="text-gray-500">none assigned</i>
                                <span className="ml-1 text-gray-500">
                                  — falls back to <b>{effRoleLabel}</b>
                                </span>
                              </>
                            )}
                            {roleId && !roleById[roleId] && (
                              <span className="ml-1 text-red-700">
                                — that role no longer exists, so they hold nothing
                              </span>
                            )}
                          </dd>
                        </div>
                        {groupsInForce ? (
                          <div className="flex min-w-0 gap-2">
                            <dt className="w-28 shrink-0 text-gray-500">
                              Resource groups
                            </dt>
                            <dd className="min-w-0 flex-1 text-gray-900">
                              {held === null && (
                                <i className="text-amber-700">never assigned</i>
                              )}
                              {held !== null && held.length === 0 && (
                                <i className="text-amber-700">none, deliberately</i>
                              )}
                              {held !== null && held.length > 0 && held.map((id, i) => (
                                <Fragment key={id}>
                                  {i > 0 && ", "}
                                  {groupById[id]
                                    ? groupById[id].label
                                    : <span className="text-red-700">
                                        {id} (deleted)
                                      </span>}
                                </Fragment>
                              ))}
                            </dd>
                          </div>
                        ) : (
                          <>
                            <div className="flex min-w-0 gap-2">
                              <dt className="w-28 shrink-0 text-gray-500">
                                {fellBack ? `${effRoleLabel} default` : "Role default"}
                              </dt>
                              <dd className="min-w-0 flex-1 text-gray-900">
                                {resolved.roleDefault
                                  ? scopeSummary(resolved.roleDefault,
                                                 catalogue.nameOf).text
                                  : <i className="text-gray-500">none — every customer</i>}
                              </dd>
                            </div>
                            <div className="flex min-w-0 gap-2">
                              <dt className="w-28 shrink-0 text-gray-500">Their own</dt>
                              <dd className="min-w-0 flex-1 text-gray-900">
                                {own
                                  ? scopeSummary(own, catalogue.nameOf).text
                                  : <i className="text-gray-500">not set</i>}
                              </dd>
                            </div>
                          </>
                        )}
                        <div className="flex min-w-0 gap-2 border-t border-gray-200 pt-1">
                          <dt className="w-28 shrink-0 font-medium text-gray-700">
                            Can open
                          </dt>
                          <dd className={`min-w-0 flex-1 font-medium ${
                            effective.tone === "empty" ? "text-amber-700"
                              : effective.tone === "narrowed" ? "text-blue-700"
                              : "text-gray-900"}`}>
                            {effective.text}
                            <span className="ml-1 font-normal text-gray-500">
                              {resolved.source === "groups"
                                ? "— the union of their groups"
                                : resolved.source === "unplaced"
                                  ? "— they hold no resource group"
                                  : resolved.source === "person"
                                    ? "— from their own entry"
                                    : resolved.source === "role"
                                      ? `— from the ${effRoleLabel} default`
                                      : "— nothing restricts them"}
                            </span>
                          </dd>
                        </div>
                      </dl>
                    </div>

                    {data.people.length > 0 ? (
                      <p className="text-xs text-gray-500">
                        The <b>role</b> is a field on the account — change it in
                        the <b>People</b> portal, where the account lives. This
                        tab sets which customers they may open.
                      </p>
                    ) : (
                      <div>
                        <label className="block text-xs font-medium text-gray-700">Role</label>
                        {/* No account store — proxy mode keeps identities in the
                            IDP — so the policy's per-person map is the only place
                            an assignment can live. */}
                        <select
                          value={people[subject]?.role ?? ""}
                          onChange={(e) => setPeople((c) => ({
                            ...c,
                            [subject]: { identity: subject,
                                         role: e.target.value || null,
                                         groups: c[subject]?.groups ?? null,
                                         scope: c[subject]?.scope ?? null },
                          }))}
                          className="mt-1 w-full min-w-0 rounded-md border border-gray-300
                                     px-2 py-1.5 text-sm">
                          <option value="">No role set here</option>
                          {roles.map((r) => (
                            <option key={r.id} value={r.id}>{r.label}</option>
                          ))}
                        </select>
                        <p className="mt-1 text-xs text-gray-500">
                          This instance keeps no accounts, so a role is assigned
                          here rather than in the People portal.
                        </p>
                      </div>
                    )}

                    {groupsInForce ? (
                      <div className="space-y-1">
                        <p className="text-xs font-medium text-gray-700">
                          Which territories they work on
                        </p>
                        <p className="text-[11px] text-gray-500">
                          Tick as many as apply — they <b>add up</b>. Define
                          what each one covers on the <b>Resource groups</b>{" "}
                          tab; nothing is editable per person, which is the
                          point of groups.
                        </p>
                        {groups.map((group) => {
                          const on = (held || []).includes(group.id);
                          return (
                            <label key={group.id}
                                   className="flex min-w-0 items-start gap-2 rounded-md
                                              border border-gray-200 px-3 py-2
                                              cursor-pointer hover:bg-gray-50">
                              <input type="checkbox" className="mt-0.5 shrink-0"
                                     checked={on}
                                     onChange={() => setGroupsFor(
                                       subject,
                                       on ? (held || []).filter((g) => g !== group.id)
                                          : [...(held || []), group.id])} />
                              <span className="min-w-0 flex-1">
                                <span className="block min-w-0 text-sm text-gray-900">
                                  {group.label}
                                </span>
                                <span className="block min-w-0 text-[11px] text-gray-500">
                                  {groupSummary(group, catalogue.nameOf)}
                                  {group.description ? ` · ${group.description}` : ""}
                                </span>
                              </span>
                            </label>
                          );
                        })}
                        {/*
                          "NEVER ASSIGNED" AND "ASSIGNED NOTHING" BOTH REFUSE
                          EVERYTHING, and the difference is whether anybody
                          decided. Recording the decision is what stops the
                          next administrator re-litigating it, and it is the
                          same reason the baseline editor keeps "not
                          applicable" apart from "unreviewed".
                        */}
                        {held === null && (
                          <button type="button" className={shell}
                                  onClick={() => setGroupsFor(subject, [])}>
                            Record that they get none
                          </button>
                        )}
                        {held !== null && held.length === 0 && (
                          <p className="rounded-md border border-amber-200 bg-amber-50
                                        px-3 py-2 text-xs text-amber-900">
                            Recorded as reaching <b>no venue at all</b>. That is
                            a decision, not a gap — the roster shows it
                            differently from somebody nobody has got to yet.
                          </p>
                        )}
                      </div>
                    ) : (
                      <AdminScope
                        key={subject}
                        subject={subject}
                        catalogue={catalogue}
                        scope={people[subject]?.scope ?? UNRESTRICTED}
                        onChange={(next) => setScopeFor(subject, next)} />
                    )}
                  </div>
                  );
                })()}

                {direction === "customer" && !customer && (
                  <p className="rounded-md border border-dashed border-gray-300 px-3 py-6
                                text-center text-xs text-gray-500">
                    Pick a customer on the left to see and set who can open it.
                  </p>
                )}

                {direction === "customer" && customer
                  && (() => {
                    const ec = catalogue.rows.find((r) => r.id === customer);
                    if (!ec) return (
                      <p className="rounded-md border border-dashed border-gray-300 px-3 py-6
                                    text-center text-xs text-gray-500">
                        That customer is no longer in this tenant.
                      </p>
                    );
                    if (!groupsInForce) return (
                      <div className="rounded-md border border-dashed border-gray-300
                                      px-3 py-6 text-center text-xs text-gray-500">
                        <b>{ec.name}</b> — reading by customer needs resource
                        groups. Right now access is a bespoke list on each
                        person, so "who can open this customer" has no answer
                        shorter than opening everybody.
                        <br />
                        <button type="button" onClick={() => setTab("groups")}
                                className="mt-2 text-blue-700 underline-offset-2
                                           hover:underline">
                          Set up resource groups
                        </button>
                      </div>
                    );
                    return (
                      <AdminScopeByCustomer
                        key={ec.id}
                        ec={ec}
                        catalogue={catalogue}
                        roster={everyone}
                        resolvedOf={resolvedOf}
                        groupsOf={groupsOf}
                        groupById={groupById}
                        allGroups={groups}
                        roleLabelOf={roleLabelOf}
                        onChangeGroups={setGroupsFor}
                        onPickPerson={(identity) => {
                          flip("person");
                          setSubject(identity);
                        }}
                        onPickGroup={() => setTab("groups")} />
                    );
                  })()}
              </div>
            </div>
          </div>
        )}

        {data && tab === "capabilities" && (
          <div className="max-h-[62vh] overflow-y-auto p-4">
            <p className="mb-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                          text-xs text-gray-600">
              Capabilities are <b>fixed and ship with PISR</b> — a route names
              one in code, so one invented here would be a permission nothing
              enforces. What you compose is which of them a role carries.
            </p>
            <div className="space-y-2">
              {data.grants.map((cap) => {
                const unlocks = data.features.filter((f) => f.requires.includes(cap.id));
                const held = roles.filter((r) => r.grants.includes(cap.id));
                return (
                  <div key={cap.id} className="min-w-0 rounded-md border border-gray-200 p-3">
                    <p className="min-w-0 text-sm font-medium text-gray-900">
                      {cap.label}{" "}
                      <span className="font-mono text-xs text-gray-400">{cap.id}</span>
                    </p>
                    {cap.hint && <p className="mt-1 text-xs text-gray-600">{cap.hint}</p>}
                    <p className="mt-1.5 text-xs text-gray-500">
                      Unlocks:{" "}
                      {unlocks.length
                        ? unlocks.map((f) => f.requires.length > 1
                            ? `${f.label} (with ${f.requires.filter((g) => g !== cap.id).join(", ")})`
                            : f.label).join(", ")
                        : "nothing yet — defined ahead of the view that will use it"}
                    </p>
                    <p className="mt-0.5 text-xs text-gray-500">
                      Held by: {held.length ? held.map((r) => r.label).join(", ") : "no role"}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {data && tab === "matrix" && (
          <div className="max-h-[62vh] min-w-0 overflow-auto p-4">
            <div className="mb-4 rounded-md border border-gray-200 bg-gray-50 p-3">
              <label className="block text-xs font-medium text-gray-700">
                Why can a particular person not see something?
              </label>
              <div className="mt-1 flex flex-wrap gap-2">
                <input list="pisr-roles-people" value={previewOf}
                       onChange={(e) => setPreviewOf(e.target.value)}
                       onKeyDown={(e) => { if (e.key === "Enter") runPreview(previewOf); }}
                       placeholder="Username"
                       className="min-w-0 flex-1 rounded-md border border-gray-300
                                  px-2 py-1.5 text-sm" />
                <datalist id="pisr-roles-people">
                  {everyone.map((p) => <option key={p} value={p} />)}
                </datalist>
                <button onClick={() => runPreview(previewOf)} disabled={!previewOf.trim()}
                        className={shell}>Resolve</button>
              </div>

              {preview && (
                <div className="mt-3 space-y-2 border-t border-gray-200 pt-3 text-xs">
                  {preview.warnings.map((w) => (
                    <p key={w} className="flex items-start gap-1.5 rounded border
                                          border-amber-200 bg-amber-50 px-2 py-1.5
                                          text-amber-900">
                      <TriangleAlert size={13} className="mt-0.5 shrink-0" />
                      <span>{w}</span>
                    </p>
                  ))}
                  <p className="text-gray-700">
                    <b>{preview.identity}</b> holds the{" "}
                    <b>{preview.role?.label ?? preview.roleId ?? "—"}</b> role.
                  </p>
                  <p className="min-w-0 text-gray-700">
                    <b>Can use:</b>{" "}
                    {preview.features.filter((f) => f.has).map((f) => f.label).join(", ")
                      || <span className="text-gray-500">nothing</span>}
                  </p>
                  {preview.features.some((f) => !f.has) && (
                    <p className="min-w-0 text-gray-700">
                      <b>Cannot use:</b>{" "}
                      {preview.features.filter((f) => !f.has)
                        .map((f) => `${f.label} (needs ${f.missing.join(", ")})`)
                        .join("; ")}
                    </p>
                  )}
                  <p className="min-w-0 break-words text-gray-700">
                    <b>Can reach:</b>{" "}
                    {preview.scope.unrestricted
                      ? "every customer and venue"
                      : Object.keys(preview.scope.ecs).length
                        ? Object.entries(preview.scope.ecs).map(([ec, v]) =>
                            `${ec} (${v === "*" ? "all venues" : `${(v as string[]).length} venue(s)`})`)
                          .join(", ")
                        : "nothing — an empty selection means no customers"}
                    <span className="ml-1 text-gray-500">
                      — {preview.scope.source === "person"
                          ? "from their own entry on the People tab"
                          : preview.scope.source === "role"
                            ? "from their role's default, not theirs"
                            : "nobody has set a restriction"}
                    </span>
                  </p>
                  <p className="min-w-0 text-gray-700">
                    <b>Cannot see:</b>{" "}
                    {preview.hidden.length
                      ? `${preview.hidden.length} card(s), from their role`
                      : "nothing is hidden from them"}
                  </p>
                </div>
              )}
            </div>

            {!caps && <p className="flex items-center gap-2 p-4 text-sm text-gray-500">
              <Loader2 size={15} className="animate-spin" /> Reading the route table…
            </p>}
            {caps && (
              <>
                <p className="mb-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                              text-xs text-gray-600">
                  <b>Narrowed</b> is the state that matters: a tool being
                  reachable never means entitled to everything behind it.{" "}
                  {caps.caveat}
                </p>
                <table className="w-full min-w-0 text-sm">
                  <thead>
                    <tr className="border-b border-gray-200 text-left align-bottom">
                      <th className="px-2 py-1.5 text-[11px] uppercase tracking-wide
                                     text-gray-500">Feature</th>
                      <th className="px-2 py-1.5 text-[11px] uppercase tracking-wide
                                     text-gray-500">Needs</th>
                      {caps.combinations.map((c) => (
                        <th key={c.key} className="px-2 py-1.5 text-xs font-medium text-gray-700">
                          <div className="whitespace-nowrap">{c.roleLabel}</div>
                          <div className="text-[11px] font-normal text-gray-500">
                            {c.holders.length ? `${c.holders.length} person(s)` : "nobody yet"}
                            {c.scopedHolders ? ` · ${c.scopedHolders} scoped` : ""}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {caps.features.map((f) => (
                      <Fragment key={f.id}>
                        <tr className="border-b border-gray-100">
                          <td className="min-w-0 px-2 py-1.5">
                            <button onClick={() => setOpenFeature(openFeature === f.id ? null : f.id)}
                                    className="flex min-w-0 items-center gap-1 text-left">
                              {openFeature === f.id
                                ? <ChevronDown size={13} className="shrink-0 text-gray-400" />
                                : <ChevronRight size={13} className="shrink-0 text-gray-400" />}
                              <span className="break-words text-gray-900">{f.label}</span>
                            </button>
                            {!f.wired && f.kind !== "open" && (
                              <span className="ml-4 rounded bg-gray-100 px-1 text-[10px] text-gray-600">
                                no route — checked in a handler, or not yet built
                              </span>
                            )}
                          </td>
                          <td className="whitespace-nowrap px-2 py-1.5 font-mono text-[11px]
                                         text-gray-500">
                            {f.requires.join(" + ") || "—"}
                          </td>
                          {caps.combinations.map((c) => {
                            const state = caps.cells[c.key]?.[f.id];
                            return (
                              <td key={c.key} className="whitespace-nowrap px-2 py-1.5 text-xs">
                                {state === "full" && <span className="text-green-700">yes</span>}
                                {state === "narrowed" && (
                                  <span className="text-green-700">
                                    yes <span className="text-gray-500">· narrowed</span>
                                  </span>
                                )}
                                {state === "refused" && <span className="text-gray-300">—</span>}
                              </td>
                            );
                          })}
                        </tr>
                        {openFeature === f.id && (
                          <tr className="border-b border-gray-100 bg-gray-50">
                            <td colSpan={caps.combinations.length + 2} className="px-6 py-2">
                              {f.hint && <p className="text-xs text-gray-600">{f.hint}</p>}
                              {f.routes.map((r) => (
                                <p key={`${r.methods.join()}${r.path}`}
                                   className="mt-1 font-mono text-[11px] text-gray-600">
                                  {r.methods.join("/")} {r.path}
                                  {r.narrows && (
                                    <span className="ml-1 font-sans text-gray-400">
                                      · narrowed by scope
                                    </span>
                                  )}
                                </p>
                              ))}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

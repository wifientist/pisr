import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown, ChevronRight, Loader2, ShieldCheck, TriangleAlert, X,
} from "lucide-react";
import AdminScope, { type ScopeState } from "@/pages/AdminScope";

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
 *   Roles          what does each job allow, and what does it hide
 *   People         who holds which, and whose network they work on
 *   Capabilities   what does each capability actually unlock
 *   Who can do what  the derived matrix, plus "why can this person not…"
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
  mayEditRoles: boolean;
  superGrant: string;
  policy: {
    roles: Role[];
    people: Record<string, { role: string | null; scope: ScopeState | null }>;
    orphanedAssignments: Record<string, string>;
    writable: boolean;
    path: string | null;
  };
}

const TABS = [
  ["roles", "Roles"],
  ["people", "Who sees what"],
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
  const [openRole, setOpenRole] = useState<string | null>(null);
  /** Which parts of the element tree are expanded. Independent of what is
   *  ticked — see the note in the tree itself. */
  const [openTabs, setOpenTabs] = useState<Set<string>>(new Set());
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());
  const [subject, setSubject] = useState<string>("");
  /** identity -> role id, as the ACCOUNTS say it. Read-only here. */
  const [accountRoles, setAccountRoles] = useState<Record<string, string>>({});

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
        setPeople(Object.fromEntries(
          Object.entries(payload.policy.people).map(([identity, e]) => [
            identity, { identity, role: e.role ?? null, scope: e.scope ?? null },
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
    const now = Object.fromEntries(Object.entries(people)
      .map(([i, p]) => [i, { role: p.role, scope: p.scope }]));
    return JSON.stringify(now) !== JSON.stringify(data.policy.people);
  }, [data, roles, people]);

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
          people: Object.fromEntries(Object.entries(people)
            .filter(([, p]) => p.role || (p.scope && !p.scope.unrestricted))
            .map(([i, p]) => [i, {
              role: p.role,
              scope: p.scope && !p.scope.unrestricted ? p.scope : null,
            }])),
        }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      setData((c) => (c ? { ...c, policy: body } : c));
      setRoles(body.roles);
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
                const holders = Object.values(people).filter((p) => p.role === role.id);
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

        {data && tab === "people" && (
          <div className="max-h-[62vh] overflow-y-auto p-4">
            <p className="mb-3 rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                          text-xs text-gray-600">
              <b>Whose network</b> each person works on. What KIND of work they
              do is their role, set on the account in the People portal.
              <br />
              Scope is per person and a role is not — otherwise two leads doing
              identical work in different territories would need two roles, and
              the list would grow with your org chart rather than with the
              kinds of work people do.
            </p>

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

            <label className="block text-xs font-medium text-gray-700">Person</label>
            <select value={subject} onChange={(e) => setSubject(e.target.value)}
                    className="mt-1 w-full min-w-0 rounded-md border border-gray-300
                               px-2 py-1.5 text-sm">
              <option value="">Choose somebody…</option>
              {everyone.map((identity) => (
                <option key={identity} value={identity}>
                  {identity}
                  {assignedRole(identity)
                    ? ` — ${roleById[assignedRole(identity)!]?.label
                            ?? assignedRole(identity)}`
                    : ""}
                </option>
              ))}
            </select>

            {subject && (
              <div className="mt-3 space-y-3">
                {/*
                  THE ROLE IS SHOWN, NOT SET — in this mode. It is a field on
                  the ACCOUNT, chosen when the account is created and changed
                  in the People portal, which is where a person is
                  administered. A second dropdown here would be two places to
                  change one thing with no answer to which wins.

                  It is still worth SHOWING, because the question this tab
                  answers is "whose network does this person work on", and that
                  only means something beside what kind of work they do.
                */}
                {data.people.length > 0 ? (
                  <p className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2
                                text-xs text-gray-600">
                    Holds the <b>{roleById[assignedRole(subject)]?.label
                                  ?? assignedRole(subject) ?? "—"}</b> role.
                    Change that in the <b>People</b> portal, where the account
                    lives — this tab sets whose network they work on.
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
                        [subject]: { identity: subject, role: e.target.value || null,
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

                <div className="border-t border-gray-200 pt-3">
                  <p className="text-xs font-medium text-gray-700">
                    Which customers and venues
                  </p>
                  <AdminScope
                    key={subject}
                    scope={people[subject]?.scope ?? { unrestricted: true, ecs: {} }}
                    onChange={(next) => setPeople((c) => ({
                      ...c,
                      [subject]: { identity: subject, role: c[subject]?.role ?? null,
                                   scope: next },
                    }))} />
                </div>
              </div>
            )}
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

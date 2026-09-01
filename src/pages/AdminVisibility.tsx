import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronRight, Eye, EyeOff, Loader2, ShieldCheck, X } from "lucide-react";
import AdminScope, { ALL_VENUES, type ScopeState } from "@/pages/AdminScope";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * The admin portal: which report sections an ordinary user is shown.
 *
 * ONE DENY LIST, NOT TWO. The server stores the sections hidden from `user`,
 * and admins are never subject to it. Two lists — "what guests see" and "what
 * admins see" — can disagree with each other, and a section added after the
 * policy was written would be in neither with no defensible answer for what to
 * do about it. See api/visibility.py.
 *
 * WHAT A TICK ACTUALLY DOES. Hiding a section stops its data leaving the
 * server for a user's report, on the screen and in the PDF alike, and drops
 * the findings that belong to it. It is de-cluttering applied centrally, not
 * a confidentiality boundary: both roles are fully authenticated, and the
 * policy fails open on every error path. Read api/visibility.py before
 * treating it as one.
 *
 * The whole policy is sent on save rather than a diff. Two admins in two tabs
 * would otherwise silently combine their edits into a policy neither chose;
 * last-write-wins is the honest behaviour for a setting changed a few times a
 * year, and this way it is visible rather than surprising.
 */

interface Element { id: string; label: string }
interface SectionRow {
  id: string;
  label: string;
  tab: string;
  tabLabel: string;
  hint: string;
  paths: string[];
  checks: Element[];
  columns: Element[];
}

interface Policy {
  version: number;
  hidden: Record<string, string[]>;
  scope: Record<string, ScopeState>;
  updatedAt: string | null;
  updatedBy: string | null;
  writable: boolean;
  path: string | null;
}

interface Group { id: string; label: string; hint: string; ids: string[] }

interface Payload {
  sections: SectionRow[];
  groups: Group[];
  tabs: { id: string; label: string }[];
  roles: string[];
  policy: Policy;
}

const ROLE = "user";   // the only managed role today — see MANAGED_ROLES

export default function AdminVisibility({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<"sections" | "checks" | "access">("sections");
  const [data, setData] = useState<Payload | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  // Which sections are expanded to show their per-check / per-column toggles.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<ScopeState>({ unrestricted: true, ecs: {} });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    fetch(`${API_BASE_URL}/admin/visibility`, { credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => null);
          throw new Error(body?.detail || `HTTP ${r.status}`);
        }
        return r.json();
      })
      .then((payload: Payload) => {
        setData(payload);
        setHidden(new Set(payload.policy.hidden?.[ROLE] || []));
        setScope(payload.policy.scope?.[ROLE] || { unrestricted: true, ecs: {} });
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  // Escape closes. A full-screen overlay with no keyboard exit is the kind of
  // thing that gets reported as the app having frozen.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const byTab = useMemo(() => {
    const groups = new Map<string, { label: string; rows: SectionRow[] }>();
    for (const section of data?.sections || []) {
      const group = groups.get(section.tab)
        || { label: section.tabLabel, rows: [] as SectionRow[] };
      group.rows.push(section);
      groups.set(section.tab, group);
    }
    return [...groups.entries()];
  }, [data]);

  // The Checks tab: every check, flat, grouped by the section that owns it.
  // The toggles bind to the SAME hidden ids as the ones nested in the Sections
  // tree — there is one `hidden` set and one `toggle`, so a check disabled here
  // is disabled there and vice-versa, with no second source of truth to drift.
  const checkGroups = useMemo(() => {
    const groups = new Map<string, { label: string; rows: SectionRow[] }>();
    for (const section of data?.sections || []) {
      if (!section.checks.length) continue;
      const group = groups.get(section.tab)
        || { label: section.tabLabel, rows: [] as SectionRow[] };
      group.rows.push(section);
      groups.set(section.tab, group);
    }
    return [...groups.entries()];
  }, [data]);

  const original = useMemo(
    () => new Set(data?.policy.hidden?.[ROLE] || []), [data]);
  const originalScope = useMemo(
    () => JSON.stringify(data?.policy.scope?.[ROLE] || { unrestricted: true, ecs: {} }),
    [data]);
  const dirty = useMemo(() => {
    if (JSON.stringify(scope) !== originalScope) return true;
    if (hidden.size !== original.size) return true;
    for (const id of hidden) if (!original.has(id)) return true;
    return false;
  }, [hidden, original, scope, originalScope]);

  const toggle = (id: string) => {
    setSaved(null);
    setHidden((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const setTabAll = (rows: SectionRow[], hide: boolean) => {
    setSaved(null);
    setHidden((current) => {
      const next = new Set(current);
      for (const row of rows) if (hide) next.add(row.id); else next.delete(row.id);
      return next;
    });
  };

  // A group switch: hide or show every element id it names at once. The group
  // holds no state — it reads back "hidden" only when all its ids are hidden,
  // so it and the individual toggles can never disagree.
  const setMany = (ids: string[], hide: boolean) => {
    setSaved(null);
    setHidden((current) => {
      const next = new Set(current);
      for (const id of ids) if (hide) next.add(id); else next.delete(id);
      return next;
    });
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const res = await fetch(`${API_BASE_URL}/admin/visibility`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          hidden: { [ROLE]: [...hidden].sort() },
          // Sent as null when unrestricted so the stored file has exactly one
          // way to say "no restriction" — see visibility.PolicyStore._clean_scope.
          scope: scope.unrestricted ? null : { [ROLE]: scope },
        }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      setData((current) => (current ? { ...current, policy: body as Policy } : current));
      const ecCount = Object.keys(scope.ecs).length;
      setSaved(`Saved. ${hiddenSections} section(s)`
        + (hiddenElements ? ` and ${hiddenElements} element(s)` : "") + ` hidden; `
        // "named", not "reachable": an id in the policy that this tenant does
        // not contain still counts here, and calling it reachable contradicted
        // the warning shown right above it in AdminScope.
        + (scope.unrestricted ? "every customer reachable." : `${ecCount} customer id(s) named.`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const readOnly = Boolean(data && !data.policy.writable);

  // `hidden` holds ids at three levels — sections, checks, columns — so the
  // plain size would read "12 of 8 sections hidden" once an admin hides a few
  // checks. Split it: whole sections against the section total, elements
  // (checks and columns) counted on their own.
  const sectionIds = useMemo(
    () => new Set((data?.sections || []).map((s) => s.id)), [data]);
  const hiddenSections = useMemo(
    () => [...hidden].filter((id) => sectionIds.has(id)).length, [hidden, sectionIds]);
  const hiddenElements = hidden.size - hiddenSections;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto
                    bg-gray-900/40 p-4 sm:p-8">
      {/* min-w-0 on the panel and every row below it: this is a grid of
          truncating labels and long dotted paths, and without it an id like
          `wireless.clients-per-ssid` sets the width of the dialog on a phone.
          See the note in CLAUDE.md — the overflow is invisible on a desktop. */}
      <div className="w-full max-w-3xl min-w-0 rounded-lg border border-gray-200 bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-semibold text-gray-900">
              <ShieldCheck size={17} className="text-gray-400 shrink-0" />
              Report sections
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
              What ordinary users see. Admins are never subject to any of it,
              which is what makes this safe to edit.
            </p>
          </div>
          <button onClick={onClose} aria-label="Close"
                  className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
            <X size={18} />
          </button>
        </div>

        {/* Two tabs because these are two different kinds of setting and the
            dialog should not let them look alike: Sections tidies a report and
            fails open, Access decides what a user may open at all and fails
            closed. See api/scope.py. */}
        <div className="flex gap-1 border-b border-gray-200 px-4">
          {([["sections", "Sections"], ["checks", "Checks"], ["access", "Access"]] as const).map(([id, label]) => (
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

        {readOnly && (
          <p className="m-4 rounded-md bg-amber-50 border border-amber-200 px-3 py-2 text-sm text-amber-900">
            <b>Read-only.</b> {data?.policy.path} is not writable by the container,
            so changes cannot be saved. Mount a writable volume at its directory —
            without one a policy written inside the container is lost at the next
            deploy, which looks like the save silently undoing itself.
          </p>
        )}

        {!data && !error && (
          <p className="flex items-center gap-2 p-8 text-sm text-gray-500">
            <Loader2 size={15} className="animate-spin" /> Loading the catalogue…
          </p>
        )}

        {data && tab === "access" && (
          <div className="max-h-[60vh] overflow-y-auto p-4">
            <AdminScope scope={scope} onChange={setScope} />
          </div>
        )}

        {data && tab === "sections" && (
          <div className="max-h-[60vh] overflow-y-auto p-4 space-y-5">
            <p className="text-xs text-gray-500">
              Tick a section to hide it from users, on the screen and in the PDF
              alike. This is de-cluttering, not confidentiality — a user can
              still be told a fact by another card that mentions it.
            </p>

            {/* Cross-cutting switches. A concept like VLANs is not one section —
                it is a table, a column and a config category in three tabs — so
                one toggle here hides or shows all of them at once. The section
                toggles below stay in sync because this writes the same ids. */}
            {(data.groups || []).length > 0 && (
              <div className="rounded-md border border-blue-200 bg-blue-50/50 p-3 space-y-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-blue-900">
                  Quick switches
                </h3>
                {data.groups.map((group) => {
                  const known = group.ids.filter((id) => data.sections.some((s) =>
                    s.id === id || s.checks.some((c) => c.id === id)
                                 || s.columns.some((c) => c.id === id)));
                  const allHidden = known.length > 0 && known.every((id) => hidden.has(id));
                  const someHidden = known.some((id) => hidden.has(id));
                  return (
                    <label key={group.id}
                           className="flex items-start gap-3 cursor-pointer min-w-0">
                      <input type="checkbox"
                             ref={(el) => { if (el) el.indeterminate = someHidden && !allHidden; }}
                             checked={allHidden}
                             onChange={() => setMany(known, !allHidden)}
                             className="mt-0.5 shrink-0" />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="text-sm font-medium text-gray-900">{group.label}</span>
                          <span className="text-[11px] text-gray-500">
                            {allHidden ? "hidden" : someHidden ? "partly hidden" : "shown"}
                            {" · "}{known.length} element(s)
                          </span>
                        </span>
                        {group.hint && (
                          <span className="block text-xs text-gray-500">{group.hint}</span>
                        )}
                      </span>
                    </label>
                  );
                })}
              </div>
            )}
            {byTab.map(([groupTab, group]) => {
              const allHidden = group.rows.every((row) => hidden.has(row.id));
              return (
                <div key={groupTab} className="min-w-0">
                  <div className="flex items-center justify-between gap-3 mb-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                      {group.label}
                    </h3>
                    <button
                      onClick={() => setTabAll(group.rows, !allHidden)}
                      className="text-xs text-blue-700 hover:text-blue-900 shrink-0"
                    >
                      {allHidden ? "Show all" : "Hide all"}
                    </button>
                  </div>
                  <div className="space-y-1">
                    {group.rows.map((row) => {
                      const isHidden = hidden.has(row.id);
                      const elements = [...row.checks, ...row.columns];
                      const isOpen = expanded.has(row.id);
                      return (
                        <div key={row.id}
                             className={`rounded-md border min-w-0 ${isHidden
                               ? "border-gray-300 bg-gray-50"
                               : "border-gray-200"}`}>
                          <div className="flex items-start gap-2 px-2 py-2">
                            {/* Expander, only when there is something inside. */}
                            {elements.length > 0 ? (
                              <button type="button" onClick={() => setExpanded((e) => {
                                        const n = new Set(e); n.has(row.id) ? n.delete(row.id) : n.add(row.id); return n;
                                      })}
                                      className="mt-0.5 shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100"
                                      aria-label={isOpen ? "Collapse" : "Expand"}>
                                <ChevronRight size={14} className={`transition-transform ${isOpen ? "rotate-90" : ""}`} />
                              </button>
                            ) : <span className="w-[22px] shrink-0" />}
                            <label className="flex min-w-0 flex-1 items-start gap-3 cursor-pointer">
                              <input type="checkbox" checked={isHidden}
                                     onChange={() => toggle(row.id)} className="mt-1 shrink-0" />
                              <span className="min-w-0 flex-1">
                                <span className="flex items-center gap-2 min-w-0">
                                  <span className={`text-sm font-medium truncate ${
                                    isHidden ? "text-gray-500" : "text-gray-900"}`}>{row.label}</span>
                                  {isHidden
                                    ? <EyeOff size={13} className="shrink-0 text-gray-400" />
                                    : <Eye size={13} className="shrink-0 text-green-600" />}
                                </span>
                                {row.hint && <span className="block text-xs text-gray-500">{row.hint}</span>}
                                <span className="block text-[11px] text-gray-400 font-mono break-all">
                                  {row.id}
                                  {row.paths.length === 0 && " · display only"}
                                  {elements.length > 0 && ` · ${row.checks.length} check(s)${row.columns.length ? `, ${row.columns.length} column(s)` : ""}`}
                                </span>
                              </span>
                            </label>
                          </div>
                          {/* The finer toggles. A section hidden as a whole makes
                              its children moot, so they read dimmed and their own
                              state does not matter until it is shown again. */}
                          {isOpen && (
                            <div className={`border-t border-gray-100 px-3 py-2 space-y-1
                                             ${isHidden ? "opacity-50" : ""}`}>
                              {elements.map((el, i) => {
                                const kind = i < row.checks.length ? "check" : "column";
                                const elHidden = hidden.has(el.id);
                                return (
                                  <label key={el.id}
                                         className="flex items-center gap-2 text-xs text-gray-700 cursor-pointer">
                                    <input type="checkbox" checked={elHidden}
                                           disabled={isHidden}
                                           onChange={() => toggle(el.id)} className="shrink-0" />
                                    <span className={elHidden ? "text-gray-400 line-through" : ""}>{el.label}</span>
                                    <span className="rounded bg-gray-100 px-1 text-[10px] text-gray-500">{kind}</span>
                                  </label>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {data && tab === "checks" && (
          <div className="max-h-[60vh] overflow-y-auto p-4 space-y-5">
            <p className="text-xs text-gray-500">
              Every check in the report, in one place. Ticking one hides that
              check's finding from users — the same toggle as the one nested
              under its section on the <b>Sections</b> tab, so changing it in
              either place changes both. A check whose whole section is hidden
              is already gone; it shows here dimmed.
            </p>
            {checkGroups.map(([groupTab, group]) => (
              <div key={groupTab} className="min-w-0">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">
                  {group.label}
                </h3>
                <div className="space-y-3">
                  {group.rows.map((section) => {
                    const sectionHidden = hidden.has(section.id);
                    return (
                      <div key={section.id} className="min-w-0">
                        <div className="mb-1 flex items-center gap-2 text-[11px] text-gray-500">
                          <span className="truncate font-medium">{section.label}</span>
                          {sectionHidden && (
                            <span className="shrink-0 rounded bg-gray-100 px-1 text-[10px] text-gray-500">
                              section hidden
                            </span>
                          )}
                        </div>
                        <div className={`space-y-1 pl-1 ${sectionHidden ? "opacity-50" : ""}`}>
                          {section.checks.map((check) => {
                            const checkHidden = hidden.has(check.id);
                            return (
                              <label key={check.id}
                                     className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                                <input type="checkbox" checked={checkHidden}
                                       disabled={sectionHidden}
                                       onChange={() => toggle(check.id)} className="shrink-0" />
                                <span className={`min-w-0 truncate ${
                                  checkHidden ? "text-gray-400 line-through" : ""}`}>
                                  {check.label}
                                </span>
                                <span className="ml-auto shrink-0 font-mono text-[10px] text-gray-400 break-all">
                                  {check.id}
                                </span>
                              </label>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}

        {data && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 p-4">
            <p className="min-w-0 text-xs text-gray-500">
              {hiddenSections} of {data.sections.length} sections hidden
              {hiddenElements > 0 && `, ${hiddenElements} element(s)`} ·{" "}
              {scope.unrestricted
                ? "every customer reachable"
                : `${Object.keys(scope.ecs).length} customer id(s) named`}.
              {data.policy.updatedAt && (
                <> Last saved {new Date(data.policy.updatedAt).toLocaleString()}
                  {data.policy.updatedBy ? ` by ${data.policy.updatedBy}` : ""}.</>
              )}
              {saved && <span className="text-green-700"> {saved}</span>}
            </p>
            <div className="flex items-center gap-2 shrink-0">
              <button onClick={onClose}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm
                                 font-medium text-gray-700 hover:bg-gray-50">
                Close
              </button>
              <button onClick={save} disabled={busy || readOnly || !dirty}
                      className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5
                                 text-sm font-medium text-white hover:bg-blue-700
                                 disabled:opacity-50">
                {busy && <Loader2 size={14} className="animate-spin" />}
                {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

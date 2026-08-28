import { useCallback, useEffect, useMemo, useState } from "react";
import { Eye, EyeOff, Loader2, ShieldCheck, X } from "lucide-react";
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

interface SectionRow {
  id: string;
  label: string;
  tab: string;
  tabLabel: string;
  hint: string;
  paths: string[];
  checks: string[];
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

interface Payload {
  sections: SectionRow[];
  tabs: { id: string; label: string }[];
  roles: string[];
  policy: Policy;
}

const ROLE = "user";   // the only managed role today — see MANAGED_ROLES

export default function AdminVisibility({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<"sections" | "access">("sections");
  const [data, setData] = useState<Payload | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
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
      setSaved(`Saved. ${hidden.size} section(s) hidden; `
        + (scope.unrestricted ? "every customer reachable." : `${ecCount} customer(s) reachable.`));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const readOnly = Boolean(data && !data.policy.writable);

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
          {([["sections", "Sections"], ["access", "Access"]] as const).map(([id, label]) => (
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
                      return (
                        <label key={row.id}
                               className={`flex items-start gap-3 rounded-md border px-3 py-2 cursor-pointer
                                           min-w-0 ${isHidden
                                             ? "border-gray-300 bg-gray-50"
                                             : "border-gray-200 hover:bg-gray-50"}`}>
                          <input
                            type="checkbox"
                            checked={isHidden}
                            onChange={() => toggle(row.id)}
                            className="mt-1 shrink-0"
                          />
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-2 min-w-0">
                              <span className={`text-sm font-medium truncate ${
                                isHidden ? "text-gray-500" : "text-gray-900"}`}>
                                {row.label}
                              </span>
                              {isHidden
                                ? <EyeOff size={13} className="shrink-0 text-gray-400" />
                                : <Eye size={13} className="shrink-0 text-green-600" />}
                            </span>
                            {row.hint && (
                              <span className="block text-xs text-gray-500">{row.hint}</span>
                            )}
                            <span className="block text-[11px] text-gray-400 font-mono break-all">
                              {row.id}
                              {/* Said out loud so an admin can tell the two kinds
                                  apart: a section with paths is withheld from the
                                  payload, one without is only un-drawn because
                                  every number in it is read from data another
                                  section owns. */}
                              {row.paths.length === 0 && " · display only"}
                              {row.checks.length > 0 && ` · ${row.checks.length} check(s)`}
                            </span>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {data && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-gray-200 p-4">
            <p className="min-w-0 text-xs text-gray-500">
              {hidden.size} of {data.sections.length} sections hidden ·{" "}
              {scope.unrestricted
                ? "every customer reachable"
                : `${Object.keys(scope.ecs).length} customer(s) reachable`}.
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

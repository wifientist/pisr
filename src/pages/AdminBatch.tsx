import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft, ExternalLink, FileDown, Layers, Loader2, Play, RefreshCw, Square, X,
} from "lucide-react";
import { apiFetch } from "@/utils/api";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * Batch runs: check every venue of a tenant and keep the counts.
 *
 * STARTED BY A PERSON, RUN BY THIS TAB. There is no scheduler behind it: the
 * loop below calls the server once per venue, a few at a time, so no request
 * outlives Cloudflare's 100-second origin timeout and closing the dialog stops
 * the batch. The server records each venue's outcome as it lands
 * (api/batch_runs.py), which is what lets an interrupted run be picked up by
 * running only the venues that are not fresh.
 *
 * WHAT IS KEPT is counts, plus WHICH checks failed by id — see
 * api/services/pisr/rollup.py. Never a finding's own words: for those, and for
 * the devices behind them, every venue row links to the live report.
 *
 * Admin only. The chip is offered on the server's role; `require_admin` on
 * /api/admin/batch is the control.
 */

interface Summary {
  total: number;
  counts: { critical: number; warning: number; info: number };
  byCategory: Record<string, Record<string, number>>;
  skipped: number;
  passed: number;
  deviceCount: number;
  aps: number;
  switches: number;
  clients: number;
  alarms: number;
  readErrors: string[];
  /** {severity: {checkId: deviceCount}} — ids from PISR's own catalogue. */
  checks?: Record<string, Record<string, number>>;
}

type VenueStatus = "ok" | "partial" | "failed";

interface VenueRecord {
  venueId: string;
  name: string;
  listed: boolean;
  lastCompleteAt: string | null;
  lastAttemptAt: string | null;
  lastAttemptStatus: VenueStatus | null;
  lastAttemptError: string | null;
  lastAttemptReadErrors: string[];
  fresh: boolean;
  ageDays: number | null;
  summary: Summary | null;
}

interface Totals {
  total: number;
  counts: { critical: number; warning: number; info: number };
  byCategory: Record<string, number>;
  skipped: number;
}

interface Run {
  id: string;
  tenantId: string;
  tenantName: string | null;
  startedAt: string;
  startedBy: string;
  finishedAt: string | null;
  stopped: boolean;
  planned: number;
  venueIds: string[];
  results: Record<string, VenueStatus>;
  tally: { ok: number; partial: number; failed: number; notRun: number };
  status: "complete" | "incomplete" | "running" | "interrupted";
}

interface TenantRollup {
  tenantId: string;
  name: string;
  listedAt: string | null;
  venueCount: number;
  checked: number;
  fresh: number;
  /** Checked venues with no critical and no warning — the number to watch. */
  clean: number;
  neverChecked: number;
  lastAttemptNotOk: number;
  venuesWithCritical: number;
  totals: Totals;
  lastRun: Run | null;
  venues?: VenueRecord[];
}

interface BatchState {
  configured: boolean;
  writable: boolean;
  broken: boolean;
  path: string | null;
  isMsp: boolean;
  days: number;
  categories: { key: string; label: string }[];
  msp: { tenantCount: number; venueCount: number; checked: number; fresh: number;
         clean: number; totals: Totals; tenants: TenantRollup[] };
  tenant: TenantRollup | null;
  runs: Run[];
}

interface LiveVenue { id: string; name: string }

interface Progress {
  runId: string;
  total: number;
  done: number;
  ok: number;
  partial: number;
  failed: number;
  active: string[];
  stopping: boolean;
  halted: string | null;
}

/** Scheme + host of wherever this admin reached PISR — the server cannot know it. */
function reportLink(isMsp: boolean, tenantId: string | null, tenantName: string | null,
                    venueId: string): string {
  const params = new URLSearchParams();
  if (isMsp && tenantId) {
    params.set("ec", tenantId);
    if (tenantName) params.set("ecName", tenantName);
  }
  params.set("venue", venueId);
  return `${window.location.origin}/?${params.toString()}`;
}

function when(stamp: string | null): string {
  if (!stamp) return "never";
  const d = new Date(stamp);
  return Number.isNaN(d.getTime()) ? stamp : d.toLocaleString();
}

function ago(days: number | null): string {
  if (days === null) return "";
  if (days < 1 / 24) return "just now";
  if (days < 1) return `${Math.round(days * 24)}h ago`;
  return `${Math.round(days)}d ago`;
}

const RUN_TONE: Record<Run["status"], string> = {
  complete: "bg-green-50 text-green-700",
  incomplete: "bg-amber-50 text-amber-800",
  running: "bg-blue-50 text-blue-700",
  interrupted: "bg-red-50 text-red-700",
};

const STATUS_TONE: Record<VenueStatus, string> = {
  ok: "bg-green-50 text-green-700",
  partial: "bg-amber-50 text-amber-800",
  failed: "bg-red-50 text-red-700",
};

type Severity = "critical" | "warning" | "info";
const SEVERITIES: Severity[] = ["critical", "warning", "info"];
const SEVERITY_LABEL: Record<Severity, string> = {
  critical: "Critical", warning: "Warning", info: "Info",
};

/**
 * How much the roll-up PDF says about each venue.
 *
 * Counts only by default for Info, because on a settled estate the info checks
 * are the long tail and listing them everywhere buries the two lines that
 * matter. Whatever is ticked here is listed per venue as named checks — the
 * check's catalogue label and how many devices it names, never the finding's
 * own text, which PISR does not keep.
 */
function DetailPicker({ value, onChange }: {
  value: Set<Severity>;
  onChange: (next: Set<Severity>) => void;
}) {
  return (
    <span className="flex items-center gap-2 text-xs text-gray-600">
      <span title="Which checks each venue lists by name in the PDF">List checks:</span>
      {SEVERITIES.map((sev) => (
        <label key={sev} className="flex items-center gap-1">
          <input type="checkbox" checked={value.has(sev)}
                 onChange={() => {
                   const next = new Set(value);
                   if (next.has(sev)) next.delete(sev); else next.add(sev);
                   onChange(next);
                 }} />
          {SEVERITY_LABEL[sev]}
        </label>
      ))}
    </span>
  );
}

function Count({ n, tone }: { n: number | undefined; tone: string }) {
  const value = n ?? 0;
  return (
    <td className={`px-2 py-1.5 text-right tabular-nums ${value ? tone : "text-gray-300"}`}>
      {value}
    </td>
  );
}

async function errorOf(res: Response): Promise<string> {
  const body = await res.json().catch(() => null);
  return body?.detail || `HTTP ${res.status}`;
}

export default function AdminBatch({ controllerId, isMsp, onClose }: {
  controllerId: number;
  isMsp: boolean;
  onClose: () => void;
}) {
  const [days, setDays] = useState(7);
  const [state, setState] = useState<BatchState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ecs, setEcs] = useState<{ id: string; name: string }[] | null>(null);
  const [tenant, setTenant] = useState<{ id: string | null; name: string | null } | null>(
    isMsp ? null : { id: null, name: null });
  const [venues, setVenues] = useState<LiveVenue[] | null>(null);
  const [venuesLoading, setVenuesLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [concurrency, setConcurrency] = useState(2);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [live, setLive] = useState<Record<string, { status: VenueStatus; summary: Summary | null }>>({});
  const [exporting, setExporting] = useState(false);
  // Which severities the PDF names checks for. Critical and warning by
  // default: those are the two a reader acts on, and info is the long tail.
  const [detail, setDetail] = useState<Set<Severity>>(
    () => new Set<Severity>(["critical", "warning"]));
  const [filter, setFilter] = useState("");
  const stopRef = useRef(false);

  const running = progress !== null && progress.done < progress.total && !progress.halted;
  const tenantQs = tenant?.id ? `tenant_id=${encodeURIComponent(tenant.id)}&` : "";

  const loadState = useCallback(async () => {
    try {
      const res = await apiFetch(`${API_BASE_URL}/admin/batch/state?${tenantQs}days=${days}`);
      if (!res.ok) throw new Error(await errorOf(res));
      setState(await res.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the batch record");
    }
  }, [tenantQs, days]);

  useEffect(() => { loadState(); }, [loadState]);

  // The MSP-EC list, through the ordinary route — an admin is unrestricted, so
  // it returns every EC, and there is deliberately no admin-only mirror of it.
  useEffect(() => {
    if (!isMsp) return;
    apiFetch(`${API_BASE_URL}/r1/${controllerId}/msp/mspEcs`)
      .then(async (r) => (r.ok ? r.json() : Promise.reject(new Error(await errorOf(r)))))
      .then((json) => {
        const rows = Array.isArray(json) ? json : Array.isArray(json?.data) ? json.data : [];
        setEcs(rows
          .map((ec: any) => ({ id: String(ec.id || ec.tenantId || ""), name: String(ec.name || ec.id || "") }))
          .filter((ec: { id: string }) => ec.id)
          .sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name)));
      })
      .catch((e: Error) => setError(e.message));
  }, [isMsp, controllerId]);

  // The tenant's live venue list — the denominator of "48 of 58". Read fresh
  // each time a tenant is opened, and sent with every run so the record's
  // idea of what the tenant has stays current.
  useEffect(() => {
    if (!tenant) { setVenues(null); return; }
    let cancelled = false;
    setVenuesLoading(true);
    setVenues(null);
    setSelected(new Set());
    setLive({});
    apiFetch(`${API_BASE_URL}/pisr/${controllerId}/venues${tenant.id ? `?tenant_id=${encodeURIComponent(tenant.id)}` : ""}`)
      .then(async (r) => (r.ok ? r.json() : Promise.reject(new Error(await errorOf(r)))))
      .then((json) => {
        if (cancelled) return;
        setVenues((json.venues || []).map((v: any) => ({ id: v.id, name: v.name || v.id })));
      })
      .catch((e: Error) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setVenuesLoading(false); });
    return () => { cancelled = true; };
  }, [tenant, controllerId]);

  const records = useMemo(() => {
    const map = new Map<string, VenueRecord>();
    for (const row of state?.tenant?.venues || []) map.set(row.venueId, row);
    return map;
  }, [state]);

  /** Live listing first (the tenant as it is now), then anything only on record. */
  const rows = useMemo(() => {
    const listed = (venues || []).map((v) => ({ id: v.id, name: v.name, listed: true,
                                                rec: records.get(v.id) || null }));
    const liveIds = new Set(listed.map((r) => r.id));
    const extra = [...records.values()]
      .filter((r) => !liveIds.has(r.venueId))
      .map((r) => ({ id: r.venueId, name: r.name, listed: false, rec: r }));
    return [...listed, ...extra];
  }, [venues, records]);

  const visibleRows = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return needle ? rows.filter((r) => r.name.toLowerCase().includes(needle)) : rows;
  }, [rows, filter]);

  const staleIds = useMemo(
    () => rows.filter((r) => r.listed && !r.rec?.fresh).map((r) => r.id), [rows]);

  const toggle = (id: string) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const tenantName = tenant?.name ?? state?.tenant?.name ?? null;

  const start = async (ids: string[]) => {
    if (!venues || !ids.length || running) return;
    setError(null);
    setLive({});
    stopRef.current = false;
    let run: Run;
    try {
      const res = await apiFetch(`${API_BASE_URL}/admin/batch/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tenantId: tenant?.id ?? null,
          tenantName,
          venues: venues.map((v) => ({ id: v.id, name: v.name })),
          venueIds: ids,
        }),
      });
      if (!res.ok) throw new Error(await errorOf(res));
      run = await res.json();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the run");
      return;
    }

    const tally = { done: 0, ok: 0, partial: 0, failed: 0 };
    const active = new Set<string>();
    let halted: string | null = null;
    const publish = () => setProgress({
      runId: run.id, total: ids.length, ...tally, active: [...active],
      stopping: stopRef.current, halted,
    });
    publish();

    let next = 0;
    const worker = async () => {
      while (!stopRef.current && !halted && next < ids.length) {
        const venueId = ids[next++];
        active.add(venueId);
        publish();
        let status: VenueStatus = "failed";
        let summary: Summary | null = null;
        try {
          const res = await apiFetch(
            `${API_BASE_URL}/admin/batch/runs/${run.id}/venues/${encodeURIComponent(venueId)}`,
            { method: "POST" });
          if (res.ok) {
            const body = await res.json();
            status = body.status;
            summary = body.summary;
          } else if ([401, 403, 409, 503].includes(res.status)) {
            // Not this venue's fault: the session ended, the store went away,
            // or the run was closed elsewhere. Every later venue would get the
            // same answer, so stop rather than record a wall of failures.
            halted = await errorOf(res);
          }
          // Any other non-2xx (a 502 or 524 from the tunnel) was not recorded
          // by the server; the venue stays not-fresh and the re-run picks it up.
        } catch {
          // Network error — likewise unrecorded, likewise picked up next time.
        }
        active.delete(venueId);
        tally.done += 1;
        tally[status] += 1;
        setLive((prev) => ({ ...prev, [venueId]: { status, summary } }));
        publish();
      }
    };
    await Promise.all(Array.from({ length: Math.max(1, concurrency) }, worker));

    await apiFetch(`${API_BASE_URL}/admin/batch/runs/${run.id}/finish`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stopped: stopRef.current || Boolean(halted) }),
    }).catch(() => undefined);
    // Unreached venues count as done for the bar, so it reads as finished.
    tally.done = ids.length;
    publish();
    if (halted) setError(`Batch stopped: ${halted}`);
    setSelected(new Set());
    loadState();
  };

  const stop = () => {
    stopRef.current = true;
    setProgress((p) => (p ? { ...p, stopping: true } : p));
  };

  const close = () => {
    if (running && !window.confirm(
      "A batch is running in this dialog. Closing it stops the batch after the " +
      "venues in flight finish. Close anyway?")) return;
    stopRef.current = true;
    onClose();
  };

  const downloadPdf = async (scopeTenant: boolean) => {
    setExporting(true);
    setError(null);
    try {
      const params = new URLSearchParams({ days: String(days), origin: window.location.origin });
      const wanted = SEVERITIES.filter((sev) => detail.has(sev));
      if (wanted.length) params.set("detail", wanted.join(","));
      if (scopeTenant && tenant?.id) params.set("tenant_id", tenant.id);
      const res = await apiFetch(`${API_BASE_URL}/admin/batch/rollup.pdf?${params}`);
      if (!res.ok) throw new Error(await errorOf(res));
      const blob = await res.blob();
      const name = res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1]
                   || "batch-rollup.pdf";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : "PDF export failed");
    } finally {
      setExporting(false);
    }
  };

  const cannotWrite = state && (!state.configured || state.broken || !state.writable);
  const btn = "inline-flex items-center gap-1.5 rounded-md border border-gray-300 bg-white " +
              "px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 " +
              "disabled:cursor-not-allowed disabled:opacity-50";

  // ── the MSP view: every tenant, summarised ─────────────────────────

  const mspView = () => {
    const byId = new Map((state?.msp.tenants || []).map((t) => [t.tenantId, t]));
    const list = ecs ?? [];
    const onlyOnRecord = (state?.msp.tenants || []).filter(
      (t) => !list.some((ec) => ec.id === t.tenantId));
    const all = [...list.map((ec) => ({ id: ec.id, name: ec.name, roll: byId.get(ec.id) })),
                 ...onlyOnRecord.map((t) => ({ id: t.tenantId, name: t.name, roll: t }))];
    const needle = filter.trim().toLowerCase();
    const shown = needle ? all.filter((t) => t.name.toLowerCase().includes(needle)) : all;
    return (
      <div className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <input value={filter} onChange={(e) => setFilter(e.target.value)}
                 placeholder="Filter tenants"
                 className="min-w-0 flex-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-sm" />
          <DetailPicker value={detail} onChange={setDetail} />
          <button className={btn} disabled={exporting} onClick={() => downloadPdf(false)}>
            {exporting ? <Loader2 size={13} className="animate-spin" /> : <FileDown size={13} />}
            MSP roll-up PDF
          </button>
        </div>
        {!ecs && <p className="mt-4 text-sm text-gray-500">Loading tenants…</p>}
        {ecs && (
          <div className="mt-3 min-w-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
                <tr className="border-b border-gray-200">
                  <th className="px-2 py-1.5">Tenant</th>
                  <th className="px-2 py-1.5 text-right">Checked</th>
                  <th className="px-2 py-1.5 text-right">Clean</th>
                  <th className="px-2 py-1.5 text-right">Crit</th>
                  <th className="px-2 py-1.5 text-right">Warn</th>
                  <th className="px-2 py-1.5 text-right">Info</th>
                  <th className="px-2 py-1.5">Last run</th>
                </tr>
              </thead>
              <tbody>
                {shown.map(({ id, name, roll }) => (
                  <tr key={id} onClick={() => { setFilter(""); setTenant({ id, name }); }}
                      className="cursor-pointer border-b border-gray-100 hover:bg-blue-50">
                    <td className="px-2 py-1.5 font-medium text-gray-900">{name}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-gray-600">
                      {roll?.listedAt ? `${roll.checked}/${roll.venueCount}` : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">
                      <span className={roll?.clean ? "font-semibold text-green-700" : "text-gray-300"}>
                        {roll?.clean ?? 0}
                      </span>
                    </td>
                    <Count n={roll?.totals.counts.critical} tone="font-semibold text-red-700" />
                    <Count n={roll?.totals.counts.warning} tone="text-amber-700" />
                    <Count n={roll?.totals.counts.info} tone="text-blue-700" />
                    <td className="px-2 py-1.5 text-xs text-gray-500">
                      {roll?.lastRun ? (
                        <span className="whitespace-nowrap">
                          {when(roll.lastRun.startedAt)}{" "}
                          <span className={`rounded px-1 ${RUN_TONE[roll.lastRun.status]}`}>
                            {roll.lastRun.tally.ok}/{roll.lastRun.planned}
                          </span>
                        </span>
                      ) : "never"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  };

  // ── the tenant view: its venues, and the run controls ──────────────

  const tenantView = () => {
    const roll = state?.tenant;
    const checkedCount = rows.filter((r) => r.listed && r.rec?.lastCompleteAt).length;
    const listedCount = rows.filter((r) => r.listed).length;
    return (
      <div className="p-4">
        {isMsp && (
          <button onClick={() => { if (!running) { setTenant(null); setFilter(""); } }}
                  disabled={running}
                  className="mb-3 inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 disabled:opacity-50">
            <ArrowLeft size={13} /> All tenants
          </button>
        )}
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="min-w-0 break-words font-semibold text-gray-900">
            {tenantName || roll?.name || "This tenant"}
          </h3>
          <span className="text-xs text-gray-500">
            {checkedCount}/{listedCount} checked · {roll?.fresh ?? 0} fresh ·{" "}
            <span className="font-semibold text-green-700">
              {roll?.clean ?? 0} clean
            </span> ·{" "}
            <span className="text-red-700">{roll?.totals.counts.critical ?? 0} critical</span> ·{" "}
            <span className="text-amber-700">{roll?.totals.counts.warning ?? 0} warning</span> ·{" "}
            <span className="text-blue-700">{roll?.totals.counts.info ?? 0} info</span>
          </span>
        </div>

        {/* Controls */}
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <label className="flex items-center gap-1 text-gray-600">
            Fresh within
            <input type="number" min={0} max={365} value={days}
                   onChange={(e) => setDays(Math.max(0, Number(e.target.value) || 0))}
                   className="w-14 rounded border border-gray-300 px-1.5 py-1 text-right" />
            days
          </label>
          <button className={btn} disabled={running || !staleIds.length}
                  onClick={() => setSelected(new Set(staleIds))}>
            Select not fresh ({staleIds.length})
          </button>
          <button className={btn} disabled={running || !venues?.length}
                  onClick={() => setSelected(new Set((venues || []).map((v) => v.id)))}>
            All
          </button>
          <button className={btn} disabled={running || !selected.size}
                  onClick={() => setSelected(new Set())}>
            None
          </button>
          <label className="flex items-center gap-1 text-gray-600">
            At once
            <select value={concurrency} disabled={running}
                    onChange={(e) => setConcurrency(Number(e.target.value))}
                    className="rounded border border-gray-300 px-1 py-1">
              {[1, 2, 3, 4].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span className="flex-1" />
          <DetailPicker value={detail} onChange={setDetail} />
          <button className={btn} disabled={exporting || running} onClick={() => downloadPdf(true)}>
            {exporting ? <Loader2 size={13} className="animate-spin" /> : <FileDown size={13} />}
            Tenant PDF
          </button>
          {running ? (
            <button onClick={stop} disabled={progress?.stopping}
                    className={`${btn} border-red-300 text-red-700 hover:bg-red-50`}>
              <Square size={13} /> {progress?.stopping ? "Stopping…" : "Stop"}
            </button>
          ) : (
            <button
              onClick={() => start(rows.filter((r) => r.listed && selected.has(r.id)).map((r) => r.id))}
              disabled={!selected.size || !!cannotWrite || !venues}
              className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5
                         text-xs font-medium text-white hover:bg-blue-700
                         disabled:cursor-not-allowed disabled:bg-gray-300">
              <Play size={13} /> Run {selected.size || ""} venue{selected.size === 1 ? "" : "s"}
            </button>
          )}
        </div>

        {progress && (
          <div className="mt-3 rounded-md border border-gray-200 bg-gray-50 p-2.5 text-xs text-gray-700">
            <div className="flex flex-wrap justify-between gap-2">
              <span>
                {running ? "Running" : progress.halted ? "Stopped" : "Finished"} ·{" "}
                {Math.min(progress.done, progress.total)}/{progress.total}
              </span>
              <span>
                <span className="text-green-700">{progress.ok} ok</span> ·{" "}
                <span className="text-amber-700">{progress.partial} partial</span> ·{" "}
                <span className="text-red-700">{progress.failed} failed</span>
              </span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded bg-gray-200">
              <div className="h-full bg-blue-500 transition-all"
                   style={{ width: `${progress.total ? (100 * progress.done) / progress.total : 0}%` }} />
            </div>
          </div>
        )}

        {/* Recent runs */}
        {!!state?.runs.length && (
          <details className="mt-3 text-xs">
            <summary className="cursor-pointer text-gray-600">Recent runs ({state.runs.length})</summary>
            <ul className="mt-1.5 space-y-1">
              {state.runs.map((run) => {
                const retry = run.venueIds.filter((id) => run.results[id] !== "ok"
                                                   && rows.some((r) => r.listed && r.id === id));
                return (
                  <li key={run.id} className="flex flex-wrap items-center gap-2 text-gray-600">
                    <span className={`rounded px-1.5 py-0.5 ${RUN_TONE[run.status]}`}>{run.status}</span>
                    <span className="tabular-nums">{run.tally.ok}/{run.planned} ok</span>
                    {!!run.tally.partial && <span className="text-amber-700">{run.tally.partial} partial</span>}
                    {!!run.tally.failed && <span className="text-red-700">{run.tally.failed} failed</span>}
                    {!!run.tally.notRun && <span>{run.tally.notRun} not reached</span>}
                    <span className="text-gray-400">{when(run.startedAt)} · {run.startedBy}</span>
                    {!!retry.length && run.status !== "running" && (
                      <button className="text-blue-700 hover:underline disabled:opacity-50"
                              disabled={running}
                              onClick={() => setSelected(new Set(retry))}>
                        select the {retry.length} not ok
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          </details>
        )}

        <input value={filter} onChange={(e) => setFilter(e.target.value)}
               placeholder="Filter venues"
               className="mt-3 w-full min-w-0 rounded-md border border-gray-300 px-2.5 py-1.5 text-sm" />

        {venuesLoading && <p className="mt-4 text-sm text-gray-500">Loading venues…</p>}
        {venues && (
          <div className="mt-2 min-w-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-[11px] uppercase tracking-wide text-gray-500">
                <tr className="border-b border-gray-200">
                  <th className="w-6 px-2 py-1.5" />
                  <th className="px-2 py-1.5">Venue</th>
                  <th className="px-2 py-1.5">Last checked</th>
                  <th className="px-2 py-1.5 text-right">Crit</th>
                  <th className="px-2 py-1.5 text-right">Warn</th>
                  <th className="px-2 py-1.5 text-right">Info</th>
                  <th className="px-2 py-1.5" />
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => {
                  const rec = row.rec;
                  const now = live[row.id];
                  const inFlight = progress?.active.includes(row.id);
                  // A result from this session's run beats the record read
                  // before it; the record is re-read when the run finishes.
                  const summary = now?.status === "ok" ? now.summary : rec?.summary;
                  const attempt = now?.status ?? rec?.lastAttemptStatus ?? null;
                  return (
                    <tr key={row.id} className="border-b border-gray-100">
                      <td className="px-2 py-1.5">
                        <input type="checkbox" disabled={!row.listed || running}
                               checked={selected.has(row.id)} onChange={() => toggle(row.id)} />
                      </td>
                      <td className="min-w-0 px-2 py-1.5">
                        <span className="break-words text-gray-900">{row.name}</span>
                        {!row.listed && (
                          <span className="ml-1 rounded bg-gray-100 px-1 text-[10px] text-gray-500">not listed</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-2 py-1.5 text-xs text-gray-500">
                        {inFlight ? (
                          <span className="inline-flex items-center gap-1 text-blue-700">
                            <Loader2 size={12} className="animate-spin" /> checking
                          </span>
                        ) : (
                          <>
                            {now?.status === "ok" ? "just now"
                              : rec?.lastCompleteAt ? ago(rec.ageDays) : "never"}
                            {attempt && attempt !== "ok" && (
                              <span className={`ml-1 rounded px-1 ${STATUS_TONE[attempt]}`}
                                    title={[rec?.lastAttemptError,
                                            ...(rec?.lastAttemptReadErrors || [])]
                                      .filter(Boolean).join(", ") || undefined}>
                                latest {attempt}
                              </span>
                            )}
                            {!now && rec?.lastCompleteAt && !rec.fresh && (
                              <span className="ml-1 rounded bg-gray-100 px-1 text-gray-500">stale</span>
                            )}
                          </>
                        )}
                      </td>
                      <Count n={summary?.counts.critical} tone="font-semibold text-red-700" />
                      <Count n={summary?.counts.warning} tone="text-amber-700" />
                      <Count n={summary?.counts.info} tone="text-blue-700" />
                      <td className="px-2 py-1.5 text-right">
                        {row.listed && (
                          <a href={reportLink(isMsp, tenant?.id ?? null, tenantName, row.id)}
                             target="_blank" rel="noopener noreferrer" title="Open the live report"
                             className="inline-flex text-gray-400 hover:text-blue-700">
                            <ExternalLink size={14} />
                          </a>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto
                    bg-gray-900/40 p-4 sm:p-8">
      <div className="w-full max-w-4xl min-w-0 rounded-lg border border-gray-200 bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-semibold text-gray-900">
              <Layers size={17} className="shrink-0 text-gray-400" />
              Batch runs
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
              Check many venues in one go and keep each one's punch-list counts.
              This tab runs the batch — closing it stops the batch. The detail
              stays in each venue's live report.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <button onClick={loadState} aria-label="Refresh" title="Re-read the record"
                    className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
              <RefreshCw size={16} />
            </button>
            <button onClick={close} aria-label="Close"
                    className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
              <X size={18} />
            </button>
          </div>
        </div>

        {error && (
          <p className="m-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
        )}
        {cannotWrite && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <b>Runs cannot be recorded.</b>{" "}
            {state?.broken
              ? `${state.path} could not be read and is being left alone so it can be recovered.`
              : `${state?.path ?? "PISR_BATCH_FILE"} is not writable by the container.`}
          </p>
        )}

        {tenant ? tenantView() : mspView()}
      </div>
    </div>
  );
}

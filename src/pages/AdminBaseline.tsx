import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight, Download, Loader2, Save, SlidersHorizontal, Upload, X,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * The recommendation editor: what the {company} column recommends, per setting.
 *
 * THE FIELD LIST IS STATIC, from the committed catalogue (api/baselines/
 * field_catalogue.json, built from the OpenAPI spec). So the editor shows every
 * settable field immediately — no venue to load first. Loading a venue is
 * OPTIONAL and only adds a "now: <value>" column so you can see current state
 * beside the recommendation.
 *
 * THREE STATES PER FIELD: a recommended VALUE, explicitly NOT APPLICABLE (shown
 * "—", never a mismatch), or NOT REVIEWED (no column). N.A. and not-reviewed
 * look the same to a reader but are different to the admin. See api/baselines.py.
 *
 * TYPE-AWARE INPUTS from the catalogue: boolean -> a true/false choice, enum ->
 * a dropdown of the spec's values, integer/number -> a number field, else text.
 *
 * RUCKUS IS READ-ONLY REFERENCE, shown beside the editable column, never sent
 * back. The save is a whole-document replace, so the working copy starts from
 * the entire stored baseline and overlays only the edits — nothing is dropped.
 */

type Mode = "value" | "na" | "none";

interface CatField {
  type: string;
  label: string;
  curated: boolean;
  enum?: string[];
}
interface CatEndpoint { label: string; fields: Record<string, CatField> }

interface Loaded {
  values: Record<string, unknown>;
  notApplicable: string[];
  status: string;
  source: string;
  show: boolean;
  writable: boolean;
  path: string | null;
}

/** Coerce a typed-in recommendation to bool/number/string, like R1's values. */
function parseValue(text: string, type?: string): unknown {
  const t = text.trim();
  if (type === "boolean") return t === "true";
  if ((type === "integer" || type === "number") && t !== "" && !Number.isNaN(Number(t)))
    return Number(t);
  if (t === "true") return true;
  if (t === "false") return false;
  if (t !== "" && !Number.isNaN(Number(t)) && /^-?\d+(\.\d+)?$/.test(t)) return Number(t);
  return text;
}
function valueToText(v: unknown): string {
  if (typeof v === "boolean") return v ? "true" : "false";
  if (v === null || v === undefined) return "";
  return String(v);
}

export default function AdminBaseline({ onClose }: { onClose: () => void }) {
  const { activeControllerId, activeControllerSubtype } = useAuth();
  const isMsp = (activeControllerSubtype || "").toUpperCase() === "MSP";

  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [orgName, setOrgName] = useState("Org");
  const [ruckus, setRuckus] = useState<Record<string, unknown>>({});
  const [catalogue, setCatalogue] = useState<Record<string, CatEndpoint>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  // Editable meta.
  const [status, setStatus] = useState("unverified");
  const [source, setSource] = useState("");
  const [show, setShow] = useState(true);

  // View controls.
  const [search, setSearch] = useState("");
  const [showAll, setShowAll] = useState(false);   // curated vs every field
  const [setOnly, setSetOnly] = useState(false);   // review: only fields I've set
  const [open, setOpen] = useState<Set<string>>(new Set());

  // Optional venue overlay: baselineKey -> current value text.
  const [ecs, setEcs] = useState<{ id: string; name: string }[] | null>(null);
  const [ec, setEc] = useState<string>("");
  const [venues, setVenues] = useState<{ id: string; name: string }[] | null>(null);
  const [venue, setVenue] = useState<string>("");
  const [current, setCurrent] = useState<Record<string, string> | null>(null);
  const [loadingVenue, setLoadingVenue] = useState(false);

  // baselineKey -> the admin's choice. Only CHANGED fields; the stored baseline
  // is the base and this overlays it.
  const [edits, setEdits] = useState<Record<string, { mode: Mode; value: string }>>({});

  // ── load baseline + catalogue ─────────────────────────────────────
  useEffect(() => {
    fetch(`${API_BASE_URL}/admin/baseline`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        setLoaded(d.org);
        setOrgName(d.orgName || "Org");
        setRuckus(d.ruckus || {});
        setCatalogue(d.catalogue?.levels?.venue?.endpoints || {});
        setStatus(d.org.status || "unverified");
        setSource(d.org.source || "");
        setShow(d.org.show !== false);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  // ── optional EC/venue pickers (current-value overlay only) ────────
  useEffect(() => {
    if (!isMsp || activeControllerId === null) return;
    fetch(`${API_BASE_URL}/r1/${activeControllerId}/msp/mspEcs`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => setEcs((Array.isArray(body) ? body : body.data || [])
        .map((e: any) => ({ id: e.id, name: e.name || e.id }))))
      .catch(() => setEcs([]));
  }, [isMsp, activeControllerId]);

  useEffect(() => {
    if (activeControllerId === null) return;
    if (isMsp && !ec) { setVenues(null); return; }
    const q = isMsp ? `?tenant_id=${encodeURIComponent(ec)}` : "";
    setVenues(null); setVenue("");
    fetch(`${API_BASE_URL}/pisr/${activeControllerId}/venues${q}`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => setVenues((body.venues || []).map((v: any) => ({ id: v.id, name: v.name || v.id }))))
      .catch(() => setVenues([]));
  }, [activeControllerId, isMsp, ec]);

  const loadCurrent = useCallback(() => {
    if (activeControllerId === null || !venue) return;
    setLoadingVenue(true); setError(null);
    const q = new URLSearchParams({ venue_id: venue });
    if (isMsp && ec) q.set("tenant_id", ec);
    fetch(`${API_BASE_URL}/pisr/${activeControllerId}/report?${q}`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((report) => {
        const map: Record<string, string> = {};
        for (const c of report.config?.categories || [])
          for (const row of c.rows || [])
            if (row.baselineKey) map[row.baselineKey] = row.valueText ?? valueToText(row.value);
        setCurrent(map);
      })
      .catch((e: Error) => setError(`Could not load current values: ${e.message}`))
      .finally(() => setLoadingVenue(false));
  }, [activeControllerId, isMsp, ec, venue]);

  const currentOf = useCallback((key: string): { mode: Mode; value: string } => {
    if (edits[key]) return edits[key];
    if (loaded && key in loaded.values) return { mode: "value", value: valueToText(loaded.values[key]) };
    if (loaded && loaded.notApplicable.includes(key)) return { mode: "na", value: "" };
    return { mode: "none", value: "" };
  }, [edits, loaded]);

  const setField = (key: string, mode: Mode, value?: string) =>
    setEdits((e) => ({ ...e, [key]: { mode, value: value ?? e[key]?.value ?? "" } }));

  // The endpoints/fields to show, after curated/search/review filters.
  const groups = useMemo(() => {
    const q = search.trim().toLowerCase();
    const out: { endpoint: string; label: string;
                 rows: { key: string; field: CatField }[] }[] = [];
    for (const [endpoint, ep] of Object.entries(catalogue)) {
      const rows: { key: string; field: CatField }[] = [];
      for (const [path, field] of Object.entries(ep.fields)) {
        if (!showAll && !field.curated) continue;
        const key = `${endpoint}.${path}`;
        if (setOnly && currentOf(key).mode === "none") continue;
        if (q && !field.label.toLowerCase().includes(q) && !key.toLowerCase().includes(q)) continue;
        rows.push({ key, field });
      }
      if (rows.length) out.push({ endpoint, label: ep.label, rows });
    }
    return out.sort((a, b) => a.label.localeCompare(b.label));
  }, [catalogue, showAll, setOnly, search, currentOf]);

  const counts = useMemo(() => {
    if (!loaded) return { values: 0, na: 0 };
    const val = new Set(Object.keys(loaded.values));
    const na = new Set(loaded.notApplicable);
    for (const [k, e] of Object.entries(edits)) {
      val.delete(k); na.delete(k);
      if (e.mode === "value") val.add(k);
      else if (e.mode === "na") na.add(k);
    }
    return { values: val.size, na: na.size };
  }, [loaded, edits]);

  const dirty = Object.keys(edits).length > 0 || (loaded &&
    (status !== (loaded.status || "unverified") || source !== (loaded.source || "")
     || show !== (loaded.show !== false)));
  const readOnly = Boolean(loaded && !loaded.writable);

  // ── download / import ─────────────────────────────────────────────
  const fileRef = useRef<HTMLInputElement>(null);

  const downloadTemplate = () => {
    // The curated key settings across the whole catalogue — short and editable.
    const fields: Record<string, unknown> = {};
    for (const [endpoint, ep] of Object.entries(catalogue))
      for (const [path, field] of Object.entries(ep.fields)) {
        if (!field.curated) continue;
        const key = `${endpoint}.${path}`;
        const cur = currentOf(key);
        fields[key] = {
          label: field.label, type: field.type,
          ...(field.enum ? { options: field.enum } : {}),
          ruckus: key in ruckus ? ruckus[key] : null,
          value: cur.mode === "value" ? parseValue(cur.value, field.type) : null,
          na: cur.mode === "na",
        };
      }
    const template = {
      _help: "Set `value` for a field to recommend it; leave null to skip " +
             "(blank is unchanged on import). Set `na` true to mark " +
             "not-applicable. `type`, `options` and `ruckus` are reference only.",
      orgName, status, source, fields,
    };
    const blob = new Blob([JSON.stringify(template, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(orgName || "org").toLowerCase().replace(/\s+/g, "-")}-baseline-template.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const importFile = (file: File) => {
    setError(null);
    file.text().then((text) => {
      let doc: any;
      try { doc = JSON.parse(text); }
      catch { setError("That file is not valid JSON."); return; }
      const fields = doc?.fields;
      if (!fields || typeof fields !== "object") {
        setError("No `fields` object in that file — download a template first.");
        return;
      }
      const next = { ...edits };
      let applied = 0;
      for (const [key, raw] of Object.entries<any>(fields)) {
        const entry = (raw && typeof raw === "object" && !Array.isArray(raw)) ? raw : { value: raw };
        if (entry.na === true) { next[key] = { mode: "na", value: "" }; applied++; }
        else if (entry.value !== null && entry.value !== undefined && entry.value !== "") {
          next[key] = { mode: "value", value: valueToText(entry.value) }; applied++;
        }
      }
      setEdits(next);
      if (typeof doc.status === "string") setStatus(doc.status);
      if (typeof doc.source === "string") setSource(doc.source);
      setError(applied ? null : "Nothing to import — every field's `value` was blank.");
    });
  };

  const save = async () => {
    if (!loaded) return;
    setBusy(true); setError(null); setSaved(false);
    const values: Record<string, unknown> = { ...loaded.values };
    const na = new Set(loaded.notApplicable);
    for (const [k, e] of Object.entries(edits)) {
      delete values[k]; na.delete(k);
      const type = catalogueTypeOf(catalogue, k);
      if (e.mode === "value") values[k] = parseValue(e.value, type);
      else if (e.mode === "na") na.add(k);
    }
    try {
      const res = await fetch(`${API_BASE_URL}/admin/baseline`, {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values, notApplicable: [...na], status, source, show }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) { setError(body?.detail || `Save failed (HTTP ${res.status}).`); return; }
      setLoaded(body); setEdits({}); setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const seg = (active: boolean) =>
    `px-2 py-0.5 text-[11px] rounded ${active ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-100"}`;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto
                    bg-gray-900/40 p-4 sm:p-8">
      <div className="w-full max-w-4xl min-w-0 rounded-lg border border-gray-200 bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-semibold text-gray-900">
              <SlidersHorizontal size={17} className="text-gray-400 shrink-0" />
              {orgName} recommendations
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
              What the {orgName} column recommends for each venue setting. RUCKUS
              values are shown for reference and are not edited here.
            </p>
          </div>
          <button onClick={onClose} aria-label="Close"
                  className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
            <X size={18} />
          </button>
        </div>

        {error && <p className="m-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
        {readOnly && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <b>Read-only.</b> {loaded?.path} is not writable by the container, so
            recommendations cannot be saved. Mount a writable volume at its directory.
          </p>
        )}
        {!loaded && !error && (
          <p className="flex items-center gap-2 p-8 text-sm text-gray-500">
            <Loader2 size={15} className="animate-spin" /> Loading…
          </p>
        )}

        {loaded && (
          <div className="max-h-[70vh] overflow-y-auto p-4 space-y-4">
            {/* Master switch. */}
            <label className="flex items-start gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 cursor-pointer">
              <input type="checkbox" className="mt-1 shrink-0" checked={show}
                     disabled={readOnly} onChange={(e) => setShow(e.target.checked)} />
              <span className="min-w-0 text-sm">
                <span className="font-medium text-gray-900">Show recommendations in reports</span>
                <span className="block text-xs text-gray-500">
                  When off, neither the {orgName} nor the RUCKUS column appears on
                  any report — the values below are kept, just not shown.
                </span>
              </span>
            </label>

            {/* Meta. */}
            <div className={`flex flex-wrap items-end gap-3 rounded-md border border-gray-200 p-3 ${show ? "" : "opacity-50"}`}>
              <label className="flex items-center gap-2 pb-1 text-xs text-gray-700 cursor-pointer">
                <input type="checkbox" disabled={readOnly} checked={status === "verified"}
                       onChange={(e) => setStatus(e.target.checked ? "verified" : "unverified")} />
                <span><span className="font-medium">Verified</span> — our confirmed standard (else the column reads “draft”)</span>
              </label>
              <label className="min-w-0 flex-1 text-xs text-gray-700">
                <span className="block font-medium">Source / note</span>
                <input value={source} disabled={readOnly}
                       onChange={(e) => setSource(e.target.value)}
                       placeholder="where these came from, e.g. Acme WiFi standard v3"
                       className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100" />
              </label>
              <p className="pb-1 text-xs text-gray-500">{counts.values} value(s) · {counts.na} n/a</p>
              <div className="ml-auto flex items-center gap-2 pb-0.5">
                <button onClick={downloadTemplate}
                        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50">
                  <Download size={13} /> Starter
                </button>
                <button onClick={() => fileRef.current?.click()} disabled={readOnly}
                        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                  <Upload size={13} /> Import
                </button>
                <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
                       onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }} />
              </div>
            </div>

            {/* Filters. */}
            <div className="flex flex-wrap items-center gap-3">
              <input value={search} onChange={(e) => setSearch(e.target.value)}
                     placeholder="Search settings…"
                     className="min-w-0 flex-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-sm
                                focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500" />
              <label className="flex items-center gap-1.5 text-xs text-gray-700 cursor-pointer">
                <input type="checkbox" checked={setOnly} onChange={(e) => setSetOnly(e.target.checked)} />
                Only set ({counts.values + counts.na})
              </label>
              <label className="flex items-center gap-1.5 text-xs text-gray-700 cursor-pointer">
                <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
                All fields
              </label>
            </div>

            {/* Optional venue overlay. */}
            <div className="flex flex-wrap items-end gap-2 text-xs text-gray-600">
              <span className="pb-1">Current values (optional):</span>
              {isMsp && (
                <select value={ec} onChange={(e) => setEc(e.target.value)}
                        className="rounded-md border border-gray-300 px-2 py-1">
                  <option value="">customer…</option>
                  {(ecs || []).map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
                </select>
              )}
              <select value={venue} disabled={!venues} onChange={(e) => setVenue(e.target.value)}
                      className="rounded-md border border-gray-300 px-2 py-1 disabled:bg-gray-100">
                <option value="">{venues ? "venue…" : "…"}</option>
                {(venues || []).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
              </select>
              <button onClick={loadCurrent} disabled={!venue || loadingVenue}
                      className="rounded-md border border-gray-300 px-2 py-1 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50">
                {loadingVenue ? "Loading…" : current ? "Reload" : "Show current"}
              </button>
            </div>

            {/* The grid, from the static catalogue. */}
            {groups.map((g) => {
              const isOpen = setOnly || !!search || open.has(g.endpoint);
              return (
                <div key={g.endpoint} className="min-w-0 rounded-md border border-gray-200">
                  <button onClick={() => setOpen((o) => {
                            const n = new Set(o); n.has(g.endpoint) ? n.delete(g.endpoint) : n.add(g.endpoint); return n;
                          })}
                          disabled={setOnly || !!search}
                          className="flex w-full items-center gap-2 px-3 py-2 text-left disabled:cursor-default">
                    <ChevronRight size={14} className={`shrink-0 text-gray-400 transition-transform ${isOpen ? "rotate-90" : ""}`} />
                    <span className="text-sm font-medium text-gray-900">{g.label}</span>
                    <span className="text-xs text-gray-400">{g.rows.length}</span>
                  </button>
                  {isOpen && (
                    <div className="border-t border-gray-100 p-2 space-y-1">
                      {g.rows.map(({ key, field }) => {
                        const cur = currentOf(key);
                        const ref = key in ruckus ? ruckus[key] : undefined;
                        const nowVal = current?.[key];
                        return (
                          <div key={key} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded px-2 py-1.5 hover:bg-gray-50">
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-sm text-gray-800">{field.label}</div>
                              <div className="truncate text-[11px] text-gray-400">
                                {nowVal !== undefined && <>now: {nowVal || "—"} · </>}
                                {ref !== undefined ? <>RUCKUS: {valueToText(ref)}</> : <span className="font-mono">{key}</span>}
                              </div>
                            </div>
                            <div className="flex shrink-0 items-center gap-1 rounded border border-gray-200 p-0.5">
                              <button disabled={readOnly} className={seg(cur.mode === "value")}
                                      onClick={() => setField(key, "value", cur.value || (nowVal ?? ""))}>Value</button>
                              <button disabled={readOnly} className={seg(cur.mode === "na")}
                                      onClick={() => setField(key, "na", "")}>N/A</button>
                              <button disabled={readOnly} className={seg(cur.mode === "none")}
                                      onClick={() => setField(key, "none", "")} title="Not reviewed">—</button>
                            </div>
                            {cur.mode === "value" && (
                              <ValueInput field={field} value={cur.value} disabled={readOnly}
                                          onChange={(v) => setField(key, "value", v)} placeholder={nowVal} />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
            {!groups.length && (
              <p className="text-sm text-gray-500">
                {Object.keys(catalogue).length
                  ? "No settings match."
                  : "No field catalogue built — run scripts/build_field_catalogue.py."}
              </p>
            )}
          </div>
        )}

        {loaded && (
          <div className="flex items-center justify-between gap-3 border-t border-gray-200 p-4">
            <p className="text-xs text-gray-500">{saved ? "Saved." : dirty ? "Unsaved changes." : "No changes."}</p>
            <div className="flex items-center gap-2">
              <button onClick={onClose}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50">Close</button>
              <button onClick={save} disabled={busy || readOnly || !dirty}
                      className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300">
                <Save size={14} /> {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** The value input for a field in Value mode — typed by the catalogue. */
function ValueInput({ field, value, disabled, onChange, placeholder }: {
  field: CatField; value: string; disabled?: boolean;
  onChange: (v: string) => void; placeholder?: string;
}) {
  const cls = "w-32 shrink-0 rounded border border-gray-300 px-2 py-1 text-xs disabled:bg-gray-100";
  if (field.type === "boolean")
    return (
      <select value={value || "true"} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={cls}>
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  if (field.enum?.length)
    return (
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)} className={cls}>
        <option value="">choose…</option>
        {field.enum.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  return (
    <input value={value} disabled={disabled}
           type={field.type === "integer" || field.type === "number" ? "number" : "text"}
           onChange={(e) => onChange(e.target.value)} placeholder={placeholder || field.type}
           className={cls} />
  );
}

/** The catalogue type for a baseline key, for coercion on save. */
function catalogueTypeOf(catalogue: Record<string, CatEndpoint>, key: string): string | undefined {
  const dot = key.indexOf(".");
  if (dot < 0) return undefined;
  const ep = catalogue[key.slice(0, dot)];
  return ep?.fields?.[key.slice(dot + 1)]?.type;
}

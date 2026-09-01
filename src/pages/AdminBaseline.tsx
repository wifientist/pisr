import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronRight, Download, Loader2, Save, SlidersHorizontal, Upload, X,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * The recommendation editor: what the {company} column recommends, per setting.
 *
 * THREE STATES PER FIELD, and the third is the reason this exists: a
 * recommended VALUE, explicitly NOT APPLICABLE (shown as "—", never a
 * mismatch), or NOT REVIEWED (no column). "Not applicable" and "not reviewed"
 * look the same to a reader but are different to the admin maintaining the
 * baseline — one is "we looked and there is nothing to recommend", the other is
 * "we have not looked". See api/baselines.py.
 *
 * THE FIELD LIST COMES FROM A REAL VENUE, not a hardcoded catalogue. R1's field
 * set is whatever it returns for a venue, so the editor loads one venue's config
 * and lets the admin annotate the fields it exposes. Picking a different venue
 * surfaces different fields; the editor says so rather than pretending to be
 * exhaustive.
 *
 * RUCKUS IS READ-ONLY REFERENCE. Its values are shown beside the editable
 * column and never sent back — vendor guidance lives in the repo.
 *
 * OFF-CATALOGUE ENTRIES ARE PRESERVED. The save is a whole-document replace, so
 * the working copy starts from the ENTIRE stored baseline and overlays only the
 * fields this venue exposed — otherwise saving from one venue would delete
 * recommendations set from another.
 */

type Mode = "value" | "na" | "none";

interface Row {
  baselineKey: string;
  label: string;
  valueText: string;
  value: unknown;
}
interface Category { key: string; slug: string; label?: string; rows: Row[] }

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
function parseValue(text: string): unknown {
  const t = text.trim();
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
  const [statuses, setStatuses] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  // Editable meta.
  const [status, setStatus] = useState("unverified");
  const [source, setSource] = useState("");
  const [show, setShow] = useState(true);

  // The field catalogue for one venue, and edits overlaid on the stored file.
  const [ecs, setEcs] = useState<{ id: string; name: string }[] | null>(null);
  const [ec, setEc] = useState<string>("");
  const [venues, setVenues] = useState<{ id: string; name: string }[] | null>(null);
  const [venue, setVenue] = useState<string>("");
  const [cats, setCats] = useState<Category[] | null>(null);
  const [loadingCat, setLoadingCat] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  // baselineKey -> the admin's choice for it. Only CHANGED fields live here;
  // the stored baseline is the base and this is the overlay.
  const [edits, setEdits] = useState<Record<string, { mode: Mode; value: string }>>({});

  // ── load the baseline itself ──────────────────────────────────────
  useEffect(() => {
    fetch(`${API_BASE_URL}/admin/baseline`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        setLoaded(d.org);
        setOrgName(d.orgName || "Org");
        setRuckus(d.ruckus || {});
        setStatuses(d.statuses || ["verified", "placeholder", "unverified"]);
        setStatus(d.org.status || "unverified");
        setSource(d.org.source || "");
        setShow(d.org.show !== false);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  // ── EC list (MSP only) ────────────────────────────────────────────
  useEffect(() => {
    if (!isMsp || activeControllerId === null) return;
    fetch(`${API_BASE_URL}/r1/${activeControllerId}/msp/mspEcs`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((body) => {
        const rows = Array.isArray(body) ? body : body.data || [];
        setEcs(rows.map((e: any) => ({ id: e.id, name: e.name || e.id })));
      })
      .catch(() => setEcs([]));
  }, [isMsp, activeControllerId]);

  // ── venues for the chosen EC (or the sole tenant) ─────────────────
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

  const loadFields = useCallback(() => {
    if (activeControllerId === null || !venue) return;
    setLoadingCat(true); setError(null); setCats(null);
    const q = new URLSearchParams({ venue_id: venue });
    if (isMsp && ec) q.set("tenant_id", ec);
    fetch(`${API_BASE_URL}/pisr/${activeControllerId}/report?${q}`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((report) => {
        const categories: Category[] = (report.config?.categories || [])
          .map((c: any) => ({
            key: c.key, slug: c.slug,
            label: c.label || c.slug,
            rows: (c.rows || []).filter((r: Row) => r.baselineKey),
          }))
          .filter((c: Category) => c.rows.length);
        setCats(categories);
      })
      .catch((e: Error) => setError(`Could not load that venue's config: ${e.message}`))
      .finally(() => setLoadingCat(false));
  }, [activeControllerId, isMsp, ec, venue]);

  // The current mode/value for a field: an edit if one exists, else the stored
  // baseline, else "not reviewed".
  const currentOf = useCallback((key: string): { mode: Mode; value: string } => {
    if (edits[key]) return edits[key];
    if (loaded && key in loaded.values) return { mode: "value", value: valueToText(loaded.values[key]) };
    if (loaded && loaded.notApplicable.includes(key)) return { mode: "na", value: "" };
    return { mode: "none", value: "" };
  }, [edits, loaded]);

  const setField = (key: string, mode: Mode, value?: string) =>
    setEdits((e) => ({ ...e, [key]: { mode, value: value ?? e[key]?.value ?? "" } }));

  const counts = useMemo(() => {
    // Effective final state = stored baseline overlaid with edits.
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

  const save = async () => {
    if (!loaded) return;
    setBusy(true); setError(null); setSaved(false);
    // Start from the WHOLE stored baseline so off-catalogue entries survive.
    const values: Record<string, unknown> = { ...loaded.values };
    const na = new Set(loaded.notApplicable);
    for (const [k, e] of Object.entries(edits)) {
      delete values[k]; na.delete(k);
      if (e.mode === "value") values[k] = parseValue(e.value);
      else if (e.mode === "na") na.add(k);
      // "none" leaves it removed from both.
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

  // ── download a template / import a filled one ─────────────────────
  //
  // The template is every field the loaded venue exposes — R1's field set is
  // dynamic, so this is the only place "all possible values" comes from. Each
  // entry carries the venue's current value and the RUCKUS reference for
  // context, and an editable `value`/`na`. On import, ONLY entries with a
  // filled `value` (or `na: true`) take effect; a blank one is skipped and
  // leaves whatever is already set untouched — so a half-filled template adds
  // to the baseline rather than wiping it.
  const fileRef = useRef<HTMLInputElement>(null);

  const downloadTemplate = () => {
    if (!cats) return;
    const fields: Record<string, unknown> = {};
    for (const cat of cats) {
      for (const row of cat.rows) {
        const cur = currentOf(row.baselineKey);
        fields[row.baselineKey] = {
          label: row.label,
          current: row.value,                               // reference only
          ruckus: row.baselineKey in ruckus ? ruckus[row.baselineKey] : null,
          value: cur.mode === "value" ? parseValue(cur.value) : null,
          na: cur.mode === "na",
        };
      }
    }
    const template = {
      _help: "Set `value` for a field to recommend it. Leave it null to skip " +
             "(a blank field is left unchanged on import). Set `na` true to " +
             "mark a field not-applicable. `current` and `ruckus` are reference " +
             "only and are ignored on import.",
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
      const next: Record<string, { mode: Mode; value: string }> = { ...edits };
      let applied = 0;
      for (const [key, raw] of Object.entries<any>(fields)) {
        // Accept either the template's object shape or a bare value, so a
        // hand-authored `{"fields": {"key": true}}` works too.
        const entry = (raw && typeof raw === "object" && !Array.isArray(raw)) ? raw : { value: raw };
        const v = entry.value;
        if (entry.na === true) { next[key] = { mode: "na", value: "" }; applied++; }
        else if (v !== null && v !== undefined && v !== "") {
          next[key] = { mode: "value", value: valueToText(v) }; applied++;
        }
        // else: blank → skipped, leaving whatever is already set.
      }
      setEdits(next);
      if (typeof doc.status === "string") setStatus(doc.status);
      if (typeof doc.source === "string") setSource(doc.source);
      setError(applied ? null : "Nothing to import — every field's `value` was blank.");
    });
  };

  const readOnly = Boolean(loaded && !loaded.writable);
  const dirty = Object.keys(edits).length > 0 || (loaded &&
    (status !== (loaded.status || "unverified") || source !== (loaded.source || "")
     || show !== (loaded.show !== false)));

  const seg = (active: boolean) =>
    `px-2 py-0.5 text-[11px] rounded ${active ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-gray-100"}`;

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto
                    bg-gray-900/40 p-4 sm:p-8">
      {/* min-w-0 throughout: baseline keys are long unbreakable dotted tokens. */}
      <div className="w-full max-w-4xl min-w-0 rounded-lg border border-gray-200 bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-semibold text-gray-900">
              <SlidersHorizontal size={17} className="text-gray-400 shrink-0" />
              {orgName} recommendations
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
              What the {orgName} column recommends for each setting. RUCKUS values
              are shown for reference and are not edited here.
            </p>
          </div>
          <button onClick={onClose} aria-label="Close"
                  className="shrink-0 rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-700">
            <X size={18} />
          </button>
        </div>

        {error && (
          <p className="m-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
        )}
        {readOnly && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <b>Read-only.</b> {loaded?.path} is not writable by the container, so
            recommendations cannot be saved. Mount a writable volume at its
            directory — without one an edit is lost at the next deploy.
          </p>
        )}

        {!loaded && !error && (
          <p className="flex items-center gap-2 p-8 text-sm text-gray-500">
            <Loader2 size={15} className="animate-spin" /> Loading the baseline…
          </p>
        )}

        {loaded && (
          <div className="max-h-[70vh] overflow-y-auto p-4 space-y-4">
            {/* The master switch, first: whether recommendations appear in
                reports at all. Off keeps the values but shows neither column. */}
            <label className="flex items-start gap-2 rounded-md border border-gray-200 bg-gray-50 px-3 py-2 cursor-pointer">
              <input type="checkbox" className="mt-1 shrink-0" checked={show}
                     disabled={readOnly} onChange={(e) => setShow(e.target.checked)} />
              <span className="min-w-0 text-sm">
                <span className="font-medium text-gray-900">Show recommendations in reports</span>
                <span className="block text-xs text-gray-500">
                  When off, neither the {orgName} nor the RUCKUS column appears
                  on any report — the values below are kept, just not shown.
                </span>
              </span>
            </label>

            {/* Meta: how trustworthy, and where the values came from. */}
            <div className={`flex flex-wrap items-end gap-3 rounded-md border border-gray-200 p-3 ${show ? "" : "opacity-50"}`}>
              <label className="text-xs text-gray-700">
                <span className="block font-medium">Trust</span>
                <select value={status} disabled={readOnly}
                        onChange={(e) => setStatus(e.target.value)}
                        className="mt-1 rounded-md border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100">
                  {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <label className="min-w-0 flex-1 text-xs text-gray-700">
                <span className="block font-medium">Source</span>
                <input value={source} disabled={readOnly}
                       onChange={(e) => setSource(e.target.value)}
                       placeholder="e.g. Acme WiFi standard v3"
                       className="mt-1 w-full rounded-md border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100" />
              </label>
              <p className="pb-1 text-xs text-gray-500">
                {counts.values} value(s) · {counts.na} not-applicable
              </p>
              <div className="ml-auto flex items-center gap-2 pb-0.5">
                <button onClick={downloadTemplate} disabled={!cats}
                        title={cats ? "Download every field this venue exposes as a JSON template"
                                    : "Load a venue's fields first"}
                        className="inline-flex items-center gap-1 rounded-md border border-gray-300
                                   px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50
                                   disabled:cursor-not-allowed disabled:opacity-50">
                  <Download size={13} /> Template
                </button>
                <button onClick={() => fileRef.current?.click()} disabled={readOnly}
                        title="Import a filled-in template — only its non-blank values take effect"
                        className="inline-flex items-center gap-1 rounded-md border border-gray-300
                                   px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50
                                   disabled:cursor-not-allowed disabled:opacity-50">
                  <Upload size={13} /> Import
                </button>
                <input ref={fileRef} type="file" accept="application/json,.json" className="hidden"
                       onChange={(e) => {
                         const f = e.target.files?.[0];
                         if (f) importFile(f);
                         e.target.value = "";   // allow re-importing the same file
                       }} />
              </div>
            </div>

            {/* Venue picker — sources the field list. */}
            <div className="flex flex-wrap items-end gap-2 rounded-md border border-gray-200 p-3">
              {isMsp && (
                <label className="text-xs text-gray-700">
                  <span className="block font-medium">Customer</span>
                  <select value={ec} onChange={(e) => setEc(e.target.value)}
                          className="mt-1 rounded-md border border-gray-300 px-2 py-1 text-sm">
                    <option value="">Choose…</option>
                    {(ecs || []).map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
                  </select>
                </label>
              )}
              <label className="text-xs text-gray-700">
                <span className="block font-medium">Venue</span>
                <select value={venue} disabled={!venues} onChange={(e) => setVenue(e.target.value)}
                        className="mt-1 rounded-md border border-gray-300 px-2 py-1 text-sm disabled:bg-gray-100">
                  <option value="">{venues ? "Choose…" : "…"}</option>
                  {(venues || []).map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                </select>
              </label>
              <button onClick={loadFields} disabled={!venue || loadingCat}
                      className="mb-0.5 rounded-md bg-gray-800 px-3 py-1.5 text-sm font-medium text-white
                                 hover:bg-gray-900 disabled:cursor-not-allowed disabled:bg-gray-300">
                {loadingCat ? "Loading…" : "Load this venue's fields"}
              </button>
              <p className="w-full text-[11px] text-gray-500">
                The list below is only what this venue exposes — pick another
                venue to surface fields it doesn't have.
              </p>
            </div>

            {/* The editable grid. */}
            {cats && cats.map((cat) => {
              const isOpen = open.has(cat.slug);
              return (
                <div key={cat.slug} className="min-w-0 rounded-md border border-gray-200">
                  <button onClick={() => setOpen((o) => {
                            const n = new Set(o); n.has(cat.slug) ? n.delete(cat.slug) : n.add(cat.slug); return n;
                          })}
                          className="flex w-full items-center gap-2 px-3 py-2 text-left">
                    <ChevronRight size={14} className={`shrink-0 text-gray-400 transition-transform ${isOpen ? "rotate-90" : ""}`} />
                    <span className="text-sm font-medium text-gray-900">{cat.label}</span>
                    <span className="text-xs text-gray-400">{cat.rows.length}</span>
                  </button>
                  {isOpen && (
                    <div className="border-t border-gray-100 p-2 space-y-1">
                      {cat.rows.map((row) => {
                        const cur = currentOf(row.baselineKey);
                        const ref = ruckus[row.baselineKey];
                        return (
                          <div key={row.baselineKey}
                               className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 rounded px-2 py-1.5 hover:bg-gray-50">
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-sm text-gray-800">{row.label}</div>
                              <div className="truncate text-[11px] text-gray-400">
                                now: {row.valueText || "—"}
                                {ref !== undefined && <> · RUCKUS: {valueToText(ref)}</>}
                              </div>
                            </div>
                            <div className="flex shrink-0 items-center gap-1 rounded border border-gray-200 p-0.5">
                              <button disabled={readOnly} className={seg(cur.mode === "value")}
                                      onClick={() => setField(row.baselineKey, "value",
                                        cur.value || row.valueText)}>Value</button>
                              <button disabled={readOnly} className={seg(cur.mode === "na")}
                                      onClick={() => setField(row.baselineKey, "na", "")}>N/A</button>
                              <button disabled={readOnly} className={seg(cur.mode === "none")}
                                      onClick={() => setField(row.baselineKey, "none", "")}
                                      title="Not reviewed — no recommendation shown">—</button>
                            </div>
                            {cur.mode === "value" && (
                              <input value={cur.value} disabled={readOnly}
                                     onChange={(e) => setField(row.baselineKey, "value", e.target.value)}
                                     placeholder={row.valueText}
                                     className="w-28 shrink-0 rounded border border-gray-300 px-2 py-1 text-xs disabled:bg-gray-100" />
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
            {cats && !cats.length && (
              <p className="text-sm text-gray-500">This venue's config has no comparable settings.</p>
            )}
          </div>
        )}

        {loaded && (
          <div className="flex items-center justify-between gap-3 border-t border-gray-200 p-4">
            <p className="text-xs text-gray-500">
              {saved ? "Saved." : dirty ? "Unsaved changes." : "No changes."}
            </p>
            <div className="flex items-center gap-2">
              <button onClick={onClose}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50">
                Close
              </button>
              <button onClick={save} disabled={busy || readOnly || !dirty}
                      className="inline-flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-sm
                                 font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300">
                <Save size={14} /> {busy ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Building2, RefreshCw, ChevronRight, AlertTriangle, AlertOctagon, Info,
  CheckCircle2, MinusCircle, Wifi, Cable, Network, Zap, Server, Users,
  Search, ArrowLeft, Radio, Key, ShieldCheck, FileDown,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import {
  registerSections, tabVisible, useVisible, VisibilityProvider,
} from "@/context/VisibilityContext";
import SingleEcSelector from "@/components/SingleEcSelector";
import { apiFetch } from "@/utils/api";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * PISR — Property Install Status Report.
 *
 * A read-only poll of one venue: what got installed, what is online, how it is
 * addressed, what VLANs and PoE it uses, and which SSIDs are demonstrably
 * carrying traffic. Nothing on this page writes to RUCKUS ONE.
 *
 * Human-driven only: every request fires because someone clicked. There is no
 * auto-refresh and no polling timer — a report is a moment, deliberately.
 */

type Tab = "overview" | "wireless" | "wired" | "addressing" | "identity" | "devices";

// The tab ids in bar order, separate from the labelled `allTabs` array further
// down because the effect that falls back off a hidden tab has to run ABOVE
// this component's early returns — React counts hooks, and a hook that only
// runs once a venue is chosen is a hook that changes count mid-session.
const TAB_ORDER: Tab[] = [
  "overview", "wireless", "wired", "addressing", "identity", "devices",
];

interface VenueRow {
  id: string;
  name: string;
  addressLine: string | null;
  city: string | null;
  country: string | null;
  aps: { total: number; online: number; offline: number } | null;
  switches: number | null;
  clients: number | null;
  networks: number | null;
  firmwareUpToDate: boolean | null;
}

const SEVERITY: Record<string, {
  badge: string; card: string; icon: React.ReactNode; label: string;
}> = {
  critical: {
    badge: "bg-red-600 text-white", card: "bg-red-50 border-red-300",
    icon: <AlertOctagon size={16} className="text-red-600" />, label: "Critical",
  },
  warning: {
    badge: "bg-amber-500 text-white", card: "bg-amber-50 border-amber-300",
    icon: <AlertTriangle size={16} className="text-amber-600" />, label: "Warning",
  },
  info: {
    badge: "bg-blue-500 text-white", card: "bg-blue-50 border-blue-200",
    icon: <Info size={16} className="text-blue-600" />, label: "Info",
  },
  ok: {
    badge: "bg-green-600 text-white", card: "bg-green-50 border-green-200",
    icon: <CheckCircle2 size={16} className="text-green-600" />, label: "Pass",
  },
  skipped: {
    badge: "bg-gray-400 text-white", card: "bg-gray-50 border-gray-200",
    icon: <MinusCircle size={16} className="text-gray-400" />, label: "Not checked",
  },
};

/**
 * Every section id this file renders, and the ONLY place the frontend holds
 * them as a list.
 *
 * Not fetched from /api/admin/visibility: that endpoint is admin-only, and an
 * ordinary user's page needs to know how many sections a tab has in order to
 * decide whether the tab still has anything on it. Not imported from the
 * Python catalogue either — the web build stage of the Dockerfile does not
 * copy api/, and adding it there to share one list of strings would couple the
 * frontend build to the backend tree for no real gain.
 *
 * So it is duplicated, deliberately, and `api/tests/test_sections.py` asserts
 * the two lists are identical. If you add a card, add its id to
 * api/sections.py, tag the card below, and the test will tell you if you
 * missed the PDF.
 */
export const SECTION_IDS = [
  "addressing.ap-subnets",
  "addressing.dhcp-pools",
  "addressing.dns",
  "addressing.external",
  "addressing.gateways",
  "addressing.switch-subnets",
  "devices.aps",
  "devices.switches",
  "identity.dpsk-pools",
  "identity.dpsk-summary",
  "identity.other-groups",
  "identity.policy-sets",
  "identity.radius",
  "overview.access-points",
  "overview.property",
  "overview.summary",
  "overview.switches",
  "overview.venue-config",
  "overview.verification",
  "report.sources",
  "wired.aps-on-ports",
  "wired.link-speeds",
  "wired.poe-budget",
  "wired.poe-standard",
  "wired.port-errors",
  "wired.summary",
  "wired.top-poe-draws",
  "wired.vlans",
  "wireless.ap-groups",
  "wireless.busiest-aps",
  "wireless.channel-plan",
  "wireless.clients-by-band",
  "wireless.clients-per-ssid",
  "wireless.connection-health",
  "wireless.signal-quality",
  "wireless.ssids",
] as const;

registerSections(SECTION_IDS);

const CHART_COLORS = ["#2563eb", "#0891b2", "#7c3aed", "#db2777", "#ea580c",
                      "#65a30d", "#0d9488", "#4f46e5"];

const fmtTime = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" }) : "—";

const fmtNum = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: digits });

const pctText = (p: number | null | undefined) => (p === null || p === undefined ? "—" : `${p}%`);

// ── presentational pieces ─────────────────────────────────────

/**
 * A card, which knows whether it is being shown.
 *
 * `id` names a section in api/sections.py. A card with one asks the visibility
 * context about itself and renders nothing when hidden; a card without one —
 * the venue picker, the loading placeholder — is always shown.
 *
 * Self-hiding rather than being wrapped at every call site because a wrapper
 * is a thing you can forget to add, and the failure of forgetting is an empty
 * card under a heading saying there are none. Doing it here means the id is
 * the only thing to get right.
 *
 * NOT A CONTROL. The data behind a hidden section arrives already emptied by
 * api/redact.py — see src/context/VisibilityContext.tsx. This removes the
 * container, not the contents.
 */
function Card({ id, title, hint, right, icon, children, className = "" }: {
  id?: string; title?: string; hint?: string; right?: React.ReactNode;
  icon?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  if (!useVisible(id)) return null;
  return (
    <div className={`min-w-0 bg-white border border-gray-200 rounded-lg p-4 ${className}`}>
      {(title || right) && (
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0">
            <h3 className="font-semibold text-gray-800 flex items-center gap-2">
              {icon}{title}
            </h3>
            {hint && <p className="text-xs text-gray-500 mt-0.5">{hint}</p>}
          </div>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

/**
 * The same self-hiding as `Card`, for blocks that are not cards — the tile
 * rows at the top of a tab, the sources footer.
 *
 * Kept as a wrapper rather than folded into each block because these have no
 * common component to hang an `id` prop off, and inventing one would mean
 * restructuring layout that is otherwise unchanged from its rtools2 original.
 */
function Section({ id, children }: { id: string; children: React.ReactNode }) {
  return useVisible(id) ? <>{children}</> : null;
}

function Stat({ label, value, sub, tone = "gray" }: {
  label: string; value: React.ReactNode; sub?: string; tone?: "gray" | "green" | "red" | "amber" | "blue";
}) {
  const tones: Record<string, string> = {
    gray: "text-gray-900", green: "text-green-700", red: "text-red-700",
    amber: "text-amber-700", blue: "text-blue-700",
  };
  return (
    <div className="bg-white border border-gray-200 rounded-lg px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-2xl font-semibold ${tones[tone]}`}>{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-0.5">{sub}</div>}
    </div>
  );
}

/** A donut of labelled segments. Renders a hollow grey ring when there is no data. */
function Donut({ segments, size = 132, center, centerLabel }: {
  segments: { label: string; value: number; color: string }[];
  size?: number; center?: React.ReactNode; centerLabel?: string;
}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const radius = size / 2 - 12;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="#f1f5f9" strokeWidth={16} />
        {total > 0 && segments.filter(s => s.value > 0).map((segment) => {
          const length = (segment.value / total) * circumference;
          const dash = `${length} ${circumference - length}`;
          const el = (
            <circle
              key={segment.label} cx={size / 2} cy={size / 2} r={radius} fill="none"
              stroke={segment.color} strokeWidth={16} strokeDasharray={dash}
              strokeDashoffset={-offset} transform={`rotate(-90 ${size / 2} ${size / 2})`}
            />
          );
          offset += length;
          return el;
        })}
        <text x="50%" y="47%" textAnchor="middle" className="fill-gray-900"
              style={{ fontSize: 22, fontWeight: 600 }}>
          {center ?? total}
        </text>
        {centerLabel && (
          <text x="50%" y="63%" textAnchor="middle" className="fill-gray-500" style={{ fontSize: 11 }}>
            {centerLabel}
          </text>
        )}
      </svg>
      <div className="space-y-1.5 min-w-0">
        {segments.map((segment) => (
          <div key={segment.label} className="flex items-center gap-2 text-sm">
            <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: segment.color }} />
            <span className="text-gray-600 truncate">{segment.label}</span>
            <span className="font-medium text-gray-900 ml-auto">{fmtNum(segment.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function BarList({ rows, limit = 8, unit = "" }: {
  rows: { label: string; count: number }[]; limit?: number; unit?: string;
}) {
  // limit={Infinity} prints the whole list with no "+n more" footer — used
  // where the tail IS the point, like every channel a band has a radio on.
  const shown = rows.slice(0, limit);
  const max = Math.max(1, ...shown.map((r) => r.count));
  if (!rows.length) return <p className="text-sm text-gray-400">Nothing reported.</p>;
  return (
    <div className="space-y-2">
      {shown.map((row, i) => (
        <div key={`${row.label}-${i}`}>
          <div className="flex justify-between text-sm mb-0.5">
            {/* min-w-0: a flex item defaults to min-width:auto, so `truncate`
                (white-space:nowrap) would otherwise widen the row rather than
                ellipse. An IPv6 nameserver is the case that shows it. */}
            <span className="text-gray-700 truncate pr-2 min-w-0">{row.label}</span>
            <span className="text-gray-900 font-medium shrink-0">{fmtNum(row.count)}{unit}</span>
          </div>
          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
            <div className="h-full rounded-full"
                 style={{ width: `${(row.count / max) * 100}%`,
                          background: CHART_COLORS[i % CHART_COLORS.length] }} />
          </div>
        </div>
      ))}
      {rows.length > limit && (
        <p className="text-xs text-gray-400">+{rows.length - limit} more</p>
      )}
    </div>
  );
}

function Meter({ pct, caption, right, tone }: {
  pct: number | null; caption: string; right?: string; tone?: "auto" | "blue";
}) {
  const value = pct ?? 0;
  const colour = tone === "blue" ? "bg-blue-500"
    : value >= 95 ? "bg-red-500" : value >= 85 ? "bg-amber-500" : "bg-green-500";
  return (
    <div>
      <div className="flex justify-between text-sm mb-1">
        <span className="text-gray-700 truncate pr-2 min-w-0">{caption}</span>
        <span className="text-gray-900 font-medium shrink-0">{right ?? pctText(pct)}</span>
      </div>
      <div className="h-2.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${colour}`} style={{ width: `${Math.min(value, 100)}%` }} />
      </div>
    </div>
  );
}

function Pill({ children, tone = "gray" }: { children: React.ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    gray: "bg-gray-100 text-gray-700", green: "bg-green-100 text-green-800",
    red: "bg-red-100 text-red-800", amber: "bg-amber-100 text-amber-800",
    blue: "bg-blue-100 text-blue-800", purple: "bg-purple-100 text-purple-800",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}

function StatePill({ state, status }: { state: string; status?: string }) {
  const tone = state === "online" ? "green" : state === "offline" ? "red" : "amber";
  return <Pill tone={tone}>{status || state}</Pill>;
}

/** A table that scrolls inside itself rather than stretching the page. */
function MiniTable({ columns, rows, empty = "Nothing to show.", maxHeight = "20rem" }: {
  columns: { key: string; header: string; className?: string }[];
  rows: Record<string, any>[]; empty?: string; maxHeight?: string;
}) {
  if (!rows.length) return <p className="text-sm text-gray-400">{empty}</p>;
  return (
    <div className="min-w-0 overflow-auto border border-gray-200 rounded" style={{ maxHeight }}>
      {/* min-w-full keeps narrow tables filling the card; w-max lets a wide one
          size to its content so the container scrolls sideways instead of
          crushing every column to unreadable width. */}
      <table className="min-w-full w-max text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>
            {columns.map((col) => (
              <th key={col.key}
                  className={`text-left font-semibold text-gray-700 px-3 py-2 whitespace-nowrap ${col.className || ""}`}>
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-gray-100 hover:bg-gray-50">
              {columns.map((col) => (
                <td key={col.key} className={`px-3 py-1.5 text-gray-700 ${col.className || ""}`}>
                  {row[col.key] === null || row[col.key] === undefined || row[col.key] === ""
                    ? <span className="text-gray-300">—</span>
                    : typeof row[col.key] === "boolean"
                      ? (row[col.key] ? "yes" : "no")
                      : row[col.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Evidence rows are plain dicts built by the checks, so the column headers are
// their keys. "onlineInScope" is a fine key and a poor header — but naive
// title-casing turns "ssid" into "Ssid", which is worse than leaving it alone.
const HEADER_WORDS: Record<string, string> = {
  ssid: "SSID", ssids: "SSIDs", ip: "IP", ap: "AP", aps: "APs", vlan: "VLAN",
  vlans: "VLANs", dns: "DNS", dhcp: "DHCP", poe: "PoE", snr: "SNR", rssi: "RSSI",
  mac: "MAC", bssid: "BSSID", cidr: "CIDR", lldp: "LLDP", wan: "WAN", lan: "LAN",
  w: "W", pct: "%", id: "ID", os: "OS",
};

function evidenceHeader(key: string): string {
  const words = key.replace(/([a-z0-9])([A-Z])/g, "$1 $2").split(" ");
  return words
    .map((word, i) => {
      const known = HEADER_WORDS[word.toLowerCase()];
      if (known) return known;
      return i === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word.toLowerCase();
    })
    .join(" ");
}

function Finding({ finding }: { finding: any }) {
  const [open, setOpen] = useState(false);
  const style = SEVERITY[finding.severity] || SEVERITY.info;
  const evidence: any[] = finding.evidence || [];
  const columns = evidence.length
    ? Object.keys(evidence[0]).map((key) => ({ key, header: evidenceHeader(key) }))
    : [];

  return (
    <div className={`border rounded-lg ${style.card}`}>
      <button
        className="w-full flex items-start gap-3 text-left px-3 py-2.5"
        onClick={() => evidence.length && setOpen(!open)}
      >
        <span className="mt-0.5">{style.icon}</span>
        <span className="min-w-0 flex-1">
          <span className="font-medium text-gray-900">{finding.title}</span>
          {/* The headline is the verdict; the check name says what was tested.
              Only shown when they differ, which is exactly when the finding is
              not a plain pass. */}
          {finding.check && finding.check !== finding.title && (
            <span className="block text-[11px] text-gray-400 mt-0.5">
              check: {finding.check}
            </span>
          )}
          <span className="block text-sm text-gray-700 mt-0.5">{finding.summary}</span>
          {finding.detail && <span className="block text-xs text-gray-500 mt-0.5">{finding.detail}</span>}
        </span>
        {!!evidence.length && (
          <span className="text-xs text-gray-500 flex items-center gap-1 shrink-0">
            {evidence.length} row{evidence.length === 1 ? "" : "s"}
            <ChevronRight size={14} className={open ? "rotate-90 transition" : "transition"} />
          </span>
        )}
      </button>
      {open && !!evidence.length && (
        <div className="px-3 pb-3">
          <MiniTable columns={columns} rows={evidence} maxHeight="16rem" />
        </div>
      )}
    </div>
  );
}

// ── page ──────────────────────────────────────────────────────

export default function PISR() {
  const { activeControllerId, activeControllerType, activeControllerSubtype, controllers } = useAuth();
  const activeController = controllers.find((c) => c.id === activeControllerId);
  const isR1 = activeControllerType === "RuckusONE";
  const needsEcSelection = activeControllerSubtype === "MSP";

  const [ecId, setEcId] = useState<string | null>(null);
  const [ecName, setEcName] = useState<string | null>(null);
  const ecChosen = isR1 && (!needsEcSelection || !!ecId);

  const [venues, setVenues] = useState<VenueRow[]>([]);
  const [venuesLoading, setVenuesLoading] = useState(false);
  const [venueFilter, setVenueFilter] = useState("");
  const [venue, setVenue] = useState<VenueRow | null>(null);

  const [report, setReport] = useState<any>(null);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("overview");
  const [deviceFilter, setDeviceFilter] = useState("");
  const [showPasses, setShowPasses] = useState(false);

  const base = `${API_BASE_URL}/pisr/${activeControllerId}`;

  const qs = useCallback((extra: Record<string, string> = {}) => {
    const params = new URLSearchParams(extra);
    if (needsEcSelection && ecId) params.set("tenant_id", ecId);
    const text = params.toString();
    return text ? `?${text}` : "";
  }, [needsEcSelection, ecId]);

  const loadVenues = useCallback(async () => {
    if (!ecChosen || !activeControllerId) return;
    setVenuesLoading(true);
    setError("");
    try {
      const res = await apiFetch(`${base}/venues${qs()}`, { credentials: "include" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      setVenues((await res.json()).venues || []);
    } catch (err: any) {
      setError(err.message || "Could not load venues");
    } finally {
      setVenuesLoading(false);
    }
  }, [base, qs, ecChosen, activeControllerId]);

  useEffect(() => { loadVenues(); }, [loadVenues]);

  const [exporting, setExporting] = useState(false);

  /**
   * Download the report as a PDF.
   *
   * The endpoint re-polls rather than rendering the report already on screen —
   * PISR stores nothing — so the PDF is its own snapshot and can differ by a
   * few clients from a page that has been open a while. Fetched rather than
   * linked so the auth wrapper applies and a failure surfaces as an error
   * instead of a broken download.
   */
  const downloadPdf = useCallback(async () => {
    if (!venue) return;
    setExporting(true);
    setError("");
    try {
      const res = await apiFetch(`${base}/report.pdf${qs({ venue_id: venue.id })}`,
                                 { credentials: "include" });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail || `Export failed (HTTP ${res.status})`);
      }
      const blob = await res.blob();
      const name = res.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1]
                   || "site-review.pdf";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message || "PDF export failed");
    } finally {
      setExporting(false);
    }
  }, [venue, base, qs]);

  const poll = useCallback(async (target: VenueRow) => {
    setPolling(true);
    setError("");
    try {
      const res = await apiFetch(`${base}/report${qs({ venue_id: target.id })}`, { credentials: "include" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
      setReport(await res.json());
    } catch (err: any) {
      setError(err.message || "Could not build the report");
    } finally {
      setPolling(false);
    }
  }, [base, qs]);

  const chooseVenue = (row: VenueRow) => {
    setVenue(row);
    setReport(null);
    setTab("overview");
    poll(row);
  };

  // Which sections this reader is not being shown, as the server decided it.
  // Read here rather than at the tab bar so the effect below can sit with the
  // other hooks: everything from `if (!activeControllerId)` onwards is an early
  // return, and a hook after one of those is only sometimes called.
  const hiddenSections: string[] | undefined = report?.visibility?.hidden;

  // If the selected tab just vanished under a policy change — or the first
  // report arrives with it already hidden — fall back to the first tab that
  // survived, rather than rendering a page with nothing on it.
  useEffect(() => {
    if (tabVisible(hiddenSections, tab)) return;
    const fallback = TAB_ORDER.find((id) => tabVisible(hiddenSections, id));
    if (fallback) setTab(fallback);
  }, [hiddenSections, tab]);

  const filteredVenues = useMemo(() => {
    const needle = venueFilter.trim().toLowerCase();
    if (!needle) return venues;
    return venues.filter((v) =>
      [v.name, v.addressLine, v.city, v.country].filter(Boolean)
        .join(" ").toLowerCase().includes(needle));
  }, [venues, venueFilter]);

  // ── gates ───────────────────────────────────────────────────

  if (!activeControllerId || !isR1) {
    return (
      <div className="p-6 max-w-3xl">
        <Header />
        <Card title="Pick a RUCKUS ONE controller">
          <p className="text-sm text-gray-600">
            PISR reports on a RUCKUS ONE venue. The active controller is{" "}
            {activeController ? `${activeController.name} (${activeControllerType})` : "not set"}.
            Choose an R1 controller on the Controllers page to continue.
          </p>
        </Card>
      </div>
    );
  }

  if (!ecChosen) {
    return (
      <div className="p-6 max-w-5xl">
        <Header />
        <Card title="Choose the MSP-EC"
              hint="This is an MSP controller, so it owns no venues itself — pick the end customer to report on.">
          <SingleEcSelector
            controllerId={activeControllerId}
            selectedEcId={ecId}
            onEcSelect={(id, ec) => { setEcId(id); setEcName(ec?.name || null); setVenue(null); setReport(null); }}
          />
        </Card>
      </div>
    );
  }

  if (!venue) {
    return (
      <div className="p-6 max-w-6xl">
        <Header ec={ecName} onChangeEc={needsEcSelection ? () => { setEcId(null); setVenues([]); } : undefined} />
        <Card
          title="Choose a venue"
          hint="One venue per report. Counts come from RUCKUS ONE's own aggregates and are a hint, not the report."
          right={
            <button onClick={loadVenues} disabled={venuesLoading}
                    className="inline-flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border border-gray-300 hover:bg-gray-50 disabled:opacity-50">
              <RefreshCw size={14} className={venuesLoading ? "animate-spin" : ""} /> Reload
            </button>
          }
        >
          {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
          <div className="relative mb-4">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input
              value={venueFilter} onChange={(e) => setVenueFilter(e.target.value)}
              placeholder="Search venues…"
              className="w-full pl-9 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            />
          </div>

          {venuesLoading ? (
            <p className="text-sm text-gray-500">Loading venues…</p>
          ) : !filteredVenues.length ? (
            <p className="text-sm text-gray-500">No venues match.</p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {/* min-w-0 on the card is load-bearing. A grid item defaults to
                  min-width:auto, so it will not shrink below its content's
                  minimum — and the address line sets white-space:nowrap via
                  `truncate`, which makes that minimum the full length of the
                  address. Without it the column widens to fit a long address,
                  the card overflows its grid, and the whole page scrolls
                  sideways on a phone instead of the text ellipsing. */}
              {filteredVenues.map((row) => (
                <button key={row.id} onClick={() => chooseVenue(row)}
                        className="min-w-0 text-left border border-gray-200 rounded-lg p-3 hover:border-blue-400 hover:bg-blue-50/40 transition">
                  <div className="flex items-start justify-between gap-2 min-w-0">
                    {/* break-words, not truncate: the name is what identifies
                        the venue, so it wraps rather than being cut off — and
                        a name with no spaces in it has to be breakable or it
                        pushes the chevron off the card on its own. */}
                    <span className="font-medium text-gray-900 min-w-0 break-words">{row.name}</span>
                    <ChevronRight size={16} className="text-gray-400 mt-0.5 shrink-0" />
                  </div>
                  <div className="text-xs text-gray-500 truncate">
                    {[row.addressLine, row.city, row.country].filter(Boolean).join(", ") || "No address"}
                  </div>
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {row.aps && (
                      <Pill tone={row.aps.offline ? "amber" : "green"}>
                        {row.aps.online}/{row.aps.total} APs up
                      </Pill>
                    )}
                    {row.switches !== null && <Pill>{row.switches} switches</Pill>}
                    {row.clients !== null && <Pill tone="blue">{row.clients} clients</Pill>}
                    {row.networks !== null && <Pill tone="purple">{row.networks} SSIDs</Pill>}
                  </div>
                </button>
              ))}
            </div>
          )}
        </Card>
      </div>
    );
  }

  // ── report ──────────────────────────────────────────────────

  const verification = report?.verification;
  const findings: any[] = verification?.findings || [];
  const visibleFindings = showPasses
    ? findings
    : findings.filter((f) => f.severity !== "ok" && f.severity !== "skipped");

  const allTabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    { id: "overview", label: "Overview", icon: <Building2 size={15} /> },
    { id: "wireless", label: "Wireless", icon: <Wifi size={15} /> },
    { id: "wired", label: "Wired & PoE", icon: <Cable size={15} /> },
    { id: "addressing", label: "Addressing", icon: <Network size={15} /> },
    { id: "identity", label: "Identity & Policy", icon: <Key size={15} /> },
    { id: "devices", label: "Devices", icon: <Server size={15} /> },
  ];
  // A tab whose every section is hidden is dropped rather than shown empty.
  // The hidden list rides on the report, so this is undefined until the first
  // poll, and `tabVisible` answers true for everything until then — which is
  // what keeps the bar stable rather than reshuffling as the report lands.
  const tabs = allTabs.filter((entry) => tabVisible(hiddenSections, entry.id));

  return (
    <div className="p-6 max-w-7xl">
      <Header ec={ecName} />

      <div className="bg-white border border-gray-200 rounded-lg p-4 mb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <button onClick={() => { setVenue(null); setReport(null); }}
                    className="inline-flex items-center gap-1 text-xs text-gray-500 hover:text-gray-800 mb-1">
              <ArrowLeft size={13} /> All venues
            </button>
            <h2 className="text-xl font-semibold text-gray-900 flex items-center gap-2">
              {venue.name}
              {report?.venue?.isProperty && <Pill tone="purple">Property</Pill>}
            </h2>
            <p className="text-sm text-gray-500">
              {[report?.venue?.address?.line || venue.addressLine,
                report?.venue?.address?.city || venue.city,
                report?.venue?.address?.country || venue.country].filter(Boolean).join(", ") || "No address"}
              {report?.venue?.address?.timezone ? ` · ${report.venue.address.timezone}` : ""}
            </p>
          </div>
          <div className="text-right">
            <button onClick={downloadPdf} disabled={!report || exporting}
                    className="inline-flex items-center gap-1.5 px-3 py-2 mr-2 rounded border border-gray-300 text-sm font-medium hover:bg-gray-50 disabled:opacity-50">
              <FileDown size={15} className={exporting ? "animate-pulse" : ""} />
              {exporting ? "Building…" : "PDF"}
            </button>
            <button onClick={() => poll(venue)} disabled={polling}
                    className="inline-flex items-center gap-1.5 px-3 py-2 rounded bg-blue-600 text-white text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
              <RefreshCw size={15} className={polling ? "animate-spin" : ""} />
              {polling ? "Polling…" : "Refresh"}
            </button>
            <p className="text-xs text-gray-500 mt-1">
              {report ? `Polled ${fmtTime(report.meta?.polledAt)} · ${report.meta?.elapsedSeconds}s` : "Not polled yet"}
            </p>
          </div>
        </div>
      </div>

      {error && (
        <div className="border border-red-300 bg-red-50 text-red-800 rounded-lg p-3 mb-4 text-sm">{error}</div>
      )}

      {polling && !report && (
        <Card><p className="text-sm text-gray-500">Reading the venue — APs, switches, ports, clients and SSIDs, all at once. A big venue takes a few seconds.</p></Card>
      )}

      {report && (
        // Everything that renders report data sits inside the provider, which
        // is fed from the report itself — so a card and the policy it was
        // rendered under can never be a version apart.
        <VisibilityProvider hidden={hiddenSections}>
          <div className="flex gap-1 border-b border-gray-200 mb-4 overflow-x-auto">
            {tabs.map((entry) => (
              <button key={entry.id} onClick={() => setTab(entry.id)}
                      className={`inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 -mb-px whitespace-nowrap ${
                        tab === entry.id
                          ? "border-blue-600 text-blue-700"
                          : "border-transparent text-gray-500 hover:text-gray-800"}`}>
                {entry.icon}{entry.label}
              </button>
            ))}
          </div>

          {tab === "overview" && (
            <Overview report={report} findings={visibleFindings} allFindings={findings}
                      showPasses={showPasses} onTogglePasses={() => setShowPasses(!showPasses)} />
          )}
          {tab === "wireless" && <Wireless report={report} />}
          {tab === "wired" && <Wired report={report} />}
          {tab === "addressing" && <Addressing report={report} />}
          {tab === "identity" && <Dpsk report={report} />}
          {tab === "devices" && (
            <Devices report={report} filter={deviceFilter} onFilter={setDeviceFilter} />
          )}

          <Section id="report.sources">
            <Sources meta={report.meta} />
          </Section>
        </VisibilityProvider>
      )}
    </div>
  );
}

function Header({ ec, onChangeEc }: { ec?: string | null; onChangeEc?: () => void }) {
  return (
    <div className="mb-4">
      <div className="flex items-center gap-2 flex-wrap">
        <h1 className="text-2xl font-bold text-gray-900">PISR</h1>
        <Pill tone="purple">Alpha</Pill>
        <Pill tone="green">Read-only</Pill>
        {ec && (
          <span className="text-sm text-gray-500">
            · {ec}
            {onChangeEc && (
              <button onClick={onChangeEc} className="ml-2 text-blue-600 hover:underline text-xs">change</button>
            )}
          </span>
        )}
      </div>
      <p className="text-sm text-gray-500 mt-0.5">
        Property Install Status Report — one venue, polled on demand. PISR only reads: it never
        changes configuration, and it stores nothing between refreshes.
      </p>
    </div>
  );
}

// ── tabs ──────────────────────────────────────────────────────

/**
 * Summarise a device group's reachability.
 *
 * `online + offline` does not account for a fleet — R1 has states that are
 * neither, "never contacted cloud" chief among them. Keying the summary off
 * `offline` alone made a venue with 213 online, 0 offline and 5 never-contacted
 * APs claim "all reachable" over the top of "213/218", which is both wrong and
 * exactly the kind of thing a site review exists to catch. Anything not online
 * is named by the status R1 gave it.
 */
function reachSub(group: any): string {
  if (!group.total) return "none at this venue";
  const missing = group.total - group.online;
  if (missing <= 0) return "all reachable";

  const named: any[] = group.notOnlineByStatus || [];
  if (named.length) {
    const parts = named.slice(0, 2).map((row) => `${row.count} ${String(row.label).toLowerCase()}`);
    const rest = named.slice(2).reduce((sum, row) => sum + row.count, 0);
    if (rest) parts.push(`${rest} other`);
    return parts.join(" · ");
  }
  return `${missing} not online`;
}

function reachTone(group: any): "red" | "amber" | "green" {
  if (group.offline) return "red";
  if (group.total && group.online < group.total) return "amber";
  return "green";
}


/**
 * Channel plan as a frequency chart, one row per band.
 *
 * Channels are placed at their true centre frequency rather than laid out as
 * evenly spaced boxes, so adjacency and gaps are visible: 2.4 GHz 1/6/11 sit
 * with real spectrum between them, and a 6 GHz plan spreads across 1.2 GHz.
 *
 * Each channel is drawn as its 20 MHz slot. An in-use channel also gets a
 * translucent band behind it at the radio's actual operating width, which is
 * how an 80 or 160 MHz radio shows the spectrum it really occupies.
 */
/**
 * A band's channel-allocation chart: one row per bonding width, channel numbers
 * along the top, frequency axis underneath — the layout every Wi-Fi channel
 * chart uses. Blocks are trapezoids because that is the convention; they read
 * as channel masks.
 *
 * Drawn in a 1000-unit box scaled uniformly. Stretching a 0-100 box to fit
 * would smear the axis type horizontally along with the bars.
 */
function SpectrumChart({ band }: { band: any }) {
  // Gutter fits the longest row label ("20 MHz · 1/6/11") at 14 units/char.
  const W = 1000, GUTTER = 118, REGION = 15, ROW_H = 27, AXIS = 22;
  const plot = W - GUTTER;
  const span = Math.max(1, band.maxMhz - band.minMhz);
  const x = (mhz: number) => GUTTER + ((mhz - band.minMhz) / span) * plot;

  // A horizontal channel number needs ~22 units. Where slots are tighter than
  // that — 59 channels on a 6 GHz plan — turn the labels on their side rather
  // than thinning them away, so every channel is still named.
  const allSlots: any[] = band.slots || [];
  const gaps = allSlots.slice(1).map((s: any, i: number) =>
    (Math.abs(s.centreMhz - allSlots[i].centreMhz) / span) * plot);
  const vertical = gaps.length ? Math.min(...gaps) < 22 : false;
  const TOP = vertical ? 62 : 38;
  const labelSize = vertical ? 9 : 12;

  // 2.4 GHz slots are 20 MHz wide but 5 MHz apart, so each covers three
  // quarters of its neighbour. Drawn opaque and in order, every shape hid the
  // previous one's slope and the row read as leaning parallelograms rather
  // than channel masks. Overlapping slots are drawn translucent with a toned
  // outline — which is how a real 2.4 GHz chart shows they sit on top of each
  // other — and in-use ones are drawn last, opaque.
  const OUTLINE: Record<string, string> = {
    "#bbf7d0": "#4ade80", "#e8eaed": "#c3c9d0",
    "#2a78d6": "#1d4ed8", "#f59e0b": "#b45309",
  };

  // Paint order, least significant first.
  //
  // Sorting only in-use last left every state that differs by colour alone to
  // paint in channel order. On an overlapping row that buries information: a
  // not-permitted slot drawn later lays its 45%-opacity grey across a
  // permitted-but-unused neighbour drawn earlier, and the green reads as grey.
  // 2.4 GHz channel 10 under channel 12 is the case that shows it.
  //
  // Ranking by how much each state has to say — grey says least, an in-use
  // channel outside the plan says most — puts the meaningful colour on top
  // wherever two slots share spectrum.
  const rank = (b: any) => (b.offPlan ? 3 : b.inUse ? 2 : b.allowed ? 1 : 0);

  const rows = band.rows || [];
  const H = TOP + rows.length * ROW_H + AXIS;
  const axisY = TOP + rows.length * ROW_H;

  // Thin the channel labels by SPACING. Labelling only in-use channels fails
  // on wide bonding: a 160 MHz block marks all eight of its channels in use,
  // which lights up nearly every label on a 6 GHz plan.
  // Vertical labels fit every channel; horizontal ones are thinned by spacing.
  const labels: any[] = [];
  let lastX = -1e9;
  for (const slot of allSlots) {
    const pos = x(slot.centreMhz);
    if (!vertical && pos - lastX < 22) continue;
    labels.push({ x: pos, text: slot.channel, psc: !!slot.psc });
    lastX = pos;
  }

  const step = span > 800 ? 200 : span > 300 ? 100 : span > 120 ? 40 : 20;
  const ticks: number[] = [];
  for (let f = Math.ceil(band.minMhz / step) * step; f <= band.maxMhz; f += step) {
    const pos = x(f);
    if (pos >= GUTTER + 12 && pos <= W - 12) ticks.push(f);
  }

  const xr = (mhz: number) => x(mhz);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ display: "block", width: "100%", height: "auto" }}>
      {/* Regulatory sub-bands behind everything. Every band alternates between
          two greys and carries a divider at each boundary — a single flat grey
          said nothing about where one ended, and on 6 GHz there are four in a
          row. DFS gets no tint of its own: amber is the in-use-but-not-
          permitted colour in the blocks painted on top, and a background that
          borrowed it made a whole sub-band look like a finding. DFS is named
          in the sub-band label instead. */}
      {(band.regions || []).map((r: any, ri: number) => {
        const x0 = xr(r.clipLoMhz), x1 = xr(r.clipHiMhz);
        if (x1 - x0 < 2) return null;
        const fill = ri % 2 ? "#eceff3" : "#f8fafc";
        const stroke = ri % 2 ? "#dfe4ea" : "#eef2f6";
        return (
          <g key={r.label}>
            <rect x={x0} y={REGION} width={x1 - x0} height={axisY - REGION + 4}
                  fill={fill} stroke={stroke} strokeWidth="1" />
            {ri > 0 && (
              <line x1={x0} y1={REGION} x2={x0} y2={axisY + 4}
                    stroke="#cbd5e1" strokeWidth="1" />
            )}
            {x1 - x0 > 46 && (
              <text x={(x0 + x1) / 2} y={REGION - 4} textAnchor="middle" fontSize="12"
                    fontWeight="600" fill="#94a3b8">
                {r.label}{r.dfs ? " · DFS" : ""}
              </text>
            )}
          </g>
        );
      })}
      {/* 6 GHz Preferred Scanning Channels — a client only probes these. */}
      {band.isSixGhz && (band.slots || []).filter((s: any) => s.psc).map((s: any) => (
        <line key={`psc-${s.channel}`} x1={xr(s.centreMhz)} y1={REGION}
              x2={xr(s.centreMhz)} y2={axisY} stroke="#7c3aed" strokeWidth="1"
              strokeDasharray="3 3" opacity="0.55" />
      ))}
      {rows.map((row: any, i: number) => {
        const top = TOP + i * ROW_H;
        const bottom = top + ROW_H - 6;
        // Overlap is a per-ROW property: 1/6/11 sit 25 MHz apart and do not
        // overlap, while 2/3/4/5 do. A band-wide flag drew the clean row
        // translucent for no reason.
        const centres = row.blocks
          .map((b: any) => (b.loMhz + b.hiMhz) / 2).sort((a: number, z: number) => a - z);
        const gaps = centres.slice(1).map((c: number, k: number) => c - centres[k]);
        const overlapping = gaps.length ? Math.min(...gaps) < row.width - 0.1 : false;
        return (
          <g key={row.width}>
            <text x={GUTTER - 6} y={top + (ROW_H - 6) / 2 + 4} textAnchor="end" fontSize="14"
                  fill={row.radios ? "#374151" : "#9ca3af"}
                  fontWeight={row.radios ? 600 : 400}>{row.label || `${row.width} MHz`}</text>
            {[...row.blocks]
              .sort((p: any, q: any) => rank(p) - rank(q))
              .map((b: any) => {
              const x0 = x(b.loMhz), x1 = x(b.hiMhz);
              const inset = Math.min(22, Math.max(1, (x1 - x0) * 0.18));
              // blue = permitted and in use; light green = permitted, spare;
              // light grey = not permitted here; amber = in use but not permitted.
              const fill = b.offPlan ? "#f59e0b"
                         : b.inUse ? "#2a78d6"
                         : b.allowed ? "#bbf7d0" : "#e8eaed";
              return (
                <g key={b.label}>
                  <polygon fill={fill}
                           fillOpacity={!overlapping || b.inUse || b.offPlan ? 1 : 0.45}
                           stroke={overlapping ? (OUTLINE[fill] || "#ffffff") : "#ffffff"}
                           strokeWidth="1" strokeLinejoin="round"
                           points={`${x0},${bottom} ${x0 + inset},${top} ${x1 - inset},${top} ${x1},${bottom}`}>
                    <title>{`${b.label} · ${b.loMhz}–${b.hiMhz} MHz${
                      b.inUse ? ` · ${b.count} radio(s) at ${row.width} MHz`
                              : " · permitted, unused"}${
                      b.offPlan ? " · NOT permitted by the venue" : ""}`}</title>
                  </polygon>
                  {/* Name every BONDED block by its centre channel, not just
                      the ones in use — on a 40/80/160 row that number is the
                      only thing identifying the block. The 20 MHz row is left
                      bare; those channels are named along the top already. */}
                  {row.width > 20 && x1 - x0 >= String(b.label).length * 8 + 5 && (
                    <text x={(x0 + x1) / 2} y={top + (bottom - top) / 2 + 4} textAnchor="middle"
                          fontSize="12"
                          fill={b.inUse || b.offPlan ? "#fff"
                                : b.allowed ? "#15803d" : "#9aa1a9"}
                          fontWeight={b.inUse || b.offPlan ? 700 : 600}>{b.label}</text>
                  )}
                </g>
              );
            })}
          </g>
        );
      })}
      {labels.map((l) => (
        <text key={l.text} x={l.x} y={TOP - 6} fontSize={labelSize}
              textAnchor={vertical ? "start" : "middle"}
              transform={vertical ? `rotate(-90 ${l.x} ${TOP - 6})` : undefined}
              fill={l.psc ? "#7c3aed" : "#6b7280"} fontWeight={l.psc ? 700 : 400}>
          {l.text}
        </text>
      ))}
      <line x1={GUTTER} y1={axisY} x2={W} y2={axisY} stroke="#d1d5db" strokeWidth="1" />
      {ticks.map((f) => (
        <g key={f}>
          <line x1={x(f)} y1={axisY} x2={x(f)} y2={axisY + 4} stroke="#d1d5db" strokeWidth="1" />
          <text x={x(f)} y={axisY + 15} textAnchor="middle" fontSize="13" fill="#9ca3af">{f}</text>
        </g>
      ))}
      <text x={W} y={axisY + 15} textAnchor="end" fontSize="11" fill="#c3c2b7">MHz</text>
    </svg>
  );
}

function ChannelPlan({ plan }: { plan: any[] }) {
  if (!plan?.length) return null;
  return (
    <div className="mt-4 pt-3 border-t border-gray-100">
      <div className="flex items-baseline justify-between mb-1">
        <p className="text-xs uppercase tracking-wide text-gray-500">Channel plan</p>
        <p className="text-[11px] text-gray-500">
          <span className="inline-block w-2.5 h-2.5 rounded-sm align-middle mr-1"
                style={{ background: "#2a78d6" }} />permitted, in use
          <span className="inline-block w-2.5 h-2.5 rounded-sm align-middle ml-3 mr-1"
                style={{ background: "#bbf7d0" }} />permitted, unused
          <span className="inline-block w-2.5 h-2.5 rounded-sm align-middle ml-3 mr-1"
                style={{ background: "#e8eaed" }} />not permitted
          <span className="inline-block w-2.5 h-2.5 rounded-sm align-middle ml-3 mr-1"
                style={{ background: "#f59e0b" }} />in use, not permitted
        </p>
      </div>
      <p className="text-[11px] text-gray-500 mb-3">
        One row per bonding width, labelled by the bonded centre channel. The alternating
        grey bands are the regulatory sub-bands, labelled above; a DFS band says so in its
        label, and is one a radio must vacate on radar detection. On 6 GHz a dotted violet
        line marks a Preferred Scanning Channel — a client only probes those.
      </p>
      {plan.map((band) => (
        <div key={band.band} className="mb-4">
          <div className="text-[11px] text-gray-600 mb-0.5">
            <span className="font-semibold text-gray-900">{band.band}</span>
            {" — "}{band.width || "auto"} · {band.method || "—"} ·{" "}
            {band.inUseCount} of {band.allowedCount} permitted in use across{" "}
            {band.radios} radio(s)
            {band.offPlanCount ? (
              <span className="text-amber-700 font-semibold"> · {band.offPlanCount} off-plan</span>
            ) : null}
            {band.outsidePlanCount ? (
              <span className="text-amber-700 font-semibold">
                {" "}· {band.outsidePlanCount} radio(s) outside the 1/6/11 plan
              </span>
            ) : null}
          </div>
          <SpectrumChart band={band} />
        </div>
      ))}
    </div>
  );
}

function Overview({ report, findings, allFindings, showPasses, onTogglePasses }: {
  report: any; findings: any[]; allFindings: any[];
  showPasses: boolean; onTogglePasses: () => void;
}) {
  const counts = report.verification?.counts || {};
  const aps = report.inventory.aps;
  const switches = report.inventory.switches;
  const property = report.venue?.property;

  return (
    <div className="space-y-4">
      <Section id="overview.summary">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="APs online" value={`${aps.online}/${aps.total}`}
              tone={reachTone(aps)} sub={reachSub(aps)} />
        <Stat label="Switches online" value={`${switches.online}/${switches.total}`}
              tone={reachTone(switches)}
              sub={switches.online < switches.total
                ? reachSub(switches)
                : `${fmtNum(switches.ports)} ports`} />
        <Stat label="SSIDs activated" value={report.wireless.activated} tone="blue"
              sub={report.wireless.unresolved
                ? `${report.wireless.unresolved} without a definition`
                : "on this venue"} />
        <Stat label="Clients now" value={fmtNum(report.clients.total)} tone="blue"
              sub={report.clients.capped ? "capped at 10,000" : "live associations"} />
        {report.poe.hasPoeBudget ? (
          <Stat label="PoE allocated" value={pctText(report.poe.allocatedPct)}
                tone={(report.poe.allocatedPct || 0) >= 85 ? "amber" : "gray"}
                sub={`${fmtNum(report.poe.allocatedWatts, 1)} W of ${fmtNum(report.poe.capacityWatts, 1)} W`} />
        ) : (
          <Stat label="PoE allocated" value="—" tone="gray"
                sub={report.poe.switchCount
                  ? "no switch reports a PoE budget"
                  : "no switches at this venue"} />
        )}
      </div>
      </Section>

      <Card id="overview.verification"
        title="Verification"
        icon={<CheckCircle2 size={17} className="text-gray-400" />}
        hint={`${report.verification?.score?.passed} of ${report.verification?.score?.ran} checks passed.`}
        right={
          <button onClick={onTogglePasses}
                  className="text-sm px-3 py-1.5 rounded border border-gray-300 hover:bg-gray-50">
            {showPasses ? "Hide passes" : `Show all ${allFindings.length}`}
          </button>
        }
      >
        <div className="flex flex-wrap gap-2 mb-3">
          {(["critical", "warning", "info", "ok", "skipped"] as const).map((level) => (
            <span key={level}
                  className={`px-2.5 py-1 rounded text-xs font-medium ${SEVERITY[level].badge} ${
                    counts[level] ? "" : "opacity-40"}`}>
              {counts[level] || 0} {SEVERITY[level].label.toLowerCase()}
            </span>
          ))}
        </div>
        {findings.length ? (
          <div className="space-y-2">
            {findings.map((finding) => <Finding key={finding.id} finding={finding} />)}
          </div>
        ) : (
          <p className="text-sm text-green-700">Nothing to flag — every check that could run passed.</p>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card id="overview.access-points" title="Access points" icon={<Wifi size={17} className="text-gray-400" />}
              hint="Status as RUCKUS ONE reports it.">
          <Donut
            centerLabel="APs"
            segments={[
              { label: "Online", value: aps.online, color: "#16a34a" },
              { label: "Offline", value: aps.offline, color: "#dc2626" },
              { label: "Other", value: aps.other, color: "#f59e0b" },
            ]}
          />
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <div>
              <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Models</p>
              <BarList rows={aps.byModel} limit={5} />
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Firmware</p>
              <BarList rows={aps.byFirmware} limit={5} />
            </div>
          </div>
        </Card>

        <Card id="overview.switches" title="Switches" icon={<Cable size={17} className="text-gray-400" />}
              hint="Wired estate in this venue.">
          {switches.total ? (
            <>
              <Donut
                centerLabel="switches"
                segments={[
                  { label: "Online", value: switches.online, color: "#16a34a" },
                  { label: "Offline", value: switches.offline, color: "#dc2626" },
                  { label: "Other", value: switches.other, color: "#f59e0b" },
                ]}
              />
              <div className="mt-4 grid gap-4 sm:grid-cols-2">
                <div>
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Models</p>
                  <BarList rows={switches.byModel} limit={5} />
                </div>
                <div>
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Firmware</p>
                  <BarList rows={switches.byFirmware} limit={5} />
                </div>
              </div>
            </>
          ) : (
            <p className="text-sm text-gray-500">No switches are assigned to this venue.</p>
          )}
        </Card>
      </div>

      <div className="space-y-4">
        <Card id="overview.venue-config" title="Venue configuration" icon={<Building2 size={17} className="text-gray-400" />}
              hint="What the venue is set to, as opposed to what the hardware landed on.">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-gray-500">AP management VLAN</dt>
            <dd className="text-gray-900">{report.venue.managementVlan ?? "untagged"}</dd>

            <dt className="text-gray-500">Mesh</dt>
            <dd className="text-gray-900">
              {report.venue.meshEnabled === null || report.venue.meshEnabled === undefined
                ? "—" : report.venue.meshEnabled
                  ? `enabled (${report.venue.mesh?.radioType || "?"})` : "disabled"}
            </dd>

            <dt className="text-gray-500">Mesh zero-touch</dt>
            <dd className="text-gray-900">
              {report.venue.meshZeroTouch === null || report.venue.meshZeroTouch === undefined
                ? "—" : report.venue.meshZeroTouch ? "enabled" : "disabled"}
            </dd>

            <dt className="text-gray-500">AP groups</dt>
            <dd className="text-gray-900">{report.wireless.groups.length}</dd>

            <dt className="text-gray-500">SSIDs activated</dt>
            <dd className="text-gray-900">{report.wireless.activated}</dd>

            <dt className="text-gray-500">Config template</dt>
            <dd className="text-gray-900">
              {report.venue.enforced
                ? "enforced — venue is driven by a template"
                : "not enforced"}
            </dd>

            <dt className="text-gray-500">5 GHz radios</dt>
            <dd className="text-gray-900">
              {report.venue.dual5g ? "dual 5 GHz configured" : "single 5 GHz"}
            </dd>

            <dt className="text-gray-500">Country</dt>
            <dd className="text-gray-900">
              {report.venue.address.country || "—"}
              {report.venue.address.timezone ? ` · ${report.venue.address.timezone}` : ""}
            </dd>

            <dt className="text-gray-500">Coordinates</dt>
            <dd className="text-gray-900">
              {report.venue.address.latitude && report.venue.address.longitude
                ? `${report.venue.address.latitude}, ${report.venue.address.longitude}` : "—"}
            </dd>
          </dl>

          <ChannelPlan plan={report.radios?.plan || []} />

          {(report.venue.radio || []).length > 0 && (
            <div className="mt-4 pt-3 border-t border-gray-100">
              <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Radio defaults</p>
              <MiniTable
                maxHeight="14rem"
                columns={[
                  { key: "band", header: "Band" },
                  { key: "method", header: "Channel method" },
                  { key: "width", header: "Width" },
                  { key: "power", header: "Tx power" },
                  // The whole list, wrapping. At 20 MHz a 6 GHz plan has 56
                  // channels and the point of the row is which ones — a
                  // truncated list answers nothing. `whitespace-normal`
                  // overrides MiniTable's nowrap so it can run to several lines.
                  { key: "allowedText", header: "Allowed channels",
                    className: "whitespace-normal max-w-[46rem]" },
                ]}
                rows={(report.venue.radio || []).map((entry: any) => ({
                  ...entry,
                  allowedText: entry.allowed?.length ? (
                    <span>
                      <span className="font-semibold">{entry.allowed.length}</span>
                      {" — "}{entry.allowed.join(", ")}
                    </span>
                  ) : null,
                }))}
                empty="No venue radio settings returned."
              />
            </div>
          )}
        </Card>

        <Card id="overview.property" title="MDU Property Features" icon={<Building2 size={17} className="text-gray-400" />}
              hint={property
                ? "Property Management is enabled on this venue."
                : "Property Management is a RUCKUS ONE feature enabled per venue."}>
          {property ? (
            <>
              <div className="grid gap-2 sm:grid-cols-3 mb-3">
                <Stat label="Units" value={property.unitCount ?? "—"} tone="blue" />
                <Stat label="With a resident" value={property.unitsWithResident ?? "—"}
                      tone={property.unitsWithoutResident ? "amber" : "green"}
                      sub={property.unitsWithoutResident
                        ? `${property.unitsWithoutResident} unassigned` : "all assigned"} />
                <Stat label="Unit identities" value={property.unitIdentityCount ?? "—"} />
              </div>
              <dl className="grid grid-cols-2 gap-y-2 text-sm">
                <dt className="text-gray-500">Status</dt>
                <dd className="text-gray-900">{property.status || "—"}</dd>
                <dt className="text-gray-500">Unit status</dt>
                <dd className="text-gray-900">
                  {(property.unitsByStatus || []).length
                    ? property.unitsByStatus.map((s: any) => `${s.count} ${s.label.toLowerCase()}`).join(", ")
                    : "—"}
                </dd>
                <dt className="text-gray-500">Resident portal</dt>
                <dd className="text-gray-900">
                  {property.residentPortalAllowed ? "allowed" : "not allowed"}
                  {property.residentPortalId ? " · configured" : ""}
                </dd>
                <dt className="text-gray-500">Resident API</dt>
                <dd className="text-gray-900">{property.residentApiAllowed ? "allowed" : "not allowed"}</dd>
                <dt className="text-gray-500">Guest access</dt>
                <dd className="text-gray-900">{property.guestAllowed ? "allowed" : "not allowed"}</dd>
                <dt className="text-gray-500">Resident comms</dt>
                <dd className="text-gray-900">
                  {property.communication
                    ? [property.communication.email && "email", property.communication.sms && "SMS"]
                        .filter(Boolean).join(", ") || "none enabled"
                    : "—"}
                </dd>
                {property.maxUnitCount ? (<>
                  <dt className="text-gray-500">Unit cap</dt>
                  <dd className="text-gray-900">{property.maxUnitCount}</dd>
                </>) : null}
              </dl>
            </>
          ) : (
            <div>
              <p className="text-sm text-gray-700">
                <span className="font-medium">Property Management is not enabled on this venue.</span>
              </p>
              <p className="text-xs text-gray-500 mt-1.5">
                RUCKUS ONE returns no property configuration for it, so there are no units,
                residents or resident-portal settings to report. This is a plain venue, not an
                MDU property — nothing here is missing or misconfigured.
              </p>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

/**
 * Activated SSIDs as a table rather than a card grid.
 *
 * A per-unit-SSID property puts hundreds of rows here, which a two-column card
 * grid turns into a scroll with no way to compare rows or find one. Headers
 * stick to the top of the scroll box so the columns stay readable at row 300,
 * and the filter covers SSID, network name, security, type and AP group.
 */
function SsidTable({ report, rows }: { report: any; rows: any[] }) {
  const [query, setQuery] = useState("");
  const [darkOnly, setDarkOnly] = useState(false);

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (darkOnly && row.apsBroadcasting) return false;
      if (!needle) return true;
      const hay = [row.ssid, row.name, row.security, row.type, row.networkId,
                   ...(row.vlans || []), ...(row.radios || []),
                   ...(row.scopes || []).map((s: any) => s.group)]
        .filter(Boolean).join(" ").toLowerCase();
      return hay.includes(needle);
    });
  }, [rows, query, darkOnly]);

  const dark = rows.filter((r) => !r.apsBroadcasting).length;

  if (!rows.length) {
    return (
      <Card id="wireless.ssids" title="SSIDs activated on this venue" icon={<Wifi size={17} className="text-gray-400" />}>
        <p className="text-sm text-red-700">No Wi-Fi network is activated on this venue.</p>
      </Card>
    );
  }

  return (
    <Card id="wireless.ssids" title={`SSIDs activated on this venue (${rows.length})`}
          icon={<Wifi size={17} className="text-gray-400" />}
          hint="Config on the left, live evidence on the right. An SSID with clients on it is working; a quiet one is only untested."
          right={
            <div className="flex items-center gap-2">
              {dark > 0 && (
                <button onClick={() => setDarkOnly((v) => !v)}
                        className={`text-xs px-2.5 py-1.5 rounded border ${
                          darkOnly ? "border-amber-400 bg-amber-50 text-amber-800"
                                   : "border-gray-300 hover:bg-gray-50 text-gray-600"}`}>
                  {darkOnly ? `Showing ${dark} not on air` : `${dark} not on air`}
                </button>
              )}
              <div className="relative">
                <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400" />
                <input value={query} onChange={(e) => setQuery(e.target.value)}
                       placeholder="Filter SSIDs…"
                       className="pl-7 pr-2 py-1.5 text-xs border border-gray-300 rounded w-48" />
              </div>
            </div>
          }>
      {report.wireless.unresolved > 0 && (
        <p className="mb-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2">
          {report.wireless.unresolved} activation(s) reference a network that is not in the
          tenant's network list, so their SSID, security and type are unknown — only the
          network id is available.
        </p>
      )}

      <div className="overflow-auto border border-gray-200 rounded" style={{ maxHeight: "34rem" }}>
        <table className="w-full text-sm">
          <thead className="bg-gray-50 sticky top-0 z-10 shadow-[inset_0_-1px_0_#e5e7eb]">
            <tr>
              {["SSID", "Type", "Security", "VLAN", "Radios", "AP groups",
                "On air", "Clients", "Flags"].map((header, i) => (
                <th key={header}
                    className={`text-left font-semibold text-gray-700 px-3 py-2 whitespace-nowrap ${
                      i >= 6 && i <= 7 ? "text-right" : ""}`}>
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row) => (
              <tr key={row.networkId}
                  className={`border-t border-gray-100 hover:bg-gray-50 ${
                    row.clientsNow ? "bg-green-50/30"
                    : row.apsBroadcasting ? "" : "bg-amber-50/30"}`}>
                <td className="px-3 py-1.5 max-w-[16rem]">
                  {row.resolved === false ? (
                    <>
                      <span className="text-gray-400 italic">no network definition</span>
                      <div className="text-[11px] text-gray-400 font-mono truncate"
                           title={row.networkId}>{row.networkId}</div>
                    </>
                  ) : (
                    <>
                      <div className="font-medium text-gray-900 truncate" title={row.ssid}>
                        {row.ssid}
                      </div>
                      {row.name && row.name !== row.ssid && (
                        <div className="text-[11px] text-gray-500 truncate" title={row.name}>
                          {row.name}
                        </div>
                      )}
                    </>
                  )}
                </td>
                <td className="px-3 py-1.5 text-gray-700 whitespace-nowrap">
                  {row.type || <span className="text-gray-300">—</span>}
                </td>
                <td className="px-3 py-1.5 whitespace-nowrap">
                  {row.security
                    ? <Pill tone={String(row.security).toUpperCase() === "OPEN" ? "amber" : "blue"}>
                        {row.security}
                      </Pill>
                    : <span className="text-gray-300">—</span>}
                </td>
                <td className="px-3 py-1.5 text-gray-700 whitespace-nowrap">
                  {row.vlans?.length ? row.vlans.join(", ")
                                     : <span className="text-gray-400">untagged</span>}
                </td>
                <td className="px-3 py-1.5 text-gray-700 whitespace-nowrap">
                  {row.radios?.length ? row.radios.join(", ")
                                      : <span className="text-gray-300">—</span>}
                </td>
                <td className="px-3 py-1.5 text-gray-600 max-w-[18rem]">
                  <span className="truncate block"
                        title={(row.scopes || []).map((s: any) => s.group).join(", ")}>
                    {row.allApGroups && <Pill tone="green">all</Pill>}{" "}
                    {(row.scopes || []).map((s: any) => s.group).join(", ")}
                  </span>
                </td>
                <td className={`px-3 py-1.5 text-right font-semibold whitespace-nowrap ${
                  row.apsBroadcasting ? "text-blue-700" : "text-amber-600"}`}
                    title={row.apsBroadcasting
                      ? `beaconed by ${row.apsBroadcasting} online AP(s)`
                      : "not beaconed by any online AP"}>
                  {row.apsBroadcasting}
                </td>
                <td className={`px-3 py-1.5 text-right font-semibold whitespace-nowrap ${
                  row.clientsNow ? "text-green-700" : "text-gray-400"}`}
                    title={row.apsCarrying ? `${row.apsCarrying} AP(s) carrying clients` : undefined}>
                  {row.clientsNow}
                </td>
                <td className="px-3 py-1.5 whitespace-nowrap">
                  <span className="flex flex-wrap gap-1">
                    {row.scheduled && <Pill tone="amber">scheduled</Pill>}
                    {row.enforced && <Pill>enforced</Pill>}
                    {row.captive && <Pill tone="amber">{row.captive}</Pill>}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {shown.length !== rows.length && (
        <p className="mt-2 text-xs text-gray-500">
          Showing {shown.length} of {rows.length} SSIDs.
        </p>
      )}
    </Card>
  );
}

function Wireless({ report }: { report: any }) {
  const rows: any[] = report.wireless.rows || [];
  return (
    <div className="space-y-4">
      <Section id="wireless.ssids">
        <div className="grid gap-3 sm:grid-cols-4">
          <Stat label="SSIDs here" value={report.wireless.activated} tone="blue" />
          <Stat label="Carrying clients" value={rows.filter((r) => r.clientsNow > 0).length}
                tone={rows.some((r) => r.clientsNow > 0) ? "green" : "amber"}
                sub="proof, not config" />
          <Stat label="AP groups" value={report.wireless.groups.length} />
          <Stat label="Clients now" value={fmtNum(report.clients.total)} tone="blue" />
        </div>
      </Section>

      <SsidTable report={report} rows={rows} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card id="wireless.clients-by-band" title="Clients by band" icon={<Radio size={17} className="text-gray-400" />}>
          <BarList rows={report.clients.byBand} limit={6} />
        </Card>
        <Card id="wireless.signal-quality" title="Signal quality" icon={<Radio size={17} className="text-gray-400" />}
              hint="RSSI of currently associated clients.">
          <BarList rows={report.clients.byRssi} limit={4} />
        </Card>
        <Card id="wireless.clients-per-ssid" title="Clients per SSID" icon={<Wifi size={17} className="text-gray-400" />}>
          <BarList rows={report.clients.bySsid} limit={8} />
        </Card>
        <Card id="wireless.connection-health" title="Connection health" hint="R1's own verdict per client.">
          <BarList rows={report.clients.byHealth} limit={4} />
        </Card>
        <Card id="wireless.busiest-aps" title="Busiest APs" icon={<Users size={17} className="text-gray-400" />}>
          <BarList rows={report.clients.topAps} limit={8} />
        </Card>
      </div>

      <Card id="wireless.channel-plan" title="Channel plan" icon={<Radio size={17} className="text-gray-400" />}
            hint="What the venue asks for, and what the online APs actually landed on.">
        <div className="grid gap-4 md:grid-cols-3">
          {(report.radios.bands || []).map((band: any) => {
            const configured = (report.venue.radio || []).find((entry: any) =>
              entry.band.replace(/[^0-9.]/g, "") === String(band.band).replace(/[^0-9.]/g, ""));
            return (
              <div key={band.band} className="border border-gray-200 rounded-lg p-3">
                <div className="flex items-baseline justify-between mb-2">
                  <span className="font-medium text-gray-900">{band.band}</span>
                  <span className="text-xs text-gray-500">{band.radios} radios</span>
                </div>
                {configured && (
                  <p className="text-xs text-gray-500 mb-2">
                    configured: {configured.width || "—"} · {configured.power || "—"} ·{" "}
                    {String(configured.method || "").toLowerCase().replace(/_/g, " ") || "—"}
                  </p>
                )}
                <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">
                  Channels in use
                  <span className="normal-case tracking-normal text-gray-400">
                    {" "}({band.channels.length})</span>
                </p>
                {/* No cap: a 6 GHz plan can spread radios over 30-odd channels
                    and the thin tail is exactly where a stray one shows up. */}
                <BarList rows={band.channels} limit={Infinity} />
                {!!band.widths.length && (
                  <p className="text-xs text-gray-500 mt-2">
                    widths: {band.widths.map((w: any) => `${w.label} ×${w.count}`).join(", ")}
                  </p>
                )}
              </div>
            );
          })}
          {!(report.radios.bands || []).length && (
            <p className="text-sm text-gray-500">No online AP reported a radio.</p>
          )}
        </div>
      </Card>

      <Card id="wireless.ap-groups" title="AP groups" hint="SSID scopes land on these.">
        <MiniTable
          columns={[
            { key: "name", header: "Group" },
            { key: "aps", header: "APs" },
            { key: "onlineAps", header: "Online" },
            { key: "ssids", header: "SSIDs here" },
          ]}
          rows={report.wireless.groups.map((group: any) => ({
            ...group,
            ssids: (report.wireless.perApGroup.find((entry: any) => entry.label === group.name) || {}).count || 0,
          }))}
        />
      </Card>
    </div>
  );
}

function Wired({ report }: { report: any }) {
  const poe = report.poe;
  const ports = report.ports;
  return (
    <div className="space-y-4">
      <Section id="wired.summary">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="PoE capacity" value={`${fmtNum(poe.capacityWatts, 1)} W`} sub="chassis budget" />
        <Stat label="Allocated" value={`${fmtNum(poe.allocatedWatts, 1)} W`}
              tone={(poe.allocatedPct || 0) >= 85 ? "amber" : "gray"} sub={pctText(poe.allocatedPct)} />
        <Stat label="Actually drawn" value={`${fmtNum(poe.drawWatts, 1)} W`} tone="blue"
              sub={`${poe.poweredPorts} powered ports`} />
        <Stat label="Ports up" value={`${fmtNum(ports.up)}/${fmtNum(ports.total)}`}
              tone={ports.erroredCount ? "amber" : "gray"}
              sub={ports.erroredCount ? `${ports.erroredCount} counting errors` : "no errors counted"} />
      </div>
      </Section>

      <Card id="wired.poe-budget" title="PoE budget per switch" icon={<Zap size={17} className="text-gray-400" />}
            hint="Allocated is power committed to attached devices; drawn is what they are actually pulling. A wide gap is class negotiation, not capacity.">
        {poe.switches.length ? (
          <div className="space-y-3">
            {poe.switches.map((row: any) => (
              <div key={row.name}>
                <Meter
                  pct={row.allocatedPct}
                  caption={`${row.name} · ${row.model || "?"} · ${row.poweredPorts} powered`}
                  right={`${fmtNum(row.allocatedWatts, 1)} W of ${fmtNum(row.capacityWatts, 1)} W (${pctText(row.allocatedPct)})`}
                />
                <div className="mt-1">
                  <Meter pct={row.drawPct} tone="blue"
                         caption="drawn" right={`${fmtNum(row.drawWatts, 1)} W`} />
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-500">No switch reported a PoE budget.</p>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card id="wired.poe-standard" title="PoE standard in use" hint="Per powered port.">
          <BarList rows={poe.byType} limit={6} />
        </Card>
        <Card id="wired.link-speeds" title="Link speeds" hint="Ports that are up.">
          <BarList rows={ports.bySpeed} limit={6} />
        </Card>
      </div>

      <Card id="wired.aps-on-ports" title="APs on switch ports" icon={<Zap size={17} className="text-gray-400" />}
            hint="Joined by LLDP — the port reports the AP it can see. Watts and port speed are blank where the switch is not in this tenant.">
        <MiniTable
          columns={[
            { key: "ap", header: "AP" },
            { key: "model", header: "Model" },
            { key: "switch", header: "Switch" },
            { key: "port", header: "Port" },
            { key: "watts", header: "Watts" },
            { key: "poeType", header: "PoE" },
            { key: "link", header: "AP link" },
            { key: "portSpeed", header: "Port speed" },
          ]}
          rows={(poe.apsOnPoe || []).map((row: any) => ({
            ...row,
            watts: row.watts ? `${row.watts} W` : null,
            port: row.portFound ? row.port : null,
          }))}
          empty="No AP reported an uplink."
        />
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card id="wired.top-poe-draws" title="Biggest PoE draws">
          <MiniTable
            columns={[
              { key: "switch", header: "Switch" },
              { key: "port", header: "Port" },
              { key: "watts", header: "W" },
              { key: "poeType", header: "Type" },
              { key: "ap", header: "AP" },
            ]}
            rows={poe.topConsumers || []}
            empty="No port is drawing power."
          />
        </Card>
        <Card id="wired.port-errors" title="Ports counting errors" hint="Up ports with CRC or interface errors — cabling and optics show up here first.">
          <MiniTable
            columns={[
              { key: "switch", header: "Switch" },
              { key: "port", header: "Port" },
              { key: "crc", header: "CRC" },
              { key: "inErr", header: "In" },
              { key: "outErr", header: "Out" },
              { key: "speed", header: "Speed" },
            ]}
            rows={ports.errored || []}
            empty="No up port is counting errors."
          />
        </Card>
      </div>

      <Card id="wired.vlans" title="VLANs seen in this venue" icon={<Network size={17} className="text-gray-400" />}
            hint="Where each VLAN is declared by configuration, and where it shows up in live traffic. The two are not the same thing.">
        {!report.vlans.portsKnown && (
          <p className="mb-3 text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded px-3 py-2">
            No switch ports were read for this venue, so the tagged and untagged
            columns are <strong>unknown</strong>, not zero — nothing was looked at.
          </p>
        )}
        {!!report.vlans.undeclaredWithClients && (
          <p className="mb-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2">
            {report.vlans.undeclaredWithClients} VLAN(s) carry clients but are declared
            by nothing in this venue — no SSID, DHCP pool, switch port or venue setting.
            On a DPSK or RADIUS site that is usually dynamic per-identity VLAN
            assignment, which this venue's own config cannot show.
          </p>
        )}
        <MiniTable
          maxHeight="26rem"
          columns={[
            { key: "vlan", header: "VLAN" },
            { key: "originCell", header: "Origin" },
            { key: "untaggedCell", header: "Untagged ports" },
            { key: "taggedCell", header: "Tagged ports" },
            { key: "ssidText", header: "SSIDs" },
            { key: "apsManagedOn", header: "APs managed" },
            { key: "clients", header: "Clients" },
            { key: "dhcpPool", header: "DHCP pool" },
            { key: "declaredText", header: "Declared by" },
          ]}
          rows={(report.vlans.rows || []).map((row: any) => ({
            ...row,
            vlan: row.isManagement ? `${row.vlan} (mgmt)` : row.vlan,
            originCell: row.origin === "configured"
              ? <Pill tone="green">configured</Pill>
              : <Pill tone={row.clients ? "amber" : "gray"}>undeclared</Pill>,
            // A zero here is only meaningful if ports were actually read.
            untaggedCell: report.vlans.portsKnown ? row.untaggedPorts : null,
            taggedCell: report.vlans.portsKnown ? row.taggedPorts : null,
            ssidText: row.ssids.join(", "),
            declaredText: (row.declaredBy || []).join(", "),
          }))}
          empty="No VLAN information was returned."
        />
      </Card>
    </div>
  );
}

const SUBNET_SOURCE: Record<string, { label: string; tone: string; title: string }> = {
  reported: { label: "reported", tone: "green", title: "The device reports this netmask itself — the prefix is a fact, not a guess." },
  inferred: { label: "inferred", tone: "blue", title: "Derived from devices sharing a default gateway: the smallest subnet that holds them all. The real subnet may be larger." },
  assumed: { label: "assumed /24", tone: "amber", title: "No netmask and no gateway were reported, so a /24 is assumed. Treat the prefix as a placeholder." },
};

function SubnetTable({ rows, noun }: { rows: any[]; noun: string }) {
  if (!rows?.length) return <p className="text-sm text-gray-400">No addresses reported.</p>;
  return (
    <MiniTable
      maxHeight="18rem"
      columns={[
        { key: "cidrCell", header: "Subnet" },
        { key: "sourceCell", header: "Prefix from" },
        { key: "count", header: noun, className: "text-right" },
        { key: "usageCell", header: "Of usable", className: "text-right" },
      ]}
      rows={rows.map((row) => {
        const source = SUBNET_SOURCE[row.source] || SUBNET_SOURCE.assumed;
        return {
          ...row,
          cidrCell: <span className="font-mono text-gray-900">{row.cidr}</span>,
          sourceCell: <span title={source.title}><Pill tone={source.tone}>{source.label}</Pill></span>,
          usageCell: row.usable
            ? <span className="text-gray-600">
                {row.count} / {fmtNum(row.usable)}
                <span className="text-gray-400"> ({pctText(row.utilisationPct)})</span>
              </span>
            : null,
        };
      })}
    />
  );
}

function Addressing({ report }: { report: any }) {
  const addressing = report.addressing;
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <Card id="addressing.ap-subnets" title="Where the APs landed" icon={<Network size={17} className="text-gray-400" />}
              hint="Subnets from each AP's own netmask where it reports one, otherwise inferred from a shared gateway.">
          <SubnetTable rows={addressing.apSubnets} noun="APs" />
          {!!addressing.apsWithoutIp && (
            <p className="text-sm text-amber-700 mt-3">
              {addressing.apsWithoutIp} online AP(s) report no address at all.
            </p>
          )}
        </Card>

        <Card id="addressing.external" title="How the site looks from outside" hint="The public address APs egress through.">
          {addressing.external.length ? (
            <div className="space-y-2">
              {addressing.external.map((row: any) => (
                <div key={row.ip} className="flex items-center justify-between gap-3 border border-gray-200 rounded p-3">
                  <div className="min-w-0">
                    {/* break-all, because an IPv6 literal offers the browser no
                        break opportunity of its own and would otherwise set the
                        width of this card — and through it, of the page. */}
                    <div className="font-mono text-lg text-gray-900 break-all">{row.ip}</div>
                    <div className="text-xs text-gray-500">
                      {row.private ? "private address — the APs sit behind another NAT" : "public address"}
                    </div>
                  </div>
                  <Pill tone={row.private ? "amber" : "green"}>{row.count} APs</Pill>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-500">No AP reported an external address.</p>
          )}
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card id="addressing.switch-subnets" title="Switch subnets"><SubnetTable rows={addressing.switchSubnets} noun="switches" /></Card>
        <Card id="addressing.gateways" title="Gateways"><BarList rows={addressing.gateways} limit={6} /></Card>
        <Card id="addressing.dns" title="DNS servers"><BarList rows={addressing.dns} limit={6} /></Card>
      </div>

      <Card id="addressing.dhcp-pools" title="DHCP pools" icon={<Network size={17} className="text-gray-400" />}
            hint="R1-managed pools on this venue, with how full they are.">
        {addressing.dhcpPools.length ? (
          <div className="space-y-4">
            {addressing.dhcpPools.map((pool: any) => (
              <div key={pool.name} className="border border-gray-200 rounded p-3">
                <div className="flex flex-wrap items-center gap-2 mb-2">
                  <span className="font-medium text-gray-900 break-words min-w-0">{pool.name}</span>
                  {pool.vlan !== null && pool.vlan !== undefined && <Pill>VLAN {pool.vlan}</Pill>}
                  <Pill tone={pool.active ? "green" : "gray"}>{pool.active ? "active" : "inactive"}</Pill>
                  <span className="text-xs text-gray-500 font-mono break-all min-w-0">
                    {pool.subnet}/{pool.mask} · {pool.start}–{pool.end}
                  </span>
                </div>
                <Meter pct={pool.pct} caption={`${fmtNum(pool.used)} of ${fmtNum(pool.total)} addresses`} />
                <div className="text-xs text-gray-500 mt-1">
                  lease {pool.leaseHours ?? "—"}h · DNS {pool.dns.join(", ") || "—"}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-500">This venue runs no R1-managed DHCP pool.</p>
        )}
      </Card>

    </div>
  );
}


/**
 * DPSK / identity summary.
 *
 * Counts and configuration only. Passphrases, resident names, emails, phone
 * numbers and device MACs are never fetched into this payload — the backend
 * builds it from an allowlist and raises rather than emit a forbidden key
 * (shape._dpsk_safe), so there is nothing sensitive here to render.
 */
function Dpsk({ report }: { report: any }) {
  const dpsk = report.dpsk || {};
  const pools: any[] = dpsk.pools || [];

  if (!dpsk.inUse) {
    return (
      <div className="space-y-4">
        <Card id="identity.dpsk-summary" title="DPSK" icon={<Key size={17} className="text-gray-400" />}>
          <p className="text-sm text-gray-700">
            No DPSK pool backs any SSID activated on this venue — DPSK is not in use here.
          </p>
          <p className="text-xs text-gray-500 mt-2">
            {dpsk.poolsOnTenant || 0} DPSK pool(s) and {dpsk.identityGroupsOnTenant || 0} identity
            group(s) exist on the tenant. None of them backs an SSID activated on this venue, and
            no identity group names this venue as its property.
          </p>
        </Card>
        <PolicyChain report={report} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Section id="identity.dpsk-summary">
      <div className="grid gap-3 sm:grid-cols-4">
        <Stat label="DPSK pools" value={dpsk.poolCount} tone="blue" sub="used by this venue" />
        <Stat label="Passphrases" value={fmtNum(dpsk.passphraseTotal)} tone="blue"
              sub={dpsk.passphraseCountsKnown ? "across those pools" : "partial — a count failed"} />
        <Stat label="Identities" value={fmtNum(dpsk.identityTotal)}
              tone={dpsk.poolsWithUnresolvedGroups ? "gray" : "green"}
              sub={dpsk.poolsWithUnresolvedGroups
                ? `partial — ${dpsk.poolsWithUnresolvedGroups} pool(s) unresolved`
                : "in the linked groups"} />
        <Stat label="DPSK SSIDs" value={(dpsk.dpskSsids || []).length} sub="backed by a pool" />
      </div>
      </Section>

      {/* Stays with the DPSK cards rather than the summary tiles: it is a
          statement about what the identity data below contains, and it would
          be stranded — and misleading — above a tab whose pools are hidden. */}
      <Section id="identity.dpsk-pools">
      <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 flex items-start gap-2">
        <ShieldCheck size={15} className="text-gray-400 mt-0.5 shrink-0" />
        <p className="text-xs text-gray-600">
          Counts and settings only. Passphrases, resident names, email addresses, phone
          numbers and device MAC addresses are never read into this report.
        </p>
      </div>
      </Section>

      {pools.map((pool) => (
        <Card key={pool.id} id="identity.dpsk-pools" title={pool.name || "Unnamed pool"}
              icon={<Key size={17} className="text-gray-400" />}
              hint={(pool.networksHere
                      ? `Backs ${pool.networksHere} SSID(s) activated here`
                      : "Configured for this property — backs no SSID activated here yet") +
                    (pool.networksTotal !== pool.networksHere
                      ? ` · ${pool.networksTotal} network(s) tenant-wide` : "") +
                    (pool.linkedBy?.length ? ` · linked by ${pool.linkedBy.join(" + ")}` : "")}>
          <div className="grid gap-3 sm:grid-cols-4 mb-4">
            <Stat label="Passphrases"
                  value={pool.passphraseCount === null ? "—" : fmtNum(pool.passphraseCount)}
                  tone={pool.passphraseCount === 0 ? "red" : "blue"}
                  sub={pool.passphraseCount === 0 ? "pool cannot admit anyone" : undefined} />
            <Stat label="Identities" value={fmtNum(pool.identityCount)}
                  tone={pool.identityCount ? "green" : "amber"} />
            <Stat label="SSIDs here" value={pool.networksHere} />
            <Stat label="Devices / key"
                  value={pool.deviceLimitPerPassphrase ?? "—"}
                  sub={pool.deviceLimitPerPassphrase ? "limit" : "no limit set"} />
          </div>

          <dl className="grid grid-cols-2 gap-y-2 text-sm mb-4">
            <dt className="text-gray-500">Passphrase format</dt>
            <dd className="text-gray-900">
              {pool.passphraseFormat || "—"}
              {pool.passphraseLength ? ` · length ${pool.passphraseLength}` : ""}
              {pool.wordCount ? ` · ${pool.wordCount} words` : ""}
              {pool.numericSuffix ? " · numeric suffix" : ""}
            </dd>
            <dt className="text-gray-500">Expiration</dt>
            <dd className="text-gray-900">
              {pool.expirationType || "none set"}
              {pool.expirationDate ? ` · ${pool.expirationDate}` : ""}
              {pool.expirationOffset ? ` · offset ${pool.expirationOffset}` : ""}
            </dd>
            <dt className="text-gray-500">Default access</dt>
            <dd className="text-gray-900">
              {pool.policyDefaultAccess === null || pool.policyDefaultAccess === undefined
                ? "—" : pool.policyDefaultAccess ? "allow" : "deny"}
              {pool.hasPolicySet ? " · policy set attached" : ""}
            </dd>
            <dt className="text-gray-500">Notifications</dt>
            <dd className="text-gray-900">{pool.autoNotifications ? "automatic" : "off"}</dd>
          </dl>

          {!(pool.identityGroups || []).length && (
            <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded px-3 py-2 mb-3">
              <strong>Orphaned pool.</strong> No identity group references this pool,
              against a complete list of the tenant's groups. RUCKUS ONE will not let you
              create a pool without one, so the group was deleted afterwards — the pool
              still reports <code>isReferenced=true</code>, the stale guard that should
              have prevented it. Its passphrases cannot be administered through a group.
            </p>
          )}
          {!!(pool.identityGroups || []).length && (
            <>
              <p className="text-xs uppercase tracking-wide text-gray-500 mb-2">Identity groups</p>
              <MiniTable
                maxHeight="14rem"
                columns={[
                  { key: "name", header: "Group" },
                  { key: "identityCount", header: "Identities" },
                  { key: "networkCount", header: "Networks" },
                  { key: "scope", header: "Scope" },
                  { key: "cleanup", header: "Inactive cleanup" },
                ]}
                rows={(pool.identityGroups || []).map((g: any) => ({
                  ...g,
                  scope: g.isProperty ? "property" : "tenant",
                  cleanup: g.autoCleanup
                    ? `after ${g.inactiveAfterDays ?? "?"} days` : "off",
                }))}
              />
            </>
          )}

          {!!(pool.ssidsHere || []).length && (
            <p className="mt-3 text-xs text-gray-500">
              <span className="text-gray-600 font-medium">SSIDs on this venue: </span>
              {pool.ssidsHere.slice(0, 12).join(", ")}
              {pool.ssidsHere.length > 12 ? ` … +${pool.ssidsHere.length - 12} more` : ""}
            </p>
          )}
        </Card>
      ))}

      {!!(dpsk.otherIdentityGroups || []).length && (
        <Card id="identity.other-groups" title="Other identity groups on this property"
              icon={<Users size={17} className="text-gray-400" />}
              hint="Attached to this property but not to a DPSK pool used here — MAC registration or certificate groups.">
          <MiniTable
            columns={[
              { key: "name", header: "Group" },
              { key: "identityCount", header: "Identities" },
              { key: "backing", header: "Backed by" },
            ]}
            rows={(dpsk.otherIdentityGroups || []).map((g: any) => ({
              ...g,
              backing: [g.hasDpskPool && "DPSK pool",
                        g.hasMacRegistrationPool && "MAC registration",
                        g.hasCertificateTemplate && "certificate template"]
                .filter(Boolean).join(", ") || "nothing",
            }))}
          />
        </Card>
      )}

      <PolicyChain report={report} />
    </div>
  );
}

/**
 * The adaptive policy chain: policy set -> policies (in priority order) ->
 * RADIUS attribute group -> rate limits.
 *
 * Scoped to the sets this venue's DPSK pools and identity groups point at, so
 * a tenant-wide policy library does not drown one site.
 */
function PolicyChain({ report }: { report: any }) {
  const policy = report.policy || {};
  const sets: any[] = policy.sets || [];

  if (!policy.inUse) {
    return (
      <Card id="identity.policy-sets" title="Adaptive policy" icon={<ShieldCheck size={17} className="text-gray-400" />}>
        <p className="text-sm text-gray-700">
          No adaptive policy set is attached to this venue's DPSK pools or identity groups.
        </p>
        <p className="text-xs text-gray-500 mt-2">
          {policy.setsOnTenant || 0} policy set(s), {policy.policiesOnTenant || 0} policy/policies
          and {policy.radiusGroupsOnTenant || 0} RADIUS attribute group(s) exist on the tenant,
          none reachable from this venue.
        </p>
      </Card>
    );
  }

  return (
    <>
      {sets.map((set) => (
        <Card key={set.id} id="identity.policy-sets" title={`Policy set — ${set.name}`}
              icon={<ShieldCheck size={17} className="text-gray-400" />}
              hint={(set.assignedTo?.length ? `Assigned to ${set.assignedTo.join(", ")}` : "Not assigned")
                    + ` · ${set.policies?.length ?? 0} policy/policies, evaluated in priority order`}>
          <MiniTable
            maxHeight="22rem"
            columns={[
              { key: "priority", header: "Priority" },
              { key: "policy", header: "Policy" },
              { key: "policyType", header: "Type" },
              { key: "conditions", header: "Conditions" },
              { key: "radiusCell", header: "RADIUS attribute group" },
              { key: "rateCell", header: "Rate limit" },
            ]}
            rows={(set.policies || []).map((row: any) => ({
              ...row,
              radiusCell: row.radiusGroupMissing
                ? <span className="text-red-700">missing — group deleted</span>
                : row.radiusGroup,
              rateCell: (row.rateLimits || []).some((r: any) => r.mbps)
                ? (row.rateLimits || []).filter((r: any) => r.mbps)
                    .map((r: any) => `${r.mbps} Mbps`).join(" / ")
                : null,
            }))}
            empty="This set has no policies."
          />
          {!!set.unresolvedPolicyIds?.length && (
            <p className="mt-2 text-xs text-amber-800">
              {set.unresolvedPolicyIds.length} policy id(s) in this set no longer resolve to a
              policy.
            </p>
          )}
        </Card>
      ))}

      <Card id="identity.radius" title="RADIUS attribute groups" icon={<ShieldCheck size={17} className="text-gray-400" />}
            hint="The rate tiers policies hand back on match. Policy counts come from each policy's own onMatchResponse — the group's assignments list paginates and cannot be counted.">
        <MiniTable
          columns={[
            { key: "name", header: "Group" },
            { key: "description", header: "Description" },
            { key: "rateCell", header: "Rate limit" },
            { key: "policyCount", header: "Policies" },
            { key: "orphanCell", header: "Stale assignments" },
          ]}
          rows={(policy.radiusGroups || []).map((row: any) => ({
            ...row,
            rateCell: (row.rateLimits || []).filter((r: any) => r.mbps)
              .map((r: any) => `${r.mbps} Mbps`).join(" / ") || null,
            orphanCell: row.orphanedAssignments
              ? <span className="text-amber-800">{row.orphanedAssignments} — group is pinned</span>
              : null,
          }))}
          empty="No RADIUS attribute group is reachable from this venue."
        />
      </Card>
    </>
  );
}

function Devices({ report, filter, onFilter }: {
  report: any; filter: string; onFilter: (value: string) => void;
}) {
  // Defaults to everything that is NOT online: a site review is about what is
  // wrong, and on a healthy venue the operational rows are the ones you scroll
  // past. "All" is one click away.
  const [state, setState] = useState<"attention" | "online" | "offline" | "other" | "all">("attention");

  const needle = filter.trim().toLowerCase();
  const textMatch = (row: any) =>
    !needle || Object.values(row).join(" ").toLowerCase().includes(needle);
  const stateMatch = (row: any) =>
    state === "all" ? true
    : state === "attention" ? row.state !== "online"
    : row.state === state;
  const match = (row: any) => textMatch(row) && stateMatch(row);

  const allAps = report.inventory.rows.aps;
  const allSwitches = report.inventory.rows.switches;
  const aps = allAps.filter(match);
  const switches = allSwitches.filter(match);

  const tally = (rows: any[], key: string) =>
    key === "all" ? rows.length
    : key === "attention" ? rows.filter((r: any) => r.state !== "online").length
    : rows.filter((r: any) => r.state === key).length;

  const FILTERS = [
    { key: "attention", label: "Needs attention" },
    { key: "offline", label: "Offline" },
    { key: "other", label: "Other" },
    { key: "online", label: "Online" },
    { key: "all", label: "All" },
  ] as const;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative flex-1 min-w-[16rem]">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input value={filter} onChange={(e) => onFilter(e.target.value)}
                 placeholder="Filter devices by name, serial, model, IP…"
                 className="w-full pl-9 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent" />
        </div>
        {/* flex-wrap, not overflow-x-auto: these are the only way to reach
            the offline and needs-attention lists, and a filter you have to
            discover by scrolling sideways is a filter most people never find.
            Five of them do not fit on a 320px phone in one row. */}
        <div className="flex flex-wrap items-center gap-1 bg-gray-100 rounded-lg p-0.5">
          {FILTERS.map((entry) => {
            const count = tally(allAps, entry.key) + tally(allSwitches, entry.key);
            return (
              <button key={entry.key} onClick={() => setState(entry.key)}
                      className={`px-2.5 py-1.5 text-xs font-medium rounded-md transition ${
                        state === entry.key ? "bg-white shadow text-gray-900"
                                            : "text-gray-600 hover:text-gray-900"}`}>
                {entry.label}
                <span className={`ml-1.5 ${
                  entry.key === "attention" && count ? "text-red-600 font-semibold" : "text-gray-400"}`}>
                  {count}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {state === "attention" && !aps.length && !switches.length && (
        <p className="text-sm text-green-700 bg-green-50 border border-green-200 rounded px-3 py-2">
          Every AP and switch at this venue is online
          {needle ? " that matches the current text filter" : ""}. Choose “All” to see them.
        </p>
      )}

      <Card id="devices.aps" title={`Access points (${aps.length})`} icon={<Wifi size={17} className="text-gray-400" />}
            hint="Full per-AP detail including addressing — scroll sideways for the rest of the columns.">
        <MiniTable
          maxHeight="30rem"
          columns={[
            { key: "statusPill", header: "Status", className: "whitespace-nowrap" },
            { key: "name", header: "Name", className: "whitespace-nowrap" },
            { key: "model", header: "Model", className: "whitespace-nowrap" },
            { key: "serial", header: "Serial", className: "whitespace-nowrap" },
            { key: "ip", header: "IP", className: "whitespace-nowrap" },
            { key: "netmask", header: "Mask", className: "whitespace-nowrap" },
            { key: "gateway", header: "Gateway", className: "whitespace-nowrap" },
            { key: "dns", header: "DNS", className: "whitespace-nowrap" },
            { key: "assignment", header: "Assigned", className: "whitespace-nowrap" },
            { key: "externalIp", header: "External IP", className: "whitespace-nowrap" },
            { key: "firmware", header: "Firmware", className: "whitespace-nowrap" },
            { key: "apGroup", header: "AP group", className: "whitespace-nowrap" },
            { key: "mgmtVlan", header: "Mgmt VLAN", className: "whitespace-nowrap" },
            { key: "clients", header: "Clients", className: "whitespace-nowrap" },
            { key: "uplinkStatus", header: "Uplink", className: "whitespace-nowrap" },
            { key: "ssidCount", header: "SSIDs on air", className: "whitespace-nowrap" },
          ]}
          rows={aps.map((row: any) => ({
            ...row,
            statusPill: <StatePill state={row.state} status={row.status} />,
            ssidCount: row.ssidsBroadcast?.length || 0,
          }))}
          empty="No AP matches."
        />
      </Card>

      <Card id="devices.switches" title={`Switches (${switches.length})`} icon={<Cable size={17} className="text-gray-400" />}
            hint="Full per-switch detail including management addressing — scroll sideways for the rest.">
        <MiniTable
          maxHeight="24rem"
          columns={[
            { key: "statusPill", header: "Status", className: "whitespace-nowrap" },
            { key: "name", header: "Name", className: "whitespace-nowrap" },
            { key: "model", header: "Model", className: "whitespace-nowrap" },
            { key: "serial", header: "Serial", className: "whitespace-nowrap" },
            { key: "ip", header: "IP", className: "whitespace-nowrap" },
            { key: "mask", header: "Mask", className: "whitespace-nowrap" },
            { key: "gateway", header: "Gateway", className: "whitespace-nowrap" },
            { key: "dnsText", header: "DNS", className: "whitespace-nowrap" },
            { key: "assignment", header: "Assigned", className: "whitespace-nowrap" },
            { key: "firmware", header: "Firmware", className: "whitespace-nowrap" },
            { key: "ports", header: "Ports", className: "whitespace-nowrap" },
            { key: "clients", header: "Clients", className: "whitespace-nowrap" },
            { key: "uptime", header: "Uptime", className: "whitespace-nowrap" },
          ]}
          rows={switches.map((row: any) => ({
            ...row,
            statusPill: <StatePill state={row.state} status={row.status} />,
            // Switch DNS arrives as a list on some models and a comma string
            // on others; MiniTable renders values verbatim, so flatten here.
            dnsText: Array.isArray(row.dns) ? row.dns.join(", ") : row.dns,
          }))}
          empty="No switch matches."
        />
      </Card>
    </div>
  );
}

function Sources({ meta }: { meta: any }) {
  const [open, setOpen] = useState(false);
  const errors: Record<string, string> = meta?.errors || {};
  const errorKeys = Object.keys(errors);

  return (
    <div className="mt-4">
      {!!errorKeys.length && (
        <div className="border border-amber-300 bg-amber-50 rounded-lg p-3 mb-3 text-sm text-amber-900">
          <p className="font-medium mb-1">This report is incomplete.</p>
          <ul className="list-disc list-inside space-y-0.5">
            {errorKeys.map((key) => <li key={key}><span className="font-mono">{key}</span>: {errors[key]}</li>)}
          </ul>
        </div>
      )}
      <button onClick={() => setOpen(!open)} className="text-xs text-gray-500 hover:text-gray-800">
        {open ? "Hide" : "Show"} what PISR read ({meta?.sources?.length || 0} endpoints, all read-only)
      </button>
      {open && (
        <div className="mt-2 border border-gray-200 rounded-lg p-3 bg-gray-50">
          <ul className="text-xs font-mono text-gray-600 space-y-0.5">
            {(meta?.sources || []).map((source: string) => <li key={source}>{source}</li>)}
          </ul>
          <p className="text-xs text-gray-500 mt-2">
            {fmtNum(meta?.counts?.aps)} APs · {fmtNum(meta?.counts?.switches)} switches ·{" "}
            {fmtNum(meta?.counts?.ports)} ports · {fmtNum(meta?.counts?.clients)} clients ·{" "}
            {fmtNum(meta?.counts?.networks)} networks, in {meta?.elapsedSeconds}s.
          </p>
        </div>
      )}
    </div>
  );
}

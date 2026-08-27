import {
  createContext, useCallback, useContext, useEffect, useState, type ReactNode,
} from "react";
import { Lock, LogOut, ShieldAlert } from "lucide-react";
import { UNAUTHENTICATED_EVENT } from "@/utils/api";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

export interface ControllerRow {
  id: number;
  name: string;
  controller_type: string;
  controller_subtype: string | null;
  r1_tenant_id: string | null;
  r1_region: string | null;
}

interface AuthContextType {
  activeControllerId: number | null;
  activeControllerName: string | null;
  activeControllerType: string | null;
  activeControllerSubtype: string | null;
  controllers: ControllerRow[];
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
};

interface AuthStatus {
  mode: "passphrase" | "proxy" | "disabled";
  required: boolean;
  authenticated: boolean;
  user: string | null;
  logoutUrl: string | null;
}

/**
 * The passphrase prompt.
 *
 * Deliberately says nothing about the tenant. An unauthenticated caller gets
 * the SPA bundle — it has to load in order to render this — and the bundle
 * contains no tenant data, so this screen is all they see. /api/config, which
 * does name the tenant, is behind the gate with everything else.
 */
function LoginScreen({ onSignedIn }: { onSignedIn: () => void }) {
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !passphrase) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ passphrase }),
      });
      if (res.ok) {
        setPassphrase("");
        onSignedIn();
        return;
      }
      const body = await res.json().catch(() => null);
      setError(body?.detail || `Sign-in failed (HTTP ${res.status}).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 p-8">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-gray-200 bg-white p-6 shadow-sm"
      >
        <div className="flex items-center gap-2 text-gray-900">
          <Lock size={18} className="text-gray-400" />
          <h1 className="font-semibold">PISR</h1>
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Property Install Status Report
        </p>

        <label
          htmlFor="pisr-passphrase"
          className="mt-6 block text-sm font-medium text-gray-700"
        >
          Passphrase
        </label>
        <input
          id="pisr-passphrase"
          type="password"
          autoFocus
          autoComplete="current-password"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          className="mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm
                     focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />

        {error && (
          <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || !passphrase}
          className="mt-4 w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white
                     hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

/**
 * Proxy mode, with no identity forwarded.
 *
 * A passphrase form would be worse than useless here — there is no passphrase
 * to accept, and offering one invites someone to hunt for a password that does
 * not exist. The only way in is back through the proxy, so say that.
 */
function ProxyDeadEnd() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 p-8">
      <div className="max-w-md rounded-lg border border-amber-200 bg-amber-50 p-6">
        <div className="flex items-center gap-2">
          <ShieldAlert size={18} className="text-amber-600" />
          <h1 className="font-semibold text-amber-900">Not signed in</h1>
        </div>
        <p className="mt-2 text-sm text-amber-800">
          This PISR instance authenticates through your organisation's single
          sign-on, and no identity arrived with this request.
        </p>
        <p className="mt-3 text-sm text-amber-800">
          Reach it through the usual address rather than directly, and sign in
          when your identity provider asks. If you already did, the proxy in
          front of PISR may not be forwarding the identity header — its logs
          will say.
        </p>
      </div>
    </div>
  );
}

/**
 * There are no user accounts in standalone PISR.
 *
 * This shim exists so that PISR.tsx can be carried across from rtools2
 * unmodified. There, it read its tenant identity out of a login session via
 * useAuth(); here the same hook is backed by .env, fetched once from
 * /api/config. The five keys below are exactly what PISR.tsx destructures.
 *
 * What this provider added on top of the original shim is the gate: one shared
 * passphrase in exchange for a signed HttpOnly session cookie, checked by
 * api/auth.py on every /api request. The cookie is not readable from JavaScript
 * by design, so the only way to know whether a session exists is to ask —
 * hence the /api/auth/status call before the /api/config one.
 *
 * Do not replace the /api/config call with build-time VITE_* variables.
 * Reading it at runtime is what lets one built image serve any tenant, and what
 * makes switching R1_EC_TYPE a container restart instead of a rebuild.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [controller, setController] = useState<ControllerRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setError(null);
    setStatus(null);
    setController(null);
  }, []);

  // A 401 from anywhere in the app — a session that expired mid-report, or a
  // passphrase rotated on the server while this tab sat open — drops straight
  // back to the form instead of leaving a dead page behind an error banner.
  useEffect(() => {
    const onUnauthenticated = () => {
      setController(null);
      // Re-ask rather than assuming: which screen belongs here depends on the
      // mode, and only the server knows it.
      setStatus(null);
    };
    window.addEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
    return () => window.removeEventListener(UNAUTHENTICATED_EVENT, onUnauthenticated);
  }, []);

  useEffect(() => {
    if (status !== null) return;
    let cancelled = false;
    fetch(`${API_BASE_URL}/auth/status`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((s: AuthStatus) => { if (!cancelled) setStatus(s); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [status]);

  useEffect(() => {
    if (!status?.authenticated || controller) return;
    let cancelled = false;
    fetch(`${API_BASE_URL}/config`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((row: ControllerRow) => { if (!cancelled) setController(row); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [status, controller]);

  const signOut = async () => {
    // In proxy mode PISR holds no session to end — the SSO cookie belongs to
    // the proxy. Hand off to its sign-out URL if the operator configured one.
    if (status?.mode === "proxy") {
      if (status.logoutUrl) window.location.href = status.logoutUrl;
      return;
    }
    await fetch(`${API_BASE_URL}/logout`, {
      method: "POST",
      credentials: "same-origin",
    }).catch(() => undefined);
    setController(null);
    setStatus(null);
  };

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-8">
        <div className="max-w-lg rounded-lg border border-red-200 bg-red-50 p-6">
          <h1 className="font-semibold text-red-800">
            Could not read the PISR configuration
          </h1>
          <p className="mt-2 text-sm text-red-700">{error}</p>
          <p className="mt-3 text-sm text-red-700">
            The API did not answer <code>/api/config</code>. If the container
            just started, check its logs — a missing or invalid value in{" "}
            <code>.env</code> stops it from booting at all.
          </p>
          <button
            onClick={refresh}
            className="mt-4 rounded-md border border-red-300 bg-white px-3 py-1.5
                       text-sm font-medium text-red-700 hover:bg-red-100"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (status && status.required && !status.authenticated) {
    return status.mode === "proxy"
      ? <ProxyDeadEnd />
      : <LoginScreen onSignedIn={refresh} />;
  }

  // Gate rendering on the fetch. PISR.tsx shows a "pick a RUCKUS ONE
  // controller" panel whenever activeControllerId is null, and without this
  // gate that panel flashes for one frame on every page load.
  if (!controller) {
    return (
      <div className="min-h-screen flex items-center justify-center p-8 text-gray-500">
        Loading configuration…
      </div>
    );
  }

  return (
    <AuthContext.Provider
      value={{
        activeControllerId: controller.id,
        activeControllerName: controller.name,
        activeControllerType: controller.controller_type,
        activeControllerSubtype: controller.controller_subtype,
        controllers: [controller],
      }}
    >
      {children}
      {/*
        Rendered here rather than in PISR.tsx's header: that file is kept
        byte-identical to its rtools2 original so upstream changes stay a
        readable diff. Fixed to the corner so it lands on top of whichever tab
        is showing, and hidden entirely when there is no gate to sign out of.
      */}
      {status?.required && (
        (() => {
          // In proxy mode the button is only a button if there is somewhere to
          // send them; otherwise it is a label saying who PISR thinks you are.
          const canSignOut =
            status.mode !== "proxy" || Boolean(status.logoutUrl);
          const shell =
            "fixed bottom-4 right-4 z-50 flex items-center gap-1.5 rounded-full " +
            "border border-gray-300 bg-white/90 px-3 py-1.5 text-xs font-medium " +
            "text-gray-600 shadow-sm backdrop-blur";
          return canSignOut ? (
            <button
              onClick={signOut}
              title={status.user ? `Signed in as ${status.user}` : "Sign out of PISR"}
              className={`${shell} hover:bg-white hover:text-gray-900`}
            >
              <LogOut size={13} />
              {status.user ? `Sign out · ${status.user}` : "Sign out"}
            </button>
          ) : (
            <span className={shell} title="Signed in via single sign-on">
              <Lock size={13} />
              {status.user}
            </span>
          );
        })()
      )}
    </AuthContext.Provider>
  );
}

import {
  createContext, useCallback, useContext, useEffect, useState, type ReactNode,
} from "react";
import { Lock, LogOut, ShieldAlert, SlidersHorizontal, Target, Users } from "lucide-react";
import { UNAUTHENTICATED_EVENT } from "@/utils/api";
import AdminVisibility from "@/pages/AdminVisibility";
import AdminAccounts from "@/pages/AdminAccounts";
import AdminBaseline from "@/pages/AdminBaseline";

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
  /** See AuthStatus.role — presentation only. */
  role: "admin" | "user" | null;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
};

interface AuthStatus {
  mode: "passphrase" | "proxy" | "accounts" | "disabled";
  required: boolean;
  authenticated: boolean;
  user: string | null;
  /** accounts mode only: no accounts exist yet, so nobody can sign in. */
  setupNeeded?: boolean;
  /** accounts mode only: whether a break-glass passphrase is configured. */
  breakGlass?: boolean;
  /**
   * "admin" or "user", or null when not signed in.
   *
   * Used only to decide what to RENDER — the admin portal button, and which
   * cards to draw. It is not a control and must never be treated as one: this
   * bundle is served unauthenticated by design, so anyone can read what it
   * would do with a different value. The server filters the report before it
   * is sent and `require_admin` guards the portal's routes; both of those hold
   * whatever this says.
   */
  role: "admin" | "user" | null;
  logoutUrl: string | null;
}

const FIELD =
  "mt-1 w-full rounded-md border border-gray-300 px-3 py-2 text-sm " +
  "focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500";

const SUBMIT =
  "mt-4 w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white " +
  "hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300";

/** The card every signed-out screen sits in. */
function AuthCard({ children, onSubmit }: {
  children: ReactNode;
  onSubmit?: (e: React.FormEvent) => void;
}) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 p-8">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm min-w-0 rounded-lg border border-gray-200 bg-white p-6 shadow-sm"
      >
        <div className="flex items-center gap-2 text-gray-900">
          <Lock size={18} className="text-gray-400 shrink-0" />
          <h1 className="font-semibold">PISR</h1>
        </div>
        <p className="mt-1 text-sm text-gray-500">
          Property Install Status Report
        </p>
        {children}
      </form>
    </div>
  );
}

/**
 * The sign-in prompt, for both modes that have one.
 *
 * Deliberately says nothing about the tenant. An unauthenticated caller gets
 * the SPA bundle — it has to load in order to render this — and the bundle
 * contains no tenant data, so this screen is all they see. /api/config, which
 * does name the tenant, is behind the gate with everything else.
 *
 * In ACCOUNTS mode it asks for a username and password; in passphrase mode,
 * one passphrase. The break-glass passphrase gets a link rather than a third
 * field, because it is a recovery door and a form that offers it as an equal
 * option invites people to use it as one — which throws away the audit trail
 * and the revocation that accounts mode exists for.
 */
function LoginScreen({ status, onSignedIn }: {
  status: AuthStatus | null;
  onSignedIn: () => void;
}) {
  const accounts = status?.mode === "accounts";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [breakGlass, setBreakGlass] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const usingPassphrase = !accounts || breakGlass;
  const ready = usingPassphrase ? Boolean(passphrase) : Boolean(username && password);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(
          usingPassphrase ? { passphrase } : { username, password }),
      });
      if (res.ok) {
        setPassphrase("");
        setPassword("");
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
    <AuthCard onSubmit={submit}>
      {accounts && status?.setupNeeded && (
        <p className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2
                      text-xs text-amber-900">
          <b>No accounts exist yet</b>, so nobody can sign in. Create the first
          administrator on the box:
          <code className="mt-1 block break-all text-[11px]">
            docker compose run --rm pisr python scripts/pisr_admin.py add-user
            &lt;name&gt; --admin
          </code>
        </p>
      )}

      {!usingPassphrase && (
        <>
          <label htmlFor="pisr-username"
                 className="mt-6 block text-sm font-medium text-gray-700">
            Username
          </label>
          <input id="pisr-username" autoFocus autoComplete="username"
                 value={username}
                 onChange={(e) => setUsername(e.target.value)}
                 className={FIELD} />

          <label htmlFor="pisr-password"
                 className="mt-4 block text-sm font-medium text-gray-700">
            Password
          </label>
          <input id="pisr-password" type="password" autoComplete="current-password"
                 value={password}
                 onChange={(e) => setPassword(e.target.value)}
                 className={FIELD} />
        </>
      )}

      {usingPassphrase && (
        <>
          <label htmlFor="pisr-passphrase"
                 className="mt-6 block text-sm font-medium text-gray-700">
            {breakGlass ? "Break-glass passphrase" : "Passphrase"}
          </label>
          <input id="pisr-passphrase" type="password" autoFocus
                 autoComplete="current-password"
                 value={passphrase}
                 onChange={(e) => setPassphrase(e.target.value)}
                 className={FIELD} />
          {breakGlass && (
            <p className="mt-2 text-xs text-gray-500">
              This is the recovery door. It signs in as an administrator without
              an account, so it leaves no name in the audit trail — use it to
              get back in and fix things, not day to day.
            </p>
          )}
        </>
      )}

      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      <button type="submit" disabled={busy || !ready} className={SUBMIT}>
        {busy ? "Signing in…" : "Sign in"}
      </button>

      {accounts && status?.breakGlass && (
        <button
          type="button"
          onClick={() => { setBreakGlass(!breakGlass); setError(null); }}
          className="mt-3 w-full text-center text-xs text-gray-500 hover:text-gray-800"
        >
          {breakGlass ? "Sign in with an account instead"
                      : "Use the break-glass passphrase"}
        </button>
      )}

      {accounts && !breakGlass && (
        <p className="mt-4 border-t border-gray-100 pt-3 text-xs text-gray-500">
          Forgotten your password? Ask an administrator to send you a reset
          link — PISR cannot email you one.
        </p>
      )}
    </AuthCard>
  );
}

/**
 * Setting a password from an enrolment link.
 *
 * Reached as `/?enroll=<token>`, not a path: App.tsx has no router and adding
 * one for a single screen is not worth it. The token in the query is the whole
 * credential, which is why the link is delivered out of band and is single-use.
 *
 * The server signs the person in on success, so this hands straight over to
 * the app rather than bouncing somebody who has just chosen a password to a
 * form asking for it — which reads as though the enrolment failed.
 */
function EnrollScreen({ token, onDone, onGiveUp }: {
  token: string;
  onDone: () => void;
  onGiveUp: () => void;
}) {
  const [info, setInfo] = useState<
    { username: string; reset: boolean; minPasswordLength: number } | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE_URL}/enroll/${encodeURIComponent(token)}`,
          { credentials: "same-origin" })
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new Error(body?.detail || `HTTP ${r.status}`);
        return body;
      })
      .then((body) => { if (!cancelled) setInfo(body); })
      .catch((e: Error) => { if (!cancelled) setFatal(e.message); });
    return () => { cancelled = true; };
  }, [token]);

  const mismatch = Boolean(confirm) && password !== confirm;
  const ready = Boolean(password) && password === confirm;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !ready) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/enroll`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ token, password }),
      });
      if (res.ok) {
        setPassword("");
        setConfirm("");
        onDone();
        return;
      }
      const body = await res.json().catch(() => null);
      setError(body?.detail || `Could not set the password (HTTP ${res.status}).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not set the password.");
    } finally {
      setBusy(false);
    }
  };

  if (fatal) {
    return (
      <AuthCard>
        <p className="mt-6 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {fatal}
        </p>
        <button type="button" onClick={onGiveUp} className={SUBMIT}>
          Go to the sign-in page
        </button>
      </AuthCard>
    );
  }

  if (!info) {
    return (
      <AuthCard>
        <p className="mt-6 text-sm text-gray-500">Checking that link…</p>
      </AuthCard>
    );
  }

  return (
    <AuthCard onSubmit={submit}>
      <p className="mt-6 text-sm text-gray-700">
        {info.reset ? "Choose a new password for " : "Setting up "}
        <b className="break-all">{info.username}</b>.
      </p>

      <label htmlFor="enroll-password"
             className="mt-4 block text-sm font-medium text-gray-700">
        New password
      </label>
      <input id="enroll-password" type="password" autoFocus
             autoComplete="new-password"
             value={password} onChange={(e) => setPassword(e.target.value)}
             className={FIELD} />
      <p className="mt-1 text-xs text-gray-500">
        At least {info.minPasswordLength} characters. Length is the only rule —
        a long ordinary phrase beats a short complicated one.
      </p>

      <label htmlFor="enroll-confirm"
             className="mt-4 block text-sm font-medium text-gray-700">
        Again
      </label>
      <input id="enroll-confirm" type="password" autoComplete="new-password"
             value={confirm} onChange={(e) => setConfirm(e.target.value)}
             className={FIELD} />
      {mismatch && (
        <p className="mt-1 text-xs text-red-600">Those do not match.</p>
      )}

      {error && (
        <p className="mt-3 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}

      <button type="submit" disabled={busy || !ready} className={SUBMIT}>
        {busy ? "Saving…" : "Set password and sign in"}
      </button>
    </AuthCard>
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
  const [portalOpen, setPortalOpen] = useState(false);
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [baselineOpen, setBaselineOpen] = useState(false);

  // UP HERE WITH THE OTHER HOOKS, DELIBERATELY. This component returns early
  // three times below, and a useState added under one of those is called only
  // on some renders — which React sees as the hook count changing mid-session
  // ("rendered more hooks than during the previous render") and a blank page.
  // CLAUDE.md documents the same trap in PISR.tsx.
  //
  // Read once from the URL rather than watched: the token arrives in the
  // initial navigation and nothing else ever puts one there.
  const [enrollToken, setEnrollToken] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("enroll"));

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

  // Before the error screen and before the login form: somebody following an
  // enrolment link is by definition not signed in, and has not asked for
  // /api/config either.
  if (enrollToken) {
    const leave = () => {
      // Take the token out of the address bar either way. It is single-use, so
      // a spent one left in a bookmark or pasted into a chat is only ever
      // confusing — and a live one has no business sitting in browser history
      // longer than it must.
      window.history.replaceState({}, "", window.location.pathname);
      setEnrollToken(null);
    };
    return (
      <EnrollScreen
        token={enrollToken}
        onDone={() => { leave(); refresh(); }}
        onGiveUp={leave}
      />
    );
  }

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
      : <LoginScreen status={status} onSignedIn={refresh} />;
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
        role: status?.role ?? null,
        // A gate-disabled instance reports mode "disabled" and role "admin";
        // treating a missing role as admin as well would promote every caller
        // the moment /api/auth/status changed shape, so this asks for the word.
        isAdmin: status?.role === "admin",
      }}
    >
      {children}
      {/*
        Rendered here rather than in PISR.tsx's Header: that file is kept as
        close to its rtools2 original as possible, and Header would additionally
        need signOut threaded down to it, which the context does not expose.
        Fixed to the corner so it lands on top of whichever tab is showing, and
        hidden entirely when there is no gate to sign out of.

        Top right, because that is where a browser user looks for whoever they
        are signed in as. It overlays PISR.tsx's own header row, which is left
        aligned and so has the space — except on a narrow phone, where the
        Alpha and Read-only pills reach far enough right to collide. There the
        label is dropped and the icon stands alone.
      */}
      {(() => {
        // In proxy mode the sign-out is only a button if there is somewhere to
        // send them; otherwise it is a label saying who PISR thinks you are.
        const canSignOut =
          !status || status.mode !== "proxy" || Boolean(status.logoutUrl);
        const shell =
          "flex items-center gap-1.5 rounded-full border border-gray-300 " +
          "bg-white px-3 py-1.5 text-xs font-medium text-gray-700 shadow-md";
        // The admin chip is offered on `role`, which the server decided. It is
        // a convenience only: this bundle is served unauthenticated, so anyone
        // can read that the portal exists and call its routes directly —
        // require_admin on the router is what refuses them.
        const isAdmin = status?.role === "admin";
        if (!status?.required && !isAdmin) return null;
        return (
          <div className="fixed top-3 right-3 z-50 flex items-center gap-2">
            {isAdmin && (
              <button
                onClick={() => setPortalOpen(true)}
                title="Choose which report sections users see"
                className={`${shell} hover:bg-white hover:text-gray-900`}
              >
                <SlidersHorizontal size={13} className="shrink-0" />
                <span className="hidden sm:inline">Sections</span>
              </button>
            )}
            {/* Offered only in the mode that has accounts to manage. In proxy
                mode the identities live in the IDP and in passphrase mode
                there are none, so a portal listing nothing would be a puzzle
                rather than a feature. */}
            {isAdmin && status?.mode === "accounts" && (
              <button
                onClick={() => setAccountsOpen(true)}
                title="Add people, issue enrolment links, revoke access"
                className={`${shell} hover:bg-white hover:text-gray-900`}
              >
                <Users size={13} className="shrink-0" />
                <span className="hidden sm:inline">People</span>
              </button>
            )}
            {isAdmin && (
              <button
                onClick={() => setBaselineOpen(true)}
                title="Edit the recommended configuration values"
                className={`${shell} hover:bg-white hover:text-gray-900`}
              >
                <Target size={13} className="shrink-0" />
                <span className="hidden sm:inline">Baselines</span>
              </button>
            )}
            {status?.required && (canSignOut ? (
              <button
                onClick={signOut}
                title={status.user ? `Signed in as ${status.user}` : "Sign out of PISR"}
                className={`${shell} hover:bg-white hover:text-gray-900`}
              >
                <LogOut size={13} className="shrink-0" />
                <span className="hidden sm:inline">
                  {status.user ? `Sign out · ${status.user}` : "Sign out"}
                </span>
              </button>
            ) : (
              <span className={shell} title="Signed in via single sign-on">
                <Lock size={13} className="shrink-0" />
                <span className="hidden sm:inline">{status.user}</span>
              </span>
            ))}
          </div>
        );
      })()}
      {/*
        The portal changes what OTHER people see, never this admin's own page,
        so there is nothing to re-fetch when it closes. An admin checking their
        work has to sign in as a user — which is the honest thing to make them
        do, since a preview rendered by the same bundle that hides things is a
        preview of the bundle, not of the server's answer.
      */}
      {portalOpen && <AdminVisibility onClose={() => setPortalOpen(false)} />}
      {/*
        Closing this one DOES have a consequence for the admin's own session —
        they can demote or disable themselves — but the server re-checks the
        role on every request, so the next call resolves it rather than this
        component needing to guess. Deleting your own account signs you out at
        the next request, which is the honest outcome.
      */}
      {accountsOpen && <AdminAccounts onClose={() => setAccountsOpen(false)} />}
      {baselineOpen && <AdminBaseline onClose={() => setBaselineOpen(false)} />}
    </AuthContext.Provider>
  );
}

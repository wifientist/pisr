import { useCallback, useEffect, useState } from "react";
import {
  Check, Copy, KeyRound, Loader2, Trash2, UserPlus, Users, X,
} from "lucide-react";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "/api";

/**
 * The accounts portal: create people, hand out enrolment links, revoke.
 *
 * NOTHING IS EMAILED, and that is the whole reason this exists. PISR's gate was
 * Cloudflare Access until its one-time-PIN mail turned out to be silently
 * discarded by two of three customer domains — so an admin creates an account
 * here, copies the enrolment link, and delivers it however they like. See
 * api/accounts.py.
 *
 * THE LINK IS SHOWN ONCE. Only its hash reaches the server's disk, so there is
 * no route that can show it again and no amount of admin privilege recovers
 * it. This dialog therefore keeps a freshly minted link on screen until the
 * admin dismisses it, rather than clearing on the next render — losing it to a
 * stray click means reissuing, which is a support question nobody enjoys.
 *
 * WHAT THIS IS NOT. It does not set passwords. There is no field for one and no
 * route behind it: the person whose account it is should be the only one who
 * ever knows theirs, which is also why "reset" issues a link instead of
 * choosing something on their behalf.
 */

interface AccountRow {
  id: string;
  username: string;
  role: "admin" | "user";
  disabled: boolean;
  enrolled: boolean;
  createdAt: string | null;
  createdBy: string | null;
  passwordChangedAt: string | null;
  invitePending: boolean;
  inviteExpiresAt: string | null;
  inviteExpired: boolean;
}

interface Payload {
  users: AccountRow[];
  roles: string[];
  writable: boolean;
  broken: boolean;
  path: string | null;
  usernameRule: string;
  minPasswordLength: number;
  inviteDays: number;
  mode: string;
}

interface MintedInvite {
  account: AccountRow;
  /** A PATH. The server does not know its own public address — see below. */
  enrolPath: string;
  expiresAt: string | null;
}

/**
 * The enrolment link the admin actually copies.
 *
 * THE SERVER SENDS A PATH AND THE BROWSER MAKES IT ABSOLUTE, because only the
 * browser knows the address this admin reached PISR on. The server building it
 * produced `http://backend:8080/...` in dev, where vite proxies to the
 * container's internal name, and in production would have rested on whatever
 * Host the tunnel forwarded — a link that looks right and opens nothing. See
 * api/routers/accounts_router.py.
 */
function absoluteEnrolUrl(path: string): string {
  return new URL(path, window.location.origin).toString();
}

/** A link just minted, held on screen until dismissed. */
interface FreshLink {
  username: string;
  url: string;
  expiresAt: string | null;
  reset: boolean;
}

function state(row: AccountRow): { label: string; tone: string } {
  if (row.disabled) return { label: "Disabled", tone: "bg-gray-100 text-gray-600" };
  if (row.enrolled) return { label: "Active", tone: "bg-green-50 text-green-700" };
  if (row.inviteExpired)
    return { label: "Invite expired", tone: "bg-red-50 text-red-700" };
  if (row.invitePending)
    return { label: "Invited", tone: "bg-amber-50 text-amber-800" };
  return { label: "No way in", tone: "bg-red-50 text-red-700" };
}

export default function AdminAccounts({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [newAdmin, setNewAdmin] = useState(false);
  const [link, setLink] = useState<FreshLink | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(() => {
    fetch(`${API_BASE_URL}/admin/accounts`, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((payload: Payload) => setData(payload))
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  const call = async (path: string, init: RequestInit): Promise<unknown | null> => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE_URL}/admin/accounts${path}`, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        ...init,
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setError(body?.detail || `Request failed (HTTP ${res.status}).`);
        return null;
      }
      load();
      return body;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
      return null;
    } finally {
      setBusy(false);
    }
  };

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    const body = (await call("", {
      method: "POST",
      body: JSON.stringify({
        username: newName.trim().toLowerCase(),
        role: newAdmin ? "admin" : "user",
      }),
    })) as MintedInvite | null;
    if (body) {
      setLink({
        username: body.account.username,
        url: absoluteEnrolUrl(body.enrolPath),
        expiresAt: body.expiresAt,
        reset: false,
      });
      setNewName("");
      setNewAdmin(false);
    }
  };

  const reissue = async (row: AccountRow) => {
    const body = (await call(`/${row.id}/invite`, { method: "POST" })) as
      MintedInvite | null;
    if (body) {
      setLink({
        username: body.account.username,
        url: absoluteEnrolUrl(body.enrolPath),
        expiresAt: body.expiresAt,
        reset: row.enrolled,
      });
    }
  };

  const patch = (row: AccountRow, change: Partial<Pick<AccountRow, "role" | "disabled">>) =>
    call(`/${row.id}`, { method: "PATCH", body: JSON.stringify(change) });

  const remove = (row: AccountRow) => {
    // Deleting is not the usual intent behind "remove this person" — disabling
    // is, and it is reversible. Say so rather than only asking twice.
    if (!window.confirm(
      `Delete ${row.username} permanently?\n\n` +
      "Disabling them instead stops them signing in and can be undone.")) return;
    return call(`/${row.id}`, { method: "DELETE" });
  };

  const copy = async (url: string) => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be refused; the link is selectable text anyway.
      setCopied(false);
    }
  };

  const readOnly = Boolean(data && !data.writable);

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto
                    bg-gray-900/40 p-4 sm:p-8">
      {/* min-w-0 throughout: usernames and a full enrolment URL are both
          unbreakable tokens, and without it they set the width of the dialog
          on a phone. See the note in CLAUDE.md. */}
      <div className="w-full max-w-3xl min-w-0 rounded-lg border border-gray-200 bg-white shadow-xl">
        <div className="flex items-start justify-between gap-3 border-b border-gray-200 p-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 font-semibold text-gray-900">
              <Users size={17} className="text-gray-400 shrink-0" />
              People
            </h2>
            <p className="mt-0.5 text-xs text-gray-500">
              Accounts that can sign in to PISR. Nothing is emailed — you copy
              the enrolment link and send it however you like.
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

        {data && data.mode !== "accounts" && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <b>These accounts decide nothing yet.</b> This instance is in{" "}
            <code>{data.mode}</code> mode. Create them now if you like — then set{" "}
            <code>PISR_AUTH_MODE=accounts</code> and restart, and they take effect.
          </p>
        )}

        {data?.broken && (
          <p className="m-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
            <b>{data.path} could not be read.</b> Nobody can sign in with an
            account until it is repaired or removed, and it will not be
            overwritten from here. Use the break-glass passphrase meanwhile.
          </p>
        )}

        {readOnly && !data?.broken && (
          <p className="m-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            <b>Read-only.</b> {data?.path} is not writable by the container, so
            accounts cannot be created or changed. Mount a writable volume at
            its directory — without one every account is lost at the next deploy.
          </p>
        )}

        {/* A freshly minted link. Deliberately sticky: it cannot be shown
            again, so it stays until dismissed rather than disappearing on the
            next render. */}
        {link && (
          <div className="m-4 rounded-md border border-blue-200 bg-blue-50 p-3">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 text-sm font-medium text-blue-900">
                {link.reset ? "Password reset link for " : "Enrolment link for "}
                <b>{link.username}</b>
              </p>
              <button onClick={() => setLink(null)} aria-label="Dismiss"
                      className="shrink-0 rounded p-0.5 text-blue-400 hover:text-blue-700">
                <X size={15} />
              </button>
            </div>
            <p className="mt-1 text-xs text-blue-800">
              Send this to them however you like — it is single-use and{" "}
              {link.expiresAt
                ? <>expires <b>{new Date(link.expiresAt).toLocaleString()}</b></>
                : "expires"}
              . <b>It cannot be shown again</b>; only its hash is stored. If it
              gets lost, issue another.
              {link.reset && " Their current password keeps working until this is used."}
            </p>
            <div className="mt-2 flex items-center gap-2">
              <code className="min-w-0 flex-1 break-all rounded border border-blue-200
                               bg-white px-2 py-1.5 text-xs text-gray-800">
                {link.url}
              </code>
              <button onClick={() => copy(link.url)}
                      className="shrink-0 inline-flex items-center gap-1 rounded-md border
                                 border-blue-300 bg-white px-2.5 py-1.5 text-xs
                                 font-medium text-blue-700 hover:bg-blue-100">
                {copied ? <Check size={13} /> : <Copy size={13} />}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        )}

        {!data && !error && (
          <p className="flex items-center gap-2 p-8 text-sm text-gray-500">
            <Loader2 size={15} className="animate-spin" /> Loading accounts…
          </p>
        )}

        {data && (
          <div className="max-h-[60vh] overflow-y-auto p-4 space-y-4">
            {data.users.length === 0 && (
              <p className="rounded-md bg-gray-50 px-3 py-2 text-sm text-gray-600">
                No accounts yet. The first one is usually made on the box with{" "}
                <code>scripts/pisr_admin.py</code>, but you can add one here.
              </p>
            )}

            <div className="space-y-2">
              {data.users.map((row) => {
                const tone = state(row);
                return (
                  <div key={row.id}
                       className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2
                                  rounded-md border border-gray-200 p-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium text-gray-900">
                          {row.username}
                        </span>
                        <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px]
                                          font-medium ${tone.tone}`}>
                          {tone.label}
                        </span>
                      </div>
                      <p className="truncate text-xs text-gray-500">
                        {row.passwordChangedAt
                          ? `Password set ${new Date(row.passwordChangedAt).toLocaleDateString()}`
                          : row.inviteExpiresAt
                            ? `Invite expires ${new Date(row.inviteExpiresAt).toLocaleDateString()}`
                            : "Never enrolled"}
                        {row.createdBy ? ` · added by ${row.createdBy}` : ""}
                      </p>
                    </div>

                    <select
                      value={row.role}
                      disabled={busy || readOnly}
                      onChange={(e) => patch(row, { role: e.target.value as "admin" | "user" })}
                      className="shrink-0 rounded-md border border-gray-300 px-2 py-1 text-xs
                                 disabled:bg-gray-100"
                    >
                      <option value="user">User</option>
                      <option value="admin">Admin</option>
                    </select>

                    <button
                      onClick={() => reissue(row)}
                      disabled={busy || readOnly}
                      title={row.enrolled
                        ? "Issue a password reset link"
                        : "Issue a fresh enrolment link"}
                      className="shrink-0 inline-flex items-center gap-1 rounded-md border
                                 border-gray-300 px-2 py-1 text-xs font-medium text-gray-700
                                 hover:bg-gray-50 disabled:opacity-50"
                    >
                      <KeyRound size={12} />
                      {row.enrolled ? "Reset" : "Re-invite"}
                    </button>

                    <button
                      onClick={() => patch(row, { disabled: !row.disabled })}
                      disabled={busy || readOnly}
                      className="shrink-0 rounded-md border border-gray-300 px-2 py-1 text-xs
                                 font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                    >
                      {row.disabled ? "Enable" : "Disable"}
                    </button>

                    <button
                      onClick={() => remove(row)}
                      disabled={busy || readOnly}
                      aria-label={`Delete ${row.username}`}
                      className="shrink-0 rounded-md border border-gray-300 p-1 text-gray-500
                                 hover:bg-red-50 hover:text-red-700 disabled:opacity-50"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                );
              })}
            </div>

            <form onSubmit={create}
                  className="flex min-w-0 flex-wrap items-end gap-2 border-t
                             border-gray-200 pt-4">
              <div className="min-w-0 flex-1">
                <label htmlFor="new-account"
                       className="block text-xs font-medium text-gray-700">
                  New account
                </label>
                <input
                  id="new-account"
                  value={newName}
                  disabled={busy || readOnly}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="username"
                  className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5
                             text-sm focus:border-blue-500 focus:outline-none
                             focus:ring-1 focus:ring-blue-500 disabled:bg-gray-100"
                />
                <p className="mt-1 text-[11px] text-gray-500">{data.usernameRule}</p>
              </div>
              <label className="flex shrink-0 items-center gap-1.5 pb-1.5 text-xs text-gray-700">
                <input type="checkbox" checked={newAdmin} disabled={busy || readOnly}
                       onChange={(e) => setNewAdmin(e.target.checked)} />
                Admin
              </label>
              <button
                type="submit"
                disabled={busy || readOnly || !newName.trim()}
                className="mb-1 shrink-0 inline-flex items-center gap-1.5 rounded-md
                           bg-blue-600 px-3 py-1.5 text-sm font-medium text-white
                           hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
              >
                <UserPlus size={14} />
                Add
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}

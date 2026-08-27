/**
 * The fetch wrapper PISR.tsx imports.
 *
 * In rtools2 this carried session cookies and handled a 401 by refreshing the
 * access token, retrying the request, and redirecting to /login if that failed.
 *
 * Standalone PISR has one shared passphrase and one signed session cookie, so
 * there is no refresh token to spend and no /login route to redirect to — the
 * login form is a state of the app, not a page. A 401 therefore means the
 * session expired (or the passphrase was rotated), and the only useful response
 * is to tell AuthProvider to show the form again. It announces that on an event
 * rather than calling into React, so this module stays importable from
 * anywhere and PISR.tsx needs no edit.
 *
 * A 401 is still returned to the caller unchanged. PISR.tsx renders `detail`
 * into its error banner, and that banner is the correct thing to show for the
 * fraction of a second before the form replaces it.
 *
 * The signature is kept compatible with the original so PISR.tsx needs no edit.
 */

export const UNAUTHENTICATED_EVENT = "pisr:unauthenticated";

export async function apiFetch(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  const res = await fetch(url, { credentials: "same-origin", ...options });
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent(UNAUTHENTICATED_EVENT));
  }
  return res;
}

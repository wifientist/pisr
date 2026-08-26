/**
 * The fetch wrapper PISR.tsx imports.
 *
 * In rtools2 this carried session cookies and handled a 401 by refreshing the
 * access token, retrying the request, and redirecting to /login if that failed.
 *
 * Standalone PISR has no users, no session and no login page, so all of that
 * machinery is gone and nothing replaces it. A 401 here means exactly one
 * thing — RUCKUS ONE rejected the credentials in .env — and it is passed
 * straight through to the caller, which already renders `detail` into its error
 * banner. The failure becomes legible instead of being swallowed by a redirect
 * to a route that does not exist.
 *
 * The signature is kept compatible with the original so PISR.tsx needs no edit.
 */
export async function apiFetch(
  url: string,
  options: RequestInit = {},
): Promise<Response> {
  return fetch(url, options);
}

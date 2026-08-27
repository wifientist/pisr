# Project: pisr

Standalone extraction of the PISR tool from rtools2. One purpose: poll one
RUCKUS ONE venue, shape it, check it, render it.

## Development Environment

- Everything runs in Docker. Do not run Python or Node directly on the host.
- Production: `docker compose up --build` → <http://localhost:8090>
- Dev (hot reload): `docker compose -f docker-compose.dev.yml up` → frontend
  <http://localhost:4173>, API <http://127.0.0.1:4174>
- Run backend commands with `docker compose run --rm pisr <cmd>` (prod compose)
  or `docker compose -f docker-compose.dev.yml exec backend <cmd>` (dev).
- Production redeploys itself by polling this repo — a systemd user timer on
  the box, not a webhook. See `deploy/README.md`. Nothing listens inbound for
  it, deliberately: the box reaches GitHub, GitHub never reaches the box, which
  is the same property the tunnel is there for. A deploy endpoint inside PISR
  would need the podman socket, and that makes the container host-root.

## Architecture

- **Backend**: Python FastAPI (`api/`), serves the API *and* the built SPA
- **Frontend**: React 19 + TypeScript + Vite + Tailwind 3 (`src/`)
- **No database, no Redis, no scheduler.** Config is `.env`, read once at
  import by `api/config.py`. Any setting can arrive as `<NAME>_FILE` pointing
  at a file instead, which is how a secret gets in without going through the
  container environment.
- **Auth has two modes, both in `api/auth.py`**, gating everything under `/api`
  plus `/docs`. `passphrase` (default) is one shared secret for a signed
  HttpOnly cookie. `proxy` trusts an identity header from an authenticating
  reverse proxy (oauth2-proxy) and switches the passphrase off. No accounts, no
  roles, no session store, and no OIDC implemented here.
- **External API**: RuckusONE only. R1 has a 15-SSID-per-AP-Group limit, which
  `checks.py` asserts on.

## Constraints that are not negotiable

- **Read-only.** Every R1 call is a `GET` or a `POST` to a `*/query` path. Do not
  add a call that creates, updates, deletes, activates, reboots or syncs
  anything. This is a documented guarantee of the tool, stated in the module
  docstrings and the README.
- **Human-triggered.** No scheduler, no background tasks, no recurring polls. If
  something needs to happen repeatedly, a person clicks the button.
- **PISR stores nothing.** No snapshot files, no cache, no database. A report
  lives for the length of one HTTP response.

## Traps

- **Never cache `R1Client` at module level.** It authenticates once in
  `__init__` and never re-authenticates; a process-lifetime client dies ~1h after
  start. See the docstring on `build_r1_client` in `api/r1_client.py`.
- **The `StaticFiles` mount in `api/main.py` must stay last** — a `Mount("/")`
  matches everything above it.
- **The SPA bundle is served unauthenticated, on purpose.** It has to load in
  order to render the login form, and it carries no tenant data. Everything
  that *does* know the tenant is behind the gate. Don't "fix" this by gating
  `/` — you get a blank page with no way to sign in.
- **The container healthcheck hits `/healthz`, not `/api/status`.** The latter
  is gated now, and names the tenant besides. If you add a healthcheck
  anywhere, point it at `/healthz`.
- **Proxy mode's security is `PISR_TRUSTED_PROXY_IPS` plus the port binding,
  not the header.** A header is a claim anyone can make. If you ever find PISR
  in proxy mode published on `0.0.0.0`, that is a live authentication bypass —
  `expose:`, not `ports:`.
- **`SessionGateMiddleware` gates by path prefix, not by route**, so a router
  added later is gated by default. Note that Starlette's `add_middleware` does
  `insert(0, ...)`, so the LAST middleware added is the OUTERMOST — the reverse
  of the intuitive reading, and the reason the gate sits in front of CORS. That
  is the safe direction; the cost is that a CORS preflight to a gated path gets
  a 401 with no CORS headers, which only matters if the frontend ever moves to
  its own origin.
- **A forwarded header is believed only from a peer in
  `PISR_TRUSTED_PROXY_IPS`** — `X-Forwarded-Proto` for the cookie's `Secure`
  flag, and `PISR_CLIENT_IP_HEADER` for the address the login throttle counts
  against. Both fail closed to the TCP peer. Do not "simplify" either into
  reading the header unconditionally: a client-address header any caller can
  set makes the throttle *weaker* than no header at all, because an attacker
  varies it per request and is never counted twice.
- **`_proxy_identity` keys off `_peer_ip`, never `_client_ip`.** Who may assert
  an identity is a question about who opened the socket. Resolving a forwarded
  address first would let a header decide whether that same header is trusted.
- **`R1_EC_TYPE` and `R1_VERBOSE` are literal-compared** in `api/services/pisr/fetch.py`
  and `api/r1api/client.py` respectively. Don't rename `R1_VERBOSE`; don't
  lowercase `MSP`.
- **`_jinja()` in `pisr_router.py` walks two `.parent`s** to reach
  `api/templates`. It fails at PDF-request time, not import time.
- **The build SHA on `/api/status` comes from a Dockerfile `ARG`,** passed by
  `docker-compose.yml` from the environment and set by `deploy/pisr-update.sh`.
  It is also an OCI label, which is what lets the deploy script confirm the
  running container without a session cookie. A hand-built image says
  `unknown`; that is correct, not a bug. Do not move it to `/healthz` — that
  endpoint is public and the commit is reconnaissance.
- **WeasyPrint needs Pango at import.** If you trim the Dockerfile's apt list
  further, the container stops starting rather than the PDF endpoint breaking
  later.
- **The `APT::Sandbox::User "root"` line in the Dockerfile is what makes the
  image build under rootless Podman.** Without it apt drops to uid 65534, which
  a rootless subuid range usually does not map, and the build fails in apt. It
  does nothing under Docker, which is exactly why someone will delete it.

## Files kept byte-identical to rtools2

`api/r1api/**`, `api/services/pisr/{collect,fetch,shape,checks}.py`,
`api/reports/pisr.py`, `api/templates/reports/pisr.html`, `src/pages/PISR.tsx`,
`src/components/SingleEcSelector.tsx`, `src/hooks/useSingleEc.tsx`.

Keep them that way where you can — it makes pulling upstream changes a readable
diff rather than an archaeology exercise. If you must diverge, note it here.

### Known divergences

- **Spectrum chart paint order** — `src/pages/PISR.tsx` (`SpectrumChart`) and
  `api/reports/pisr.py` (`_spectrum`). Both sorted blocks by `inUse` alone, so
  states differing only by colour painted in channel order and a translucent
  not-permitted slot could cover a permitted-but-unused neighbour — 2.4 GHz
  channel 10 under channel 12. Both now sort by a `rank` of grey < green <
  blue < amber. The two renderers must keep matching each other; the PDF is
  meant to be the same picture as the screen.

- **`min-w-0` on flex and grid children** — `src/pages/PISR.tsx`, in `Card`,
  `MiniTable`, `BarList`, `Meter`, the venue card and the external-address row.
  A flex or grid item defaults to `min-width: auto`, so it will not shrink
  below its content's minimum. Anything with `truncate` (`white-space: nowrap`)
  or an unbreakable token — an IPv6 literal, a long address — therefore widened
  its column instead of ellipsing, and the page scrolled sideways on a phone.
  Note that a scroll container only gets an automatic minimum of zero when it
  is *itself* a flex or grid item: `MiniTable`'s `overflow-auto` wrapper is a
  plain block, so the fix had to go on the `Card` around it.

  **If you add a grid or flex layout here, put `min-w-0` on the children.** The
  overflow is invisible on a desktop viewport and only shows up on a phone.
  `docker compose exec` a headless browser and check `scrollWidth` at 320px.

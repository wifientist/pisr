# Project: pisr

Poll one RUCKUS ONE venue, shape it, check it, render it. One purpose, done
properly.

It began as an extraction from rtools2 and has since diverged into its own
thing — see "Lineage" below before assuming any file is meant to match an
upstream copy. It is not.

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
- **Auth has three modes, all in `api/auth.py`**, gating everything under
  `/api` plus `/docs`. `passphrase` (default) is one shared secret for a signed
  HttpOnly cookie. `proxy` trusts an identity header from an authenticating
  reverse proxy (oauth2-proxy, or Cloudflare Access with a verified assertion)
  and switches the passphrase off. `accounts` keeps per-person logins in a JSON
  file — see `api/accounts.py`. No session store and no OIDC implemented here.
- **`accounts` mode exists because Cloudflare Access stopped being usable.**
  Its one-time-PIN mail is silently discarded by two of three customer domains,
  and every mail-based scheme inherits that — a new sending domain is a *worse*
  corporate-filter signal than cloudflare.com, not a better one. So an admin
  creates an account and hands over a single-use enrolment link OUT OF BAND.
  Nothing is emailed, deliberately and permanently: if a future change makes
  sign-in depend on mail arriving, it has reintroduced the exact fault this
  replaced.
- **Two roles, `admin` and `user`.** In proxy mode the role comes from the
  verified identity against `PISR_ADMIN_EMAILS`; in accounts mode it is stored
  per account; in passphrase mode it comes from which of two passphrases signed
  the cookie. An admin sets a policy — which report sections a user is shown,
  and which MSP-ECs and venues a user may reach — stored as one JSON file. Both
  roles are fully authenticated; the role decides what a report contains, not
  whether you may have one.
- **External API**: RuckusONE only. R1 has a 15-SSID-per-AP-Group limit, which
  `checks.py` asserts on.

## Constraints that are not negotiable

- **Read-only.** Every R1 call is a `GET` or a `POST` to a `*/query` path. Do not
  add a call that creates, updates, deletes, activates, reboots or syncs
  anything. This is a documented guarantee of the tool, stated in the module
  docstrings and the README.
- **Human-triggered.** No scheduler, no background tasks, no recurring polls. If
  something needs to happen repeatedly, a person clicks the button.
- **PISR stores nothing — of the tenant.** No snapshot files, no cache, no
  database, no report that outlives the response carrying it. It writes exactly
  TWO files, both on the `pisr-config` volume, and neither holds anything
  belonging to the customer whose network is being reported on:

  - the role policy at `PISR_VISIBILITY_FILE` (`/data/visibility.json`) — a
    list of section ids and venue ids, no venue data, no device, no credential.
    See `api/visibility.py`.
  - local accounts at `PISR_ACCOUNTS_FILE` (`/data/accounts.json`), in
    `accounts` mode only — usernames, roles and scrypt hashes for the OPERATORS
    of the tool. See `api/accounts.py`.

  Guard the distinction rather than the file count. Both of these are
  configuration with a portal in front of it; a customer's *report* is the
  thing that must not be persisted, and if something later wants to keep one
  there it is a different feature that has to make its own case.

## The role policy, in four files

Read them in this order; each explains the next.

- `api/sections.py` — the catalogue of hideable report sections. Each names the
  report paths it OWNS and the check ids its findings come from.
- `api/redact.py` — empties those paths and drops those findings. **The single
  enforcement point for section visibility**, applied at `build_report`'s
  boundary so the JSON route and the PDF route cannot disagree.
- `api/scope.py` — which MSP-ECs and venues a role may reach. A different kind
  of control with different rules; see the trap below.
- `api/visibility.py` — the file on disk, holding both halves.

## Traps

- **`hashlib.scrypt` needs `maxmem` passed explicitly.** At PISR's parameters
  (N=2^15, r=8) it wants 128·N·r = *exactly* 32 MiB, which is also OpenSSL's
  default ceiling — so the default raises `ValueError: memory limit exceeded`
  rather than being merely slow, and it raises at the first sign-in rather than
  at import. `accounts._SCRYPT_MAXMEM` is 64 MiB for that reason. Raise N
  without raising it and nobody can log in;
  `test_accounts.py::test_scrypt_params_are_within_the_maxmem_ceiling` is what
  says so.

- **The session key includes the stored password hash**
  (`auth._account_key`). That is what makes a password change end that
  person's other sessions, and a disable or delete end them immediately rather
  than at the next expiry. It is `_signing_key`'s trick applied per person. Do
  not "simplify" it to sign on the account id alone — revocation silently
  becomes "within twelve hours".

- **The login throttle keys on BOTH the address and the username, and in
  `accounts` mode it backs off rather than locking out.** Both halves matter.
  Under rootless podman every caller shares one apparent address, so `ip:`
  collapses to a global counter and a hard lockout there is an outage switch
  anyone can flip; a hard lockout on `user:` lets anyone lock out a named
  person on purpose. Backoff (doubling, capped at 60s) throttles guessing
  without handing a stranger a way to take the tool away. `ip:` and `enroll:`
  additionally get a free allowance because they are shared — a colleague
  mistyping a password must not slow everybody else down — while `user:` gets
  none, since only the person guessing pays it.

- **The enrolment routes throttle FAILURES ONLY, checked after the lookup.** A
  valid token is answered whatever anyone else has been doing, because the
  address is shared; throttling before the lookup would let one person clicking
  a stale link delay everybody's enrolment. Note also that a password below the
  floor is classified as the invitee's own slip and never counted — otherwise
  mistyping your new password makes fixing it slower, during the one flow you
  cannot skip.

- **The enrolment link is built by the BROWSER, not the server.**
  `accounts_router` returns a path and `AdminAccounts.tsx` resolves it against
  `window.location.origin`. The server does not know its own public address: an
  earlier version produced `http://backend:8080/...` in dev, and "fixing" that
  by reading `X-Forwarded-Host` would believe a header from any peer, which is
  precisely what `auth.py` is careful never to do.

- **`PISR_AUTH_ADMIN_PASSPHRASE` is break-glass in `accounts` mode**, and that
  is a deliberate exception to proxy mode's rule that a shared secret must not
  be a way around the identity system. The difference is where identity lives:
  proxy mode's IDP is external and is still there when the volume is not,
  whereas accounts live ON that volume. Losing it, or deleting the last admin,
  would otherwise need an SSH session to a box that is deliberately awkward to
  SSH into. `main.py` warns about it at every startup on purpose.

- **The accounts file fails CLOSED; the visibility policy fails OPEN.** Same
  volume, same shape, opposite rules, for the reason the existing section-vs-
  scope trap gives: one is de-cluttering and one is the gate. An unreadable
  accounts file admits nobody and is *not overwritten* — a damaged but
  recoverable file is worth more than our idea of what was in it.

- **`scripts/pisr_admin.py` is a SECOND WRITER of `/data/accounts.json`**,
  running in its own container against the same volume. That is why
  `AccountStore` compares `st_mtime_ns` rather than `st_mtime` (two writes in
  one second is a real sequence here) and re-reads before every mutation. Drop
  either and a portal save silently deletes what the CLI just added.

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
  anywhere, point it at `/healthz`. It also needs `BUILDAH_FORMAT=docker`,
  which `deploy/pisr-update.sh` exports — podman's default OCI format has no
  healthcheck field and discards it with a build warning. And it checks
  localhost from inside the container, so it cannot see a dead published port;
  the deploy script's external probe is what catches that.
- **Proxy mode's security is `PISR_TRUSTED_PROXY_IPS` plus the port binding,
  not the header.** A header is a claim anyone can make. If you ever find PISR
  in proxy mode published on `0.0.0.0`, that is a live authentication bypass —
  `expose:`, not `ports:`. A bind to a specific address is not automatically
  wrong, though: what matters is whether anything other than the proxy can
  open a socket to it. An isolated bridge carrying only the proxy and PISR is
  as sound as loopback, and unlike loopback it works with the proxy on another
  host.
- **`SessionGateMiddleware` gates by path prefix, not by route**, so a router
  added later is gated by default. Note that Starlette's `add_middleware` does
  `insert(0, ...)`, so the LAST middleware added is the OUTERMOST — the reverse
  of the intuitive reading, and the reason the gate sits in front of CORS. That
  is the safe direction; the cost is that a CORS preflight to a gated path gets
  a 401 with no CORS headers, which only matters if the frontend ever moves to
  its own origin.
- **Under rootless podman the peer is never the proxy's real address.**
  Publishing a port rootlessly rewrites the source, so everything arrives from
  inside podman's network (10.89.0.0/24 and friends). `PISR_TRUSTED_PROXY_IPS`
  has to name *that*, found by logging in once and reading "Signed in from" out
  of the container log — and it then distinguishes nothing, because every
  caller looks the same. The network boundary is the real control there; the
  IP check is a backstop for deployments where the peer is genuine.
- **A forwarded header is believed only from a peer in
  `PISR_TRUSTED_PROXY_IPS`** — `X-Forwarded-Proto` for the cookie's `Secure`
  flag, and `PISR_CLIENT_IP_HEADER` for the address the login throttle counts
  against. Both fail closed to the TCP peer. Do not "simplify" either into
  reading the header unconditionally: a client-address header any caller can
  set makes the throttle *weaker* than no header at all, because an attacker
  varies it per request and is never counted twice.
- **When `PISR_ACCESS_TEAM`/`PISR_ACCESS_AUD` are set, the verified assertion
  is the identity and the peer address is not consulted at all** — that is the
  point of it, since under rootless podman the peer distinguishes nothing.
  `api/cf_access.py` fails closed on every path: bad signature, wrong `aud`,
  wrong issuer, expired, unreachable JWKS. The `aud` check is the one that
  binds a token to *this* application; without it any app in the account
  passes. Half-configuring it stops the container starting.
- **`_proxy_identity` keys off `_peer_ip`, never `_client_ip`.** Who may assert
  an identity is a question about who opened the socket. Resolving a forwarded
  address first would let a header decide whether that same header is trusted.
- **Section visibility fails OPEN; EC/venue scope fails CLOSED.** They live in
  one file and one dialog and they are not the same kind of control. Hiding a
  section is de-cluttering — a corrupt policy, an unknown id, a missing file
  all resolve to "hide nothing", because the alternative is an app that renders
  nothing and explains nothing. Scope is access: once an admin names a single
  EC, anything unnamed is refused, an empty venue list means NO venues rather
  than all of them, and an EC row with no identifiable id is dropped rather
  than passed through. On an MSP tenant the ECs are different companies. Do not
  let a refactor make the two consistent with each other.

- **Scope is enforced at the route, not by filtering a response.**
  `pisr_router._require_scope` 403s on the report, the PDF and the venue list;
  the filtering in `msp_router` and `get_venues` only keeps other customers'
  names off a picker. Both exist on purpose — a filter that is also the check
  is a filter somebody later "optimises" into a UI concern.

- **The PDF route re-polls, so every control has to be applied twice.**
  `get_report_pdf` calls `build_report` itself rather than rendering what the
  browser holds. Both the scope check and `redact` are repeated there. Add a
  control to `get_report` and not to `get_report_pdf` and the download is the
  way around it.

- **Redaction empties, it never deletes.** `redact._blank_like` replaces a
  hidden path with an empty value of the same type. Two renderers in two
  languages call `.length`, `.map` and `|length` on this payload assuming the
  keys exist; deleting one turns a hidden card into a blank page.

- **Findings are cross-cutting and get forgotten.** Checks read the whole
  report, so hiding the PoE cards without dropping their findings leaves the
  Verification card reporting on cards that are not there. Each section names
  its check ids and `redact` recomputes the tallies. Note that a check
  function's name and its finding id can differ — `check_empty_ap_groups`
  emits `ssid-scope` — and the finding id is what is filtered.
  `api/tests/test_sections.py::test_checks_exist` is what caught that.

- **`api/tests/test_sections.py` is the only thing stopping the three id lists
  drifting.** Section ids are hand-written into `api/sections.py`,
  `src/pages/PISR.tsx` and `api/templates/reports/pisr.html`, because the
  Dockerfile's web stage does not copy `api/` and there is no module both sides
  can import. It needs the working tree, not the runtime image:

      docker compose -f docker-compose.dev.yml run --rm --no-deps \
        -v "$PWD:/repo" backend python /repo/api/tests/test_sections.py

- **Hooks in `PISR.tsx` must sit above its early returns.** The component
  returns early three times before a venue is chosen (no controller, no EC, no
  venue). A `useEffect` added below one of those is called only after a venue
  is picked, which React sees as the hook count changing mid-session — "rendered
  more hooks than during the previous render", and a blank page. The tab
  fallback effect is up with the other hooks for exactly this reason.

- **`/data` is created and chowned in the Dockerfile before `USER pisr`.**
  Docker and podman seed a fresh named volume from what the image has at the
  mount point, ownership included — so without that `mkdir`/`chown` the volume
  arrives root-owned, the uid-1000 process cannot write it, and the portal
  silently reports itself read-only. This does not help a BIND mount, where the
  host's ownership wins.

- **`R1_EC_TYPE` and `R1_VERBOSE` are literal-compared** in `api/services/pisr/fetch.py`
  and `api/r1api/client.py` respectively. Don't rename `R1_VERBOSE`; don't
  lowercase `MSP`.
- **`_jinja()` in `pisr_router.py` walks two `.parent`s** to reach
  `api/templates`. It fails at PDF-request time, not import time.
- **Anything long-lived spawned by `deploy/pisr-update.sh` must close the lock
  fd with `9>&-`.** `exec 9>` does not set close-on-exec, so conmon and
  rootlessport would inherit the flock and hold it for the life of the
  container — every later run then declines with "Another deploy is in
  progress" on a box where none is. This only became reachable once
  `KillMode=process` let those processes survive; the two interact.
- **`KillMode=process` in `deploy/pisr-update.service` is load-bearing.**
  podman leaves `conmon` and `rootlessport` in the unit's cgroup, and a oneshot
  unit's default `KillMode=control-group` SIGKILLs them ~90s after the script
  exits — killing the container the deploy just brought up, having already
  reported success. Do not remove it.
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

## Lineage: PISR is freestanding now

This started as an extraction from rtools2, and for a while the rule was to keep
the carried-over files byte-identical so upstream changes stayed a readable
diff. **That rule is retired.** The report pipeline, both renderers and the
whole role-policy layer have diverged deliberately and substantially, and the
pretence that a future `git diff` against rtools2 would mean anything was
costing more than it bought — it pushed real fixes into awkward shapes to avoid
touching a file.

What that changes, practically:

- **Edit any file here on its merits.** No file is frozen. If `shape.py` wants
  a new tally, give it one; if `collect.py` should read another endpoint, add
  it. Judge the change by whether it is right for PISR.
- **The constraints in the section above still bind.** Read-only,
  human-triggered, no tenant data stored. Those were never about rtools2 — they
  are what this tool promises in its own docstrings and README.
- **`api/r1api/**` is still the odd one out**, and worth leaving alone by
  default. Not because of upstream, but because it is a general RUCKUS ONE
  client with its own semantics (pagination quirks, the ES window, the
  page-0/1 alias) that took live tenants to work out, and PISR uses a fraction
  of it. Changing it to suit one caller is how those hard-won notes rot.
- If something here would genuinely help rtools2, port it deliberately as a
  patch. Do not shape PISR around making that easy.

The notes below started life as a divergence list. They are kept because the
reasoning is still load-bearing, not because anything is being tracked against
an upstream.

## Design notes

- **R1 connection pool** — `api/r1api/client.py`. One of the few edits made to
  the R1 client, and the reason to know about it is that rtools2 still carries
  the bug. A bare `requests.Session()` gets urllib3's default
  `pool_maxsize=10`, while `collect.py` fans a report out over asyncio's
  default executor — `min(32, cpu_count + 4)` threads. On anything with more
  than six cores that exceeds the pool, and every request past the tenth pays
  a fresh TCP and TLS handshake and is then discarded rather than pooled;
  production logged it as "Connection pool is full, discarding connection:
  api.ruckus.cloud". An `HTTPAdapter` is now mounted with the pool sized off
  the same expression, floored at 10 so a small box never ends up with less
  than the default. `R1_POOL_MAXSIZE` overrides. No retries were added: the
  calls would be safe to repeat, but a silent retry turns a failing R1
  endpoint into a slow one.

- **Spectrum chart paint order** — `src/pages/PISR.tsx` (`SpectrumChart`) and
  `api/reports/pisr.py` (`_spectrum`). Both sorted blocks by `inUse` alone, so
  states differing only by colour painted in channel order and a translucent
  not-permitted slot could cover a permitted-but-unused neighbour — 2.4 GHz
  channel 10 under channel 12. Both now sort by a `rank` of grey < green <
  blue < amber. The two renderers must keep matching each other; the PDF is
  meant to be the same picture as the screen.

- **The Config tab, and why it is split in two.** Venue-level settings (35
  categories, one per R1 endpoint) load with the report. AP-group and per-AP
  settings do NOT — they are one R1 call per object, and an MDU with a
  per-unit AP group would put several hundred requests behind every report for
  a tab most readers never open. `/pisr/{cid}/config/detail` fetches them on a
  button press, capped at `fetch.AP_CONFIG_LIMIT` APs.

  The bulk `/venues/aps/query` cannot substitute: it ACCEPTS the nested field
  names (`radio`, `clientAdmissionControl`, `useVenueSettings`) and echoes them
  in its `fields` reply, then returns none of them. Verified 2026-08-28 — do
  not spend an afternoon rediscovering that.

  **The detail route repeats every control the report route applies** — the
  scope check and the scrub — because it is a second, independent path to R1
  data. The PDF route taught that lesson already.

  Categories are grouped by ENDPOINT, not by meaning. A settings dump has no
  natural taxonomy, R1's console groups these differently again, and a third
  grouping invented here would leave a reader unable to map the tab onto
  either. It also makes each category a unit an admin can hide — and
  `redact.py` filters `config.categories` by slug, because they live in a list
  and no dotted path can own one.

  `GET /venues/aps/{serial}` is the AP config. Its sibling
  `GET /venues/{venueId}/aps/{serial}` is a different object whose payload is
  the AP's plaintext `loginPassword`, and is never called.

  **The venue-config reads are registered individually in `build_report`'s
  `reads` dict, not fetched behind their own thread pool.** They were, briefly,
  and it produced exactly the failure documented under "R1 connection pool"
  below: a second uncoordinated pool plus the main fan-out pushed concurrent
  requests past `pool_maxsize`, and urllib3 logged "Connection pool is full"
  while discarding connections. One executor, one bound. Nineteen extra reads
  cost nothing measurable that way — the report is faster now than it was with
  six.

- **Credentials are scrubbed at the FETCH as well as at the boundary.**
  `redact.redact` scrubbing every report is the guarantee, but between
  `build_report` and that boundary a secret would sit in the report object —
  which `checks.run_checks` walks, and which copies fields into finding
  evidence. So `fetch.radius_server_profiles` and `fetch.venue_config` scrub on
  the way in, and the boundary scrub goes back to being what it should be: a
  backstop that normally finds nothing. If it ever logs a warning, something
  upstream started leaking.

- **`GET /radiusServerProfiles` returns plaintext `sharedSecret`**, nested
  under `primary`/`secondary`, in a list. It is also TENANT-WIDE — R1 offers no
  venue filter — so the Config tab labels it as such rather than letting a
  reader take it for this venue's configuration.

- **Get the RUCKUS ONE OpenAPI document and READ IT BEFORE PROBING.** Export
  the consolidated API spec from RUCKUS and drop the JSON in `spec/`, which is
  gitignored — it is a 7MB vendor artefact that RUCKUS regenerates, so it is
  not carried in this repo. 956 paths and 483 GETs, and it is the difference
  between an afternoon of guessing and a grep. Two rounds of name-guessing missed seven venue-level
  settings endpoints that were there all along, because R1 is inconsistent
  about its prefixes — `syslogSettings` but `apModelUsbPortSettings`,
  `rogueApSettings` but `apRebootTimeoutSettings`, `ledSettings` but
  `apModelLedSettings` (both exist and return the same thing).

      python3 -c "import json;d=json.load(open('spec/…json'));
        print([p for p in d['paths'] if 'venues' in p and 'syslog' in p.lower()])"

  Only two things on the original wanted list have no venue-level endpoint,
  and the spec says why: **IoT controller** is per-AP
  (`/venues/{v}/aps/{serial}/iotSettings`) and **location-based services** is
  a tenant-wide profile object (`/lbsServerProfiles/query`). Both are recorded
  in `fetch.VENUE_CONFIG_NOT_FOUND` and surfaced in the payload so the tab can
  say what it looked for.

- **`GET /venues/{venueId}/aps/{serialNumber}/passwords` exists. Never call
  it.** The spec calls it "Get AP Password". Nothing in PISR has any use for
  it, and the scrubber should not be the reason it is safe.

- **Config values are labelled and compared, not dumped.** `api/config_labels.py`
  turns R1's keys into prose and its values into readable text; `api/baselines.py`
  puts two "recommended" columns beside them.

  **The label map is deliberately NOT exhaustive.** Roughly a hundred keys are
  named explicitly; everything else de-camelCases, so `serverLossTimeout` reads
  as "Server loss timeout". That fallback is the load-bearing part — R1 adds
  fields without warning, and a hardcoded layout would make a new one silently
  disappear, which is precisely what this tab exists to prevent. An unlabelled
  field looks slightly worse; a missing field looks like a setting that does
  not exist.

  The raw path travels with every row. An installer does not need it; the
  person cross-referencing the R1 console or the spec cannot work without it.

  **Baselines are keyed on `<endpoint>.<dotted path>`, never on labels.**
  Labels are prose and change; a baseline keyed to prose drifts silently, which
  for a "recommended value" column means quietly comparing against nothing.

  **The RUCKUS baseline ships as placeholders and says so.** `status:
  "placeholder"` in `api/baselines/ruckus.json`, surfaced in the UI on every
  column header until someone sources the real guidance and sets `"verified"`.
  There is a test asserting it has not gone green by accident. A fabricated
  "RUCKUS recommends" in front of an install crew is worse than an empty
  column — an empty column asks a question, a wrong one answers it.

  **The customer's name is `PISR_ORG_NAME` and their baseline is a mounted
  file.** Neither belongs in this repository. Unset, the column reads "Org".

- **R1 returns live credentials in ordinary config responses.** Observed on a
  live tenant, unmarked and with no opt-out:

      GET /venues/{id}/switchSettings  -> switchLoginPassword
      GET /venues/{id}/aps/{serial}    -> loginPassword
      GET /venues/{id}/wifiSettings    -> apPassword

  The third was found by the scrubber itself, after the Config tab started
  reading `wifiSettings` for its other nineteen keys — which is exactly the
  case the backstop exists for, and the reason its warning is worth reading.

  Those are working admin passwords for a customer's switches and APs. Any
  report carrying one puts it in a JSON response, in a PDF that gets emailed
  around, and in a browser cache — for a tool built to be handed to an install
  crew. **`api/scrub.py` runs over every report inside `redact.redact`**,
  unconditionally and regardless of role: no role here is entitled to a
  customer's switch password, so this is not part of the visibility policy.

  It is a BACKSTOP, not the control. The shapers allowlist what they emit; the
  scrubber catches the next person who passes a config dict through without
  reading every key, and R1 adding a field to an endpoint PISR already reads.
  **If a config block exists only to be scrubbed, do not fetch it at all.**

  **Matching is tokenised, and that was learned the hard way.** The first
  version used a bare substring list including "psk" — which matched `dpsk`,
  redacted the entire DPSK card and made the PDF fail to render. Compound terms
  ("password", "sessionkey") match anywhere in the flattened key because
  `switchLoginPassword` buries the word; short terms ("psk", "token") match
  only as whole camelCase tokens. A false positive here is not a safe trade:
  it deletes real content silently.

- **The punch list** — `api/services/pisr/punchlist.py`, its own tab, leftmost
  and the one a venue lands on. It ADDS NO DATA: every task is a finding
  `checks.py` already produced or an alarm R1 already raised, re-cut by TRADE
  instead of by subsystem. A port error and a mesh fallback are the same visit
  with the same ladder; a firmware mismatch is a different person who is
  probably not on site. If a task is wrong, the bug is in the check.

  **The PDF must not print the same finding twice.** On screen the Punch list
  and Overview tabs are separate places a reader chooses between, and repeating
  findings across them costs nothing. In one linear document it is the same
  warnings again three pages later, and a reader who has to work out whether
  the second list is new information stops trusting both. So in the template
  `punchlist_shown` gates three things: the actionable findings loop collapses
  to "Checks that passed", the skipped list drops (the punch list carries it),
  and the standalone alarms table goes entirely. Every guard has a
  `not punchlist_shown` fallback — hiding the punch list by policy must not
  take the findings and alarms with it, and there is no test for that, so
  check it by hand if you touch those guards.

  **`redact.py` REBUILDS it rather than filtering it.** The punch list is
  derived from `verification` and `incidents`, so after those are filtered it
  is regenerated from the redacted copies. Filtering it separately would be a
  second implementation of the same rule, and the failure mode is a task
  naming a finding the reader is no longer shown — precisely the leak the
  whole design exists to prevent. The rebuild is skipped when the punch list
  is itself hidden, or it would refill the path that was just emptied.

  Passes are counted, not listed. Skipped checks ARE listed, separately: on an
  install "could not be checked" usually means a prerequisite is missing, and
  reading it as a pass is how a venue gets signed off half-done.

  A check missing from `CHECK_CATEGORY` falls into "Devices not up" — over-
  reported rather than filed where nobody looks — and
  `test_every_check_has_a_category` stops that becoming normal.

  **No history, and that is a design boundary, not an oversight.** There is no
  "fixed since yesterday" and no ticking items off, because PISR stores
  nothing. Making the list stateful means giving PISR somewhere to write, which
  is a decision about the whole tool. The honest workaround is exporting the
  PDF at the end of each visit.

- **Mesh fallback, uptime and tags on APs** — `meshRole`, `uptime` and `tags`
  were fetched and shaped from the beginning and rendered nowhere. A meshing AP
  is the install defect that passes every other check in the report: online,
  provisioned, broadcasting, serving clients, with a dead cable behind it.

  `shape._is_meshing` is deliberately conservative — only values that clearly
  mean meshing count, and an unrecognised `meshRole` is treated as wired. The
  live tenant only ever returns `DISABLED`, so the rest of the vocabulary is
  inferred, and the cost of a false positive is a crew pulling a good cable.

  **`uptime` is assumed to be SECONDS.** R1 does not label the unit. Live
  values cluster at 1.9M–6.5M, which is 22–75 days and plausible for a settled
  fleet; read as milliseconds the same numbers are 30 minutes to 2 hours, which
  no mixed fleet clusters into. If every AP starts reporting "32 minutes", this
  is the assumption to revisit.

  `check_ap_uptime` is RELATIVE, not absolute, and that is the point: during an
  install every AP has just booted, so an absolute threshold would fire on
  every AP on the day the report is most likely to be run. Comparing each AP
  against the venue median keeps it silent through commissioning and only
  speaks once there is a settled fleet for an outlier to stand out from.

- **RUCKUS ONE alarms** — `fetch.incidents` / `shape.incident_card`, shown on
  Overview beside Verification. `POST /alarms/query` (read-only), filtered by
  `filters.venueId`, which genuinely scopes — a 16-alarm tenant cut to the 4
  belonging to one venue. Verified live 2026-08-28.

  **The endpoint was found by probing, not documentation.** Everything obvious
  404s: `/incidents`, `/incidents/query`, `/venues/{id}/incidents`,
  `/aiOps/incidents`, `/events`, `/alarms`. `/events/query` DOES exist but
  returned `EVENT-10002` on every attempt, so it is not used. `/alarms/query`
  is the one that works.

  Field names were established the same way: the endpoint echoes back a
  `fields` list containing only the names it recognises, so offering it a wide
  set and reading the reply enumerates the schema. Valid: `id`, `name`,
  `message`, `reason`, `severity`, `entityType`, `entityId`, `serialNumber`,
  `apMac`, `model`, `venueId`, `tenantId`, `startTime`. Notably absent —
  **no status, no clearedTime, no acknowledged flag.** This is the ACTIVE
  alarm list and nothing more; do not build a history or "recently cleared"
  view on it without re-probing first.

  `message` is a JSON *string* wrapping a template with `@@apName` style
  placeholders that the R1 console substitutes from its own context. Left
  alone they render literally. `_alarm_text` unwraps and substitutes them from
  the AP and switch names the report already holds, falling back to the
  serial. **No placeholder may reach the UI** — there is a test for it.

  `startTime` is epoch milliseconds and arrives as an int *and* as a float
  (`1785702964230.0`), hence the `int()` in `_epoch_ms_iso`.

  `entityType` includes `EDGE`. PISR has no Edge inventory, so those alarms
  name a bare serial — honest, and the reason the fallback exists.

  Kept separate from `verification` on purpose: that is PISR's opinion about
  whether the install looks finished, this is the platform's about whether the
  venue is healthy now. They disagree usefully and merging them would lose that.

- **Wired clients** — `api/services/pisr/{fetch,collect,shape}.py`,
  `api/reports/pisr.py`, the PDF template and `PISR.tsx`. PISR read wireless
  clients only: `/venues/aps/clients/query` is AP-scoped despite its service
  being named for both, so a report could see every association and nothing
  plugged into a wall. `fetch.wired_clients` adds the switch MAC table
  (`POST /venues/switches/clients/query` — read-only, a `*/query` path), which
  `r1api` already implemented and nothing used.

  **A ROW IS A LEARNED MAC, NOT A CLIENT.** An AP's uplink port has learned
  every wireless client behind it, the APs and switches are in the table as
  addresses themselves, and a port feeding another switch has learned
  everything behind that. `len(rows)` would therefore be several times the
  number of things plugged in, and would move when someone joined the Wi-Fi.
  `shape.wired_client_card` classifies instead, using the LLDP AP-to-port join
  and the AP/switch MACs the report already holds, and publishes the excluded
  counts so the three figures reconcile in public. The third class — an
  unmanaged switch downstream — is NOT separable and is left in; it shows up as
  a port with an implausible address count, which is what `topPorts` is for.

  `clientIpv4Addr` is the only MAC->IP binding R1 offers (no ARP endpoint) and
  its coverage runs from about a third to most of the table, so the card
  reports the count rather than treating a blank as "no IP".

- **Wired and PoE are separate tabs** — they were one "Wired & PoE" tab.

      Wired   port tiles, wired clients, link speeds, port errors, VLANs
      PoE     capacity/allocated/drawn tiles, budget per switch,
              PoE standard in use, APs on switch ports

  The split is by question, not by cable: Wired is "what is on the wire and is
  it healthy", PoE is "is there enough power and who is drawing it". Port
  health briefly sat on the PoE tab and moved back — it is not a power
  question, and `poe.summary` lost its "Ports up" tile for the same reason.
  Section ids are
  `<tab>.<thing>` and the test enforces it, so every PoE section had to be
  renamed; `visibility.RENAMED` migrates a stored policy so a section an admin
  had hidden does not silently become visible. **Add an entry there whenever
  you rename a section id** — the failure otherwise lands at the next report,
  not at the rename, and nobody connects the two. A section that is *deleted*
  belongs nowhere in that map; it should drop, and it does.

  "Biggest PoE draws" was removed outright. `poe.topConsumers` is still shaped
  and still in the payload, now owned by no section — harmless, and cheaper
  than another divergence in `shape.py` to delete it.

- **Channel plan by width** — `api/services/pisr/shape.py` (`radio_card`) and
  `src/pages/PISR.tsx`. The band tallies counted channels and widths
  independently, which cannot answer "which channels at which width" — and on a
  band running more than one width those are different questions, since a
  40 MHz and a 20 MHz radio on the same channel number are not co-channel in
  the way a flat list implies. `byWidth` adds the join; `channels` and `widths`
  are untouched, because the spectrum chart and the checks read those. The card
  renders buckets only when a band has more than one width, so the ordinary
  single-width site looks exactly as it did.

  A radio with a channel but no readable width gets its own bucket rather than
  being folded into 20 MHz — do not "fix" that by reaching for `_width_mhz`,
  which falls back to 20 by design for the spectrum chart and would file
  unknown-width radios under a width they may not be on. The buckets are meant
  to sum to the band's radio count.

  The width label is also now built from the digits rather than interpolated
  around R1's raw `channelBandwidth`, which rendered "20MHz MHz" whenever R1
  already carried the unit. Where R1 sends a bare number the output is
  identical, so the change only shows where it was already wrong.

  The PDF does NOT render this list — it draws the spectrum chart from
  `radios.plan` instead — so this divergence is screen-only.

- **Section visibility markup** — `src/pages/PISR.tsx` and
  `api/templates/reports/pisr.html`. Both carry section ids: `Card` takes an
  `id` and returns null when hidden, non-card blocks are wrapped in `Section`,
  and the template guards blocks with `{% if visible('...') %}` /
  `{% if visible_tab('...') %}`.

  The guards are COSMETIC. `redact.py` has already emptied the data, so a guard
  that was forgotten leaves an empty table rather than a leak. What they buy is
  the difference between "there are none" and "you are not shown these".

  `visible`/`visible_tab` are injected at `template.render()` in `pisr_router`
  rather than added to `build_context`. That began as a way to leave
  `api/reports/pisr.py` untouched; it is worth keeping anyway, because the
  guards are a rendering concern and the context builder has no other reason
  to know a policy exists.

- **`break-words` on the `Finding` body** — a finding can carry an unbreakable
  token in its title *and* in its summary; an R1 alarm names its device in
  both, and a RUCKUS Edge serial is 34 characters with no break opportunity.
  `overflow-wrap` inherits, so the class sits on the container that wraps
  title, summary and detail — fixing only the title left the summary widening
  the card. Found by the 320px check below, which is the only way it shows.

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

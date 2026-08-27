# PISR — Property Install Status Report

A read-only poll of one RUCKUS ONE venue: what was installed, what is online, how
it is addressed, what VLANs and PoE it uses, which SSIDs are activated, and which
of those are demonstrably carrying traffic. Then it checks about thirty things
about all of that and tells you which ones look wrong.

Extracted from [rtools2](https://github.com/wifientist/rtools2) into a
single-purpose tool: no user accounts, no database, no Redis, no scheduler.
Credentials come from `.env`, and one shared passphrase stands in front of the
whole thing.

## Quick start

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env                 # R1_TENANT_ID, R1_CLIENT_ID, R1_SHARED_SECRET
                             # and PISR_AUTH_PASSPHRASE
docker compose up --build
```

Open <http://localhost:8090>, enter the passphrase, pick a venue, wait a few
seconds, read the report. "Download PDF" gives you
`site-review-<venue>-<timestamp>.pdf`.

The container will not start without `PISR_AUTH_PASSPHRASE` set. That is
deliberate — see [Securing this app](#securing-this-app).

For hot reload while working on it:

```bash
docker compose -f docker-compose.dev.yml up
```

Frontend on <http://localhost:4173>, API on <http://127.0.0.1:4174>, both
reloading on save.

## What it guarantees

**Read-only by construction.** Every call PISR makes is a `GET` or a `POST` to a
`*/query` path. It creates nothing, changes nothing, activates nothing, reboots
nothing, and stores nothing — no snapshot files, no database rows, no cache. A
report exists for the length of one HTTP response.

You can verify this rather than take its word for it: set `R1_VERBOSE=1`, run a
report, and grep the log for any method that isn't a `GET` or a `POST` to a
`*/query` path. There should be nothing.

**Human-triggered only.** Every endpoint runs once per request. There is no
scheduler, no background task, and no recurring-poll entry point for anything to
call. The page refreshes when someone clicks refresh.

## Architecture

One container, one process. FastAPI serves both the API and the built SPA from
the same origin, so there is no nginx, no node at runtime, no CORS, and no proxy
timeout to tune.

```
src/pages/PISR.tsx          the whole UI — six tabs, ~20 private components
   │  GET /api/pisr/{id}/venues · /report · /report.pdf
   ▼
api/routers/pisr_router.py
   ├─ api/config.py          the pseudo-controller, from .env
   ├─ api/r1_client.py       builds an R1Client per request
   └─ api/services/pisr/
        ├─ collect.py        fans 21 reads out concurrently, assembles
        ├─ fetch.py          every read PISR makes (stdlib only)
        ├─ shape.py          raw R1 rows -> card payloads (stdlib only)
        └─ checks.py         ~30 checks over the shaped report (stdlib only)
   └─ api/reports/pisr.py -> templates/reports/pisr.html -> WeasyPrint -> PDF
```

`api/r1api/` is the RUCKUS ONE client, copied wholesale from rtools2 and
unmodified. PISR uses four of its service methods; the rest is carried along so
the package stays diffable against its origin.

## Things worth knowing before you change something

**Why a backend exists at all.** RUCKUS ONE enforces a strict CORS origin
allowlist at its edge — only `https://ruckus.cloud` is permitted. A page opened
from `file://` (origin `null`) or served from `localhost` gets a 403 on
preflight with no `Access-Control-Allow-Origin` header. So a browser cannot call
R1 directly, and a purely static build of this tool is not possible. Something
outside the browser has to make the calls.

**Never cache the R1Client at module level.** `R1Client` authenticates once in
`__init__` and never re-authenticates — `_request` never inspects a response for
a 401. A process-lifetime client would serve a token that expires about an hour
after start and then fail every request until restarted. Building one per request
is nearly free (the token cache is process-wide, so a hit does no HTTP at all).
See the comment on `build_r1_client` in `api/r1_client.py`.

**`R1_EC_TYPE` must be exactly `EC` or `MSP`.** It's compared as a literal string
in three places. A lowercase `msp` would silently drop the `x-rks-tenantid`
header and report on the MSP's own venue-less tenant — an empty report with no
error anywhere. `config.py` uppercases and validates it for this reason.

**`R1_VERBOSE` is read directly by `api/r1api/client.py`.** Renaming it means
editing a file that is otherwise a verbatim copy.

**The `{controller_id}` in the URLs is a vestige.** In rtools2 it selected one of
a user's saved controllers. Here there is only ever one, from `.env`. It is kept
so `pisr_router.py`, `PISR.tsx` and `useSingleEc.tsx` stay diffable against their
originals — those three files are byte-identical to rtools2 apart from the router
edits, and keeping them that way makes pulling upstream changes a readable merge.

**The static mount in `main.py` must stay last.** Starlette matches routes in
order and a `Mount("/")` matches everything; move it above the routers and every
API call quietly returns `index.html` with a 200.

**Credentials are plaintext in `.env`.** rtools2 kept them Fernet-encrypted in
Postgres. That machinery went with the database. This is the right trade for a
single-purpose tool, but it means `.env` is the whole secret store — it is in
`.gitignore`, it should stay there, and it should be `chmod 600`. See
[Securing this app](#securing-this-app) for how to get it off the disk of the
working tree entirely, and for everything else that deployment needs.

## MSP tenants

Set `R1_EC_TYPE=MSP`. The UI then shows an EC picker before the venue picker,
and every read is scoped to the chosen EC with the `x-rks-tenantid` header. An
MSP account owns no venues of its own, which is why the picker is mandatory
rather than optional.

## Securing this app

Two different things need protecting and they fail in different ways.

**The credential.** `.env` holds a RUCKUS ONE client ID and shared secret. PISR
is read-only by construction, but the credential need not be — a copy of it is
worth whatever the role behind it can do. That is a *secrets* problem.

**The report.** A venue report is AP serials, MACs, IP addressing, VLANs, SSIDs
and connected clients for a property. Anyone who can reach the port and get
past the gate sees all of it, and PISR will happily make the RUCKUS ONE calls
on their behalf. That is an *access* problem, and it is the larger one: an
attacker who can use PISR does not need to steal the credential.

Neither is solved by the other. The sections below take them in the order you
would actually do them.

### If you are deploying this, do these

| | Why |
|---|---|
| `chmod 600 .env` | It is the whole secret store |
| Scope the R1 credential to a **read-only** role | Makes a leak cost what the tool costs, not what an admin costs |
| Set `PISR_AUTH_PASSPHRASE` | Not optional — the container will not start without it |
| Set `PISR_BIND` to one interface | Fewer people can reach it at all |
| Get a certificate, set `PISR_COOKIE_SECURE=1` | Otherwise the passphrase crosses the wire in clear text |
| Leave `R1_VERBOSE=0` | It logs tenant data to disk |

Everything after this is detail on those six lines, plus what to do when a
shared passphrase stops being enough.

### Access control

PISR serves a full venue inventory — AP serials, MACs, IP addressing, VLANs,
SSIDs, connected clients — to anyone who can reach its port. It publishes on
every interface so the rest of the LAN can use it. Those two facts together are
why there is a gate, and why the gate is not optional: an unset
`PISR_AUTH_PASSPHRASE` stops the container from starting rather than quietly
opening the inventory to the network.

**What it is.** One shared passphrase, exchanged once at `POST /api/login` for
a signed, HttpOnly, SameSite=Lax session cookie. `SessionGateMiddleware` in
`api/auth.py` requires that cookie on everything under `/api`, plus `/docs`,
`/redoc` and `/openapi.json`. The cookie carries its own expiry and its own
HMAC signature, so the process holds no session state and a restart loses
nothing. Five wrong guesses from one IP earn a five-minute lockout.

Two paths are deliberately open: `/healthz`, which the container healthcheck
needs and which says nothing but `ok`; and the SPA bundle itself, which has to
load in order to render the login form and which contains no tenant data.
Everything that names the tenant — `/api/config`, `/api/status`, every report
route — is behind the cookie.

**What it is not.** A shared passphrase is not identity. It does not record who
ran a report, and it cannot be revoked for one person without changing it for
everyone. Rotating `PISR_AUTH_PASSPHRASE` or `PISR_SESSION_SECRET` invalidates
every outstanding session immediately, which is the whole revocation story.
When that stops being enough, see [SSO](#sso).

### SSO

`PISR_AUTH_MODE=proxy` hands authentication to a reverse proxy in front —
normally [oauth2-proxy](https://github.com/oauth2-proxy/oauth2-proxy) against
Entra, Okta or Google Workspace. The proxy does the whole OIDC exchange and
forwards the caller's identity in a header; PISR reads the header and nothing
else. It validates no tokens, fetches no JWKS, holds no client secret and has
no callback route, because implementing OIDC inside a read-only reporting tool
is how you acquire a security surface you did not need.

Concretely, that is one container and four settings:

```yaml
services:
  pisr:
    build: .
    env_file: ${PISR_ENV_FILE:-.env}
    # NOT 0.0.0.0 and NOT a published port. Only the proxy may reach it.
    expose: ["8080"]

  auth:
    image: quay.io/oauth2-proxy/oauth2-proxy:latest
    ports: ["443:4180"]
    command:
      - --provider=oidc
      - --oidc-issuer-url=https://login.microsoftonline.com/<tenant>/v2.0
      - --upstream=http://pisr:8080
      - --set-xauthrequest          # emits X-Forwarded-Email
      - --email-domain=yourcompany.com
      - --http-address=0.0.0.0:4180
    environment:
      OAUTH2_PROXY_CLIENT_ID: ...
      OAUTH2_PROXY_CLIENT_SECRET: ...
      OAUTH2_PROXY_COOKIE_SECRET: ...
```

```
PISR_AUTH_MODE=proxy
PISR_TRUSTED_PROXY_HEADER=X-Forwarded-Email
PISR_TRUSTED_PROXY_IPS=172.20.0.0/16      # the compose network
PISR_PROXY_LOGOUT_URL=/oauth2/sign_out
```

**The header is a claim, not proof.** Anyone who can open a socket to PISR can
send `X-Forwarded-Email: someone.important@corp.example`. Two things stop that
and you need both: `PISR_TRUSTED_PROXY_IPS`, which is required in proxy mode
and has no default, and the port binding — `expose`, not `ports`, so PISR is
reachable only from the proxy's network. Publishing PISR on `0.0.0.0` in proxy
mode turns the proxy into a suggestion.

In proxy mode the passphrase is switched off and `/api/login` returns a 400
saying so. Leaving both doors open would make the shared secret a way around
SSO, which throws away the audit trail and per-person revocation that were the
reasons to adopt it.

**What SSO actually buys here.** Everyone who gets in sees the same one
tenant's report — there is no per-user authorization, because there are no
differing permission levels. What changes is that there is no shared secret to
rotate when someone leaves, the report log names who ran it (`pisr: user=...`
in `pisr_router.py`, `-` under a shared passphrase), and a security review has
an answer to "how does this authenticate."

**The long pole is not the wiring.** It is getting corporate IT to register an
application and issue a client ID and secret. Plan accordingly.

**It needs TLS to mean much.** Over plain HTTP the passphrase and the cookie
both cross the wire in the clear. On a trusted office LAN that is a considered
trade; on guest wifi or the public internet it is not one to make. If PISR ever
needs to be reachable from outside a network you control, put an
identity-aware proxy in front of it — Cloudflare Access, Tailscale, or a
reverse proxy terminating TLS — and set `PISR_COOKIE_SECURE=1` so the browser
refuses to send the cookie over anything but HTTPS.

### TLS

The gate is only as private as the transport under it. Over plain HTTP both the
passphrase and the session cookie cross the wire readable by anyone positioned
to see them. On a switched corporate LAN that takes port mirroring or ARP
spoofing rather than idle curiosity — a considered trade, not a free one, and
not one to make on guest wifi.

Options, best first, and the best one depends on where you are:

**A certificate from your corporate PKI.** If PISR is going on a corp network,
the organisation almost certainly already runs an internal CA that every
domain-joined machine already trusts. Ask IT for a cert for
`pisr.corp.internal`. Nothing to install on clients, nothing in public
Certificate Transparency logs, no public DNS record, no outbound internet
needed to renew. In a corporate setting this beats everything below it.

**Let's Encrypt over the DNS-01 challenge.** If you own a domain but have no
internal CA, you can get a publicly-trusted certificate for a host that
resolves to a private address — DNS-01 proves domain control with a TXT record
and never needs inbound reachability. Caddy does it in about eight lines with a
DNS-provider module. Two costs worth knowing: the hostname becomes a matter of
public record in CT logs, and a public A record pointing at `192.168.x.x` tells
a reader something about your addressing. Split-horizon DNS fixes the second,
not the first.

**Your own CA.** `step-ca` runs a private ACME server that Caddy renews against
automatically — the right answer if internal services will accumulate. `mkcert`
is the five-minute version with no renewal story. Either way the root CA has to
land in the trust store of every device that browses to PISR, which is trivial
with MDM and a recurring support task without it. Phones are the annoying part.

**Self-signed.** It stops passive sniffing and does not stop an active attacker.
Mostly it is worth avoiding on different grounds: PISR's entire job is telling
people what is misconfigured, and training its users to click through
certificate warnings undercuts that. Use it knowing it is a placeholder.

**Or skip TLS and shrink the exposure instead.** Behind a VPN, on a management
VLAN, bound to one interface, with the passphrase in front — that is a coherent
position for an internal tool and a defensible one in a review. It is a real
choice, not a failure to get around to TLS.

Any certificate path wants a reverse proxy in front, and Caddy is the easy pick
— roughly ten lines and it renews on its own. That does contradict the "no
nginx, one container" claim in [Architecture](#architecture); update it rather
than let the README quietly lie. The reason that claim existed was proxy read
timeouts, and Caddy's `reverse_proxy` does not impose nginx's 60-second
default — worth confirming against your slowest venue rather than taking this
paragraph's word for it, because a switch-port crawl is exactly the request
that would find such a limit.

Once there is a certificate, set `PISR_COOKIE_SECURE=1`. The cookie is then
marked `Secure` and a browser will not send it over plain HTTP at all. Setting
it *before* there is TLS locks you out — the cookie is set and then never sent.

### Network exposure

By default PISR publishes on `0.0.0.0`, which is what makes it reachable from
the rest of the LAN. That is the intent, and it is also exactly why the gate is
not optional.

`PISR_BIND` narrows it without touching `docker-compose.yml`:

```
PISR_BIND=127.0.0.1     # this host only
PISR_BIND=10.0.25.25    # one interface — the VPN side, say
```

This is a network control and not a substitute for the gate. Run both. A bind
address is the difference between "an attacker must already be on the right
network" and "an attacker must only be on some network", which is worth having,
but it authenticates nobody.

**In `PISR_AUTH_MODE=proxy` this stops being advice and becomes a requirement.**
Replace the `ports:` block with `expose: ["8080"]` so PISR is reachable only
from the proxy's network. A published port in proxy mode is an authentication
bypass: the identity header is a claim, and anyone who can open a socket can
make it.

### Secrets

`.env` is the entire secret store, and `R1_SHARED_SECRET` is the valuable thing
in it. Three levels, in increasing order of paranoia:

1. **A `chmod 600` `.env` next to `docker-compose.yml`.** The default. It is in
   `.gitignore` and excluded by `.dockerignore`, so it reaches neither a commit
   nor an image layer.

2. **The file outside the working tree**, so a stray `chmod -R`, a backup job
   or an editor swapfile cannot reach it:

   ```bash
   sudo install -d -m 0750 /etc/pisr
   sudo install -m 0600 .env /etc/pisr/pisr.env
   PISR_ENV_FILE=/etc/pisr/pisr.env docker compose up -d
   ```

3. **The credentials as mounted files rather than environment variables.**
   `docker inspect` and `/proc/1/environ` print the environment; they do not
   print `/run/secrets`. Every setting in `config.py` reads `<NAME>_FILE` in
   preference to `<NAME>`, so this is configuration, not code:

   ```
   R1_SHARED_SECRET_FILE=/run/secrets/r1_shared_secret
   PISR_AUTH_PASSPHRASE_FILE=/run/secrets/pisr_passphrase
   ```

   with the `secrets:` blocks in `docker-compose.yml` uncommented.

**Scope the RUCKUS ONE credential to a read-only role.** This is worth more
than any of the above. PISR is read-only by construction, but a credential
minted from a full-admin role is not — a copy of it can reboot APs whatever
PISR does with it. A read-only API client makes the blast radius of a leak
match the guarantee the tool advertises.

**`R1_VERBOSE=1` writes tenant data to the container log.** Not credentials —
the authentication exchange is outside the verbose path — but response bodies
including client MACs and addressing, retained by the json-file driver at 2 MB
x 3. Fine for a debugging session, not a setting to leave on.

## Diagnostics

```bash
# does a live tenant populate the fields the report is built from?
docker compose run --rm pisr python scripts/probe_pisr.py

# is the process alive? (public — this is what the healthcheck uses)
curl localhost:8090/healthz

# everything under /api needs a session, so get one first
curl -c jar -X POST localhost:8090/api/login \
     -H 'Content-Type: application/json' \
     -d '{"passphrase":"'"$PISR_AUTH_PASSPHRASE"'"}'

# the check catalogue, without running a report (touches R1 not at all)
curl -b jar localhost:8090/api/pisr/1/checks | jq

# what tenant is this pointed at?
curl -b jar localhost:8090/api/status
curl -b jar localhost:8090/api/config
```

## Provenance

Extracted from rtools2 at commit `c332420`. The report logic — `shape.py`,
`checks.py`, `fetch.py`, `collect.py`, `reports/pisr.py`, the PDF template, and
`PISR.tsx` — is carried across verbatim. What changed is only the layer that
used to authenticate users and look credentials up in a database.

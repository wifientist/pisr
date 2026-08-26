# PISR — Property Install Status Report

A read-only poll of one RUCKUS ONE venue: what was installed, what is online, how
it is addressed, what VLANs and PoE it uses, which SSIDs are activated, and which
of those are demonstrably carrying traffic. Then it checks about thirty things
about all of that and tells you which ones look wrong.

Extracted from [rtools2](https://github.com/wifientist/rtools2) into a
single-purpose tool: no user accounts, no database, no Redis, no scheduler.
Credentials come from `.env`.

## Quick start

```bash
cp .env.example .env
$EDITOR .env                 # R1_TENANT_ID, R1_CLIENT_ID, R1_SHARED_SECRET
docker compose up --build
```

Open <http://localhost:8090>. Pick a venue, wait a few seconds, read the report.
"Download PDF" gives you `site-review-<venue>-<timestamp>.pdf`.

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
`.gitignore`, and it should stay there.

## MSP tenants

Set `R1_EC_TYPE=MSP`. The UI then shows an EC picker before the venue picker,
and every read is scoped to the chosen EC with the `x-rks-tenantid` header. An
MSP account owns no venues of its own, which is why the picker is mandatory
rather than optional.

## Diagnostics

```bash
# does a live tenant populate the fields the report is built from?
docker compose run --rm pisr python scripts/probe_pisr.py

# the check catalogue, without running a report (touches R1 not at all)
curl localhost:8090/api/pisr/1/checks | jq

# what tenant is this pointed at?
curl localhost:8090/api/status
curl localhost:8090/api/config
```

## Provenance

Extracted from rtools2 at commit `c332420`. The report logic — `shape.py`,
`checks.py`, `fetch.py`, `collect.py`, `reports/pisr.py`, the PDF template, and
`PISR.tsx` — is carried across verbatim. What changed is only the layer that
used to authenticate users and look credentials up in a database.

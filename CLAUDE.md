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

## Architecture

- **Backend**: Python FastAPI (`api/`), serves the API *and* the built SPA
- **Frontend**: React 19 + TypeScript + Vite + Tailwind 3 (`src/`)
- **No database, no Redis, no scheduler, no auth.** Config is `.env`, read once
  at import by `api/config.py`.
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
- **`R1_EC_TYPE` and `R1_VERBOSE` are literal-compared** in `api/services/pisr/fetch.py`
  and `api/r1api/client.py` respectively. Don't rename `R1_VERBOSE`; don't
  lowercase `MSP`.
- **`_jinja()` in `pisr_router.py` walks two `.parent`s** to reach
  `api/templates`. It fails at PDF-request time, not import time.
- **WeasyPrint needs Pango at import.** If you trim the Dockerfile's apt list
  further, the container stops starting rather than the PDF endpoint breaking
  later.

## Files kept byte-identical to rtools2

`api/r1api/**`, `api/services/pisr/{collect,fetch,shape,checks}.py`,
`api/reports/pisr.py`, `api/templates/reports/pisr.html`, `src/pages/PISR.tsx`,
`src/components/SingleEcSelector.tsx`, `src/hooks/useSingleEc.tsx`.

Keep them that way where you can — it makes pulling upstream changes a readable
diff rather than an archaeology exercise. If you must diverge, note it here.

# ── Stage 1: build the SPA ───────────────────────────────────────────
FROM node:20-alpine AS web
WORKDIR /web

COPY package.json package-lock.json* ./
RUN npm install

COPY index.html vite.config.ts tsconfig.json tailwind.config.js postcss.config.js ./
COPY public ./public
COPY src ./src
RUN npm run build          # -> /web/dist


# ── Stage 2: runtime ─────────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

# WeasyPrint >= 53 renders through Pango directly. It needs NO cairo and NO
# gdk-pixbuf — rtools2's Dockerfile still installs both from an older era — and
# no Chromium, since that whole block there belongs to Playwright, which PISR
# does not use.
#
#   libpango-1.0-0     text layout (pulls in harfbuzz, glib, fribidi)
#   libpangoft2-1.0-0  the FreeType backend Pango renders through
#   fonts-dejavu-core  something for the default sans-serif stack to resolve to
#   shared-mime-info   MIME sniffing for resources embedded in the template
#
# Swap fonts-dejavu-core for fonts-noto-core if any tenant has venue names
# outside Latin-1. WeasyPrint checks for Pango at IMPORT time and raises if it
# is missing, so trimming this too far shows up as a container that will not
# start — not as a PDF endpoint that breaks in a week.

# Rootless Podman. apt-get drops to the unprivileged `_apt` user — uid 65534 —
# before it fetches anything, and in a rootless container that uid only exists
# if the subuid range mapped into the user namespace reaches it. Most ranges do
# not, and one that has to fit inside an LXC's own 0-65535 map has no room to,
# so the drop fails and the build dies in apt rather than in anything of ours.
# Telling apt to stay root skips the drop. It costs nothing under Docker, where
# the build is already root: the packages come from Debian's signed repos and
# this layer is a throwaway either way.
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/00sandbox

RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      fonts-dejavu-core \
      shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ /app/
COPY --from=web /web/dist /app/static

# Which commit this image is. Passed by docker-compose.yml from the build
# environment; "unknown" when someone builds by hand without it, which is
# honest rather than wrong.
#
# Recorded twice on purpose. The ENV is what the running process reports on
# /api/status, behind the gate. The LABEL is readable with `podman inspect`
# without a session cookie and without a request, which is what lets the
# deploy script confirm that the container now running is the commit it just
# built — the repo's HEAD cannot answer that, and diverges from the truth in
# exactly the cases worth catching.
ARG PISR_BUILD_SHA=unknown
ARG PISR_BUILD_TIME=
ENV PISR_BUILD_SHA=${PISR_BUILD_SHA} \
    PISR_BUILD_TIME=${PISR_BUILD_TIME}
LABEL org.opencontainers.image.revision="${PISR_BUILD_SHA}" \
      org.opencontainers.image.created="${PISR_BUILD_TIME}" \
      org.opencontainers.image.source="https://github.com/wifientist/pisr"

# /data holds one file: the section visibility policy (see api/visibility.py).
#
# Created HERE, owned by pisr, and that ordering is the whole point. Docker and
# podman seed a fresh NAMED volume from whatever the image has at the mount
# point, ownership included — so a directory that does not exist in the image,
# or exists owned by root, produces a root-owned volume that the uid-1000
# process cannot write. The failure is quiet: the container starts, the portal
# loads, and saving is the only thing that does not work.
#
# This does NOT help a BIND mount, where the host directory's ownership wins.
# If you bind-mount a config directory, chown it to 1000:1000 on the host (or,
# rootless, to the uid that maps to 1000 inside).
RUN useradd -m -u 1000 pisr \
 && mkdir -p /data \
 && chown -R pisr:pisr /app /data
USER pisr

EXPOSE 8080

# /healthz, not /api/status: everything under /api now requires a session
# cookie, and /api/status names the tenant, region and EC type besides.
#
# Under podman this is only honoured when the image is built in docker format —
# OCI has no healthcheck field and podman discards it with a warning. The
# deploy script exports BUILDAH_FORMAT=docker for that reason. A hand-built
# podman image without it simply has no healthcheck, which is a missing check
# rather than a broken one.
#
# Note what this can and cannot see. It runs INSIDE the container and asks
# localhost, so it answers "is the app serving", not "can anyone reach it" —
# during the rootlessport incident it would have passed throughout, while the
# published port was dead. The deploy script probes the published address from
# outside for exactly that reason; the two checks are not redundant.
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

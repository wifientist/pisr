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

RUN useradd -m -u 1000 pisr && chown -R pisr:pisr /app
USER pisr

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]

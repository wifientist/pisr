#!/usr/bin/env bash
#
# "Is PRISM itself healthy, and how is it configured?"
#
# Read-only, and safe during a deploy.
#
# The health probe deliberately hits the PUBLISHED address rather than
# localhost. Rootless podman publishes through a separate rootlessport process,
# and when that dies the container keeps running and serving to nobody — a
# state in which `podman ps` says Up and the container's own healthcheck passes,
# because it asks localhost from inside where nothing is wrong. Only the
# published address sees it.

set -uo pipefail

# Pre-rename (PISR_) settings still count; see deploy/legacy-names.sh.
_here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=legacy-names.sh
[ -r "$_here/legacy-names.sh" ] && . "$_here/legacy-names.sh"

ENV_FILE="${PRISM_ENV_FILE:-$(legacy_env_file "$HOME/prism-config/prism.env")}"
bar() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

# Derived exactly as deploy/prism-update.sh derives it — bind and port both come
# from the env file compose reads, because compose publishes on BIND:PORT and
# either half can be non-default.
_envget1() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\([^[:space:]#]*\).*/\1/p" \
              "$ENV_FILE" 2>/dev/null | tail -1; }
_envget() { local v; v="$(_envget1 "$1")"; [ -n "$v" ] || v="$(_envget1 "$(legacy_name "$1")")"; printf '%s' "$v"; }
port="$(_envget PRISM_PORT)"; bind="$(_envget PRISM_BIND)"
case "$bind" in
  ""|0.0.0.0|"*") host="127.0.0.1" ;;
  "::"|"[::]")    host="[::1]" ;;
  *:*)            host="[$bind]" ;;
  *)              host="$bind" ;;
esac
URL="${PRISM_HEALTH_URL:-http://${host}:${port:-8080}/healthz}"

# The container is `prism`, or `pisr` on a box not yet cut over from the old name.
CTR=prism
podman container exists prism 2>/dev/null || ! podman container exists pisr 2>/dev/null || CTR=pisr

bar "Container"
podman ps --filter name="^$CTR\$" --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null \
  || echo "  podman not available"
podman ps --filter name="^$CTR\$" --format '{{.Names}}' 2>/dev/null | grep -q . \
  || echo "  NOT RUNNING — start with: cd ~/app && podman-compose up -d"

bar "Reachable on its published address?"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null)"
if [ "$code" = "200" ]; then
  echo "  OK    $URL -> 200"
else
  echo "  FAIL  $URL -> ${code:-no response}"
  echo "        If the container says Up, its port forwarding is dead."
  echo "        Fix:  cd ~/app && podman-compose down && podman-compose up -d"
fi

bar "How the gate is configured"
podman logs "$CTR" 2>&1 | grep -i "Gate:" | tail -1 | sed 's/^/  /' \
  || echo "  (no Gate line — container may predate it)"

bar "Build actually running"
printf '  commit: %s\n' "$(podman inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$CTR" 2>/dev/null | cut -c1-12)"
printf '  built:  %s\n' "$(podman inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.created"}}' "$CTR" 2>/dev/null)"

bar "R1 connection pool"
podman logs "$CTR" 2>&1 | grep -i "pool sized" | tail -1 | sed 's/^/  /' \
  || echo "  (not logged yet — appears on the first R1 call after a restart)"
exhausted="$(podman logs "$CTR" 2>&1 | grep -c 'Connection pool is full')"
echo "  pool-exhausted warnings since start: $exhausted"

bar "Recent warnings and errors (404s are normal — see below)"
podman logs "$CTR" 2>&1 | grep -E '^(WARNING|ERROR)' | tail -8 | sed 's/^/  /'
echo
echo "  A 404 PROPERTY-MANAGEMENT-002 is NOT a fault: every venue is asked for"
echo "  its Property config and most venues do not have one. Logged at info."
echo
echo "  App log:  podman logs -f $CTR"

#!/usr/bin/env bash
#
# "Is the auto-deploy working, and what did it last do?"
#
# Read-only. Nothing here changes anything, so it is safe to run at any time,
# including during a deploy.

set -uo pipefail

APP_DIR="${PISR_APP_DIR:-$HOME/app}"
UNIT=pisr-update

bar() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

bar "Timer"
if systemctl --user is-enabled "$UNIT.timer" >/dev/null 2>&1; then
  systemctl --user list-timers "$UNIT.timer" --no-pager | sed -n '1,2p'
else
  echo "  TIMER IS NOT ENABLED — deploys are manual until:"
  echo "    systemctl --user enable --now $UNIT.timer"
fi

bar "Last run"
# Result=success with ExecMainStatus=0 is a run that finished; it does NOT
# mean it deployed anything. Most runs find nothing to do and that is correct.
systemctl --user show "$UNIT.service" \
  -p Result -p ExecMainStatus -p ExecMainExitTimestamp | sed 's/^/  /'

bar "Versions — these three should match"
cd "$APP_DIR" 2>/dev/null || { echo "  no such dir: $APP_DIR"; exit 1; }
git fetch -q origin main 2>/dev/null
running="$(podman inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' pisr 2>/dev/null)"
printf '  %-14s %s\n' "origin/main:" "$(git rev-parse --short origin/main 2>/dev/null || echo '?')"
printf '  %-14s %s\n' "local repo:"  "$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
printf '  %-14s %s\n' "container:"   "${running:0:7}"
if [ "$(git rev-parse HEAD 2>/dev/null)" != "$(git rev-parse origin/main 2>/dev/null)" ]; then
  echo "  -> repo is behind origin; the next tick should deploy it"
elif [ -n "$running" ] && [ "$running" != "$(git rev-parse HEAD 2>/dev/null)" ]; then
  echo "  -> container is behind the repo; the next tick should rebuild"
else
  echo "  -> up to date"
fi

bar "Recent deploy activity (podman chatter filtered out)"
journalctl --user -u "$UNIT.service" -n 200 --no-pager 2>/dev/null \
  | grep -vE 'podman\[|container (create|init|start|died|cleanup|remove|stop)|network (create|remove)|pod (create|remove)|Using cache|--> |STEP |Found left-over|deficiencies|remains running|image (build|pull)' \
  | tail -20 | sed 's/^/  /'

bar "Anything blocked or failing"
journalctl --user -u "$UNIT.service" -n 400 --no-pager -p warning 2>/dev/null \
  | tail -8 | sed 's/^/  /' || true
echo
echo "  Full log:  journalctl --user -u $UNIT.service -f"
echo "  Deploy now: systemctl --user start $UNIT.service"
echo "  Pause:      systemctl --user stop $UNIT.timer"

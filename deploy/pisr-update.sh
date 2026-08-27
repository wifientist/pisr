#!/usr/bin/env bash
#
# Pull-based deploy for PISR. Run on a timer by the user that owns the
# rootless podman containers; see deploy/README.md.
#
# WHY PULL AND NOT A WEBHOOK. The box reaches GitHub; GitHub does not reach the
# box. That is the same property cloudflared is bought for — the tunnel is an
# outbound connection, and nothing needs a port open inbound. A deploy webhook
# would need one, would need to be exempt from the session gate because GitHub
# cannot hold a session cookie, and would need the podman socket to do its job,
# which makes the internet-facing container host-root-equivalent. This script
# needs none of that.
#
# WHAT IT WILL NOT DO. It will not touch a dirty working tree, and it never
# runs `git clean` — the production .env lives untracked in the app directory
# and deleting it would be an outage with no obvious cause. `git reset --hard`
# leaves untracked files alone, which is exactly the behaviour wanted here.
#
# WHAT IT DOES ON A BAD BUILD. Building on the production box means a broken
# commit is an outage, so the deploy is not considered finished until /healthz
# answers. If it does not within PISR_HEALTH_TIMEOUT, the previous commit is
# rebuilt and brought back up, and the run exits non-zero so `systemctl --user
# status pisr-update` and the journal both show it.

set -euo pipefail

APP_DIR="${PISR_APP_DIR:-$HOME/app}"
BRANCH="${PISR_BRANCH:-main}"
REMOTE="${PISR_REMOTE:-origin}"
COMPOSE="${PISR_COMPOSE:-podman-compose}"
# The health URL is derived, not guessed. Hardcoding a port here would be a
# quiet trap: if it disagrees with the port compose actually publishes, every
# deploy looks unhealthy and rolls itself back, and the journal blames the
# commit rather than this line. So read PISR_PORT from the same env file
# compose reads, and fall back to the same default compose falls back to.
# Setting PISR_HEALTH_URL explicitly still wins over all of it.
if [ -z "${PISR_HEALTH_URL:-}" ]; then
  _env_file="${PISR_ENV_FILE:-${PISR_APP_DIR:-$HOME/app}/.env}"
  _port="$(sed -n 's/^[[:space:]]*PISR_PORT[[:space:]]*=[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
             "$_env_file" 2>/dev/null | tail -1)"
  PISR_HEALTH_URL="http://127.0.0.1:${_port:-8080}/healthz"
fi
HEALTH_URL="$PISR_HEALTH_URL"
HEALTH_TIMEOUT="${PISR_HEALTH_TIMEOUT:-120}"
ALLOW_DIRTY="${PISR_UPDATE_ALLOW_DIRTY:-0}"
LOCK_FILE="${PISR_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/pisr-update.lock}"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# One deploy at a time. A rebuild can outlast the timer interval, and two
# concurrent `up -d --build` runs on the same project is a good way to end up
# with no container at all.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another deploy is in progress; leaving it alone."
  exit 0
fi

cd "$APP_DIR" || die "PISR_APP_DIR=$APP_DIR is not a directory"
git rev-parse --git-dir >/dev/null 2>&1 || die "$APP_DIR is not a git repository"

# A dirty tree means someone edited the deployed copy by hand. Clobbering that
# silently is how a fix nobody wrote down disappears, so stop and say so.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  if [ "$ALLOW_DIRTY" != "1" ]; then
    git status --short --untracked-files=no >&2
    die "Working tree has local modifications — refusing to overwrite them.
     Commit or discard them, or set PISR_UPDATE_ALLOW_DIRTY=1 to lose them."
  fi
  log "Working tree is dirty; PISR_UPDATE_ALLOW_DIRTY=1 so discarding."
fi

git fetch --quiet "$REMOTE" "$BRANCH" || die "git fetch failed"

current="$(git rev-parse HEAD)"
target="$(git rev-parse "$REMOTE/$BRANCH")"

if [ "$current" = "$target" ]; then
  # The quiet path, and the one taken almost every time. Nothing is logged
  # beyond this so the journal stays readable.
  exit 0
fi

deploy() {
  # --build because this box builds its own image; there is no registry in
  # this deployment shape.
  $COMPOSE up -d --build
}

healthy() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

log "Deploying ${current:0:12} -> ${target:0:12} ($BRANCH)"
git log --oneline "$current..$target" | sed 's/^/    /'

git reset --hard --quiet "$target"

if deploy && healthy; then
  log "Deployed ${target:0:12}; $HEALTH_URL is answering."
  exit 0
fi

# Past here the new commit is either unbuildable or unhealthy. Neither is
# something a timer should leave running.
log "New commit ${target:0:12} did not come up healthy within ${HEALTH_TIMEOUT}s."
log "Rolling back to ${current:0:12}."

git reset --hard --quiet "$current"
if deploy && healthy; then
  die "Rolled back to ${current:0:12}, which is healthy. ${target:0:12} needs a look."
fi

die "Rollback to ${current:0:12} ALSO failed to come up. PISR is down and needs a human."

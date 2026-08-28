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
_envget() {  # last uncommented assignment of $1 in the env file, or ""
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\\([^[:space:]#]*\\).*/\\1/p" \
    "$_env_file" 2>/dev/null | tail -1
}

if [ -z "${PISR_HEALTH_URL:-}" ]; then
  _env_file="${PISR_ENV_FILE:-${PISR_APP_DIR:-$HOME/app}/.env}"
  _port="$(_envget PISR_PORT)"

  # The HOST has to be derived too, not just the port. compose publishes on
  # ${PISR_BIND}:${PISR_PORT}, and PISR_BIND is frequently a specific address
  # rather than a wildcard — an instance published on 10.10.77.108:8080 has
  # nothing at all on 127.0.0.1:8080, so a loopback health check fails against
  # a perfectly healthy container. Only a wildcard bind (or none) implies
  # loopback works.
  _bind="$(_envget PISR_BIND)"
  case "$_bind" in
    ""|0.0.0.0|"*") _host="127.0.0.1" ;;
    "::"|"[::]")    _host="[::1]" ;;
    *:*)            _host="[$_bind]" ;;   # a bare IPv6 literal needs brackets
    *)              _host="$_bind" ;;
  esac

  PISR_HEALTH_URL="http://${_host}:${_port:-8080}/healthz"
fi
HEALTH_URL="$PISR_HEALTH_URL"
HEALTH_TIMEOUT="${PISR_HEALTH_TIMEOUT:-120}"
ALLOW_DIRTY="${PISR_UPDATE_ALLOW_DIRTY:-0}"
LOCK_FILE="${PISR_LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/pisr-update.lock}"
HOSTNAME_="$(hostname 2>/dev/null || echo host)"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ── Slack, optional ──────────────────────────────────────────────────
#
# Outbound only, like everything else here: the box posts to Slack, Slack never
# reaches the box. Set PISR_SLACK_WEBHOOK_URL, or PISR_SLACK_WEBHOOK_URL_FILE
# pointing at a file containing it — the same <NAME>_FILE convention config.py
# uses, and worth using, because a webhook URL is a credential. Anyone holding
# it can post to that channel as this app.
#
# Nothing here can fail a deploy. Slack being down, slow or misconfigured is
# not a reason to leave production on the previous commit, so every failure
# path is swallowed and noted.
SLACK_URL="${PISR_SLACK_WEBHOOK_URL:-}"
if [ -z "$SLACK_URL" ] && [ -n "${PISR_SLACK_WEBHOOK_URL_FILE:-}" ]; then
  SLACK_URL="$(cat "$PISR_SLACK_WEBHOOK_URL_FILE" 2>/dev/null || true)"
fi
SLACK_URL="$(printf '%s' "$SLACK_URL" | tr -d '\r\n')"

_json_escape() {
  # Backslash first or it would escape the escapes. Then quotes, tabs, and
  # newlines — a commit subject is user-controlled text arriving in a JSON
  # document, and an unescaped quote in one would silently break the payload.
  printf '%s' "$1" \
    | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' \
    | awk 'BEGIN{ORS=""} {print (NR>1 ? "\\n" : "") $0}'
}

notify() {
  [ -n "$SLACK_URL" ] || return 0
  curl -fsS --max-time 10 -X POST -H 'Content-type: application/json' \
       --data "{\"text\":\"$(_json_escape "$1")\"}" "$SLACK_URL" >/dev/null 2>&1 \
    || log "  (Slack notification failed; the deploy itself is unaffected)"
}

# Blocking conditions repeat every tick until a human intervenes, so an
# unconditional message would be 288 identical Slack posts a day. Only the
# first of each distinct one is sent; the state resets on any run that gets
# far enough to attempt a deploy.
_BLOCK_STATE="${PISR_BLOCK_STATE:-${XDG_RUNTIME_DIR:-/tmp}/pisr-update.blocked}"
notify_blocked() {
  [ -n "$SLACK_URL" ] || return 0
  local seen=""
  [ -f "$_BLOCK_STATE" ] && seen="$(cat "$_BLOCK_STATE" 2>/dev/null || true)"
  [ "$seen" = "$1" ] && return 0
  printf '%s' "$1" > "$_BLOCK_STATE" 2>/dev/null || true
  notify "$1"
}

# Checked up front, because of how they fail if they are missing. A absent
# curl makes every health check fail, which makes every deploy roll back and
# report that the commit is bad — the tool that is actually missing is never
# named, and the same wrong conclusion is reached again on the next push. Two
# seconds of checking here saves that entire diagnosis.
for tool in git curl flock; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed, and this script cannot work without it."
done

# One deploy at a time. A rebuild can outlast the timer interval, and two
# concurrent `up -d --build` runs on the same project is a good way to end up
# with no container at all.
# Taking the lock, and recognising one that was never really taken.
#
# flock releases when the last fd on the inode closes, which is normally when
# this script exits — but any process that inherited the fd keeps it held. That
# happened here: before `9>&-` below, the container's own supervisors inherited
# it and held the lock for the life of the container, so every later run
# declined with "another deploy is in progress" while nothing was in progress.
#
# The `9>&-` on the compose call fixes the known source. This is the backstop
# for the unknown ones: the holder writes its pid into the file, and a run that
# cannot take the lock checks whether that pid is still a live copy of this
# script. If it is not, the lock is not held by a deploy at all — it was leaked
# into something that merely inherited it. Unlinking the file and reopening
# gets a fresh inode; the leaked fd keeps its lock on the old one, harmlessly,
# until whatever holds it exits.
_holder_is_live_deploy() {
  local pid
  pid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  [ -d "/proc/$pid" ] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'pisr-update' || return 1
  return 0
}

# 9<> and not 9>: `>` truncates on open, which would erase the holder's pid
# before the check below could read it, and make every genuinely-held lock look
# leaked — turning a safety net into the concurrency bug it exists to prevent.
exec 9<>"$LOCK_FILE"
if ! flock -n 9; then
  if _holder_is_live_deploy; then
    log "Another deploy is in progress; leaving it alone."
    exit 0
  fi
  log "Lock at $LOCK_FILE is held, but not by a running deploy — it was leaked"
  log "into a process that inherited it. Taking a fresh one."
  rm -f "$LOCK_FILE"
  exec 9<>"$LOCK_FILE"
  flock -n 9 || die "Could not take $LOCK_FILE even after replacing it."
fi

# Truncate-and-write through the path rather than through fd 9, whose offset is
# wherever the open left it. Safe: the lock is held by this process now.
printf '%s' "$$" > "$LOCK_FILE"

cd "$APP_DIR" || die "PISR_APP_DIR=$APP_DIR is not a directory"
git rev-parse --git-dir >/dev/null 2>&1 || die "$APP_DIR is not a git repository"

# A dirty tree means someone edited the deployed copy by hand. Clobbering that
# silently is how a fix nobody wrote down disappears, so stop and say so.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  if [ "$ALLOW_DIRTY" != "1" ]; then
    git status --short --untracked-files=no >&2
    notify_blocked ":warning: PISR deploy blocked on ${HOSTNAME_:-host} — the working tree in $APP_DIR has local modifications, so nothing is being deployed until someone commits or discards them."
    die "Working tree has local modifications — refusing to overwrite them.
     Commit or discard them, or set PISR_UPDATE_ALLOW_DIRTY=1 to lose them."
  fi
  log "Working tree is dirty; PISR_UPDATE_ALLOW_DIRTY=1 so discarding."
fi

git fetch --quiet "$REMOTE" "$BRANCH" || die "git fetch failed"

current="$(git rev-parse HEAD)"
target="$(git rev-parse "$REMOTE/$BRANCH")"

running_sha() {
  # What the CONTAINER is, not what the repository says. The two agree only
  # when the last deploy actually took — a build that failed, a container that
  # never got recreated, or a stale image all show up right here and nowhere
  # else. Read from the image label so this needs no session cookie and no
  # HTTP request.
  local engine="${PISR_ENGINE:-podman}"
  command -v "$engine" >/dev/null 2>&1 || { echo ""; return 0; }
  $engine inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${PISR_CONTAINER:-pisr}" 2>/dev/null || echo ""
}


# Deploy when the repository has moved OR when the container is not what the
# repository says it should be. The second half matters more than it looks:
# the script used to compare git to git only, so a `git pull` run by hand left
# HEAD equal to origin with the container still on the old image, and every
# subsequent tick concluded there was nothing to do. The repository was up to
# date and the running code was not, indefinitely, silently.
#
# The container's own label is the authority on what is actually serving. An
# empty answer — an image built before the label existed, or no podman — is
# treated as "cannot tell" and left alone rather than as drift, since deploying
# on every tick would be worse than deploying on none.
running="$(running_sha)"
drifted=""
if [ -n "$running" ] && [ "$running" != "$target" ] && [ "$current" = "$target" ]; then
  drifted="yes"
fi

if [ "$current" = "$target" ] && [ -z "$drifted" ]; then
  # The quiet path, and the one taken almost every time. Nothing is logged
  # beyond this so the journal stays readable.
  exit 0
fi

if [ -n "$drifted" ]; then
  log "Repository is at ${target:0:12} but the container is running ${running:0:12}."
  log "Rebuilding to close the gap — something deployed outside this script."
fi

deploy() {
  # Stamped into the image as both an env var and an OCI label; see the
  # Dockerfile. Exported rather than passed, because docker-compose.yml reads
  # them from the environment as build args.
  export PISR_BUILD_SHA="$1"
  export PISR_BUILD_TIME
  PISR_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # podman defaults to the OCI image format, which has no field for a
  # HEALTHCHECK — so the one in the Dockerfile was silently discarded, with a
  # warning on every build and no health status on `podman ps` ever. Docker's
  # manifest format has the field. The image is built locally and never pushed,
  # so the format costs nothing, and this keeps one Dockerfile working under
  # both engines rather than a healthcheck that only means something under one.
  export BUILDAH_FORMAT="${BUILDAH_FORMAT:-docker}"

  # --build because this box builds its own image; there is no registry in
  # this deployment shape.
  #
  # 9>&- closes the lock fd for this command and everything it spawns. See the
  # note by the flock above — without it the container's supervisor processes
  # inherit the lock and no later run can ever take it.
  $COMPOSE up -d --build 9>&-
}

confirm_running() {
  # Advisory, deliberately. A mismatch here means the deploy did not take, but
  # health already passed, so PISR is up and serving — rolling back over a
  # label would trade a working instance for a tidier one. Say it loudly and
  # let a person decide.
  local want="$1" got
  got="$(running_sha)"
  if [ -z "$got" ]; then
    log "  (could not read the running container's build label; skipping the check)"
  elif [ "$got" = "$want" ]; then
    log "  container is running ${got:0:12}, as expected."
  else
    log "  WARNING: expected ${want:0:12} but the container reports ${got:0:12}."
    log "  The build may not have been picked up. Check: $COMPOSE ps"
  fi
}

healthy() {
  # Announced rather than silent. This loop can legitimately run for two
  # minutes while an image builds and a container starts, and an unexplained
  # two-minute pause is indistinguishable from a hang to whoever is watching.
  log "  waiting up to ${HEALTH_TIMEOUT}s for $HEALTH_URL"
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  local last=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    if last="$(curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>&1)"; then
      return 0
    fi
    sleep 3
  done
  # The last thing curl said. Usually the whole diagnosis: "Connection
  # refused" is a container that never came up, a 401 is a health URL pointed
  # at a gated path, and a timeout is something else entirely.
  log "  last response from $HEALTH_URL: ${last:-<no response>}"
  return 1
}

# Before touching anything: is the health check even capable of passing?
#
# If PISR is not answering now, it will not answer after a deploy either, and
# the run would deploy, fail the check, roll back, fail the check again, and
# announce that PISR is down and the commit is bad — when the truth may simply
# be that PISR_HEALTH_URL names the wrong port, or that curl cannot reach it
# from wherever this script runs. Every one of those conclusions is wrong and
# all of them are expensive to unpick. A deploy whose success cannot be
# measured should not start.
if ! curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
  if [ "${PISR_ALLOW_UNHEALTHY_START:-0}" != "1" ]; then
    notify_blocked ":warning: PISR deploy blocked on ${HOSTNAME_:-host} — $HEALTH_URL is not answering before any change was made. Either PISR is already down, or the health URL is wrong. Nothing was deployed."
    die "$HEALTH_URL is not answering, and nothing has been changed yet.
     Either PISR is already down, or PISR_HEALTH_URL is wrong for this box.
     Check it by hand before deploying:  curl -v $HEALTH_URL
     If PISR really is down and you are deploying to fix it, re-run with
     PISR_ALLOW_UNHEALTHY_START=1."
  fi
  log "$HEALTH_URL is not answering; PISR_ALLOW_UNHEALTHY_START=1, continuing."
fi

# Got past every blocking condition, so forget any we notified about.
rm -f "$_BLOCK_STATE" 2>/dev/null || true

if [ "$current" = "$target" ]; then
  log "Rebuilding ${target:0:12} ($BRANCH) — repository unchanged, container stale."
  subjects="(no new commits; the container was behind the repository)"
else
  log "Deploying ${current:0:12} -> ${target:0:12} ($BRANCH)"
  subjects="$(git log --oneline "$current..$target")"
  printf '%s\n' "$subjects" | sed 's/^/    /'
fi

git reset --hard --quiet "$target"

if deploy "$target" && healthy; then
  log "Deployed ${target:0:12}; $HEALTH_URL is answering."
  confirm_running "$target"
  notify ":rocket: PISR deployed on ${HOSTNAME_:-host} — ${current:0:12} → ${target:0:12}, healthy.
$subjects"
  exit 0
fi

# Past here the new commit is either unbuildable or unhealthy. Neither is
# something a timer should leave running.
log "New commit ${target:0:12} did not come up healthy within ${HEALTH_TIMEOUT}s."
log "Rolling back to ${current:0:12}."

git reset --hard --quiet "$current"
if deploy "$current" && healthy; then
  notify ":rewind: PISR ROLLED BACK on ${HOSTNAME_:-host} — ${target:0:12} did not answer $HEALTH_URL within ${HEALTH_TIMEOUT}s. Restored ${current:0:12}, which is healthy and serving. The new commit needs a look:
$subjects"
  die "Rolled back to ${current:0:12}, which is healthy. ${target:0:12} needs a look."
fi

notify ":rotating_light: PISR deploy on ${HOSTNAME_:-host} failed BOTH ways — neither ${target:0:12} nor the rollback to ${current:0:12} passed $HEALTH_URL. Both commits failing the same check usually means the check is wrong rather than the code, but this needs a human either way."
die "Rollback to ${current:0:12} did not pass the health check either.
     Both the new commit and the known-good one failed, which points at the
     check rather than at either commit — $HEALTH_URL may be wrong, or
     unreachable from here. Confirm before assuming an outage:
       curl -v $HEALTH_URL
       $COMPOSE ps"

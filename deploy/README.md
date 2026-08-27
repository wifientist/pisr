# Automatic deploys

A timer on the box asks GitHub whether `main` has moved, and rebuilds if it
has. Nothing listens on a port for this.

## Why not a webhook

The tunnel PISR sits behind is an outbound connection — that is the whole
reason to use one. A GitHub webhook would need an inbound path back to the
box, an endpoint exempt from the session gate (GitHub cannot hold a session
cookie), and access to the podman socket in order to actually deploy. That
last one makes an internet-facing container host-root-equivalent, which turns
the worst case of a PISR compromise from "someone read a venue inventory" into
"someone owns the host". Polling costs one HTTPS request every five minutes
and gives all of that up in exchange for nothing.

## What the script guarantees

- **It will not clobber local edits.** A dirty working tree stops the deploy
  and logs what is dirty. Override with `PISR_UPDATE_ALLOW_DIRTY=1` only when
  you mean to lose those changes.
- **It never deletes untracked files.** `git reset --hard` leaves them alone
  and there is no `git clean` anywhere, so the production `.env` in the app
  directory survives every deploy. Do not add one.
- **A bad commit rolls back.** Building on the production box means a broken
  commit is an outage. The deploy is not finished until `/healthz` answers; if
  it does not within `PISR_HEALTH_TIMEOUT`, the previous commit is rebuilt and
  restored, and the unit exits non-zero so the failure is visible in
  `systemctl --user status` and the journal.
- **One at a time.** A `flock` keeps a slow rebuild from overlapping the next
  timer tick — and the compose call closes that lock's file descriptor, so the
  container's own supervisor processes do not inherit it and lock out every
  future run.
- **It notices a stale container, not just a moved branch.** If someone runs
  `git pull` by hand, HEAD equals origin while the container still runs the old
  image — and a script comparing git to git would see nothing to do, forever.
  The container's build label is checked against the target too, and a mismatch
  triggers a rebuild. An unreadable label (an image built before the label
  existed) counts as "cannot tell" and is left alone, since deploying every
  tick would be worse than deploying on none.
- **It refuses to deploy what it cannot measure.** If `/healthz` is not
  answering *before* anything is touched, the run stops and changes nothing.
  Otherwise a wrong `PISR_HEALTH_URL` produces a confident, entirely false
  story: deploy, fail, roll back, fail again, announce an outage and blame the
  commit — when PISR was fine the whole time and only the URL was wrong. If
  PISR really is down and the deploy is the fix, `PISR_ALLOW_UNHEALTHY_START=1`
  overrides it.

## One-time host setup

Run as the user that owns the containers (`appuser` below).

```bash
# 1. Linger, so the user's systemd — and therefore rootless podman and this
#    timer — keeps running when nobody is logged in. Without this the timer
#    stops at logout and silently never fires again. This is the step people
#    miss.
sudo loginctl enable-linger appuser

# 2. Settings. Anything the script or podman-compose needs goes here, NOT in a
#    tracked file — editing a tracked file would make the working tree dirty
#    and stop the deploy that reads it.
mkdir -p ~/.config
cat > ~/.config/pisr-update.env <<'EOF'
PISR_APP_DIR=/home/appuser/app
PISR_ENV_FILE=/home/appuser/pisr-config/pisr.env
EOF

# 3. Install the units.
mkdir -p ~/.config/systemd/user
cp ~/app/deploy/pisr-update.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pisr-update.timer
```

The unit's `ExecStart` is `%h/app/deploy/pisr-update.sh`, so it assumes the
clone is at `~/app`. If `PISR_APP_DIR` points somewhere else, edit that line to
match — systemd resolves `ExecStart` itself and will not read it from the
environment file.

`PISR_ENV_FILE` is read by `docker-compose.yml` itself, not by the script; it
is here because the script's environment is what podman-compose inherits.

There is deliberately no `PISR_HEALTH_URL` above. The script reads `PISR_BIND`
and `PISR_PORT` out of that same env file and builds the URL from both. A
wildcard bind (or none) means loopback; a specific address means that address,
because an instance published on `10.10.77.108:8080` has nothing whatsoever on
`127.0.0.1:8080` and a loopback check would fail against a perfectly healthy
container. The port falls back to 8080 — the same default compose falls back
to. Setting the two independently is a
quiet trap: if the health URL names a port nothing is listening on, every
deploy looks unhealthy and rolls itself back, and the journal blames the commit
rather than the setting. Note that `.env.example` ships `PISR_PORT=8090` while
compose defaults to 8080, so the two really do differ between installs.

Set `PISR_HEALTH_URL` explicitly only if PISR is reachable somewhere the
derivation cannot work out — and point it at `/healthz`, never `/api/status`,
which is behind the session gate and would fail every check.

## Operating it

```bash
systemctl --user list-timers pisr-update.timer   # when it next runs
systemctl --user start pisr-update.service       # deploy now, don't wait
journalctl --user -u pisr-update.service -f      # watch a deploy
journalctl --user -u pisr-update.service -p err  # only the failures
```

A run that finds nothing new logs nothing at all, so a quiet journal is the
healthy state. Every run that actually deploys logs the commit range it moved
through.

To pause deploys — during an incident, or while debugging on the box:

```bash
systemctl --user stop pisr-update.timer
```

## Slack notifications (optional)

Outbound only, like everything else here — the box posts to Slack, Slack never
reaches the box. Create an Incoming Webhook, then either put the URL in
`~/.config/pisr-update.env`:

```
PISR_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00/B00/xxxx
```

or keep it out of the environment entirely, which is worth doing because a
webhook URL is a credential — anyone holding it can post to that channel as
this app:

```bash
install -m 0600 /dev/stdin ~/.config/pisr-slack-webhook   # paste, then Ctrl-D
echo 'PISR_SLACK_WEBHOOK_URL_FILE=/home/appuser/.config/pisr-slack-webhook' \
  >> ~/.config/pisr-update.env
```

You get a message when something happened and silence otherwise:

- **Deployed** — the commit range and subjects, confirmed healthy
- **Rolled back** — the new commit failed its health check and the previous one
  is serving again
- **Failed both ways** — neither commit passed, which usually means the health
  check is wrong rather than the code
- **Blocked** — a dirty working tree, or PISR already unreachable before any
  change was attempted

Nothing is sent for a run that finds no new commits, which is almost all of
them. Blocking conditions repeat every tick until someone intervenes, so only
the first of each distinct one is sent — otherwise a dirty working tree would
be 288 identical messages a day. That state resets as soon as a run gets far
enough to attempt a deploy.

Slack cannot fail a deploy. A webhook that is down, slow or wrong is noted in
the journal and otherwise ignored: it is not a reason to leave production on
the previous commit.

## Which build is actually running

The repository on the box tells you what it *fetched*. It does not tell you
what the container is serving, and the two disagree in exactly the situations
worth catching — a build that failed, a container that never got recreated, a
stale image. So the commit is baked into the image at build time, and can be
read two ways:

```bash
# From the image label. No session cookie, no HTTP request.
podman inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' pisr

# From the running process, behind the session gate.
curl -b jar http://127.0.0.1:8080/api/status | jq .build
```

`/healthz` deliberately does not carry it. That endpoint is public, and naming
the exact commit an internet-facing instance is running is free reconnaissance
for anyone deciding which bugs to try.

The deploy script checks the label itself after every successful deploy and
warns on a mismatch. It warns rather than rolling back: health has already
passed by that point, so the instance is up and serving, and trading a working
instance for a tidier label is not a call a timer should make at 3am.

A build done by hand without `PISR_BUILD_SHA` reports `unknown` rather than
guessing — the deploy script always sets it.

## Why the unit sets KillMode=process

Because without it the unit kills the container it just deployed, about ninety
seconds after reporting success.

`podman-compose up -d` returns once the container is running, but it leaves
`conmon` and `rootlessport` alive to supervise it — and having been forked from
the deploy script, those live in the unit's own cgroup. A `Type=oneshot` unit
stops the instant its script exits, and systemd's default
`KillMode=control-group` SIGTERMs everything in the cgroup on stop, waits
`TimeoutStopSec`, then SIGKILLs it. The container goes with it:

```
pisr-update.service: State 'stop-sigterm' timed out. Killing.
pisr-update.service: Killing process 3998 (rootlessport) with signal SIGKILL.
pisr-update.service: Failed with result 'timeout'.
```

`KillMode=process` confines the kill to the main process, which has already
exited, and leaves its descendants running. If you ever rewrite this unit,
keep it — the symptom is a deploy that logs success and takes the service down
a minute and a half later, and nothing in the deploy output hints at it.

## "podman ps says Up, but nothing answers"

A container can be running with its port forwarding dead. Rootless podman
publishes a port through a separate `rootlessport` process; kill that and the
container carries on serving perfectly well to nobody, because there is no
longer a path from `PISR_BIND:PISR_PORT` into its network namespace.

Two things then mislead you at once. `podman ps` reports `Up 27 minutes`,
truthfully. And the container's own HEALTHCHECK passes, because it curls
`localhost:8080` from *inside* the container, where nothing is wrong.

`podman-compose up -d` will not fix it either — it sees a running container
and does nothing. It needs a full recreate:

```bash
cd ~/app && export PISR_ENV_FILE=/home/appuser/pisr-config/pisr.env
podman-compose down && podman-compose up -d
```

The deploy script's pre-deploy health probe catches this state for what it is,
from outside, which is the point of probing the published address rather than
asking podman whether it thinks things are fine.

## If a deploy fails

The unit will have rolled back already. The journal shows which commit failed:

```bash
journalctl --user -u pisr-update.service -n 50
```

Fix it upstream and push; the next tick picks it up. If the rollback *also*
failed, the log says so explicitly and the service is down — that is the one
case needing hands on the box.

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
  timer tick.

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
PISR_HEALTH_URL=http://127.0.0.1:8090/healthz
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

`PISR_HEALTH_URL` must match the port `docker-compose.yml` actually publishes
(`PISR_PORT`, default 8080). Point it at `/healthz` and not `/api/status` —
the latter is behind the session gate and would fail every check.

`PISR_ENV_FILE` is read by `docker-compose.yml` itself, not by the script; it
is here because the script's environment is what podman-compose inherits.

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

## If a deploy fails

The unit will have rolled back already. The journal shows which commit failed:

```bash
journalctl --user -u pisr-update.service -n 50
```

Fix it upstream and push; the next tick picks it up. If the rollback *also*
failed, the log says so explicitly and the service is down — that is the one
case needing hands on the box.

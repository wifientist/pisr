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

## Two status scripts

Rather than remembering the commands:

```bash
~/app/deploy/pisr-deploy-status.sh   # is the auto-deploy working, what did it last do
~/app/deploy/pisr-app-status.sh      # is PISR healthy, how is it configured
```

Both are read-only and safe to run at any time, including mid-deploy. They live
in the repo rather than in `$HOME` so a deploy keeps them current; symlink them
for convenience:

```bash
ln -sf ~/app/deploy/pisr-deploy-status.sh ~/pisr-deploy-status
ln -sf ~/app/deploy/pisr-app-status.sh    ~/pisr-app-status
```

`pisr-deploy-status` prints the timer schedule, the last run's outcome, and the
three versions that should agree — origin, the local repo, and the commit the
container was actually built from — saying which way they disagree when they
do. It then shows recent deploy activity with podman's build chatter filtered
out, since that is most of the volume and none of the meaning.

`pisr-app-status` probes the PUBLISHED address rather than localhost, on
purpose: rootless podman forwards through a separate process, and when that
dies the container keeps running and serving to nobody. In that state `podman
ps` says Up and the container's own healthcheck passes — it asks localhost from
inside, where nothing is wrong. Only the published address sees it, and this
script tells you the recovery when it does.

## Probing the gate from outside

`pisr-probe-auth.sh` asks the questions a stranger would, against a running
instance:

```bash
~/app/deploy/pisr-probe-auth.sh https://pisr.example.com
~/app/deploy/pisr-probe-auth.sh          # defaults to the published address
```

It exists because accounts mode changed what the login page is. Behind
Cloudflare Access it was the inner of two gates; on its own it is the only one,
and a handful of properties that used to be belt-and-braces became
load-bearing — no account enumeration, a throttle that slows an attacker
without ever locking a real person out, nothing under `/api` answering without
a cookie. Those are asserted in `api/tests/`, but a test asserts them about the
source; this asserts them about the thing actually serving traffic, through
whatever proxy, tunnel and configuration sit in front of it.

It creates nothing, changes no password and needs no session — every request is
a GET or a login that is meant to fail. Exit status is the number of failures,
so it works from a timer.

Two things to know before running it against production:

- **It trips the login backoff.** Under rootless podman every caller shares one
  apparent address, so real people may see "Too many attempts" for up to a
  minute. It cannot lock anyone out — that it *cannot* is one of the things
  being tested — but it is not a thing to run mid-shift.
- **The failure counter is remembered for `PISR_AUTH_LOCKOUT_SECONDS`** (300s
  by default) even after a block expires, so a second run inside five minutes
  starts part way up the backoff curve and the enumeration checks skip
  themselves rather than report a number they cannot measure. Wait it out.

What it deliberately does not cover: role enforcement, EC/venue scope and
credential scrubbing all need a session, and a version of this that
authenticated would stop being the outside view. `api/tests/test_sections.py`
and `api/tests/test_accounts.py` own those.

### As a signed-in user

`pisr-probe-authed.sh` is the other half: what somebody who legitimately holds a
`user` session can reach by asking for things that are not theirs.

```bash
PISR_PROBE_USER=testuser PISR_PROBE_PASS='...' \
  ~/app/deploy/pisr-probe-authed.sh https://pisr.example.com
```

**Where to put the credentials.** One file with both, which is the usual way:

```bash
cat > ~/probe.creds <<'EOF'
PISR_PROBE_USER=probeuser
PISR_PROBE_PASS=whatever-you-set
PISR_PROBE_ADMIN_USER=probeadmin      # optional, read-only
PISR_PROBE_ADMIN_PASS=whatever
EOF
chmod 600 ~/probe.creds

PISR_PROBE_CREDS=~/probe.creds ./pisr-probe-authed.sh https://pisr.example.com
```

The file is PARSED, not sourced — sourcing would execute whatever is in it, and
a credentials file is exactly the thing somebody pastes into without reading.
Splitting on the first `=` means a password may contain `=` freely. Individual
env vars still work, as does the `<NAME>_FILE` convention
(`PISR_PROBE_PASS_FILE`).

Never put the password on the command line: `/proc/<pid>/cmdline` is
world-readable, so argv is visible to every user on the box via `ps`.
The script sends the login body to `curl` over stdin rather than `-d`, so the
password is not in curl's argv either. Admin credentials are optional and used READ-ONLY, to find an EC
that exists and that the user's filtered list omits, which is the only way to
test the cross-customer case for real rather than against a made-up id.

It covers every admin route, EC/venue scope on all four paths that enforce it
(report, PDF, config detail, venue list), traversal and injection payloads in
`venue_id` and the PDF `label`, made-up controller ids, credential scrubbing in
the returned report, and the session cookie's flags — which the unauthenticated
probe cannot check, because it never obtains a session.

**It does not mutate, and that is enforced by construction.** Every destructive
verb is aimed at an account id that does not exist, or carries a body that fails
validation, so a *broken* authorization check is still harmless. That also
sharpens the result: `403` means authorization refused it, while `404` or `422`
means the request reached the handler and something else refused it — which is a
failure, because authorization did not gate it.

Two lessons from writing it, both worth knowing before you read its output:

- **On an MSP controller, omitting `tenant_id` makes every payload test
  vacuous.** The request is refused with 400 before `venue_id` is looked at, so
  ten injection payloads "passed" without ever reaching the code being probed.
  It now sends the user's own tenant so the request gets as far as the scope
  check and the R1 client.
- **A 200 is not automatically a finding.** An unscoped user asking for a venue
  id that does not exist correctly gets a report-shaped object with nothing in
  it, because the id is an opaque lookup key passed to R1, never a path. The
  script inspects the body for file content, tracebacks and real venue data
  instead of judging on the status code.

**If a login is rejected, `PISR_PROBE_DEBUG=1` says why** without printing any
password — it shows the usernames and the password *lengths* it parsed. A
length of 0, or one that is not what you typed, is a creds-file problem. The
right length and still a 401 means the account itself cannot sign in, and the
commonest reason is that it was created but never enrolled:

```
probeadmin  admin  invited, not yet enrolled
```

An account with no password 401s exactly like a wrong one. `pisr_admin.py list`
is the fastest way to tell them apart. Note also that keys are matched after
trimming whitespace, but an inline `#` is not a comment — a password may
contain one, so comments must be on their own line.

**Supply the admin credentials.** Without them three checks silently lose their
meaning, and two of them used to lie about it:

- the cross-customer test falls back to a made-up tenant id, which proves
  fail-closed but not isolation between real customers;
- whether the role is scoped at all is unknown, and an unrestricted role
  reaching any tenant is *correct* — judging that without knowing is how a
  clean instance gets reported as leaking;
- redaction cannot be measured, because measuring it means fetching the same
  report as both roles and comparing.

A rejected admin login is now a FAIL that names the reason, not a note.

**Redaction is compared, not inferred, and the comparison recurses.** Hiding the
Config tab empties `config.categories` while `config` itself stays a populated
dict, so a top-level diff sees nothing and reports that redaction is not
working. The script walks both reports and names the paths that are full for an
admin and empty for the user.

Delete the test account when you are done.

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

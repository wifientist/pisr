#!/usr/bin/env bash
#
# Probe PISR's gate from the outside, the way a stranger would.
#
# WHAT THIS IS FOR. Accounts mode put PISR's login page on the public internet
# with nothing in front of it — before that, Cloudflare Access was the outer
# gate and this one was the inner. That change makes a handful of properties
# load-bearing which used to be belt-and-braces, and this asserts them against
# a RUNNING instance rather than against the source.
#
#   ./pisr-probe-auth.sh https://pisr.example.com
#   ./pisr-probe-auth.sh http://10.10.77.108:8080
#
# Defaults to the address deploy/pisr-app-status.sh would probe, derived from
# PISR_BIND and PISR_PORT in the same env file the deploy script reads.
#
# READ-ONLY, AND IT STAYS THAT WAY. It creates nothing, changes no password,
# disables nobody and needs no session. Every request is either a GET, or a
# login/enrol attempt that is MEANT to fail. Do not add a check that needs an
# admin cookie — the point of this script is the view from outside, and a
# version that authenticates would stop being that.
#
# TWO THINGS IT WILL DO TO A LIVE INSTANCE, both deliberate and both brief:
#
#   * It trips the login backoff. Under rootless podman every caller shares one
#     apparent address, so for up to `_BACKOFF_CAP_SECONDS` (60s) real people
#     may see "Too many attempts". It cannot LOCK anyone out — accounts mode
#     backs off rather than locking, which is the property being tested — but
#     do not run this during an install crew's shift for fun.
#
#   * It guesses at one deliberately absurd username, never a real one, so no
#     actual person's `user:` counter is touched.
#
# Exit status is the number of FAILED checks, so it is usable from a timer.

set -uo pipefail

BASE="${1:-}"

# Same derivation as pisr-app-status.sh: the published address, not localhost.
# Rootless podman forwards through a separate process, and when that dies the
# container keeps serving to nobody while its own healthcheck still passes.
if [ -z "$BASE" ]; then
  _env_file="${PISR_ENV_FILE:-${PISR_APP_DIR:-$HOME/app}/.env}"
  _get() { [ -r "$_env_file" ] && sed -n "s/^$1=//p" "$_env_file" | tail -1 | tr -d '"'"'"'\r'; }
  _bind="$(_get PISR_BIND)"
  _port="$(_get PISR_PORT)"
  case "$_bind" in ""|"0.0.0.0"|"::"|"*") _bind="127.0.0.1" ;; esac
  BASE="http://${_bind}:${_port:-8080}"
fi
BASE="${BASE%/}"

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
head_() { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }

code()    { curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@"; }
body()    { curl -s --max-time 10 "$@"; }
headers() { curl -s -D - -o /dev/null --max-time 10 "$@"; }

post_json() {  # post_json <path> <json>
  curl -s --max-time 15 -X POST "$BASE$1" \
    -H 'Content-Type: application/json' -d "$2"
}
post_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BASE$1" \
    -H 'Content-Type: application/json' -d "$2"
}

# Same, but the body arrives on stdin. argv has a size limit measured in
# hundreds of KB and curl dies with "Argument list too long" well before PISR
# sees anything — which looks like a finding and is not one.
# Status and body together, tab-separated. The enumeration checks below need
# both: a 429 and a 401 can carry different bodies and take wildly different
# times, and comparing them as though they were the same kind of answer is how
# this script talked itself into a false result.
post_probe() {  # post_probe <path> <json>  ->  "<code>\t<body>"
  curl -s --max-time 15 -w '\t%{http_code}' -X POST "$BASE$1" \
    -H 'Content-Type: application/json' -d "$2" \
    | awk -F'\t' '{printf "%s\t%s", $NF, $1}'
}

post_code_stdin() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X POST "$BASE$1" \
    -H 'Content-Type: application/json' --data-binary @-
}

printf '\033[1mProbing %s\033[0m\n' "$BASE"

# ── Is it even there ─────────────────────────────────────────────────
head_ "Reachable"
if [ "$(code "$BASE/healthz")" = "200" ]; then
  ok "/healthz answers"
else
  bad "/healthz does not answer — nothing below will mean anything."
  exit 1
fi

MODE="$(body "$BASE/api/auth/status" | sed -n 's/.*"mode":"\([a-z]*\)".*/\1/p')"
printf '  gate mode: %s\n' "${MODE:-unknown}"

# ── What a stranger can reach without a cookie ───────────────────────
#
# The allowlist is small and every entry has a reason. Anything ELSE under
# /api answering 200 unauthenticated is a hole.
head_ "Unauthenticated surface"

for p in /api/status /api/config /api/admin/visibility /api/admin/accounts \
         /api/pisr/1/venues /api/r1/1/msp/mspEcs /api/checks; do
  c="$(code "$BASE$p")"
  case "$c" in
    401|403) ok "$p -> $c" ;;
    404)     ok "$p -> 404 (route absent on this build)" ;;
    *)       bad "$p -> $c — should require a session" ;;
  esac
done

# The API docs publish the whole route surface, which is a map of what to try
# next. Gated on purpose.
for p in /docs /redoc /openapi.json; do
  c="$(code "$BASE$p")"
  if [ "$c" = "401" ] || [ "$c" = "404" ]; then ok "$p -> $c"
  else bad "$p -> $c — the route surface should not be public"; fi
done

# Deliberately open: the SPA has to load to render the login form, and it
# carries no tenant data.
[ "$(code "$BASE/")" = "200" ] && ok "/ (SPA) -> 200, expected" \
  || note "/ did not return 200 — fine if you serve the SPA elsewhere"

# /healthz must stay boring. A build SHA here would be free reconnaissance.
h="$(body "$BASE/healthz")"
case "$h" in
  *sha*|*commit*|*version*|*tenant*|*region*)
    bad "/healthz leaks build or tenant detail: $h" ;;
  *) ok "/healthz says nothing but status" ;;
esac

# ── Headers a browser needs to defend the page ───────────────────────
head_ "Security headers"
H="$(headers "$BASE/")"
want_header() {  # want_header <name> <substring-or-empty>
  line="$(printf '%s' "$H" | grep -i "^$1:" | head -1 | tr -d '\r')"
  if [ -z "$line" ]; then bad "$1 missing"; return; fi
  if [ -n "${2:-}" ] && ! printf '%s' "$line" | grep -qi -- "$2"; then
    bad "$1 present but missing '$2' — $line"; return
  fi
  ok "$1"
}
want_header "content-security-policy" "frame-ancestors 'none'"
want_header "x-content-type-options"  "nosniff"
want_header "referrer-policy"
want_header "permissions-policy"
want_header "cross-origin-opener-policy"

case "$BASE" in
  https://*)
    if printf '%s' "$H" | grep -qi '^strict-transport-security:'; then
      ok "strict-transport-security (HTTPS)"
    else
      bad "no HSTS on an HTTPS origin"
    fi ;;
  *) note "plain HTTP — HSTS deliberately not sent, and the session cookie" \
          "crosses the wire readable. Fine on a LAN address, not on the internet." ;;
esac

# ── Account enumeration ──────────────────────────────────────────────
#
# The one that matters most now the login page is public. "No such user" and
# "wrong password" must be indistinguishable in BOTH the message and the time
# taken — an early return on an unknown username is measurable over the
# internet and turns login into a user directory.
head_ "Account enumeration"

if [ "$MODE" != "accounts" ]; then
  note "gate is '$MODE', not 'accounts' — skipping the username checks"
else
  GHOST="nosuchuser-$$-$RANDOM"     # never a real account, so nobody is throttled
  PW="a-wrong-but-plausible-password"

  p1="$(post_probe /api/login "{\"username\":\"$GHOST\",\"password\":\"$PW\"}")"
  c1="${p1%%	*}"; r1="${p1#*	}"
  p2="$(post_probe /api/login "{\"username\":\"$GHOST-2\",\"password\":\"$PW\"}")"
  c2="${p2%%	*}"; r2="${p2#*	}"

  # THE THROTTLE MAKES THESE UNMEASURABLE, so say so rather than guessing. A
  # 429 and a 401 differ in body and in time for reasons that have nothing to
  # do with enumeration, and treating one as the other produces both false
  # passes ("identical messages" — both were 429) and false failures ("too
  # fast to have hashed" — it never got as far as hashing).
  #
  # Note the counter is REMEMBERED for PISR_AUTH_LOCKOUT_SECONDS (default 300)
  # even after a block expires, so a second run inside five minutes starts part
  # way up the backoff curve. Wait it out for a clean measurement.
  if [ "$c1" = "429" ] || [ "$c2" = "429" ]; then
    note "already throttled from an earlier run — enumeration checks skipped."
    note "wait ~5 minutes (the counter is remembered that long) and re-run."
  else
    if [ "$r1" = "$r2" ]; then
      ok "unknown users return an identical message"
    else
      bad "two unknown users returned different messages"
    fi

    case "$r1" in
      *"no such"*|*"not found"*|*"unknown user"*|*"does not exist"*)
        bad "the message distinguishes a missing account: $r1" ;;
      *) ok "the message does not admit whether the account exists" ;;
    esac

    # Timing. Not a rigorous side-channel study — a check that the unknown-user
    # path does real work rather than returning early. A missing dummy-hash
    # burn shows up here as single-digit milliseconds against ~60.
    t_start=$(date +%s%N)
    pt="$(post_probe /api/login "{\"username\":\"$GHOST-t\",\"password\":\"$PW\"}")"
    t_ghost=$(( ($(date +%s%N) - t_start) / 1000000 ))
    ct="${pt%%	*}"
    if [ "$ct" != "401" ]; then
      note "timing not measurable (got $ct, not 401)"
    else
      printf '  unknown-user login took %sms\n' "$t_ghost"
      if [ "$t_ghost" -lt 15 ]; then
        bad "that is too fast to have hashed anything — likely an early return"
      else
        ok "the unknown-user path spends time hashing, as it should"
      fi
    fi
  fi
fi

# ── Throttle ─────────────────────────────────────────────────────────
#
# Two properties, and they pull in opposite directions:
#   it must SLOW an attacker, and it must never LOCK anyone out.
# A hard lockout on a public login page is an off switch a stranger can flip.
head_ "Login throttle"

if [ "$MODE" != "accounts" ]; then
  note "gate is '$MODE' — passphrase mode uses a hard lockout by design"
else
  seen429=0; seen401=0; retry=""
  for i in $(seq 1 12); do
    out="$(curl -s -D - -o /dev/null --max-time 15 -X POST "$BASE/api/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"$GHOST-b\",\"password\":\"$PW-$i\"}" 2>/dev/null)"
    c="$(printf '%s' "$out" | sed -n '1s/.* \([0-9][0-9][0-9]\).*/\1/p')"
    [ "$c" = "429" ] && { seen429=$((seen429+1))
      r="$(printf '%s' "$out" | grep -i '^retry-after:' | tr -dc '0-9')"
      [ -n "$r" ] && retry="$r"; }
    [ "$c" = "401" ] && seen401=$((seen401+1))
  done

  if [ "$seen429" -gt 0 ]; then
    ok "throttle engages (saw $seen429 x 429 in 12 attempts)"
  else
    bad "12 wrong passwords and never throttled — check the gate"
  fi

  if [ -n "$retry" ] && [ "$retry" -le 60 ]; then
    ok "Retry-After is ${retry}s — capped, so this is backoff not lockout"
  elif [ -n "$retry" ]; then
    bad "Retry-After is ${retry}s — that is a lockout, and a stranger can trigger it"
  fi

  # The message must not say WHICH key is throttled: "this username is
  # rate-limited" confirms the username exists.
  m="$(post_json /api/login "{\"username\":\"$GHOST-b\",\"password\":\"x\"}")"
  case "$m" in
    *username*|*account*) bad "the throttle message names the account: $m" ;;
    *) ok "the throttle message does not confirm an account exists" ;;
  esac
fi

# ── Enrolment ────────────────────────────────────────────────────────
#
# The only unauthenticated WRITE path in PISR. The token is 256 random bits, so
# guessing is not the threat; what matters is that rubbish is refused and that
# a bad token cannot be told from an unused one.
head_ "Enrolment"

if [ "$MODE" != "accounts" ]; then
  note "gate is '$MODE' — enrolment routes should not exist"
  c="$(code "$BASE/api/enroll/anything")"
  [ "$c" = "404" ] && ok "/api/enroll/... -> 404 outside accounts mode" \
    || bad "/api/enroll/... -> $c outside accounts mode"
else
  for t in "" "x" "../../etc/passwd" "$(printf 'a%.0s' $(seq 1 500))"; do
    c="$(code "$BASE/api/enroll/$t")"
    case "$c" in
      404|410|422|429|405) ok "junk enrolment token -> $c" ;;
      307) ok "junk enrolment token -> 307 (trailing-slash redirect, not a way in)" ;;
      *) bad "junk enrolment token -> $c" ;;
    esac
  done
  c="$(post_code /api/enroll '{"token":"nope","password":"a-long-enough-password"}')"
  case "$c" in
    400|404|410|429) ok "redeeming a bad token -> $c" ;;
    204) bad "redeeming a NONSENSE token succeeded — stop and investigate" ;;
    *) bad "redeeming a bad token -> $c" ;;
  esac
fi

# ── Cookie flags ─────────────────────────────────────────────────────
#
# Checked on a FAILED login: no cookie should be set at all. A session cookie
# handed out before authentication succeeds would be the whole ballgame.
head_ "Cookies"
sc="$(curl -s -D - -o /dev/null --max-time 15 -X POST "$BASE/api/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"${GHOST:-ghost}-c\",\"password\":\"wrong-password-here\"}" \
      | grep -i '^set-cookie:' | tr -d '\r')"
if [ -z "$sc" ]; then
  ok "a failed login sets no cookie"
else
  bad "a failed login set a cookie: $sc"
fi

# ── Method and input handling ────────────────────────────────────────
head_ "Input handling"
c="$(code -X GET "$BASE/api/login")"
case "$c" in 405|401|404) ok "GET /api/login -> $c" ;; *) bad "GET /api/login -> $c" ;; esac

c="$(post_code /api/login 'not json at all')"
case "$c" in 400|422) ok "malformed JSON -> $c" ;; *) bad "malformed JSON -> $c" ;; esac

c="$(post_code /api/login '{}')"
case "$c" in 400|422) ok "empty body -> $c" ;; *) bad "empty body -> $c" ;; esac

# A huge body should be refused or handled, never hang or 500.
c="$( { printf '{"username":"x","password":"'
        printf 'a%.0s' $(seq 1 200000)
        printf '"}'; } | post_code_stdin /api/login )"
case "$c" in 400|401|413|422|429) ok "200KB password -> $c" ;;
  500) bad "200KB password -> 500" ;;
  *) note "200KB password -> $c" ;; esac

# ── Verdict ──────────────────────────────────────────────────────────
printf '\n\033[1m── Result ──\033[0m\n'
printf '  %d passed, %d failed, %d note(s)\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32mNothing here contradicts the design.\033[0m\n'
  printf '  This probes the OUTSIDE only. Role enforcement, EC/venue scope and\n'
  printf '  credential scrubbing need a session; api/tests/ covers those.\n'
else
  printf '  \033[31mSomething above needs attention before this faces the internet.\033[0m\n'
fi
printf '  The login backoff you just tripped clears within ~60s.\n\n'
exit "$FAIL"

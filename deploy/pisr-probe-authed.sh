#!/usr/bin/env bash
#
# Probe PISR as a signed-in ORDINARY USER, trying to reach what they should not.
#
# The companion to pisr-probe-auth.sh. That one asks what a stranger can reach;
# this one asks what somebody who legitimately holds a `user` session can reach
# by asking for things that are not theirs — another customer's tenant, an
# admin route, a venue outside their scope, an id they made up.
#
#   PISR_PROBE_CREDS=~/probe.creds ./pisr-probe-authed.sh https://pisr.example.com
#
# where ~/probe.creds holds PISR_PROBE_USER= and PISR_PROBE_PASS= lines. Or
# pass them in the environment directly:
#
#   PISR_PROBE_USER=testuser PISR_PROBE_PASS='...' \
#     ./pisr-probe-authed.sh https://pisr.example.com
#
# Optionally, admin credentials as well. They are used READ-ONLY, to learn what
# the policy actually says so the user's view can be checked against it —
# which EC exists that this user should NOT see, and which sections are hidden:
#
#   PISR_PROBE_ADMIN_USER=... PISR_PROBE_ADMIN_PASS=... \
#   PISR_PROBE_USER=... PISR_PROBE_PASS=... ./pisr-probe-authed.sh <url>
#
# CREDENTIALS COME FROM THE ENVIRONMENT, never argv — argv is visible in `ps`
# and lands in shell history. Prefix the command rather than exporting, and
# they die with the process.
#
# ── IT DOES NOT MUTATE, AND THAT IS ENFORCED BY CONSTRUCTION ─────────
#
# Every destructive verb it sends is aimed somewhere that cannot destroy
# anything even if authorization were completely broken:
#
#   DELETE / PATCH / invite   are aimed at an account id that does not exist
#   POST   (create account)   sends a username that fails validation
#   PUT    (visibility)       sends a body shape that fails validation
#
# That is not belt-and-braces, it is the point: it means a FAILED check is
# still safe. And it sharpens the result, because the status code now
# distinguishes two things a 403-only test cannot —
#
#   403          the authorization ran and refused.                  PASS
#   404/422/400  the request reached the HANDLER and was refused by
#                something else. Authorization did not gate it.      FAIL
#   200/201/204  it worked. Stop and fix this today.                 FAIL
#
# Exit status is the number of failures.

set -uo pipefail

BASE="${1:-}"
[ -n "$BASE" ] || { echo "usage: PISR_PROBE_USER=... PISR_PROBE_PASS=... $0 <base-url>" >&2; exit 2; }
BASE="${BASE%/}"
# One file holding everything, which is the usual way to run this:
#
#   PISR_PROBE_CREDS=~/probe.creds ./pisr-probe-authed.sh <url>
#
#   # ~/probe.creds
#   PISR_PROBE_USER=probeuser
#   PISR_PROBE_PASS=whatever-you-set
#   PISR_PROBE_ADMIN_USER=probeadmin      # optional
#   PISR_PROBE_ADMIN_PASS=whatever        # optional
#
# PARSED, NOT SOURCED. Sourcing would execute whatever is in the file, and a
# credentials file is exactly the thing somebody pastes into without reading.
# Splitting on the first `=` also means a password may contain `=` freely.
if [ -n "${PISR_PROBE_CREDS:-}" ]; then
  [ -r "$PISR_PROBE_CREDS" ] || { echo "cannot read $PISR_PROBE_CREDS" >&2; exit 2; }
  while IFS='=' read -r _k _v; do
    _k="${_k#"${_k%%[![:space:]]*}"}"          # trim leading space
    case "$_k" in ''|'#'*) continue ;; esac
    _v="$(printf '%s' "$_v" | tr -d '\r' | sed 's/^"\(.*\)"$/\1/; s/^'"'"'\(.*\)'"'"'$/\1/')"
    case "$_k" in
      PISR_PROBE_USER)       PISR_PROBE_USER="$_v" ;;
      PISR_PROBE_PASS)       PISR_PROBE_PASS="$_v" ;;
      PISR_PROBE_ADMIN_USER) PISR_PROBE_ADMIN_USER="$_v" ;;
      PISR_PROBE_ADMIN_PASS) PISR_PROBE_ADMIN_PASS="$_v" ;;
    esac
  done < "$PISR_PROBE_CREDS"
  unset _k _v
  # Said once, not enforced: these are meant to be throwaway accounts you
  # delete afterwards, so this is a reminder rather than a rule.
  case "$(ls -l "$PISR_PROBE_CREDS" 2>/dev/null | cut -c5-10)" in
    ------) ;;
    *) printf '  \033[33mnote\033[0m  %s is readable by others; chmod 600 it.\n' \
              "$PISR_PROBE_CREDS" ;;
  esac
fi

# Or the <NAME>_FILE convention config.py uses for every other secret.
[ -n "${PISR_PROBE_PASS_FILE:-}" ] && \
  PISR_PROBE_PASS="$(tr -d '\r\n' < "$PISR_PROBE_PASS_FILE")"
[ -n "${PISR_PROBE_ADMIN_PASS_FILE:-}" ] && \
  PISR_PROBE_ADMIN_PASS="$(tr -d '\r\n' < "$PISR_PROBE_ADMIN_PASS_FILE")"

: "${PISR_PROBE_USER:?set PISR_PROBE_USER}"
: "${PISR_PROBE_PASS:?set PISR_PROBE_PASS (or PISR_PROBE_PASS_FILE)}"

ADMIN_USER="${PISR_PROBE_ADMIN_USER:-}"
ADMIN_PASS="${PISR_PROBE_ADMIN_PASS:-}"

TMPD="$(mktemp -d)"; JAR="$TMPD/user.jar"; AJAR="$TMPD/admin.jar"
cleanup() { rm -rf "$TMPD"; }
trap cleanup EXIT

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '  \033[33mNOTE\033[0m  %s\n' "$*"; WARN=$((WARN+1)); }
head_() { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }

code() {  # code <jar> <method> <path> [json]
  local jar="$1" m="$2" p="$3" d="${4:-}"
  if [ -n "$d" ]; then
    curl -s -o /dev/null -w '%{http_code}' --max-time 25 -b "$jar" -X "$m" \
      -H 'Content-Type: application/json' -d "$d" "$BASE$p"
  else
    curl -s -o /dev/null -w '%{http_code}' --max-time 25 -b "$jar" -X "$m" "$BASE$p"
  fi
}
bodyof() { curl -s --max-time 25 -g -b "$1" "$BASE$2"; }

# A GET with ONE query parameter, encoded by curl rather than by hand.
#
# Both halves matter for the injection payloads below. `-G --data-urlencode`
# encodes spaces, quotes and braces so the payload arrives intact instead of
# producing a malformed URL — and `-g` switches off curl's own globbing, which
# otherwise eats `{}` and `[]` and returns an empty status. Without these, two
# of the payloads silently tested nothing and reported a blank code, which
# looks like a pass and is not.
code_q() {  # code_q <jar> <path> <param> <value> [extra-param] [extra-value]
  local jar="$1" path="$2"
  shift 2
  local args=()
  while [ "$#" -ge 2 ]; do args+=(--data-urlencode "$1=$2"); shift 2; done
  curl -s -o /dev/null -w '%{http_code}' --max-time 25 -g -b "$jar" \
    -G "${args[@]}" "$BASE$path"
}

# Minimal JSON string escaping, so a password containing a quote or a
# backslash produces valid JSON rather than a confusing 422.
_json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

_login_body() { printf '{"username":"%s","password":"%s"}' \
  "$(_json_escape "$1")" "$(_json_escape "$2")"; }

# THE BODY GOES OVER STDIN, NOT ARGV. /proc/<pid>/cmdline is world-readable on
# Linux, so a password passed as `curl -d '{...}'` is visible in `ps` to every
# user on the box for as long as the request takes. This script warns about
# argv in its own header; it should not then do it itself.
login() {  # login <jar> <user> <pass>
  _login_body "$2" "$3" \
    | curl -s -o /dev/null -w '%{http_code}' --max-time 25 -c "$1" -X POST \
        "$BASE/api/login" -H 'Content-Type: application/json' --data-binary @-
}

# The verdict used throughout: refused by AUTHORIZATION, or refused by
# something downstream that happened to save us?
gated() {  # gated <label> <code>
  case "$2" in
    403)         ok "$1 -> 403" ;;
    401)         note "$1 -> 401 (session not accepted — check the run, not the gate)" ;;
    200|201|204) bad "$1 -> $2  ** IT WORKED. Fix today. **" ;;
    400|404|422) bad "$1 -> $2 — reached the handler, so authorization did not gate it" ;;
    429)         note "$1 -> 429 (throttled; re-run later)" ;;
    *)           bad "$1 -> $2 (unexpected)" ;;
  esac
}

printf '\033[1mProbing %s as %s\033[0m\n' "$BASE" "$PISR_PROBE_USER"

# ── Sign in ──────────────────────────────────────────────────────────
head_ "Session"
c="$(login "$JAR" "$PISR_PROBE_USER" "$PISR_PROBE_PASS")"
[ "$c" = "204" ] || { bad "could not sign in as $PISR_PROBE_USER (HTTP $c)"; exit 1; }
ok "signed in"

# Cookie flags. The unauthenticated probe never obtains a real session, so this
# is the only place they are ever verified.
SC="$(_login_body "$PISR_PROBE_USER" "$PISR_PROBE_PASS" \
      | curl -s -D - -o /dev/null --max-time 25 -X POST "$BASE/api/login" \
          -H 'Content-Type: application/json' --data-binary @- \
      | grep -i '^set-cookie: *pisr_session' | tr -d '\r')"
printf '%s' "$SC" | grep -qi 'httponly' \
  && ok "cookie is HttpOnly" || bad "cookie is NOT HttpOnly — an XSS could read the session"
printf '%s' "$SC" | grep -qi 'samesite=lax' \
  && ok "cookie is SameSite=Lax" || bad "cookie is not SameSite=Lax — a cross-site POST could ride it"
case "$BASE" in
  https://*) printf '%s' "$SC" | grep -qi 'secure' \
      && ok "cookie is Secure" \
      || bad "cookie lacks Secure on an HTTPS origin — set PISR_COOKIE_SECURE=1" ;;
  *) note "plain HTTP — Secure deliberately not set" ;;
esac

WHO="$(bodyof "$JAR" /api/auth/status)"
case "$WHO" in
  *'"role":"user"'*)  ok "session reports role=user" ;;
  *'"role":"admin"'*) bad "the test account is an ADMIN — this probe proves nothing. Use a user." ;;
  *) bad "could not read the role: $WHO" ;;
esac

# Stateless sessions do not revoke on logout: the signed cookie stays valid
# until it expires. Worth stating rather than assuming otherwise.
cp "$JAR" "$JAR.copy"
curl -s -o /dev/null --max-time 25 -b "$JAR" -X POST "$BASE/api/logout" >/dev/null
if [ "$(code "$JAR.copy" GET /api/status)" = "200" ]; then
  note "a cookie kept after logout still works — logout is client-side only."
  note "revocation is: change the password, or disable the account."
else
  ok "the session did not survive logout"
fi
login "$JAR" "$PISR_PROBE_USER" "$PISR_PROBE_PASS" >/dev/null   # sign back in

CID="$(bodyof "$JAR" /api/config | sed -n 's/.*"id":\([0-9]*\).*/\1/p' | head -1)"
CID="${CID:-1}"
printf '  controller id: %s\n' "$CID"

# ── Admin routes, with a user session ────────────────────────────────
head_ "Admin routes (a user must be refused)"

GHOST_ID="u_doesnotexist$$"

gated "GET    /api/admin/visibility"              "$(code "$JAR" GET    /api/admin/visibility)"
gated "GET    /api/admin/accounts"                "$(code "$JAR" GET    /api/admin/accounts)"
gated "PUT    /api/admin/visibility"              "$(code "$JAR" PUT    /api/admin/visibility '{"hidden":null}')"
gated "POST   /api/admin/accounts"                "$(code "$JAR" POST   /api/admin/accounts '{"username":"NOT A VALID NAME","role":"admin"}')"
gated "PATCH  /api/admin/accounts/{ghost}"        "$(code "$JAR" PATCH  "/api/admin/accounts/$GHOST_ID" '{"role":"admin"}')"
gated "DELETE /api/admin/accounts/{ghost}"        "$(code "$JAR" DELETE "/api/admin/accounts/$GHOST_ID")"
gated "POST   /api/admin/accounts/{ghost}/invite" "$(code "$JAR" POST   "/api/admin/accounts/$GHOST_ID/invite")"

# ── Scope: reaching a customer that is not yours ─────────────────────
head_ "EC / venue scope"

MINE="$(bodyof "$JAR" "/api/r1/$CID/msp/mspEcs")"
MY_EC="$(printf '%s' "$MINE" | grep -o '"id":"[0-9a-f]\{16,\}"' | head -1 | cut -d'"' -f4)"
[ -n "$MY_EC" ] && printf '  an EC this user CAN see: %s…\n' "${MY_EC:0:12}" \
                || note "this user can see no ECs (scope may deny everything)"

# A tenant id certainly in nobody's policy. Note that a role with NO scope set
# is unrestricted by design, so a non-403 here is only a finding once a scope
# exists — the message says so rather than crying wolf.
# IS THIS ROLE SCOPED AT ALL? Everything below depends on the answer and the
# script used to assume it. An UNRESTRICTED role reaching a foreign tenant is
# correct behaviour, not a leak — reporting it as a failure sends you hunting
# for a bug in the one case where there is none. With admin credentials the
# policy says; without them this stays honestly unknown.
# ONE admin login, reused by everything below. Two of them would spend the
# login backoff twice, and the second failure then reports "throttled" when the
# real cause was "rejected" — which is precisely the misdiagnosis this whole
# section exists to avoid.
SCOPED="unknown"
ADMIN_OK=""
if [ -n "$ADMIN_USER" ]; then
  ac="$(login "$AJAR" "$ADMIN_USER" "$ADMIN_PASS")"
  case "$ac" in
    204) ADMIN_OK=yes ;;
    401) bad "admin credentials were supplied but REJECTED (401). Check "\
             "PISR_PROBE_ADMIN_USER/PASS — everything needing them is skipped." ;;
    429) bad "admin login was throttled (429). Wait ~5 minutes and re-run; "\
             "the checks needing admin are skipped." ;;
    *)   bad "admin login returned $ac — the checks needing admin are skipped." ;;
  esac
  if [ -n "$ADMIN_OK" ]; then
    POL="$(bodyof "$AJAR" /api/admin/visibility)"
    case "$POL" in
      *'"unrestricted": true'*|*'"unrestricted":true'*) SCOPED="no" ;;
      *'"ecs"'*)                                        SCOPED="yes" ;;
      *)                                                SCOPED="unknown" ;;
    esac
  fi
fi
case "$SCOPED" in
  yes) printf '  this role IS scoped, so a foreign tenant must be refused\n' ;;
  no)  printf '  this role is UNRESTRICTED, so reaching any tenant is correct\n' ;;
  *)   printf '  scope state unknown (no admin session) — judging conservatively\n' ;;
esac

FAKE_EC="00000000000000000000000000000000"
for p in "/api/pisr/$CID/venues?tenant_id=$FAKE_EC" \
         "/api/pisr/$CID/report?tenant_id=$FAKE_EC&venue_id=$FAKE_EC" \
         "/api/pisr/$CID/report.pdf?tenant_id=$FAKE_EC&venue_id=$FAKE_EC" \
         "/api/pisr/$CID/config/detail?tenant_id=$FAKE_EC&venue_id=$FAKE_EC"; do
  c="$(code "$JAR" GET "$p")"; short="${p%%\?*}"
  case "$c" in
    403) ok "$short with a foreign tenant -> 403" ;;
    401) note "$short -> 401" ;;
    429) note "$short -> 429 (throttled)" ;;
    200) case "$SCOPED" in
           yes) bad  "$short with a foreign tenant -> 200  ** data returned **" ;;
           no)  ok   "$short -> 200 (this role is unrestricted; correct)" ;;
           *)   note "$short with a foreign tenant -> 200 — correct IF this role is unrestricted, a leak if not" ;;
         esac ;;
    *)   note "$short with a foreign tenant -> $c" ;;
  esac
done

# With admin credentials, the real version: an EC that EXISTS and that this
# user's filtered list omits.
if [ -n "$ADMIN_OK" ]; then
  if true; then
    ALL="$(bodyof "$AJAR" "/api/r1/$CID/msp/mspEcs")"
    for ec in $(printf '%s' "$ALL" | grep -o '"id":"[0-9a-f]\{16,\}"' | cut -d'"' -f4); do
      printf '%s' "$MINE" | grep -q "$ec" && continue
      c="$(code "$JAR" GET "/api/pisr/$CID/venues?tenant_id=$ec")"
      case "$c" in
        403) ok "a REAL EC the user cannot see -> 403 (${ec:0:12}…)" ;;
        200) bad "a REAL EC the user cannot see -> 200 ** another customer's data **" ;;
        *)   note "a REAL EC the user cannot see -> $c" ;;
      esac
      break
    done
  fi
fi

# ── Injection, traversal, and made-up ids ────────────────────────────
#
# PISR has no database, so the realistic targets are the R1 client and the
# Jinja template behind the PDF — not SQL. Nothing here should return 200, and
# nothing should return 500: a 500 means the input reached something that did
# not expect it.
head_ "Injection and traversal"

payloads=(
  '../../../../etc/passwd'
  '..%2f..%2f..%2fetc%2fpasswd'
  '%00'
  'a%0d%0aX-Injected:%20yes'
  '{{7*7}}'
  '${7*7}'
  "'; DROP TABLE venues; --"
  '{"$ne":null}'
  '<script>alert(1)</script>'
)
payloads+=("$(printf 'A%.0s' $(seq 1 3000))")

# THE TENANT MUST BE SUPPLIED OR THIS TESTS NOTHING. On an MSP controller a
# request with no tenant_id is refused with 400 ("select an MSP-EC first")
# before venue_id is looked at, so every payload came back 400 and every check
# passed without the payload ever reaching the code being probed. Sending the
# user's OWN tenant gets the request past that gate and as far as the scope
# check and the R1 client, which is where these inputs are actually handled.
for v in "${payloads[@]}"; do
  out="$TMPD/inj.json"
  if [ -n "$MY_EC" ]; then
    c="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -g -b "$JAR" -G \
      --data-urlencode "venue_id=$v" --data-urlencode "tenant_id=$MY_EC" \
      "$BASE/api/pisr/$CID/report")"
  else
    c="$(curl -s -o "$out" -w '%{http_code}' --max-time 60 -g -b "$JAR" -G \
      --data-urlencode "venue_id=$v" "$BASE/api/pisr/$CID/report")"
  fi
  label="$(printf '%s' "$v" | cut -c1-28)"

  # A 200 IS NOT AUTOMATICALLY A FINDING, and assuming it was is how the first
  # version of this script reported ten failures against correct behaviour.
  # An unscoped user asking for a venue id that does not exist gets exactly
  # what they should: a report-shaped object with nothing in it, because the id
  # is an opaque lookup key passed to R1, never a path. What would be a finding
  # is file contents, a traceback, or actual venue data coming back.
  case "$c" in
    400|403|404|422) ok "venue_id=$label -> $c (refused)" ;;
    429)             note "venue_id=$label -> 429 (throttled)" ;;
    500)             bad "venue_id=$label -> 500 — it reached something unprepared" ;;
    "")              bad "venue_id=$label -> (no response — the request was never sent)" ;;
    000)             bad "venue_id=$label -> 000 (curl could not complete it)" ;;
    200)
      if grep -qE 'root:x:|/bin/(ba)?sh|BEGIN [A-Z ]*PRIVATE KEY' "$out"; then
        bad "venue_id=$label -> 200 AND THE BODY CONTAINS FILE CONTENT"
      elif grep -qiE 'traceback|File "/app' "$out"; then
        bad "venue_id=$label -> 200 with a traceback in the body"
      elif grep -q '"name": *"[^"]' "$out" && ! grep -q '"name": *null' "$out"; then
        note "venue_id=$label -> 200 with a named venue — check it is one this user may see"
      else
        ok "venue_id=$label -> 200, empty report (treated as an opaque id)"
      fi ;;
    *) note "venue_id=$label -> $c" ;;
  esac
done

# The PDF route takes a `label` rendered into the document. This cannot read the
# rendered text, so it checks only that nothing errors.
if [ -n "$MY_EC" ]; then
  c="$(code_q "$JAR" "/api/pisr/$CID/report.pdf" venue_id x label '{{7*7}}' tenant_id "$MY_EC")"
else
  c="$(code_q "$JAR" "/api/pisr/$CID/report.pdf" venue_id x label '{{7*7}}')"
fi
case "$c" in
  400|403|404|422) ok "PDF label={{7*7}} -> $c" ;;
  500)             bad "PDF label={{7*7}} -> 500 — check for template injection by hand" ;;
  *)               note "PDF label={{7*7}} -> $c (inspect the PDF for '49')" ;;
esac

# The controller id is in the path; a different one must not become somebody
# else's tenant.
for cid in 0 999 -1 abc; do
  c="$(code "$JAR" GET "/api/pisr/$cid/venues")"
  case "$c" in
    200) bad "controller_id=$cid -> 200" ;;
    500) bad "controller_id=$cid -> 500" ;;
    *)   ok "controller_id=$cid -> $c" ;;
  esac
done

# ── What the user is actually served ─────────────────────────────────
head_ "Data returned to this user"

VENUE="$(bodyof "$JAR" "/api/pisr/$CID/venues${MY_EC:+?tenant_id=$MY_EC}" \
         | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)"
if [ -z "$VENUE" ]; then
  note "no venue reachable by this user — skipping the report checks."
  note "give the account a scope including one venue to exercise these."
else
  printf '  venue: %s…\n' "${VENUE:0:16}"
  REP="$(bodyof "$JAR" "/api/pisr/$CID/report?venue_id=$VENUE${MY_EC:+&tenant_id=$MY_EC}")"
  if [ "${#REP}" -lt 100 ]; then
    note "the report came back empty or errored; skipping content checks"
  else
    # Credentials must never reach any role: api/scrub.py runs over every report
    # inside redact.redact, unconditionally and regardless of role.
    leak=0
    for k in switchLoginPassword loginPassword apPassword sharedSecret password; do
      if printf '%s' "$REP" | grep -q "\"$k\""; then
        val="$(printf '%s' "$REP" | grep -o "\"$k\":\"[^\"]*\"" | head -1)"
        case "$val" in
          ''|*redacted*|*REDACTED*) ;;   # absent, or present and scrubbed
          *) bad "report contains $k with a value: $val"; leak=1 ;;
        esac
      fi
    done
    [ "$leak" = "0" ] && ok "no unscrubbed credential fields in the report"

    printf '%s' "$REP" | grep -qi 'traceback\|File "/app' \
      && bad "the report body contains a traceback" \
      || ok "no traceback in the report body"

    # REDACTION, MEASURED RATHER THAN INFERRED. The earlier version read the
    # policy through the admin session and reported "nothing is hidden" when
    # that session had failed — a false negative that flatly contradicted the
    # configured policy. Fetching the SAME report as both roles and comparing
    # is the only version of this check that cannot lie: whatever the policy
    # says, what matters is that the user's copy is emptier.
    if [ -z "$ADMIN_OK" ]; then
      note "no working admin session, so redaction could not be compared."
      note "supply PISR_PROBE_ADMIN_USER/PASS to exercise it."
    else
      AREP="$TMPD/admin-report.json"
      curl -s -o "$AREP" --max-time 120 -g -b "$AJAR" -G \
        --data-urlencode "venue_id=$VENUE" ${MY_EC:+--data-urlencode "tenant_id=$MY_EC"} \
        "$BASE/api/pisr/$CID/report" >/dev/null
      printf '%s' "$REP" > "$TMPD/user-report.json"

      emptied="$(python3 - "$AREP" "$TMPD/user-report.json" <<'PYEOF'
import json, sys
def load(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None
a, u = load(sys.argv[1]), load(sys.argv[2])
if not isinstance(a, dict) or not isinstance(u, dict):
    print("ERR"); sys.exit()
def filled(v):
    if v is None: return False
    if isinstance(v, (list, dict, str)): return len(v) > 0
    return True

# RECURSIVE, because redaction mostly empties NESTED paths. Hiding the Config
# tab empties `config.categories`, and `config` itself stays a populated dict —
# so a top-level comparison sees no difference and reports, wrongly, that
# nothing was redacted.
def walk(av, uv, path="", depth=0):
    if depth > 4:
        return []
    if isinstance(av, dict) and isinstance(uv, dict):
        out = []
        for k in av:
            out += walk(av[k], uv.get(k), f"{path}.{k}" if path else k, depth + 1)
        return out
    return [path] if filled(av) and not filled(uv) else []

gone = walk(a, u)
print(",".join(gone[:12]) if gone else "NONE")
PYEOF
)"
      case "$emptied" in
        ERR)  note "could not parse one of the reports; compare them by hand" ;;
        NONE) note "the user's report is not emptier than the admin's anywhere."
              note "if you have hidden a section, check it names a path that this"
              note "venue actually populates — otherwise nothing visibly changes." ;;
        *)    ok "redaction is applied: emptied for this user -> $emptied" ;;
      esac
    fi
  fi
fi

# ── Verdict ──────────────────────────────────────────────────────────
printf '\n\033[1m── Result ──\033[0m\n'
printf '  %d passed, %d failed, %d note(s)\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -eq 0 ]; then
  printf '  \033[32mThis user could not reach past their own role or scope.\033[0m\n'
else
  printf '  \033[31mSomething above let a user reach further than it should.\033[0m\n'
fi
printf '  Remember to delete the test account when you are done.\n\n'
exit "$FAIL"

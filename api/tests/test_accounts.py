"""
The guard on local accounts — the gate itself, so the bar is higher here.

WHY THIS EXISTS. `api/accounts.py` is the first thing PISR has that holds a
secret and decides who gets in. Everything it does wrong fails quietly: a hash
that verifies when it should not, an invite that works twice, a cookie that
survives the password change meant to kill it, a last admin deleted leaving an
instance nobody can administer. None of those show up on screen. They show up
months later as somebody still having access.

RUNS WITHOUT PYTEST, like `test_sections.py` and for the same reason — adding a
test framework to requirements.txt would put it in the production image. Under
pytest the `test_*` functions collect normally; run directly it executes them.

    docker compose -f docker-compose.dev.yml run --rm --no-deps \
      -v "$PWD:/repo" backend python /repo/api/tests/test_accounts.py

THE ENVIRONMENT IS SET BEFORE config IS IMPORTED, which is the only way to
steer it: `api/config.py` reads .env once at import and freezes the result.
python-dotenv does not override variables that already exist, so setting them
here wins. Each test gets its own accounts file under a temp directory, so
nothing here can touch a real deployment's /data.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

# api/tests/ -> api/
API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(API))

_TMP = Path(tempfile.mkdtemp(prefix="pisr-accounts-test-"))

# Set BEFORE importing config. See the module docstring.
os.environ["PISR_AUTH_MODE"] = "accounts"
os.environ["PISR_ACCOUNTS_FILE"] = str(_TMP / "accounts.json")
os.environ["PISR_INVITE_DAYS"] = "7"
os.environ["PISR_ACCOUNTS_MIN_PASSWORD"] = "12"
os.environ["PISR_SESSION_SECRET"] = "test-session-secret-not-a-real-one"
os.environ["PISR_AUTH_ADMIN_PASSPHRASE"] = "break-glass-passphrase"
# Only so config's controller half can load without a real .env present.
for _name, _value in (("R1_TENANT_ID", "t"), ("R1_CLIENT_ID", "c"),
                      ("R1_SHARED_SECRET", "s")):
    os.environ.setdefault(_name, _value)

import accounts                       # noqa: E402
import auth                           # noqa: E402
from config import AUTH               # noqa: E402

GOOD_PASSWORD = "a-perfectly-ordinary-long-passphrase"


def _fresh() -> accounts.AccountStore:
    """
    A store with its own empty file.

    The global `accounts.STORE` is repointed at it too, because `auth`'s cookie
    verification goes through the global rather than taking a store — which is
    correct for the app and inconvenient for exactly one line here.
    """
    path = _TMP / f"accounts-{time.time_ns()}.json"
    store = accounts.AccountStore(str(path))
    accounts.STORE = store
    return store


# ── Hashing ──────────────────────────────────────────────────────────

def test_hash_round_trip():
    encoded = accounts.hash_password(GOOD_PASSWORD)
    assert accounts.verify_password(GOOD_PASSWORD, encoded)
    assert not accounts.verify_password(GOOD_PASSWORD + "x", encoded)
    assert not accounts.verify_password("", encoded)


def test_hash_is_salted():
    """Two hashes of the same password must differ, or the salt is not one."""
    assert accounts.hash_password(GOOD_PASSWORD) != accounts.hash_password(GOOD_PASSWORD)


def test_scrypt_params_are_within_the_maxmem_ceiling():
    """
    The trap this whole module nearly fell into.

    128 * N * R at the chosen parameters is exactly 32 MiB, which is also
    OpenSSL's DEFAULT maxmem — so `hashlib.scrypt` raises "memory limit
    exceeded" rather than merely being slow, and it raises at the first login
    rather than at import. If somebody raises N without raising _SCRYPT_MAXMEM,
    this is what says so.
    """
    needed = 128 * accounts._SCRYPT_N * accounts._SCRYPT_R
    assert accounts._SCRYPT_MAXMEM > needed, (
        f"scrypt needs {needed} bytes but maxmem is "
        f"{accounts._SCRYPT_MAXMEM} — every sign-in would raise")
    # And prove it rather than only computing it.
    assert accounts.verify_password(GOOD_PASSWORD,
                                    accounts.hash_password(GOOD_PASSWORD))


def test_old_parameters_still_verify_and_are_flagged_for_upgrade():
    """
    Parameters live in the hash string so they can be raised later without
    invalidating everyone's password. If this breaks, raising N logs the whole
    estate out permanently.
    """
    import base64
    import hashlib
    import secrets as _secrets

    salt = _secrets.token_bytes(16)
    weak_n = 2 ** 14
    key = hashlib.scrypt(GOOD_PASSWORD.encode(), salt=salt, n=weak_n, r=8, p=1,
                         maxmem=accounts._SCRYPT_MAXMEM, dklen=32)
    old = "$".join(("scrypt", str(weak_n), "8", "1",
                    base64.b64encode(salt).decode(),
                    base64.b64encode(key).decode()))

    assert accounts.verify_password(GOOD_PASSWORD, old), "old hash must still verify"
    assert accounts.needs_rehash(old), "old parameters must be flagged"
    assert not accounts.needs_rehash(accounts.hash_password(GOOD_PASSWORD))


def test_unusable_hashes_refuse_rather_than_raise():
    """A corrupt record must fail to authenticate, not 500 the login route."""
    for broken in (None, "", "not-a-hash", "scrypt$x$8$1$aaaa$bbbb",
                   "bcrypt$1$2$3$4$5", "scrypt$32768$8$1$!!!$!!!"):
        assert accounts.verify_password(GOOD_PASSWORD, broken) is False


# ── Usernames and passwords ──────────────────────────────────────────

def test_username_rules():
    for good in ("alice", "j.smith", "bob-2", "a_b", "x1"):
        assert accounts.username_error(good) is None, good
    for bad in ("", "a", "Alice", "has space", "toolong" * 10, "-leading",
                "trailing!", "e@mail.com"):
        assert accounts.username_error(bad) is not None, bad


def test_password_floor_and_username_reuse():
    assert accounts.password_error("short", "alice") is not None
    assert accounts.password_error("", "alice") is not None
    assert accounts.password_error(GOOD_PASSWORD, "alice") is None
    # Long enough, but it is the username.
    name = "averylongusername"
    assert accounts.password_error(name, name) is not None
    assert accounts.password_error(name.upper(), name) is not None, \
        "case must not be a way around the username check"


# ── Invites ──────────────────────────────────────────────────────────

def test_invite_token_is_never_stored_in_the_clear():
    """
    The property that makes a stolen accounts.json useless for signing in. If
    the raw token ever lands in the file, anyone who can read it can enrol as
    anybody who has not enrolled yet.
    """
    store = _fresh()
    _, token = store.create("alice", accounts.ROLE_ADMIN, actor="test")
    body = store.path.read_text(encoding="utf-8")
    assert token not in body, "the raw invite token reached the file"
    assert accounts._token_hash(token) in body, "the hash should be there"


def test_invite_redeems_once_and_only_once():
    store = _fresh()
    _, token = store.create("alice", accounts.ROLE_ADMIN, actor="test")

    account = store.redeem_invite(token, GOOD_PASSWORD)
    assert account.enrolled
    assert account.invite is None, "the invite must be consumed in the same write"
    assert accounts.verify_password(GOOD_PASSWORD, account.hash)

    try:
        store.redeem_invite(token, "another-long-enough-password")
    except accounts.AccountsError:
        pass
    else:
        raise AssertionError("a redeemed invite was accepted a second time")


def test_expired_invite_is_refused():
    store = _fresh()
    _, token = store.create("alice", accounts.ROLE_USER, actor="test",
                            invite_days=0)
    time.sleep(0.01)
    try:
        store.redeem_invite(token, GOOD_PASSWORD)
    except accounts.AccountsError as exc:
        assert "expired" in str(exc).lower()
    else:
        raise AssertionError("an expired invite was accepted")


def test_invite_with_unreadable_expiry_is_treated_as_expired():
    """Fails closed — unlike the visibility policy, this one is the gate."""
    assert accounts.Invite(hash="x", issued_at=None, expires_at=None).expired
    assert accounts.Invite(hash="x", issued_at=None, expires_at="rubbish").expired


def test_reset_invite_leaves_the_existing_password_working():
    """
    A mis-clicked reset must not be an outage. The old password keeps working
    until the new link is actually redeemed.
    """
    store = _fresh()
    account, token = store.create("alice", accounts.ROLE_USER, actor="test")
    store.redeem_invite(token, GOOD_PASSWORD)

    account, _new_token = store.issue_invite(account.id, actor="test")
    assert accounts.verify_password(GOOD_PASSWORD, account.hash), \
        "issuing a reset link cleared the password"
    assert account.invite is not None


def test_setting_a_password_drops_any_outstanding_invite():
    """A reset link that still worked afterwards is a second key to the door."""
    store = _fresh()
    account, token = store.create("alice", accounts.ROLE_USER, actor="test")
    store.redeem_invite(token, GOOD_PASSWORD)
    account, stale = store.issue_invite(account.id, actor="test")

    store.set_password(account.id, "a-different-long-password")
    try:
        store.redeem_invite(stale, "yet-another-long-password")
    except accounts.AccountsError:
        pass
    else:
        raise AssertionError("an invite outlived the password change")


def test_disabled_account_cannot_enrol():
    store = _fresh()
    store.create("alice", accounts.ROLE_ADMIN, actor="test")   # keep one admin
    account, token = store.create("bob", accounts.ROLE_USER, actor="test")
    store.update(account.id, actor="test", disabled=True)
    try:
        store.redeem_invite(token, GOOD_PASSWORD)
    except accounts.AccountsError as exc:
        assert "expired" not in str(exc).lower(), \
            "a disabled account should not be distinguishable from a bad link"
    else:
        raise AssertionError("a disabled account enrolled")


# ── The last admin ───────────────────────────────────────────────────

def _enrolled_admin(store, name="alice"):
    account, token = store.create(name, accounts.ROLE_ADMIN, actor="test")
    return store.redeem_invite(token, GOOD_PASSWORD)


def test_the_last_admin_cannot_be_deleted_demoted_or_disabled():
    """
    There must be no state an admin can reach that only an SSH session gets
    them out of — this box is deliberately awkward to SSH into.
    """
    for action in ("delete", "demote", "disable"):
        store = _fresh()
        admin = _enrolled_admin(store)
        try:
            if action == "delete":
                store.delete(admin.id, actor="test")
            elif action == "demote":
                store.update(admin.id, actor="test", role=accounts.ROLE_USER)
            else:
                store.update(admin.id, actor="test", disabled=True)
        except accounts.AccountsError as exc:
            assert "only administrator" in str(exc)
        else:
            raise AssertionError(f"{action} stranded the instance")


def test_an_admin_can_go_once_another_can_sign_in():
    store = _fresh()
    first = _enrolled_admin(store, "alice")
    _enrolled_admin(store, "bob")
    store.delete(first.id, actor="test")           # no longer the last one
    assert store.by_username("alice") is None


def test_an_unenrolled_admin_does_not_count_as_a_way_in():
    """
    The subtle version of the last-admin guard. An admin who has never set a
    password cannot sign in, so deleting the one who CAN would leave nobody —
    counting them by role alone is how that happens.
    """
    store = _fresh()
    admin = _enrolled_admin(store, "alice")
    store.create("bob", accounts.ROLE_ADMIN, actor="test")   # invited, not enrolled
    try:
        store.delete(admin.id, actor="test")
    except accounts.AccountsError:
        pass
    else:
        raise AssertionError("deleted the only admin who could actually sign in")


# ── The file ─────────────────────────────────────────────────────────

def test_duplicate_usernames_are_refused():
    store = _fresh()
    store.create("alice", accounts.ROLE_ADMIN, actor="test")
    for attempt in ("alice", "ALICE", "  Alice  "):
        try:
            store.create(attempt, accounts.ROLE_USER, actor="test")
        except accounts.AccountsError:
            pass
        else:
            raise AssertionError(f"{attempt!r} created a second account")


def test_the_file_is_not_world_readable():
    """It holds password hashes; the mode is not incidental."""
    store = _fresh()
    store.create("alice", accounts.ROLE_ADMIN, actor="test")
    mode = store.path.stat().st_mode & 0o777
    assert mode == 0o600, f"accounts file is mode {mode:o}, expected 600"


def test_no_temp_files_are_left_behind():
    store = _fresh()
    for name in ("alice", "bob", "carol"):
        store.create(name, accounts.ROLE_USER, actor="test")
    litter = [p.name for p in store.path.parent.glob(".accounts-*.tmp")]
    assert not litter, f"left temp files behind: {litter}"


def test_public_never_carries_the_hash():
    """
    The one shape that leaves the store for the portal. An admin is not
    entitled to anybody's password material, their own included.
    """
    store = _fresh()
    account = _enrolled_admin(store)
    published = account.public()
    assert "hash" not in published
    assert account.hash and account.hash not in repr(published)
    # And the summary the portal actually fetches.
    assert "hash" not in repr(store.summary())


def test_an_unreadable_file_fails_closed_and_is_not_overwritten():
    """
    The opposite of visibility.py, deliberately. An unreadable policy means
    "hide nothing"; an unreadable accounts file must mean "nobody signs in" —
    and must not be clobbered, because a damaged-but-recoverable file is worth
    more than our idea of what was in it.
    """
    store = _fresh()
    store.create("alice", accounts.ROLE_ADMIN, actor="test")
    original = store.path.read_text(encoding="utf-8")

    store.path.write_text("{ this is not json", encoding="utf-8")
    assert store.broken
    assert store.list() == [], "a broken file must admit nobody"
    assert store.by_username("alice") is None

    try:
        store.create("bob", accounts.ROLE_USER, actor="test")
    except accounts.AccountsError as exc:
        assert "could not be read" in str(exc)
    else:
        raise AssertionError("a mutation overwrote an unreadable file")
    assert "this is not json" in store.path.read_text(encoding="utf-8"), \
        "the damaged file was overwritten"
    assert original.startswith("{"), "sanity: the pre-damage file was valid JSON"


def test_a_newer_format_version_is_refused():
    store = _fresh()
    store.path.write_text('{"version": 99, "users": []}', encoding="utf-8")
    assert store.broken, "a file from a future PISR must not be acted on"


def test_a_second_writer_is_picked_up():
    """
    The CLI writes this file from another container while the server runs. If
    the freshness check misses that, a portal save silently deletes whatever
    the CLI just added.
    """
    store = _fresh()
    store.create("alice", accounts.ROLE_ADMIN, actor="test")

    other = accounts.AccountStore(str(store.path))
    other.create("bob", accounts.ROLE_USER, actor="cli")

    assert store.by_username("bob") is not None, "missed another writer's change"
    store.create("carol", accounts.ROLE_USER, actor="test")
    assert {a.username for a in store.list()} == {"alice", "bob", "carol"}, \
        "a save built on a stale copy dropped somebody"


# ── Sessions ─────────────────────────────────────────────────────────

def test_a_minted_cookie_verifies():
    store = _fresh()
    admin = _enrolled_admin(store)
    assert auth._valid_account(auth._mint_account(admin)) == ("alice", "admin")


def test_a_tampered_cookie_does_not_verify():
    store = _fresh()
    admin = _enrolled_admin(store)
    token = auth._mint_account(admin)
    payload, _, sig = token.rpartition(".")
    for forged in (f"{payload}.{'0' * len(sig)}", f"{payload}x.{sig}",
                   token[:-1], "", "...", f"nonsense.{sig}"):
        assert auth._valid_account(forged) is None, forged


def test_an_expired_cookie_does_not_verify():
    store = _fresh()
    admin = _enrolled_admin(store)
    stale = auth._mint_account(admin, now=time.time() - AUTH.session_seconds - 10)
    assert auth._valid_account(stale) is None


def test_changing_a_password_ends_other_sessions():
    """
    The reason the stored hash is folded into the signing key. Without it, a
    password changed because it leaked would leave every session opened with
    the old one working until it expired.
    """
    store = _fresh()
    admin = _enrolled_admin(store)
    token = auth._mint_account(admin)
    assert auth._valid_account(token) is not None

    store.set_password(admin.id, "a-completely-different-password")
    assert auth._valid_account(token) is None, "the old session survived"


def test_disabling_and_deleting_end_sessions_immediately():
    for action in ("disable", "delete"):
        store = _fresh()
        admin = _enrolled_admin(store, "alice")
        victim, token = store.create("bob", accounts.ROLE_USER, actor="test")
        victim = store.redeem_invite(token, GOOD_PASSWORD)
        cookie = auth._mint_account(victim)
        assert auth._valid_account(cookie) is not None

        if action == "disable":
            store.update(victim.id, actor="test", disabled=True)
        else:
            store.delete(victim.id, actor="test")
        assert auth._valid_account(cookie) is None, f"{action} left a live session"
        assert admin  # the admin is only here to satisfy the last-admin guard


def test_a_role_change_invalidates_the_old_cookie():
    """
    The role is in the KEY, not the payload — so there is no role field to
    edit, and a demotion cannot be ridden out on an outstanding cookie.
    """
    store = _fresh()
    _enrolled_admin(store, "alice")
    other, token = store.create("bob", accounts.ROLE_ADMIN, actor="test")
    other = store.redeem_invite(token, GOOD_PASSWORD)
    cookie = auth._mint_account(other)
    assert auth._valid_account(cookie) == ("bob", "admin")

    store.update(other.id, actor="test", role=accounts.ROLE_USER)
    assert auth._valid_account(cookie) is None, "a demoted admin kept an admin cookie"


def test_break_glass_cookie_works_and_dies_with_the_passphrase():
    _fresh()
    cookie = auth._mint_breakglass()
    assert auth._valid_account(cookie) == (auth.BREAKGLASS_NAME, "admin")

    original = AUTH.admin_passphrase
    try:
        object.__setattr__(AUTH, "admin_passphrase", "")
        assert auth._valid_account(cookie) is None, \
            "withdrawing the passphrase must end its sessions"
    finally:
        object.__setattr__(AUTH, "admin_passphrase", original)


def test_break_glass_id_cannot_collide_with_a_real_account():
    assert accounts.username_error(auth.BREAKGLASS_ID) is not None
    assert not auth.BREAKGLASS_ID.startswith("u_")


# ── Throttle ─────────────────────────────────────────────────────────

def test_accounts_mode_backs_off_and_never_locks_out():
    """
    A hard lockout on an internet-facing login page is an outage switch: on the
    address key every caller looks the same under rootless podman, and on the
    username key anybody could lock out a named person on purpose. Backoff
    throttles guessing without handing anyone an off switch.
    """
    auth._clear_failures("user:victim")
    assert auth._uses_backoff(), "this test is meaningless outside accounts mode"

    delays = []
    for _ in range(6):
        auth._record_failure("user:victim")
        delays.append(auth._locked_until("user:victim") - time.time())

    assert all(d > 0 for d in delays), "backoff must actually delay"
    assert delays[3] > delays[0], f"backoff did not grow: {delays}"
    assert max(delays) <= auth._BACKOFF_CAP_SECONDS + 1, \
        f"backoff exceeded its cap: {delays}"
    auth._clear_failures("user:victim")


def test_shared_keys_get_a_free_allowance_and_targeted_ones_do_not():
    """
    Under rootless podman `ip:` and `enroll:` are shared by every caller, so a
    colleague mistyping a password must not slow everybody else down. A
    username is not shared, so its delay is paid only by whoever is guessing.
    """
    auth._clear_failures("ip:203.0.113.9", "user:alice", "enroll:203.0.113.9")

    auth._record_failure("user:alice")
    assert auth._locked_until("user:alice") > 0, "a username should bite at once"

    for _ in range(auth._FREE_ATTEMPTS["ip"]):
        auth._record_failure("ip:203.0.113.9")
    assert auth._locked_until("ip:203.0.113.9") == 0, \
        "ordinary fumbling on a shared address should be free"

    auth._record_failure("ip:203.0.113.9")
    assert auth._locked_until("ip:203.0.113.9") > 0, \
        "past the allowance a shared address must still back off"

    auth._clear_failures("ip:203.0.113.9", "user:alice", "enroll:203.0.113.9")


def test_both_keys_are_counted():
    """
    Recording only one of them leaves the other as an unthrottled way to make
    the same guesses.
    """
    auth._clear_failures("ip:198.51.100.7", "user:alice")
    # Enough to get past the shared key's allowance, recorded against both in
    # one call — which is what the login route does.
    for _ in range(auth._FREE_ATTEMPTS["ip"] + 1):
        auth._record_failure("ip:198.51.100.7", "user:alice")
    assert auth._locked_until("ip:198.51.100.7") > 0, "the address was not counted"
    assert auth._locked_until("user:alice") > 0, "the username was not counted"

    auth._clear_failures("ip:198.51.100.7", "user:alice")
    assert auth._locked_until("user:alice") == 0
    assert auth._locked_until("ip:198.51.100.7") == 0


def test_the_attempt_table_is_bounded():
    """A flood of made-up usernames must not grow this without limit."""
    for i in range(auth._ATTEMPTS_MAX_TRACKED + 50):
        auth._record_failure(f"user:flood-{i}")
    assert len(auth._attempts) <= auth._ATTEMPTS_MAX_TRACKED
    auth._attempts.clear()


def test_an_unknown_user_costs_the_same_as_a_wrong_password():
    """
    Otherwise login is an enumeration oracle anyone can read with a stopwatch:
    a missing account returns in microseconds, a real one takes ~60ms.
    """
    store = _fresh()
    admin = _enrolled_admin(store)

    start = time.perf_counter()
    accounts.verify_password("wrong-but-long-enough", admin.hash)
    real = time.perf_counter() - start

    start = time.perf_counter()
    accounts.burn_dummy_hash()
    dummy = time.perf_counter() - start

    # Generous: this is a CI-friendly assertion that the dummy path does real
    # work, not a timing-attack proof. An early return would be ~1000x faster.
    assert dummy > real / 4, (
        f"the unknown-user path took {dummy * 1000:.1f}ms against "
        f"{real * 1000:.1f}ms for a real verification — it is not burning a hash")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}\n        {exc}")
        except Exception as exc:  # a raised error is a failure too
            failures += 1
            print(f"  ERROR {name}\n        {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)

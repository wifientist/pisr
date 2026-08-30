"""
Local accounts: the second file PISR writes, and the first one holding secrets.

WHY THIS EXISTS. PISR's production gate was Cloudflare Access, and it stopped
working for a reason no amount of configuration fixes: Access's one-time-PIN
emails are silently discarded by the recipients' corporate mail filters, with
no bounce and no Access log row. Every email-based alternative — SES, magic
links, our own OTP — inherits that failure, because the failure is in somebody
else's mail policy and a brand-new sending domain is a *worse* signal to a
corporate filter than cloudflare.com, not a better one.

So this mode does not send mail. An admin creates an account here, PISR mints a
single-use enrolment link, and the admin delivers it OUT OF BAND — Teams, SMS,
read out over the phone, typed in by hand. The person sets a password and signs
in with it from then on. No IDP, no mail, no second factor, nothing that has to
be delivered at the moment somebody is trying to log in.

  Read `api/auth.py`'s docstring for how this fits the other two modes, and
  `api/visibility.py` for the file-on-a-volume pattern this copies.

THIS IS THE SECOND FILE PISR WRITES, and CLAUDE.md's "the ONE file it writes"
needed rewriting for it. The rule that matters — no snapshot files, no cache, no
database — is about TENANT data: a report is a live poll and must never be
persisted. This file holds usernames, roles and scrypt hashes for the OPERATORS
of the tool. It contains no venue, no device, no address, no R1 credential, and
nothing belonging to the customer whose network is being reported on. It is
`.env` with a portal in front of it.

  Guard that distinction the same way visibility.py asks you to. A file holding
  operator logins does not make this a tool that stores things.

NO PLAINTEXT PASSWORD IS EVER STORED, LOGGED OR RETURNED. What is on disk is a
scrypt hash; what a route returns is never the hash; what the log records is a
username and an outcome. The one secret that leaves this module is a freshly
minted invite token, returned exactly once to the admin who asked for it, and
even that is stored only as a SHA-256.

SHAPE ON DISK, versioned like the visibility policy so a later format can be
recognised rather than guessed at:

    {
      "version": 1,
      "users": [
        {
          "id": "u_xTgH2k9v",
          "username": "jsmith",
          "role": "user",
          "disabled": false,
          "hash": "scrypt$32768$8$1$<salt b64>$<key b64>",
          "invite": {"hash": "<sha256 hex>",
                     "issuedAt": "...", "expiresAt": "..."},
          "createdAt": "...", "createdBy": "alice",
          "passwordChangedAt": "..."
        }
      ]
    }

`hash` is absent until the person enrols; `invite` is absent once they have.
An account with neither cannot sign in and cannot enrol — reissue an invite.

FAILS CLOSED, unlike the visibility half of the policy file. That one is
de-cluttering and a corrupt file resolves to "hide nothing"; this one is the
gate itself, and a file that cannot be read means nobody signs in. The recovery
path is deliberate and documented: PISR_AUTH_ADMIN_PASSPHRASE in the
environment, which needs no file at all. See auth.py.

TWO WRITERS, WHICH IS THE ONE REAL DIFFERENCE FROM visibility.py. That file is
only ever written by the admin portal, inside this process. This one is also
written by `scripts/pisr_admin.py`, running in a SEPARATE container against the
same volume — that is how the first admin comes to exist. So:

  * the freshness check uses st_mtime_ns rather than st_mtime, because two
    writes within the same second are a thing that actually happens here and a
    float mtime can miss them;
  * every mutation is read-modify-write under the lock, re-reading the file
    first, so a change made by the CLI thirty seconds ago is not overwritten by
    a portal save built on a stale copy.

Neither of those is paranoia about concurrency in general. They are both about
the CLI, which is a second writer by design.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from config import AUTH

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES: Tuple[str, ...] = (ROLE_ADMIN, ROLE_USER)


# ── Password hashing ─────────────────────────────────────────────────
#
# hashlib.scrypt, which is stdlib. That is not a small point: requirements.txt
# lists 23 packages deliberately dropped when PISR was extracted, and adding
# argon2-cffi to hash a handful of passwords would be a real cost against a
# real principle. scrypt is memory-hard, it is in OpenSSL, and it is right
# here.
#
# Parameters are stored IN the hash string, so raising them later still leaves
# every existing hash verifiable — `needs_rehash` then upgrades each one at the
# next successful sign-in, which is the only moment the plaintext exists to
# rehash from.

_SCRYPT_N = 2 ** 15      # 32768
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# MUST BE SET EXPLICITLY, and this is the trap. 128 * N * R is exactly 32 MiB
# at these parameters, and OpenSSL's default maxmem is also 32 MiB — so the
# default fails with "memory limit exceeded" rather than being merely slow.
# Verified in the dev container: maxmem=0 raises, maxmem=64MiB takes ~62ms.
# Anyone lowering this must lower N with it.
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """A new scrypt hash, salted, with its parameters recorded alongside."""
    salt = secrets.token_bytes(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM, dklen=_SCRYPT_DKLEN)
    return "$".join((
        "scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        base64.b64encode(salt).decode(), base64.b64encode(key).decode()))


def verify_password(password: str, encoded: Optional[str]) -> bool:
    """
    Does this password match this hash?

    False for every malformed, missing or unparseable hash rather than raising:
    a corrupt record must fail to authenticate, not 500 the login route and
    tell the caller which account is broken.
    """
    if not encoded:
        return False
    try:
        scheme, n, r, p, salt_b64, key_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=int(n), r=int(r), p=int(p),
            maxmem=_SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError, MemoryError) as exc:
        logger.warning("accounts: unusable password hash (%s); refusing.",
                       type(exc).__name__)
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(encoded: Optional[str]) -> bool:
    """True when a stored hash predates the current parameters."""
    if not encoded:
        return False
    try:
        scheme, n, r, p, _, _ = encoded.split("$")
    except ValueError:
        return False
    return (scheme != "scrypt"
            or (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P))


# Burned against an unknown username so that "no such account" costs the same
# as "wrong password". Without it, login is an account enumeration oracle: a
# missing user returns in microseconds and a real one takes 60ms, which is a
# difference anyone can measure over the internet. Built once at import.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def burn_dummy_hash() -> None:
    """Spend the same time a real verification would, and learn nothing."""
    verify_password("not-the-password", _DUMMY_HASH)


# ── Usernames and passwords ──────────────────────────────────────────

# Deliberately narrow: lowercase, and the three separators a name plausibly
# contains. Usernames are case-folded on the way in, so `JSmith` and `jsmith`
# are the same account rather than two accounts one letter apart — which is the
# kind of thing that gets noticed only after somebody has been signing in as
# the wrong one for a week.
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")

USERNAME_RULE = ("2–32 characters, lowercase letters, digits, dot, dash or "
                 "underscore, starting with a letter or digit.")


def normalise_username(raw: str) -> str:
    return (raw or "").strip().casefold()


def username_error(username: str) -> Optional[str]:
    """Why this username is unusable, or None."""
    if not username:
        return "A username is required."
    if not _USERNAME_RE.match(username):
        return f"Not a usable username. {USERNAME_RULE}"
    return None


def password_error(password: str, username: str = "") -> Optional[str]:
    """
    Why this password is unusable, or None.

    Length only — no composition rules. A required digit and a required symbol
    push people towards `Password1!`, which is worse than a longer thing they
    can actually remember. The one extra test is that the password is not the
    username, which is the single most common way a floor gets satisfied
    without being met.
    """
    if not password:
        return "A password is required."
    if len(password) < AUTH.min_password_length:
        return (f"Passwords must be at least {AUTH.min_password_length} "
                "characters. Length is the only rule — a long ordinary phrase "
                "beats a short complicated one.")
    if username and password.casefold() == username.casefold():
        return "The password cannot be the username."
    return None


# ── Invites ──────────────────────────────────────────────────────────
#
# An invite is 256 bits from secrets.token_urlsafe, stored as a plain SHA-256.
# Plain, not scrypt, and that is correct rather than an oversight: a KDF exists
# to make GUESSING expensive, and there is nothing to guess here — the token is
# uniformly random and full-entropy. Hashing it at all is so that a stolen copy
# of accounts.json does not hand over live enrolment links.

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_iso(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Invite:
    """The half of an invite that is safe to hold in memory."""

    hash: str
    issued_at: Optional[str]
    expires_at: Optional[str]

    @property
    def expired(self) -> bool:
        """
        An invite with an unreadable expiry is treated as EXPIRED.

        Fails closed, unlike everything in visibility.py: this is the gate, and
        an unreadable date is not a reason to admit somebody indefinitely.
        """
        moment = _parse_iso(self.expires_at)
        return moment is None or moment <= _now()


@dataclass(frozen=True)
class Account:
    """One row, as the rest of PISR sees it."""

    id: str
    username: str
    role: str
    disabled: bool
    hash: Optional[str]
    invite: Optional[Invite]
    created_at: Optional[str]
    created_by: Optional[str]
    password_changed_at: Optional[str]

    @property
    def enrolled(self) -> bool:
        return bool(self.hash)

    @property
    def can_sign_in(self) -> bool:
        return self.enrolled and not self.disabled

    def public(self) -> Dict[str, Any]:
        """
        What the admin portal is allowed to see.

        THE HASH IS NOT IN HERE and must never be. It is the one field whose
        accidental inclusion would turn a portal that lists accounts into a
        portal that hands out offline-crackable material to anyone who is
        already an admin — and admins are not entitled to each other's
        passwords either.
        """
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "disabled": self.disabled,
            "enrolled": self.enrolled,
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "passwordChangedAt": self.password_changed_at,
            "invitePending": self.invite is not None and not self.invite.expired,
            "inviteExpiresAt": self.invite.expires_at if self.invite else None,
            "inviteExpired": self.invite is not None and self.invite.expired,
        }


def _account_from_raw(raw: Any) -> Optional[Account]:
    """
    One stored row, or None if it is not usable.

    Dropped rather than repaired. A row missing an id or a username cannot be
    addressed by any route, and inventing one would create an account nobody
    asked for.
    """
    if not isinstance(raw, dict):
        return None
    uid = raw.get("id")
    username = normalise_username(raw.get("username") or "")
    if not isinstance(uid, str) or not uid or not username:
        return None

    invite_raw = raw.get("invite")
    invite = None
    if isinstance(invite_raw, dict) and isinstance(invite_raw.get("hash"), str):
        invite = Invite(hash=invite_raw["hash"],
                        issued_at=invite_raw.get("issuedAt"),
                        expires_at=invite_raw.get("expiresAt"))

    # An unrecognised role reads as `user`, never as admin. A typo in a
    # hand-edited file should cost someone the portal, not hand it to them.
    role = raw.get("role")
    if role not in ROLES:
        if role is not None:
            logger.warning(
                "accounts: %r has unknown role %r; treating as %r.",
                username, role, ROLE_USER)
        role = ROLE_USER

    return Account(
        id=uid,
        username=username,
        role=role,
        disabled=bool(raw.get("disabled")),
        hash=raw.get("hash") if isinstance(raw.get("hash"), str) else None,
        invite=invite,
        created_at=raw.get("createdAt"),
        created_by=raw.get("createdBy"),
        password_changed_at=raw.get("passwordChangedAt"),
    )


def _account_to_raw(account: Account) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "disabled": account.disabled,
        "createdAt": account.created_at,
        "createdBy": account.created_by,
    }
    # Absent rather than null: an account that has not enrolled has no hash,
    # and `"hash": null` invites a reader to think the field means something.
    if account.hash:
        row["hash"] = account.hash
    if account.password_changed_at:
        row["passwordChangedAt"] = account.password_changed_at
    if account.invite:
        row["invite"] = {"hash": account.invite.hash,
                         "issuedAt": account.invite.issued_at,
                         "expiresAt": account.invite.expires_at}
    return row


class AccountsError(RuntimeError):
    """Something a caller did wrong, with a message fit to show them."""


class AccountStore:
    """
    Reads and writes one small JSON file, the same way `visibility.PolicyStore`
    does — see this module's docstring for the two differences that matter.
    """

    def __init__(self, path: Optional[str]):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._users: List[Account] = []
        self._stamp: Optional[Tuple[int, int]] = None
        self._loaded = False
        self._broken = False

    # ── state ────────────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def writable(self) -> bool:
        """
        Can an admin actually save?

        Tests the DIRECTORY, not the file: the file legitimately does not exist
        until the first account is created, and the failure this is really
        looking for is a container running without the volume — whose portal
        would otherwise accept a new account and lose it at the next deploy.
        """
        if not self.path:
            return False
        try:
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            return False

    @property
    def broken(self) -> bool:
        """
        True when the file exists but could not be read.

        Distinct from "no file yet", which is the ordinary state of a fresh
        deployment. This one means sign-in is impossible for everyone and the
        break-glass passphrase is the way back — so main.py says so at startup
        rather than leaving it to be discovered at a login prompt.
        """
        with self._lock:
            self._load()
            return self._broken

    # ── reading ──────────────────────────────────────────────────────

    def _stat(self) -> Optional[Tuple[int, int]]:
        """
        st_mtime_ns, not st_mtime. The CLI is a second writer against the same
        volume, so two writes inside one second is a real sequence here rather
        than a thought experiment, and a float mtime can compare equal across
        them.
        """
        try:
            info = self.path.stat()
            return (info.st_mtime_ns, info.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        """Re-read if the file changed. Caller holds the lock."""
        if not self.path:
            self._loaded = True
            return

        stamp = self._stat()
        if self._loaded and stamp == self._stamp:
            return

        if stamp is None:
            # No file yet — a fresh deployment, not an error. Nobody can sign
            # in until `scripts/pisr_admin.py` creates the first admin, which
            # main.py says at startup.
            self._users = []
            self._stamp = None
            self._loaded = True
            self._broken = False
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # FAILS CLOSED, unlike visibility.py. An unreadable policy means
            # "hide nothing"; an unreadable account file means nobody signs in.
            # Keeping a stale copy in memory would be worse, not better — it
            # would let a deleted account keep working for as long as the
            # process lived.
            logger.error(
                "accounts: could not read %s (%s). NOBODY CAN SIGN IN until "
                "this is fixed. Use PISR_AUTH_ADMIN_PASSPHRASE to get back in, "
                "then repair or delete the file.", self.path, exc)
            self._users = []
            self._stamp = stamp   # do not re-read a file we know is broken
            self._loaded = True
            self._broken = True
            return

        version = raw.get("version")
        if not isinstance(version, int) or version > FORMAT_VERSION:
            logger.error(
                "accounts: %s declares version %r, which this PISR does not "
                "understand (it writes version %d). Refusing to act on half of "
                "it — a login file read with a key missing is how a disabled "
                "account signs in.", self.path, version, FORMAT_VERSION)
            self._users = []
            self._stamp = stamp
            self._loaded = True
            self._broken = True
            return

        rows = raw.get("users")
        parsed: List[Account] = []
        seen: Dict[str, Account] = {}
        for row in rows if isinstance(rows, list) else []:
            account = _account_from_raw(row)
            if account is None:
                logger.warning("accounts: dropped an unreadable row in %s.", self.path)
                continue
            if account.username in seen or any(a.id == account.id for a in parsed):
                # Only reachable from a hand-edited file. Keeping the first is
                # arbitrary but stable; the alternative is a login whose result
                # depends on dictionary ordering.
                logger.warning(
                    "accounts: %s contains a duplicate username or id (%r); "
                    "keeping the first.", self.path, account.username)
                continue
            seen[account.username] = account
            parsed.append(account)

        self._users = parsed
        self._stamp = stamp
        self._loaded = True
        self._broken = False

    def list(self) -> List[Account]:
        with self._lock:
            self._load()
            return sorted(self._users, key=lambda a: a.username)

    def by_username(self, username: str) -> Optional[Account]:
        wanted = normalise_username(username)
        with self._lock:
            self._load()
            return next((a for a in self._users if a.username == wanted), None)

    def by_id(self, uid: str) -> Optional[Account]:
        with self._lock:
            self._load()
            return next((a for a in self._users if a.id == uid), None)

    def admin_count(self, exclude_id: Optional[str] = None) -> int:
        """
        Admins who could still sign in, optionally ignoring one.

        `can_sign_in` rather than a bare role test, deliberately: an admin who
        has never enrolled or has been disabled is not a way back into the
        portal, and counting them is how the last usable admin gets deleted.
        """
        with self._lock:
            self._load()
            return sum(1 for a in self._users
                       if a.role == ROLE_ADMIN and a.can_sign_in and a.id != exclude_id)

    def find_by_invite(self, token: str) -> Optional[Account]:
        """
        The account this invite token belongs to, expired or not.

        Expiry is the caller's to check and report, because "this link has
        expired, ask for another" and "this link is not valid" are different
        messages and only one of them is worth acting on.
        """
        if not token:
            return None
        digest = _token_hash(token)
        with self._lock:
            self._load()
            for account in self._users:
                if account.invite and hmac.compare_digest(account.invite.hash, digest):
                    return account
        return None

    # ── writing ──────────────────────────────────────────────────────

    def _write(self, users: List[Account]) -> None:
        """
        Replace the file. Caller holds the lock.

        Written to a temporary file in the SAME directory and renamed:
        `os.replace` is atomic within a filesystem, so a reader sees the old
        file or the new one and never a half-written one. A tempfile in /tmp
        would be on a different filesystem from the mounted volume and the
        rename would degrade to a copy, which is exactly what this avoids.
        """
        if not self.path:
            raise AccountsError(
                "PISR_ACCOUNTS_FILE is not set, so there is nowhere to save an "
                "account. Set it and mount a writable volume at its directory.")

        payload = {
            "version": FORMAT_VERSION,
            "users": [_account_to_raw(a) for a in sorted(users, key=lambda a: a.username)],
        }
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".accounts-", suffix=".tmp")
            try:
                # 0600 before anything is written to it. mkstemp already
                # creates it that way; this is here so that a later reader does
                # not have to know that, because the file holds password
                # hashes and the mode is not incidental.
                os.fchmod(handle, 0o600)
                with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                    tmp.write(body)
                    tmp.flush()
                    # The rename is atomic only relative to data that actually
                    # reached the disk. Without this, a power loss between
                    # rename and writeback leaves a correctly-named empty file
                    # — which for this file means every account is gone.
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, self.path)
            except BaseException:
                # Including SIGTERM during a deploy: a dot-prefixed temp file
                # left in a mounted volume is litter that accumulates one per
                # interrupted save, and this one would hold password hashes.
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise AccountsError(
                f"Could not write the accounts file to {self.path}: {exc}. "
                "Check the volume is mounted and writable by the container's "
                "user.") from exc

        self._users = sorted(users, key=lambda a: a.username)
        self._stamp = self._stat()
        self._loaded = True
        self._broken = False

    def _mutate(self, change: Callable[[List[Account]], Any]) -> Any:
        """
        Read-modify-write under the lock, re-reading the file first.

        The re-read is what makes the CLI safe to use while the container is
        running: without it, a portal save built on a copy loaded at startup
        would silently delete an account the CLI added since.

        A mutation on a BROKEN file is refused rather than allowed to overwrite
        it. The file is unreadable, so we do not know what is in it, and
        writing our idea of the account list over somebody's damaged-but-
        recoverable one turns a fixable problem into a permanent one.
        """
        with self._lock:
            self._load()
            if self._broken:
                raise AccountsError(
                    f"{self.path} exists but could not be read, so it will not "
                    "be overwritten — repair or move it first. Sign in with "
                    "PISR_AUTH_ADMIN_PASSPHRASE in the meantime.")
            users = list(self._users)
            result = change(users)
            self._write(users)
            return result

    # ── operations ───────────────────────────────────────────────────

    def create(self, username: str, role: str, actor: Optional[str],
               invite_days: Optional[int] = None) -> Tuple[Account, str]:
        """
        A new account and its enrolment token.

        THE TOKEN IS RETURNED ONCE AND NEVER STORED. What goes to disk is its
        SHA-256, so an admin who loses the link reissues one rather than
        recovering it — which is the property that makes a stolen accounts.json
        useless for signing in.
        """
        username = normalise_username(username)
        problem = username_error(username)
        if problem:
            raise AccountsError(problem)
        if role not in ROLES:
            raise AccountsError(f"Role must be one of {', '.join(ROLES)}.")

        token = secrets.token_urlsafe(32)

        def change(users: List[Account]) -> Account:
            if any(a.username == username for a in users):
                raise AccountsError(f"There is already an account called {username!r}.")
            account = Account(
                id="u_" + secrets.token_urlsafe(8),
                username=username,
                role=role,
                disabled=False,
                hash=None,
                invite=self._new_invite(token, invite_days),
                created_at=_iso(_now()),
                created_by=actor or "unknown",
                password_changed_at=None,
            )
            users.append(account)
            return account

        account = self._mutate(change)
        logger.info("accounts: %s created %r as %s", actor or "unknown",
                    account.username, account.role)
        return account, token

    @staticmethod
    def _new_invite(token: str, invite_days: Optional[int]) -> Invite:
        days = AUTH.invite_days if invite_days is None else invite_days
        issued = _now()
        return Invite(hash=_token_hash(token),
                      issued_at=_iso(issued),
                      expires_at=_iso(issued + timedelta(days=days)))

    def issue_invite(self, uid: str, actor: Optional[str],
                     invite_days: Optional[int] = None) -> Tuple[Account, str]:
        """
        A fresh enrolment link for an existing account — the password reset.

        THE EXISTING PASSWORD IS LEFT ALONE until the invite is actually
        redeemed. An admin who clicks "reset" by accident has not locked
        anybody out, and somebody who never receives the link is no worse off
        than before it was issued. Clearing the hash here would make a
        mis-click into an outage.
        """
        token = secrets.token_urlsafe(32)

        def change(users: List[Account]) -> Account:
            index, existing = _find(users, uid)
            updated = _replace(existing, invite=self._new_invite(token, invite_days))
            users[index] = updated
            return updated

        account = self._mutate(change)
        logger.info("accounts: %s issued an enrolment link for %r",
                    actor or "unknown", account.username)
        return account, token

    def redeem_invite(self, token: str, password: str) -> Account:
        """
        Set a password using a valid invite, and consume it.

        Single-use: the invite is dropped in the same write that stores the
        hash, so a link that leaks after the fact opens nothing. Re-checked
        inside the mutation rather than trusting the caller's earlier lookup —
        between a GET that validated the token and this POST, an admin may have
        reissued or deleted it.
        """
        def change(users: List[Account]) -> Account:
            digest = _token_hash(token or "")
            match = next(
                ((i, a) for i, a in enumerate(users)
                 if a.invite and hmac.compare_digest(a.invite.hash, digest)),
                None)
            if match is None:
                raise AccountsError("That enrolment link is not valid.")
            index, existing = match
            if existing.invite.expired:
                raise AccountsError(
                    "That enrolment link has expired. Ask an administrator for "
                    "a new one.")
            if existing.disabled:
                # Deliberately the same message as an invalid link. A disabled
                # account should not be able to confirm its own existence to
                # somebody holding an old invite.
                raise AccountsError("That enrolment link is not valid.")
            problem = password_error(password, existing.username)
            if problem:
                raise AccountsError(problem)
            users[index] = _replace(
                existing,
                hash=hash_password(password),
                invite=None,
                password_changed_at=_iso(_now()))
            return users[index]

        account = self._mutate(change)
        logger.info("accounts: %r enrolled and set a password", account.username)
        return account

    def set_password(self, uid: str, password: str) -> Account:
        """Change a password in place — the self-service path."""
        def change(users: List[Account]) -> Account:
            index, existing = _find(users, uid)
            problem = password_error(password, existing.username)
            if problem:
                raise AccountsError(problem)
            # Any outstanding invite is dropped: a reset link that still worked
            # after the owner set a new password would be a second, older key
            # to the same door.
            users[index] = _replace(existing,
                                    hash=hash_password(password),
                                    invite=None,
                                    password_changed_at=_iso(_now()))
            return users[index]

        account = self._mutate(change)
        logger.info("accounts: %r changed their password", account.username)
        return account

    def upgrade_hash(self, uid: str, password: str) -> None:
        """
        Re-hash at the current parameters, at the one moment the plaintext is
        available. Best-effort: a failure here must never fail the sign-in that
        triggered it, because the old hash is still perfectly valid.
        """
        try:
            def change(users: List[Account]) -> None:
                index, existing = _find(users, uid)
                if needs_rehash(existing.hash):
                    users[index] = _replace(existing, hash=hash_password(password))

            self._mutate(change)
        except (AccountsError, OSError) as exc:
            logger.warning("accounts: could not upgrade the hash for %s: %s", uid, exc)

    def update(self, uid: str, actor: Optional[str], *,
               role: Optional[str] = None,
               disabled: Optional[bool] = None) -> Account:
        """
        Change a role or enable/disable. Refuses to remove the last admin.

        The guard is the same principle as `admin_router`'s "admins are never
        subject to the policy": there must be no state an admin can reach that
        only an SSH session can get them out of, and this box is deliberately
        awkward to SSH into.
        """
        if role is not None and role not in ROLES:
            raise AccountsError(f"Role must be one of {', '.join(ROLES)}.")

        def change(users: List[Account]) -> Account:
            index, existing = _find(users, uid)
            losing_admin = (
                existing.role == ROLE_ADMIN and existing.can_sign_in
                and ((role is not None and role != ROLE_ADMIN) or disabled is True))
            if losing_admin and self._admin_count_in(users, exclude_id=uid) == 0:
                raise AccountsError(
                    f"{existing.username} is the only administrator who can "
                    "sign in. Promote somebody else first, or you will have "
                    "nobody left who can reach this portal.")
            users[index] = _replace(
                existing,
                role=existing.role if role is None else role,
                disabled=existing.disabled if disabled is None else disabled)
            return users[index]

        account = self._mutate(change)
        logger.info("accounts: %s updated %r — role=%s disabled=%s",
                    actor or "unknown", account.username,
                    account.role, account.disabled)
        return account

    def delete(self, uid: str, actor: Optional[str]) -> Account:
        """Remove an account. Refuses to remove the last admin."""
        def change(users: List[Account]) -> Account:
            index, existing = _find(users, uid)
            if (existing.role == ROLE_ADMIN and existing.can_sign_in
                    and self._admin_count_in(users, exclude_id=uid) == 0):
                raise AccountsError(
                    f"{existing.username} is the only administrator who can "
                    "sign in, so deleting them would leave this instance with "
                    "no way into the portal.")
            return users.pop(index)

        account = self._mutate(change)
        logger.info("accounts: %s deleted %r", actor or "unknown", account.username)
        return account

    @staticmethod
    def _admin_count_in(users: List[Account], exclude_id: Optional[str]) -> int:
        return sum(1 for a in users
                   if a.role == ROLE_ADMIN and a.can_sign_in and a.id != exclude_id)

    # ── reporting ────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        """What the admin portal needs to draw itself."""
        with self._lock:
            self._load()
            return {
                "users": [a.public() for a in sorted(self._users, key=lambda a: a.username)],
                "roles": list(ROLES),
                "writable": self.writable,
                "broken": self._broken,
                "path": str(self.path) if self.path else None,
                "usernameRule": USERNAME_RULE,
                "minPasswordLength": AUTH.min_password_length,
                "inviteDays": AUTH.invite_days,
            }


def _find(users: List[Account], uid: str) -> Tuple[int, Account]:
    for index, account in enumerate(users):
        if account.id == uid:
            return index, account
    raise AccountsError("No such account.")


def _replace(account: Account, **changes: Any) -> Account:
    """
    dataclasses.replace by hand, so that `invite=None` clears an invite rather
    than being mistaken for "leave it alone". Every caller here that passes
    invite=None means to consume it.
    """
    fields = {
        "id": account.id, "username": account.username, "role": account.role,
        "disabled": account.disabled, "hash": account.hash,
        "invite": account.invite, "created_at": account.created_at,
        "created_by": account.created_by,
        "password_changed_at": account.password_changed_at,
    }
    fields.update(changes)
    return Account(**fields)


STORE = AccountStore(AUTH.accounts_file)

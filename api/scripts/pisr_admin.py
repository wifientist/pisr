"""
Manage local accounts from the command line.

THIS IS HOW THE FIRST ADMIN EXISTS. Accounts mode has no self-registration and
no default login — a fresh deployment has an empty accounts file and nobody who
can sign in, which is the correct starting state and also a chicken-and-egg
problem. This resolves it: create the first admin here, and every account after
that can be created from the portal.

IT IS ALSO THE RECOVERY PATH. Deleted the last admin, lost the volume, or
corrupted the file — this runs against the volume without needing a session, a
password or a working gate.

    docker compose run --rm pisr python scripts/pisr_admin.py list
    docker compose run --rm pisr python scripts/pisr_admin.py add-user alice --admin
    docker compose run --rm pisr python scripts/pisr_admin.py invite alice
    docker compose run --rm pisr python scripts/pisr_admin.py set-role alice user
    docker compose run --rm pisr python scripts/pisr_admin.py disable alice
    docker compose run --rm pisr python scripts/pisr_admin.py enable alice
    docker compose run --rm pisr python scripts/pisr_admin.py delete alice

    (dev: docker compose -f docker-compose.dev.yml exec backend \
          python scripts/pisr_admin.py list)

IT RUNS IN A SEPARATE CONTAINER FROM THE SERVER, against the same volume. That
is why `accounts.AccountStore` re-reads before every mutation and compares
st_mtime_ns rather than st_mtime — see its module docstring. Nothing here needs
the server stopped.

IT PRINTS ENROLMENT LINKS, NOT PASSWORDS. There is no `set-password` command,
deliberately: a password typed on a command line lands in the shell history and
in the process table, and the person whose account it is should be the only one
who ever knows it. Hand over the link instead; it is single-use and expires.

The link's host is a guess — this process has no idea what address people reach
PISR on — so it prints a path and says so. The portal, which is answering a
real request, prints a complete URL.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import accounts  # noqa: E402
from config import AUTH  # noqa: E402


def _store() -> accounts.AccountStore:
    if not accounts.STORE.configured:
        _die("PISR_ACCOUNTS_FILE is not set, so there is nowhere to keep "
             "accounts.")
    return accounts.STORE


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _find(username: str):
    account = _store().by_username(username)
    if account is None:
        _die(f"no account called {username!r}. `list` shows what there is.")
    return account


def _print_link(token: str) -> None:
    print()
    print("  Enrolment link — hand this over out of band (Teams, SMS, in")
    print("  person). It is single-use, it expires, and it CANNOT be shown")
    print("  again: only its hash is stored. Reissue with `invite` if lost.")
    print()
    print(f"      https://<your-pisr-address>/?enroll={token}")
    print()
    print(f"  Valid for {AUTH.invite_days} day(s).")
    print()


def cmd_list(args) -> None:
    store = _store()
    if store.broken:
        _die(f"{store.path} exists but could not be read. Repair or move it.")

    people = store.list()
    if not people:
        print("No accounts yet. Create the first admin:")
        print("    pisr_admin.py add-user <name> --admin")
        return

    width = max(len(a.username) for a in people)
    print(f"{'USERNAME'.ljust(width)}  ROLE   STATE")
    for account in people:
        if account.disabled:
            state = "disabled"
        elif not account.enrolled:
            state = ("invite expired" if account.invite and account.invite.expired
                     else "invited, not yet enrolled" if account.invite
                     else "NO PASSWORD AND NO INVITE — run `invite`")
        else:
            state = "ok"
        print(f"{account.username.ljust(width)}  {account.role.ljust(5)}  {state}")

    admins = sum(1 for a in people if a.role == "admin" and a.can_sign_in)
    if not admins:
        print()
        print("warning: no admin can sign in, so the portals are unreachable.")


def cmd_add_user(args) -> None:
    store = _store()
    role = accounts.ROLE_ADMIN if args.admin else accounts.ROLE_USER
    try:
        account, token = store.create(args.username, role, actor="cli")
    except accounts.AccountsError as exc:
        _die(str(exc))
    print(f"Created {account.username} as {account.role}.")
    _print_link(token)


def cmd_invite(args) -> None:
    account = _find(args.username)
    try:
        account, token = _store().issue_invite(account.id, actor="cli")
    except accounts.AccountsError as exc:
        _die(str(exc))
    note = ("Their existing password keeps working until this link is used."
            if account.enrolled else "")
    print(f"New enrolment link for {account.username}. {note}".rstrip())
    _print_link(token)


def cmd_set_role(args) -> None:
    account = _find(args.username)
    try:
        account = _store().update(account.id, actor="cli", role=args.role)
    except accounts.AccountsError as exc:
        _die(str(exc))
    print(f"{account.username} is now {account.role}.")


def cmd_disable(args) -> None:
    account = _find(args.username)
    try:
        account = _store().update(account.id, actor="cli", disabled=True)
    except accounts.AccountsError as exc:
        _die(str(exc))
    print(f"{account.username} is disabled. Every session they held is now dead.")


def cmd_enable(args) -> None:
    account = _find(args.username)
    try:
        account = _store().update(account.id, actor="cli", disabled=False)
    except accounts.AccountsError as exc:
        _die(str(exc))
    print(f"{account.username} is enabled.")


def cmd_delete(args) -> None:
    account = _find(args.username)
    if not args.yes:
        # A password reset is the usual intent behind "remove this person", and
        # it is not what this does. Deleting loses the account; `disable` keeps
        # it and stops them signing in, which is reversible.
        reply = input(f"Delete {account.username} permanently? "
                      "(`disable` is reversible) [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("Left alone.")
            return
    try:
        _store().delete(account.id, actor="cli")
    except accounts.AccountsError as exc:
        _die(str(exc))
    print(f"Deleted {account.username}.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pisr_admin.py",
        description="Manage PISR's local accounts. Creates the first admin, "
                    "and recovers an instance nobody can sign in to.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show every account").set_defaults(func=cmd_list)

    add = sub.add_parser("add-user", help="create an account and print its link")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true",
                     help="give the admin role rather than user")
    add.set_defaults(func=cmd_add_user)

    inv = sub.add_parser("invite", help="reissue an enrolment link (the reset)")
    inv.add_argument("username")
    inv.set_defaults(func=cmd_invite)

    role = sub.add_parser("set-role", help="change somebody's role")
    role.add_argument("username")
    role.add_argument("role", choices=list(accounts.ROLES))
    role.set_defaults(func=cmd_set_role)

    dis = sub.add_parser("disable", help="stop somebody signing in, reversibly")
    dis.add_argument("username")
    dis.set_defaults(func=cmd_disable)

    ena = sub.add_parser("enable", help="undo disable")
    ena.add_argument("username")
    ena.set_defaults(func=cmd_enable)

    dele = sub.add_parser("delete", help="remove an account permanently")
    dele.add_argument("username")
    dele.add_argument("--yes", action="store_true", help="skip the prompt")
    dele.set_defaults(func=cmd_delete)

    args = parser.parse_args()

    if AUTH.mode != "accounts":
        # A warning rather than a refusal: creating the accounts BEFORE
        # flipping PISR_AUTH_MODE is the sensible migration order, exactly as
        # it is for PISR_ADMIN_EMAILS and proxy mode.
        print(f"note: PISR_AUTH_MODE is {AUTH.mode!r}, so these accounts do not "
              "decide anything yet.\n", file=sys.stderr)

    args.func(args)


if __name__ == "__main__":
    main()

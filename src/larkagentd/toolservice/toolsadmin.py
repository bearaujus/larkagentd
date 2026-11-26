"""Switch tools on and off and grant them to people, until the dashboard exists.

    larkagentd-tool-admin list
    larkagentd-tool-admin enable|disable TOOL
    larkagentd-tool-admin grant|deny|revoke TOOL USER      (USER: a saved Lark email, or an owner id)

It needs only DATABASE_URL. `enable` and `disable` change the tool's flag for everyone, which has
no effect on a write, destructive or execute tool: those run only for a person with a grant. A
tool without an executor in the code can be neither enabled nor granted.
"""

import argparse
import getpass
import os
import sys

from ..core.fields import normalize_email
from ..core.store import Store
from .registry import ToolRegistry


class AdminError(Exception):
    pass


def resolve_owner(store, who):
    """An owner id from a saved Lark email or from the id itself. One match is required."""
    who = who.strip()
    if "@" not in who:
        with store.engine.connect() as conn:
            known = store.owner_exists(conn, who)
        if not known:
            raise AdminError("no_such_owner")
        return who
    try:
        email = normalize_email(who)
    except ValueError:
        raise AdminError("bad_email") from None
    with store.engine.connect() as conn:
        found = store.owners_with_email(conn, email)
    if len(found) != 1:
        raise AdminError("no_match" if not found else "ambiguous")
    return found[0]


def show(rows):
    for tool in rows:
        status = (
            "retired"
            if tool["retired"]
            else "no executor"
            if not tool["implemented"]
            else "on for everyone"
            if tool["enabled"] and tool["risk"] in ("read", "own", "costly")
            else "off"
        )
        print(f"{tool['name']:22} {tool['kind']:9} {tool['risk']:11} {status}")
        for principal, allowed in tool["grants"]:
            print(f"{'':22} {'allow' if allowed else 'deny':6} {principal}")


def run(args, store):
    registry = ToolRegistry(store)
    if args.command == "list":
        show(registry.listing())
        return 0
    if args.command in ("enable", "disable"):
        if not registry.set_enabled(args.tool, args.command == "enable"):
            raise AdminError("no_such_tool")
        print(f"{args.tool}: {args.command}d for everyone")
        return 0
    owner = resolve_owner(store, args.user)
    if args.command == "revoke":
        done = registry.revoke(args.tool, owner)
        print(f"{args.tool}: grant {'removed' if done else 'was not set'}")
        return 0
    allowed = args.command == "grant"
    if not registry.grant(args.tool, owner, allowed, by=getpass.getuser()):
        raise AdminError("no_such_tool")
    print(f"{args.tool}: {'allowed' if allowed else 'denied'} for the chosen person")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="larkagentd-tool-admin")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("tool")
    for name in ("grant", "deny", "revoke"):
        one = sub.add_parser(name)
        one.add_argument("tool")
        one.add_argument("user")
    return p


def main(argv=None, store=None):
    args = parser().parse_args(argv)
    try:
        if store is None:
            if not os.environ.get("DATABASE_URL"):
                raise AdminError("DATABASE_URL_missing")
            store = Store(os.environ["DATABASE_URL"])
        return run(args, store)
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

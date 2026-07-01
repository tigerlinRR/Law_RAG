#!/usr/bin/env python
"""Manage users and their client access (ethical walls).

  python scripts/user_admin.py add    <username> --password PW [--role lawyer|admin] [--clients A B]
  python scripts/user_admin.py passwd <username> --password PW
  python scripts/user_admin.py grant  <username> <client>
  python scripts/user_admin.py revoke <username> <client>
  python scripts/user_admin.py list

An 'admin' user sees all clients; a 'lawyer' sees only granted clients.
"""
from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from lawrag import auth, db

console = Console()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("--password", required=True)
    a.add_argument("--role", default="lawyer", choices=["lawyer", "admin"])
    a.add_argument("--clients", nargs="*", default=[])

    p = sub.add_parser("passwd")
    p.add_argument("username")
    p.add_argument("--password", required=True)

    g = sub.add_parser("grant"); g.add_argument("username"); g.add_argument("client")
    r = sub.add_parser("revoke"); r.add_argument("username"); r.add_argument("client")
    sub.add_parser("list")
    args = ap.parse_args()

    db.init_schema()  # ensure auth tables exist

    if args.cmd == "add":
        auth.create_user(args.username, args.password, args.role, args.clients)
        console.print(f"[green]created[/] {args.username} ({args.role}), "
                      f"clients={args.clients or 'ALL' if args.role == 'admin' else args.clients}")
    elif args.cmd == "passwd":
        auth.set_password(args.username, args.password)
        console.print(f"[green]password updated[/] for {args.username}")
    elif args.cmd == "grant":
        auth.grant(args.username, args.client)
        console.print(f"[green]granted[/] {args.username} -> {args.client}")
    elif args.cmd == "revoke":
        auth.revoke(args.username, args.client)
        console.print(f"[yellow]revoked[/] {args.username} -> {args.client}")
    elif args.cmd == "list":
        table = Table("User", "Role", "Clients")
        for u in auth.list_users():
            clients = "ALL" if u["role"] == "admin" else (", ".join(u["clients"]) or "—")
            table.add_row(u["username"], u["role"], clients)
        console.print(table)


if __name__ == "__main__":
    main()

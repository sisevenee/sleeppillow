#!/usr/bin/env python3
"""Create and list Sleep Pillow App accounts without storing plaintext passwords."""

from __future__ import annotations

import argparse
import getpass
import re
import sys

import pymysql

from pillow_api_mysql import db_connection, password_hash

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def create_user(username: str, role: str) -> None:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Username must be 3-64 characters: letters, digits, _, -, or .")
    password = getpass.getpass(f"Set password for {username}: ")
    confirmation = getpass.getpass("Confirm password: ")
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
            (username, password_hash(password), role),
        )
    print(f"Created {role} account: {username}")


def list_users() -> None:
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT username, role, is_active, created_at FROM users ORDER BY role, username")
        rows = cursor.fetchall()
    for row in rows:
        print(f"{row['username']}\t{row['role']}\t{'active' if row['is_active'] else 'disabled'}\t{row['created_at']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create", help="Create one account and prompt for its password")
    create.add_argument("username")
    create.add_argument("--role", choices=("admin", "user"), default="user")
    subcommands.add_parser("list", help="List account names and roles; passwords are never shown")
    args = parser.parse_args()
    try:
        if args.command == "create":
            create_user(args.username, args.role)
        else:
            list_users()
    except (ValueError, pymysql.MySQLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

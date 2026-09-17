#!/usr/bin/env python3
"""Create and manage independently authenticated ESP32 devices."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import sys

import pymysql
from pymysql.cursors import DictCursor

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ.get("PILLOW_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("PILLOW_MYSQL_PORT", "3306")),
        user=os.environ.get("PILLOW_MYSQL_USER", "pillow_api"),
        password=os.environ["PILLOW_MYSQL_PASSWORD"],
        database=os.environ.get("PILLOW_MYSQL_DATABASE", "pillow"),
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


def create_device(device_id: str, display_name: str) -> None:
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("device ID must contain only letters, digits, _ or -, up to 64 characters")
    token = secrets.token_urlsafe(32)
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO devices (device_id, display_name) VALUES (%s, %s)",
            (device_id, display_name),
        )
        cursor.execute(
            "INSERT INTO device_credentials (device_id, token_hash, label) VALUES (%s, %s, %s)",
            (cursor.lastrowid, token_digest, "initial provisioning token"),
        )
    # This is the only time this token is available in plaintext. Put it in the
    # target ESP32 provisioning configuration and do not save it in Git.
    print(f"deviceId={device_id}")
    print(f"deviceToken={token}")


def list_devices() -> None:
    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT device_id, display_name, is_active, created_at FROM devices ORDER BY device_id"
        )
        for row in cursor.fetchall():
            print(f"{row['device_id']}\t{row['display_name']}\tactive={bool(row['is_active'])}\tcreated={row['created_at']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add")
    add.add_argument("device_id")
    add.add_argument("display_name")
    commands.add_parser("list")
    args = parser.parse_args()
    if args.command == "add":
        create_device(args.device_id, args.display_name)
    else:
        list_devices()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, pymysql.MySQLError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)

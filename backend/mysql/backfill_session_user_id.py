#!/usr/bin/env python3
"""Backfill sleep_sessions.user_id for sessions that predate the归属链 fix.

This script only fills rows whose user_id IS NULL. It never overwrites a value that is
already present, so re-running it is safe and it will not undo any live attribution that
the API wrote at ingest time.

Attribution source, in order of decreasing confidence:

  1. sleep_manual_markers — a point-in-time record that this user was using this device.
     A session started/ended within the marker's window is attributed to that user. This is
     the only source that survives a device being handed to a different participant.
  2. user_current_devices — the device's current owner. Correct when the device never changed
     hands, and is the same fallback the API applies at query time.

Always preview first:

    python3 backfill_session_user_id.py --dry-run
    python3 backfill_session_user_id.py --apply

The script refuses to run with --apply unless the sleep_sessions.user_id column exists;
run migrate_sessions_user_id.sql first.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta

import pymysql
from pymysql.cursors import DictCursor

MYSQL_CONFIG = {
    "host": os.environ.get("PILLOW_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PILLOW_MYSQL_PORT", "3306")),
    "user": os.environ.get("PILLOW_MYSQL_USER", "pillow_api"),
    "password": os.environ.get("PILLOW_MYSQL_PASSWORD", ""),
    "database": os.environ.get("PILLOW_MYSQL_DATABASE", "pillow"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": False,
}

# A manual marker is a momentary tap, so it only proves usage within a window around it.
# 12 hours is wide enough to cover "prepare" (evening) through "wake" (next morning) and
# narrow enough that it will not claim a different participant's recording.
MARKER_MATCH_WINDOW = timedelta(hours=12)


def column_exists(cursor: pymysql.cursors.Cursor) -> bool:
    cursor.execute(
        """
        SELECT COUNT(*) AS count FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sleep_sessions'
          AND COLUMN_NAME = 'user_id'
        """
    )
    return int(cursor.fetchone()["count"]) > 0


def load_pending_sessions(cursor: pymysql.cursors.Cursor) -> list[dict]:
    cursor.execute(
        """
        SELECT s.id, s.device_id, s.started_at,
               COALESCE(s.ended_at, s.last_sample_at, s.started_at) AS effective_end
        FROM sleep_sessions s
        WHERE s.user_id IS NULL
        ORDER BY s.device_id, s.started_at
        """
    )
    return cursor.fetchall()


def load_markers_by_device(cursor: pymysql.cursors.Cursor) -> dict[int, list[dict]]:
    cursor.execute(
        """
        SELECT device_id, user_id, marked_at
        FROM sleep_manual_markers
        ORDER BY device_id, marked_at
        """
    )
    grouped: dict[int, list[dict]] = {}
    for row in cursor.fetchall():
        grouped.setdefault(int(row["device_id"]), []).append(row)
    return grouped


def load_current_owners(cursor: pymysql.cursors.Cursor) -> dict[int, dict]:
    cursor.execute(
        """
        SELECT ucd.device_id, u.id AS user_id, u.username, ucd.selected_at
        FROM user_current_devices ucd
        JOIN users u ON u.id = ucd.user_id
        WHERE u.role = 'user' AND u.is_active = 1
        """
    )
    return {int(row["device_id"]): row for row in cursor.fetchall()}


def marker_owner(session: dict, markers: list[dict]) -> dict | None:
    """Return the marker whose timestamp sits closest to the session, within the window."""
    session_start = session["started_at"]
    session_end = session["effective_end"]
    best: dict | None = None
    best_distance: timedelta | None = None
    for marker in markers:
        marked_at = marker["marked_at"]
        # Distance is zero when the marker falls inside the session.
        if session_start <= marked_at <= session_end:
            distance = timedelta(0)
        elif marked_at < session_start:
            distance = session_start - marked_at
        else:
            distance = marked_at - session_end
        if distance > MARKER_MATCH_WINDOW:
            continue
        if best_distance is None or distance < best_distance:
            best = marker
            best_distance = distance
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill sleep_sessions.user_id")
    parser.add_argument("--apply", action="store_true", help="write the attribution (default is read-only)")
    parser.add_argument("--dry-run", action="store_true", help="explicitly request the read-only preview")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        raise SystemExit("Choose either --dry-run or --apply, not both.")
    if not MYSQL_CONFIG["password"]:
        raise SystemExit("PILLOW_MYSQL_PASSWORD is required")

    with pymysql.connect(**MYSQL_CONFIG) as connection:
        with connection.cursor() as cursor:
            if not column_exists(cursor):
                raise SystemExit(
                    "sleep_sessions.user_id does not exist. Run migrate_sessions_user_id.sql first."
                )
            sessions = load_pending_sessions(cursor)
            markers_by_device = load_markers_by_device(cursor)
            current_owners = load_current_owners(cursor)

        if not sessions:
            print("No unattributed sessions. Nothing to do.", flush=True)
            return

        resolutions: list[tuple[int, int, str]] = []
        unassigned = 0
        for session in sessions:
            device_id = int(session["device_id"])
            marker = marker_owner(session, markers_by_device.get(device_id, []))
            if marker is not None:
                resolutions.append((int(session["id"]), int(marker["user_id"]), "marker"))
                continue
            owner = current_owners.get(device_id)
            if owner is not None:
                resolutions.append((int(session["id"]), int(owner["user_id"]), "current_device"))
                continue
            unassigned += 1
            print(
                f"  session {session['id']} (device {device_id}) has no marker and no current owner; left NULL",
                flush=True,
            )

        by_source: dict[str, int] = {}
        for _, _, source in resolutions:
            by_source[source] = by_source.get(source, 0) + 1
        print(f"Unattributed sessions found: {len(sessions)}", flush=True)
        for source, count in sorted(by_source.items()):
            print(f"  resolvable via {source}: {count}", flush=True)
        print(f"  left as NULL: {unassigned}", flush=True)

        if not args.apply:
            print("Dry run only. Re-run with --apply to write these values.", flush=True)
            return

        with connection.cursor() as cursor:
            cursor.executemany(
                "UPDATE sleep_sessions SET user_id = %s WHERE id = %s AND user_id IS NULL",
                [(user_id, session_id) for session_id, user_id, _ in resolutions],
            )
        connection.commit()
        print(f"Updated {len(resolutions)} session(s).", flush=True)


if __name__ == "__main__":
    main()

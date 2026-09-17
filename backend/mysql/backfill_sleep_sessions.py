#!/usr/bin/env python3
"""One-time, non-destructive backfill of sleep_sessions from existing telemetry."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pymysql
from pymysql.cursors import DictCursor

GAP = timedelta(minutes=15)
MYSQL_CONFIG = {
    "host": os.environ.get("PILLOW_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("PILLOW_MYSQL_PORT", "3306")),
    "user": os.environ.get("PILLOW_MYSQL_USER", "pillow_api"),
    "password": os.environ.get("PILLOW_MYSQL_PASSWORD", ""),
    "database": os.environ.get("PILLOW_MYSQL_DATABASE", "pillow"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}


def insert_session(cursor: pymysql.cursors.Cursor, device_id: int, started_at: datetime, last_sample_at: datetime, closed: bool) -> None:
    cursor.execute(
        """
        INSERT INTO sleep_sessions (
            device_id, started_at, ended_at, last_sample_at, start_source, end_source, closed_at
        ) VALUES (%s, %s, %s, %s, 'telemetry', %s, %s)
        """,
        (
            device_id,
            started_at,
            last_sample_at if closed else None,
            last_sample_at,
            "data_gap" if closed else None,
            datetime.utcnow() if closed else None,
        ),
    )


def main() -> None:
    if not MYSQL_CONFIG["password"]:
        raise RuntimeError("PILLOW_MYSQL_PASSWORD is required")
    with pymysql.connect(**MYSQL_CONFIG) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM sleep_sessions")
        existing = int(cursor.fetchone()["count"])
        if existing:
            raise RuntimeError(
                "sleep_sessions already contains data. Backfill stops without changing existing sessions."
            )
        cursor.execute("SELECT id, device_id FROM devices ORDER BY id")
        devices = cursor.fetchall()
        inserted = 0
        now = datetime.utcnow()
        for device in devices:
            cursor.execute(
                "SELECT sampled_at FROM telemetry WHERE device_id = %s ORDER BY sampled_at ASC, id ASC",
                (device["id"],),
            )
            samples = cursor.fetchall()
            if not samples:
                continue
            started_at = samples[0]["sampled_at"]
            previous_at = started_at
            for sample in samples[1:]:
                sampled_at = sample["sampled_at"]
                if sampled_at > previous_at + GAP:
                    insert_session(cursor, int(device["id"]), started_at, previous_at, True)
                    inserted += 1
                    started_at = sampled_at
                previous_at = sampled_at
            # A very recent final segment remains active so new uploaded samples can continue it.
            insert_session(cursor, int(device["id"]), started_at, previous_at, previous_at < now - GAP)
            inserted += 1
    print(f"Backfilled {inserted} sleep session(s).", flush=True)


if __name__ == "__main__":
    main()

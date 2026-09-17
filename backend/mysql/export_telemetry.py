#!/usr/bin/env python3
"""Export one device's MySQL telemetry to a CSV aligned with the SD log."""

from __future__ import annotations

import argparse
import csv
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
from pymysql.cursors import DictCursor

CHINA_TIME = ZoneInfo("Asia/Shanghai")
CSV_HEADER = [
    "Timestamp", "DateTime", "HeartRate", "RespiratoryRate", "Temperature",
    "SleepStage", "Confidence", "TimeSource", "AnalogVoltage", "FPGA_Input",
    "DeviceId", "ServerReceivedAt",
]


def db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ.get("PILLOW_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("PILLOW_MYSQL_PORT", "3306")),
        user=os.environ.get("PILLOW_MYSQL_USER", "pillow_api"),
        password=os.environ["PILLOW_MYSQL_PASSWORD"],
        database=os.environ.get("PILLOW_MYSQL_DATABASE", "pillow"),
        charset="utf8mb4",
        cursorclass=DictCursor,
    )


def as_china_time(value: datetime) -> datetime:
    # MySQL DATETIME values are stored by this project as UTC without tzinfo.
    return value.replace(tzinfo=timezone.utc).astimezone(CHINA_TIME)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export MySQL telemetry as a comparison CSV")
    parser.add_argument("device_id", help="Registered server device ID, for example SP-3C38CDB1E590")
    parser.add_argument("day", type=date.fromisoformat, help="China-local date in YYYY-MM-DD format")
    parser.add_argument(
        "--output-dir",
        default="/home/ubuntu/pillow-lqdw/data/exports",
        help="Directory for generated CSV files",
    )
    args = parser.parse_args()

    local_start = datetime.combine(args.day, time.min, tzinfo=CHINA_TIME)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    with db_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.device_id, t.sampled_at, t.heart_rate, t.respiratory_rate,
                   t.temperature, t.sleep_stage, t.confidence, t.time_source,
                   t.analog_voltage, t.fpga_input, t.received_at
            FROM telemetry t
            JOIN devices d ON d.id = t.device_id
            WHERE d.device_id = %s AND t.sampled_at >= %s AND t.sampled_at < %s
            ORDER BY t.sampled_at, t.id
            """,
            (args.device_id, utc_start, utc_end),
        )
        rows = cursor.fetchall()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"sleep_{args.device_id}_{args.day.isoformat()}_server.csv"
    # utf-8-sig lets Excel on Windows display Chinese column values correctly.
    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file)
        writer.writerow(CSV_HEADER)
        for row in rows:
            sampled_at = as_china_time(row["sampled_at"])
            received_at = as_china_time(row["received_at"])
            writer.writerow([
                int(sampled_at.timestamp()),
                sampled_at.strftime("%Y-%m-%d %H:%M:%S"),
                f"{float(row['heart_rate']):.1f}",
                f"{float(row['respiratory_rate']):.1f}",
                f"{float(row['temperature']):.1f}",
                row["sleep_stage"],
                f"{float(row['confidence']):.1f}",
                row["time_source"] or "",
                "" if row["analog_voltage"] is None else f"{float(row['analog_voltage']):.3f}",
                "" if row["fpga_input"] is None else row["fpga_input"],
                row["device_id"],
                received_at.isoformat(),
            ])
    print(f"Exported {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()

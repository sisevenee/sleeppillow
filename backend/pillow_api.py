#!/usr/bin/env python3
"""Minimal HTTP API for Sleep Pillow telemetry.

The service deliberately uses only Python's standard library.  It is suitable
for a prototype deployment and keeps the persistence layer (SQLite) separate
from the HTTP handlers so it can later be replaced by MySQL.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("PILLOW_API_HOST", "0.0.0.0")
PORT = int(os.environ.get("PILLOW_API_PORT", "8080"))
DATABASE_PATH = Path(os.environ.get("PILLOW_DATABASE", "/var/lib/pillow-api/pillow.db"))
DEVICE_WRITE_TOKEN = os.environ.get("PILLOW_DEVICE_WRITE_TOKEN", "")
APP_READ_TOKEN = os.environ.get("PILLOW_APP_READ_TOKEN", "")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY_BYTES = 16 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                heart_rate REAL NOT NULL,
                respiratory_rate REAL NOT NULL,
                temperature REAL NOT NULL,
                sleep_stage TEXT NOT NULL,
                confidence REAL NOT NULL,
                time_source TEXT,
                analog_voltage REAL,
                fpga_input INTEGER,
                received_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry(device_id, timestamp DESC)"
        )


def telemetry_to_response(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "deviceId": row["device_id"],
        "timestamp": row["timestamp"],
        "heartRate": row["heart_rate"],
        "respiratoryRate": row["respiratory_rate"],
        "temperature": row["temperature"],
        "sleepStage": row["sleep_stage"],
        "confidence": row["confidence"],
        "timeSource": row["time_source"],
        "analogVoltage": row["analog_voltage"],
        "fpgaInput": row["fpga_input"],
        "receivedAt": row["received_at"],
    }


class PillowApiHandler(BaseHTTPRequestHandler):
    server_version = "SleepPillowAPI/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # systemd captures this in journalctl; avoid logging request bodies or tokens.
        super().log_message(format, *args)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_token(self, expected_token: str) -> bool:
        supplied = self.headers.get("Authorization", "")
        if not expected_token or supplied != f"Bearer {expected_token}":
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Unauthorized"})
            return False
        return True

    def read_json_body(self) -> dict[str, Any] | None:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            self.send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return None
        try:
            size = int(content_length)
        except ValueError:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"})
            return None
        if size < 1 or size > MAX_BODY_BYTES:
            self.send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "Request body must be 1-16384 bytes"})
            return None
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON"})
            return None
        if not isinstance(payload, dict):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
            return None
        return payload

    @staticmethod
    def route_device_id(path: str, suffix: str) -> str | None:
        prefix = "/api/v1/devices/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        device_id = path[len(prefix) : -len(suffix)]
        return device_id if DEVICE_ID_RE.fullmatch(device_id) else None

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(HTTPStatus.OK, {"status": "ok", "time": utc_now()})
            return
        if not self.require_token(APP_READ_TOKEN):
            return

        device_id = self.route_device_id(parsed.path, "/latest")
        if device_id:
            with database_connection() as connection:
                row = connection.execute(
                    "SELECT * FROM telemetry WHERE device_id = ? ORDER BY id DESC LIMIT 1", (device_id,)
                ).fetchone()
            if row is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "No telemetry found for this device"})
            else:
                self.send_json(HTTPStatus.OK, telemetry_to_response(row))
            return

        device_id = self.route_device_id(parsed.path, "/telemetry")
        if device_id:
            query = parse_qs(parsed.query)
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            limit = min(max(limit, 1), 1000)
            with database_connection() as connection:
                rows = connection.execute(
                    "SELECT * FROM telemetry WHERE device_id = ? ORDER BY id DESC LIMIT ?", (device_id, limit)
                ).fetchall()
            self.send_json(HTTPStatus.OK, {"deviceId": device_id, "items": [telemetry_to_response(row) for row in reversed(rows)]})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        device_id = self.route_device_id(parsed.path, "/telemetry")
        if device_id is None:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Route not found"})
            return
        if not self.require_token(DEVICE_WRITE_TOKEN):
            return
        payload = self.read_json_body()
        if payload is None:
            return

        required = ("timestamp", "heartRate", "respiratoryRate", "temperature", "sleepStage", "confidence")
        missing = [field for field in required if field not in payload]
        if missing:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Missing required fields", "fields": missing})
            return
        try:
            timestamp = str(payload["timestamp"])
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            heart_rate = float(payload["heartRate"])
            respiratory_rate = float(payload["respiratoryRate"])
            temperature = float(payload["temperature"])
            sleep_stage = str(payload["sleepStage"]).strip()
            confidence = float(payload["confidence"])
            time_source = str(payload.get("timeSource", "")) or None
            analog_voltage = float(payload["analogVoltage"]) if payload.get("analogVoltage") is not None else None
            fpga_input = int(payload["fpgaInput"]) if payload.get("fpgaInput") is not None else None
        except (TypeError, ValueError):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Telemetry field has an invalid type or timestamp"})
            return
        if not sleep_stage or len(sleep_stage) > 64 or fpga_input not in (None, 0, 1):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid sleepStage or fpgaInput"})
            return

        with database_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO telemetry (
                    device_id, timestamp, heart_rate, respiratory_rate, temperature,
                    sleep_stage, confidence, time_source, analog_voltage, fpga_input, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, timestamp, heart_rate, respiratory_rate, temperature, sleep_stage, confidence,
                 time_source, analog_voltage, fpga_input, utc_now()),
            )
        self.send_json(HTTPStatus.CREATED, {"id": cursor.lastrowid, "deviceId": device_id, "status": "stored"})


def main() -> None:
    if not DEVICE_WRITE_TOKEN or not APP_READ_TOKEN:
        raise RuntimeError("PILLOW_DEVICE_WRITE_TOKEN and PILLOW_APP_READ_TOKEN must be configured")
    initialize_database()
    httpd = ThreadingHTTPServer((HOST, PORT), PillowApiHandler)
    print(f"Sleep Pillow API listening on {HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
set -euo pipefail

source /etc/pillow-api/pillow-api.env
base_url="http://127.0.0.1:8080"
device_id="BackendTest_001"

printf 'Health: '
curl --fail --silent --show-error "$base_url/health"
printf '\nUpload: '
curl --fail --silent --show-error \
  -X POST "$base_url/api/v1/devices/$device_id/telemetry" \
  -H "Authorization: Bearer $PILLOW_DEVICE_WRITE_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"timestamp":"2026-09-08T15:10:00+08:00","heartRate":65,"respiratoryRate":13.2,"temperature":25.8,"sleepStage":"Light Sleep","confidence":81.5,"timeSource":"NTP","analogVoltage":0.40,"fpgaInput":1}'
printf '\nLatest: '
curl --fail --silent --show-error \
  "$base_url/api/v1/devices/$device_id/latest" \
  -H "Authorization: Bearer $PILLOW_APP_READ_TOKEN"
printf '\n'

python3 - <<'PY'
import sqlite3
connection = sqlite3.connect('/var/lib/pillow-api/pillow.db')
connection.execute("DELETE FROM telemetry WHERE device_id = ?", ('BackendTest_001',))
connection.commit()
PY

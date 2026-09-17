# Sleep Pillow API

Prototype backend for ESP32 sleep telemetry. It uses Python standard library
HTTP handling and SQLite, so deployment has no third-party runtime dependency.

## HTTP API

`GET /health` does not require credentials.

ESP32 writes telemetry with:

```text
POST /api/v1/devices/SleepPillow_001/telemetry
Authorization: Bearer <PILLOW_DEVICE_WRITE_TOKEN>
Content-Type: application/json
```

```json
{
  "timestamp": "2026-09-08T11:31:16+08:00",
  "heartRate": 56,
  "respiratoryRate": 9.0,
  "temperature": 26.0,
  "sleepStage": "Deep Sleep",
  "confidence": 82.4,
  "timeSource": "NTP",
  "analogVoltage": 0.40,
  "fpgaInput": 0
}
```

The App reads the latest value with:

```text
GET /api/v1/devices/SleepPillow_001/latest
Authorization: Bearer <PILLOW_APP_READ_TOKEN>
```

Historical data is available through:

```text
GET /api/v1/devices/SleepPillow_001/telemetry?limit=100
Authorization: Bearer <PILLOW_APP_READ_TOKEN>
```

This first deployment listens on HTTP port `8080` for functional testing.
Before non-test use, put the service behind HTTPS with a domain and avoid
embedding the app-read token in a distributable client; use user sign-in and
short-lived server tokens instead.

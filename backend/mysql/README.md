# Multi-device MySQL API

This version supports multiple ESP32 devices. Every device has its own
`deviceId` and write token. The database stores only a SHA-256 hash of each
device token. ESP32 upload credentials and App login accounts are independent.

## Routes

| Purpose | Method and path | Authentication |
| --- | --- | --- |
| Health | `GET /health` | None |
| Login | `POST /api/v1/auth/login` | None |
| My current device | `GET` / `POST /api/v1/me/current-device` | App login session |
| Sleep report | `GET /api/v1/me/sleep-reports` | App login session |
| Device recording status | `GET /api/v1/me/device-recording` | App login session |
| Prepare / wake marker | `POST /api/v1/me/sleep-markers/prepare` or `/wake` | App login session |
| Legacy start / end marker | `POST /api/v1/me/sleep-sessions/start` or `/end` | App login session |
| Questionnaire export | `GET /api/v1/admin/exports/questionnaires?start=YYYY-MM-DD&end=YYYY-MM-DD&type=pre_sleep` | Administrator session |
| Sleep export | `GET /api/v1/admin/exports/sleep?start=YYYY-MM-DD&end=YYYY-MM-DD` | Administrator session |
| List devices | `GET /api/v1/devices` | Administrator session |
| Upload telemetry | `POST /api/v1/devices/{deviceId}/telemetry` | That device's token |
| Latest telemetry | `GET /api/v1/devices/{deviceId}/latest` | App login session |
| History | `GET /api/v1/devices/{deviceId}/telemetry?limit=100` | App login session |

The administrator dashboard is at `/admin`. It requires an account whose role
is `admin` and can list all registered devices, inspect history, and export the
loaded rows as CSV. Its batch export area has two date-range actions (up to 31
China-calendar days per export):

- Questionnaire export creates one Excel-compatible CSV. Each response occupies
  one row, fixed metadata columns come first, and answer columns are created
  from the stored question text. Image answers retain their attachment ID so an
  administrator can find the protected image in the dashboard.
- Sleep export creates one ZIP with `sleep_overview.csv` plus raw CSV files
  named `YYYY-MM-DD_userXX_sleep.csv`. A 22:00--08:00 recording belongs to the
  China-local date on which it ends. The overview includes both monitoring
  duration and sleep duration excluding explicit Awake-stage segments.

A normal account stores only its most recently selected device. The App should
update that selection immediately after a real BLE connection returns the
device's hardware `deviceId`; no device is preassigned or permanently locked to
an account. Batch sleep exports use this current selection, so accounts that
have not yet connected a physical device do not receive an incorrectly assigned
sleep file.

## Sleep sessions and reports

The API groups telemetry into a `sleep_sessions` table. The first valid ESP32
telemetry sample starts a session automatically (representing device power-on),
and a session is closed at its last sample after 15 minutes without new data
(representing power-off or stopped upload). App “prepare to sleep” and “wake”
actions are stored separately in `sleep_manual_markers`; they never split or
stop a continuous device recording. The report uses only records actually
received from ESP32: duration, sample count, and average heart rate,
respiratory rate, and temperature. It does not invent sleep efficiency or
stage summaries.

After updating an existing server, apply the idempotent schema update before
restarting the API service:

```bash
cd /home/ubuntu/pillow-lqdw
sudo mysql < backend/mysql/schema.sql
sudo bash -c 'set -a; source /home/ubuntu/pillow-lqdw/secrets/pillow-api-mysql.env; set +a; python3 backend/mysql/backfill_sleep_sessions.py'
sudo systemctl restart pillow-api
curl --fail http://127.0.0.1:8080/health
```

Create accounts on the server after applying `schema.sql`:

```bash
cd /home/ubuntu/pillow-lqdw/backend/mysql
sudo bash -c 'set -a; source /home/ubuntu/pillow-lqdw/secrets/pillow-api-mysql.env; set +a; python3 manage_users.py create admin --role admin'
sudo bash -c 'set -a; source /home/ubuntu/pillow-lqdw/secrets/pillow-api-mysql.env; set +a; python3 manage_users.py create user01'
sudo bash -c 'set -a; source /home/ubuntu/pillow-lqdw/secrets/pillow-api-mysql.env; set +a; python3 manage_users.py list'
```

`manage_users.py` prompts for a password, stores only a salted PBKDF2 hash, and
never prints the password. Before handing accounts to users outside the lab,
put the HTTP service behind HTTPS; passwords must not be sent over plain HTTP.

The POST JSON remains aligned with the SD CSV fields. `timestamp` must include
a timezone, for example `2026-09-08T15:30:00+08:00`.

```json
{
  "timestamp": "2026-09-08T15:30:00+08:00",
  "heartRate": 68,
  "respiratoryRate": 13.2,
  "temperature": 25.8,
  "sleepStage": "Light Sleep",
  "confidence": 81.5,
  "timeSource": "NTP",
  "analogVoltage": 0.40,
  "fpgaInput": 1
}
```

Uploading the same device/timestamp again updates that record instead of
creating a duplicate. This makes later SD-based retry and backfill safe.

## Database backups

`deploy-backup.sh` installs a nightly logical backup and runs one immediately:

```bash
sudo backend/mysql/deploy-backup.sh            # install + back up now
sudo backend/mysql/deploy-backup.sh --verify   # also restore-test the result
```

It installs `pillow-db-backup.sh` to `/usr/local/bin/`, plus
`pillow-db-backup.service` and `pillow-db-backup.timer` to
`/etc/systemd/system/`. The timer runs at 03:30 China time with
`Persistent=true`, so a missed run is replayed after downtime.

Backups are written to `/var/backups/pillow` as `pillow-YYYYMMDD-HHMMSS.sql.gz`,
owned by root with mode 640. Each dump is checked with `gzip -t` and for the
`Dump completed` trailer before it replaces the previous file, so a truncated
dump is never left behind. Retention keeps 30 daily files plus one snapshot per
month for 12 months. `--verify-restore` imports the newest dump into a scratch
database `pillow_restorecheck`, compares `COUNT(*)` for every base table against
the live database, and then drops the scratch database.

```bash
systemctl list-timers pillow-db-backup.timer     # next scheduled run
journalctl -u pillow-db-backup.service -n 20     # last run
tail -n 40 /var/log/pillow-db-backup.log         # full history
gzip -dc /var/backups/pillow/<file>.sql.gz | sudo mysql pillow   # restore
```

Note that `/var/backups/pillow` lives on the same disk as the database. It
protects against operator error and bad writes, not against losing the host
itself; copy the dumps off the machine periodically.

## CSV comparison export

The MySQL database is the server's canonical store. To create a readable CSV
for one device and one China-local day, run `export_telemetry.py`. It writes a
UTF-8-with-BOM file beneath `data/exports/`, using the SD CSV's ten data fields
plus `DeviceId` and `ServerReceivedAt` for comparison.

## Session attribution (`sleep_sessions.user_id`)

`sleep_sessions` originally recorded only `device_id`. Because
`user_current_devices` keeps a single "current device" row per account and a
device may be reassigned at any time, a session recorded before a handover
could not be traced back to the participant who actually slept on it.

`user_id` now records who was using the device when the session was opened:

- The API stamps it once, at session creation, from `user_current_devices`.
  It is never rewritten, so a later handover cannot change who an earlier
  night belonged to.
- It is nullable on purpose. An ESP32 can upload before anyone logs in or
  selects a device, and a bare NULL must not make the data disappear.
- Read paths resolve with `COALESCE(s.user_id, ucd.user_id)`, so rows written
  before the column existed still appear under the device's current owner.
- The foreign key is `ON DELETE SET NULL`: removing a participant must not
  cascade-delete recorded nights.

### Applying the change to an existing database

`migrate_sessions_user_id.sql` is structure-only, touches no business data, and
is safe to re-run (each step is guarded by an `information_schema` check):

```bash
sudo bash -c 'set -a; . secrets/pillow-api-mysql.env; set +a; \
  mysql -h "$PILLOW_MYSQL_HOST" -u "$PILLOW_MYSQL_USER" -p"$PILLOW_MYSQL_PASSWORD" \
  "$PILLOW_MYSQL_DATABASE"' < backend/mysql/migrate_sessions_user_id.sql
```

Then attribute the historical rows. `backfill_session_user_id.py` only fills
rows where `user_id IS NULL`, never overwrites an existing value, and is
therefore safe to re-run. It prefers `sleep_manual_markers`, the only source
that survives a device handover, and falls back to the device's current owner:

```bash
sudo bash -c 'set -a; . secrets/pillow-api-mysql.env; set +a; \
  python3 backend/mysql/backfill_session_user_id.py --dry-run'
sudo bash -c 'set -a; . secrets/pillow-api-mysql.env; set +a; \
  python3 backend/mysql/backfill_session_user_id.py --apply'
```

The dry run reports how many sessions are resolvable by marker versus by the
current owner, and how many would stay NULL, so the split is reviewable before
anything is written. Take a backup first if the run is not a dry run.

## Data completeness matrix

`GET /api/v1/admin/completeness?start=YYYY-MM-DD&end=YYYY-MM-DD` returns the
participant x sleep-night grid behind the admin dashboard's 数据完整度 tab.
Rows are active ordinary participants; columns are China-local sleep-night
dates, so a 22:00--08:00 recording appears under the date its monitoring ended.
The range is capped at 62 days because one column is rendered per day.

Each cell carries three independent facts — a sleep session, the bedtime form,
and the next-morning form — and is labelled `complete`, `partial`, or
`missing`. A fourth state, surfaced as `mismatch`, flags a night where the
device did record data but the session is credited to a different participant
while this participant submitted a questionnaire. That case is not a missing
data problem and should be resolved by hand rather than read as a hardware
failure.

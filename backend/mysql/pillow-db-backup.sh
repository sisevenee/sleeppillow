#!/usr/bin/env bash
# Daily logical backup of the "pillow" MySQL database.
#
# Installed to /usr/local/bin/pillow-db-backup.sh by deploy-backup.sh and
# triggered by pillow-db-backup.timer. Must run as root: mysqldump authenticates
# as root@localhost through the unix socket, and the backup directory is root-only.
#
# Usage:
#   pillow-db-backup.sh                  run a normal backup
#   pillow-db-backup.sh --verify-restore import the newest backup into a scratch
#                                        database, compare row counts, then drop it
set -euo pipefail

backup_dir="/var/backups/pillow"
log_file="/var/log/pillow-db-backup.log"
database="pillow"
keep_daily=30
keep_monthly=12

stamp="$(date +%Y%m%d-%H%M%S)"
target="$backup_dir/pillow-$stamp.sql.gz"
partial="$target.partial"

log() {
  printf '%s  %s\n' "$(date '+%F %T%z')" "$*" | tee -a "$log_file"
}

fail() {
  log "ERROR: $*"
  exit 1
}

# Rotate the log so it cannot grow without bound.
if [[ -f "$log_file" ]] && (( $(stat -c %s "$log_file") > 2097152 )); then
  mv -f "$log_file" "$log_file.1"
fi

install -d -o root -g root -m 750 "$backup_dir"

# ---------------------------------------------------------------- verify mode
if [[ "${1:-}" == "--verify-restore" ]]; then
  newest="$(find "$backup_dir" -maxdepth 1 -name 'pillow-*.sql.gz' -printf '%f\n' | sort | tail -1)"
  [[ -n "$newest" ]] || fail "no backup file found under $backup_dir"
  scratch="pillow_restorecheck"
  log "verify-restore: source $newest -> scratch database $scratch"

  mysql -e "DROP DATABASE IF EXISTS \`$scratch\`; CREATE DATABASE \`$scratch\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

  if ! gzip -dc "$backup_dir/$newest" | mysql "$scratch"; then
    mysql -e "DROP DATABASE IF EXISTS \`$scratch\`;"
    fail "restore into $scratch failed"
  fi

  # Compare row counts between live and restored copies for every base table.
  # COUNT(*) is used on both sides deliberately: information_schema.table_rows is
  # a sampled estimate for InnoDB and is routinely stale, so it cannot be compared.
  mismatch=0
  checked=0
  while read -r table; do
    live_rows="$(mysql -N -B -e "SELECT COUNT(*) FROM \`$database\`.\`$table\`;")"
    restored_rows="$(mysql -N -B -e "SELECT COUNT(*) FROM \`$scratch\`.\`$table\`;")"
    checked=$(( checked + 1 ))
    if [[ "$live_rows" != "$restored_rows" ]]; then
      log "  DIFF $table live=$live_rows restored=$restored_rows"
      mismatch=1
    fi
  done < <(mysql -N -B -e "
    SELECT t.table_name
      FROM information_schema.tables t
     WHERE t.table_schema='$database' AND t.table_type='BASE TABLE'
     ORDER BY t.table_name;")

  mysql -e "DROP DATABASE IF EXISTS \`$scratch\`;"

  if (( mismatch )); then
    fail "restore differs from the live database (see DIFF lines above)"
  fi
  log "verify-restore OK: $checked tables restorable and row counts match"
  exit 0
fi

# --------------------------------------------------------------- backup mode
log "backup start -> $target"

# --single-transaction gives a consistent InnoDB snapshot without blocking writes,
# so the API stays available while the dump runs.
if ! nice -n 10 ionice -c2 -n7 mysqldump \
      --single-transaction --quick \
      --routines --triggers --events \
      --default-character-set=utf8mb4 --no-tablespaces \
      "$database" | gzip -9 > "$partial"; then
  rm -f "$partial"
  fail "mysqldump or gzip failed"
fi

# A truncated dump is worse than no dump, so prove the file is complete before
# it is allowed to replace the previous one.
gzip -t "$partial" || { rm -f "$partial"; fail "gzip integrity check failed"; }
gzip -dc "$partial" | tail -5 | grep -q 'Dump completed' \
  || { rm -f "$partial"; fail "dump trailer missing, file is truncated"; }

mv -f "$partial" "$target"
chmod 640 "$target"

size="$(du -h "$target" | cut -f1)"
inserts="$(gzip -dc "$target" | grep -c '^INSERT INTO' || true)"
log "backup ok: $target ($size, $inserts INSERT statements)"

# Keep one snapshot per month for a year, taken on the 1st.
if [[ "$(date +%d)" == "01" ]]; then
  cp -p "$target" "$backup_dir/pillow-monthly-$(date +%Y%m).sql.gz"
  log "monthly snapshot kept: pillow-monthly-$(date +%Y%m).sql.gz"
fi

# --------------------------------------------------------------- retention
mapfile -t dailies < <(find "$backup_dir" -maxdepth 1 -name 'pillow-[0-9]*.sql.gz' -printf '%f\n' | sort)
if (( ${#dailies[@]} > keep_daily )); then
  for name in "${dailies[@]:0:$(( ${#dailies[@]} - keep_daily ))}"; do
    rm -f "$backup_dir/$name" && log "pruned daily $name"
  done
fi

mapfile -t monthlies < <(find "$backup_dir" -maxdepth 1 -name 'pillow-monthly-*.sql.gz' -printf '%f\n' | sort)
if (( ${#monthlies[@]} > keep_monthly )); then
  for name in "${monthlies[@]:0:$(( ${#monthlies[@]} - keep_monthly ))}"; do
    rm -f "$backup_dir/$name" && log "pruned monthly $name"
  done
fi

files="$(find "$backup_dir" -maxdepth 1 -name 'pillow-*.sql.gz' | wc -l)"
total="$(du -sh "$backup_dir" | cut -f1)"
log "retention: $files backup file(s), $total in $backup_dir"

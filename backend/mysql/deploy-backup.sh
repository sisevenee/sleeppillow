#!/usr/bin/env bash
# Run with sudo on the server. Installs the nightly MySQL backup job
# (script + systemd service + systemd timer) and runs one backup immediately.
#
# Idempotent: safe to re-run after changing pillow-db-backup.sh.
#
#   sudo backend/mysql/deploy-backup.sh              install, then back up once
#   sudo backend/mysql/deploy-backup.sh --verify     also restore-test the result
set -euo pipefail

project_root="/home/ubuntu/pillow-lqdw"
src_dir="$project_root/backend/mysql"
script_dst="/usr/local/bin/pillow-db-backup.sh"
unit_dst="/etc/systemd/system"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this with sudo." >&2
  exit 1
fi

for f in pillow-db-backup.sh pillow-db-backup.service pillow-db-backup.timer; do
  if [[ ! -f "$src_dir/$f" ]]; then
    echo "Missing $src_dir/$f" >&2
    exit 1
  fi
done

install -o root -g root -m 750 "$src_dir/pillow-db-backup.sh" "$script_dst"
for unit in pillow-db-backup.service pillow-db-backup.timer; do
  install -o root -g root -m 644 "$src_dir/$unit" "$unit_dst/$unit"
done

systemctl daemon-reload
systemctl enable --now pillow-db-backup.timer

echo "--- timer ---"
systemctl list-timers pillow-db-backup.timer --no-pager

echo
echo "--- running one backup now ---"
"$script_dst"

echo
echo "--- backup directory ---"
ls -la /var/backups/pillow/

if [[ "${1:-}" == "--verify" ]]; then
  echo
  echo "--- restore test ---"
  "$script_dst" --verify-restore
fi

echo
echo "Installed. Logs: journalctl -u pillow-db-backup.service  |  /var/log/pillow-db-backup.log"

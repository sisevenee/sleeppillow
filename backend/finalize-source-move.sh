#!/usr/bin/env bash
set -euo pipefail

restore_old_copy() {
  install -o root -g pillowapi -m 640 /home/ubuntu/pillow-lqdw/pillow_api.py /opt/pillow-api/pillow_api.py
  systemctl restart pillow-api
}

rm -f /opt/pillow-api/pillow_api.py
if ! systemctl restart pillow-api; then
  restore_old_copy
  exit 1
fi

if ! curl --retry 5 --retry-connrefused --retry-delay 1 --fail --silent --show-error http://127.0.0.1:8080/health > /dev/null; then
  restore_old_copy
  exit 1
fi

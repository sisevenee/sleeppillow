#!/usr/bin/env bash
set -euo pipefail

useradd --system --home /nonexistent --shell /usr/sbin/nologin pillowapi 2>/dev/null || true
install -d -o pillowapi -g pillowapi -m 750 /opt/pillow-api /var/lib/pillow-api /etc/pillow-api
install -o root -g pillowapi -m 640 /tmp/pillow_api.py /opt/pillow-api/pillow_api.py
install -o root -g root -m 644 /tmp/pillow-api.service /etc/systemd/system/pillow-api.service

if [[ ! -f /etc/pillow-api/pillow-api.env ]]; then
  write_token="$(openssl rand -hex 32)"
  read_token="$(openssl rand -hex 32)"
  umask 077
  printf 'PILLOW_DEVICE_WRITE_TOKEN=%s\nPILLOW_APP_READ_TOKEN=%s\n' "$write_token" "$read_token" > /etc/pillow-api/pillow-api.env
  chown root:pillowapi /etc/pillow-api/pillow-api.env
  chmod 640 /etc/pillow-api/pillow-api.env
fi

systemctl daemon-reload
systemctl enable --now pillow-api
systemctl is-active --quiet pillow-api

#!/usr/bin/env bash
set -euo pipefail

install -d -o ubuntu -g ubuntu -m 775 /home/ubuntu/pillow-lqdw
install -o ubuntu -g ubuntu -m 644 /opt/pillow-api/pillow_api.py /home/ubuntu/pillow-lqdw/pillow_api.py
install -o root -g root -m 644 /tmp/pillow-api.service /etc/systemd/system/pillow-api.service

systemctl daemon-reload
systemctl restart pillow-api
curl --fail --silent --show-error http://127.0.0.1:8080/health > /dev/null

# The service now loads the read-only project-directory mount; remove the old copy.
rm -f /opt/pillow-api/pillow_api.py

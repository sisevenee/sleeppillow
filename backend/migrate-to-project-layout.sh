#!/usr/bin/env bash
set -euo pipefail

project_root="/home/ubuntu/pillow-lqdw"
old_source="$project_root/pillow_api.py"
old_readme="$project_root/README.md"
old_unit="$project_root/pillow-api.service"

install -d -o ubuntu -g ubuntu -m 775 "$project_root/backend" "$project_root/docs" "$project_root/deploy"
install -d -o pillowapi -g pillowapi -m 750 "$project_root/data/sqlite"
install -d -o root -g pillowapi -m 750 "$project_root/secrets"

install -o ubuntu -g ubuntu -m 644 "$old_source" "$project_root/backend/pillow_api.py"
install -o ubuntu -g ubuntu -m 644 "$old_readme" "$project_root/docs/API.md"
install -o ubuntu -g ubuntu -m 644 /tmp/pillow-api.service "$project_root/deploy/pillow-api.service"
install -o root -g pillowapi -m 640 /etc/pillow-api/pillow-api.env "$project_root/secrets/pillow-api.env"

if ! grep -q '^PILLOW_DATABASE=' "$project_root/secrets/pillow-api.env"; then
  printf 'PILLOW_DATABASE=/var/lib/pillow-api/pillow.db\n' >> "$project_root/secrets/pillow-api.env"
fi

systemctl stop pillow-api
cp -a /var/lib/pillow-api/pillow.db "$project_root/data/sqlite/pillow.db"
chown pillowapi:pillowapi "$project_root/data/sqlite/pillow.db"
chmod 640 "$project_root/data/sqlite/pillow.db"

cp /etc/systemd/system/pillow-api.service /tmp/pillow-api.service.before-project-layout
install -o root -g root -m 644 /tmp/pillow-api.service /etc/systemd/system/pillow-api.service
systemctl daemon-reload
systemctl start pillow-api

for _ in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:8080/health > /dev/null; then
    rm -f "$old_source" "$old_readme" "$old_unit"
    rm -f /opt/pillow-api/pillow_api.py
    rm -f /etc/pillow-api/pillow-api.env
    rm -f /var/lib/pillow-api/pillow.db
    exit 0
  fi
  sleep 1
done

cp /tmp/pillow-api.service.before-project-layout /etc/systemd/system/pillow-api.service
systemctl daemon-reload
systemctl start pillow-api
echo "New project layout did not become healthy; restored the earlier service configuration." >&2
exit 1

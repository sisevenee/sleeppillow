#!/usr/bin/env bash
# Run with sudo on the server. This script installs MySQL and switches the API
# from the SQLite prototype only after the MySQL health check succeeds.
set -euo pipefail

project_root="/home/ubuntu/pillow-lqdw"
api_env="$project_root/secrets/pillow-api-mysql.env"
mysql_data="$project_root/data/mysql"
old_unit="/etc/systemd/system/pillow-api.service"
backup_unit="/tmp/pillow-api.service.sqlite-backup"

if [[ ! -f "$project_root/backend/mysql/pillow_api_mysql.py" ]]; then
  echo "Missing MySQL API source under $project_root/backend/mysql" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server python3-pymysql curl

systemctl stop mysql
install -d -o mysql -g mysql -m 750 "$mysql_data"
if [[ -z "$(find "$mysql_data" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  cp -a /var/lib/mysql/. "$mysql_data/"
  chown -R mysql:mysql "$mysql_data"
fi

install -d -o root -g root -m 755 /etc/systemd/system/mysql.service.d
cat > /etc/systemd/system/mysql.service.d/pillow-project-data.conf <<'EOF'
[Service]
# MySQL itself reaches "ready for connections", but the Ubuntu unit's notify
# socket is not writable after the project-data bind mount. Health checks use
# mysqladmin, so systemd should not wait for that notification.
Type=simple
BindPaths=/home/ubuntu/pillow-lqdw/data/mysql:/var/lib/mysql
EOF

systemctl daemon-reload
systemctl start mysql
for _ in 1 2 3 4 5; do
  if mysqladmin ping --silent; then
    break
  fi
  sleep 1
done
mysqladmin ping --silent

app_read_token="$(sed -n 's/^PILLOW_APP_READ_TOKEN=//p' "$project_root/secrets/pillow-api.env" | head -1)"
if [[ -z "$app_read_token" ]]; then
  echo "Could not read existing App token" >&2
  exit 1
fi
mysql_password="$(openssl rand -hex 32)"

mysql <<SQL
CREATE DATABASE IF NOT EXISTS pillow CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'pillow_api'@'127.0.0.1' IDENTIFIED BY '${mysql_password}';
ALTER USER 'pillow_api'@'127.0.0.1' IDENTIFIED BY '${mysql_password}';
GRANT SELECT, INSERT, UPDATE ON pillow.* TO 'pillow_api'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
mysql < "$project_root/backend/mysql/schema.sql"

umask 077
cat > "$api_env" <<EOF
PILLOW_APP_READ_TOKEN=$app_read_token
PILLOW_MYSQL_HOST=127.0.0.1
PILLOW_MYSQL_PORT=3306
PILLOW_MYSQL_USER=pillow_api
PILLOW_MYSQL_PASSWORD=$mysql_password
PILLOW_MYSQL_DATABASE=pillow
EOF
chown root:pillowapi "$api_env"
chmod 640 "$api_env"

cp "$old_unit" "$backup_unit"
install -o root -g root -m 644 "$project_root/backend/mysql/pillow-api-mysql.service" "$old_unit"
systemctl daemon-reload
systemctl restart pillow-api

for _ in 1 2 3 4 5; do
  if curl --fail --silent http://127.0.0.1:8080/health > /dev/null; then
    echo "MySQL API is healthy. SQLite API service has been replaced."
    exit 0
  fi
  sleep 1
done

cp "$backup_unit" "$old_unit"
systemctl daemon-reload
systemctl restart pillow-api
echo "MySQL API health check failed. The earlier SQLite service was restored." >&2
exit 1

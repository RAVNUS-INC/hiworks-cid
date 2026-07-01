#!/usr/bin/env bash
# 최초 설치. 리포를 /opt/hiworks 에 clone 한 뒤 그 안에서 실행:
#   git clone <REPO_URL> /opt/hiworks && bash /opt/hiworks/deploy/install.sh
set -euo pipefail
APP=/opt/hiworks
ENV=/etc/hiworks-sync.env

echo "== 패키지 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git mariadb-server unixodbc odbc-mariadb ca-certificates curl nano

echo "== venv + 파이썬 의존성 =="
python3 -m venv $APP/venv
$APP/venv/bin/pip install --upgrade pip
$APP/venv/bin/pip install -r $APP/requirements.txt
$APP/venv/bin/playwright install --with-deps chromium

echo "== env 파일 =="
if [ ! -f "$ENV" ]; then
  cp $APP/deploy/hiworks-sync.env.example "$ENV"
  chmod 600 "$ENV"
  echo ">> $ENV 를 열어 HIWORKS_ID/HIWORKS_PW/MYSQL_PASSWORD 를 채우세요."
fi

echo "== MySQL 스키마/계정 =="
systemctl enable --now mariadb
mysql < $APP/schema.sql
set -a; . "$ENV"; set +a
mysql <<SQL
CREATE USER IF NOT EXISTS 'hiworks_sync'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD:-changeme_sync}';
GRANT SELECT,INSERT,UPDATE,DELETE ON asterisk.cid_lookup TO 'hiworks_sync'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "== systemd =="
cp $APP/deploy/hiworks-cidlookup.service $APP/deploy/hiworks-sync.service $APP/deploy/hiworks-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hiworks-cidlookup.service
systemctl enable --now hiworks-sync.timer
echo "설치 완료. nano $ENV 로 자격증명 입력 후 deploy/update.sh 또는 아래 테스트를 진행하세요."

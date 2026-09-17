#!/usr/bin/env bash
# 최초 설치. 리포를 /opt/hiworks 에 clone 한 뒤 그 안에서 실행:
#   git clone <REPO_URL> /opt/hiworks && bash /opt/hiworks/deploy/install.sh
set -euo pipefail
APP=/opt/hiworks
ENV=/etc/hiworks-sync.env

echo "== env 파일 =="
if [ ! -f "$ENV" ]; then
  (umask 077; cp "$APP/deploy/hiworks-sync.env.example" "$ENV")
  chmod 600 "$ENV"
  echo ">> $ENV 를 열어 HIWORKS_ID/HIWORKS_PW/MYSQL_PASSWORD 를 실제 값으로 채운 뒤 이 설치 명령을 다시 실행하세요."
  echo "아직 패키지, DB, 서비스를 변경하지 않았습니다."
  exit 1
fi
set -a
. "$ENV"
set +a
for key in HIWORKS_ID HIWORKS_PW MYSQL_USER MYSQL_PASSWORD; do
  value=${!key:-}
  case "$value" in
    ""|api@yourcompany.com|여기에_*|CHANGE_ME|changeme_sync|'********')
      echo ">> $ENV 의 $key 를 실제 값으로 채운 뒤 다시 실행하세요." >&2
      exit 1
      ;;
  esac
done

echo "== 패키지 =="
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git mariadb-server unixodbc odbc-mariadb ca-certificates curl nano

echo "== venv + 파이썬 의존성 =="
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"
"$APP/venv/bin/playwright" install --with-deps chromium

echo "== MySQL 스키마/계정 =="
systemctl enable --now mariadb
"$APP/venv/bin/python" "$APP/deploy/migrate.py" --initialize

echo "== systemd =="
cp "$APP/deploy/hiworks-cidlookup.service" "$APP/deploy/hiworks-sync.service" "$APP/deploy/hiworks-sync.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hiworks-cidlookup.service
systemctl enable --now hiworks-sync.timer
echo "설치 완료. README의 실행 / 테스트 절차로 동기화와 조회를 확인하세요."

#!/usr/bin/env bash
# 코드 갱신: git pull -> 의존성 반영 -> 유닛 갱신 -> 재시작
set -euo pipefail
APP=/opt/hiworks
cd $APP
git pull --ff-only
$APP/venv/bin/pip install -r $APP/requirements.txt
cp $APP/deploy/hiworks-cidlookup.service $APP/deploy/hiworks-sync.service $APP/deploy/hiworks-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart hiworks-cidlookup.service
systemctl start hiworks-sync.service   # 즉시 1회 동기화
echo "업데이트 완료."

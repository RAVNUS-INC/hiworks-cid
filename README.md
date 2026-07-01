# hiworks-cid

하이웍스 공유 주소록을 주기적으로 MySQL에 동기화하고, Asterisk 수신전화 발신자 이름(CID)을
조회하도록 해주는 도구. Proxmox의 작은 LXC(Asterisk와 같은 망, 별도 서버)에서 돌리는 구성.

## 구조

```
src/hiworks_auth.py   전용계정 자동 로그인 → 세션 쿠키 발급/갱신 (Playwright headless)
src/hiworks_sync.py   contact-api 에서 연락처 전체 조회 → MySQL cid_lookup upsert (401시 자동 재로그인)
src/http_lookup.py    조회 HTTP 엔드포인트 (/cid?number=..., OpenCNAM 호환) — Asterisk가 LAN으로 호출
schema.sql            MySQL 테이블/계정
deploy/               install.sh, update.sh, systemd 유닛, env 예시
asterisk/             Asterisk 쪽 설정 스니펫(func_odbc / CURL 예시)
```

동작: `동기화 서버(이 리포)` 가 하이웍스에서 번호→이름을 로컬 MySQL에 적재하고,
`Asterisk 서버` 는 통화마다 이 서버의 HTTP 엔드포인트(기본 8088)를 조회해 `CALLERID(name)` 를 채운다.

## 배포 (git 기반)

```bash
# 동기화용 LXC(root) 안에서
git clone <REPO_URL> /opt/hiworks
bash /opt/hiworks/deploy/install.sh
nano /etc/hiworks-sync.env          # HIWORKS_ID/HIWORKS_PW/MYSQL_PASSWORD 입력
systemctl restart hiworks-cidlookup
```

### 갱신
```bash
bash /opt/hiworks/deploy/update.sh   # git pull → 의존성/유닛 반영 → 재시작 + 즉시 1회 동기화
```

### 테스트
```bash
set -a; . /etc/hiworks-sync.env; set +a
/opt/hiworks/venv/bin/python /opt/hiworks/src/hiworks_auth.py --force   # 로그인 확인
/opt/hiworks/venv/bin/python /opt/hiworks/src/hiworks_sync.py           # 동기화
mysql -e "SELECT COUNT(*) FROM asterisk.cid_lookup;"
curl "http://127.0.0.1:8088/cid?number=01012345678"
```

## Asterisk 연동
`asterisk/asterisk-cid.conf` 참고. 권장(별도 서버): `extensions.conf` 에서
`Set(CALLERID(name)=${CURL(http://<이서버IP>:8088/cid?number=${CALLERID(num)})})`.

## 보안 (중요)
- **자격증명/쿠키는 절대 커밋하지 마세요.** `.gitignore` 가 `hiworks-sync.env`, `*.env`,
  `cookies.json`, `login-debug-*` 를 제외합니다. 실제 비밀은 `/etc/hiworks-sync.env`(권한 600)에만.
- 조회 포트(8088)/MySQL 은 방화벽에서 **Asterisk 서버 IP만** 허용.
- 리포는 **private** 권장. 연락처는 개인정보입니다.

## 참고
`contact-api.office.hiworks.com` 은 하이웍스 웹앱 내부용 비공식 엔드포인트입니다(공식 주소록
Open API 미제공). 쿠키 세션 인증을 쓰므로 전용계정 자동 로그인으로 세션을 갱신합니다.
하이웍스 약관/정책 확인을 권장합니다.

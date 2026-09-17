# hiworks-cid

하이웍스(Hiworks) 공유 주소록을 로컬 MySQL로 주기 동기화하고, **Asterisk 수신 전화의 발신자
이름(CID)** 을 표시해 주는 도구입니다. 통화가 오면 "누가 전화했는지"를 주소록 이름으로 보여줍니다.

> Sync your Hiworks shared address book into MySQL and resolve incoming caller names in Asterisk.
> Korean groupware (Hiworks) → Asterisk CID name lookup.

---

## 왜 필요한가

하이웍스는 공식 주소록 Open API를 제공하지 않습니다. 이 프로젝트는 하이웍스 웹앱이 사용하는
내부 엔드포인트(`contact-api.office.hiworks.com`)를 이용해 연락처를 읽어와, Asterisk가 통화마다
빠르게 조회할 수 있는 로컬 데이터로 만들어 둡니다.

## 동작 원리

```
 [Hiworks contact-api]                  Sync/Lookup 서버                 Asterisk 서버
        │  (주기 동기화, cron/timer)          │                              │
        └──────────────►  hiworks_sync.py ──► MySQL(cid_lookup) ◄─ http_lookup.py
                                                                    ▲   (LAN, 통화마다)
                                                                    └── CURL() / func_odbc
```

- `hiworks_sync.py` 가 전용 계정으로 로그인해 연락처 전체를 가져와 `cid_lookup` 테이블에 upsert
- Asterisk 는 통화마다 이 서버의 HTTP 엔드포인트(기본 `:8088`)를 조회해 `CALLERID(name)` 를 채움
- 통화 경로에 하이웍스를 직접 두지 않으므로 빠르고, 하이웍스가 잠깐 죽어도 발신자 표시는 계속 동작

## 구성 파일

| 경로 | 설명 |
|------|------|
| `src/hiworks_auth.py` | 전용 계정 자동 로그인 → 세션 쿠키 발급/갱신 (Playwright headless) |
| `src/hiworks_sync.py` | 연락처 전체 조회 → MySQL `cid_lookup` upsert (401 시 자동 재로그인) |
| `src/http_lookup.py` | 조회 HTTP 엔드포인트 (`/cid?number=...`, OpenCNAM 호환 경로 포함) |
| `schema.sql` | MySQL 테이블/계정 |
| `deploy/` | `install.sh`, `update.sh`, `migrate.py`, systemd 유닛, `.env` 예시 |
| `asterisk/asterisk-cid.conf` | Asterisk func_odbc 연동 스니펫 (HTTP 예시는 아래 참고) |

## 요구 사항

- Linux 서버 1대 (예: Proxmox LXC, Debian 12/13 기준). Asterisk 와 같은 망이면 됩니다.
- Python 3.10+, MySQL/MariaDB
- Playwright + Chromium (자동 로그인용)

## 설치

서버에서 root로 실행합니다. 최초 실행은 설정 파일만 만들고 종료 코드 1로 중단합니다.

```bash
git clone https://github.com/<your-org>/hiworks-cid.git /opt/hiworks
bash /opt/hiworks/deploy/install.sh
nano /etc/hiworks-sync.env           # 실제 하이웍스/DB 자격증명 입력
bash /opt/hiworks/deploy/install.sh  # 설정을 채운 뒤 다시 실행
```

`install.sh`는 필수 설정의 빈 값·예시 값을 먼저 확인하고, 설정이 준비되면 패키지 → Python venv +
의존성 + Chromium → MySQL 스키마/계정 → systemd 유닛(조회 서비스 상시 + 동기화 타이머 2분)을
설치합니다. DB 계정은 없을 때만 생성하며 기존 계정의 비밀번호를 변경하지 않습니다.

## 설정

`/etc/hiworks-sync.env` (권한 600) 를 채웁니다. 예시는 `deploy/hiworks-sync.env.example` 참고.

```bash
# 하이웍스 전용 계정(주소록 읽기 권한). 아래 예시를 실제 값으로 바꾸세요.
HIWORKS_ID='api@yourcompany.com'
HIWORKS_PW='여기에_전용계정_비밀번호'
MYSQL_HOST=127.0.0.1
MYSQL_USER=hiworks_sync
MYSQL_PASSWORD='여기에_MySQL_비밀번호'
MYSQL_DB=asterisk
```

이 파일은 셸과 systemd가 함께 읽으므로 값에 공백이나 `$` 같은 문자가 있으면 인용 부호로
감싸고, 주석은 별도 줄에 적습니다. 배포 스크립트는 로컬 MariaDB(3306)에 root Unix 소켓으로
관리 접속합니다. 소켓 경로가 다르면 `MYSQL_ADMIN_SOCKET`을 설정하세요
(기본 `/run/mysqld/mysqld.sock`). 원격 DB의 계정 생성·스키마 변경은 해당 DB 관리자가 수행해야 합니다.

> 팁: 하이웍스 로그인은 SPA라 폼 셀렉터가 환경에 따라 다를 수 있습니다. 로그인 실패 시
> `login-debug-*.png/html` 이 생성되니, 이를 참고해 `src/hiworks_auth.py` 상단의
> `id_selectors`/`pw_selectors` 를 조정하세요.

## 실행 / 테스트

```bash
cd /opt/hiworks
set -a; . /etc/hiworks-sync.env; set +a
venv/bin/python src/hiworks_auth.py --force         # 로그인/쿠키 발급 확인
venv/bin/python src/hiworks_sync.py                 # 동기화
mysql -e "SELECT COUNT(*) FROM asterisk.cid_lookup;"
curl "http://127.0.0.1:8088/cid?number=01012345678"
```

로컬 회귀 테스트는 프로젝트 루트에서 실행합니다. 배포 테스트는 패키지 설치·서비스 명령과 DB를
모의 처리하므로 운영 자원을 변경하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
bash -n deploy/install.sh deploy/update.sh
```

## Asterisk 연동

별도 서버 구성에서는 아래 HTTP 조회 예시를 사용합니다.

```
; extensions.conf — LOOKUP_HOST 는 이 서버의 IP
exten => _X.,1,Set(NUM=${FILTER(0-9,${CALLERID(num)})})
 same => n,Set(FOUND=${CURL(http://LOOKUP_HOST:8088/cid?number=${NUM})})
 same => n,ExecIf($["${FOUND}"!=""]?Set(CALLERID(name)=${FOUND}))
```

로컬 MySQL 직결(func_odbc)을 쓰려면 `asterisk/asterisk-cid.conf`를 참고하세요.

## 갱신

```bash
bash /opt/hiworks/deploy/update.sh
```

`git pull` → 의존성 반영 → 앱 DB 인증 확인 → 스키마 마이그레이션 → 유닛 반영 → 조회 서비스
재시작 → 즉시 1회 동기화 순서로 진행합니다. 인증이나 마이그레이션 실패 시 서비스 재시작 전에
중단합니다. `grade`가 없는 기존 테이블은 먼저 덤프를 저장하고 컬럼을 추가하며, 이미 있으면
변경하지 않습니다. 백업 위치는 `/var/backups/hiworks-cid/`이고 파일 권한은 600입니다
(`MIGRATION_BACKUP_DIR`로 변경 가능). 백업 실패 시 컬럼도 변경하지 않습니다.

DB 인증이 실패하면 `/etc/hiworks-sync.env`의 DB명·계정·비밀번호를 기존 DB 설정과 대조하세요.
예전 설치에서 예시 비밀번호로 계정이 만들어졌다면 env 편집이나 스크립트 재실행만으로 DB
비밀번호가 바뀌지 않습니다. 기존 비밀번호를 확인할 수 있으면 env를 일치시키고, 확인할 수
없으면 DB 관리자가 의도적으로 비밀번호를 재설정한 다음 env에도 같은 값을 넣습니다.
자격증명을 명령행 인자로 전달하지 말고 아래 명령의 비밀번호 프롬프트로 접속을 확인하세요.

```bash
mariadb --host=127.0.0.1 --user=hiworks_sync --password asterisk
```

배포 전 코드 버전과 설정을 별도로 백업하세요. 코드 롤백 시 추가된 nullable `grade` 컬럼은
그대로 둘 수 있습니다. 데이터 복구가 필요한 경우 동기화를 중지하고 현재 DB도 백업한 뒤,
관리자가 덤프를 검토하여 복원해야 합니다. 덤프 복원은 현재 연락처를 덮어씁니다.

## 자동 로그인 / 쿠키 갱신

`contact-api` 는 브라우저 세션 쿠키로 인증합니다(영구 아님). `hiworks_auth.py` 가 전용 계정으로
헤드리스 로그인해 쿠키를 `cookies.json` 에 캐시하고, 동기화 중 `401/403` 이 나면 자동으로
재로그인해 갱신합니다. 자격증명은 환경변수에서만 읽으며 코드/리포에 저장하지 않습니다.

## 보안

- **비밀은 커밋 금지.** `.gitignore` 가 `*.env`, `cookies.json`, `login-debug-*`, `venv/` 를 제외합니다.
  실제 자격증명은 `/etc/hiworks-sync.env` (권한 600) 에만 두세요.
- 조회 포트(8088)/MySQL 은 방화벽에서 **Asterisk 서버 IP만** 허용하세요.
- 주소록은 개인정보입니다. 서버 접근통제와 백업 취급에 유의하세요.
- 전용 계정은 **주소록 읽기 최소 권한** 으로 두는 것을 권장합니다.

## 한계 · 주의 (반드시 읽어주세요)

- 이 도구는 하이웍스의 **비공식(문서화되지 않은) 내부 엔드포인트** 를 사용합니다.
  하이웍스가 사양을 변경하면 동작이 깨질 수 있습니다.
- 자동 로그인 스크래핑/자동화가 서비스 **이용약관에 저촉될 수 있습니다.** 사용 전 소속 조직 및
  하이웍스 정책을 확인하고, 책임하에 사용하세요.
- 2단계 인증(OTP)이 강제된 계정에서는 헤드리스 자동 로그인이 그대로는 동작하지 않습니다.

## 라이선스

MIT 를 권장합니다. `LICENSE` 파일을 추가해 명시하세요.

## 기여

이슈/PR 환영합니다. 로그인 폼 셀렉터, 전화번호 정규화(국가/사업자번호 형식), OpenCNAM 응답 형식 등
환경별 케이스 보강에 특히 도움이 됩니다.

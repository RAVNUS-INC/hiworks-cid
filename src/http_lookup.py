#!/usr/bin/env python3
"""
(보조) HTTP 조회 엔드포인트 — MySQL cid_lookup 을 그대로 읽어 이름을 반환.
Asterisk func_curl/CURL() 로 쓰거나, OpenCNAM 호환 응답도 지원.

  GET /cid?number=01012345678      -> text/plain 이름 (없으면 빈 문자열)
  GET /opencnam/v3/phone/+8210...  -> OpenCNAM 호환(text/plain 이름)

필요 패키지: pip install flask pymysql
실행: MYSQL_* 환경변수 설정 후  python3 http_lookup.py  (기본 0.0.0.0:8088)
Asterisk 예시:
  Set(CALLERID(name)=${CURL(http://127.0.0.1:8088/cid?number=${CALLERID(num)})})
"""
import os
import threading
import pymysql

from flask import Flask, request, Response

from phone_norm import normalize

app = Flask(__name__)

# DB 커넥션을 요청마다 새로 열지 않고 재사용한다.
#  - autocommit=True 는 필수: 안 그러면 지속 커넥션이 InnoDB REPEATABLE READ
#    스냅샷에 고착돼 동기화가 갱신한 이름을 못 보고 옛 값을 돌려준다.
#  - Flask 개발서버가 threaded 로 떠도 안전하도록 락으로 직렬화(로컬 PK 조회라 빠름).
#  - 커넥션이 죽으면(재시작/idle timeout) 쿼리에서 잡아 새로 열고 1회 재시도.
_conn = None
_conn_lock = threading.Lock()


def _connect():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.getenv("MYSQL_DB", "asterisk"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _reset_conn():
    global _conn
    try:
        if _conn:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _query_one(d):
    global _conn
    if _conn is None:
        _conn = _connect()
    with _conn.cursor() as cur:
        cur.execute("SELECT name, grade, company FROM cid_lookup WHERE phone=%s LIMIT 1", (d,))
        return cur.fetchone()


def format_cid(name, grade, company):
    """CID 표시 문자열: '이름 직급 (회사)'. 빈 값은 생략."""
    s = (name or "").strip()
    if grade:
        s += f" {grade.strip()}"
    if company:
        s += f" ({company.strip()})"
    return s


def lookup(num):
    d = normalize(num, min_len=1)
    if not d:
        return ""
    with _conn_lock:
        try:
            row = _query_one(d)
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            _reset_conn()          # 죽은 커넥션 → 새로 열고 1회 재시도
            row = _query_one(d)
        except Exception:
            _reset_conn()
            raise
    return format_cid(row["name"], row["grade"], row["company"]) if row else ""


@app.route("/cid")
def cid():
    return Response(lookup(request.args.get("number", "")), mimetype="text/plain")


@app.route("/opencnam/v3/phone/<path:number>")
def opencnam(number):
    # OpenCNAM 호환: 이름 문자열을 그대로 반환
    return Response(lookup(number), mimetype="text/plain")


# /health 판정 기준 (Uptime Kuma HTTP 모니터용): 초과 시 503
HEALTH_MAX_FAILURES = int(os.getenv("HEALTH_MAX_FAILURES", "3"))
HEALTH_MAX_AGE = int(os.getenv("HEALTH_MAX_AGE", "600"))  # 마지막 동기화 성공 후 최대 초(2분 주기의 5배)
STATE_FILE = os.getenv("SYNC_STATE_FILE", "sync_state.json")


@app.route("/health")
def health():
    """종합 상태: DB 조회 가능 + 동기화 연속실패/신선도. 비정상이면 503 (개인정보 없음)."""
    import json, time
    body = {"status": "ok"}
    # DB 살아있나 + 행 수
    try:
        with _conn_lock:
            global _conn
            if _conn is None:
                _conn = _connect()
            with _conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM cid_lookup")
                body["rows"] = cur.fetchone()["c"]
    except Exception as e:
        _reset_conn()
        body.update(status="db_error", error=type(e).__name__)
    # 동기화 상태 (sync_state.json)
    try:
        st = json.loads(open(STATE_FILE).read())
        st = st if isinstance(st, dict) else {"fails": int(st)}
    except Exception:
        st = {}
    fails = int(st.get("fails", 0))
    last = st.get("last_success")
    body["sync_consecutive_failures"] = fails
    if last:
        body["sync_last_success_age_sec"] = int(time.time() - last)
    if fails >= HEALTH_MAX_FAILURES:
        body["status"] = "sync_failing"
    elif last and time.time() - last > HEALTH_MAX_AGE:
        body["status"] = "sync_stale"
    code = 200 if body["status"] == "ok" else 503
    return Response(json.dumps(body), status=code, mimetype="application/json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")))

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
import json
import math
import os
import threading
import time
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


def _timeout_setting(name, default="1"):
    value = float(os.getenv(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


CONNECT_TIMEOUT = _timeout_setting("MYSQL_CONNECT_TIMEOUT")
READ_TIMEOUT = _timeout_setting("MYSQL_READ_TIMEOUT")
WRITE_TIMEOUT = _timeout_setting("MYSQL_WRITE_TIMEOUT")
LOCK_TIMEOUT = _timeout_setting("DB_LOCK_TIMEOUT")


class DatabaseUnavailable(RuntimeError):
    """DB 조회 또는 공유 연결 대기 실패. HTTP에서는 빈 본문과 503을 반환한다."""


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
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        write_timeout=WRITE_TIMEOUT,
    )


def _reset_conn():
    """반드시 _conn_lock을 잡은 상태에서 호출한다."""
    global _conn
    try:
        if _conn:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _query_one(sql, args=None):
    """조회와 실패한 연결 정리/재시도를 모두 같은 락 안에서 수행한다."""
    global _conn
    if not _conn_lock.acquire(timeout=LOCK_TIMEOUT):
        raise DatabaseUnavailable("DB connection is busy")
    try:
        for attempt in range(2):
            try:
                if _conn is None:
                    _conn = _connect()
                with _conn.cursor() as cur:
                    cur.execute(sql, args)
                    return cur.fetchone()
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
                _reset_conn()
                if attempt == 1:
                    raise DatabaseUnavailable("DB query failed after reconnect") from e
            except Exception as e:
                _reset_conn()
                raise DatabaseUnavailable("DB query failed") from e
    finally:
        _conn_lock.release()


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
    row = _query_one("SELECT name, grade, company FROM cid_lookup WHERE phone=%s LIMIT 1", (d,))
    return format_cid(row["name"], row["grade"], row["company"]) if row else ""


def _cid_response(number):
    try:
        return Response(lookup(number), mimetype="text/plain")
    except DatabaseUnavailable:
        # Asterisk가 오류 HTML을 발신자 이름으로 쓰지 않도록 본문은 비운다.
        return Response("", status=503, mimetype="text/plain")


@app.route("/cid")
def cid():
    return _cid_response(request.args.get("number", ""))


@app.route("/opencnam/v3/phone/<path:number>")
def opencnam(number):
    # OpenCNAM 호환: 이름 문자열을 그대로 반환
    return _cid_response(number)


# /health 판정 기준 (Uptime Kuma HTTP 모니터용): 초과 시 503
HEALTH_MAX_FAILURES = int(os.getenv("HEALTH_MAX_FAILURES", "3"))
HEALTH_MAX_AGE = int(os.getenv("HEALTH_MAX_AGE", "600"))  # 마지막 동기화 성공 후 최대 초(2분 주기의 5배)
STATE_FILE = os.getenv("SYNC_STATE_FILE", "sync_state.json")


def _sync_health():
    result = {"sync_status": "sync_unknown", "sync_consecutive_failures": None}
    try:
        with open(STATE_FILE) as state:
            st = json.load(state)
    except (OSError, ValueError):
        return result
    # 상태 파일을 읽는 동안 완료된 동기화를 미래 시각으로 오판하지 않는다.
    now = time.time()
    if not isinstance(st, dict):
        return result
    fails = st.get("fails")
    if type(fails) is not int or fails < 0:
        return result
    result["sync_consecutive_failures"] = fails
    last = st.get("last_success")
    # bool은 int의 하위형이지만 유효한 카운터/시각이 아니다.
    if type(last) not in (int, float) or not 0 < last <= now or not math.isfinite(last):
        return result
    result["sync_last_success_age_sec"] = int(now - last)
    if fails >= HEALTH_MAX_FAILURES:
        result["sync_status"] = "sync_failing"
    elif now - last > HEALTH_MAX_AGE:
        result["sync_status"] = "sync_stale"
    else:
        result["sync_status"] = "ok"
    return result


@app.route("/health")
def health():
    """종합 상태: DB 조회 가능 + 동기화 연속실패/신선도. 비정상이면 503 (개인정보 없음)."""
    body = {"status": "ok"}
    # DB 살아있나 + 행 수
    try:
        body["rows"] = _query_one("SELECT COUNT(*) AS c FROM cid_lookup")["c"]
    except Exception as e:
        body.update(status="db_error", error=type(e.__cause__ or e).__name__)
    # 동기화 상태 (sync_state.json)
    body.update(_sync_health())
    if body["status"] == "ok":
        body["status"] = body["sync_status"]
    code = 200 if body["status"] == "ok" else 503
    return Response(json.dumps(body), status=code, mimetype="application/json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")))

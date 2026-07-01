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
import pymysql
from flask import Flask, request, Response

from phone_norm import normalize

app = Flask(__name__)


def lookup(num):
    d = normalize(num, min_len=1)
    if not d:
        return ""
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.getenv("MYSQL_DB", "asterisk"),
        charset="utf8mb4",
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM cid_lookup WHERE phone=%s LIMIT 1", (d,))
            row = cur.fetchone()
            return row[0] if row else ""
    finally:
        conn.close()


@app.route("/cid")
def cid():
    return Response(lookup(request.args.get("number", "")), mimetype="text/plain")


@app.route("/opencnam/v3/phone/<path:number>")
def opencnam(number):
    # OpenCNAM 호환: 이름 문자열을 그대로 반환
    return Response(lookup(number), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8088")))

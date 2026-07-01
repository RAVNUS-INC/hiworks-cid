#!/usr/bin/env python3
"""
Hiworks 주소록 -> MySQL(asterisk.cid_lookup) 동기화

동작:
  1) contact-api.office.hiworks.com/v2/contacts 를 호출해 전체 연락처를 가져온다
  2) 전화번호를 숫자만 남겨 정규화한다 (한 연락처가 여러 번호를 가지면 각각 등록)
  3) MySQL cid_lookup 테이블을 현재 스냅샷으로 교체(upsert + 사라진 번호 삭제)한다

인증:
  이 API는 브라우저 쿠키 세션을 사용한다. hiworks_auth.get_cookie() 가 전용계정으로
  헤드리스 로그인해 쿠키를 발급/캐시하고, 이 스크립트는 그 쿠키로 API를 호출한다.
  세션이 만료돼 401/403 이 나면 자동으로 강제 재로그인 후 1회 재시도한다.
  (수동 쿠키로 테스트하려면 HIWORKS_COOKIE 환경변수에 넣으면 그걸 우선 사용한다.)

환경변수:
  HIWORKS_ID / HIWORKS_PW           전용계정 자격증명 (hiworks_auth 가 사용)
  HIWORKS_COOKIE                    선택. 있으면 자동로그인 대신 이 쿠키를 사용(수동 테스트용)
  MYSQL_HOST/PORT/USER/PASSWORD/DB  MySQL 접속정보 (DB 기본 asterisk)

필요 패키지: pip install requests pymysql playwright  (+ playwright install chromium)
"""

import os
import re
import sys
import requests
import pymysql

from phone_norm import normalize

API = "https://contact-api.office.hiworks.com/v2/contacts"
PAGE_LIMIT = 500


def _get_cookie(force=False):
    """수동 HIWORKS_COOKIE 우선, 없으면 hiworks_auth 로 자동 로그인."""
    manual = os.environ.get("HIWORKS_COOKIE")
    if manual and not force:
        return manual
    import hiworks_auth
    return hiworks_auth.get_cookie(force=force)


def fetch_all():
    cookie = _get_cookie()
    rows, offset, relogged = [], 0, False
    while True:
        r = requests.get(
            API,
            params={"page[limit]": PAGE_LIMIT, "page[offset]": offset},
            headers={"Cookie": cookie, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code in (401, 403):
            if relogged:
                sys.exit("인증 실패: 재로그인 후에도 401/403. 자격증명/계정상태를 확인하세요.")
            cookie = _get_cookie(force=True)  # 세션 만료 → 자동 재로그인
            relogged = True
            continue
        r.raise_for_status()
        j = r.json()
        batch = j.get("data", [])
        rows.extend(batch)
        total = (j.get("meta", {}) or {}).get("page", {}).get("total", len(rows))
        offset += PAGE_LIMIT
        if offset >= total or not batch:
            break
    return rows


def build_entries(rows):
    """(phone, name, company, grade) 목록. 한 연락처에 번호가 여러 개면 분해."""
    seen = {}
    for c in rows:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        company = (c.get("company") or "").strip() or None
        grade = (c.get("grade") or "").strip() or None
        # phone 필드는 단일 문자열이지만 여러 번호가 섞여 올 수 있어 구분자로 분해
        for part in re.split(r"[,/;\n]", c.get("phone") or ""):
            p = normalize(part)
            if p:
                # 먼저 들어온 값 우선(중복 번호는 첫 이름 유지)
                seen.setdefault(p, (name, company, grade))
    return [(p, n, co, g) for p, (n, co, g) in seen.items()]


def sync_mysql(entries):
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.getenv("MYSQL_DB", "asterisk"),
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            # upsert
            cur.executemany(
                "INSERT INTO cid_lookup (phone, name, company, grade) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), company=VALUES(company), grade=VALUES(grade)",
                entries,
            )
            # 이번 스냅샷에 없는 번호 삭제
            phones = [e[0] for e in entries]
            if phones:
                fmt = ",".join(["%s"] * len(phones))
                cur.execute(f"DELETE FROM cid_lookup WHERE phone NOT IN ({fmt})", phones)
            else:
                cur.execute("DELETE FROM cid_lookup")
        conn.commit()
    finally:
        conn.close()


def main():
    rows = fetch_all()
    entries = build_entries(rows)
    sync_mysql(entries)
    print(f"동기화 완료: 연락처 {len(rows)}건 -> 번호 {len(entries)}건 적재")


if __name__ == "__main__":
    main()

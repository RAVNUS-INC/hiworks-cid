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
  HIWORKS_OFFICE_TOKEN              선택. 설정 시 조직도(직원) 동기화 활성 — Open API Bearer 토큰
  HIWORKS_ORG_API_URL               선택. 조직도 API 전체 URL (기본 https://api.office.hiworks.com/hrm/v2/organizations)

필요 패키지: pip install requests pymysql playwright  (+ playwright install chromium)
"""

import os
import re
import sys
import socket
import requests
import pymysql
from pathlib import Path

from phone_norm import normalize

API = "https://contact-api.office.hiworks.com/v2/contacts"
PAGE_LIMIT = 500

# 조직도(직원) 동기화 — Open API Bearer 토큰. 미설정이면 공유주소록만 동기화.
ORG_TOKEN = os.getenv("HIWORKS_OFFICE_TOKEN")
ORG_API_URL = os.getenv("HIWORKS_ORG_API_URL",
                        "https://api.office.hiworks.com/hrm/v2/organizations")

# 실패 알림(n8n 등 웹훅). 미설정이면 조용히 비활성.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")
ALERT_AFTER_FAILURES = int(os.getenv("ALERT_AFTER_FAILURES", "3"))   # 연속 N회부터 알림
ALERT_REPEAT_EVERY = int(os.getenv("ALERT_REPEAT_EVERY", "30"))      # 이후 N회마다 재알림(2분 주기면 ~1시간)
# 연속 실패 카운터 저장 파일 (git 제외)
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))


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
    """공유주소록 -> {phone: (name, company, grade)}. 한 연락처에 번호가 여러 개면 분해."""
    seen = {}
    for c in rows:
        # 공유주소록만 동기화 (개인 소유 항목은 제외).
        # 현재 API는 type='shared'만 내려주지만, 만일을 대비해 코드로도 막는다.
        if c.get("type") != "shared" or c.get("owner"):
            continue
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
    return seen


def fetch_org():
    """조직도 API (Open API, Bearer 토큰). 토큰 미설정이면 None."""
    if not ORG_TOKEN:
        return None
    r = requests.get(
        ORG_API_URL,
        headers={"Authorization": f"Bearer {ORG_TOKEN}", "Accept": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def build_org_entries(root):
    """조직도 트리 -> {phone: (name, 부서명, None)}.

    부서명을 company 자리에 넣으면 조회 측 format_cid 가 '이름 (부서)'를 만든다(grade 없음).
    각 직원의 phone/cell 둘 다 등록. 트리는 entries + nodes[] 재귀.
    """
    seen = {}

    def walk(node):
        dept = (node.get("name") or "").strip() or None
        for e in node.get("entries") or []:
            name = (e.get("name") or "").strip()
            if not name:
                continue
            for field in ("cell", "phone"):   # 휴대폰 우선 등록(둘 다 있으면 각각 등록됨)
                for part in re.split(r"[,/;\n]", e.get(field) or ""):
                    p = normalize(part)
                    if p:
                        seen.setdefault(p, (name, dept, None))
        for child in node.get("nodes") or []:
            walk(child)

    walk(root)
    return seen


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


def _read_fail_count():
    try:
        return int(STATE_FILE.read_text().strip() or "0")
    except Exception:
        return 0


def _write_fail_count(n):
    try:
        STATE_FILE.write_text(str(n))
    except Exception as e:
        print(f"상태파일 기록 실패: {e}", file=sys.stderr)


def _send_alert(payload):
    """웹훅(n8n)으로 알림 POST. URL 미설정이면 no-op. 알림 실패가 sync를 막지 않게 예외 삼킴."""
    if not ALERT_WEBHOOK_URL:
        return
    body = {"service": "hiworks-cid-sync", "host": socket.gethostname(), **payload}
    try:
        requests.post(ALERT_WEBHOOK_URL, json=body, timeout=10)
    except Exception as e:
        print(f"알림 전송 실패: {e}", file=sys.stderr)


def main():
    try:
        rows = fetch_all()
        contacts = build_entries(rows)
        # 조직도(직원). 토큰 미설정이면 None → 공유주소록만.
        # 실패 시 전체 실패로 처리(부분 성공으로 직원이 스냅샷에서 사라지는 것 방지).
        org = fetch_org()
        employees = build_org_entries(org) if org else {}
        # 같은 번호가 양쪽에 있으면 직원(조직도) 우선
        merged = {**contacts, **employees}
        entries = [(p, n, co, g) for p, (n, co, g) in merged.items()]
        sync_mysql(entries)
    except Exception as e:
        n = _read_fail_count() + 1
        _write_fail_count(n)
        msg = f"{type(e).__name__}: {e}"
        print(f"동기화 실패({n}회 연속): {msg}", file=sys.stderr)
        # 연속 N회부터 알림, 이후 REPEAT 간격으로만 재알림(스팸 방지)
        if n >= ALERT_AFTER_FAILURES and (n - ALERT_AFTER_FAILURES) % ALERT_REPEAT_EVERY == 0:
            _send_alert({"status": "failed", "consecutive_failures": n, "error": msg})
        sys.exit(1)

    # 성공: 카운터 리셋, 직전에 알림 나갔었다면 복구 통지
    prev = _read_fail_count()
    _write_fail_count(0)
    if prev >= ALERT_AFTER_FAILURES:
        _send_alert({"status": "recovered", "after_failures": prev})
    emp_note = f" (직원 {len(employees)}번호 포함)" if employees else ""
    print(f"동기화 완료: 연락처 {len(rows)}건 -> 번호 {len(entries)}건 적재{emp_note}")


if __name__ == "__main__":
    main()

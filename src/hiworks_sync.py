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
import json
import time
import socket
import tempfile
import requests
import pymysql
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from phone_norm import normalize
from hiworks_payload import (
    PayloadError,
    parse_contact_detail,
    parse_contacts_page,
    validate_org_tree,
)

API = "https://contact-api.office.hiworks.com/v2/contacts"
PAGE_LIMIT = 500
CONTACT_WEB_ORIGIN = "https://address-book.office.hiworks.com"
DETAIL_CACHE_FILE = Path(os.getenv("CONTACT_DETAIL_CACHE_FILE", "contact_details.json"))
DETAIL_CACHE_MAX_AGE = int(os.getenv("CONTACT_DETAIL_CACHE_MAX_AGE", "86400"))
DETAIL_WORKERS = int(os.getenv("CONTACT_DETAIL_WORKERS", "6"))

# 조직도(직원) 동기화 — Open API Bearer 토큰. 미설정이면 공유주소록만 동기화.
ORG_TOKEN = os.getenv("HIWORKS_OFFICE_TOKEN")
ORG_API_URL = os.getenv("HIWORKS_ORG_API_URL",
                        "https://api.hiworks.com/hrm/v2/organizations")

# 실패 알림(n8n 등 웹훅). 미설정이면 조용히 비활성.
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL")
ALERT_AFTER_FAILURES = int(os.getenv("ALERT_AFTER_FAILURES", "3"))   # 연속 N회부터 알림
ALERT_REPEAT_EVERY = int(os.getenv("ALERT_REPEAT_EVERY", "30"))      # 이후 N회마다 재알림(2분 주기면 ~1시간)
# 연속 실패 카운터/마지막 성공시각 저장 파일 (git 제외). /health 가 읽음.
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
# 동기화 성공 시마다 호출할 Uptime Kuma push URL. 끊기면 Kuma 가 알림.
HEARTBEAT_URL = os.getenv("HEARTBEAT_URL")


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
    expected_total = None
    seen_ids = set()
    while True:
        r = requests.get(
            API,
            params={"page[limit]": PAGE_LIMIT, "page[offset]": offset},
            headers={"Cookie": cookie, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code in (401, 403):
            if relogged:
                # sys.exit(SystemExit)는 main의 except Exception을 우회해 실패 카운터/알림을
                # 건너뛰므로 반드시 일반 예외로 올린다.
                raise RuntimeError("인증 실패: 재로그인 후에도 401/403. 자격증명/계정상태를 확인하세요.")
            cookie = _get_cookie(force=True)  # 세션 만료 → 자동 재로그인
            relogged = True
            continue
        r.raise_for_status()
        page = parse_contacts_page(r.json())
        if page.offset != offset:
            raise PayloadError("연락처 페이지 offset이 요청한 위치와 다릅니다.")
        if expected_total is None:
            expected_total = page.total
        elif page.total != expected_total:
            raise PayloadError("연락처 수집 도중 전체 건수가 변경되었습니다. 다음 동기화에서 다시 시도합니다.")

        expected_count = min(page.limit, expected_total - offset)
        if len(page.rows) != expected_count:
            raise PayloadError("연락처 페이지 건수가 메타데이터와 일치하지 않습니다.")
        for row in page.rows:
            if row["no"] in seen_ids:
                raise PayloadError("연락처 수집에 중복된 ID가 있습니다. 불완전한 스냅샷을 적용하지 않습니다.")
            seen_ids.add(row["no"])
        rows.extend(page.rows)
        offset += len(page.rows)
        if offset == expected_total:
            return rows


class DetailAuthenticationError(RuntimeError):
    """A contact-detail request needs a refreshed browser session."""


def _load_detail_cache():
    """Load the private detail cache. Malformed/old formats are treated as empty."""
    try:
        payload = json.loads(DETAIL_CACHE_FILE.read_text())
        if payload.get("version") != 1 or not isinstance(payload.get("contacts"), dict):
            return {}
        return payload["contacts"]
    except Exception:
        return {}


def _save_detail_cache(contacts):
    """Atomically save contact phone details with private file permissions."""
    temporary = None
    try:
        DETAIL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=DETAIL_CACHE_FILE.parent,
            prefix=f".{DETAIL_CACHE_FILE.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"version": 1, "contacts": contacts}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, DETAIL_CACHE_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cached_detail(cache, row, now):
    entry = cache.get(str(row["no"]))
    if not isinstance(entry, dict) or entry.get("updated_at") != row.get("updated_at"):
        return None
    fetched_at = entry.get("fetched_at")
    if type(fetched_at) not in (int, float) or not 0 <= now - fetched_at <= DETAIL_CACHE_MAX_AGE:
        return None
    phones = entry.get("phones")
    if not isinstance(phones, list):
        return None
    for phone in phones:
        if not isinstance(phone, dict) or not isinstance(phone.get("type"), str) \
                or not isinstance(phone.get("phone"), str) \
                or type(phone.get("is_default")) is not bool:
            return None
    return phones


def _fetch_contact_detail(row, cookie):
    response = requests.get(
        f"{API}/{row['no']}",
        headers={
            "Cookie": cookie,
            "Accept": "application/json",
            # The undocumented detail endpoint returns HTTP 500 without this custom header.
            "type": row["type"],
            "Origin": CONTACT_WEB_ORIGIN,
            "Referer": f"{CONTACT_WEB_ORIGIN}/",
        },
        timeout=20,
    )
    if response.status_code in (401, 403):
        raise DetailAuthenticationError("연락처 상세 인증이 만료되었습니다.")
    response.raise_for_status()
    detail = parse_contact_detail(response.json(), expected_no=row["no"])
    phones = detail["phones"]
    # A partial detail response must never silently remove the primary list number.
    listed = normalize(row.get("phone"))
    detailed = {normalize(phone["phone"]) for phone in phones}
    if listed and listed not in detailed:
        raise PayloadError("연락처 상세에 목록의 대표 전화번호가 없습니다.")
    return phones


def _fetch_detail_batch(rows, cookie):
    results, errors = {}, {}
    workers = min(max(DETAIL_WORKERS, 1), max(len(rows), 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_fetch_contact_detail, row, cookie): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                results[row["no"]] = future.result()
            except Exception as error:
                errors[row["no"]] = error
    return results, errors


def fetch_contact_phones(rows):
    """Return {contact_no: phones[]} from detail API, reusing unchanged private cache entries."""
    if DETAIL_CACHE_MAX_AGE <= 0 or DETAIL_WORKERS <= 0:
        raise ValueError("CONTACT_DETAIL_CACHE_MAX_AGE와 CONTACT_DETAIL_WORKERS는 양수여야 합니다.")
    eligible = [
        row for row in rows
        if row.get("type") == "shared" and not row.get("owner") and (row.get("name") or "").strip()
    ]
    now = time.time()
    old_cache = _load_detail_cache()
    phones_by_contact, cache_entries, pending = {}, {}, []
    for row in eligible:
        cached = _cached_detail(old_cache, row, now)
        if cached is None:
            pending.append(row)
            continue
        phones_by_contact[row["no"]] = cached
        cache_entries[str(row["no"])] = old_cache[str(row["no"])]

    if pending:
        fetched, errors = _fetch_detail_batch(pending, _get_cookie())
        authentication_rows = [row for row in pending if isinstance(errors.get(row["no"]), DetailAuthenticationError)]
        other_errors = [error for error in errors.values() if not isinstance(error, DetailAuthenticationError)]
        if other_errors:
            raise other_errors[0]
        if authentication_rows:
            retried, retry_errors = _fetch_detail_batch(authentication_rows, _get_cookie(force=True))
            fetched.update(retried)
            if retry_errors:
                error = next(iter(retry_errors.values()))
                if isinstance(error, DetailAuthenticationError):
                    raise RuntimeError("재로그인 후에도 연락처 상세 인증에 실패했습니다.") from error
                raise error
        for row in pending:
            phones = fetched[row["no"]]
            phones_by_contact[row["no"]] = phones
            cache_entries[str(row["no"])] = {
                "updated_at": row.get("updated_at"),
                "fetched_at": now,
                "phones": phones,
            }

    _save_detail_cache(cache_entries)
    return phones_by_contact


def build_entries(rows, phones_by_contact=None):
    """공유주소록 -> {phone: (name, company, grade)}. 상세의 모든 번호를 각각 등록."""
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
        if phones_by_contact is None:
            raw_phones = [c.get("phone") or ""]
        else:
            if c["no"] not in phones_by_contact:
                raise PayloadError("공유 연락처의 상세 전화번호가 누락되었습니다.")
            raw_phones = [phone["phone"] for phone in phones_by_contact[c["no"]]]
        for raw_phone in raw_phones:
            # 각 상세 번호 안에 구분자로 여러 번호가 들어간 기존 데이터도 호환한다.
            for part in re.split(r"[,/;\n]", raw_phone):
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
        # 이 API는 GET에도 Content-Type: application/json 을 요구한다(없으면 200 + 에러 봉투)
        headers={
            "Authorization": f"Bearer {ORG_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=20,
    )
    r.raise_for_status()
    # null/[] 등도 비활성화로 해석하지 않는다. 토큰 미설정만 None을 반환한다.
    return validate_org_tree(r.json())


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


def _load_state():
    """상태파일 -> {"fails": n, "last_success": ts|None}. 구버전(정수만) 형식도 호환."""
    try:
        raw = STATE_FILE.read_text().strip()
        d = json.loads(raw)
        if isinstance(d, dict):
            return {"fails": int(d.get("fails", 0)), "last_success": d.get("last_success")}
        return {"fails": int(d), "last_success": None}
    except Exception:
        return {"fails": 0, "last_success": None}


def _save_state(state):
    temporary = None
    try:
        # /health가 읽는 동안 파일을 비우지 않도록 같은 디렉터리에서 원자적으로 교체한다.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=STATE_FILE.parent,
            prefix=f".{STATE_FILE.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, STATE_FILE)
    except Exception as e:
        print(f"상태파일 기록 실패: {e}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as e:
                print(f"임시 상태파일 삭제 실패: {e}", file=sys.stderr)


def _heartbeat():
    """성공 시 Uptime Kuma push URL 호출. 미설정이면 no-op. 실패해도 sync 를 막지 않음."""
    if not HEARTBEAT_URL:
        return
    try:
        requests.get(HEARTBEAT_URL, timeout=10)
    except Exception as e:
        print(f"하트비트 전송 실패: {e}", file=sys.stderr)


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
        phones_by_contact = fetch_contact_phones(rows)
        contacts = build_entries(rows, phones_by_contact)
        # 조직도(직원). 토큰 미설정이면 None → 공유주소록만.
        # 실패 시 전체 실패로 처리(부분 성공으로 직원이 스냅샷에서 사라지는 것 방지).
        org = fetch_org()
        employees = build_org_entries(org) if org is not None else {}
        # 같은 번호가 양쪽에 있으면 직원(조직도) 우선
        merged = {**contacts, **employees}
        entries = [(p, n, co, g) for p, (n, co, g) in merged.items()]
        sync_mysql(entries)
    except Exception as e:
        state = _load_state()
        n = state["fails"] + 1
        _save_state({**state, "fails": n})
        msg = f"{type(e).__name__}: {e}"
        print(f"동기화 실패({n}회 연속): {msg}", file=sys.stderr)
        # 연속 N회부터 알림, 이후 REPEAT 간격으로만 재알림(스팸 방지)
        if n >= ALERT_AFTER_FAILURES and (n - ALERT_AFTER_FAILURES) % ALERT_REPEAT_EVERY == 0:
            _send_alert({"status": "failed", "consecutive_failures": n, "error": msg})
        sys.exit(1)

    # 성공: 카운터 리셋 + 성공시각 기록 + 하트비트, 직전에 알림 나갔었다면 복구 통지
    prev = _load_state()["fails"]
    _save_state({"fails": 0, "last_success": time.time()})
    _heartbeat()
    if prev >= ALERT_AFTER_FAILURES:
        _send_alert({"status": "recovered", "after_failures": prev})
    emp_note = f" (직원 {len(employees)}번호 포함)" if employees else ""
    detail_numbers = sum(len(phones) for phones in phones_by_contact.values())
    print(f"동기화 완료: 연락처 {len(rows)}건/상세번호 {detail_numbers}건 -> "
          f"고유번호 {len(entries)}건 적재{emp_note}")


if __name__ == "__main__":
    main()

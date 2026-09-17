#!/usr/bin/env python3
"""
하이웍스 전용계정 자동 로그인 → 세션 쿠키 발급/갱신 (Playwright 헤드리스)

- 쿠키를 cookies.json 에 캐시한다.
- get_cookie() 는 캐시된 쿠키 헤더 문자열을 돌려주고,
  force=True 이거나 캐시가 없으면 헤드리스 브라우저로 로그인해 새로 받는다.
- 로그인 폼은 SPA라 셀렉터가 바뀔 수 있어 여러 후보를 시도하고,
  실패하면 스크린샷/HTML을 남겨 디버깅할 수 있게 한다.

자격증명은 환경변수에서만 읽는다 (코드/파일에 저장하지 않음):
  HIWORKS_ID   예: api@yourcompany.com
  HIWORKS_PW   비밀번호
  COOKIE_FILE  선택. 기본 ./cookies.json

설치:
  pip install playwright
  playwright install chromium
"""

import os
import sys
import json
import tempfile
import time
from pathlib import Path

from hiworks_payload import PayloadError, parse_contacts_page

LOGIN_URL = "https://office.hiworks.com/login"
CONTACTS_API = "https://contact-api.office.hiworks.com/v2/contacts"
COOKIE_FILE = Path(os.getenv("COOKIE_FILE", "cookies.json"))
# 쿠키 유효로 간주할 최대 나이(초). 넘으면 재로그인. 12시간 기본.
MAX_AGE = int(os.getenv("COOKIE_MAX_AGE", str(12 * 3600)))
# 무인 재로그인이 SPA 로딩 레이스로 간헐 실패할 수 있어 재시도 횟수를 둔다.
LOGIN_ATTEMPTS = int(os.getenv("LOGIN_ATTEMPTS", "2"))


class LoginError(RuntimeError):
    """로그인 실패(폼 못 찾음/쿠키 없음 등). 재시도 대상."""


def _cookies_to_header(cookies):
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def _load_cache():
    if not COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(COOKIE_FILE.read_text())
        if time.time() - data.get("ts", 0) > MAX_AGE:
            return None
        return data.get("cookies") or None
    except Exception:
        return None


def _save_cache(cookies):
    fd, name = tempfile.mkstemp(prefix=f".{COOKIE_FILE.name}.", dir=COOKIE_FILE.parent)
    try:
        with os.fdopen(fd, "w") as cache:
            os.fchmod(cache.fileno(), 0o600)
            json.dump({"ts": time.time(), "cookies": cookies}, cache)
            cache.flush()
            os.fsync(cache.fileno())
        os.replace(name, COOKIE_FILE)
    finally:
        Path(name).unlink(missing_ok=True)


def _submit(page, sel):
    """제출 버튼이 활성화되면 클릭, 안 되면 Enter. (Mantine 버튼은 입력 전 disabled)"""
    try:
        page.wait_for_selector(f"{sel}:not([disabled])", timeout=6000)
        page.click(f"{sel}:not([disabled])")
        return
    except Exception:
        pass
    page.keyboard.press("Enter")


def _login_page(page, uid, pw):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # 하이웍스 로그인은 2단계(아이디 → 비밀번호) Mantine SPA.
    id_sel = "input[placeholder*='onhiworks'], input[type='email'], input[type='text']"
    pw_sel = "input[type='password']"
    submit = "button[type='submit']"
    try:
        page.wait_for_selector(id_sel, timeout=15000)
    except Exception:
        try:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(id_sel, timeout=15000)
        except Exception as e:
            _dump(page, "no-fields")
            raise LoginError("로그인 폼(아이디 입력칸)을 못 찾음. login-debug-no-fields.* 확인.") from e
    page.fill(id_sel, uid)
    if not page.query_selector(pw_sel):
        _submit(page, submit)
        try:
            page.wait_for_selector(pw_sel, timeout=15000)
        except Exception as e:
            _dump(page, "no-password")
            raise LoginError("아이디 다음 단계에서 비밀번호칸을 못 찾음. "
                             "login-debug-no-password.* 확인.") from e
    page.fill(pw_sel, pw)
    _submit(page, submit)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(2)


def _authenticated_cookies(ctx):
    # 같은 브라우저 쿠키로 주소록 조회가 실제로 성공해야 로그인 성공이다.
    response = ctx.request.get(
        CONTACTS_API,
        params={"page[limit]": 1, "page[offset]": 0},
        headers={"Accept": "application/json"},
        timeout=15000,
    )
    try:
        if response.status != 200:
            raise LoginError(f"로그인 후 주소록 인증 확인 실패(HTTP {response.status}).")
        try:
            page = parse_contacts_page(response.json())
            if page.offset != 0 or len(page.rows) != min(page.limit, page.total):
                raise PayloadError("인증 확인 응답의 페이지 정보가 일치하지 않습니다.")
        except ValueError as e:
            raise LoginError("로그인 후 주소록 인증 확인 응답이 올바르지 않습니다.") from e
    finally:
        response.dispose()
    # API URL에 적용되는 쿠키만 저장하여 다른 호스트의 동명 쿠키를 섞지 않는다.
    cookies = ctx.cookies(CONTACTS_API)
    if not cookies:
        raise LoginError("로그인 후 주소록 API에 사용할 쿠키가 없습니다.")
    return [{"name": c["name"], "value": c["value"], "domain": c["domain"]} for c in cookies]


def login_and_get_cookies():
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    try:
        uid = os.environ["HIWORKS_ID"]
        pw = os.environ["HIWORKS_PW"]
    except KeyError as e:
        raise LoginError(f"필수 로그인 환경변수가 없습니다: {e.args[0]}") from e
    try:
        with sync_playwright() as p:
            browser = None
            try:
                # 컨테이너(root/LXC)에서 샌드박스 없이 구동
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                ctx = browser.new_context()
                page = ctx.new_page()
                _login_page(page, uid, pw)
                try:
                    return _authenticated_cookies(ctx)
                except LoginError:
                    _dump(page, "login-failed")
                    raise
            finally:
                if browser is not None:
                    browser.close()
    except PlaywrightError as e:
        # Playwright TimeoutError도 포함한다. 예외 원문에는 자격증명이 있을 수 있다.
        raise LoginError(f"브라우저 로그인/인증 확인 실패({type(e).__name__}).") from e


def _dump(page, tag):
    try:
        page.screenshot(path=f"login-debug-{tag}.png", full_page=True)
        Path(f"login-debug-{tag}.html").write_text(page.content())
    except Exception:
        pass


def _login_with_retry(attempts=None):
    """무인 재로그인 안정화용: LoginError 면 잠깐 쉬고 재시도, 마지막 실패는 그대로 올린다."""
    attempts = LOGIN_ATTEMPTS if attempts is None else attempts
    if attempts < 1:
        raise LoginError("LOGIN_ATTEMPTS는 1 이상이어야 합니다.")
    last = None
    for i in range(1, attempts + 1):
        try:
            return login_and_get_cookies()
        except LoginError as e:
            last = e
            if i < attempts:
                print(f"로그인 시도 {i}/{attempts} 실패: {e} -> 재시도", file=sys.stderr)
                time.sleep(3)
    raise last


def get_cookie(force=False):
    """동기화 스크립트가 부르는 진입점. Cookie 헤더 문자열을 반환."""
    if not force:
        cached = _load_cache()
        if cached:
            return _cookies_to_header(cached)
    cookies = _login_with_retry()
    _save_cache(cookies)
    return _cookies_to_header(cookies)


if __name__ == "__main__":
    # 단독 실행: 강제 로그인 테스트
    try:
        get_cookie(force="--force" in sys.argv)
        print("쿠키 발급 성공")
    except LoginError as e:
        sys.exit(str(e))

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
import time
from pathlib import Path

LOGIN_URL = "https://office.hiworks.com/login"
# 로그인 성공 판정: 이 도메인 쿠키가 잡히면 성공으로 본다
COOKIE_DOMAIN_HINT = "hiworks.com"
COOKIE_FILE = Path(os.getenv("COOKIE_FILE", "cookies.json"))
# 쿠키 유효로 간주할 최대 나이(초). 넘으면 재로그인. 12시간 기본.
MAX_AGE = int(os.getenv("COOKIE_MAX_AGE", str(12 * 3600)))


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
    COOKIE_FILE.write_text(json.dumps({"ts": time.time(), "cookies": cookies}))
    try:
        os.chmod(COOKIE_FILE, 0o600)
    except Exception:
        pass


def login_and_get_cookies():
    from playwright.sync_api import sync_playwright

    uid = os.environ["HIWORKS_ID"]
    pw = os.environ["HIWORKS_PW"]

    with sync_playwright() as p:
        # 컨테이너(root/LXC)에서 샌드박스 없이 구동
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="networkidle")

        # 아이디/비번 필드 후보들을 순서대로 시도
        id_selectors = [
            "input[type=email]",
            "input[name=id]", "input[name=userId]", "input[name=username]",
            "input[placeholder*='아이디']", "input[placeholder*='이메일']",
        ]
        pw_selectors = [
            "input[type=password]",
            "input[name=password]", "input[name=passwd]", "input[name=pw]",
        ]

        def fill_first(selectors, value):
            for s in selectors:
                el = page.query_selector(s)
                if el:
                    el.fill(value)
                    return True
            return False

        if not fill_first(id_selectors, uid) or not fill_first(pw_selectors, pw):
            _dump(page, "no-fields")
            browser.close()
            sys.exit("로그인 폼 필드를 못 찾음. login-debug.* 를 열어 셀렉터를 확인하세요.")

        # 제출: Enter 또는 로그인 버튼
        submitted = False
        for s in ["button[type=submit]", "button:has-text('로그인')", "input[type=submit]"]:
            el = page.query_selector(s)
            if el:
                el.click()
                submitted = True
                break
        if not submitted:
            page.keyboard.press("Enter")

        # 로그인 후 리다이렉트/쿠키 대기
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(2)

        cookies = ctx.cookies()  # 모든 도메인 쿠키
        hiworks_cookies = [c for c in cookies if COOKIE_DOMAIN_HINT in c.get("domain", "")]

        if not hiworks_cookies:
            _dump(page, "login-failed")
            browser.close()
            sys.exit("로그인 실패(쿠키 없음). 자격증명/캡차/차단 여부를 login-debug.* 로 확인하세요.")

        browser.close()
        # requests 에 쓰기 좋은 형태로 정리
        return [{"name": c["name"], "value": c["value"], "domain": c["domain"]} for c in hiworks_cookies]


def _dump(page, tag):
    try:
        page.screenshot(path=f"login-debug-{tag}.png", full_page=True)
        Path(f"login-debug-{tag}.html").write_text(page.content())
    except Exception:
        pass


def get_cookie(force=False):
    """동기화 스크립트가 부르는 진입점. Cookie 헤더 문자열을 반환."""
    if not force:
        cached = _load_cache()
        if cached:
            return _cookies_to_header(cached)
    cookies = login_and_get_cookies()
    _save_cache(cookies)
    return _cookies_to_header(cookies)


if __name__ == "__main__":
    # 단독 실행: 강제 로그인 테스트
    print(get_cookie(force="--force" in sys.argv)[:40] + "... (쿠키 발급 성공)")

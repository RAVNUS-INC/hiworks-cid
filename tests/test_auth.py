#!/usr/bin/env python3
"""실제 로그인 없이 인증 확인/재시도/쿠키 캐시를 검증한다."""
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import hiworks_auth as auth
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cookies.json"
        self.runtime = MagicMock()
        self.browser = self.runtime.chromium.launch.return_value
        self.context = self.browser.new_context.return_value
        self.page = self.context.new_page.return_value
        self.page.query_selector.return_value = None
        self.response = self.context.request.get.return_value
        self.response.status = 200
        self.payload = {
            "data": [{"no": 1, "type": "shared", "owner": False, "name": "테스트",
                      "phone": "01012345678", "company": None, "grade": ""}],
            "meta": {"page": {"limit": 1, "offset": 0, "total": 380}},
        }
        self.response.json.return_value = self.payload
        self.cookies = [{"name": "session", "value": "test-session-secret", "domain": ".hiworks.com"}]
        self.context.cookies.return_value = self.cookies
        self.playwright = MagicMock()
        self.playwright.return_value.__enter__.return_value = self.runtime
        patches = [
            patch.dict(os.environ, {"HIWORKS_ID": "test@example.com", "HIWORKS_PW": "test-password"}),
            patch.object(auth, "COOKIE_FILE", self.cache),
            patch.object(auth, "LOGIN_ATTEMPTS", 2),
            patch.object(auth.time, "sleep"),
            patch.object(auth, "_dump"),
            patch("playwright.sync_api.sync_playwright", self.playwright),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stderr = io.StringIO()
        redirect = redirect_stderr(self.stderr)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_verified_api_session_is_cached_and_cookies_are_scoped_to_api(self):
        self.assertEqual(auth.get_cookie(force=True), "session=test-session-secret")
        self.context.request.get.assert_called_once_with(
            auth.CONTACTS_API,
            params={"page[limit]": 1, "page[offset]": 0},
            headers={"Accept": "application/json"},
            timeout=15000,
        )
        self.context.cookies.assert_called_once_with(auth.CONTACTS_API)
        saved = json.loads(self.cache.read_text())
        self.assertEqual(saved["cookies"], self.cookies)
        self.assertIn("ts", saved)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.response.dispose.assert_called_once()
        self.browser.close.assert_called_once()

    def test_csrf_cookie_alone_does_not_establish_authenticated_session(self):
        self.context.cookies.return_value = [{"name": "csrf", "value": "public", "domain": ".hiworks.com"}]
        self.response.status = 401
        with self.assertRaises(auth.LoginError):
            auth.get_cookie(force=True)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.context.request.get.call_count, 2)
        self.context.cookies.assert_not_called()
        self.assertEqual(self.browser.close.call_count, 2)

    def test_failed_auth_statuses_are_not_cached(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.response.status = status
                with self.assertRaises(auth.LoginError):
                    auth.get_cookie(force=True)
                self.assertFalse(self.cache.exists())
        self.assertEqual(self.browser.close.call_count, 6)

    def test_http_200_error_envelope_or_incomplete_page_is_rejected(self):
        invalid = [
            {"code": "unauthorized", "message": "login required"},
            {"data": []},
            {"data": [], "meta": {"page": {"limit": 1, "offset": 0, "total": 380}}},
            {"data": self.payload["data"], "meta": {"page": {"limit": 1, "offset": 1, "total": 380}}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                with self.assertRaises(auth.LoginError):
                    auth.get_cookie(force=True)
                self.assertFalse(self.cache.exists())

    def test_non_json_probe_response_is_rejected(self):
        self.response.json.side_effect = ValueError("not JSON")
        with self.assertRaises(auth.LoginError):
            auth.get_cookie(force=True)
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.response.dispose.call_count, 2)

    def test_valid_empty_address_book_is_authenticated(self):
        self.response.json.return_value = {"data": [], "meta": {"page": {"limit": 1, "offset": 0, "total": 0}}}
        self.assertEqual(auth.get_cookie(force=True), "session=test-session-secret")

    def test_verified_response_without_applicable_cookies_is_rejected(self):
        self.context.cookies.return_value = []
        with self.assertRaises(auth.LoginError):
            auth.get_cookie(force=True)
        self.assertFalse(self.cache.exists())

    def test_browser_timeout_is_retried_and_browser_is_always_closed(self):
        self.page.goto.side_effect = [PlaywrightTimeoutError("sensitive trace"), None]
        self.assertEqual(auth.get_cookie(force=True), "session=test-session-secret")
        self.assertEqual(self.browser.close.call_count, 2)
        self.assertEqual(self.runtime.chromium.launch.call_count, 2)
        self.assertNotIn("sensitive trace", self.stderr.getvalue())

    def test_probe_timeout_is_retried(self):
        self.context.request.get.side_effect = [PlaywrightTimeoutError("probe timeout"), self.response]
        self.assertEqual(auth.get_cookie(force=True), "session=test-session-secret")
        self.assertEqual(self.context.request.get.call_count, 2)
        self.assertEqual(self.browser.close.call_count, 2)

    def test_repeated_timeout_does_not_overwrite_previous_cache(self):
        self.cache.write_text("previous cache")
        self.page.goto.side_effect = PlaywrightTimeoutError("sensitive trace")
        with self.assertRaises(auth.LoginError):
            auth.get_cookie(force=True)
        self.assertEqual(self.cache.read_text(), "previous cache")
        self.assertEqual(self.browser.close.call_count, 2)

    def test_existing_cache_format_is_reused_without_browser_login(self):
        self.cache.write_text(json.dumps({"ts": time.time(), "cookies": self.cookies}))
        self.assertEqual(auth.get_cookie(), "session=test-session-secret")
        self.playwright.assert_not_called()

    def test_atomic_cache_write_is_private_before_replace(self):
        self.cache.write_text("old cache")
        original_replace = os.replace
        observed = []

        def inspect_replace(source, target):
            observed.append(stat.S_IMODE(Path(source).stat().st_mode))
            self.assertEqual(self.cache.read_text(), "old cache")
            self.assertEqual(json.loads(Path(source).read_text())["cookies"], self.cookies)
            original_replace(source, target)

        with patch.object(auth.os, "replace", side_effect=inspect_replace):
            auth._save_cache(self.cookies)
        self.assertEqual(observed, [0o600])
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_failed_cache_replace_preserves_old_file_and_removes_temporary_file(self):
        self.cache.write_text("old cache")
        with patch.object(auth.os, "replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                auth._save_cache(self.cookies)
        self.assertEqual(self.cache.read_text(), "old cache")
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_cli_does_not_print_cached_session_value(self):
        self.cache.write_text(json.dumps({"ts": time.time(), "cookies": self.cookies}))
        result = subprocess.run(
            [sys.executable, str(Path(auth.__file__).resolve())],
            env={**os.environ, "COOKIE_FILE": str(self.cache)},
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), "쿠키 발급 성공")
        self.assertNotIn("test-session-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""HTTP/DB 복구 회귀 검사. 실제 DB/네트워크 없이 python tests/test_http_lookup.py."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import http_lookup as lookup


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=None):
        self.sql = sql
        self.connection.queries.append((sql, args))
        if self.connection.before_query:
            self.connection.before_query()
        if self.connection.error:
            raise self.connection.error

    def fetchone(self):
        if self.connection.closed:
            raise lookup.pymysql.err.InterfaceError(0, "connection closed during query")
        if "COUNT(*)" in self.sql:
            return {"c": 1}
        return self.connection.row


class FakeConnection:
    def __init__(self, row=None, error=None, before_query=None):
        self.row = row
        self.error = error
        self.before_query = before_query
        self.queries = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.json"
        self.now = 1_700_000_000
        self.state.write_text(json.dumps({"fails": 0, "last_success": self.now - 10}))
        self.row = {"name": "홍길동", "grade": "부장", "company": "개발팀"}
        self.connection = FakeConnection(self.row)
        self.patches = [
            patch.object(lookup, "_conn", None),
            patch.object(lookup, "_conn_lock", threading.Lock()),
            patch.object(lookup, "LOCK_TIMEOUT", 0.05),
            patch.object(lookup, "STATE_FILE", str(self.state)),
            patch.object(lookup, "HEALTH_MAX_FAILURES", 3),
            patch.object(lookup, "HEALTH_MAX_AGE", 600),
            patch.object(lookup.time, "time", return_value=self.now),
            patch.object(lookup, "_connect", return_value=self.connection),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connect = lookup._connect
        self.client = lookup.app.test_client()

    def write_state(self, state):
        self.state.write_text(json.dumps(state))

    def assert_empty_unavailable(self, response):
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data, b"")
        self.assertEqual(response.mimetype, "text/plain")

    def test_cid_and_opencnam_preserve_utf8_and_normalize_number(self):
        for path in ("/cid?number=010-1234-5678", "/opencnam/v3/phone/+821012345678"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), "홍길동 부장 (개발팀)")
                self.assertEqual(response.mimetype, "text/plain")
                self.assertEqual(self.connection.queries[-1][1], ("01012345678",))

    def test_missing_and_unregistered_numbers_keep_empty_success(self):
        response = self.client.get("/cid")
        self.assertEqual((response.status_code, response.data), (200, b""))
        self.connect.assert_not_called()
        self.connection.row = None
        response = self.client.get("/cid?number=01012345678")
        self.assertEqual((response.status_code, response.data), (200, b""))

    def test_cid_reconnects_stale_connection_on_first_request(self):
        stale = FakeConnection(error=lookup.pymysql.err.OperationalError(2006, "gone away"))
        lookup._conn = stale
        response = self.client.get("/cid?number=01012345678")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(stale.closed)
        self.assertFalse(self.connection.closed)
        self.assertIs(lookup._conn, self.connection)
        self.connect.assert_called_once()

    def test_health_reconnects_stale_connection_on_first_request(self):
        stale = FakeConnection(error=lookup.pymysql.err.InterfaceError(0, "closed"))
        lookup._conn = stale
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["rows"], 1)
        self.assertTrue(stale.closed)
        self.connect.assert_called_once()

    def test_retry_failure_resets_connection_and_returns_empty_503(self):
        for path in ("/cid?number=01012345678", "/opencnam/v3/phone/01012345678"):
            with self.subTest(path=path):
                stale = FakeConnection(error=lookup.pymysql.err.OperationalError(2006, "stale"))
                replacement = FakeConnection(error=lookup.pymysql.err.OperationalError(2013, "lost"))
                lookup._conn = stale
                self.connect.return_value = replacement
                self.assert_empty_unavailable(self.client.get(path))
                self.assertTrue(stale.closed)
                self.assertTrue(replacement.closed)
                self.assertIsNone(lookup._conn)
                self.assertFalse(lookup._conn_lock.locked())

    def test_connection_failure_returns_empty_503_after_one_retry(self):
        self.connect.side_effect = lookup.pymysql.err.OperationalError(2003, "cannot connect")
        self.assert_empty_unavailable(self.client.get("/cid?number=01012345678"))
        self.assertEqual(self.connect.call_count, 2)
        self.assertIsNone(lookup._conn)

    def test_nontransient_db_failure_is_not_retried_and_resets_connection(self):
        self.connection.error = lookup.pymysql.err.ProgrammingError(1146, "missing table")
        self.assert_empty_unavailable(self.client.get("/cid?number=01012345678"))
        self.connect.assert_called_once()
        self.assertTrue(self.connection.closed)
        self.assertIsNone(lookup._conn)

    def test_health_missing_or_malformed_state_is_unknown(self):
        for raw in (None, "{", "[]", "null", "0", "3", "true", '"state"'):
            with self.subTest(raw=raw):
                if raw is None:
                    self.state.unlink(missing_ok=True)
                else:
                    self.state.write_text(raw)
                response = self.client.get("/health")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["status"], "sync_unknown")
                self.assertEqual(response.json["sync_status"], "sync_unknown")

    def test_health_requires_valid_failure_count_and_success_time(self):
        states = [{}, {"fails": 0}, {"last_success": self.now}, {"fails": 3, "last_success": None}]
        states.extend({"fails": value, "last_success": self.now} for value in (
            None, True, False, -1, 1.5, "0", [], {}, float("nan"), float("inf"),
        ))
        states.extend({"fails": 0, "last_success": value} for value in (
            None, True, False, -1, 0, str(self.now), [], {}, float("nan"),
            float("inf"), float("-inf"), self.now + 1, 10 ** 400,
        ))
        for state in states:
            with self.subTest(state=state):
                self.write_state(state)
                response = self.client.get("/health")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["status"], "sync_unknown")

    def test_health_failure_and_staleness_thresholds(self):
        cases = [
            (2, 600, 200, "ok"),
            (3, 10, 503, "sync_failing"),
            (0, 601, 503, "sync_stale"),
            (3, 601, 503, "sync_failing"),
        ]
        for fails, age, code, status in cases:
            with self.subTest(fails=fails, age=age):
                self.write_state({"fails": fails, "last_success": self.now - age})
                response = self.client.get("/health")
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json["status"], status)
                self.assertEqual(response.json["sync_last_success_age_sec"], age)

    def test_health_uses_time_after_reading_newly_completed_sync(self):
        def newly_written_state(_file):
            lookup.time.time.return_value = self.now + 1
            return {"fails": 0, "last_success": self.now + 1}

        with patch.object(lookup.json, "load", side_effect=newly_written_state):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["sync_last_success_age_sec"], 0)

    def test_health_preserves_db_error_when_sync_also_failed(self):
        self.connect.side_effect = lookup.pymysql.err.OperationalError(2003, "cannot connect")
        self.write_state({"fails": 3, "last_success": self.now - 10})
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["status"], "db_error")
        self.assertEqual(response.json["sync_status"], "sync_failing")
        self.assertEqual(response.json["error"], "OperationalError")

    def test_stalled_query_does_not_block_other_requests_indefinitely(self):
        entered, release = threading.Event(), threading.Event()

        def block_query():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release fake DB")

        self.connection.before_query = block_query
        completed = []

        def request_cid():
            with lookup.app.test_client() as client:
                completed.append(client.get("/cid?number=01012345678"))

        worker = threading.Thread(target=request_cid, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assert_empty_unavailable(self.client.get("/cid?number=01012345678"))
            health = self.client.get("/health")
            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json["status"], "db_error")
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(len(self.connection.queries), 1)
            self.assertFalse(self.connection.closed)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed[0].status_code, 200)

    def test_health_recovery_keeps_cleanup_and_retry_inside_lock(self):
        stale = FakeConnection(error=lookup.pymysql.err.OperationalError(2006, "stale"))
        lookup._conn = stale
        entered, release = threading.Event(), threading.Event()
        original_reset = lookup._reset_conn
        cleanup_lock_states = []

        def record_cleanup():
            cleanup_lock_states.append(lookup._conn_lock.locked())
            original_reset()

        def block_replacement():
            entered.set()
            if not release.wait(3):
                raise AssertionError("test did not release replacement DB")

        self.connection.before_query = block_replacement
        completed = []

        def request_health():
            with lookup.app.test_client() as client:
                completed.append(client.get("/health"))

        with patch.object(lookup, "_reset_conn", side_effect=record_cleanup):
            worker = threading.Thread(target=request_health, daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assert_empty_unavailable(self.client.get("/cid?number=01012345678"))
                self.assertFalse(self.connection.closed)
                self.assertIs(lookup._conn, self.connection)
                self.assertEqual(cleanup_lock_states, [True])
            finally:
                release.set()
                worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed[0].status_code, 200)
        self.assertEqual(self.client.get("/cid?number=01012345678").status_code, 200)


class TimeoutConfigurationTests(unittest.TestCase):
    def test_connect_passes_finite_socket_timeouts(self):
        with patch.dict(os.environ, {"MYSQL_USER": "test", "MYSQL_PASSWORD": "test"}), \
                patch.object(lookup.pymysql, "connect") as connect, \
                patch.object(lookup, "CONNECT_TIMEOUT", 0.2), \
                patch.object(lookup, "READ_TIMEOUT", 0.3), \
                patch.object(lookup, "WRITE_TIMEOUT", 0.4):
            lookup._connect()
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 0.2)
        self.assertEqual(connect.call_args.kwargs["read_timeout"], 0.3)
        self.assertEqual(connect.call_args.kwargs["write_timeout"], 0.4)

    def test_invalid_timeout_settings_fail_at_configuration_load(self):
        for value in ("0", "-1", "nan", "inf", "bad"):
            with self.subTest(value=value), patch.dict(os.environ, {"TEST_DB_TIMEOUT": value}):
                with self.assertRaises(ValueError):
                    lookup._timeout_setting("TEST_DB_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

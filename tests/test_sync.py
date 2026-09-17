"""Test synchronization without calling real HTTP, database, or notification services."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import hiworks_sync as sync
from hiworks_payload import PayloadError
from test_payload import contact, contacts_page


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


class FetchContactsTests(unittest.TestCase):
    def setUp(self):
        self.cookie_patch = patch.object(sync, "_get_cookie", return_value="synthetic-cookie")
        self.cookie = self.cookie_patch.start()
        self.addCleanup(self.cookie_patch.stop)
        self.http_patch = patch.object(sync.requests, "get")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    def test_complete_multiple_pages_and_actual_page_limit(self):
        self.http.side_effect = [
            response(contacts_page([contact(1), contact(2)], limit=2, total=3)),
            response(contacts_page([contact(3)], limit=2, offset=2, total=3)),
        ]
        self.assertEqual([row["no"] for row in sync.fetch_all()], [1, 2, 3])
        self.assertEqual([call.kwargs["params"]["page[offset]"] for call in self.http.call_args_list], [0, 2])

    def test_explicit_empty_snapshot(self):
        self.http.return_value = response(contacts_page([]))
        self.assertEqual(sync.fetch_all(), [])

    def test_error_envelope_and_missing_metadata_are_rejected(self):
        for payload in ({"code": "EXPIRED"}, {"data": [contact()]}):
            self.http.return_value = response(payload)
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                sync.fetch_all()

    def test_incomplete_first_page_is_rejected(self):
        self.http.return_value = response(contacts_page([contact()], limit=500, total=2))
        with self.assertRaises(PayloadError):
            sync.fetch_all()

    def test_premature_empty_second_page_is_rejected(self):
        self.http.side_effect = [
            response(contacts_page([contact(1)], limit=1, total=2)),
            response(contacts_page([], limit=1, offset=1, total=2)),
        ]
        with self.assertRaises(PayloadError):
            sync.fetch_all()

    def test_page_offset_total_or_duplicate_id_mismatch_is_rejected(self):
        second_pages = [
            contacts_page([contact(2)], limit=1, offset=0, total=2),
            contacts_page([contact(2)], limit=1, offset=1, total=3),
            contacts_page([contact(1)], limit=1, offset=1, total=2),
        ]
        for second in second_pages:
            self.http.side_effect = [response(contacts_page([contact(1)], limit=1, total=2)), response(second)]
            with self.subTest(second=second), self.assertRaises(PayloadError):
                sync.fetch_all()

    def test_duplicate_ids_in_one_page_are_rejected(self):
        self.http.return_value = response(contacts_page([contact(1), contact(1)]))
        with self.assertRaises(PayloadError):
            sync.fetch_all()

    def test_refresh_cookie_once_without_skipping_page(self):
        self.http.side_effect = [response(None, 401), response(contacts_page())]
        self.assertEqual(sync.fetch_all(), [contact()])
        self.cookie.assert_any_call(force=True)
        self.assertEqual([call.kwargs["params"]["page[offset]"] for call in self.http.call_args_list], [0, 0])

    def test_second_authentication_failure_is_reported(self):
        self.http.side_effect = [response(None, 401), response(None, 403)]
        with self.assertRaises(RuntimeError):
            sync.fetch_all()


class SnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.state_file = Path(folder) / "state.json"
        self.state_file.write_text(json.dumps({"fails": 0, "last_success": 12345}))
        self.resources.enter_context(patch.object(sync, "STATE_FILE", self.state_file))
        self.resources.enter_context(patch.object(sync, "_get_cookie", return_value="synthetic-cookie"))
        self.http = self.resources.enter_context(patch.object(sync.requests, "get"))
        self.database = self.resources.enter_context(patch.object(sync, "sync_mysql"))
        self.heartbeat = self.resources.enter_context(patch.object(sync, "_heartbeat"))
        self.alert = self.resources.enter_context(patch.object(sync, "_send_alert"))
        self.resources.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.resources.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def assert_failed_without_database_update(self):
        with self.assertRaises(SystemExit) as raised:
            sync.main()
        self.assertEqual(raised.exception.code, 1)
        self.database.assert_not_called()
        self.heartbeat.assert_not_called()
        self.assertEqual(sync._load_state(), {"fails": 1, "last_success": 12345})

    def test_invalid_contact_response_never_updates_database(self):
        self.http.return_value = response({"code": "EXPIRED"})
        self.assert_failed_without_database_update()

    def test_falsey_organization_responses_never_remove_employees(self):
        with patch.object(sync, "ORG_TOKEN", "synthetic-token"):
            for invalid in (None, [], False, "", {}):
                self.state_file.write_text(json.dumps({"fails": 0, "last_success": 12345}))
                self.http.side_effect = [response(contacts_page()), response(invalid)]
                with self.subTest(payload=invalid):
                    self.assert_failed_without_database_update()

    def test_incomplete_pages_never_update_database(self):
        self.http.side_effect = [
            response(contacts_page([contact(1)], limit=1, total=2)),
            response(contacts_page([], limit=1, offset=1, total=2)),
        ]
        self.assert_failed_without_database_update()

    def test_explicit_empty_snapshot_is_allowed(self):
        with patch.object(sync, "ORG_TOKEN", None):
            self.http.return_value = response(contacts_page([]))
            sync.main()
        self.database.assert_called_once_with([])
        self.heartbeat.assert_called_once_with()
        self.assertEqual(sync._load_state()["fails"], 0)

    def test_organization_disabled_does_not_make_request(self):
        with patch.object(sync, "ORG_TOKEN", None):
            self.assertIsNone(sync.fetch_org())
        self.http.assert_not_called()

    def test_success_merges_employees_with_precedence_and_clears_failures(self):
        self.state_file.write_text(json.dumps({"fails": 3, "last_success": 12345}))
        organization = {"name": "Department", "entries": [{"name": "Employee", "phone": "", "cell": "01011112222"}]}
        with patch.object(sync, "ORG_TOKEN", "synthetic-token"), patch.object(sync, "ALERT_AFTER_FAILURES", 3):
            self.http.side_effect = [response(contacts_page()), response(organization)]
            sync.main()
        self.database.assert_called_once_with([("01011112222", "Employee", "Department", None)])
        self.alert.assert_called_once_with({"status": "recovered", "after_failures": 3})
        self.assertEqual(sync._load_state()["fails"], 0)
        self.assertGreater(sync._load_state()["last_success"], 12345)


class StateFileTests(unittest.TestCase):
    def test_atomic_replacement_keeps_previous_file_until_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            old = {"fails": 3, "last_success": 100}
            new = {"fails": 0, "last_success": 200}
            target.write_text(json.dumps(old))
            replace = sync.os.replace

            def inspect_replace(source, destination):
                self.assertEqual(Path(source).parent, target.parent)
                self.assertEqual(json.loads(target.read_text()), old)
                self.assertEqual(json.loads(Path(source).read_text()), new)
                replace(source, destination)

            with patch.object(sync, "STATE_FILE", target), patch.object(sync.os, "replace", side_effect=inspect_replace):
                sync._save_state(new)
                self.assertEqual(sync._load_state(), new)
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_failed_replacement_preserves_previous_state_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "state.json"
            old = {"fails": 3, "last_success": 100}
            target.write_text(json.dumps(old))
            with patch.object(sync, "STATE_FILE", target), patch.object(sync.os, "replace", side_effect=OSError("synthetic failure")), contextlib.redirect_stderr(io.StringIO()):
                sync._save_state({"fails": 0, "last_success": 200})
                self.assertEqual(sync._load_state(), old)
            self.assertEqual(list(Path(folder).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()

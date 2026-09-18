"""Test synchronization without calling real HTTP, database, or notification services."""

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import hiworks_sync as sync
from hiworks_payload import PayloadError
from test_payload import contact, contact_detail, contacts_page


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


class ContactDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "contact_details.json"
        self.row = contact(7)
        self.row["updated_at"] = "2026-09-18T00:00:00"
        patches = [
            patch.object(sync, "DETAIL_CACHE_FILE", self.cache),
            patch.object(sync, "DETAIL_CACHE_MAX_AGE", 3600),
            patch.object(sync, "DETAIL_WORKERS", 1),
            patch.object(sync.time, "time", return_value=1_000),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_detail_request_uses_required_headers_and_builds_every_phone_type(self):
        phones = [
            {"type": "휴대폰", "phone": "010-1111-2222", "is_default": True},
            {"type": "회사전화", "phone": "02-1234-5678", "is_default": False},
            {"type": "팩스", "phone": "02-9876-5432", "is_default": False},
            {"type": "기타", "phone": "1588-0000", "is_default": False},
        ]
        payload = contact_detail(7, phones)
        with patch.object(sync.requests, "get", return_value=response(payload)) as get:
            actual = sync._fetch_contact_detail(self.row, "session=secret")
        self.assertEqual(actual, payload["data"])
        self.assertEqual(get.call_args.args[0], f"{sync.API}/7")
        self.assertEqual(get.call_args.kwargs["headers"], {
            "Cookie": "session=secret", "Accept": "application/json", "type": "shared",
            "Origin": sync.CONTACT_WEB_ORIGIN, "Referer": f"{sync.CONTACT_WEB_ORIGIN}/",
        })
        entries = sync.build_entries([self.row], {7: payload["data"]})
        self.assertEqual(set(entries), {"01011112222", "0212345678", "0298765432", "15880000"})

    def test_unchanged_private_cache_avoids_detail_requests(self):
        phones = [{"type": "휴대폰", "phone": "01011112222", "is_default": True}]
        detail = contact_detail(7, phones)["data"]
        sync._save_detail_cache({"7": {
            "updated_at": self.row["updated_at"], "fetched_at": 999, "detail": detail,
        }})
        with patch.object(sync, "_get_cookie") as cookie, patch.object(sync.requests, "get") as get:
            self.assertEqual(sync.fetch_contact_details([self.row]), {7: detail})
        cookie.assert_not_called()
        get.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)

    def test_changed_contact_refreshes_cache(self):
        old_phones = [{"type": "휴대폰", "phone": "01011112222", "is_default": True}]
        new_phones = [
            *old_phones,
            {"type": "회사전화", "phone": "02-1234-5678", "is_default": False},
        ]
        old_detail = contact_detail(7, old_phones)["data"]
        new_detail = contact_detail(7, new_phones)["data"]
        sync._save_detail_cache({"7": {
            "updated_at": "old", "fetched_at": 999, "detail": old_detail,
        }})
        with patch.object(sync, "_get_cookie", return_value="session=secret"), \
                patch.object(sync.requests, "get", return_value=response(contact_detail(7, new_phones))) as get:
            self.assertEqual(sync.fetch_contact_details([self.row]), {7: new_detail})
        self.assertEqual(get.call_count, 1)
        saved = json.loads(self.cache.read_text())
        self.assertEqual(saved["contacts"]["7"]["detail"], new_detail)
        self.assertEqual(saved["contacts"]["7"]["updated_at"], self.row["updated_at"])

    def test_partial_detail_never_replaces_existing_cache(self):
        old = {"version": 2, "contacts": {"7": {
            "updated_at": "old", "fetched_at": 999,
            "detail": contact_detail(7)["data"],
        }}}
        self.cache.write_text(json.dumps(old))
        incomplete = contact_detail(7, [
            {"type": "회사전화", "phone": "02-1234-5678", "is_default": True},
        ])
        with patch.object(sync, "_get_cookie", return_value="session=secret"), \
                patch.object(sync.requests, "get", return_value=response(incomplete)):
            with self.assertRaises(PayloadError):
                sync.fetch_contact_details([self.row])
        self.assertEqual(json.loads(self.cache.read_text()), old)

    def test_authentication_failures_relogin_once(self):
        phones = [{"type": "휴대폰", "phone": "01011112222", "is_default": True}]
        first = response(None, 401)
        second = response(contact_detail(7, phones))
        with patch.object(sync, "_get_cookie", side_effect=["old", "new"]) as cookie, \
                patch.object(sync.requests, "get", side_effect=[first, second]):
            self.assertEqual(sync.fetch_contact_details([self.row]), {7: contact_detail(7, phones)["data"]})
        self.assertEqual(cookie.call_args_list[1].kwargs, {"force": True})


class ContactMirrorAndCidPolicyTests(unittest.TestCase):
    def detail(self, number, phones, **extra):
        result = contact_detail(number, phones, **extra)["data"]
        result.update({
            "name": extra.get("name", "Example"),
            "company": extra.get("company", "Example Company"),
            "department": "Design", "grade": "Manager", "homepage": "https://example.invalid",
            "birth": "", "memo": "memo", "image": "", "calendar_type": "solar",
            "allow_editing": True, "is_star": False, "is_owner": False,
            "updater": "Admin", "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-09-18 00:00:00",
        })
        return result

    def test_mirror_preserves_full_payload_and_relations(self):
        row = contact(7)
        row.update({"email": "person@example.invalid", "department": "Design", "custom_future": {"x": 1}})
        detail = self.detail(
            7,
            [{"type": "휴대폰", "phone": "010-1111-2222", "is_default": True}],
            emails=[{"email": "person@example.invalid", "is_default": True}],
            addresses=[{"type": "회사", "address": "Synthetic address"}],
            tags=[{"name": "Vendor"}],
        )
        detail["custom_future_detail"] = ["kept"]
        snapshot = sync.build_contact_snapshot([row], {7: detail})
        self.assertEqual({key: len(value) for key, value in snapshot.items()}, {
            "contacts": 1, "phones": 1, "emails": 1, "addresses": 1, "tags": 1,
        })
        self.assertIn('"custom_future":{"x":1}', snapshot["contacts"][0][-2])
        self.assertIn('"custom_future_detail":["kept"]', snapshot["contacts"][0][-1])
        self.assertEqual(snapshot["phones"][0][5], "01011112222")

    def test_shared_company_and_fax_numbers_display_company(self):
        rows = [contact(1), contact(2)]
        rows[0].update({"name": "Person One", "company": "Example Co."})
        rows[1].update({"name": "Person Two", "company": "Example Co."})
        details = {
            1: self.detail(1, [{"type": "회사", "phone": "02-1111-2222", "is_default": True}]),
            2: self.detail(2, [{"type": "fax", "phone": "02-1111-2222", "is_default": True}]),
        }
        entries, conflicts = sync.build_entries(rows, details, include_conflicts=True)
        self.assertEqual(entries["0211112222"], ("Example Co.", None, None))
        self.assertEqual(conflicts[0]["kind"], "shared_company_number")

    def test_duplicate_mobile_never_selects_an_arbitrary_different_person(self):
        rows = [contact(1), contact(2)]
        rows[0].update({"name": "Person One", "company": "Company A"})
        rows[1].update({"name": "Person Two", "company": "Company B"})
        details = {
            row["no"]: self.detail(row["no"], [
                {"type": "휴대폰", "phone": "010-1111-2222", "is_default": True},
            ]) for row in rows
        }
        entries, conflicts = sync.build_entries(rows, details, include_conflicts=True)
        self.assertEqual(entries["01011112222"], ("중복 연락처", None, None))
        self.assertEqual(conflicts[0]["kind"], "duplicate_mobile_different_names")


class DatabaseWriteTests(unittest.TestCase):
    def test_mirror_and_cid_are_committed_in_one_transaction(self):
        cursor = Mock()
        context = Mock()
        context.__enter__ = Mock(return_value=cursor)
        context.__exit__ = Mock(return_value=False)
        connection = Mock()
        connection.cursor.return_value = context
        snapshot = {
            "contacts": [("shared", 1, False, "Person", "Company", None, None,
                          None, None, None, None, None, True, False, False, "Admin",
                          "created", "updated", "{}", "{}")],
            "phones": [("shared", 1, 0, "휴대폰", "010-1111-2222",
                        "01011112222", True, "{}")],
            "emails": [("shared", 1, 0, "person@example.invalid", True, "{}")],
            "addresses": [],
            "tags": [],
        }
        with patch.dict(sync.os.environ, {"MYSQL_USER": "test", "MYSQL_PASSWORD": "test"}), \
                patch.object(sync.pymysql, "connect", return_value=connection):
            sync.sync_mysql([("01011112222", "Person", "Company", None)], snapshot,
                            {"name": "Root", "entries": []})
        sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(sql[0], "DELETE FROM hiworks_contacts")
        self.assertTrue(any(statement.startswith("INSERT INTO hiworks_organization_snapshot")
                            for statement in sql))
        self.assertTrue(any(statement.startswith("DELETE FROM cid_lookup WHERE phone NOT IN")
                            for statement in sql))
        inserted_tables = [call.args[0].split()[2] for call in cursor.executemany.call_args_list]
        self.assertEqual(inserted_tables, [
            "hiworks_contacts", "hiworks_contact_phones", "hiworks_contact_emails", "cid_lookup",
        ])
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()


class SnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        folder = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.state_file = Path(folder) / "state.json"
        self.state_file.write_text(json.dumps({"fails": 0, "last_success": 12345}))
        self.resources.enter_context(patch.object(sync, "STATE_FILE", self.state_file))
        self.resources.enter_context(patch.object(sync, "_get_cookie", return_value="synthetic-cookie"))
        self.resources.enter_context(patch.object(
            sync, "fetch_contact_details",
            side_effect=lambda rows: {
                row["no"]: contact_detail(row["no"], [
                    {"type": "기본", "phone": row["phone"], "is_default": True}
                ])["data"]
                for row in rows
                if row.get("type") == "shared" and not row.get("owner")
            },
        ))
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
        self.database.assert_called_once_with([], {
            "contacts": [], "phones": [], "emails": [], "addresses": [], "tags": [],
        }, None)
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
        database_args = self.database.call_args.args
        self.assertEqual(database_args[0], [("01011112222", "Employee", "Department", None)])
        self.assertEqual(len(database_args[1]["contacts"]), 1)
        self.assertEqual(database_args[2], organization)
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

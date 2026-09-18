"""API response validation uses only synthetic, non-sensitive data."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hiworks_payload import PayloadError, parse_contact_detail, parse_contacts_page, validate_org_tree


def contact(number=1):
    return {
        "no": number, "type": "shared", "owner": False,
        "name": "Example", "phone": "01011112222", "company": "", "grade": "",
    }


def contacts_page(rows=None, *, limit=500, offset=0, total=None):
    rows = [contact()] if rows is None else rows
    return {
        "meta": {"page": {"limit": limit, "offset": offset, "total": len(rows) if total is None else total}},
        "data": rows,
    }


def contact_detail(number=1, phones=None, emails=None, addresses=None, tags=None):
    return {
        "data": {
            "no": number,
            "phones": phones if phones is not None else [
                {"type": "휴대폰", "phone": "01011112222", "is_default": True},
            ],
            "emails": emails if emails is not None else [
                {"email": "person@example.invalid", "is_default": True},
            ],
            "addresses": addresses if addresses is not None else [],
            "tags": tags if tags is not None else [],
        },
    }


class ContactsPayloadTests(unittest.TestCase):
    def test_returns_page_and_rows(self):
        page = parse_contacts_page(contacts_page())
        self.assertEqual((page.limit, page.offset, page.total), (500, 0, 1))
        self.assertEqual(page.rows, [contact()])

    def test_explicit_zero_total_and_empty_array(self):
        self.assertEqual(parse_contacts_page(contacts_page([])).rows, [])

    def test_missing_or_wrong_response_shape_is_rejected(self):
        invalid = [None, [], False, "", {}, {"code": "EXPIRED"}, {"data": []}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                parse_contacts_page(payload)
        for field, value in (("meta", None), ("data", None), ("data", {})):
            payload = contacts_page()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PayloadError):
                parse_contacts_page(payload)

    def test_pagination_fields_require_valid_integers(self):
        for field in ("limit", "offset", "total"):
            for value in (None, "1", True, -1):
                payload = contacts_page()
                payload["meta"]["page"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(PayloadError):
                    parse_contacts_page(payload)
        with self.assertRaises(PayloadError):
            parse_contacts_page(contacts_page(limit=0))

    def test_invalid_rows_and_consumed_fields_are_rejected(self):
        for row in (None, [], "invalid", {}):
            with self.subTest(row=row), self.assertRaises(PayloadError):
                parse_contacts_page(contacts_page([row]))
        invalid_values = {"no": True, "type": None, "owner": "false", "name": 1, "phone": [], "company": {}, "grade": []}
        for field, value in invalid_values.items():
            row = contact()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(PayloadError):
                parse_contacts_page(contacts_page([row]))

    def test_optional_display_fields_allow_null_or_omission(self):
        row = contact()
        del row["company"]
        row["grade"] = None
        self.assertEqual(parse_contacts_page(contacts_page([row])).rows, [row])


class ContactDetailPayloadTests(unittest.TestCase):
    def test_returns_all_phone_types(self):
        payload = contact_detail(7, [
            {"type": "휴대폰", "phone": "010-1111-2222", "is_default": True},
            {"type": "회사전화", "phone": "02-1234-5678", "is_default": False},
            {"type": "팩스", "phone": "02-9876-5432", "is_default": False},
            {"type": "기타", "phone": "1588-0000", "is_default": False},
        ])
        self.assertEqual(parse_contact_detail(payload, expected_no=7)["phones"], payload["data"]["phones"])

    def test_rejects_wrong_contact_or_invalid_phone_shape(self):
        with self.assertRaises(PayloadError):
            parse_contact_detail(contact_detail(2), expected_no=1)
        invalid = [None, {}, {"data": {}}, contact_detail()]
        invalid[-1]["data"]["phones"] = None
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                parse_contact_detail(payload)

    def test_rejects_invalid_email_or_other_array_shape(self):
        for field, value in (("email", None), ("is_default", 1)):
            payload = contact_detail()
            payload["data"]["emails"][0][field] = value
            with self.subTest(field=field), self.assertRaises(PayloadError):
                parse_contact_detail(payload)
        for field in ("emails", "addresses", "tags"):
            payload = contact_detail()
            payload["data"][field] = None
            with self.subTest(field=field), self.assertRaises(PayloadError):
                parse_contact_detail(payload)
        for field, value in (("type", None), ("phone", []), ("is_default", 1)):
            payload = contact_detail()
            payload["data"]["phones"][0][field] = value
            with self.subTest(field=field), self.assertRaises(PayloadError):
                parse_contact_detail(payload)


class OrganizationPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tree = {
            "name": "Root", "entries": [],
            "nodes": [{"name": "Department", "entries": [{"name": "Employee", "phone": "", "cell": "01011112222"}]}],
        }

    def test_nested_tree_and_leaf_without_nodes(self):
        self.assertEqual(validate_org_tree(self.tree), self.tree)

    def test_explicit_empty_tree(self):
        tree = {"name": "Root", "entries": [], "nodes": []}
        self.assertEqual(validate_org_tree(tree), tree)

    def test_falsey_and_error_envelopes_are_rejected(self):
        for payload in (None, [], False, "", {}, {"code": "EXPIRED"}):
            with self.subTest(payload=payload), self.assertRaises(PayloadError):
                validate_org_tree(payload)

    def test_invalid_nested_shape_is_rejected(self):
        for field, value in (("entries", None), ("entries", {}), ("nodes", None), ("nodes", {}), ("name", None)):
            tree = copy.deepcopy(self.tree)
            tree["nodes"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(PayloadError):
                validate_org_tree(tree)
        for field in ("name", "phone", "cell"):
            tree = copy.deepcopy(self.tree)
            del tree["nodes"][0]["entries"][0][field]
            with self.subTest(missing=field), self.assertRaises(PayloadError):
                validate_org_tree(tree)


if __name__ == "__main__":
    unittest.main()

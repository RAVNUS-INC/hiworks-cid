"""API response validation uses only synthetic, non-sensitive data."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hiworks_payload import PayloadError, parse_contacts_page, validate_org_tree


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

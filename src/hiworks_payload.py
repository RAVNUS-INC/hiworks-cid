"""Validate Hiworks responses before they can become a database snapshot."""

from dataclasses import dataclass


class PayloadError(ValueError):
    """An API response is not a usable contact or organization snapshot."""


@dataclass(frozen=True)
class ContactsPage:
    rows: list[dict]
    limit: int
    offset: int
    total: int


def _object(value, location):
    if not isinstance(value, dict):
        raise PayloadError(f"{location}: 객체가 필요합니다.")
    return value


def _array(value, location):
    if not isinstance(value, list):
        raise PayloadError(f"{location}: 배열이 필요합니다.")
    return value


def _string_field(row, field, location, *, optional=False):
    value = row.get(field)
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise PayloadError(f"{location}.{field}: 문자열이 필요합니다.")


def _integer(value, location, minimum):
    if type(value) is not int or value < minimum:
        raise PayloadError(f"{location}: {minimum} 이상의 정수가 필요합니다.")
    return value


def parse_contacts_page(payload):
    """Return validated rows and pagination metadata; do not infer missing data."""
    root = _object(payload, "연락처 응답")
    meta = _object(root.get("meta"), "연락처 meta")
    page = _object(meta.get("page"), "연락처 meta.page")
    limit = _integer(page.get("limit"), "연락처 meta.page.limit", 1)
    offset = _integer(page.get("offset"), "연락처 meta.page.offset", 0)
    total = _integer(page.get("total"), "연락처 meta.page.total", 0)
    rows = _array(root.get("data"), "연락처 data")
    for index, value in enumerate(rows):
        location = f"연락처 data[{index}]"
        row = _object(value, location)
        _integer(row.get("no"), f"{location}.no", 0)
        for field in ("type", "name", "phone"):
            _string_field(row, field, location)
        if type(row.get("owner")) is not bool:
            raise PayloadError(f"{location}.owner: 불리언이 필요합니다.")
        for field in ("company", "grade"):
            _string_field(row, field, location, optional=True)
    return ContactsPage(rows=rows, limit=limit, offset=offset, total=total)


def parse_contact_detail(payload, expected_no=None):
    """Return one validated contact detail including every multi-value field."""
    root = _object(payload, "연락처 상세 응답")
    row = _object(root.get("data"), "연락처 상세 data")
    contact_no = _integer(row.get("no"), "연락처 상세 data.no", 0)
    if expected_no is not None and contact_no != expected_no:
        raise PayloadError("연락처 상세 ID가 요청한 연락처와 다릅니다.")
    phones = _array(row.get("phones"), "연락처 상세 data.phones")
    for index, value in enumerate(phones):
        location = f"연락처 상세 data.phones[{index}]"
        phone = _object(value, location)
        for field in ("type", "phone"):
            _string_field(phone, field, location)
        if type(phone.get("is_default")) is not bool:
            raise PayloadError(f"{location}.is_default: 불리언이 필요합니다.")
    emails = _array(row.get("emails"), "연락처 상세 data.emails")
    for index, value in enumerate(emails):
        location = f"연락처 상세 data.emails[{index}]"
        email = _object(value, location)
        _string_field(email, "email", location)
        if type(email.get("is_default")) is not bool:
            raise PayloadError(f"{location}.is_default: 불리언이 필요합니다.")
    _array(row.get("addresses"), "연락처 상세 data.addresses")
    _array(row.get("tags"), "연락처 상세 data.tags")

    for field in (
        "name", "company", "department", "grade", "homepage", "calendar_type",
        "birth", "memo", "type", "updater", "created_at", "updated_at", "image",
    ):
        if field in row:
            _string_field(row, field, "연락처 상세 data", optional=True)
    for field in ("allow_editing", "is_star", "is_owner"):
        if field in row and row[field] is not None and type(row[field]) is not bool:
            raise PayloadError(f"연락처 상세 data.{field}: 불리언이 필요합니다.")
    return row


def validate_org_tree(payload):
    """Validate every department and employee, including an explicitly empty tree."""
    def walk(value, location):
        node = _object(value, location)
        _string_field(node, "name", location)
        entries = _array(node.get("entries"), f"{location}.entries")
        for index, value in enumerate(entries):
            entry_location = f"{location}.entries[{index}]"
            entry = _object(value, entry_location)
            for field in ("name", "phone", "cell"):
                _string_field(entry, field, entry_location)
        # Leaf departments may omit nodes; a present null/wrong type is invalid.
        children = _array(node.get("nodes", []), f"{location}.nodes")
        for index, child in enumerate(children):
            walk(child, f"{location}.nodes[{index}]")

    walk(payload, "조직도 응답")
    return payload

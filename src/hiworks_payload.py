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

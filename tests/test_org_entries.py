#!/usr/bin/env python3
"""build_org_entries tests: python -m unittest discover -s tests."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from hiworks_sync import build_org_entries  # noqa: E402

# 하이웍스 hrm/v2/organizations 응답 예시(문서 샘플)에 번호만 채운 형태
SAMPLE = {
    "node_id": "12312",
    "name": "하이웍스",
    "entries": [
        {"name": "홍길동", "phone": "02-123-4567", "cell": "010-1111-2222", "node_id": "12312"},
        {"name": "김철수", "phone": "", "cell": "+82 10-3333-4444", "node_id": "12312"},
        {"name": "", "phone": "010-9999-0000", "cell": "", "node_id": "12312"},  # 이름 없음 → 제외
    ],
    "nodes": [
        {
            "node_id": "23434",
            "name": "하이웍스 마케팅",
            "parent_no": "12312",
            "entries": [
                {"name": "김길동", "phone": "", "cell": "01055556666"},
                {"name": "김희선", "phone": "031-777-8888", "cell": ""},
            ],
            "nodes": [
                {
                    "node_id": "34545",
                    "name": "퍼포먼스팀",
                    "entries": [{"name": "박깊이", "phone": "", "cell": "010-1234-0000"}],
                }
            ],
        }
    ],
}

EXPECT = {
    "01011112222": ("홍길동", "하이웍스", None),
    "021234567":   ("홍길동", "하이웍스", None),
    "01033334444": ("김철수", "하이웍스", None),
    "01055556666": ("김길동", "하이웍스 마케팅", None),
    "0317778888":  ("김희선", "하이웍스 마케팅", None),
    "01012340000": ("박깊이", "퍼포먼스팀", None),   # 중첩 노드 재귀
}


class OrgEntriesTests(unittest.TestCase):
    def test_nested_departments_and_phone_normalization(self):
        self.assertEqual(build_org_entries(SAMPLE), EXPECT)

    def test_multiple_numbers_and_first_employee_precedence(self):
        root = {
            "name": "Department",
            "entries": [
                {"name": "First", "cell": "010-1111-2222 / 010-3333-4444", "phone": "02-123-4567"},
                {"name": "Second", "cell": "01011112222", "phone": ""},
            ],
        }
        self.assertEqual(build_org_entries(root), {
            "01011112222": ("First", "Department", None),
            "01033334444": ("First", "Department", None),
            "021234567": ("First", "Department", None),
        })

    def test_explicit_empty_department(self):
        self.assertEqual(build_org_entries({"name": "Department", "entries": []}), {})


if __name__ == "__main__":
    unittest.main()

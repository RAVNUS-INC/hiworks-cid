#!/usr/bin/env python3
"""phone_norm.normalize 단위 테스트. 의존성 없이 실행: python tests/test_normalize.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from phone_norm import normalize  # noqa: E402

CASES = [
    # (입력, 기대값)  — 저장 측 기본(min_len=8)
    ("010-1234-5678", "01012345678"),      # 하이픈 제거
    ("010 1234 5678", "01012345678"),      # 공백 제거
    ("+82 10-1234-5678", "01012345678"),   # +82 → 0
    ("821012345678", "01012345678"),       # 82 → 0
    ("008210 1234 5678", "01012345678"),   # 0082(국제접속) → 0
    ("02-123-4567", "021234567"),          # 서울 9자리 유지
    ("1544-1234", "15441234"),             # 대표번호 8자리 유지
    ("+82-2-1234-5678", "0212345678"),     # +82 서울 02-1234-5678
    ("(031) 123-4567", "0311234567"),      # 괄호/지역번호
    ("", None),                            # 빈 값
    (None, None),                          # None
    ("123", None),                         # 8자리 미만 → 저장 안 함
    ("abc", None),                         # 숫자 없음
]

# 조회 측(min_len=1): 짧아도 숫자열은 그대로 질의, 국제표기 변환은 동일
LOOKUP_CASES = [
    ("+821012345678", "01012345678"),
    ("01012345678", "01012345678"),
    ("123", "123"),                        # 저장 측과 달리 짧아도 통과
    ("", None),
]


class NormalizeTests(unittest.TestCase):
    def test_saved_numbers(self):
        for inp, exp in CASES:
            with self.subTest(number=inp):
                self.assertEqual(normalize(inp), exp)

    def test_lookup_numbers(self):
        for inp, exp in LOOKUP_CASES:
            with self.subTest(number=inp):
                self.assertEqual(normalize(inp, min_len=1), exp)

    def test_storage_and_lookup_share_rules(self):
        for inp in ["+821012345678", "010-1234-5678", "0082-2-123-4567"]:
            with self.subTest(number=inp):
                self.assertEqual(normalize(inp), normalize(inp, min_len=1))

    def test_equivalent_international_and_domestic_numbers_match(self):
        forms = {
            "01012345678": ["+82 (0)10-1234-5678", "0082 010-1234-5678",
                            "82 010-1234-5678", "+82 10-1234-5678", "010-1234-5678"],
            "0212345678": ["+82 (0)2-1234-5678", "0082 02-1234-5678", "+82 2-1234-5678"],
        }
        for domestic, variants in forms.items():
            for variant in variants:
                with self.subTest(number=variant):
                    self.assertEqual(normalize(variant), domestic)
                    self.assertEqual(normalize(variant, min_len=1), normalize(domestic, min_len=1))


if __name__ == "__main__":
    unittest.main()

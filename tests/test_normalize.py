#!/usr/bin/env python3
"""phone_norm.normalize 단위 테스트. 의존성 없이 실행: python tests/test_normalize.py"""
import os
import sys

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


def run():
    fails = 0
    for inp, exp in CASES:
        got = normalize(inp)
        ok = got == exp
        fails += not ok
        print(f"{'ok ' if ok else 'FAIL'} normalize({inp!r}) = {got!r} (기대 {exp!r})")
    for inp, exp in LOOKUP_CASES:
        got = normalize(inp, min_len=1)
        ok = got == exp
        fails += not ok
        print(f"{'ok ' if ok else 'FAIL'} normalize({inp!r}, min_len=1) = {got!r} (기대 {exp!r})")

    # 일관성 계약: 저장/조회가 같은 입력에 대해 (둘 다 값이 나올 때) 동일해야 함
    for inp in ["+821012345678", "010-1234-5678", "0082-2-123-4567"]:
        a = normalize(inp)                 # 저장
        b = normalize(inp, min_len=1)      # 조회
        ok = a == b
        fails += not ok
        print(f"{'ok ' if ok else 'FAIL'} 일관성 {inp!r}: 저장={a!r} 조회={b!r}")

    print(f"\n{'PASSED' if fails == 0 else f'{fails} FAILED'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

#!/usr/bin/env python3
"""
전화번호 정규화 (저장 측 hiworks_sync 와 조회 측 http_lookup 이 공유).

두 경로가 반드시 같은 규칙을 써야 CID 매칭이 된다. 그래서 로직을 여기 한 곳에 둔다.
의존성 없는 순수 함수라 테스트하기도 쉽다.

규칙:
  - 숫자만 남긴다.
  - 국제표기 국가코드(+82 / 82 / 0082, 한국)를 국내 0 형태로 바꾼다.
  - min_len 미만이면 None (저장 측은 기본 8자리, 조회 측은 1로 완화).
"""
import re


def normalize(num, min_len=8):
    if not num:
        return None
    d = re.sub(r"\D", "", num)
    # 국제표기 → 국내 0
    #   00 82 xxxx (00 = 국제전화 접속번호) 를 +82 보다 먼저 처리
    if d.startswith("0082"):
        d = "0" + d[4:]
    elif d.startswith("82"):
        d = "0" + d[2:]
    if not d or len(d) < min_len:
        return None
    return d

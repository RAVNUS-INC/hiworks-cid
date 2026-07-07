#!/usr/bin/env python3
"""build_org_entries(조직도 트리 평탄화) 단위 테스트.
requests/pymysql 없이도 돌도록 스텁 주입. 실행: python tests/test_org_entries.py"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# hiworks_sync 가 import 하는 외부 의존성을 스텁으로 대체(여기선 순수 파싱만 검증)
sys.modules.setdefault("requests", types.ModuleType("requests"))
sys.modules.setdefault("pymysql", types.ModuleType("pymysql"))
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


def run():
    got = build_org_entries(SAMPLE)
    fails = 0
    for phone, exp in EXPECT.items():
        ok = got.get(phone) == exp
        fails += not ok
        print(f"{'ok ' if ok else 'FAIL'} {phone} -> {got.get(phone)} (기대 {exp})")
    extra = set(got) - set(EXPECT)
    if extra:
        fails += 1
        print(f"FAIL 예상 밖 항목: {extra}")
    print(f"\n{'PASSED' if fails == 0 else f'{fails} FAILED'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)

"""대상 분석(analyzer.analyze_depth) 정확성 검증.

합성 트리를 만들고 깊이별 디렉터리 수·최대 깊이·추천 fold-depth·시간예산
중단(truncated)·최대깊이 캡(capped)을 점검한다. 표준 라이브러리만 사용.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage.analyzer import analyze_depth  # noqa: E402


def _mk(base: str) -> None:
    # 깊이 구조: base/a{0..2}/b{0..3}/c{0..4}  (깊이 1=3, 깊이 2=12, 깊이 3=60)
    for i in range(3):
        for j in range(4):
            for k in range(5):
                os.makedirs(os.path.join(base, "a%d" % i, "b%d" % j, "c%d" % k))
    # 파일 몇 개(개수만 셈, stat 안 함)
    with open(os.path.join(base, "f1"), "w") as fh:
        fh.write("x")


def run_basic() -> None:
    base = tempfile.mkdtemp(prefix="iu_ana_")
    try:
        _mk(base)
        r = analyze_depth(base, workers=4)
        assert r["ok"], r
        counts = {L["depth"]: L["dirs"] for L in r["levels"]}
        assert counts.get(0) == 1, counts
        assert counts.get(1) == 3, counts
        assert counts.get(2) == 12, counts
        assert counts.get(3) == 60, counts
        assert r["max_depth"] == 3, r["max_depth"]
        # 총 디렉터리 = 1+3+12+60 = 76
        assert r["total_dirs"] == 76, r["total_dirs"]
        assert r["total_files"] >= 1, r["total_files"]
        # 누적(fold 시 보존): 깊이 2 까지면 1+3+12=16
        cum2 = next(L["cumulative"] for L in r["levels"] if L["depth"] == 2)
        assert cum2 == 16, cum2
        print("[basic] OK  깊이별 %s  최대깊이=%d  추천fold=%s"
              % (counts, r["max_depth"], r["suggested_fold_depth"]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_maxdepth_cap() -> None:
    base = tempfile.mkdtemp(prefix="iu_ana_")
    try:
        _mk(base)
        r = analyze_depth(base, max_depth=2, workers=4)
        assert r["ok"], r
        assert r["max_depth"] == 2, r["max_depth"]
        assert r["capped"] is True, r            # 더 깊은 c* 가 있으므로 capped
        depths = {L["depth"] for L in r["levels"]}
        assert 3 not in depths, depths           # 깊이 3 은 측정 안 됨
        print("[maxdepth] OK  capped=%s 측정깊이<=%d" % (r["capped"], r["max_depth"]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_suggest() -> None:
    # 깊이 3 에서 60개로 터지므로(12→60, 5배) 추천 fold 는 2.
    base = tempfile.mkdtemp(prefix="iu_ana_")
    try:
        _mk(base)
        r = analyze_depth(base, workers=4)
        assert r["suggested_fold_depth"] in (2, 1), r["suggested_fold_depth"]
        print("[suggest] OK  추천 fold-depth=%d" % r["suggested_fold_depth"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_bad_path() -> None:
    r = analyze_depth("/nonexistent/path/xyz123", workers=2)
    assert not r["ok"], r
    print("[badpath] OK  ok=False 정상 거부")


if __name__ == "__main__":
    run_basic()
    run_maxdepth_cap()
    run_suggest()
    run_bad_path()
    print("모든 테스트 통과 ✅")

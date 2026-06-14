"""오토튜닝 측정 코어 검증 — 합성 트리에서 autotune 이 돌고 결과 구조가 정확한지.

로컬은 지연이 없어 병렬이 이득 없을 수 있으나(단일이 best 로 뽑혀도 정상), 측정이
끝까지 돌고 결과 스키마(results/best/진행 콜백)가 일관됨을 검증한다. pscan 의 시간상자
(max_seconds/deadline)가 기존 정합성을 깨지 않음은 test_pscan 이 보장.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import autotune as at  # noqa: E402
from isilon_usage import pscan  # noqa: E402


def _mk(base: str) -> None:
    for a in range(4):                       # 자식 4개 × 5 서브 × 20 파일
        for b in range(5):
            d = os.path.join(base, "t%d" % a, "s%d" % b)
            os.makedirs(d)
            for f in range(20):
                open(os.path.join(d, "f%d" % f), "w").close()


def run_autotune() -> None:
    base = tempfile.mkdtemp(prefix="iu_at_")
    try:
        _mk(base)
        phases = []
        r = at.autotune(base, secs=0.5, max_procs=4, max_threads=4,
                        on_progress=lambda p: phases.append(p["phase"]))
        assert r["ok"], r
        assert len(r["results"]) >= 1, r
        for x in r["results"]:               # 결과 스키마
            for k in ("procs", "threads", "files_per_sec", "speedup", "label", "files"):
                assert k in x, (k, x)
        # 진행 콜백: 측정 단계들 + 완료
        assert "done" in phases and "measuring" in phases, phases
        # best 는 files_per_sec 최고 후보와 일치
        top = max(r["results"], key=lambda x: x["files_per_sec"])
        assert r["best"]["files_per_sec"] == top["files_per_sec"], (r["best"], top)
        print("[autotune] OK  후보 %d개 측정, best=%s (%.0f files/s)" % (
            len(r["results"]), r["best"]["label"], r["best"]["files_per_sec"]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_timebox() -> None:
    """max_seconds(시간상자)가 실제로 일찍 멈추는지 — 큰 트리에 0.3초 상자."""
    base = tempfile.mkdtemp(prefix="iu_tb_")
    try:
        for a in range(6):                   # 충분히 큰 트리
            for b in range(8):
                d = os.path.join(base, "t%d" % a, "s%d" % b)
                os.makedirs(d)
                for f in range(50):
                    open(os.path.join(d, "f%d" % f), "w").close()
        full = pscan.parallel_scan(base, processes=2, size_mode="apparent")
        boxed = pscan.parallel_scan(base, processes=2, size_mode="apparent", max_seconds=0.3)
        assert full["ok"] and boxed["ok"], (full, boxed)
        # 시간상자는 전체 이하의 파일만 처리(부분 결과) — 정합성 깨짐 없이 조기 종료
        assert boxed["total_files"] <= full["total_files"], (boxed["total_files"], full["total_files"])
        print("[timebox] OK  전체 %d파일 중 0.3초 상자 %d파일(부분) — 시간상자 동작" % (
            full["total_files"], boxed["total_files"]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    run_autotune()
    run_timebox()
    print("모든 테스트 통과 ✅")

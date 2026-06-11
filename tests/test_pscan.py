"""pscan(멀티프로세스 병렬 스캔 PoC) 정확성 검증.

합성 트리를 만들고 parallel_scan 결과(용량/파일/디렉터리 수)가 os.walk 레퍼런스와
일치하는지, 프로세스 수가 달라도 같은 결과인지, node_mounts 치환이 정확한지 본다.
표준 라이브러리만 사용.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import pscan  # noqa: E402


def _mk(base: str) -> None:
    # 5 최상위 × 4 서브 × 6 파일(각 100바이트)
    for a in range(5):
        for b in range(4):
            d = os.path.join(base, "t%d" % a, "s%d" % b)
            os.makedirs(d)
            for f in range(6):
                with open(os.path.join(d, "f%d" % f), "w") as fh:
                    fh.write("x" * 100)
    with open(os.path.join(base, "rootfile"), "w") as fh:    # 루트 직속 파일
        fh.write("y" * 50)


def _reference(root: str):
    tb = tf = td = 0
    for dp, _dns, fns in os.walk(root):
        td += 1
        try:
            tb += os.stat(dp, follow_symlinks=False).st_size
        except OSError:
            pass
        for fn in fns:
            try:
                st = os.stat(os.path.join(dp, fn), follow_symlinks=False)
                tb += st.st_size
                tf += 1
            except OSError:
                pass
    return tb, tf, td


def run_correctness() -> None:
    base = tempfile.mkdtemp(prefix="iu_ps_")
    try:
        _mk(base)
        rb, rf, rd = _reference(base)
        for p in (1, 2, 4, 8):
            r = pscan.parallel_scan(base, processes=p, size_mode="apparent")
            assert r["ok"], r
            assert r["total_bytes"] == rb, (p, r["total_bytes"], rb)
            assert r["total_files"] == rf, (p, r["total_files"], rf)
            assert r["total_dirs"] == rd, (p, r["total_dirs"], rd)
        # 루트 직속 파일(1개)이 합산됐는지: 파일 = 5*4*6 + 1 = 121
        assert rf == 121, rf
        print("[correctness] OK  procs 1/2/4/8 모두 os.walk 와 일치 (files=%d dirs=%d)" % (rf, rd))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_node_mounts() -> None:
    # node_mounts 로 같은 base 를 가리키면(라운드로빈) 결과는 단일과 동일해야 한다.
    base = tempfile.mkdtemp(prefix="iu_ps_")
    try:
        _mk(base)
        rb, rf, rd = _reference(base)
        r = pscan.parallel_scan(base, processes=4, size_mode="apparent",
                                node_mounts=[base, base])
        assert r["ok"] and r["total_files"] == rf and r["total_dirs"] == rd, r
        # 라벨(per_top.path)은 표준 base 경로를 유지해야 함
        assert all(t["path"].startswith(base) for t in r["per_top"]), r["per_top"][:2]
        print("[node_mounts] OK  치환 경로로 스캔해도 합계 일치, 라벨은 표준 경로 유지")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_bad_path() -> None:
    r = pscan.parallel_scan("/no/such/dir/xyz", processes=2)
    assert not r["ok"], r
    # 빈 디렉터리: 디렉터리 1개, 파일 0
    empty = tempfile.mkdtemp(prefix="iu_ps_")
    try:
        r2 = pscan.parallel_scan(empty, processes=4)
        assert r2["ok"] and r2["total_dirs"] == 1 and r2["total_files"] == 0, r2
    finally:
        shutil.rmtree(empty, ignore_errors=True)
    print("[edge] OK  없는 경로 거부 · 빈 디렉터리 처리")


if __name__ == "__main__":
    run_correctness()
    run_node_mounts()
    run_bad_path()
    print("모든 테스트 통과 ✅")

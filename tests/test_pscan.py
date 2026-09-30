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


def run_threads_per_proc() -> None:
    """프로세스당 스레드(2단 병렬)가 켜져도 합계가 직렬과 정확히 일치해야 한다."""
    base = tempfile.mkdtemp(prefix="iu_ps_")
    try:
        _mk(base)
        rb, rf, rd = _reference(base)
        # _scan_subtree_threaded 직접: 한 서브트리 합계가 직렬 scan_subtree 와 동일
        sub = os.path.join(base, "t0")
        s_serial = pscan.scan_subtree(sub, "apparent", 0, 1)
        s_thread = pscan.scan_subtree(sub, "apparent", 0, 8)
        assert s_serial[1:] == s_thread[1:], (s_serial, s_thread)
        # parallel_scan: tpp 1/4/8 모두 레퍼런스와 일치(프로세스 수와 무관)
        for p in (1, 4):
            for tpp in (1, 4, 8):
                r = pscan.parallel_scan(base, processes=p, size_mode="apparent",
                                        threads_per_proc=tpp)
                assert r["ok"] and r["threads_per_proc"] == tpp, r
                assert (r["total_bytes"], r["total_files"], r["total_dirs"]) == (rb, rf, rd), \
                    (p, tpp, r["total_bytes"], r["total_files"], r["total_dirs"])
        print("[threads_per_proc] OK  2단 병렬(프로세스×스레드)도 합계 직렬과 동일")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_adaptive_split() -> None:
    """적응형 깊이 분할: 1단계 자식이 적어도(2개) 펼쳐 병렬화하고, 합계·per_top 정확.

    user(자식 16개) 같은 영역에서 병렬 단위가 부족해 효율이 떨어지던 문제의 회귀 가드.
    펼친 부모(shallow)가 직속 파일을 빠짐없이 세고, per_top 은 1단계 자식 기준을 유지해야.
    """
    base = tempfile.mkdtemp(prefix="iu_ps_")
    try:
        for a in range(2):                       # root 자식 2개(자식 적은 영역 모사)
            for b in range(10):
                for c in range(3):
                    d = os.path.join(base, "top%d" % a, "m%d" % b, "l%d" % c)
                    os.makedirs(d)
                    for f in range(4):
                        with open(os.path.join(d, "f%d" % f), "w") as fh:
                            fh.write("z" * 80)
            with open(os.path.join(base, "top%d" % a, "topfile"), "w") as fh:
                fh.write("t" * 30)               # top 직속 파일(shallow 부모가 세는지)
        rb, rf, rd = _reference(base)
        # 펼침이 실제로 일어나는지: 자식 2개 → 단위가 그보다 많아야
        units = pscan._split_units(
            [os.path.join(base, "top0"), os.path.join(base, "top1")], 8)
        assert len(units) > 2, len(units)
        for p in (1, 4, 8):                       # 합계가 프로세스 수와 무관하게 정확
            r = pscan.parallel_scan(base, processes=p, size_mode="apparent")
            assert r["ok"], r
            assert (r["total_bytes"], r["total_files"], r["total_dirs"]) == (rb, rf, rd), \
                (p, r["total_bytes"], r["total_files"], r["total_dirs"], (rb, rf, rd))
            # per_top 은 펼쳤어도 1단계 자식(2개) 기준 유지(드릴다운 호환)
            assert len(r["per_top"]) == 2, [t["path"] for t in r["per_top"]]
            assert all(os.path.basename(t["path"]).startswith("top")
                       for t in r["per_top"]), r["per_top"]
        print("[adaptive_split] OK  자식 2개도 펼쳐 병렬화(units=%d), 합계·per_top 정확"
              % len(units))
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


def run_write_db() -> None:
    """parallel_scan 결과를 표준 per-run DB(루트+1단계)로 써서 되읽기 검증."""
    from isilon_usage import db as dbmod
    base = tempfile.mkdtemp(prefix="iu_ps_")
    dbf = os.path.join(base, "run.db")
    try:
        _mk(base)
        r = pscan.parallel_scan(base, processes=4, size_mode="apparent")
        assert r["ok"], r
        rid = pscan.write_run_db(dbf, base, r, size_mode="apparent")
        assert rid >= 1
        conn = dbmod.connect(dbf)
        try:
            run = conn.execute("SELECT * FROM scan_runs WHERE id=?", (rid,)).fetchone()
            assert run["status"] == "done" and run["backend"] == "pscan"
            assert run["scanned_bytes"] == r["total_bytes"], (run["scanned_bytes"], r["total_bytes"])
            assert run["total_files"] == r["total_files"]
            root = conn.execute(
                "SELECT * FROM directories WHERE run_id=? AND parent_id IS NULL", (rid,)).fetchone()
            assert root["depth"] == 0 and root["total_bytes"] == r["total_bytes"], dict(root)
            kids = conn.execute(
                "SELECT * FROM directories WHERE run_id=? AND depth=1", (rid,)).fetchall()
            assert len(kids) == 5, len(kids)   # t0..t4 (rootfile 은 파일이라 제외)
            assert sum(k["total_bytes"] for k in kids) + root["own_bytes"] == r["total_bytes"]
        finally:
            conn.close()
        # run_id 갱신 모드: 미리 만든 run(sizing)을 갱신 — 중복 run 없이 같은 id 반환(자원 패널용)
        dbf2 = os.path.join(base, "run2.db")
        dbmod.init_db(dbf2)
        c2 = dbmod.connect(dbf2)
        c2.execute("INSERT INTO scan_runs (root_path,status,phase,backend,size_mode,"
                   "started_at,updated_at,scanner_pid,hostname,app_version) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (base, "sizing", "sizing", "pscan", "apparent", 0.0, 0.0, 1, "h", "v"))
        pre = c2.execute("SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        c2.commit(); c2.close()
        got = pscan.write_run_db(dbf2, base, r, size_mode="apparent", run_id=pre)
        assert got == pre, (got, pre)
        c2 = dbmod.connect(dbf2)
        try:
            nruns = c2.execute("SELECT COUNT(*) AS n FROM scan_runs").fetchone()["n"]
            st = c2.execute("SELECT status FROM scan_runs WHERE id=?", (pre,)).fetchone()["status"]
            assert nruns == 1 and st == "done", (nruns, st)
        finally:
            c2.close()
        print("[write_db] OK  pscan 결과를 per-run DB(루트+1단계)로 기록·되읽기 + run_id 갱신 검증")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    run_correctness()
    run_threads_per_proc()
    run_adaptive_split()
    run_node_mounts()
    run_bad_path()
    run_write_db()
    print("모든 테스트 통과 ✅")

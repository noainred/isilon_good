"""스캐너 정확성 검증.

합성 트리를 만들고 native 백엔드로 스캔한 뒤, 루트의 재귀 용량/파일 수가
파이썬 os.walk 로 직접 계산한 값과 일치하는지 확인한다. du 백엔드가 가능한
환경이면 native 와 du 결과가 일치하는지도 점검한다.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import db as dbmod  # noqa: E402
from isilon_usage.scanner import Scanner, BLOCK_UNIT, run_scan  # noqa: E402


def _make_tree(root: str) -> None:
    # 루트
    os.makedirs(root, exist_ok=True)
    _write(os.path.join(root, "a.bin"), 1000)
    _write(os.path.join(root, "b.bin"), 2000)
    # 하위 1
    d1 = os.path.join(root, "sub1")
    os.makedirs(d1, exist_ok=True)
    _write(os.path.join(d1, "c.bin"), 3000)
    # 하위 1-1 (가장 깊은 레벨)
    d11 = os.path.join(d1, "deep")
    os.makedirs(d11, exist_ok=True)
    _write(os.path.join(d11, "d.bin"), 4000)
    _write(os.path.join(d11, "e.bin"), 500)
    # 하위 2 (빈 디렉터리 + 파일 1개)
    d2 = os.path.join(root, "sub2")
    os.makedirs(d2, exist_ok=True)
    _write(os.path.join(d2, "f.bin"), 100)
    # 하드링크: sub1/c.bin 을 sub2/c_link.bin 에 하드링크(용량은 1번만 세야 함)
    try:
        os.link(os.path.join(d1, "c.bin"), os.path.join(d2, "c_link.bin"))
    except OSError:
        pass


def _write(path: str, size: int) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


def _expected_disk_bytes(root: str) -> tuple[int, int]:
    """os.walk 로 (총 디스크바이트, 총 파일수) 계산.

    du 와 동일하게 하드링크(st_nlink>1)는 (st_dev,st_ino)로 1회만 용량 계산.
    파일 수는 디렉터리 엔트리(이름) 기준으로 모두 센다.
    """
    total = 0
    files = 0
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root):
        st = os.stat(dirpath)
        total += st.st_blocks * BLOCK_UNIT
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                fst = os.lstat(fp)
            except OSError:
                continue
            files += 1
            if fst.st_nlink > 1:
                key = (fst.st_dev, fst.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += fst.st_blocks * BLOCK_UNIT
    return total, files


def run_case(backend: str, workers: int = 1) -> None:
    tmp = tempfile.mkdtemp(prefix="isilon_test_")
    try:
        root = os.path.join(tmp, "data")
        _make_tree(root)
        db_path = os.path.join(tmp, "scan.db")
        dbmod.init_db(db_path)

        scanner = Scanner(db_path, root, backend=backend, batch_size=2, workers=workers)
        run_id = scanner.run()

        conn = dbmod.connect(db_path)
        try:
            run = dbmod.get_run(conn, run_id)
            assert run["status"] == "done", run["status"]

            root_row = conn.execute(
                "SELECT total_bytes, total_files FROM directories "
                "WHERE run_id=? AND parent_id IS NULL", (run_id,)
            ).fetchone()

            exp_bytes, exp_files = _expected_disk_bytes(root)
            assert root_row["total_files"] == exp_files, \
                f"파일수 불일치: {root_row['total_files']} != {exp_files}"

            # native 는 정확히 일치해야 한다.
            if backend == "native":
                assert root_row["total_bytes"] == exp_bytes, \
                    f"용량 불일치: {root_row['total_bytes']} != {exp_bytes}"
            else:
                # du 는 마운트/구현에 따라 약간의 오차가 있을 수 있어 근사 비교
                diff = abs(root_row["total_bytes"] - exp_bytes)
                assert diff <= exp_bytes * 0.05 + 8192, \
                    f"du 용량 오차 큼: {root_row['total_bytes']} vs {exp_bytes}"

            # 디렉터리 개수: 루트 포함 5개(root, sub1, sub1/deep, sub2)
            ndirs = conn.execute(
                "SELECT COUNT(*) AS c FROM directories WHERE run_id=?", (run_id,)
            ).fetchone()["c"]
            assert ndirs == 4, f"디렉터리 수 {ndirs} != 4"

            # 모든 디렉터리가 done
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM directories WHERE run_id=? AND status!='done'",
                (run_id,)
            ).fetchone()["c"]
            assert pending == 0, f"미완료 디렉터리 {pending}개"

            print(f"[{backend} workers={workers}] OK  total_bytes={root_row['total_bytes']} "
                  f"files={root_row['total_files']} dirs={ndirs}")
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_hardlink_concurrency() -> None:
    """하드링크가 여러 디렉터리에 흩어져 있어도 dedup 이 워커 수와 무관하게 정확한지.

    직렬 구간 축소 리팩터(하드링크는 락 안에서 dedup)의 회귀 가드 — 워커 1 vs 8 의
    루트 합계가 서로 같고, os.walk dedup 레퍼런스와도 같아야 한다.
    """
    tmp = tempfile.mkdtemp(prefix="isilon_hl_")
    try:
        root = os.path.join(tmp, "data")
        src = os.path.join(root, "src")
        os.makedirs(src, exist_ok=True)
        originals = []
        for i in range(5):                       # 원본 5개(각 8KB)
            p = os.path.join(src, "orig%d.bin" % i)
            _write(p, 8000)
            originals.append(p)
        linked = 0
        for s in range(10):                      # 10개 디렉터리에 흩뿌려 하드링크
            d = os.path.join(root, "sub%d" % s)
            os.makedirs(d, exist_ok=True)
            for i, op in enumerate(originals):
                try:
                    os.link(op, os.path.join(d, "link%d.bin" % i))
                    linked += 1
                except OSError:
                    pass
            _write(os.path.join(d, "uniq%d.bin" % s), 1000)   # 디렉터리별 고유 파일
        if linked == 0:
            print("[hardlink-concurrency] 건너뜀 (os.link 미지원)")
            return
        exp_bytes, exp_files = _expected_disk_bytes(root)
        results = {}
        for w in (1, 8):
            db_path = os.path.join(tmp, "hl%d.db" % w)
            dbmod.init_db(db_path)
            rid = Scanner(db_path, root, backend="native", batch_size=1, workers=w).run()
            conn = dbmod.connect(db_path)
            try:
                rr = conn.execute(
                    "SELECT total_bytes, total_files FROM directories "
                    "WHERE run_id=? AND parent_id IS NULL", (rid,)).fetchone()
                results[w] = (rr["total_bytes"], rr["total_files"])
            finally:
                conn.close()
        assert results[1] == results[8], ("워커 수에 따라 결과 다름", results)
        assert results[8] == (exp_bytes, exp_files), (results[8], (exp_bytes, exp_files))
        print("[hardlink-concurrency] OK  워커 1/8 동일·dedup 정확 (bytes=%d files=%d)"
              % (exp_bytes, exp_files))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_permission_case() -> None:
    """접근 불가 디렉터리가 있어도 스캔이 멈추지 않고 오류로 집계되는지."""
    tmp = tempfile.mkdtemp(prefix="isilon_perm_")
    try:
        root = os.path.join(tmp, "data")
        os.makedirs(os.path.join(root, "ok"), exist_ok=True)
        _write(os.path.join(root, "ok", "f.bin"), 1000)
        denied = os.path.join(root, "denied")
        os.makedirs(denied, exist_ok=True)
        _write(os.path.join(denied, "secret.bin"), 1000)
        os.chmod(denied, 0o000)

        db_path = os.path.join(tmp, "scan.db")
        dbmod.init_db(db_path)
        run_id = Scanner(db_path, root, batch_size=2).run()

        conn = dbmod.connect(db_path)
        try:
            run = dbmod.get_run(conn, run_id)
            assert run["status"] == "done", run["status"]  # 멈추지 않고 완료
            err = run["error_dirs"]
            if os.geteuid() == 0:
                print(f"[permission] OK (root — 권한 우회, error_dirs={err}, 크래시 없음)")
            else:
                assert err >= 1, f"오류 디렉터리가 잡히지 않음 (error_dirs={err})"
                print(f"[permission] OK  error_dirs={err}")
        finally:
            conn.close()
            os.chmod(denied, 0o755)  # 정리 위해 권한 복구
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_fold_case(workers: int = 2) -> None:
    """깊이 접기(fold_depth): N까지만 행 저장하되 합계는 무제한과 '정확히 동일'.

    또 DB 크기 가드(db_max_bytes)가 자동 일시정지하는지도 확인한다.
    """
    tmp = tempfile.mkdtemp(prefix="isilon_fold_")
    try:
        root = os.path.join(tmp, "data")
        _make_tree(root)   # root(0) → sub1(1) → deep(2), sub2(1)
        exp_bytes, exp_files = _expected_disk_bytes(root)

        # fold_depth=1: 행은 깊이 0·1 만, deep(2)은 sub1 own_bytes 로 접힘
        db_path = os.path.join(tmp, "fold.db")
        dbmod.init_db(db_path)
        sc = Scanner(db_path, root, backend="native", batch_size=2,
                     workers=workers, fold_depth=1)
        run_id = sc.run()
        conn = dbmod.connect(db_path)
        try:
            run = dbmod.get_run(conn, run_id)
            assert run["status"] == "done", run["status"]
            rr = conn.execute(
                "SELECT total_bytes, total_files FROM directories "
                "WHERE run_id=? AND parent_id IS NULL", (run_id,)).fetchone()
            # 핵심: 접어도 합계는 무제한(정확값)과 같아야 한다
            assert rr["total_bytes"] == exp_bytes, \
                "접기 합계 불일치: %d != %d" % (rr["total_bytes"], exp_bytes)
            assert rr["total_files"] == exp_files, \
                "접기 파일수 불일치: %d != %d" % (rr["total_files"], exp_files)
            ndirs = conn.execute(
                "SELECT COUNT(*) c FROM directories WHERE run_id=?", (run_id,)).fetchone()["c"]
            maxd = conn.execute(
                "SELECT COALESCE(MAX(depth),0) d FROM directories WHERE run_id=?",
                (run_id,)).fetchone()["d"]
            assert ndirs == 3, "접기 후 디렉터리 수 %d != 3(root,sub1,sub2)" % ndirs
            assert maxd == 1, "접기 후 최대 깊이 %d != 1" % maxd
            print("[fold] OK  합계=%d(무제한과 동일) 파일=%d 행=%d 깊이=%d"
                  % (rr["total_bytes"], rr["total_files"], ndirs, maxd))
        finally:
            conn.close()

        # DB 크기 가드: 1바이트 한도 → 자동 일시정지 + 사유 기록
        g_path = os.path.join(tmp, "guard.db")
        dbmod.init_db(g_path)
        scg = Scanner(g_path, root, backend="native", batch_size=2,
                      workers=workers, db_max_bytes=1)
        gid = scg.run()
        gconn = dbmod.connect(g_path)
        try:
            grun = dbmod.get_run(gconn, gid)
            assert grun["status"] == "paused", grun["status"]
            assert grun["error"] and "DB 크기 초과" in grun["error"], grun["error"]
            print("[db-guard] OK  자동 일시정지: %s" % grun["error"])
        finally:
            gconn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_stop_resume_case() -> None:
    """중단(stop)된 스캔을 재개하면 데이터 누락 없이 완성되는지(R7 회귀).

    가짜 stop_event(is_set 이 N번 호출 뒤 True)로 탐색 도중 멈춘 뒤 resume=True 로
    이어서 돌려, 루트 재귀 용량/파일 수가 '전체 스캔'과 정확히 같은지 확인한다.
    (중단된 디렉터리를 완료 처리하면 미방문분이 영구 누락되던 버그 방지 검증.)
    """
    d = tempfile.mkdtemp(prefix="iu_stopresume_")
    try:
        tree = os.path.join(d, "tree")
        for a in range(6):
            for b in range(6):
                p = os.path.join(tree, "d%d" % a, "s%d" % b)
                os.makedirs(p)
                for f in range(5):
                    with open(os.path.join(p, "f%d.bin" % f), "wb") as fh:
                        fh.write(b"x" * 1000)

        def _root_total(dbp):
            c = dbmod.connect(dbp)
            try:
                r = c.execute("SELECT total_bytes, total_files FROM directories "
                              "WHERE depth=0").fetchone()
                return (r["total_bytes"], r["total_files"]) if r else (None, None)
            finally:
                c.close()

        db_full = os.path.join(d, "full.db")
        run_scan(db_full, tree, workers=1, with_monitor=False)
        exp_b, exp_f = _root_total(db_full)
        assert exp_b and exp_f, ("전체 스캔 총량 이상", exp_b, exp_f)

        class TripAfter:                      # is_set() 이 n번 뒤 True — 탐색 중간 결정적 중단
            def __init__(self, n): self.n = n; self.c = 0
            def is_set(self): self.c += 1; return self.c > self.n
            def set(self): self.n = -1
            def clear(self): self.n = 10 ** 9
            def wait(self, t=None): return False

        db_r = os.path.join(d, "resume.db")
        run_scan(db_r, tree, workers=1, with_monitor=False, stop_event=TripAfter(40))
        run_scan(db_r, tree, workers=1, with_monitor=False, resume=True)  # 재개 → 완성
        got_b, got_f = _root_total(db_r)
        assert (got_b, got_f) == (exp_b, exp_f), \
            ("중단→재개 후 누락!", (got_b, got_f), "expected", (exp_b, exp_f))
        print("[stop-resume] OK  중단→재개 후 누락 없음 (total_bytes=%d files=%d)" % (exp_b, exp_f))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_sizing_window_case() -> None:
    """집계 '레벨별 일괄 UPDATE(id 윈도우)' 회귀 가드.

    행 루프 → 일괄 UPDATE 교체(1.99.25) 후에도 ① 윈도우 경계와 무관하게 모든
    디렉터리의 재귀 합이 os.walk 참조와 정확히 같고, ② 집계 도중 중단돼도
    status='done' 규약으로 재개·완주하고, ③ scan_log 진단 로그가 남아야 한다.
    """
    from isilon_usage import scanner as scmod
    d = tempfile.mkdtemp(prefix="iu_aggwin_")
    old_window = scmod.AGG_WINDOW
    try:
        tree = os.path.join(d, "tree")
        for a in range(4):                     # 3레벨 + 빈/중간 디렉터리 혼합
            for b in range(3):
                p = os.path.join(tree, "d%d" % a, "s%d" % b)
                os.makedirs(p)
                for f in range(b + 1):
                    _write(os.path.join(p, "f%d.bin" % f), 700 * (f + 1) + a)
            os.makedirs(os.path.join(tree, "d%d" % a, "empty"))
        _write(os.path.join(tree, "r.bin"), 1234)

        def _blocks(st):
            # 스캐너 _entry_bytes(disk 모드)와 동일 규칙: st_blocks 미지원이면 st_size
            b = getattr(st, "st_blocks", None)
            return st.st_size if b is None else b * BLOCK_UNIT

        def _ref_recursive(root):
            """디렉터리별 (재귀 disk bytes, 재귀 파일 수) — 디렉터리 자체 블록 포함."""
            ref = {}
            for cur, dirs, files in os.walk(root):
                b = _blocks(os.stat(cur))
                for f in files:
                    b += _blocks(os.lstat(os.path.join(cur, f)))
                ref[os.path.abspath(cur)] = [b, len(files)]
            for p in sorted(ref, key=lambda x: -x.count(os.sep)):
                parent = os.path.dirname(p)
                if parent != p and parent in ref:
                    ref[parent][0] += ref[p][0]
                    ref[parent][1] += ref[p][1]
            return {p: tuple(v) for p, v in ref.items()}

        def _check_all(dbp, ref, tag):
            c = dbmod.connect(dbp)
            try:
                rid = c.execute("SELECT MAX(id) m FROM scan_runs").fetchone()["m"]
                bad = []
                for r in c.execute("SELECT path, status, total_bytes, total_files "
                                   "FROM directories WHERE run_id=?", (rid,)):
                    assert r["status"] == "done", (tag, r["path"], r["status"])
                    got = (r["total_bytes"], r["total_files"])
                    if got != ref[os.path.abspath(r["path"])]:
                        bad.append((r["path"], got, ref[os.path.abspath(r["path"])]))
                assert not bad, (tag, "불일치", bad[:5])
                logs = [r["message"] for r in c.execute(
                    "SELECT message FROM scan_log WHERE run_id=?", (rid,))]
                assert any("집계 시작" in m for m in logs), (tag, logs)
                assert any(m.startswith("집계 depth") for m in logs), (tag, logs)
                return rid
            finally:
                c.close()

        ref = _ref_recursive(tree)
        scmod.AGG_WINDOW = 3   # 레벨당 여러 윈도우/커밋 경계를 강제
        dbp = os.path.join(d, "win.db")
        run_scan(dbp, tree, workers=2, with_monitor=False)
        rid = _check_all(dbp, ref, "윈도우=3")

        # 집계 도중 중단 흉내: 가장 깊은 레벨 절반 + 상위 전 레벨을 미완료로 되돌림
        c = dbmod.connect(dbp)
        maxd = c.execute("SELECT MAX(depth) m FROM directories WHERE run_id=?",
                         (rid,)).fetchone()["m"]
        deep = [r["id"] for r in c.execute(
            "SELECT id FROM directories WHERE run_id=? AND depth=? ORDER BY id",
            (rid, maxd))]
        half = deep[: max(1, len(deep) // 2)]
        c.execute("UPDATE directories SET status='discovered', total_bytes=0, "
                  "total_files=0 WHERE id IN (%s)" % ",".join("?" * len(half)), half)
        c.execute("UPDATE directories SET status='discovered', total_bytes=0, "
                  "total_files=0 WHERE run_id=? AND depth<?", (rid, maxd))
        c.execute("UPDATE scan_runs SET status='sizing', phase='sizing', "
                  "finished_at=NULL WHERE id=?", (rid,))
        c.commit()
        c.close()
        run_scan(dbp, tree, workers=1, with_monitor=False, resume=True)
        _check_all(dbp, ref, "집계 중단→재개")
        print("[sizing-window] OK  윈도우 분할·중단 재개 모두 참조값 일치 (%d 디렉터리)"
              % len(ref))
    finally:
        scmod.AGG_WINDOW = old_window
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    run_stop_resume_case()
    run_sizing_window_case()
    run_case("native")
    run_case("native", workers=4)   # stat 동시 처리해도 결과 동일해야 함
    if shutil.which("du"):
        run_case("du")
    else:
        print("[du] 건너뜀 (du 명령 없음)")
    run_fold_case()
    run_hardlink_concurrency()
    run_permission_case()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

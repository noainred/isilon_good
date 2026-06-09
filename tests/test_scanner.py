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
from isilon_usage.scanner import Scanner, BLOCK_UNIT  # noqa: E402


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


def main() -> int:
    run_case("native")
    run_case("native", workers=4)   # stat 동시 처리해도 결과 동일해야 함
    if shutil.which("du"):
        run_case("du")
    else:
        print("[du] 건너뜀 (du 명령 없음)")
    run_fold_case()
    run_permission_case()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

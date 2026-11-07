"""1.99.40 회귀 테스트 — 버그 리뷰(2026-10-27)에서 재현된 5건.

1) UTF-8 이 아닌(예: EUC-KR) 이름 하나로 스캔이 영구 실패하던 문제
2) NAS 마운트가 빠진 채 재개/진행되면 거의 0 인 합계가 '완료'로 굳던 문제
3) 한글(비 ASCII) 비밀번호·토큰 비교가 예외(→ 500)로 로그인 불가가 되던 문제
4) 로그인 없는 재개 요청으로 완료된 스캔(pscan 은 결과 DB 삭제)을 다시 돌리던 문제
5) 예약 스캔이 비정상 종료 후 'paused' 로 남으면 그 예약이 영구 정지하던 문제
각 검사는 수정 전 코드에서 실패함을 확인했다.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import db as dbmod  # noqa: E402
from isilon_usage import manager as mgrmod  # noqa: E402
from isilon_usage.scanner import Scanner, run_scan  # noqa: E402


class _TripAfter:
    """is_set() 이 n번 호출된 뒤 True — 결정적 중단."""

    def __init__(self, n):
        self.n = n
        self.c = 0

    def is_set(self):
        self.c += 1
        return self.c > self.n

    def set(self):
        self.n = -1

    def clear(self):
        self.n = 10 ** 9

    def wait(self, t=None):
        return False


def _ref_totals(root: bytes):
    """os.walk(바이트 경로) 기준 정답 — st_blocks*512(disk 모드), 디렉터리 자기 inode 포함."""
    tb = tf = 0
    for dp, _dn, fns in os.walk(root):
        tb += os.lstat(dp).st_blocks * 512
        for f in fns:
            tb += os.lstat(os.path.join(dp, f)).st_blocks * 512
            tf += 1
    return tb, tf


def _root(db):
    c = dbmod.connect(db)
    try:
        r = c.execute("SELECT status, total_bytes, total_files FROM directories WHERE depth=0").fetchone()
        run = c.execute("SELECT status, error, error_dirs FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        return (r["total_bytes"], r["total_files"]), dict(run)
    finally:
        c.close()


def check_non_utf8_names() -> None:
    d = tempfile.mkdtemp(prefix="iu_r1_")
    try:
        tree = os.path.join(d, "tree")
        os.makedirs(os.path.join(tree, "정상폴더"))
        with open(os.path.join(tree, "정상폴더", "a.bin"), "wb") as fh:
            fh.write(b"x" * 1000)
        bt = os.fsencode(tree)
        bad_dir = os.path.join(bt, "보고서".encode("euc-kr"))           # EUC-KR 폴더
        os.makedirs(os.path.join(bad_dir, "하위".encode("euc-kr")))
        with open(os.path.join(bad_dir, b"b.bin"), "wb") as fh:
            fh.write(b"y" * 1000)
        big = os.path.join(bad_dir, "큰파일".encode("euc-kr") + b"." + "확장".encode("euc-kr"))
        with open(big, "wb") as fh:                                       # 최대 파일 + 비 UTF-8 확장자
            fh.write(b"z" * 300000)
        exp = _ref_totals(bt)
        for fold in (0, 1):
            db = os.path.join(d, "s%d.db" % fold)
            run_scan(db, tree, workers=2, with_monitor=False, fold_depth=fold)
            got, run = _root(db)
            assert run["status"] == "done", (fold, run)
            assert got == exp, (fold, got, exp)
            c = dbmod.connect(db)
            try:
                tops = [r[0] for r in c.execute("SELECT path FROM top_files ORDER BY bytes DESC")]
                nsize = c.execute("SELECT COUNT(*) FROM scan_stats WHERE kind='size'").fetchone()[0]
                paths = [r[0] for r in c.execute("SELECT path FROM directories WHERE depth>0")]
            finally:
                c.close()
            assert tops and dbmod.fs_text(tops[0]) == os.fsdecode(big), (fold, tops[:1])
            assert nsize >= 1, "크기 통계가 저장되지 않음"
            for p in paths:                                               # DB 경로 → 실제 경로로 되돌릴 수 있어야
                assert os.path.isdir(dbmod.fs_text(p)), p
        # 중단 → 재개(비 UTF-8 경로 행이 프론티어에 남은 채로)
        db = os.path.join(d, "r.db")
        run_scan(db, tree, workers=1, with_monitor=False, stop_event=_TripAfter(15))
        run_scan(db, tree, workers=1, with_monitor=False, resume=True)
        got, run = _root(db)
        assert run["status"] == "done" and got == exp, (run, got, exp)
        # pscan 결과 기록(1단계 자식 이름이 비 UTF-8)
        from isilon_usage import pscan
        res = pscan.parallel_scan(tree, processes=2)
        rid = pscan.write_run_db(os.path.join(d, "p.db"), tree, res)
        assert rid >= 1
        print("[non-utf8] OK  EUC-KR 이름 폴더·파일(fold 0/1·재개·pscan 기록) 정답과 동일, Top-N·크기 통계 저장")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _mk_tree(tree, dirs=10, files=10):
    for a in range(dirs):
        p = os.path.join(tree, "d%d" % a)
        os.makedirs(p)
        for i in range(files):
            with open(os.path.join(p, "f%d" % i), "wb") as fh:
                fh.write(b"x" * 4096)


def check_mount_down() -> None:
    d = tempfile.mkdtemp(prefix="iu_r2_")
    try:
        tree = os.path.join(d, "nas")
        _mk_tree(tree)
        exp = _ref_totals(os.fsencode(tree))
        # (a) 중단 → 마운트 빠진 채 재개 → '완료'로 굳으면 안 됨 → 복구 후 재개하면 정답
        db = os.path.join(d, "a.db")
        run_scan(db, tree, workers=1, with_monitor=False, stop_event=_TripAfter(25))
        os.rename(tree, tree + ".off")
        try:
            run_scan(db, tree, workers=1, with_monitor=False, resume=True)
        except Exception:  # noqa: BLE001 — 접근 불가는 오류로 끝나는 게 맞다
            pass
        _got, run = _root(db)
        assert run["status"] != "done", ("마운트 없이 재개가 완료로 굳음", run)
        assert "접근" in (run["error"] or ""), run
        os.rename(tree + ".off", tree)
        run_scan(db, tree, workers=1, with_monitor=False, resume=True)
        got, run = _root(db)
        assert run["status"] == "done" and got == exp and not run["error_dirs"], (run, got, exp)

        # (b) 스캔 도중 마운트가 사라짐 → 자동 일시정지(오류 행으로 확정하지 않음) → 복구 후 재개 = 정답
        class Vanish(Scanner):
            n = 0

            def _discover_one(self, dir_id, path, depth):
                Vanish.n += 1
                if Vanish.n == 4:
                    os.rename(tree, tree + ".off")
                return Scanner._discover_one(self, dir_id, path, depth)
        db2 = os.path.join(d, "b.db")
        dbmod.init_db(db2)
        Vanish(db2, tree, workers=1).run()
        _got, run = _root(db2)
        assert run["status"] == "paused" and "접근" in (run["error"] or ""), run
        os.rename(tree + ".off", tree)
        run_scan(db2, tree, workers=1, with_monitor=False, resume=True)
        got, run = _root(db2)
        assert run["status"] == "done" and got == exp and not run["error_dirs"], (run, got, exp)

        # (b2) 마운트가 빠지고 빈 마운트 지점 폴더만 남음(실제 umount 와 같은 모습) → 자동 일시정지
        class Unmount(Scanner):
            n = 0

            def _discover_one(self, dir_id, path, depth):
                Unmount.n += 1
                if Unmount.n == 4:
                    os.rename(tree, tree + ".off")
                    os.makedirs(tree)                      # 빈 마운트 지점
                return Scanner._discover_one(self, dir_id, path, depth)
        db4 = os.path.join(d, "b2.db")
        dbmod.init_db(db4)
        Unmount(db4, tree, workers=1).run()
        _got, run = _root(db4)
        assert run["status"] == "paused" and "마운트" in (run["error"] or ""), run
        try:                                               # 빈 마운트 지점인 채로 재개 → 진행하지 않음
            run_scan(db4, tree, workers=1, with_monitor=False, resume=True)
        except Exception:  # noqa: BLE001
            pass
        _got, run = _root(db4)
        assert run["status"] != "done", run
        os.rmdir(tree)
        os.rename(tree + ".off", tree)
        run_scan(db4, tree, workers=1, with_monitor=False, resume=True)
        got, run = _root(db4)
        assert run["status"] == "done" and got == exp and not run["error_dirs"], (run, got, exp)

        # (c) 새 스캔인데 루트를 열 수 없음 → '완료 0바이트'가 아니라 오류
        db3 = os.path.join(d, "c.db")
        try:
            run_scan(db3, os.path.join(d, "없는경로"), workers=1, with_monitor=False)
        except Exception:  # noqa: BLE001
            pass
        _got3 = dbmod.connect(db3)
        try:
            st = _got3.execute("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            _got3.close()
        assert st == "error", st
        print("[mount-down] OK  마운트 없이 재개=오류(행 보존)·도중 소실·빈 마운트 지점=자동 일시정지·복구 후 재개 정답·없는 루트=오류")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_non_ascii_secrets() -> None:
    from isilon_usage import auth
    pw = "비밀번호1234"
    assert auth.verify_password(pw, pw) is True
    assert auth.verify_password("비밀번호123", pw) is False
    assert auth.verify_password("abc", pw) is False
    g = auth.AuthGuard(lambda: pw)
    tok = g.login(pw)
    assert tok and tok.get("ok"), tok
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_r3_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.settings["export_token"] = "내보내기토큰"
        assert pc.export_token_ok("내보내기토큰") and not pc.export_token_ok("다른값")
        pc.settings["enroll_token"] = "가입토큰"
        r = pc.enroll_node({"url": "http://10.1.2.3:8765", "token": "T", "id": "n1",
                            "enroll_token": "가입토큰"})
        assert r.get("ok"), r
        r = pc.enroll_node({"url": "http://10.1.2.3:8765", "token": "T", "id": "n1",
                            "enroll_token": "틀림"})
        assert not r.get("ok") and r.get("_status") == 401, r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[non-ascii-secret] OK  한글 비밀번호 로그인·포탈 export/enroll 토큰 비교 정상(예외 없음)")


def check_resume_done_rejected() -> None:
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="iu_r4_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        tree = os.path.join(d, "tree")
        _mk_tree(tree, 3, 5)
        data = os.path.join(d, "data")
        ctl = ScanController(data)
        for engine in ("pscan", "threads"):
            sid = ctl.start_scan(tree, engine=engine)["scan_id"]
            row = None
            for _ in range(300):
                mc = dbmod.connect(mgrmod.manager_db_path(data))
                row = mgrmod.get_scan(mc, sid)
                mc.close()
                if row["status"] in ("done", "error") and sid not in ctl.running_ids():
                    break
                time.sleep(0.05)
            assert row["status"] == "done", (engine, row["status"])
            c = dbmod.connect(row["db_path"])
            n0 = c.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
            c.close()
            r = ctl.resume_scan(sid)
            assert not r["ok"], (engine, "완료된 스캔 재개를 거부해야 함", r)
            assert os.path.exists(row["db_path"]), (engine, "결과 DB 가 지워짐")
            c = dbmod.connect(row["db_path"])
            n1 = c.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
            c.close()
            assert n1 == n0 and n0 > 0, (engine, n0, n1)
        print("[resume-done] OK  완료된 pscan/threads 스캔 재개 거부 · 결과 DB 보존")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def check_schedule_after_orphan() -> None:
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="iu_r5_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        c = ScanController(os.path.join(d, "data"))
        started = []
        c._chain_start = lambda sc, path, running, rp: (started.append(path) or 99)  # type: ignore[assignment]
        c._scan_status = lambda sid: "paused"                                         # type: ignore[assignment]
        c.running_ids = lambda: []                                                    # type: ignore[assignment]
        c.running_paths = lambda: set()                                               # type: ignore[assignment]
        now = time.time()
        base = {"paths": ["/data/A", "/data/B"], "path": "/data/A", "unit": "minute", "every": 5,
                "enabled": True, "chain_i": 0, "chain_scan_id": 7}
        # 이번 주기 진행 중 사용자가 멈춤(아직 다음 차례 아님) → 다음 경로로 넘어가지도, 새로 시작하지도 않음
        c.settings["schedules"] = [dict(base, last_run=now - 60)]
        c._check_schedules()
        assert started == [], started
        # 다음 차례가 왔는데 체인 스캔이 여전히 paused(비정상 종료 정리 등) → 새 주기를 첫 경로부터 시작
        c.settings["schedules"] = [dict(base, last_run=now - 600)]
        c._check_schedules()
        sc = c.settings["schedules"][0]
        assert started == ["/data/A"] and sc.get("chain_scan_id") == 99 and sc.get("chain_i") == 0, (started, sc)
        # 꺼진 예약은 차례가 와도 시작하지 않는다
        started.clear()
        c.settings["schedules"] = [dict(base, last_run=now - 600, enabled=False)]
        c._check_schedules()
        assert started == [], started
        print("[schedule-orphan] OK  paused 체인: 이번 주기는 대기, 다음 차례엔 새 주기 시작(꺼진 예약 제외)")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    check_non_ascii_secrets()
    check_schedule_after_orphan()
    check_resume_done_rejected()
    check_mount_down()
    check_non_utf8_names()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""1.99.41 회귀 테스트 — 2026-11 전체 리뷰(fable 3개 영역 + 직접 재현)에서 확인된 결함.

각 검사는 수정 전 코드에서 실패함을 확인했다. 섹션:
  A. 1.99.40 이 만든 회귀 3건(마운트 감지 오탐·예약 스캔 누적·비 UTF-8 확장자 통계)
  B. 수치 정확성(pscan 하드링크·-x, 통계 저널, 일시정지 카운터, 루트 용량·보존 등)
  C. 보안(로그인 잠금 IP별·동시 시도, 비밀번호 변경 시 세션 만료, 암호화 플래그, 경로 경계, ssh 인자, XSS)
  D. 운영 안정성·기타
"""

import os
import shutil
import subprocess
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


def _ref(root):
    """os.walk(바이트 경로) 기준 정답(st_blocks*512, 디렉터리 자기 inode 포함, 하드링크 1회)."""
    root = os.fsencode(root)
    seen = set()
    tb = tf = 0
    for dp, _dn, fns in os.walk(root):
        tb += os.lstat(dp).st_blocks * 512
        for f in fns:
            st = os.lstat(os.path.join(dp, f))
            tf += 1
            if st.st_nlink > 1:
                k = (st.st_dev, st.st_ino)
                if k in seen:
                    continue
                seen.add(k)
            tb += st.st_blocks * 512
    return tb, tf


def _mk(root, ndirs=12, nfiles=5, depth=2, size=3000):
    os.makedirs(root, exist_ok=True)
    for i in range(ndirs):
        d = os.path.join(root, "d%03d" % i)
        os.makedirs(d, exist_ok=True)
        for j in range(nfiles):
            with open(os.path.join(d, "f%02d.bin" % j), "wb") as fh:
                fh.write(b"x" * size)
        if depth > 1:
            _mk(d, max(2, ndirs // 4), nfiles, depth - 1, size)


def _run_row(db):
    c = dbmod.connect(db)
    try:
        run = dict(c.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone())
        r = c.execute("SELECT total_bytes, total_files FROM directories WHERE depth=0").fetchone()
        return run, ((r["total_bytes"], r["total_files"]) if r else None)
    finally:
        c.close()


def _can_mount():
    if os.geteuid() != 0 or not shutil.which("mount"):
        return False
    d = tempfile.mkdtemp(prefix="iu_mnt_probe_")
    try:
        ok = subprocess.call(["mount", "-t", "tmpfs", "-o", "size=8m", "tmpfs", d],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        if ok:
            subprocess.call(["umount", d])
        return ok
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================================ A. 1.99.40 회귀
def check_root_probe_false_positive() -> None:
    """A1: 처음 본 최상위 폴더 8개를 사용자가 지워도(일자별 폴더 정리 등) '마운트 해제'로 오판하지 않는다.

    1.99.40 은 깊이 1 폴더 8개를 탐침으로 삼아, 그 8개가 다 없으면 마운트 소실로 보고 재개를 영구 거부했다
    (진행분을 버리고 처음부터 다시 해야 함).
    """
    d = tempfile.mkdtemp(prefix="iu_a1_")
    try:
        root = os.path.join(d, "root")
        _mk(root, ndirs=12, depth=2)
        db = os.path.join(d, "s.db")

        class StopAfterRoot(Scanner):                      # 루트만 처리하고 멈춤(깊이 1 폴더는 모두 대기 상태)
            def _discover_one(self, dir_id, path, depth):
                Scanner._discover_one(self, dir_id, path, depth)
                if depth == 0:
                    self.stop_event.set()
        dbmod.init_db(db)
        StopAfterRoot(db, root, workers=1).run()
        run, _ = _run_row(db)
        assert run["status"] == "paused", run["status"]
        c = dbmod.connect(db)
        try:
            first8 = [dbmod.fs_text(r[0]) for r in c.execute(
                "SELECT path FROM directories WHERE depth=1 ORDER BY id LIMIT 8")]
        finally:
            c.close()
        for p in first8:                                   # 정상적인 삭제(churn)
            shutil.rmtree(p)
        exp = _ref(root)
        run_scan(db, root, workers=1, with_monitor=False, resume=True)
        run, got = _run_row(db)
        assert run["status"] == "done", ("탐침 폴더 삭제를 마운트 소실로 오판", run["status"], run["error"])
        assert got == exp, (got, exp)

        # 실행 중 변형: 루트를 처리한 직후 최상위 8개가 지워짐 → 오류 행(삭제된 폴더)으로 끝까지 완료
        root2 = os.path.join(d, "root2")
        _mk(root2, ndirs=12, depth=2)

        class Churn(Scanner):
            n = 0

            def _discover_one(self, dir_id, path, depth):
                Churn.n += 1
                if Churn.n == 2:
                    for name in sorted(os.listdir(root2))[:8]:
                        shutil.rmtree(os.path.join(root2, name), ignore_errors=True)
                return Scanner._discover_one(self, dir_id, path, depth)
        db2 = os.path.join(d, "s2.db")
        dbmod.init_db(db2)
        Churn(db2, root2, workers=1).run()
        run, got = _run_row(db2)
        assert run["status"] == "done" and "마운트" not in (run["error"] or ""), (run["status"], run["error"])
        assert got == _ref(root2), (got, _ref(root2))
        print("[root-probe-fp] OK  최상위 폴더 8개 삭제 후 재개·실행 중 삭제 모두 완료(오탐 없음), 합계 정답")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_real_mount_loss() -> None:
    """A1b: 진짜 마운트 소실(tmpfs 실제 umount)은 계속 잡는다 — 루트=마운트 지점 / 루트=마운트 아래 둘 다.

    도중 소실 → 자동 일시정지(오류 행 없음) → 빠진 채 재개 → 진행 안 함 → 다시 마운트 후 재개 → 정답.
    root 권한·mount 가 없으면 건너뛴다(CI 비루트 환경).
    """
    if not _can_mount():
        print("[real-mount-loss] 건너뜀 (root/mount 불가)")
        return
    for sub in ("", "sub"):
        d = tempfile.mkdtemp(prefix="iu_a1b_")
        mnt = os.path.join(d, "mnt")
        os.makedirs(mnt)
        mounted = False
        try:
            def mount_and_fill():
                assert subprocess.call(["mount", "-t", "tmpfs", "-o", "size=64m", "tmpfs", mnt]) == 0
                _mk(os.path.join(mnt, sub) if sub else mnt, ndirs=8, depth=2)
            mount_and_fill()
            mounted = True
            root = os.path.join(mnt, sub) if sub else mnt
            exp = _ref(root)

            class Unmount(Scanner):
                n = 0

                def _discover_one(self, dir_id, path, depth):
                    Unmount.n += 1
                    if Unmount.n == 4:
                        subprocess.call(["umount", "-l", mnt])
                    return Scanner._discover_one(self, dir_id, path, depth)
            db = os.path.join(d, "s.db")
            dbmod.init_db(db)
            Unmount(db, root, workers=1).run()
            mounted = False
            run, _ = _run_row(db)
            assert run["status"] == "paused", (sub, "도중 마운트 소실이 일시정지가 아님", run["status"], run["error"])
            assert run["error"] and ("마운트" in run["error"] or "접근" in run["error"]), run["error"]
            c = dbmod.connect(db)
            try:
                nerr = c.execute("SELECT COUNT(*) FROM directories WHERE error IS NOT NULL").fetchone()[0]
            finally:
                c.close()
            assert nerr == 0, (sub, "마운트 소실을 오류 행으로 확정", nerr)
            try:                                           # 빠진 채 재개 → 진행하지 않음
                run_scan(db, root, workers=1, with_monitor=False, resume=True)
            except Exception:  # noqa: BLE001
                pass
            run, _ = _run_row(db)
            assert run["status"] != "done", (sub, run["status"])
            mount_and_fill()
            mounted = True
            run_scan(db, root, workers=1, with_monitor=False, resume=True)
            run, got = _run_row(db)
            assert run["status"] == "done" and got == exp and not run["error_dirs"], (sub, run["status"], got, exp)
        finally:
            if mounted or os.path.ismount(mnt):
                subprocess.call(["umount", "-l", mnt])
            shutil.rmtree(d, ignore_errors=True)
    print("[real-mount-loss] OK  tmpfs 실제 해제: 루트=마운트 지점·아래 모두 일시정지 → 빠진 채 재개 거부 → 복구 후 정답")


def check_schedule_guard_pause_no_pileup() -> None:
    """A2: 예약 스캔이 디스크 여유 가드로 자동 일시정지되면, 다음 차례마다 새 스캔을 쌓지 않는다.

    1.99.40 은 멈춘 체인을 버리고 매 주기 새 스캔을 시작해, 같은 가드에 다시 걸린 일시정지 스캔과 DB 가
    주기마다 늘었다(재현: 6주기 → 6개, 5분 예약이면 하루 288개).
    """
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="iu_a2_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        root = os.path.join(d, "nas")
        os.makedirs(os.path.join(root, "a", "b"))
        c = ScanController(os.path.join(d, "data"))
        m = dict(c.settings)
        m["min_free_gb"] = 10 ** 7                         # 항상 디스크 부족 가드에 걸림
        m["schedules"] = [{"paths": [root], "unit": "minute", "every": 5, "last_run": 0}]
        c._save_settings(m)
        for _cycle in range(4):
            c._check_schedules()
            t0 = time.time()
            while c.running_ids() and time.time() - t0 < 20:
                time.sleep(0.05)
            c._check_schedules()
            m = dict(c.settings)
            sc = [dict(x) for x in m["schedules"]]
            sc[0]["last_run"] = float(sc[0].get("last_run") or 0) - 301   # 다음 차례
            m["schedules"] = sc
            c._save_settings(m)
        mc = dbmod.connect(mgrmod.manager_db_path(os.path.join(d, "data")))
        try:
            rows = [(r["id"], r["status"]) for r in mc.execute("SELECT id, status FROM scans")]
        finally:
            mc.close()
        assert len(rows) == 1 and rows[0][1] == "paused", ("가드로 멈춘 예약이 주기마다 새 스캔을 쌓음", rows)
        print("[schedule-guard] OK  디스크 가드로 멈춘 예약: 4주기 동안 스캔 1개 유지(누적 없음)")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def check_schedule_orphan_resume() -> None:
    """A2b: 비정상 종료(kill -9 등)로 멈춘 예약 스캔은 새 주기 대신 '이어서' 재개하고, 사용자가 멈춘 것은 기다린다.

    재기동 정리(_reconcile_orphans)가 고아 표시(paused_by='orphan')를 남기고, 예약 틱이 그 스캔만 자동 재개한다
    (반복 실패 시 3회까지). 사용자·가드로 멈춘 스캔은 다음 차례가 와도 새로 시작하지 않는다.
    """
    from isilon_usage.server import ScanController
    from isilon_usage import scanner as scanmod
    d = tempfile.mkdtemp(prefix="iu_a2b_")
    cwd = os.getcwd()
    os.chdir(d)
    orig = scanmod.Scanner._safe_stat
    try:
        root = os.path.join(d, "nas")
        _mk(root, ndirs=6, nfiles=40, depth=1)
        data = os.path.join(d, "data")
        a = ScanController(data)
        a._save_settings(dict(a.settings, schedules=[{"paths": [root], "unit": "minute", "every": 5,
                                                       "enabled": True, "last_run": 0}]))
        scanmod.Scanner._safe_stat = lambda self, e: (time.sleep(0.004), orig(self, e))[1]
        a._check_schedules()
        sid = a.settings["schedules"][0].get("chain_scan_id")
        assert sid, a.settings["schedules"]
        time.sleep(0.4)
        for rec in list(a._scans.values()):                # '프로세스가 죽은 것처럼' 스레드만 멈춤
            rec["stop"].set()
        t0 = time.time()
        while a.running_ids() and time.time() - t0 < 20:
            time.sleep(0.05)
        scanmod.Scanner._safe_stat = orig
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        mgrmod.update_scan(mc, sid, status="discovering", phase="discovering")   # 죽은 프로세스의 흔적
        mc.commit()
        mc.close()
        b = ScanController(data)                           # 재기동 → 고아 정리(paused_by=orphan)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        row = mgrmod.get_scan(mc, sid)
        mc.close()
        assert row["status"] == "paused" and row["paused_by"] == "orphan", (row["status"], row["paused_by"])
        b._check_schedules()                               # 차례가 아니어도 고아는 이어서 재개
        t0 = time.time()
        while b.running_ids() and time.time() - t0 < 30:
            time.sleep(0.05)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        rows = [(r["id"], r["status"], r["paused_by"]) for r in mc.execute("SELECT * FROM scans")]
        mc.close()
        assert rows == [(sid, "done", None)], ("고아 예약 스캔이 이어서 완료되지 않음", rows)

        # 사용자가 멈춘 예약 체인: 다음 차례가 와도 새 스캔을 시작하지 않는다
        started = []
        b._chain_start = lambda sc, path, running, rp: (started.append(path) or 99)   # type: ignore[assignment]
        b._scan_row = lambda s: {"status": "paused", "paused_by": None}             # type: ignore[assignment]
        b.settings["schedules"] = [{"paths": [root], "unit": "minute", "every": 5, "enabled": True,
                                    "last_run": time.time() - 3600, "chain_i": 0, "chain_scan_id": 7}]
        b._check_schedules()
        assert started == [], ("사용자가 멈춘 체인인데 새 주기 시작", started)
        # 고아인데 재개가 계속 실패 → 최대 3회 시도 후 기다림(재시작 폭주 방지)
        tries = []
        b._scan_row = lambda s: {"status": "paused", "paused_by": "orphan"}         # type: ignore[assignment]
        b.resume_scan = lambda s: (tries.append(s) or {"ok": False, "reason": "x"})  # type: ignore[assignment]
        for _ in range(6):
            b._check_schedules()
        assert len(tries) == 3 and started == [], (tries, started)
        print("[schedule-orphan] OK  고아(비정상 종료) 예약 스캔은 이어서 재개·완료, 사용자 정지는 대기, 재개 실패 3회 상한")
    finally:
        scanmod.Scanner._safe_stat = orig
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def check_resume_stale_active() -> None:
    """A2c: 서버가 떠 있는 동안 CLI 스캔 프로세스가 죽어 'sizing/discovering' 으로 남은 스캔(하트비트 15분+)은
    화면에서 재개할 수 있다(1.99.40 의 '일시정지/오류만 재개'가 이 경우를 서버 재시작 전까지 막았다).
    하트비트가 살아 있는(다른 곳에서 실행 중인) 스캔은 여전히 거부.
    """
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="iu_a2c_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        root = os.path.join(d, "nas")
        _mk(root, ndirs=3, depth=1)
        data = os.path.join(d, "data")
        c = ScanController(data)
        sid = c.start_scan(root, engine="threads")["scan_id"]
        t0 = time.time()
        while c.running_ids() and time.time() - t0 < 20:
            time.sleep(0.05)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        mgrmod.update_scan(mc, sid, status="sizing", phase="sizing")
        mc.commit()
        mc.close()
        r = c.resume_scan(sid)                              # 하트비트 신선 → 다른 곳에서 실행 중으로 보고 거부
        assert not r["ok"], r
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        mc.execute("UPDATE scans SET updated_at=? WHERE id=?", (time.time() - 3600, sid))
        mc.commit()
        mc.close()
        r = c.resume_scan(sid)
        assert r["ok"], ("하트비트가 끊긴(죽은) 진행 중 스캔을 재개하지 못함", r)
        t0 = time.time()
        while c.running_ids() and time.time() - t0 < 20:
            time.sleep(0.05)
        print("[resume-stale] OK  하트비트 끊긴 진행 중 상태 스캔 재개 허용 · 신선한 것은 거부")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


def check_ext_stats_resume() -> None:
    """A3: 비 UTF-8 '확장자'(EUC-KR "1.계획서")가 일시정지 전후에 모두 있어도 재개 후 통계·Top-N 이 저장된다.

    1.99.40 은 재개 때 복원한 키(DB 형식)와 새로 센 키(서로게이트 형식)가 저장 시 같은 키가 되어 PRIMARY KEY
    충돌 → 예외를 삼켜 크기 통계·Top-N 이 일시정지 시점에 멈췄다.
    """
    d = tempfile.mkdtemp(prefix="iu_a3_")
    try:
        root = os.path.join(d, "root")
        os.makedirs(root)
        rb = os.fsencode(root)
        euc = "1.계획서".encode("euc-kr")
        for i in range(6):
            sub = rb + b"/d%d" % i
            os.makedirs(sub)
            with open(sub + b"/" + euc, "wb") as fh:
                fh.write(b"k" * (100000 * (i + 1)))
            with open(sub + b"/big%d.bin" % i, "wb") as fh:
                fh.write(b"b" * (1000000 * (i + 1)))
        db = os.path.join(d, "s.db")

        class Stop4(Scanner):
            n = 0

            def _discover_one(self, dir_id, path, depth):
                Stop4.n += 1
                if Stop4.n == 4:
                    self.stop_event.set()
                return Scanner._discover_one(self, dir_id, path, depth)
        dbmod.init_db(db)
        Stop4(db, root, workers=1).run()
        run_scan(db, root, workers=1, with_monitor=False, resume=True)
        c = dbmod.connect(db)
        try:
            st = {k: c.execute("SELECT COALESCE(SUM(files),0) FROM scan_stats WHERE kind=?", (k,)).fetchone()[0]
                  for k in ("age", "ext", "size", "owner")}
            ntop = c.execute("SELECT COUNT(*) FROM top_files").fetchone()[0]
            status = c.execute("SELECT status FROM scan_runs").fetchone()[0]
        finally:
            c.close()
        assert status == "done", status
        assert st == {"age": 12, "ext": 12, "size": 12, "owner": 12} and ntop == 12, (st, ntop)
        print("[ext-stats-resume] OK  비 UTF-8 확장자 재개 후 나이·확장자·크기·소유자 통계 12/12, Top-N 12")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================================ B. 수치 정확성
def check_pscan_hardlinks_and_xdev() -> None:
    """B1: pscan 이 하드링크를 inode 당 한 번 세고(단위 안·단위 사이 모두), -x 면 하위 마운트를 세지 않는다.

    예전엔 중복 제거가 없어 4MB 파일 링크 6개를 6.9배로 셌고(threads·du 와 불일치), -x 를 무시했다.
    """
    from isilon_usage import pscan
    d = tempfile.mkdtemp(prefix="iu_b1_")
    try:
        root = os.path.join(d, "root")
        for sub in ("a", "b", "c"):
            os.makedirs(os.path.join(root, sub, "deep"))
        big = os.path.join(root, "a", "big.bin")
        with open(big, "wb") as fh:
            fh.write(os.urandom(4 * 1024 * 1024))
        for i in range(4):                                # 같은 단위 안 링크
            os.link(big, os.path.join(root, "a", "deep", "l%d" % i))
        os.link(big, os.path.join(root, "b", "lb"))       # 다른 단위 링크
        os.link(big, os.path.join(root, "c", "deep", "lc"))
        os.link(big, os.path.join(root, "rootlink"))      # 루트 직속 링크
        with open(os.path.join(root, "c", "x.bin"), "wb") as fh:
            fh.write(b"x" * 5000)
        exp = _ref(root)
        for procs, threads in ((1, 1), (4, 1), (4, 3)):
            r = pscan.parallel_scan(root, processes=procs, threads_per_proc=threads)
            assert r["ok"] and (r["total_bytes"], r["total_files"]) == exp, (procs, threads, r["total_bytes"],
                                                                              r["total_files"], exp)
            assert sum(t["bytes"] for t in r["per_top"]) + (r["total_bytes"] - sum(
                t["bytes"] for t in r["per_top"])) == r["total_bytes"]
        r = pscan.parallel_scan(root, processes=2, hardlink_dedup=False)
        assert r["total_bytes"] > exp[0], "중복 제거를 끄면 링크마다 세야 함"
        # threads 엔진과 같은 값
        db = os.path.join(d, "t.db")
        run_scan(db, root, workers=2, with_monitor=False)
        _run, got = _run_row(db)
        assert got == exp, (got, exp)
        # -x: 실제 하위 마운트(tmpfs) — root 권한 있을 때
        if _can_mount():
            mnt = os.path.join(root, "b", "mnt")
            os.makedirs(mnt)
            assert subprocess.call(["mount", "-t", "tmpfs", "-o", "size=16m", "tmpfs", mnt]) == 0
            try:
                with open(os.path.join(mnt, "inside.bin"), "wb") as fh:
                    fh.write(b"m" * 2 * 1024 * 1024)
                st_root = os.stat(root).st_dev
                ref_x = [0, 0]
                seen = set()
                for dp, dns, fns in os.walk(root):
                    dns[:] = [n for n in dns if os.lstat(os.path.join(dp, n)).st_dev == st_root]
                    ref_x[0] += os.lstat(dp).st_blocks * 512
                    for f in fns:
                        st = os.lstat(os.path.join(dp, f))
                        ref_x[1] += 1
                        if st.st_nlink > 1:
                            if (st.st_dev, st.st_ino) in seen:
                                continue
                            seen.add((st.st_dev, st.st_ino))
                        ref_x[0] += st.st_blocks * 512
                for procs in (1, 4):
                    r = pscan.parallel_scan(root, processes=procs, one_file_system=True)
                    assert (r["total_bytes"], r["total_files"]) == tuple(ref_x), (procs, r["total_bytes"],
                                                                                   r["total_files"], ref_x)
                r = pscan.parallel_scan(root, processes=2, one_file_system=False)
                assert r["total_bytes"] > ref_x[0], "-x 끄면 하위 마운트도 세야 함"
            finally:
                subprocess.call(["umount", "-l", mnt])
            xmsg = " · -x 실제 하위 마운트 제외"
        else:
            xmsg = " · -x 건너뜀(root/mount 불가)"
        print("[pscan-hardlink-x] OK  링크 7개(단위 안·사이·루트) 정답과 동일(프로세스 1/4·스레드 3) · threads 엔진과 일치%s"
              % xmsg)
    finally:
        shutil.rmtree(d, ignore_errors=True)


_CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, %(repo)r)
from isilon_usage import db as dbmod
from isilon_usage.scanner import Scanner

class Crash(Scanner):
    n = 0

    def _discover_one(self, dir_id, path, depth):
        Scanner._discover_one(self, dir_id, path, depth)
        Crash.n += 1
        if Crash.n == %(after)d:                 # 디렉터리 몇 개를 커밋한 뒤(20초 스냅샷 전) 강제 종료
            with self._dlock:
                self._flush_progress_locked(self._disc_conn, force=True)
            os._exit(9)

dbmod.init_db(%(db)r)
Crash(%(db)r, %(root)r, workers=1).run()
"""


def check_stats_journal_crash() -> None:
    """B3: kill -9·OOM·업그레이드 재시작으로 끊긴 뒤 재개해도 리포트 통계(나이·확장자·크기·소유자·Top-N)가
    폴더 합계와 정확히 같다.

    예전엔 통계를 20초마다 통째로 저장해, 마지막 저장 이후 커밋된 폴더의 통계가 영구 누락됐다
    (재현: 파일 600개 중 290개가 나이 통계에서 빠짐). 이제 폴더 완료를 커밋할 때 그 사이 델타를 같은
    트랜잭션으로 저널에 덧붙이고, 재개 때 스냅샷 + 저널로 복원한다.
    """
    d = tempfile.mkdtemp(prefix="iu_b3_")
    try:
        root = os.path.join(d, "root")
        os.makedirs(root)
        for i in range(30):
            sub = os.path.join(root, "d%02d" % i)
            os.makedirs(sub)
            for j in range(20):
                with open(os.path.join(sub, "f%02d.%s" % (j, ("log", "dat", "bin")[j % 3])), "wb") as fh:
                    fh.write(b"z" * (1000 + 37 * i + j))
        with open(os.path.join(root, "d07", "HUGE.bin"), "wb") as fh:
            fh.write(b"H" * 5000000)
        exp = _ref(root)
        db = os.path.join(d, "s.db")
        code = _CRASH_CHILD % {"repo": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "after": 15, "db": db, "root": root}
        rc = subprocess.call([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert rc == 9, rc
        c = dbmod.connect(db)
        try:
            committed = c.execute("SELECT COUNT(*), COALESCE(SUM(file_count),0) FROM directories "
                                  "WHERE status='discovered'").fetchone()
            snap = c.execute("SELECT COALESCE(SUM(files),0) FROM scan_stats WHERE kind='age'").fetchone()[0]
            jr = c.execute("SELECT COALESCE(SUM(files),0) FROM stats_journal WHERE kind='age'").fetchone()[0]
        finally:
            c.close()
        assert committed[0] >= 10 and snap + jr == committed[1], ("저널+스냅샷이 커밋된 폴더 파일 수와 다름",
                                                                 committed, snap, jr)
        run_scan(db, root, workers=2, with_monitor=False, resume=True)
        run, got = _run_row(db)
        assert run["status"] == "done" and got == exp, (run["status"], got, exp)
        c = dbmod.connect(db)
        try:
            st = {k: c.execute("SELECT COALESCE(SUM(files),0) FROM scan_stats WHERE kind=?", (k,)).fetchone()[0]
                  for k in ("age", "atime_age", "owner", "ext", "size")}
            tops = [r[0] for r in c.execute("SELECT path FROM top_files ORDER BY bytes DESC LIMIT 1")]
            leftover = c.execute("SELECT COUNT(*) FROM stats_journal").fetchone()[0]
        finally:
            c.close()
        assert all(v == exp[1] for v in st.values()), ("재개 후 통계 누락", st, exp[1])
        assert tops and tops[0].endswith("HUGE.bin"), tops
        assert leftover == 0, ("완료 후 저널이 남음", leftover)
        print("[stats-journal-crash] OK  강제 종료(커밋 %d폴더) 후 재개: 통계 5종 %d/%d 파일 · 최대 파일 유지 · 저널 정리"
              % (committed[0], exp[1], exp[1]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_pause_counters_and_resume_clock() -> None:
    """B4: 일시정지 직후 scan_runs 의 조사 용량·파일 수 = 커밋된 행 합(버려진 폴더의 부분 값 제외),
    재개해 도는 동안엔 지난 일시정지의 finished_at 이 지워져 경과·ETA 가 멈추지 않는다.

    예전엔 5,000파일 폴더의 첫 청크 직후 멈추면 파일 2,000개·8MB 가 커밋된 행 합보다 크게 남았고(재개 때야
    바로잡힘), 재개 중에도 finished_at 이 남아 build_status 의 경과가 그 시각에 고정됐다.
    """
    d = tempfile.mkdtemp(prefix="iu_b4_")
    try:
        root = os.path.join(d, "root")
        os.makedirs(os.path.join(root, "big"))
        for i in range(5000):
            with open(os.path.join(root, "big", "f%05d" % i), "wb") as fh:
                fh.write(b"q" * 100)
        os.makedirs(os.path.join(root, "small"))
        with open(os.path.join(root, "small", "a"), "wb") as fh:
            fh.write(b"a" * 10)
        db = os.path.join(d, "s.db")

        class StopAfterChunk(Scanner):                     # 큰 폴더의 첫 청크가 반영된 직후 멈춤
            def _stat_chunk(self, acc, entries, cur_dir):
                r = Scanner._stat_chunk(self, acc, entries, cur_dir)
                if cur_dir.endswith("big"):
                    self.stop_event.set()
                return r
        dbmod.init_db(db)
        StopAfterChunk(db, root, workers=1).run()
        c = dbmod.connect(db)
        try:
            run = c.execute("SELECT status, scanned_bytes, total_files FROM scan_runs").fetchone()
            rows = c.execute("SELECT COALESCE(SUM(own_bytes),0), COALESCE(SUM(file_count),0) FROM directories "
                             "WHERE status!='pending' AND status!='claimed'").fetchone()
        finally:
            c.close()
        assert run["status"] == "paused", run["status"]
        assert (run["scanned_bytes"], run["total_files"]) == tuple(rows), ("일시정지 카운터가 커밋 합보다 큼",
                                                                          tuple(run), tuple(rows))
        seen = {}

        class Watch(Scanner):                               # 재개 중 finished_at 확인
            def _discover_one(self, dir_id, path, depth):
                if "fin" not in seen:
                    cc = dbmod.connect(db)
                    try:
                        seen["fin"] = cc.execute("SELECT finished_at FROM scan_runs").fetchone()[0]
                    finally:
                        cc.close()
                return Scanner._discover_one(self, dir_id, path, depth)
        Watch(db, root, workers=1, resume=True).run()
        assert seen.get("fin") is None, ("재개 중인데 finished_at 이 남음", seen)
        run, got = _run_row(db)
        assert run["status"] == "done" and got == _ref(root), (run["status"], got, _ref(root))
        print("[pause-counters] OK  일시정지 직후 카운터 = 커밋 합 · 재개 중 finished_at 비움 · 완료 합계 정답")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_fold_error_counts() -> None:
    """B5: fold 로 접힌 하위 폴더의 읽기 오류 수가 완료·재개 후에도 유지된다(예전엔 카운터에만 있어 재개 때 0)."""
    from isilon_usage import scanner as scanmod
    real_scandir = os.scandir
    d = tempfile.mkdtemp(prefix="iu_b5_")

    def fake_scandir(p="."):
        if isinstance(p, str) and os.path.basename(p).startswith("deny"):
            raise PermissionError(13, "Permission denied", p)
        return real_scandir(p)
    try:
        root = os.path.join(d, "root")
        for i in range(3):
            os.makedirs(os.path.join(root, "top", "deny%d" % i))
        os.makedirs(os.path.join(root, "top", "ok"))
        with open(os.path.join(root, "top", "ok", "f"), "wb") as fh:
            fh.write(b"f" * 100)
        scanmod.os.scandir = fake_scandir
        db = os.path.join(d, "s.db")
        run_scan(db, root, workers=1, fold_depth=1, with_monitor=False)
        run, _ = _run_row(db)
        assert run["status"] == "done" and run["error_dirs"] == 3, (run["status"], run["error_dirs"])
        # 재개 경로: 카운터를 DB 에서 다시 세어도 3
        db2 = os.path.join(d, "s2.db")

        class StopAfterTop(Scanner):
            def _discover_one(self, dir_id, path, depth):
                Scanner._discover_one(self, dir_id, path, depth)
                if path.endswith("top"):
                    self.stop_event.set()
        dbmod.init_db(db2)
        StopAfterTop(db2, root, workers=1, fold_depth=1).run()
        run_scan(db2, root, workers=1, fold_depth=1, with_monitor=False, resume=True)
        run, _ = _run_row(db2)
        assert run["status"] == "done" and run["error_dirs"] == 3, ("재개 후 fold 오류 수 소실", run["error_dirs"])
        print("[fold-errors] OK  fold 하위 읽기 오류 3개가 완료·재개 후에도 3으로 유지")
    finally:
        scanmod.os.scandir = real_scandir
        shutil.rmtree(d, ignore_errors=True)


def check_du_apparent_and_pua_names() -> None:
    """B6: du 백엔드 apparent 합계가 native 와 같다(du 버전과 무관) · 이름에 원래 사설영역 문자(U+10FFB0)가
    있어도 비 UTF-8 바이트 이름(0xB0)과 섞이지 않고 둘 다 정확히 센다(1.99.40 은 그 하위 트리를 통째로 누락).
    """
    d = tempfile.mkdtemp(prefix="iu_b6_")
    try:
        root = os.path.join(d, "root")
        _mk(root, ndirs=5, depth=2)
        if shutil.which("du"):
            tot = {}
            for be in ("native", "du"):
                db = os.path.join(d, "%s.db" % be)
                run_scan(db, root, backend=be, size_mode="apparent", workers=1, with_monitor=False)
                tot[be] = _run_row(db)[1]
            assert tot["native"] == tot["du"], ("du apparent 합계가 native 와 다름", tot)
        root2 = os.fsencode(os.path.join(d, "pua"))
        os.makedirs(root2)
        a_name = "x\U0010FFB0".encode("utf-8")        # 유효한 UTF-8 사설영역 문자
        b_name = b"x\xf4\x8f\xbe"                      # 같은 앞부분의 깨진 바이트(서로게이트로 들어옴)
        c_name = b"x\xb0"                                # 비 UTF-8 한 바이트
        for nm, size in ((a_name, 1000000), (b_name, 20000), (c_name, 3000)):
            os.makedirs(os.path.join(root2, nm, b"in"))
            with open(os.path.join(root2, nm, b"in", b"f"), "wb") as fh:
                fh.write(b"p" * size)
        exp = _ref(root2)
        db = os.path.join(d, "pua.db")
        run_scan(db, os.fsdecode(root2), workers=2, with_monitor=False)
        run, got = _run_row(db)
        c = dbmod.connect(db)
        try:
            n1 = c.execute("SELECT COUNT(*) FROM directories WHERE depth=1").fetchone()[0]
            paths = [r[0] for r in c.execute("SELECT path FROM directories")]
        finally:
            c.close()
        assert run["status"] == "done" and not run["error_dirs"] and got == exp and n1 == 3, (
            run["status"], run["error_dirs"], got, exp, n1)
        for p_ in paths:
            assert os.path.isdir(dbmod.fs_text(p_)), p_
        print("[du-apparent-pua] OK  du apparent = native · 사설영역 문자 이름과 비 UTF-8 이름이 섞이지 않고 정답")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check_capacity_prune_and_portal_rollups() -> None:
    """B7: 루트 용량 = 마지막 완료 스캔(멈춤·오류 부분 값이 덮지 않음), 보존 정리는 마지막 완료를 지우지 않음,
    포탈 '시작 이후 시간당'은 진행 중 스캔만, 크기만 같은 서로 다른 FS 는 합치지 않음, 빈 경로 새 스캔 거부.
    """
    d = tempfile.mkdtemp(prefix="iu_b7_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        data = os.path.join(d, "data")
        mgrmod.init_manager(data)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        now = time.time()
        rows = [("done", 10 ** 12, now - 900), ("error", 0, now - 600), ("paused", 7, now - 300)]
        ids = []
        for st, b, t in rows:
            cur = mc.execute("INSERT INTO scans(db_path, db_filename, root_path, status, phase, started_at, "
                             "updated_at, finished_at, scanned_bytes) VALUES(?,?,?,?,?,?,?,?,?)",
                             (os.path.join(data, "x%d.db" % len(ids)), "x%d.db" % len(ids), "/nas", st, st,
                              t, t, t, b))
            ids.append(cur.lastrowid)
        mc.commit()
        ov = mgrmod.overall_capacity(mc)
        mc.close()
        r = ov["roots"][0]
        assert ov["total_capacity_bytes"] == 10 ** 12 and r["capacity_scan_id"] == ids[0], (ov["total_capacity_bytes"], r)
        assert r["scan_id"] == ids[2] and r["status"] == "paused" and r["scanned_bytes"] == 7, r
        res = mgrmod.prune_scans(data, keep_per_root=1)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        left = sorted(x[0] for x in mc.execute("SELECT id FROM scans"))
        mc.close()
        assert ids[0] in left and ids[2] in left and ids[1] not in left, ("마지막 완료 스캔이 정리됨", res, left)

        from isilon_usage import portal as P
        pc = P.PortalController(os.path.join(d, "hq"))
        TB = 1024 ** 4
        ft, fu, _ff, _pct = pc._fs_capacity([{"fs_total_bytes": 100 * TB, "fs_used_bytes": 40 * TB},
                                             {"fs_total_bytes": 100 * TB, "fs_used_bytes": 70 * TB}])
        assert (ft, fu) == (200 * TB, 110 * TB), (ft / TB, fu / TB)
        pc.upsert_node({"id": "n1", "url": "http://10.9.9.9:8765", "token": "T"})
        pc._cache["n1"] = {"online": True, "overall": {"total_scanned_bytes": 10 ** 15, "roots": [
            {"root_path": "/a", "status": "done", "scanned_bytes": 9 * 10 ** 14, "started_at": now - 86400,
             "updated_at": now - 80000, "finished_at": now - 80000},
            {"root_path": "/b", "status": "discovering", "scanned_bytes": 3600 * 10 ** 6,
             "started_at": now - 3600, "updated_at": now}]}}
        tw = pc.throughput_windows()
        row = next(x for x in tw["nodes"] if x["id"] == "n1")
        assert abs(row["since_start_bph"] - 3600 * 10 ** 6) < 3600 * 10 ** 6 * 0.01, row["since_start_bph"]

        from isilon_usage.server import ScanController
        root = os.path.join(d, "mnt")
        os.makedirs(root)
        with open(os.path.join(root, "f"), "wb") as fh:
            fh.write(b"x" * 10000)
        c = ScanController(os.path.join(d, "edge"))
        sid = c.start_scan(root, engine="threads")["scan_id"]
        t0 = time.time()
        while c.running_ids() and time.time() - t0 < 20:
            time.sleep(0.05)
        os.remove(os.path.join(root, "f"))                 # 마운트가 빠져 빈 지점만 남은 상황
        r2 = c.start_scan(root, engine="threads")
        assert not r2["ok"] and r2.get("empty_root"), r2
        r3 = c.start_scan(root, engine="threads", allow_empty=True)
        assert r3["ok"], r3
        t0 = time.time()
        while c.running_ids() and time.time() - t0 < 20:
            time.sleep(0.05)
        assert sid != r3["scan_id"]
        print("[capacity-rollups] OK  용량=마지막 완료(1TB, 최신 멈춤 7B 아님)·정리 시 보호·처리량=진행 중만·"
              "다른 FS 200TB·빈 경로 새 스캔 거부(allow_empty 로 허용)")
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- C. 보안
def check_login_lockout_per_client() -> None:
    import threading
    from isilon_usage import auth
    g = auth.AuthGuard(lambda: "admin", ttl=100, max_fails=3, lockout=100)
    for _ in range(3):
        g.login("x", "6.6.6.6")
    assert g.login("admin", "6.6.6.6").get("locked_out")          # 공격자 IP 는 잠김
    assert g.login("admin", "10.0.0.5").get("ok")                  # 다른 운영자는 그대로 로그인
    # 동시 시도: 검증이 느린 동안 같은 IP 의 병렬 요청은 max_fails 개까지만 검증에 들어간다
    stored = auth.hash_password("right", iterations=200000)
    g2 = auth.AuthGuard(lambda: stored, ttl=100, max_fails=3, lockout=100)
    entered = []
    orig = auth.verify_password

    def slow(pw, st):
        entered.append(1)
        time.sleep(0.2)
        return orig(pw, st)
    auth.verify_password = slow
    try:
        ths = [threading.Thread(target=g2.login, args=("bad", "7.7.7.7")) for _ in range(20)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
    finally:
        auth.verify_password = orig
    assert len(entered) <= 3, "동시 시도 %d개가 검증에 들어감(한도 3)" % len(entered)
    assert g2.login("right", "7.7.7.7").get("locked_out")
    print("[login-lockout] OK  잠금은 IP별(다른 운영자 영향 없음)·병렬 시도 %d/20 만 검증" % len(entered))


def check_password_change_revokes() -> None:
    from isilon_usage.server import ScanController
    from isilon_usage import portal as P
    from isilon_usage import auth
    d = tempfile.mkdtemp(prefix="iu_c2_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        c = ScanController(os.path.join(d, "edge"))
        assert c.setup_op_password("first1").get("ok")
        old = c.unlock("first1", "1.1.1.1")["token"]
        other = c.unlock("first1", "2.2.2.2")["token"]
        assert c.op_token_valid(old) and c.op_token_valid(other)
        r = c.update_settings({"op_lock": True, "op_password": "second2"})
        assert r.get("ok") and r.get("token"), r
        assert not c.op_token_valid(old) and not c.op_token_valid(other), "옛 세션이 살아 있음"
        assert c.op_token_valid(r["token"])
        # 포탈 푸시(api_token 경로)도 옛 세션 만료
        t3 = c.unlock("second2", "1.1.1.1")["token"]
        c.set_op_password("third3")
        assert not c.op_token_valid(t3)
        # 암호화 체크만 켬 → 실제로 해시 저장(플래그와 저장 형식 일치)
        assert not auth.is_hashed(c.settings["op_password"])
        c.update_settings({"op_password_encrypted": True})
        assert auth.is_hashed(c.settings["op_password"]) and c.settings["op_password_encrypted"]
        assert c.unlock("third3", "1.1.1.1").get("ok")
        # 해시 저장 중 암호화 끔 → 되돌릴 수 없으니 플래그는 '암호화됨' 유지
        c.update_settings({"op_password_encrypted": False})
        assert c.settings["op_password_encrypted"] is True
        # 포탈
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.set_password("pp1111", False)
        pt = pc.login("pp1111", "1.1.1.1")["token"]
        r = pc.set_password("pp2222", False)
        assert r.get("token") and not pc.token_valid(pt) and pc.token_valid(r["token"]), r
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[pw-change] OK  비밀번호 변경·푸시 시 옛 세션 만료(본인은 새 토큰)·암호화 플래그=실제 저장 형식")


def check_path_boundary_dotdot() -> None:
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="iu_c3_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        base = os.path.join(d, "base")
        deep = os.path.join(base, "a", "b", "c")
        os.makedirs(deep)
        os.makedirs(os.path.join(d, "secret"))
        os.makedirs(os.path.join(base, "a", "secret"))
        os.symlink(deep, os.path.join(base, "lnk"))
        c = ScanController(os.path.join(d, "edge"))
        c.settings["mount_bases"] = [base]
        # 커널 해석은 base/a/secret(허용 안)이지만, 어휘적으로 '..' 를 먼저 접으면 base 밖(d/secret)
        tricky = base + "/lnk/../../secret"
        assert os.path.realpath(tricky) == os.path.join(base, "a", "secret")
        lex = os.path.abspath(tricky)
        assert lex == os.path.join(d, "secret"), lex
        ok, _ = c.path_allowed(tricky)
        assert not ok, "어휘 정규화 시 base 밖인 경로가 허용됨"
        assert not c.browse_allowed(tricky)
        assert c.path_allowed(deep)[0] and c.browse_allowed(os.path.join(base, "lnk"))
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[path-dotdot] OK  '링크/../..' 로 허용 경로 밖을 가리키는 입력 거부")


def check_ssh_option_injection() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_c4_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        calls = []
        import subprocess as sp
        orig = sp.run
        sp.run = lambda cmd, **kw: calls.append((cmd, kw)) or sp.CompletedProcess(cmd, 0, b"")
        orig_which = P.shutil.which
        P.shutil.which = lambda n: "/usr/bin/" + n
        try:
            r = pc._ssh_run({"host": "10.0.0.1", "ssh_user": "-oProxyCommand=touch /tmp/x"}, "true")
            assert not r.get("ok") and not calls, r
            r = pc._ssh_run({"host": "-oProxyCommand=x", "ssh_user": "root"}, "true")
            assert not r.get("ok") and not calls, r
            r = pc._ssh_run({"host": "10.0.0.1", "ssh_user": "root", "ssh_password": "S3cr3t"}, "true")
            assert r.get("ok"), r
            cmd, kw = calls[-1]
            assert "S3cr3t" not in " ".join(cmd), "비밀번호가 명령줄에 노출"
            assert kw.get("env", {}).get("SSHPASS") == "S3cr3t"
            assert cmd[cmd.index("--") + 1] == "10.0.0.1"
        finally:
            sp.run = orig
            P.shutil.which = orig_which
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[ssh-args] OK  옵션처럼 생긴 user/host 거부·'--' 뒤 목적지·비밀번호는 환경변수")


def check_xss_escapes() -> None:
    """HTML 에 끼워 넣는 엣지/서버 값이 이스케이프되는지(정적 검사)."""
    import re
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "isilon_usage")
    ph = open(os.path.join(here, "portal.html"), encoding="utf-8").read()
    dh = open(os.path.join(here, "dashboard.html"), encoding="utf-8").read()
    assert 'return _he(r.status||"")' in ph
    assert not re.search(r'">\$\{n\.id\}', ph), "노드 id 가 이스케이프 없이 <option> 에 들어감"
    assert "a.nodes.online:" not in ph and "a.capacity.used_pct+" not in ph
    bad = [ln for ln in dh.split("\n")
           if "innerHTML" in ln and re.search(r"\+ ?\(?(?:\(j && )?j\.(?:error|reason)", ln)]
    assert not bad, "이스케이프 없는 오류 메시지 innerHTML: %s" % bad[:3]
    print("[xss] OK  포탈 상태·노드 id·스토리지 수치, 대시보드 오류 메시지 이스케이프")


# ---------------------------------------------------------------- D. 운영 안정성·기타
def check_replica_prune_and_pick() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_d1_")
    try:
        dest = os.path.join(d, "rep")
        os.makedirs(os.path.join(dest, "scans"))
        scans = []
        for i in range(8):
            fn = "s%d.db" % i
            open(os.path.join(dest, "scans", fn), "w").close()
            scans.append({"root_path": "/data", "db_filename": fn, "finished_at": 1000 + i,
                          "status": "done" if i == 1 else "paused"})
        kept = P.PortalController._prune_replica_scans(dest, scans, 3)
        names = sorted(k["db_filename"] for k in kept)
        assert names == ["s1.db", "s6.db", "s7.db"], names      # 최신 2 + 최신 완료(s1)
        assert sorted(os.listdir(os.path.join(dest, "scans"))) == names
        # 비교용 DB 선택: 더 최신인 일시정지(s7)보다 완료(s1)를 고른다
        pc = P.PortalController(os.path.join(d, "hq"))
        rep = os.path.join(pc.replicas_dir, "n1")
        shutil.copytree(dest, rep)
        import json
        json.dump({"scans": kept}, open(os.path.join(rep, "meta.json"), "w"))
        got = pc._replica_db_for("n1", "/data/x")
        assert got and got.endswith("s1.db"), got
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[replica] OK  루트별 보관(최신 완료는 항상 유지)·비교는 완료 스캔 우선")


def check_overview_bad_meta() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_d2_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "good", "url": "http://10.0.0.1:8765", "token": "t"})
        pc.upsert_node({"id": "bad", "url": "http://10.0.0.2:8765", "token": "t"})
        metas = {
            "http://10.0.0.1:8765": {"version": "1.99.41", "overall": {
                "total_capacity_bytes": 100, "root_count": 1,
                "roots": [{"root_path": "/a", "capacity_bytes": 100, "status": "done"}]}},
            "http://10.0.0.2:8765": {"version": "x", "overall": {
                "total_capacity_bytes": "많음", "root_count": "x", "active_scans": None,
                "roots": [None, "str", {"root_path": 5, "capacity_bytes": "abc",
                                        "finished_at": "어제", "status": "discovering",
                                        "started_at": "?", "fs_total_bytes": {}}]}},
        }
        orig = P._probe_meta
        P._probe_meta = lambda url, tok=None, **kw: metas[url.rstrip("/")]
        try:
            pc._poll_one("good")
            pc._poll_one("bad")
        finally:
            P._probe_meta = orig
        ov = pc.overview()
        assert ov["ok"] and ov["totals"]["used_bytes"] == 100, ov["totals"]
        assert pc.usage_export()["ok"] and pc.throughput_windows() is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[bad-meta] OK  엣지 한 대의 이상한 meta 가 포탈 overview/usage/처리량을 멈추지 않음")


def check_schedule_merge_and_month_end() -> None:
    import datetime as _d
    from isilon_usage.server import ScanController
    from isilon_usage import settings as S
    d = tempfile.mkdtemp(prefix="iu_d3_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        os.makedirs(os.path.join(d, "t"))
        c = ScanController(os.path.join(d, "edge"))
        sc = {"paths": [os.path.join(d, "t")], "unit": "hour", "every": 6, "enabled": True}
        c.update_settings({"schedules": [sc]})
        live = dict(c.settings["schedules"][0])
        live.update({"last_run": time.time() - 60, "chain_i": 1, "chain_scan_id": 7, "chain_resumes": 2})
        c._save_settings(dict(c.settings, schedules=[live]))
        stale = dict(sc, last_run=0, chain_i=0, chain_scan_id=None, enabled=False)   # 화면이 들고 있던 옛 값
        c.update_settings({"schedules": [stale]})
        got = c.settings["schedules"][0]
        assert got["chain_scan_id"] == 7 and got["chain_i"] == 1 and got["last_run"] > 0, got
        assert got["enabled"] is False                       # 사용자가 바꾼 값은 반영
        # 31일 지정 월 예약 → 2월엔 말일(28일)에 돈다
        mo = {"unit": "month", "every": 1, "at": "09:00", "start_day": 31, "start_month": 1,
              "anchor": _d.datetime(2026, 12, 1).timestamp(), "last_run": 0}
        assert S.schedule_due(mo, _d.datetime(2027, 2, 28, 10, 0).timestamp())
        assert not S.schedule_due(mo, _d.datetime(2027, 2, 27, 10, 0).timestamp())
        assert S.schedule_due(mo, _d.datetime(2027, 3, 31, 10, 0).timestamp())
        # 예약 하나의 오류가 다른 예약을 막지 않는다
        os.makedirs(os.path.join(d, "u"))
        c.settings["schedules"] = [{"paths": [os.path.join(d, "t")], "unit": "bogus", "every": "x"},
                                   {"paths": [os.path.join(d, "u")], "unit": "minute", "every": 1,
                                    "last_run": 0, "enabled": True}]
        started = []
        c._chain_start = lambda sc, path, running, rp: started.append(path) or 99
        c._check_schedules()
        assert os.path.join(d, "u") in started, started
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[sched] OK  설정 저장이 예약 진행 상태를 되돌리지 않음·31일→말일·예약별 오류 격리")


def check_node_partial_update() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_d4_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "token": "T1", "region": "서울",
                        "alias_local": "/mnt/a", "alias_logical": "/data", "unit": "hour",
                        "every": 6, "mode": "poll", "enabled": False})
        r = pc.enroll_node({"url": "http://10.0.0.1:8765", "token": "T2", "id": "e1"})
        assert r.get("ok"), r
        n = pc._get("e1")
        assert (n["unit"], n["every"], n["mode"], n["enabled"], n["region"], n["alias_logical"]) == \
            ("hour", 6, "poll", False, "서울", "/data"), n
        assert n["token"] == "T2"
        pc.import_nodes_csv("id,url,region\ne1,http://10.0.0.1:8765,\n")
        n = pc._get("e1")
        assert n["region"] == "서울" and n["unit"] == "hour" and n["token"] == "T2", n
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[node-merge] OK  재등록·CSV 갱신이 운영자가 바꾼 주기·모드·지역·별칭을 지우지 않음")


def check_cli_defaults_and_exit() -> None:
    from isilon_usage import cli
    a = cli.build_parser().parse_args(["serve", "--data-dir", "/tmp/x"])
    init = cli._initial_settings(a, [])
    assert "fold_depth" not in init, init                 # 없는 옵션 → settings 기본값(4) 사용
    d = tempfile.mkdtemp(prefix="iu_d5_")
    try:
        _mk(os.path.join(d, "t"), ndirs=3)
        env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        r = subprocess.run([sys.executable, "-m", "isilon_usage", "scan", os.path.join(d, "t"),
                            "--data-dir", os.path.join(d, "dd"), "--min-free-gb", "100000000"],
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert r.returncode == 3, (r.returncode, r.stderr.decode()[-300:])   # 디스크 가드로 일시정지 → 3
        r = subprocess.run([sys.executable, "-m", "isilon_usage", "scan", os.path.join(d, "t"),
                            "--data-dir", os.path.join(d, "dd")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert r.returncode == 0, r.stderr.decode()[-300:]
        r = subprocess.run([sys.executable, "-m", "isilon_usage", "resume", "2",
                            "--data-dir", os.path.join(d, "dd")], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert r.returncode == 2 and "완료" in r.stderr.decode(), r.stderr.decode()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[cli] OK  serve 첫 설치 fold 기본값 유지·일시정지=종료코드 3·완료 스캔 resume 거부")


def check_settings_corrupt_and_race() -> None:
    import threading
    from isilon_usage import settings as S
    d = tempfile.mkdtemp(prefix="iu_d6_")
    try:
        S.save(d, dict(S.DEFAULTS, api_token="KEEP"))
        S.save(d, dict(S.DEFAULTS, api_token="KEEP", batch_size=777))
        with open(S.settings_path(d), "w") as fh:
            fh.write('{"api_token": "KE')                     # 쓰다 만(깨진) 파일
        got = S.load(d)
        assert got["api_token"] == "KEEP", "깨진 설정을 빈 기본값으로 읽음"
        errs = []

        def w(i):
            try:
                for _ in range(30):
                    S.save(d, dict(S.DEFAULTS, api_token="T%d" % i))
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        ths = [threading.Thread(target=w, args=(i,)) for i in range(6)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        assert not errs, errs[:2]
        import json
        json.load(open(S.settings_path(d)))                    # 동시 저장 후에도 온전한 JSON
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[settings] OK  깨진 설정은 .bak 으로 복구·동시 저장에도 파일 온전")


def check_bad_request_bodies() -> None:
    import io
    from isilon_usage import server as SV

    class H:
        def __init__(self, body, headers):
            self.rfile = io.BytesIO(body)
            self.headers = headers
    ok = SV.read_json_body(H(b'{"a":1}', {"Content-Length": "7"}))
    assert ok == ({"a": 1}, None), ok
    cases = [
        (b"[1,2]", {"Content-Length": "5"}, 400),
        (b'{"a":', {"Content-Length": "50"}, 400),
        (b"", {"Transfer-Encoding": "chunked"}, 411),
        (b"", {"Content-Length": str(SV.MAX_JSON_BODY + 1)}, 413),
        (b'{"p":"/x\\u0000y"}', {"Content-Length": "17"}, 400),
        (b"xx", {"Content-Length": "-3"}, 400),
    ]
    for body, hd, st in cases:
        _b, err = SV.read_json_body(H(body, hd))
        assert err and err[0] == st, (body, hd, err)
    h = SV.DashboardHandler.__new__(SV.DashboardHandler) if hasattr(SV, "DashboardHandler") else None
    qi = (SV.DashboardHandler._query_int if h is not None else None)
    if qi is not None:
        assert qi(h, {"scan": ["99999999999999999999999"]}, "scan") is None
    print("[bad-body] OK  배열·잘린·chunked·초과·NUL·음수 길이 본문 → 400/411/413, 거대 정수 쿼리 무시")


def check_audit_records_result() -> None:
    import json
    import threading
    import urllib.request
    from isilon_usage.server import serve
    from isilon_usage import audit
    d = tempfile.mkdtemp(prefix="iu_d7_")
    cwd = os.getcwd()
    os.chdir(d)
    httpd = serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                  initial_settings={"mount_bases": [d]}, enable_scan=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def post(path, body):
            rq = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                        headers={"Content-Type": "application/json"}, method="POST")
            try:
                return urllib.request.urlopen(rq).status
            except urllib.error.HTTPError as e:
                return e.code
        post("/api/scan/start", {"path": "/definitely/not/here"})
        post("/api/no-such-endpoint", {})
        ev = audit.tail(os.path.join(d, "data"), 50)
        st = [e for e in ev if e["action"] == "/api/scan/start"]
        assert st and st[0]["ok"] is False and st[0]["detail"], ev
        assert not [e for e in ev if e["action"] == "/api/no-such-endpoint"], ev
        # 회전·길이 제한
        ad = os.path.join(d, "aud")
        audit.record(ad, action="x", ok=True, detail="y" * 5000)
        assert len(audit.tail(ad, 1)[0]["detail"]) <= audit.MAX_FIELD
        old = audit.MAX_BYTES
        audit.MAX_BYTES = 100
        try:
            for i in range(30):
                audit.record(ad, action="a%d" % i, ok=True, detail="z" * 80)
        finally:
            audit.MAX_BYTES = old
        assert os.path.exists(audit.audit_path(ad) + ".3")
    finally:
        httpd.shutdown()
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[audit] OK  실패 작업은 실패로·없는 경로는 기록 안 함·길이 제한·여러 세대 회전")


def check_misc_portal_and_upgrade() -> None:
    from isilon_usage import portal as P
    from isilon_usage import upgrade as U
    from isilon_usage import coldtier
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "isilon_usage")
    ph = open(os.path.join(here, "portal.html"), encoding="utf-8").read()
    assert '(n.region||"(미지정)")===regionFilter' in ph and "_verCmp(n.version, j.version) < 0" in ph
    d = tempfile.mkdtemp(prefix="iu_d8_")
    try:
        # 감시 폴더: 깨진 최신 아카이브는 건너뛰고 그 아래 정상 새 버전을 고른다
        wd = os.path.join(d, "w")
        os.makedirs(wd)
        for v in ("9.0.1", "9.0.2"):
            open(os.path.join(wd, "isilon_usage-%s.tar.gz" % v), "w").close()
        bad = {U.archive_sig(os.path.join(wd, "isilon_usage-9.0.2.tar.gz"))}
        f = U.find_newer_archive(wd, "1.0.0", skip=bad)
        assert f and f[0].endswith("9.0.1.tar.gz"), f
        # 업그레이드 백업 정리(최신 3개)
        cd = os.path.join(d, "code")
        for ts in range(1, 7):
            os.makedirs(os.path.join(cd, "isilon_usage.bak.%d" % ts))
        U.prune_package_backups(cd, keep=3)
        assert sorted(os.listdir(cd)) == ["isilon_usage.bak.4", "isilon_usage.bak.5", "isilon_usage.bak.6"]
        # 복제 tar 의 이름 속 '..' 는 정상 파일
        pc = P.PortalController(os.path.join(d, "hq"))
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for nm, data in (("scans/a..b.db", b"x"), ("meta.json", b'{"scans":[{"db_filename":"a..b.db",'
                                                                     b'"root_path":"/d","finished_at":5,"status":"done"}]}')):
                ti = tarfile.TarInfo(nm)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        orig = P._http_get
        P._http_get = lambda url, tok=None, timeout=0: io.BytesIO(buf.getvalue())
        try:
            pc._replicate({"id": "n1", "url": "http://x", "token": "t"})
        finally:
            P._http_get = orig
        assert os.path.exists(os.path.join(pc.replicas_dir, "n1", "scans", "a..b.db"))
        # 연결 테스트는 편집 중인 URL 을 쓴다
        pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "token": "T"})
        seen = []
        orig = P._probe_meta
        P._probe_meta = lambda url, tok=None, **kw: seen.append((url, tok)) or {"overall": {}}
        try:
            pc.test_node({"id": "e1", "url": "http://10.0.0.9:8765"})
            pc.test_node({"id": "e1", "url": "http://10.0.0.1:8765"})
        finally:
            P._probe_meta = orig
        assert seen[0] == ("http://10.0.0.9:8765", None) and seen[1] == ("http://10.0.0.1:8765", "T"), seen
        # 자동 백업 실패 백오프
        calls = []
        pc.settings["backup_dir"] = os.path.join(d, "bk")
        pc.save_backup = lambda bdir: calls.append(1) or {"ok": False, "reason": "권한"}
        now = time.time()
        pc._maybe_scheduled_backup(now)
        pc._maybe_scheduled_backup(now + 60)
        assert len(calls) == 1, calls
        # 콜드티어 미리보기: 루트 dev 와 다른 하위 디렉터리는 넘지 않음(st_dev 위장)
        root = os.path.join(d, "cold")
        os.makedirs(os.path.join(root, "same"))
        os.makedirs(os.path.join(root, "other"))
        for sub in ("same", "other"):
            fp = os.path.join(root, sub, "f")
            with open(fp, "wb") as fh:
                fh.write(b"x" * 1000)
            os.utime(fp, (1, 1))
        real_lstat = os.lstat

        class _St:
            def __init__(self, st, dev):
                self._st, self.st_dev = st, dev

            def __getattr__(self, k):
                return getattr(self._st, k)
        coldtier.os.lstat = lambda p, *a, **k: (_St(real_lstat(p), -1) if p.rstrip("/").endswith("other")
                                                else real_lstat(p))
        try:
            r = coldtier.plan_cold_move(root, days=1)
        finally:
            coldtier.os.lstat = real_lstat
        assert r["ok"] and r["cold_file_count"] == 1, r.get("cold_file_count")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[misc] OK  지역 필터·버전 비교·감시 폴더 실패 건너뛰기·백업 정리·'..' 이름·편집 URL 테스트·백업 백오프·콜드 -xdev")


SECTIONS = [
    check_root_probe_false_positive,
    check_real_mount_loss,
    check_schedule_guard_pause_no_pileup,
    check_schedule_orphan_resume,
    check_resume_stale_active,
    check_ext_stats_resume,
    check_pscan_hardlinks_and_xdev,
    check_stats_journal_crash,
    check_pause_counters_and_resume_clock,
    check_fold_error_counts,
    check_du_apparent_and_pua_names,
    check_capacity_prune_and_portal_rollups,
    check_login_lockout_per_client,
    check_password_change_revokes,
    check_path_boundary_dotdot,
    check_ssh_option_injection,
    check_xss_escapes,
    check_replica_prune_and_pick,
    check_overview_bad_meta,
    check_schedule_merge_and_month_end,
    check_node_partial_update,
    check_cli_defaults_and_exit,
    check_settings_corrupt_and_race,
    check_bad_request_bodies,
    check_audit_records_result,
    check_misc_portal_and_upgrade,
]


def main() -> int:
    only = sys.argv[1:]
    for fn in SECTIONS:
        if only and fn.__name__ not in only:
            continue
        fn()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

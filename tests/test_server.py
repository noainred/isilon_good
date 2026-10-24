"""웹 서버 API 통합 테스트.

serve() 로 실제 HTTP 서버를 띄우고 주요 엔드포인트를 호출해 검증한다:
스캔 시작/상태, 설정 저장, 폴더 탐색, 검색, 오류, 비교(diff), 내보내기,
정리(prune)/삭제, 경로 제한 거부.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage.server import serve, _warn_if_insecure  # noqa: E402


class _StubCtrl:
    def __init__(self, settings):
        self.settings = settings


def _check_insecure_warning() -> None:
    """비루프백+무인증일 때만 stderr 경고가 나오는지 검증."""
    import contextlib

    def warn(host, settings):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _warn_if_insecure(host, _StubCtrl(settings))
        return buf.getvalue()

    # 0.0.0.0 + 비번 없음 → 경고
    out = warn("0.0.0.0", {})
    assert "보안 경고" in out and "127.0.0.1" in out, out
    # 루프백 → 경고 없음
    assert warn("127.0.0.1", {}) == ""
    assert warn("localhost", {}) == ""
    # 비번 설정됨 → 경고 없음(외부 노출이라도 인증 있음)
    assert warn("0.0.0.0", {"op_password": "pw"}) == ""
    print("[server] OK  비보안 기동 경고")


def _make_tree(root: str) -> None:
    os.makedirs(os.path.join(root, "sub1", "deep"), exist_ok=True)
    os.makedirs(os.path.join(root, "sub2"), exist_ok=True)
    for rel, n in [("a.bin", 4000), ("sub1/b.bin", 5000),
                   ("sub1/deep/c.bin", 6000), ("sub2/d.bin", 1000)]:
        with open(os.path.join(root, rel), "wb") as fh:
            fh.write(b"\0" * n)


class Client:
    def __init__(self, base):
        self.base = base
        self.op_token = ""      # 로그인 토큰(설정·되돌릴 수 없는 작업용) — 있으면 POST 에 자동으로 붙인다

    def get(self, path):
        try:
            return json.load(urllib.request.urlopen(self.base + path, timeout=5))
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def get_raw(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5).read()

    def post(self, path, data, headers=None, want_status=False):
        h = {"Content-Type": "application/json"}
        if self.op_token:
            h["X-Op-Token"] = self.op_token
        h.update(headers or {})
        req = urllib.request.Request(
            self.base + path, data=json.dumps(data).encode(),
            headers=h, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=10)
            j = json.load(r)
            return (j, r.getcode()) if want_status else j
        except urllib.error.HTTPError as e:
            j = json.loads(e.read())
            return (j, e.code) if want_status else j


def _wait_done(client, scan_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/status?scan={scan_id}")
        if d.get("ok") and d["run"]["status"] in ("done", "error", "paused"):
            return d
        time.sleep(0.2)
    raise AssertionError("스캔이 시간 내에 끝나지 않음")


def _check_resume_after_upgrade() -> None:
    """업그레이드 재시작 ↔ 진행 중 스캔 자동 재개: 마커 기록/판독/재개·1회성 글루 검증."""
    import shutil

    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_resume_")
    try:
        c = ScanController(os.path.join(d, "data"))
        marker = os.path.join(c.data_dir, c._RESUME_MARK)
        # 돌던 스캔이 있으면 그 id 를 마커에 기록한다.
        c.running_ids = lambda: [5, 7]                                   # type: ignore[assignment]
        c._mark_running_scans_for_resume()
        with open(marker, encoding="utf-8") as fh:
            assert json.load(fh)["scan_ids"] == [5, 7]
        # 시작 시 자동 재개 → resume_scan 호출 + 마커 삭제(1회성).
        resumed = []
        c.resume_scan = lambda sid: (resumed.append(sid) or {"ok": True})  # type: ignore[assignment]
        c._resume_after_upgrade()
        assert resumed == [5, 7], resumed
        assert not os.path.exists(marker)               # 다음 재시작에서 반복 재개 안 함
        c._resume_after_upgrade()                        # 마커 없으면 아무 일도 없음
        assert resumed == [5, 7], resumed
        # 돌던 스캔이 없으면 마커를 만들지 않고, 남아있던 마커도 지운다.
        with open(marker, "w") as fh:
            fh.write("{}")
        c.running_ids = lambda: []                                       # type: ignore[assignment]
        c._mark_running_scans_for_resume()
        assert not os.path.exists(marker)
        print("[resume-after-upgrade] OK  돌던 스캔 표시→재시작 후 자동 재개·1회성·빈상태 처리")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_resume_real() -> None:
    """업그레이드 후 '실제' 자동 재개 — 모킹 없이 새 컨트롤러(=재시작)가 스캔을 정말 잇는지 검증.

    threads(부분 재개)와 pscan(per-run DB 없어도 재스캔)·재개 결과의 화면 노출(last_resume)까지 확인.
    """
    import shutil
    import time as _t

    from isilon_usage import db as dbmod
    from isilon_usage import manager as mgrmod
    from isilon_usage.server import ScanController

    def _mk_tree(base):
        os.makedirs(os.path.join(base, "sub"))
        for i in range(12):
            with open(os.path.join(base, "f%d.bin" % i), "wb") as fh:
                fh.write(b"x" * 800)
            with open(os.path.join(base, "sub", "g%d.bin" % i), "wb") as fh:
                fh.write(b"y" * 800)

    def _wait_status(data, sid, want, timeout=20):
        for _ in range(int(timeout / 0.1)):
            mc = dbmod.connect(mgrmod.manager_db_path(data))
            row = mgrmod.get_scan(mc, sid); mc.close()
            if row and row["status"] in want:
                return row
            _t.sleep(0.1)
        return row

    for engine, drop_db in (("threads", False), ("pscan", True)):
        d = tempfile.mkdtemp(prefix="isilon_resume_real_")
        try:
            tree = os.path.join(d, "tree"); _mk_tree(tree)
            data = os.path.join(d, "data")
            a = ScanController(data)
            r = a.start_scan(tree, engine=engine)
            sid = r["scan_id"]
            row = _wait_status(data, sid, ("done", "error"))
            assert row["status"] == "done", (engine, row["status"])
            # '업그레이드 직전 진행 중'을 흉내: 매니저 상태를 discovering 으로 되돌리고 마커 작성.
            mc = dbmod.connect(mgrmod.manager_db_path(data))
            mgrmod.update_scan(mc, sid, status="discovering", phase="discovering")
            mc.commit(); mc.close()
            if drop_db:                       # pscan: per-run DB 가 없어도 재개(재스캔)돼야 한다(버그B).
                try:
                    os.remove(row["db_path"])
                except OSError:
                    pass
            with open(os.path.join(a.data_dir, ScanController._RESUME_MARK), "w") as fh:
                json.dump({"scan_ids": [sid]}, fh)
            # 재시작 = 같은 data-dir 새 컨트롤러 → reconcile → _resume_after_upgrade 가 실제 재개.
            b = ScanController(data)
            assert not os.path.exists(os.path.join(b.data_dir, ScanController._RESUME_MARK))
            lr = b.upgrade_status().get("last_resume") or {}
            res0 = (lr.get("results") or [{}])[0]
            assert res0.get("ok"), (engine, "재개 실패", res0)          # 재개가 ok 여야 함
            row2 = _wait_status(data, sid, ("done", "error"))
            assert row2["status"] == "done", (engine, "재개 후 미완료", row2["status"])
            if drop_db:
                assert os.path.exists(row["db_path"]), "pscan 재개 시 per-run DB 재생성돼야"
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("[resume-real] OK  업그레이드 후 실제 재개(threads 이어서·pscan DB없어도 재스캔)·last_resume 노출")


def _check_persistence_guard() -> None:
    """영속화 가드: 재부팅 마운트 누락(빈 data-dir)으로 새 초기화하는 사고를 막는지 검증."""
    import shutil

    from isilon_usage import settings as setmod
    d = tempfile.mkdtemp(prefix="iu_persist_")
    try:
        data = os.path.join(d, "data"); code = os.path.join(d, "code")
        os.makedirs(data); os.makedirs(code)
        proxies = ("settings.json", "manager.db")
        # 표식 없으면(첫 설치) 통과한다.
        ok, _ = setmod.check_persistence(data, code, proxies)
        assert ok
        # 데이터가 없으면 표식을 남기지 않는다(첫 실행 보호).
        setmod.mark_initialized(data, code, proxies)
        ok, _ = setmod.check_persistence(data, code, proxies)
        assert ok
        # 데이터가 생기면 표식을 남긴다 → 데이터 있는 한 통과.
        with open(os.path.join(data, "settings.json"), "w") as fh:
            fh.write("{}")
        setmod.mark_initialized(data, code, proxies, {"role": "edge"})
        ok, _ = setmod.check_persistence(data, code, proxies)
        assert ok
        # 마운트 누락 흉내: 표식은 있는데 data-dir 가 비었다 → 시작 차단(새 초기화 방지).
        os.remove(os.path.join(data, "settings.json"))
        ok, why = setmod.check_persistence(data, code, proxies)
        assert (not ok) and "마운트" in why, (ok, why)
        # manager.db 만 있어도 데이터 있음으로 보고 통과(오탐 방지).
        with open(os.path.join(data, "manager.db"), "w") as fh:
            fh.write("x")
        ok, _ = setmod.check_persistence(data, code, proxies)
        assert ok
        # 표식의 data-dir 와 다른 폴더면 차단하지 않는다(설정 변경 오탐 방지).
        ok2, _ = setmod.check_persistence(os.path.join(d, "other"), code, proxies)
        assert ok2
        print("[persistence-guard] OK  빈 폴더 새초기화 차단·첫설치 통과·다른폴더 오탐없음")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_sequential_schedule() -> None:
    """순차 예약: 한 예약의 여러 경로를 A 끝나면 B 식으로 순서대로 실행한다."""
    import shutil

    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_chain_")
    try:
        c = ScanController(os.path.join(d, "data"))
        started = []
        ids = iter([101, 102, 103])

        from isilon_usage import settings as setmod

        def fake_start(path, **_kw):
            sid = next(ids)
            started.append((sid, path))
            # 실제 start_scan 처럼 last_scan 을 저장하며 self.settings 를 새 dict 로 교체한다
            # (예약 틱이 옛 객체에만 last_run/체인 상태를 쓰던 회귀 재현용)
            c.settings = setmod.save(c.data_dir, dict(c.settings, last_scan={"path": path}))
            return {"ok": True, "scan_id": sid}

        run = {"ids": set(), "status": {}}
        c.start_scan = fake_start                                          # type: ignore[assignment]
        c.scan_path_check = lambda p, confirm_outside=False: (True, None)   # type: ignore[assignment]
        c.running_ids = lambda: list(run["ids"])                           # type: ignore[assignment]
        c.running_paths = lambda: set()                                    # type: ignore[assignment]
        c._scan_status = lambda sid: run["status"].get(sid)                # type: ignore[assignment]
        # 분 단위·즉시 due 인 순차 예약(A→B)
        c.settings["schedules"] = [{
            "paths": ["/data/A", "/data/B"], "path": "/data/A",
            "unit": "minute", "every": 1, "last_run": 0,
            "backend": "native", "size_mode": "disk", "enabled": True,
        }]

        def sc():
            return c.settings["schedules"][0]

        # (1) 첫 점검: A 시작
        c._check_schedules()
        assert started == [(101, "/data/A")], started
        assert sc()["chain_scan_id"] == 101 and sc()["chain_i"] == 0
        run["ids"] = {101}
        # (2) A 진행 중에는 B 를 시작하지 않는다
        c._check_schedules()
        assert started == [(101, "/data/A")], "A 진행 중 B 조기 시작 금지"
        # (3) A 완료 → B 자동 시작
        run["ids"] = set(); run["status"][101] = "done"
        c._check_schedules()
        assert started == [(101, "/data/A"), (102, "/data/B")], started
        assert sc()["chain_scan_id"] == 102 and sc()["chain_i"] == 1
        run["ids"] = {102}
        # (4) B 완료 → 체인 종료(재시작·추가 시작 없음)
        run["ids"] = set(); run["status"][102] = "done"
        c._check_schedules()
        assert sc()["chain_scan_id"] is None and sc()["chain_i"] == 0
        c._check_schedules()                                  # 같은 주기 내 재시작 금지
        assert len(started) == 2, started
        # (5) 일시정지 단계는 자동 진행하지 않는다
        c.settings["schedules"] = [{
            "paths": ["/data/X", "/data/Y"], "unit": "minute", "every": 1,
            "last_run": 0, "enabled": True,
        }]
        c._check_schedules()                                  # X 시작(=103)
        xsid = sc()["chain_scan_id"]
        run["ids"] = set(); run["status"][xsid] = "paused"
        before = len(started)
        c._check_schedules()
        assert len(started) == before, "일시정지 단계에서 다음 경로 자동 시작 금지"
        print("[sequential-schedule] OK  A→B 순차 실행·조기시작 금지·완료 종료·일시정지 대기")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_email_notifications() -> None:
    """메일 알림: 다중 수신자 파싱 + 이벤트별 조건 게이트 + 중단 N분 미재시작 알림."""
    import shutil

    from isilon_usage import manager as mgrmod
    from isilon_usage import notify as notifymod
    from isilon_usage.server import ScanController

    # 1) 다중 수신자: 쉼표/세미콜론/공백/줄바꿈 섞여도 모두 To 로 합쳐진다(smtplib 모킹).
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): sent["host"] = host
        def starttls(self): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent["to"] = msg["To"]
        def quit(self): pass

    orig_smtp = notifymod.smtplib.SMTP
    notifymod.smtplib.SMTP = FakeSMTP
    try:
        ok = notifymod.send_email(
            {"notify_email": "a@x.com, b@y.com; c@z.com\n d@w.com",
             "smtp_host": "smtp.local", "smtp_port": 587, "smtp_tls": False},
            "subj", "body")
        assert ok, "발송 실패"
        assert sent["to"] == "a@x.com, b@y.com, c@z.com, d@w.com", sent.get("to")
        # 받는 사람 없으면 발송 안 함
        assert notifymod.send_email({"notify_email": "", "smtp_host": "h"}, "s", "b") is False
    finally:
        notifymod.smtplib.SMTP = orig_smtp

    d = tempfile.mkdtemp(prefix="isilon_mail_")
    orig_get = mgrmod.get_scan
    try:
        c = ScanController(os.path.join(d, "data"))
        os.makedirs(c.data_dir, exist_ok=True)
        calls = []
        c._email_scan = lambda sid, row, label: calls.append(label)  # type: ignore[assignment]

        def fake_row(status):
            return {"scanned_bytes": 0, "status": status, "root_path": "/p",
                    "hostname": "h", "total_dirs": 0, "total_files": 0}
        rowbox = {"row": None}
        mgrmod.get_scan = lambda conn, sid: rowbox["row"]   # type: ignore[assignment]

        # 완료만 켠 상태: done→발송, error/paused→무시
        c.settings.update({"notify_email": "x@y", "smtp_host": "h",
                           "notify_on_done": True, "notify_on_error": False,
                           "notify_on_stopped": False, "notify_on_stalled": False})
        for st in ("done", "error", "paused"):
            rowbox["row"] = fake_row(st); c._on_scan_finished(1)
        assert calls == ["스캔 완료"], calls

        # 장애 + 중단 켜기: error·paused 발송, done 무시
        calls.clear()
        c.settings.update({"notify_on_done": False, "notify_on_error": True,
                           "notify_on_stopped": True})
        for st in ("done", "error", "paused"):
            rowbox["row"] = fake_row(st); c._on_scan_finished(1)
        assert calls == ["스캔 오류(장애)", "스캔 중단"], calls

        # 중단 N분 미재시작 감시: 시간이 지났고 그대로 paused 면 알림
        calls.clear()
        c._scan_status = lambda sid: "paused"            # type: ignore[assignment]
        c.running_ids = lambda: []                       # type: ignore[assignment]
        c.running_paths = lambda: set()                  # type: ignore[assignment]
        rowbox["row"] = fake_row("paused")
        c._pause_watch = {42: {"due": time.time() - 1, "root": "/p"}}
        c._check_pause_watch(time.time())
        assert len(calls) == 1 and "재시작 없음" in calls[0], calls
        assert 42 not in c._pause_watch
        # 재시작(같은 루트로 스캔 중)이면 알림 안 함
        calls.clear()
        c.running_paths = lambda: {"/p"}                 # type: ignore[assignment]
        c._pause_watch = {43: {"due": time.time() - 1, "root": "/p"}}
        c._check_pause_watch(time.time())
        assert calls == [] and 43 not in c._pause_watch, calls
        print("[email-notify] OK  다중수신자·완료/장애/중단 조건 게이트·중단N분 미재시작 알림")
    finally:
        mgrmod.get_scan = orig_get
        shutil.rmtree(d, ignore_errors=True)


def _check_op_password() -> None:
    """포탈 푸시용 set_op_password: 설정/변경/해제 + 빈 비번 거부."""
    import shutil

    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_oppw_")
    try:
        c = ScanController(os.path.join(d, "data"))
        c._launch_cwd = d                                        # info.MD 를 저장소 폴더가 아닌 임시 폴더에
        os.makedirs(c.data_dir, exist_ok=True)
        assert not c.op_required()
        assert c.set_op_password("secret")["ok"]
        assert c.op_required()                                   # 설정됨
        assert c.set_op_password("", clear=False)["ok"] is False  # 빈 비번 거부
        assert c.op_required()                                   # 거부됐으니 그대로
        assert c.set_op_password("new-pass-2")["ok"]            # 변경
        assert c.op_required()
        assert c.set_op_password("", clear=True)["ok"]          # 해제
        assert not c.op_required()
        print("[op-password] OK  포탈 푸시용 설정/변경/해제·빈 비번 거부")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_auto_restart() -> None:
    """완료 후 자동 재시작(반복): start_scan 저장 + done 재시작 / 중지·오류 멈춤."""
    import shutil

    from isilon_usage import manager as mgrmod
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_loop_")
    orig_get = mgrmod.get_scan
    try:
        # 1) start_scan(auto_restart=True) 가 설정을 _auto_restart 에 등록(launch 는 모킹)
        c = ScanController(os.path.join(d, "data"))
        os.makedirs(c.data_dir, exist_ok=True)
        c._launch = lambda *a, **k: None          # type: ignore[assignment]
        c._launch_pscan = lambda *a, **k: None     # type: ignore[assignment]
        c.scan_path_check = lambda p, confirm_outside=False: (True, None)  # type: ignore[assignment]
        res = c.start_scan(d, auto_restart=True)
        assert res["ok"] and res["scan_id"] in c._auto_restart, (res, c._auto_restart)
        assert c._auto_restart[res["scan_id"]]["path"] == os.path.abspath(d)

        # 2) _on_scan_finished: done 이면 같은 설정으로 재시작(auto_restart=True 유지)
        calls = []
        c.start_scan = lambda path, **kw: (calls.append((path, kw.get("auto_restart"))) or {"ok": True, "scan_id": 999})  # type: ignore[assignment]
        rowbox = {"row": None}
        mgrmod.get_scan = lambda conn, sid: rowbox["row"]   # type: ignore[assignment]

        def fake_row(status):
            return {"scanned_bytes": 0, "status": status, "root_path": "/p", "hostname": "h",
                    "total_dirs": 0, "total_files": 0}
        sid = res["scan_id"]
        rowbox["row"] = fake_row("done")
        c._on_scan_finished(sid)
        assert calls and calls[-1][0] == os.path.abspath(d) and calls[-1][1] is True, calls
        assert sid not in c._auto_restart

        # 3) 중지(paused)·오류(error)면 재시작하지 않고 반복을 멈춘다
        for st in ("paused", "error"):
            calls.clear()
            c._auto_restart[42] = {"path": "/p", "backend": "native", "size_mode": "disk",
                                   "one_file_system": False, "engine": "threads",
                                   "processes": None, "threads": None}
            rowbox["row"] = fake_row(st)
            c._on_scan_finished(42)
            assert calls == [] and 42 not in c._auto_restart, (st, calls)

        # 4) 영속화 — 반복 설정이 디스크에 남아 '재시작'(새 컨트롤러)에도 복원된다.
        #    (메모리 dict 만으로는 재시작·업그레이드 후 반복이 끊겼다 → data-dir 에 저장/복원)
        mgrmod.get_scan = orig_get             # 이 구간은 실제 DB 행을 읽어 prune 한다
        import json as _json

        from isilon_usage import db as dbmod
        d2 = tempfile.mkdtemp(prefix="isilon_loop_persist_")
        try:
            a = ScanController(os.path.join(d2, "data"))
            os.makedirs(a.data_dir, exist_ok=True)
            a._launch = lambda *x, **k: None            # type: ignore[assignment]
            a._launch_pscan = lambda *x, **k: None       # type: ignore[assignment]
            a.scan_path_check = lambda p, confirm_outside=False: (True, None)  # type: ignore[assignment]
            r = a.start_scan(d2, auto_restart=True)
            sidp = r["scan_id"]
            mark = os.path.join(a.data_dir, ScanController._AUTO_RESTART_MARK)
            assert os.path.exists(mark), "반복 설정이 디스크에 저장되지 않음"
            assert str(sidp) in _json.load(open(mark, encoding="utf-8")), "마커에 scan_id 없음"

            # '재시작' = 같은 data-dir 로 새 컨트롤러. reconcile 이 discovering→paused 로 만들고,
            #  load 가 paused(재개 대상)를 복원해야 한다.
            b = ScanController(a.data_dir)
            assert sidp in b._auto_restart, ("재시작 후 반복 복원 실패", b._auto_restart)

            # 복원된 그 스캔이 done 으로 끝나면 반복이 이어진다(start_scan 모킹으로 호출만 확인)
            b._launch = lambda *x, **k: None             # type: ignore[assignment]
            b._launch_pscan = lambda *x, **k: None        # type: ignore[assignment]
            b.scan_path_check = lambda p, confirm_outside=False: (True, None)  # type: ignore[assignment]
            calls2 = []
            b.start_scan = lambda path, **kw: (calls2.append(kw.get("auto_restart")) or {"ok": True, "scan_id": 4242})  # type: ignore[assignment]
            mc = dbmod.connect(mgrmod.manager_db_path(b.data_dir))
            mgrmod.update_scan(mc, sidp, status="done"); mc.commit(); mc.close()
            b._on_scan_finished(sidp)
            assert calls2 == [True], ("재시작 후 반복이 이어지지 않음", calls2)

            # 5) 이미 끝난(done) 반복 항목은 로드 시 버린다(prune) — 좀비 반복 방지
            cfg = {"path": d2, "backend": "native", "size_mode": "disk",
                   "one_file_system": False, "engine": "threads",
                   "processes": None, "threads": None}
            with open(mark, "w", encoding="utf-8") as fh:
                _json.dump({str(sidp): cfg}, fh)         # sidp 는 지금 done 상태
            e = ScanController(a.data_dir)
            assert sidp not in e._auto_restart, "끝난(done) 반복이 복원됨(prune 실패)"
        finally:
            shutil.rmtree(d2, ignore_errors=True)
        print("[auto-restart] OK  설정 저장·done 재시작·중지/오류 멈춤·재시작 영속화/복원")
    finally:
        mgrmod.get_scan = orig_get
        shutil.rmtree(d, ignore_errors=True)


def _check_last_scan_remember() -> None:
    """start_scan 이 사용 파라미터를 settings.last_scan 에 기억한다(다음 시작 폼 복원용)."""
    import shutil

    from isilon_usage import settings as setmod
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_lastscan_")
    try:
        c = ScanController(os.path.join(d, "data"))
        os.makedirs(c.data_dir, exist_ok=True)
        c._launch = lambda *a, **k: None              # type: ignore[assignment]
        c._launch_pscan = lambda *a, **k: None         # type: ignore[assignment]
        c.scan_path_check = lambda p, confirm_outside=False: (True, None)  # type: ignore[assignment]
        res = c.start_scan(d, backend="du", size_mode="apparent",
                           one_file_system=True, engine="threads", auto_restart=False)
        assert res["ok"], res
        ls = setmod.load(c.data_dir).get("last_scan") or {}
        assert ls.get("path") == os.path.abspath(d), ls
        assert ls.get("backend") == "du" and ls.get("size_mode") == "apparent", ls
        assert ls.get("one_file_system") is True and ls.get("engine") == "threads", ls
        assert "workers" in ls and "ts" in ls, ls
        print("[server] start_scan → settings.last_scan 기억 OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_duplicate_scan_guard() -> None:
    """같은 루트 중복 스캔 방지 — 현장에서 같은 루트 스캔이 같은 초에 쌍으로 시작(#3/#4·#9/#10·#12/#13)해
    같은 트리를 두 번 걷고(처리량 반토막), 업그레이드 재개 때마다 둘 다 살아나 끝나지 않던 문제."""
    from isilon_usage.server import ScanController
    from isilon_usage import db as dbmod
    from isilon_usage import manager as mgrmod
    d = tempfile.mkdtemp(prefix="isilon_dup_")
    try:
        roots = {}
        for nm in ("A", "B", "C", "D", "E"):
            p = os.path.join(d, nm, "x")
            os.makedirs(p)
            with open(os.path.join(p, "f"), "wb") as fh:
                fh.write(b"x" * 10)
            roots[nm] = os.path.join(d, nm)
        c = ScanController(os.path.join(d, "data"))
        c.settings = dict(c.settings)
        c.settings["mount_bases"] = [d]

        # 실제 스캔 없이 '실행 중'으로 붙잡아 둔다(_scans 등록만) — 타이밍과 무관한 결정적 테스트
        def fake_launch(scan_id, db_path, path, *a, **kw):
            with c._lock:
                c._scans[scan_id] = {"stop": threading.Event(), "thread": None, "path": path}
        c._launch = fake_launch   # type: ignore[assignment]
        c._launch_pscan = lambda scan_id, db_path, path, *a, **kw: fake_launch(scan_id, db_path, path)  # type: ignore[assignment]

        def set_status(sid, status, updated_at=None):
            mc = dbmod.connect(mgrmod.manager_db_path(c.data_dir))
            try:
                mc.execute("UPDATE scans SET status=?, phase=?, updated_at=COALESCE(?, updated_at) "
                           "WHERE id=?", (status, status, updated_at, sid))
                mc.commit()
            finally:
                mc.close()

        # ① 같은 루트(끝 슬래시·엔진 달라도) 두 번째 시작 → 거부, 다른 루트 → 허용
        r1 = c.start_scan(roots["A"], engine="threads")
        r2 = c.start_scan(roots["A"] + "/", engine="pscan")
        r3 = c.start_scan(roots["B"], engine="threads")
        assert r1["ok"], r1
        assert not r2["ok"] and r2.get("duplicate") and r2["scan_id"] == r1["scan_id"], r2
        assert r3["ok"], r3
        # ② 동시 요청(더블클릭·포탈 중복 등록 노드) — 8개 스레드가 같은 새 루트를 동시에 → 정확히 1개만
        res = []
        ths = [threading.Thread(target=lambda: res.append(c.start_scan(roots["C"], engine="threads")))
               for _ in range(8)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        assert sum(1 for x in res if x.get("ok")) == 1, res
        # ③ 재개도 중복을 만들지 않는다: A 는 r1 이 도는 중 → A 의 옛 스캔 재개는 거부
        old = mgrmod.register_scan(c.data_dir, root_path=roots["A"],
                                   db_path=os.path.join(d, "old.db"), backend="pscan", size_mode="disk")
        set_status(old, "paused")
        rr = c.resume_scan(old)
        assert not rr["ok"] and rr.get("duplicate") and rr["scan_id"] == r1["scan_id"], rr
        # 첫 스캔이 끝나면 같은 루트를 다시 시작할 수 있다
        with c._lock:
            c._scans.pop(r1["scan_id"])
        set_status(r1["scan_id"], "done")
        assert c.start_scan(roots["A"], engine="threads")["ok"]
        # ④ 다른 프로세스가 돌리는 스캔(매니저 '진행 중' + 최신 하트비트, 이 프로세스엔 없음) → 거부,
        #    하트비트가 오래돼(죽은 스캔 잔여) 있으면 막지 않는다
        other = mgrmod.register_scan(c.data_dir, root_path=roots["D"],
                                     db_path=os.path.join(d, "o.db"), backend="native", size_mode="disk")
        set_status(other, "discovering", time.time())
        rd = c.start_scan(roots["D"], engine="threads")
        assert not rd["ok"] and rd["scan_id"] == other, rd
        set_status(other, "discovering", time.time() - 2 * c._DUP_STALE_SECS)
        assert c.start_scan(roots["D"], engine="threads")["ok"]
        # ⑤ 업그레이드 후 자동 재개: 같은 루트의 두 스캔(쌍)이 마커에 있으면 먼저 것만 재개 → 쌍 해소
        e1 = mgrmod.register_scan(c.data_dir, root_path=roots["E"],
                                  db_path=os.path.join(d, "e1.db"), backend="pscan", size_mode="disk")
        e2 = mgrmod.register_scan(c.data_dir, root_path=roots["E"],
                                  db_path=os.path.join(d, "e2.db"), backend="pscan", size_mode="disk")
        set_status(e1, "paused")
        set_status(e2, "paused")
        with open(os.path.join(c.data_dir, ScanController._RESUME_MARK), "w") as fh:
            json.dump({"scan_ids": [e1, e2], "ts": time.time()}, fh)
        c._resume_after_upgrade()
        run = c.running_ids()
        assert e1 in run and e2 not in run, (run, e1, e2)
        lr = {x["scan_id"]: x for x in (c.upgrade_status().get("last_resume") or {}).get("results", [])}
        assert lr[e1]["ok"] and not lr[e2]["ok"] and "중복" in (lr[e2]["reason"] or ""), lr
        print("[dup-guard] OK  같은 루트 중복 시작·동시 8요청·재개·타 프로세스·업그레이드 쌍 모두 1개만 실행")
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _check_troubleshoot_live() -> None:
    """진단 신뢰성(R9): 실행 중 스캔의 진단에 '실측' 블록이 붙는지 — 계측 락(대기/보유), 체크포인트 통계,
    마지막 전진 시각, 청크 나눔, 워커 구간에 'lock' 카테고리. pscan 은 진행 중에 파일 수/현재 경로가
    작업 단위 완료 전에도 올라오는지(주기 배치 보고) 확인한다.
    """
    import shutil
    import time as _t

    from isilon_usage import db as dbmod
    from isilon_usage import manager as mgrmod
    from isilon_usage import pscan as pscanmod
    from isilon_usage import scanner as scanmod
    from isilon_usage.server import ScanController

    def _wait_status(data, sid, want, timeout=60):
        row = None
        for _ in range(int(timeout / 0.1)):
            mc = dbmod.connect(mgrmod.manager_db_path(data))
            row = mgrmod.get_scan(mc, sid); mc.close()
            if row and row["status"] in want:
                return row
            _t.sleep(0.1)
        return row

    d = tempfile.mkdtemp(prefix="isilon_ts_live_")
    orig_stat = scanmod.Scanner._safe_stat
    orig_eb = pscanmod._entry_bytes
    try:
        tree = os.path.join(d, "tree")
        for a in range(6):
            sub = os.path.join(tree, "d%d" % a)
            os.makedirs(sub)
            for i in range(400):
                with open(os.path.join(sub, "f%03d.bin" % i), "wb") as fh:
                    fh.write(b"x" * 64)
        data = os.path.join(d, "data")
        ctl = ScanController(data)

        # 1) threads: stat 을 0.5ms 늦춰 스캔이 진단 표본보다 오래 돌게 한다
        def slow_stat(self, entry):
            _t.sleep(0.0005)
            return orig_stat(self, entry)
        scanmod.Scanner._safe_stat = slow_stat
        sid = ctl.start_scan(tree, engine="threads")["scan_id"]
        got = None
        for _ in range(100):
            j = ctl.troubleshoot(sample_sec=0.3)
            if j.get("running") and (j.get("scanner") or {}).get("lock"):
                got = j
                break
            _t.sleep(0.05)
        assert got is not None, "실행 중 진단에 스캐너 실측 블록이 없음"
        sc = got["scanner"]
        assert "acquisitions" in sc["lock"] and "hold_max_s" in sc["lock"], sc["lock"]
        assert "count" in sc["checkpoint"] and "busy_streak" in sc["checkpoint"], sc["checkpoint"]
        assert "sharing" in sc and "stat_us_ema" in sc, sc
        assert "lock" in got["workers"] and got["engine"] == "threads", (got["workers"], got["engine"])
        assert got.get("advance_age") is not None, got.get("advance_age")
        assert "lock_waiting_idents" not in sc, "JSON 직렬화 불가 필드가 남음"
        json.dumps(got)                                       # 전체가 JSON 으로 나가야 함
        row = _wait_status(data, sid, ("done", "error"))
        assert row["status"] == "done", row["status"]
        scanmod.Scanner._safe_stat = orig_stat

        # 2) pscan: 큰 디렉터리 하나(작업 단위 1개, 파일당 0.5ms → 약 3초)에서 단위가 끝나기 전에
        #    파일 수가 올라오는지(4,096파일 시점 보고 → ≥1초 간격 on_progress → 관리 DB).
        tree2 = os.path.join(d, "tree2", "big")
        os.makedirs(tree2)
        for i in range(6000):
            with open(os.path.join(tree2, "g%04d.bin" % i), "wb") as fh:
                fh.write(b"y" * 8)

        def slow_eb(st, size_mode):
            _t.sleep(0.0005)
            return orig_eb(st, size_mode)
        pscanmod._entry_bytes = slow_eb
        sid2 = ctl.start_scan(os.path.join(d, "tree2"), engine="pscan")["scan_id"]
        seen_partial = False
        for _ in range(300):
            mc = dbmod.connect(mgrmod.manager_db_path(data))
            row = mgrmod.get_scan(mc, sid2); mc.close()
            if row and row["status"] in ("done", "error"):
                break
            if row and row["status"] == "sizing" and (row["total_files"] or 0) > 0 \
                    and (row["processed_dirs"] or 0) < (row["total_dirs"] or 1):
                seen_partial = True
                break
            _t.sleep(0.05)
        row2 = _wait_status(data, sid2, ("done", "error"))
        assert row2["status"] == "done", (row2["status"], row2["error"])
        assert row2["total_files"] == 6000, row2["total_files"]
        assert seen_partial, "pscan 진행 중에 파일 수가 작업 단위 완료 전에 올라오지 않음"
        print("[troubleshoot-live] OK  threads 진단에 실측(락 %d회·체크포인트·전진·나눔) · pscan 진행 중 파일 수 배치 보고"
              % sc["lock"]["acquisitions"])
    finally:
        scanmod.Scanner._safe_stat = orig_stat
        pscanmod._entry_bytes = orig_eb
        shutil.rmtree(d, ignore_errors=True)


def _check_resume_keeps_start_options() -> None:
    """재개가 시작 옵션(one_file_system·fold_depth·max_depth)을 그대로 이어 쓴다
    (예전엔 one_file_system 이 항상 False, fold/max_depth 는 현재 설정값)."""
    import shutil
    import time as _t

    from isilon_usage import db as dbmod
    from isilon_usage import manager as mgrmod
    from isilon_usage import scanner as scanmod
    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_resume_opts_")
    orig_stat = scanmod.Scanner._safe_stat
    try:
        tree = os.path.join(d, "tree")
        for a_ in range(4):
            sub = os.path.join(tree, "d%d" % a_)
            os.makedirs(sub)
            for i in range(200):
                with open(os.path.join(sub, "f%03d" % i), "wb") as fh:
                    fh.write(b"x")
        data = os.path.join(d, "data")
        ctl = ScanController(data)

        def slow_stat(self, entry):
            _t.sleep(0.002)
            return orig_stat(self, entry)
        scanmod.Scanner._safe_stat = slow_stat
        sid = ctl.start_scan(tree, engine="threads", one_file_system=True, fold_depth=1)["scan_id"]
        _t.sleep(0.3)
        assert ctl.stop_scan(sid)["ok"]
        for _ in range(100):
            if sid not in ctl.running_ids():
                break
            _t.sleep(0.1)
        scanmod.Scanner._safe_stat = orig_stat
        ctl.settings["fold_depth"] = 7                  # 그 사이 설정이 바뀌어도 시작 값으로 재개해야 한다
        seen = {}

        def fake_launch(scan_id, db_path, path, backend, size_mode, one_file_system, **kw):
            seen.update(one_file_system=one_file_system, **kw)
        ctl._launch = fake_launch                       # type: ignore[assignment]
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        try:
            st = mgrmod.get_scan(mc, sid)["status"]
        finally:
            mc.close()
        assert st == "paused", st
        assert ctl.resume_scan(sid)["ok"]
        assert seen.get("one_file_system") is True and seen.get("fold_depth") == 1 \
            and seen.get("max_depth") == 0, seen
        print("[resume-opts] OK  재개가 시작 옵션(one_file_system·fold_depth·max_depth) 유지")
    finally:
        scanmod.Scanner._safe_stat = orig_stat
        shutil.rmtree(d, ignore_errors=True)


def _check_temp_password_and_reset() -> None:
    """관리 비밀번호: 기동 시 없으면 임시 비밀번호를 info.MD 에 생성 · 서버 명령(reset-password)으로 초기화하면
    실행 중 서버가 바로 반영(기존 로그인 만료) · 다른 저장이 옛 비밀번호로 되돌리지 않음 · 사용자 비밀번호로 바꾸면
    임시 해제 · 보호 해제는 다시 임시로 · info.MD 를 못 쓰면 비밀번호를 바꾸지 않음."""
    import re as _re
    import shutil
    import subprocess

    from isilon_usage import settings as setmod
    from isilon_usage.server import ScanController, reset_op_password

    def _pw_in(path):
        with open(path, encoding="utf-8") as fh:
            return _re.search(r"\| (?:임시 비밀번호\(서버가 자동 생성\)|작업 비밀번호) \| `([^`]+)` \|", fh.read()).group(1)

    d = tempfile.mkdtemp(prefix="isilon_tmppw_")
    try:
        data = os.path.join(d, "data")
        run_dir = os.path.join(d, "run")
        os.makedirs(run_dir)
        c = ScanController(data)
        c._launch_cwd = run_dir                       # 테스트가 저장소 폴더에 info.MD 를 남기지 않게
        assert not c.op_required()
        made = c.ensure_op_password()
        info = os.path.join(run_dir, "info.MD")
        assert made and made["info_path"] == info and os.path.exists(info), made
        assert oct(os.stat(info).st_mode & 0o777) == "0o600"
        pw1 = _pw_in(info)
        assert pw1 == made["password"] and c.op_required()
        ti = c.op_temp_info()
        assert ti == {"op_temp": True, "op_info_path": info}, ti
        assert c.ensure_op_password() is None         # 이미 있으면 다시 만들지 않는다
        tok1 = c.unlock(pw1)["token"]
        assert c.op_token_valid(tok1)

        # 서버 명령(별도 프로세스) 초기화 → 실행 중 컨트롤러가 다음 인증부터 반영, 기존 세션 만료
        stale = dict(c.settings)                      # (초기화 전 메모리 설정 — 아래 저장 회귀용)
        _t0 = os.stat(setmod.settings_path(data)).st_mtime_ns
        while os.stat(setmod.settings_path(data)).st_mtime_ns == _t0:   # mtime 해상도가 거친 FS 대비
            out = subprocess.run([sys.executable, "-m", "isilon_usage", "reset-password", "--data-dir", data],
                                 cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                                 capture_output=True, text=True, timeout=60)
            assert out.returncode == 0, out.stderr
        assert info in out.stdout and "임시 비밀번호:" not in out.stdout, out.stdout   # 비대화형이면 비밀번호 미출력
        pw2 = _pw_in(info)
        assert pw2 != pw1
        assert not c.op_token_valid(tok1), "초기화 후 기존 세션이 살아 있음"   # 토큰 검사만으로도 반영
        assert c.unlock(pw1).get("ok") is False and c.unlock(pw2).get("ok") is True
        # 다른 저장(예: 스캔 시작의 last_scan 기억)이 옛 메모리 값으로 비밀번호를 되돌리지 않는다
        c._save_settings(dict(stale, top_n=7))
        disk = setmod.load(data)
        assert disk["top_n"] == 7 and disk["op_password"] == pw2, (disk["top_n"], disk["op_password"] == pw1)

        # 사용자가 정한 비밀번호로 변경 → 임시 해제, info.MD 는 새 비밀번호(평문 저장 기본)
        r = c.update_settings({"op_lock": True, "op_password": "mine-1234"})
        assert r["ok"] and not c.op_temp_info()["op_temp"] and _pw_in(info) == "mine-1234"
        # 보호 해제 → 비밀번호 없는 상태로 두지 않고 새 임시 비밀번호
        c.update_settings({"op_lock": False})
        assert c.op_required() and c.op_temp_info()["op_temp"] and c.unlock(_pw_in(info)).get("ok")
        # 암호화 저장으로 사용자 비밀번호 → 남은 임시 info.MD 삭제(무의미한 비밀번호 파일 정리)
        c.update_settings({"op_password_encrypted": True, "op_lock": True, "op_password": "enc-5678"})
        assert not os.path.exists(info) and c.unlock("enc-5678").get("ok")
        assert setmod.load(data)["op_password"].startswith("pbkdf2")

        # info.MD 를 쓸 수 없으면 비밀번호를 바꾸지 않는다(아무도 모르는 비밀번호 방지)
        before = setmod.load(data)["op_password"]
        ro = os.path.join(d, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o500)
        os.chmod(data, 0o500)
        try:
            if os.access(ro, os.W_OK):                # root 로 실행하면 권한 검사가 무의미 — 건너뜀
                print("[temp-password] (root: 쓰기 실패 경우 건너뜀)")
            else:
                assert reset_op_password(data, ro) is None
        finally:
            os.chmod(data, 0o700)
            os.chmod(ro, 0o700)
        assert setmod.load(data)["op_password"] == before
        print("[temp-password] OK  기동 시 임시 비밀번호(info.MD 600)·서버 명령 초기화 즉시 반영·세션 만료·"
              "저장 회귀 없음·사용자 변경·해제→재생성·암호화 시 정리·쓰기 실패 시 미변경")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _check_partial_aggregate_api() -> None:
    """멈춘 자료 집계 API: 중지된 스캔 → POST /api/scan/partial-aggregate → 진행/완료 상태 → /api/status 에
    partial_agg_at·하한값 노출, /api/scans 에 표시, 진행 중엔 재개/삭제 거부, 재개 완료 후 표시 해제."""
    import shutil
    import time as _t

    from isilon_usage import db as dbmod
    from isilon_usage import manager as mgrmod
    from isilon_usage import scanner as scanmod
    from isilon_usage import server as srvmod
    from isilon_usage.server import ScanController

    def _row(data, sid):
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        try:
            return mgrmod.get_scan(mc, sid)
        finally:
            mc.close()

    def _wait(data, sid, want, timeout=60):
        row = None
        for _ in range(int(timeout / 0.1)):
            row = _row(data, sid)
            if row and row["status"] in want:
                return row
            _t.sleep(0.1)
        return row

    d = tempfile.mkdtemp(prefix="isilon_partial_api_")
    orig_stat = scanmod.Scanner._safe_stat
    orig_pa = srvmod.partial_aggregate
    try:
        tree = os.path.join(d, "tree")
        for a_ in range(8):
            sub = os.path.join(tree, "d%d" % a_)
            os.makedirs(sub)
            for i in range(300):
                with open(os.path.join(sub, "f%03d.bin" % i), "wb") as fh:
                    fh.write(b"x" * 64)
        data = os.path.join(d, "data")
        ctl = ScanController(data)

        def slow_stat(self, entry):
            _t.sleep(0.002)
            return orig_stat(self, entry)
        scanmod.Scanner._safe_stat = slow_stat                  # 중지할 틈을 만든다
        sid = ctl.start_scan(tree, engine="threads")["scan_id"]
        # 고정 대기 대신 '하위 폴더 1개 이상 완료'까지 기다린다(부하가 크면 0.8초 안에 하나도 안 끝나
        # 부분 집계 하한값이 0 이 되는 간헐 실패가 있었다)
        _db = _row(data, sid)["db_path"]
        for _ in range(200):
            try:
                _c = dbmod.connect(_db)
                try:
                    _n = _c.execute("SELECT COUNT(*) FROM directories WHERE depth=1 "
                                    "AND status='discovered' AND file_count>0").fetchone()[0]
                finally:
                    _c.close()
            except Exception:  # noqa: BLE001 — DB 생성 전
                _n = 0
            if _n:
                break
            _t.sleep(0.05)
        assert not ctl.partial_aggregate_scan(sid)["ok"], "실행 중엔 거부"
        assert ctl.stop_scan(sid)["ok"]
        row = _wait(data, sid, ("paused",))
        assert row["status"] == "paused", row["status"]
        scanmod.Scanner._safe_stat = orig_stat

        # 진행 중 재개/삭제 거부 — 집계 함수를 1초 지연시켜 창을 만든다
        def slow_pa(*a, **k):
            _t.sleep(1.0)
            return orig_pa(*a, **k)
        srvmod.partial_aggregate = slow_pa
        r = ctl.partial_aggregate_scan(sid)
        assert r["ok"], r
        assert not ctl.partial_aggregate_scan(sid)["ok"], "중복 실행 거부"
        assert not ctl.resume_scan(sid)["ok"] and not ctl.delete_scan(sid)["ok"], "진행 중 재개/삭제 거부"
        st = ctl.partial_aggregate_status(sid)
        assert st["running"], st
        for _ in range(100):
            st = ctl.partial_aggregate_status(sid)
            if not st["running"]:
                break
            _t.sleep(0.1)
        assert not st["running"] and st["result"]["ok"] and st["result"]["rows"] >= 1, st
        srvmod.partial_aggregate = orig_pa
        # per-run DB: 부분 집계 값·플래그, 관리 DB: 플래그
        pc = dbmod.connect(row["db_path"])
        try:
            from isilon_usage.server import build_status
            bs = build_status(pc, None, top=0)
            root = pc.execute("SELECT total_bytes, total_files FROM directories WHERE depth=0").fetchone()
        finally:
            pc.close()
        assert bs["run"]["partial_agg_at"] and root["total_bytes"] > 0, (bs["run"].get("partial_agg_at"), tuple(root))
        assert _row(data, sid)["partial_agg_at"], "관리 DB 에 부분 집계 시각 없음"
        # 이력 표는 overall.roots 를 그린다 — 같은 필드가 roots 에도 실려야 배지가 보인다(단일 소스)
        mc = dbmod.connect(mgrmod.manager_db_path(data))
        try:
            ov = mgrmod.overall_capacity(mc)
        finally:
            mc.close()
        assert any(x["scan_id"] == sid and x.get("partial_agg_at") for x in ov["roots"]), ov["roots"]
        # 재개 → 완료 → 플래그 해제(관리 DB·per-run DB)
        assert ctl.resume_scan(sid)["ok"]
        row2 = _wait(data, sid, ("done", "error"))
        assert row2["status"] == "done", (row2["status"], row2["error"])
        for _ in range(50):                                   # _on_scan_finished 가 비동기로 지움
            if not _row(data, sid)["partial_agg_at"]:
                break
            _t.sleep(0.1)
        assert not _row(data, sid)["partial_agg_at"], "완료 후 관리 DB 표시 미해제"
        pc = dbmod.connect(row["db_path"])
        try:
            flag = pc.execute("SELECT partial_agg_at FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
            root2 = pc.execute("SELECT total_files FROM directories WHERE depth=0").fetchone()[0]
        finally:
            pc.close()
        assert flag is None and root2 == 2400, (flag, root2)
        print("[partial-aggregate-api] OK  중지→부분 집계(하한값·표시)→진행 중 재개/삭제 거부→재개 완료 후 표시 해제")
    finally:
        scanmod.Scanner._safe_stat = orig_stat
        srvmod.partial_aggregate = orig_pa
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    _check_insecure_warning()
    _check_troubleshoot_live()
    _check_partial_aggregate_api()
    _check_temp_password_and_reset()
    _check_resume_keeps_start_options()
    _check_duplicate_scan_guard()
    _check_resume_after_upgrade()
    _check_resume_real()
    _check_persistence_guard()
    _check_sequential_schedule()
    _check_email_notifications()
    _check_op_password()
    _check_auto_restart()
    _check_last_scan_remember()
    tmp = tempfile.mkdtemp(prefix="isilon_srv_")
    root = os.path.join(tmp, "tree")
    _make_tree(root)
    data_dir = os.path.join(tmp, "data")

    _orig_cwd = os.getcwd()
    os.chdir(tmp)   # info.MD(작업 비번 기록)가 repo 가 아닌 임시 폴더에 생성되도록
    httpd = serve(data_dir, host="127.0.0.1", port=0,
                  initial_settings={"mount_bases": [tmp]}, enable_scan=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    c = Client(f"http://127.0.0.1:{port}")
    try:
        # 웹 스캔 가능 + 마운트/탐색
        scans = c.get("/api/scans")
        assert scans["can_scan"] and scans["mount_bases"] == [tmp], scans
        assert c.get("/api/mounts")["ok"]
        br = c.get(f"/api/browse?path={root}")
        assert br["ok"] and any(d["name"] == "sub1" for d in br["dirs"]), br

        # 탐색(browse)도 지정 경로 밖이면 '소프트 컨펌' 신호(outside_base)를 준다
        bb = c.get("/api/browse?path=/etc")
        assert not bb["ok"] and bb["reason"] == "outside_base", bb
        # 컨펌(confirm=1)하면 지정 경로 밖도 탐색 허용
        bc = c.get("/api/browse?path=/etc&confirm=1")
        assert bc["ok"] and bc.get("path") == "/etc", bc
        # 마운트 루트에서 '위로'는 (컨펌 전) 밖으로 못 나가게 현재 경로로 묶임
        bm = c.get(f"/api/browse?path={tmp}")
        assert bm["ok"] and bm["parent"] == bm["path"], bm

        # 경로 제한: 지정 경로 밖은 컨펌 없으면 outside_base 로 보류
        bad = c.post("/api/scan/start", {"path": "/etc"})
        assert not bad["ok"] and bad.get("reason") == "outside_base", bad

        # 스캔 1 시작 → 완료
        r1 = c.post("/api/scan/start", {"path": root})
        assert r1["ok"], r1
        assert "mount_readonly" in r1, r1   # 시작 응답에 마운트 ro 여부 포함
        s1 = r1["scan_id"]
        d1 = _wait_done(c, s1)
        assert d1["run"]["status"] == "done"
        assert d1["run"]["total_files"] == 4, d1["run"]["total_files"]
        assert isinstance(d1["run"]["mount_readonly"], bool), d1["run"]  # 상태에도 노출
        # DB 생존 지표: per-run DB 사용량(.db+WAL)이 상태에 포함되어야 함
        du = d1.get("db_usage")
        assert du and du["db_bytes"] > 0 and du["total_bytes"] >= du["db_bytes"], du
        assert "limit_bytes" in du, du
        # 누적 작업 시간 + 로컬 디스크 여유(DB 저장 위치)
        assert d1["run"]["elapsed_accum"] >= 0 and "session_started_at" in d1["run"], d1["run"]
        ldk = d1.get("local_disk") or {}
        assert ldk.get("db") and ldk["db"]["free_bytes"] > 0 and ldk["db"]["total_bytes"] > 0, ldk

        # 분석 리포트(나이/소유자/확장자)
        rep = c.get(f"/api/stats?scan={s1}")
        assert rep["ok"], rep
        assert rep["age"] and sum(a["files"] for a in rep["age"]) == 4, rep["age"]
        # 마지막 접근(atime) 나이 분포 — 파일 4개 모두 집계(같은 stat 결과라 추가 I/O 없음)
        assert rep["atime_age"] and sum(a["files"] for a in rep["atime_age"]) == 4, rep["atime_age"]
        assert "atime_info" in rep and "reliable" in rep["atime_info"], rep.get("atime_info")
        assert rep["owners"] and sum(o["files"] for o in rep["owners"]) == 4, rep["owners"]
        exts = {e["key"]: e["files"] for e in rep["extensions"]}
        assert exts.get(".bin", 0) >= 1, rep["extensions"]   # _make_tree 는 .bin 파일

        # 자연어 질의응답(규칙 기반) — 엔드포인트 배선 + 선택 스캔 상세 반영
        a1 = c.get(f"/api/ask?q={urllib.parse.quote('전체 용량 얼마야?')}")
        assert a1["ok"] and a1["source"] == "rules", a1
        assert a1["intent"] == "total" and a1["answer"], a1
        a2 = c.get(f"/api/ask?q={urllib.parse.quote('제일 큰 디렉터리?')}&scan={s1}")
        assert a2["ok"] and a2["intent"] == "top_dirs" and a2["answer"], a2
        a3 = c.get(f"/api/ask?q={urllib.parse.quote('안녕')}")   # 미매칭 → 도움말
        assert a3["intent"] == "help" and a3.get("suggestions"), a3

        # 추가 기능: 폴더 마지막 접근시각(atime) — 용량 계산과 별개의 라이브 조회
        at = c.get(f"/api/atime?path={urllib.parse.quote(root)}")
        assert at["ok"] and at["count"] >= 1 and "atime_info" in at, at
        assert all("atime" in e and "owner" in e for e in at["entries"]), at["entries"][:2]
        # atime 오래된 순 정렬 보장
        ats = [e["atime"] for e in at["entries"]]
        assert ats == sorted(ats), ats
        assert not c.get("/api/atime?path=")["ok"]           # 경로 없으면 거부
        assert not c.get(f"/api/atime?path={urllib.parse.quote(root + '/nope')}")["ok"]

        # 드릴다운: 루트 → 자식
        ch = c.get(f"/api/children?scan={s1}")
        assert ch["children"], ch
        root_id = ch["children"][0]["id"]
        kids = c.get(f"/api/children?scan={s1}&parent={root_id}")
        names = {k["name"] for k in kids["children"]}
        assert {"sub1", "sub2"} <= names, names

        # 병렬 워커 수가 상태에 노출됨(설정값 반영)
        assert d1["run"].get("workers", 0) >= 1, d1["run"]

        # 상위 디렉터리: 깊이 선택 + 정렬 + 드릴다운
        td = c.get(f"/api/topdirs?scan={s1}&rel=1&sort=size&order=desc")
        names1 = [r["name"] for r in td["rows"]]
        assert set(names1) == {"sub1", "sub2"} and names1[0] == "sub1", td  # sub1 이 더 큼
        td_asc = c.get(f"/api/topdirs?scan={s1}&rel=1&sort=path&order=asc")
        assert [r["name"] for r in td_asc["rows"]] == ["sub1", "sub2"], td_asc
        # 파이용 parent 요약: 루트의 하위 디렉터리 개수(sub1,sub2=2)
        assert td["parent"] and td["parent"]["subdir_count"] == 2, td["parent"]
        sub1_id = next(r["id"] for r in td["rows"] if r["name"] == "sub1")
        td2 = c.get(f"/api/topdirs?scan={s1}&under={sub1_id}&rel=1")  # sub1 직속 자식
        assert [r["name"] for r in td2["rows"]] == ["deep"], td2
        assert td2["parent"]["name"] == "sub1" and td2["parent"]["subdir_count"] == 1, td2["parent"]
        td3 = c.get(f"/api/topdirs?scan={s1}&under={sub1_id}&rel=0")  # 전체 깊이
        assert any(r["name"] == "deep" for r in td3["rows"]), td3

        # 검색 / 오류
        sr = c.get(f"/api/search?scan={s1}&q=deep")
        assert any("deep" in r["path"] for r in sr["results"]), sr
        assert c.get(f"/api/errors?scan={s1}")["ok"]

        # 내보내기 CSV
        csv = c.get_raw(f"/api/export?scan={s1}&format=csv").decode("utf-8")
        assert "path,depth" in csv and root in csv

        # 인증 정책: 일반 작업(스캔 시작 등)은 자유, '설정'·되돌릴 수 없는 작업만 인증.
        # 비밀번호가 아직 없으면 설정은 need_setup(401) → 처음 들어갈 때 비밀번호를 만든다(/api/op-setup).
        assert c.get("/api/scans")["op_required"] is False
        ns, st = c.post("/api/settings", {"settings": {"top_n": 9}}, want_status=True)
        assert st == 401 and ns.get("need_setup"), (st, ns)
        for pth in ("/api/scan/delete", "/api/prune", "/api/upgrade/install"):
            _r, st = c.post(pth, {}, want_status=True)
            assert st == 401, (pth, st, _r)
        # CSRF: 다른 사이트 폼은 JSON Content-Type 을 못 보낸다 → text/plain 최초 설정 거부
        _r, st = c.post("/api/op-setup", {"password": "pw1234"},
                        headers={"Content-Type": "text/plain"}, want_status=True)
        assert st == 415 and c.get("/api/scans")["op_required"] is False, (st, _r)
        assert not c.post("/api/op-setup", {"password": "ab"})["ok"]          # 너무 짧음
        su = c.post("/api/op-setup", {"password": "pw1234"})
        assert su["ok"] and su["token"], su
        assert c.get("/api/scans")["op_required"] is True
        assert not c.post("/api/op-setup", {"password": "other99"})["ok"]     # 이미 있으면 덮어쓰기 금지
        c.op_token = su["token"]

        # 글로벌 포탈 복제용 DB export (토큰 보호 + tar.gz 번들)
        c.post("/api/settings", {"settings": {"api_token": "secret123"}})
        try:
            c.get_raw("/api/dbexport")
            raise AssertionError("토큰 없이 통과되면 안 됨")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code
        raw = c.get_raw("/api/dbexport?token=secret123")
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        names = tar.getnames()
        assert "meta.json" in names, names
        assert any(n.startswith("scans/") and n.endswith(".db") for n in names), names
        meta = json.loads(tar.extractfile("meta.json").read().decode("utf-8"))
        assert meta["hostname"] and any(sc["status"] == "done" for sc in meta["scans"]), meta
        # 토큰은 응답에 노출되지 않고(set 여부만), 빈 값 저장 시 기존 토큰 유지
        st = c.get("/api/settings")["settings"]
        assert st["api_token"] == "" and st["api_token_set"] is True, st

        # 설정 저장 라운드트립
        up = c.post("/api/settings", {"settings": {"top_n": 3, "scan_workers": 2, "check_readonly": False,
                                                   "upgrade_url": "http://mirror.local/isilon_good/download"}})
        assert up["ok"] and up["settings"]["top_n"] == 3 and up["settings"]["scan_workers"] == 2
        assert up["settings"]["check_readonly"] is False, up["settings"]
        # 업데이트 소스 URL 을 설정에서 바꿔 저장·반영(대시보드 setUpgUrl 과 동일 경로)
        assert up["settings"]["upgrade_url"] == "http://mirror.local/isilon_good/download", up["settings"]

        # 서버 사양 기반 권장 스레드 계산
        rw = c.get("/api/recommend-workers")
        assert rw["ok"] and 1 <= rw["min"] <= rw["recommended"] <= rw["max"] <= 64, rw

        # 실측 보정(시범 탐색) — 작은 후보/예산으로 빠르게
        bw = c.post("/api/benchmark-workers", {"path": root, "candidates": "1,2", "budget": 1})
        assert bw["ok"] and bw["recommended"] in (1, 2), bw
        assert len(bw["results"]) >= 1 and all(r["dirs_per_sec"] >= 0 for r in bw["results"]), bw
        # 허용 경로 밖은 거부
        bad = c.post("/api/benchmark-workers", {"path": "/etc", "budget": 1})
        assert not bad["ok"], bad
        assert rw["cpu_count"] >= 1 and rw["rationale"], rw
        # 결과에 메모리 측정(peak RSS·배수)이 포함되어야
        assert all("peak_rss_bytes" in r and "mem_ratio" in r for r in bw["results"]), bw

        # 실측 보정 스트리밍(시작 → 단계별 폴링)
        st = c.post("/api/benchmark-workers/start", {"path": root, "candidates": "1,2", "budget": 1})
        assert st["ok"] and st.get("started"), st
        bs = None
        for _ in range(60):
            bs = c.get("/api/benchmark-workers/status")
            if bs.get("done"):
                break
            time.sleep(0.5)
        assert bs and bs["done"] and bs["recommended"] in (1, 2), bs
        assert len(bs["results"]) >= 1 and all("mem_ratio" in r for r in bs["results"]), bs

        # 테스트 데이터 생성: N=2, M=2, K=3, 100B → 디렉터리 6, 파일 18
        gtdir = os.path.join(tmp, "gtsample")
        gv = c.post("/api/gentest/validate",
                    {"path": gtdir, "n_dirs": 2, "n_subdirs": 2, "n_files": 3, "file_size": 100})
        assert gv["ok"] and gv["plan"]["total_dirs"] == 6 and gv["plan"]["total_files"] == 18, gv
        gs = c.post("/api/gentest/start",
                    {"path": gtdir, "n_dirs": 2, "n_subdirs": 2, "n_files": 3, "file_size": 100})
        assert gs["ok"] and gs.get("started"), gs
        gst = None
        for _ in range(60):
            gst = c.get("/api/gentest/status")
            if gst.get("done"):
                break
            time.sleep(0.3)
        assert gst and gst["done"] and not gst.get("error"), gst
        assert gst["created_dirs"] == 6 and gst["created_files"] == 18, gst
        nfiles = sum(len(f) for _, _, f in os.walk(gtdir))
        assert nfiles == 18, nfiles
        # 시스템 경로는 거부
        gbad = c.post("/api/gentest/start", {"path": "/etc", "n_dirs": 1})
        assert not gbad["ok"], gbad

        # 작업 보호(비밀번호): 위에서 만든 비밀번호 — 응답엔 값 대신 설정 여부만
        assert c.get("/api/scans")["op_required"] is True
        assert c.get("/api/settings")["settings"]["op_password"] == ""
        # 토큰 없이: 설정 POST → 401 잠김, 일반 작업(스캔 중지 요청)은 인증 없이 처리, 보기 GET 은 자유
        lk, st = c.post("/api/settings", {"settings": {"top_n": 9}},
                        headers={"X-Op-Token": ""}, want_status=True)
        assert st == 401 and lk.get("op_required"), (st, lk)
        _r, st = c.post("/api/scan/stop", {"scan_id": 999999}, headers={"X-Op-Token": ""}, want_status=True)
        assert st != 401, (st, _r)
        assert c.get("/api/scans")["ok"] is True   # 보기는 자유
        # 틀린 비번 → 실패, 맞는 비번 → 토큰
        assert c.post("/api/unlock", {"password": "nope"}).get("ok") is False
        tok = c.post("/api/unlock", {"password": "pw1234"})
        assert tok["ok"] and tok["token"], tok
        # 토큰으로 POST → 동작
        okp = c.post("/api/settings", {"settings": {"top_n": 9}},
                     headers={"X-Op-Token": tok["token"]})
        assert okp["ok"] and okp["settings"]["top_n"] == 9, okp
        # 보호 해제(op_lock False) — 토큰 필요. 해제되면 설정은 다시 '처음 비밀번호 만들기' 상태
        c.post("/api/settings", {"settings": {"op_lock": False}},
               headers={"X-Op-Token": tok["token"]})
        assert c.get("/api/scans")["op_required"] is False
        _r, st = c.post("/api/settings", {"settings": {"top_n": 9}}, want_status=True)
        assert st == 401 and _r.get("need_setup"), (st, _r)
        su2 = c.post("/api/op-setup", {"password": "pw5678"})
        assert su2["ok"], su2
        c.op_token = su2["token"]                    # 이후 정리·삭제 테스트용

        # 스캔 2 → diff
        r2 = c.post("/api/scan/start", {"path": root})
        _wait_done(c, r2["scan_id"])
        df = c.get(f"/api/diff?base={s1}&target={r2['scan_id']}")
        assert df["ok"] and df["total_delta"] == 0, df
        # diff 행에 크기 변화 + 파일수 변화 + 루트경로 포함(변경 많은 폴더 Top 비교)
        assert df.get("root_path") == root and df["rows"], df
        assert all({"delta", "files_delta", "base_files", "target_files"} <= set(r) for r in df["rows"]), df["rows"][:1]
        # 특정 폴더 변화 이력: 루트 경로의 스캔별 크기·파일수(같은 트리라 변화 0)
        fh = c.get(f"/api/folder-history?root={root}&path={root}")
        assert fh["ok"] and fh["found"] >= 2 and len(fh["points"]) >= 2, fh
        assert fh["points"][-1]["total_files"] == 4, fh["points"][-1]
        assert all("bytes_delta" in p and "files_delta" in p for p in fh["points"]), fh
        assert fh["points"][-1]["bytes_delta"] == 0 and fh["points"][-1]["files_delta"] == 0, fh["points"][-1]
        # root/path 누락 → 400
        miss = c.get("/api/folder-history?root=&path=")
        assert not miss["ok"], miss
        # 존재하지 않는 폴더 경로 → 빈 이력(에러 아님)
        none_fh = c.get(f"/api/folder-history?root={root}&path={root}/__no_such__")
        assert none_fh["ok"] and none_fh["found"] == 0, none_fh

        # 최대 파일 Top — /api/stats 에 포함(파일 4개 전부, 크기 내림차순)
        tf = c.get(f"/api/stats?scan={r2['scan_id']}")["top_files"]
        assert len(tf) == 4, tf
        assert all(tf[i]["bytes"] >= tf[i + 1]["bytes"] for i in range(len(tf) - 1)), tf
        assert "mtime" in tf[0] and "uid" in tf[0] and tf[0]["path"].endswith(".bin"), tf[0]
        assert "atime" in tf[0], tf[0]   # 최대 파일에 마지막 접근시각 포함

        # 용량 소진 예측 — 완료 스캔 2회라 추세 계산 가능(증가 0 → 소진 없음)
        fc = c.get(f"/api/forecast?scan={r2['scan_id']}")
        assert fc["ok"] and fc["enough"] and fc["points"] >= 2, fc
        assert fc["growth_per_day"] is not None and fc["fs_total_bytes"] > 0, fc

        # 변화 리포트 — 직전 스캔 대비(변화 없음이어도 구조는 정상)
        gw = c.get(f"/api/growers?scan={r2['scan_id']}")
        assert gw["ok"] and gw["has_base"] and gw["total_delta"] == 0, gw
        assert "growers" in gw and "added" in gw and "removed" in gw, gw

        # Prometheus /metrics
        mtx = c.get_raw("/metrics").decode("utf-8")
        assert "isilon_usage_info{" in mtx, mtx[:200]
        assert "isilon_usage_root_scanned_bytes{" in mtx, mtx[:400]
        assert 'root="%s"' % root in mtx, mtx[:400]

        # prune keep_per_root=1 → 오래된 s1 삭제
        pr = c.post("/api/prune", {"keep_per_root": 1})
        assert s1 in pr["deleted"], pr
        remaining = [s["id"] for s in c.get("/api/scans")["scans"]]
        assert s1 not in remaining and r2["scan_id"] in remaining, remaining

        # 삭제
        dl = c.post("/api/scan/delete", {"scan_id": r2["scan_id"]})
        assert dl["ok"], dl
        assert c.get("/api/scans")["scans"] == []

        # pscan 중지 정직성: pscan 은 자식 프로세스가 끝까지 돌아 중간 중지가 안 되므로
        # ok:True 로 거짓 성공을 주지 말고 ok:False+engine 으로 사실대로 알려야 한다.
        ctrl = httpd.controller
        ev_ps = threading.Event()
        ctrl._scans[9991] = {"engine": "pscan", "stop": ev_ps, "thread": None, "path": root}
        rp = c.post("/api/scan/stop", {"scan_id": 9991})
        assert rp["ok"] is False and rp.get("engine") == "pscan", rp
        assert "멈출 수 없" in rp["reason"], rp
        assert not ev_ps.is_set(), "pscan 은 멈추지 못하므로 stop 이벤트도 건드리지 않는다"
        ctrl._scans.pop(9991, None)
        # threads 스캔은 중지 신호가 실제로 먹는다(ok:True + stop 이벤트 set)
        ev_th = threading.Event()
        ctrl._scans[9992] = {"stop": ev_th, "thread": None, "path": root}
        rt = c.post("/api/scan/stop", {"scan_id": 9992})
        assert rt["ok"] and ev_th.is_set(), rt
        ctrl._scans.pop(9992, None)

        print("[server] OK  모든 API 통과")
        print("모든 테스트 통과 ✅")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        os.chdir(_orig_cwd)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

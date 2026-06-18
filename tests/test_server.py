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

    def get(self, path):
        try:
            return json.load(urllib.request.urlopen(self.base + path, timeout=5))
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def get_raw(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5).read()

    def post(self, path, data, headers=None, want_status=False):
        h = {"Content-Type": "application/json"}
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


def _check_sequential_schedule() -> None:
    """순차 예약: 한 예약의 여러 경로를 A 끝나면 B 식으로 순서대로 실행한다."""
    import shutil

    from isilon_usage.server import ScanController
    d = tempfile.mkdtemp(prefix="isilon_chain_")
    try:
        c = ScanController(os.path.join(d, "data"))
        started = []
        ids = iter([101, 102, 103])

        def fake_start(path, **_kw):
            sid = next(ids)
            started.append((sid, path))
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


def main() -> int:
    _check_insecure_warning()
    _check_resume_after_upgrade()
    _check_sequential_schedule()
    _check_email_notifications()
    _check_op_password()
    _check_auto_restart()
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

        # 작업 보호(비밀번호): 미설정이면 op_required 거짓
        assert c.get("/api/scans")["op_required"] is False
        sp = c.post("/api/settings", {"settings": {"op_lock": True, "op_password": "pw1234"}})
        assert sp["ok"] and sp["settings"]["op_password"] == "" and sp["settings"]["op_password_set"] is True, sp
        assert c.get("/api/scans")["op_required"] is True
        # 토큰 없이 POST → 401 잠김 (보기 GET 은 여전히 동작)
        lk, st = c.post("/api/settings", {"settings": {"top_n": 9}}, want_status=True)
        assert st == 401 and lk.get("op_required"), (st, lk)
        assert c.get("/api/scans")["ok"] is True   # 보기는 자유
        # 틀린 비번 → 실패, 맞는 비번 → 토큰
        assert c.post("/api/unlock", {"password": "nope"}).get("ok") is False
        tok = c.post("/api/unlock", {"password": "pw1234"})
        assert tok["ok"] and tok["token"], tok
        # 토큰으로 POST → 동작
        okp = c.post("/api/settings", {"settings": {"top_n": 9}},
                     headers={"X-Op-Token": tok["token"]})
        assert okp["ok"] and okp["settings"]["top_n"] == 9, okp
        # 보호 해제(op_lock False) — 토큰 필요
        c.post("/api/settings", {"settings": {"op_lock": False}},
               headers={"X-Op-Token": tok["token"]})
        assert c.get("/api/scans")["op_required"] is False

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

"""1.99.48 회귀 테스트 — '버그 없을 때까지 디버깅' 반복 리뷰에서 재현된 결함.

1회차(엔진·엣지 화면/서버·포탈 3개 영역 리뷰 + 직접 재현):
  E1 비 UTF-8 루트 경로 스캔 크래시  E2 예약 시차 배치가 병합에 되돌려짐  E3 pscan CLI 부분 실패=성공
  E4 일시정지 후 재개한 스캔의 남은 시간이 쉬던 시간만큼 부풀음
  P1 끊긴 노드의 옛 '진행 중'이 합계에 남음  P2 노드 수정 시 복제 주기 기준일 초기화  P3 노드 이름 수정=복제본 생성
  P4 백업 파일 0644  P5 로그인 없는 POST 의 사이트 간 위조(text/plain)  P6 '루트로' 버튼 이름  P7 사라진 지역 필터
  D1 홈이 한 주기 늦게 갱신  D2 '10,000만 개' 표기  D3 깊은 중첩 JSON 연결 끊김  D4 타입 오류 500  D5 좁은 화면 표 넘침
2회차:
  F1 1단계(폴더 찾기) 중 남은 시간 없음 → 이전 완료 스캔 기준 추정  R1 비 UTF-8 이름 응답 500  R2 days=0·min_size=0 이
  기본값으로 바뀜  R3 path 가 문자열이 아니면 500(다른 엔드포인트)  R4 gentest 숫자 500  R5 알림 인코딩 예외
  S1 집계 단계 남은 시간이 1단계 시간까지 나눠 부풀음  C2 정리(prune)가 진행·일시정지 스캔을 지움
  C3/C4 잘리거나 손상된·핵심 파일이 빠진 업그레이드 묶음을 설치  C5 서머타임 시작일 예약 반복 실행
  C6 batch_size=0 무한 루프/0 나눗셈  C7 설정의 Infinity·숫자 mount_bases 로 serve 기동 실패  CLI 없는 data-dir traceback
  Q1 접힌(fold) 폴더가 '누락'으로 보임·다른 복제 후보를 안 봄  Q2 오래된 하위 루트 스캔이 최신 상위 스캔을 이김
  Q3 중첩 루트(/a 와 /a/b) 용량 이중 합산  Q4 비활성 노드가 핑 이력에 '다운'으로 쌓임
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "isilon_usage")


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------ 엔진
def check_e1_non_utf8_root() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    from isilon_usage.scanner import run_scan
    d = tempfile.mkdtemp(prefix="iu_e1_")
    try:
        root = os.fsdecode(os.fsencode(d) + b"/\xb0\xa1root")
        os.makedirs(root + "/sub")
        with open(root + "/sub/f", "wb") as fh:
            fh.write(b"x" * 5000)
        dd = os.path.join(d, "data")
        M.init_manager(dd)
        sid = M.register_scan(dd, root_path=root, db_path=os.path.join(dd, "a.db"), backend="native", size_mode="disk")
        run_scan(os.path.join(dd, "a.db"), root, with_monitor=False,
                 manager_db=M.manager_db_path(dd), manager_scan_id=sid)
        c = dbmod.connect(M.manager_db_path(dd))
        row = M.get_scan(c, sid)
        c.close()
        assert row["status"] == "done", row["status"]
        assert dbmod.fs_text(row["root_path"]) == root            # 재개 등 파일시스템 사용 땐 원래 경로로
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[E1] OK  비 UTF-8 루트 경로 스캔 완료(저장 변환·사용 복원)")


def check_e2_stagger_kept() -> None:
    from isilon_usage.server import ScanController
    from isilon_usage import settings as S
    d = tempfile.mkdtemp(prefix="iu_e2_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        os.makedirs(os.path.join(d, "a"))
        os.makedirs(os.path.join(d, "b"))
        c = ScanController(os.path.join(d, "edge"))
        now = time.time()
        c.update_settings({"schedules": [
            {"paths": [os.path.join(d, "a")], "unit": "minute", "every": 60, "last_run": now - 100},
            {"paths": [os.path.join(d, "b")], "unit": "minute", "every": 60, "last_run": now - 100}]},
            merge_runtime=False)
        staggered = S.stagger_schedules(list(c.settings["schedules"]))
        c.update_settings({"schedules": staggered}, merge_runtime=False)      # /api/schedules/stagger 와 같은 호출
        lrs = [round(x["last_run"]) for x in c.settings["schedules"]]
        assert len(set(lrs)) == 2, "시차 배치가 저장되지 않음: %s" % lrs
        src = _read("server.py")
        assert 'update_settings({"schedules": staggered}, merge_runtime=False)' in src
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[E2] OK  예약 시차 배치가 런타임 병합에 되돌려지지 않음")


def check_e3_pscan_partial_exit() -> None:
    from isilon_usage import cli, pscan
    d = tempfile.mkdtemp(prefix="iu_e3_")
    orig = pscan.parallel_scan
    try:
        def fake(*a, **k):     # 실제 결과에 '한 단위 실패'만 덧씌운다(OOM·프로세스 풀 깨짐 상황)
            res = orig(*a, **k)
            res["complete"] = False
            res["failed_units"] = [{"path": "/x/d0", "error": "boom"}]
            return res
        pscan.parallel_scan = fake
        a = cli.build_parser().parse_args(["pscan", d])
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            rc = a.func(a)
        finally:
            sys.stderr = old
        assert rc == 1, rc
        assert "불완전" in err.getvalue()
    finally:
        pscan.parallel_scan = orig
        shutil.rmtree(d, ignore_errors=True)
    print("[E3] OK  pscan 일부 단위 실패 → 경고 + 종료코드 1")


def check_e4_eta_active_time() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    d = tempfile.mkdtemp(prefix="iu_e4_")
    try:
        M.init_manager(d)
        sid = M.register_scan(d, root_path="/x", db_path=os.path.join(d, "a.db"), backend="native", size_mode="disk")
        c = dbmod.connect(M.manager_db_path(d))
        now = time.time()
        M.update_scan(c, sid, status="sizing", phase="sizing", processed_dirs=500, total_dirs=1000,
                      started_at=now - 87000, active_elapsed=600.0)       # 하루 쉬었다 재개, 활성 600초
        c.commit()
        r = M.overall_capacity(c)["roots"][0]
        c.close()
        assert 550 <= r["eta_seconds"] <= 700, r["eta_seconds"]        # 예전엔 약 87000초(하루)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[E4] OK  재개한 스캔의 남은 시간 = 활성 시간 기준(쉬던 시간 제외)")


# ------------------------------------------------------------------ 포탈
def check_p1_p2_p4_portal() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_p1_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        now = time.time()
        pc.upsert_node({"id": "B", "url": "http://10.0.0.2:8765", "token": "t", "enabled": False})
        pc._cache["B"] = {"online": False, "ts": now - 3600, "edge_now": now, "overall": P._sanitize_overall(
            {"root_count": 1, "active_scans": 1, "roots": [{"root_path": "/a", "status": "sizing", "phase": "sizing",
             "started_at": now - 100, "updated_at": now - 50, "processed_dirs": 5, "total_dirs": 10,
             "eta_seconds": 60}]})}
        ov = pc.overview()
        assert ov["totals"]["active_scans"] == 0 and ov["running"] == [], ov["totals"]
        assert ov["nodes"][0]["running"] is None
        # P2: 수정 저장해도 복제 주기 기준일(anchor) 유지
        pc.upsert_node({"id": "D", "url": "http://10.0.0.3:8765", "token": "t", "unit": "day", "every": 3})
        a1 = pc._get("D")["anchor"]
        time.sleep(0.05)
        pc.upsert_node({"id": "D", "url": "http://10.0.0.3:8765", "token": "", "unit": "day", "every": 3,
                        "region": "서울"})
        assert pc._get("D")["anchor"] == a1
        # P4: 백업 파일은 소유자만
        old = os.umask(0o022)
        try:
            r = pc.save_backup(os.path.join(d, "bk"))
        finally:
            os.umask(old)
        assert r["ok"] and (os.stat(r["path"]).st_mode & 0o777) == 0o600, oct(os.stat(r["path"]).st_mode)
        assert (os.stat(os.path.join(d, "bk")).st_mode & 0o777) == 0o700
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[P1·P2·P4] OK  끊긴 노드 옛 진행 제외·anchor 유지·백업 0600/폴더 0700")


def check_p3_p6_p7_d1_d5_static() -> None:
    ph = _read("portal.html")
    assert '$("nId").readOnly=true' in ph and '$("nId").readOnly=false' in ph      # P3
    assert "🏠 루트로" not in ph and "⬆ 상위 폴더로" in ph                           # P6
    assert 'if(regionFilter && !(j.regions||[]).some(r=>r.region===regionFilter)) regionFilter="";' in ph   # P7
    dh = _read("dashboard.html")
    i_home = dh.index("try { renderHome(o, j); }")
    assert dh.index("_opTemp = !!j.op_temp") < i_home and dh.index("runningSet = new Set(j.running") < i_home   # D1
    css = _read("theme.css")
    assert "@media (max-width:900px){ .card{ overflow-x:auto; } }" in css                                   # D5
    print("[P3·P6·P7·D1·D5] OK  노드 이름 잠금·버튼 이름·지역 필터 해제·홈 갱신 순서·좁은 화면 표")


def check_d2_ko_count() -> None:
    node = shutil.which("node")
    if not node:
        print("[D2] SKIP  node 없음")
        return
    dh = _read("dashboard.html")
    fn = re.search(r"function _fmtKoCount\(n\) \{.*?\n\}", dh, re.S).group(0)
    js = ("function fmtNum(n){return Number(n).toLocaleString('en-US');}\n" + fn +
          "\nconsole.log(JSON.stringify([99999999, 99950000, 14063, 23176, 1440000000, 9999, 230000].map(_fmtKoCount)));")
    out = subprocess.run([node, "-e", js], stdout=subprocess.PIPE, check=True).stdout.decode()
    got = json.loads(out)
    assert got == ["1억 개", "9,995만 개", "1.4만 개", "2.3만 개", "14.4억 개", "9,999개", "23만 개"], got
    print("[D2] OK  큰 개수 표기(1억·1.4만)")


# ------------------------------------------------------------------ HTTP(엣지·포탈 공용 본문 처리)
def check_p5_d3_d4_http() -> None:
    from isilon_usage import server as SV

    class H:
        def __init__(self, body, headers):
            self.rfile = io.BytesIO(body)
            self.headers = headers
    _b, err = SV.read_json_body(H(b'{"a":1}', {"Content-Length": "7", "Content-Type": "text/plain"}))
    assert err and err[0] == 415, err                                              # P5
    ok, err = SV.read_json_body(H(b'{"a":1}', {"Content-Length": "7", "Content-Type": "application/json; charset=utf-8"}))
    assert ok == {"a": 1} and err is None
    deep = ('{"a":' + "[" * 100000 + "]" * 100000 + "}").encode()
    _b, err = SV.read_json_body(H(deep, {"Content-Length": str(len(deep)), "Content-Type": "application/json"}))
    assert err and err[0] == 400, err                                              # D3

    d = tempfile.mkdtemp(prefix="iu_d4_")
    cwd = os.getcwd()
    os.chdir(d)
    httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                     initial_settings={"mount_bases": [d]}, enable_scan=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def post(path, body, tok=""):
        rq = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                    headers={"Content-Type": "application/json", "X-Op-Token": tok})
        try:
            r = urllib.request.urlopen(rq)
            return r.status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        c = httpd.RequestHandlerClass.controller
        c.setup_op_password("test1234")
        tok = c.unlock("test1234")["token"]
        assert post("/api/scan/start", {"path": ["/usr"]}) == 400
        assert post("/api/scan/start", {"path": 123}) == 400
        rq = urllib.request.Request(base + "/api/scan/stop", data=b'{"scan_id": 1e999}', method="POST",
                                    headers={"Content-Type": "application/json"})
        try:
            st = urllib.request.urlopen(rq).status
        except urllib.error.HTTPError as e:
            st = e.code
        assert st == 400, st
        assert post("/api/settings", {"settings": "abc"}, tok) == 400
        assert post("/api/prune", {"keep_per_root": "x", "older_than_days": {}}, tok) == 400
        rq = urllib.request.Request(base + "/api/scan/start", data=b'{"path":"/x"}', method="POST",
                                    headers={"Content-Type": "text/plain"})
        try:
            st = urllib.request.urlopen(rq).status
        except urllib.error.HTTPError as e:
            st = e.code
        assert st == 415, st                                                       # P5(엣지)
    finally:
        httpd.shutdown()
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[P5·D3·D4] OK  JSON 아닌 본문 415·깊은 중첩 400·타입 오류 400(500 아님)")


# ------------------------------------------------------------------ 2회차
def check_f1_discovery_eta() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    assert M.estimate_eta_seconds("discovering", 0, 0, 250, 100, 1000) == 300.0
    assert M.estimate_eta_seconds("discovering", 0, 0, 1200, 100, 1000) is None      # 트리가 커지면 추정 안 함
    assert M.estimate_eta_seconds("discovering", 0, 0, 250, 100, None) is None      # 기준 없음(첫 스캔)
    assert M.estimate_eta_seconds("discovering", 0, 0, 1, 0.4, 3031) is None        # 시작 직후 1개로는 추정 안 함(튐)
    d = tempfile.mkdtemp(prefix="iu_f1_")
    try:
        M.init_manager(d)
        c = dbmod.connect(M.manager_db_path(d))
        old = M.register_scan(d, root_path="/x", db_path=os.path.join(d, "a.db"), backend="native", size_mode="disk")
        M.update_scan(c, old, status="done", phase="done", total_dirs=1000, scanned_bytes=1)
        c.commit()
        new = M.register_scan(d, root_path="/x", db_path=os.path.join(d, "b.db"), backend="native", size_mode="disk")
        M.update_scan(c, new, status="discovering", phase="discovering", discovered_dirs=250,
                      started_at=time.time() - 100, active_elapsed=100.0)
        c.commit()
        r = [x for x in M.overall_capacity(c)["roots"] if x["scan_id"] == new][0]
        assert r["eta_basis"] == "prev_scan" and 280 <= r["eta_seconds"] <= 320, r
        assert M.prev_total_dirs(c, "/x", new) == 1000
        c.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[F1] OK  폴더 찾기 중에도 이전 완료 스캔의 폴더 수로 남은 시간 추정(기록 없으면 안 함)")


def check_r1_to_r5_edge() -> None:
    from isilon_usage import server as SV
    from isilon_usage import notify
    b = SV._json_bytes({"p": "/a/bad\udcff"})
    assert json.loads(b.decode())["p"] == "/a/bad\udcff"                        # R1: 500 대신 이스케이프로 왕복
    assert notify.send("http://127.0.0.1:1/x", {"status": "done", "root_path": "/m\udcff"}, timeout=0.5) is False  # R5
    d = tempfile.mkdtemp(prefix="iu_r2_")
    cwd = os.getcwd()
    os.chdir(d)
    mnt = os.path.join(d, "mnt")
    os.makedirs(os.path.join(mnt, "a"))
    bad = os.fsdecode(os.fsencode(mnt) + b"/a/bad\xff")
    with open(bad, "wb") as fh:
        fh.write(b"z" * 10)
    old_f = os.path.join(mnt, "a", "old")
    with open(old_f, "wb") as fh:
        fh.write(b"y" * 10)
    t = time.time() - 3 * 86400
    os.utime(old_f, (t, t))
    httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                     initial_settings={"mount_bases": [mnt]}, enable_scan=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    def get(path):
        try:
            r = urllib.request.urlopen(base + path)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, None

    def post(path, body, tok=""):
        rq = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                    headers={"Content-Type": "application/json", "X-Op-Token": tok})
        try:
            return urllib.request.urlopen(rq).status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        st, _j = get("/api/atime?path=" + urllib.request.quote(os.path.join(mnt, "a")))
        assert st == 200, st                                                          # R1
        st, j = get("/api/coldplan?days=0&path=" + urllib.request.quote(os.path.join(mnt, "a")))
        assert st == 200 and j["days"] == 0 and j["cold_file_count"] >= 1, j          # R2
        c = httpd.RequestHandlerClass.controller
        c.setup_op_password("test1234")
        tok = c.unlock("test1234")["token"]
        assert post("/api/gentest/validate", {"path": 1}, tok) == 400                 # R3
        assert post("/api/autotune/start", {"path": [1]}, tok) == 400
        assert post("/api/gentest/validate", {"path": os.path.join(mnt, "g"), "n_dirs": "x"}, tok) == 400   # R4
    finally:
        httpd.shutdown()
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[R1~R5] OK  비 UTF-8 응답·days=0·경로 타입·gentest 숫자 400·알림 예외 없음")


# ------------------------------------------------------------------ 2회차(엔진·CLI·설정·업그레이드·포탈 복제)
def check_s1_sizing_window() -> None:
    from isilon_usage import manager as M
    s = {"phase": "sizing", "processed_dirs": 1500, "sizing_from_active": 3600.0, "sizing_from_processed": 1000}
    p, e = M.sizing_window(s, 3700.0)
    assert (p, e) == (500, 100.0), (p, e)          # 집계 단계에서만: 500개를 100초에
    eta = M.estimate_eta_seconds("sizing", p, 2000, 2000, e, already=1000)
    assert 90 <= eta <= 110, eta                     # 남은 500 / 5개/초 = 100초 (예전 식이면 약 2400초)
    assert M.sizing_window({"phase": "sizing", "processed_dirs": 7}, 9.0) == (7, 9.0)   # 기록 없음 → 예전 방식
    print("[S1] OK  집계 단계 남은 시간 = 집계 단계 속도 기준")


def check_c2_prune_keeps_active() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    d = tempfile.mkdtemp(prefix="iu_c2_")
    try:
        M.init_manager(d)
        ids = {}
        for st in ("done", "paused", "sizing", "done"):
            sid = M.register_scan(d, root_path="/r", db_path=os.path.join(d, "%s%d.db" % (st, len(ids))),
                                  backend="native", size_mode="disk")
            c = dbmod.connect(M.manager_db_path(d))
            M.update_scan(c, sid, status=st, started_at=time.time() - 400 * 86400,
                          finished_at=time.time() - 400 * 86400)
            c.commit()
            c.close()
            ids[sid] = st
        M.prune_scans(d, keep_per_root=1)
        M.prune_scans(d, older_than_days=1)
        c = dbmod.connect(M.manager_db_path(d))
        left = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM scans")}
        c.close()
        assert "paused" in left.values() and "sizing" in left.values(), left
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[C2] OK  정리는 진행 중·일시정지 스캔을 지우지 않음")


def check_c3_c4_upgrade_bundle() -> None:
    import gzip
    import tarfile
    from isilon_usage import upgrade as U
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in ("__init__.py", "cli.py"):
            data = b"x = 1\n" * 2000
            ti = tarfile.TarInfo("isilon_usage/" + name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    raw = buf.getvalue()
    gz = gzip.compress(raw)
    m = U._tar_members_from_bytes(gz)
    assert set(U.missing_members(m)) >= {"server.py", "scanner.py", "dashboard.html"}      # C4: 핵심 파일 빠짐 감지
    for bad in (gz[: len(gz) // 2], gz[:-8] + b"\0" * 8):                                 # C3: 잘림·CRC 손상
        try:
            U._tar_members_from_bytes(bad)
        except U._READ_ERRORS:
            pass
        else:
            raise AssertionError("손상된 묶음을 받아들임")
    print("[C3·C4] OK  잘림·손상·핵심 파일 누락 업그레이드 묶음 거부")


def check_c5_dst_schedule() -> None:
    import datetime as _dt
    from isilon_usage import settings as S
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        # 2026-03-08 02:00 → 03:00 (서머타임 시작). 02:30 예약은 03:00 경에 한 번만 돌아야 한다.
        ran = _dt.datetime(2026, 3, 8, 3, 0, 30).timestamp()
        sc = {"unit": "day", "every": 1, "at": "02:30", "anchor": ran - 86400 * 3, "last_run": ran}
        assert S.schedule_due(sc, ran + 600) is False      # 03:10 — 예전엔 03:30 까지 매 틱 True(반복 실행)
        sc["last_run"] = ran - 86400
        assert S.schedule_due(sc, ran + 10) is True
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()
    print("[C5] OK  서머타임 시작일 예약은 한 번만")


def check_c6_c7_cli_inputs() -> None:
    from isilon_usage import settings as S
    s = S.sanitize({"batch_size": 1e999, "scan_workers": float("inf"), "mount_bases": 5,
                    "schedules": [{"unit": "week", "weekdays": 3, "at": "01:00", "every": 1e999}]})
    assert isinstance(s["batch_size"], int) and s["mount_bases"] == [], s                # C7
    assert s["schedules"] == [] or s["schedules"][0]["weekdays"] == []
    from isilon_usage.scanner import Scanner                                             # C6
    import inspect
    src = inspect.getsource(Scanner.__init__)
    assert "max(1, int(batch_size or 1))" in src
    d = tempfile.mkdtemp(prefix="iu_cli_")
    try:
        for sub in (["stats", "--scan", "1"], ["resume", "--scan", "1"], ["partial", "--scan", "1"]):
            r = subprocess.run([sys.executable, "-m", "isilon_usage", "--data-dir", os.path.join(d, "none")] + sub,
                               capture_output=True, text=True, cwd=os.path.dirname(HERE), timeout=60)
            assert r.returncode == 2 and "Traceback" not in r.stderr, (sub, r.returncode, r.stderr[-300:])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[C6·C7·CLI] OK  batch_size 하한·Infinity/잘못된 형식 설정·없는 data-dir 안내")


def _mk_replica_db(path, root, rows, done=True):
    from isilon_usage import db as dbmod
    dbmod.init_db(path)
    c = dbmod.connect(path)
    rid = dbmod.create_run(c, root, backend="native", size_mode="disk", scanner_pid=0)
    for p, depth, sub, tb in rows:
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, subdir_count, total_bytes, own_bytes) "
                  "VALUES (?,?,?,?,?,?,?,?)", (rid, p, os.path.basename(p) or p, depth, "done", sub, tb, 0))
    c.commit()
    c.close()


def check_q1_q2_replica_compare() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_q1_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "A", "url": "http://10.0.0.1:8765", "token": "t"})
        rep = os.path.join(pc.replicas_dir, "A")
        os.makedirs(os.path.join(rep, "scans"))
        # 최신 상위 루트 스캔(/d, /d/p 에서 접힘) + 오래된 하위 루트 스캔(/d/p, 더 깊음)
        _mk_replica_db(os.path.join(rep, "scans", "top.db"), "/d",
                       [("/d", 0, 1, 900), ("/d/p", 1, 2, 800)])
        _mk_replica_db(os.path.join(rep, "scans", "sub.db"), "/d/p",
                       [("/d/p", 0, 2, 500), ("/d/p/x", 1, 0, 300), ("/d/p/y", 1, 0, 200)])
        meta = {"scans": [
            {"root_path": "/d", "status": "done", "finished_at": 2000, "db_filename": "top.db"},
            {"root_path": "/d/p", "status": "done", "finished_at": 1000, "db_filename": "sub.db"}]}
        with open(os.path.join(rep, "meta.json"), "w") as fh:
            json.dump(meta, fh)
        assert pc._replica_dbs_for("A", "/d/p")[0].endswith("top.db")          # Q2: 최신 상위 스캔 우선
        row = pc.compare_path("/d/p")["rows"][0]
        assert row["found"] and row["total_bytes"] == 800, row                   # 최신 상위 스캔의 값
        cm = pc.compare_matrix("/d/p")                                           # Q1: 접힌 첫 후보 → 다음 후보
        names = sorted(e["name"] for e in cm["children"])
        assert names == ["x", "y"] and cm["folded_nodes"] == [], cm
        rowx = pc.compare_path("/d/p/x")["rows"][0]
        assert rowx["found"] and rowx["total_bytes"] == 300, rowx
        os.remove(os.path.join(rep, "scans", "sub.db"))                          # 더 깊은 후보 없음 → '접힘'
        cm = pc.compare_matrix("/d/p")
        assert cm["children"] == [] and cm["folded_nodes"] == ["A"], cm
        rowx = pc.compare_path("/d/p/x")["rows"][0]
        assert not rowx["found"] and rowx.get("folded_at") == "/d/p", rowx
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[Q1·Q2] OK  복제 비교: 최신 스캔 우선·접힘이면 더 깊은 후보·그래도 없으면 '접힘' 표시")


def check_q3_nested_roots() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    d = tempfile.mkdtemp(prefix="iu_q3_")
    try:
        M.init_manager(d)
        for rp, b in (("/ifs/data", 1000), ("/ifs/data/proj", 400), ("/ifs/data2", 50)):
            sid = M.register_scan(d, root_path=rp, db_path=os.path.join(d, rp.replace("/", "_") + ".db"),
                                  backend="native", size_mode="disk")
            c = dbmod.connect(M.manager_db_path(d))
            M.update_scan(c, sid, status="done", phase="done", scanned_bytes=b)
            c.commit()
            c.close()
        c = dbmod.connect(M.manager_db_path(d))
        ov = M.overall_capacity(c)
        c.close()
        assert ov["total_capacity_bytes"] == 1050, ov["total_capacity_bytes"]     # 예전 1450(proj 두 번)
        nest = {r["root_path"]: r["nested_in"] for r in ov["roots"]}
        assert nest == {"/ifs/data": None, "/ifs/data/proj": "/ifs/data", "/ifs/data2": None}, nest
        assert M._nested_roots(["/", "/a", "/a/b"]) == {"/a": "/", "/a/b": "/"}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[Q3] OK  중첩 루트는 합계에서 한 번만")


def check_q4_disabled_ping() -> None:
    from isilon_usage import db as dbmod
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_q4_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "OFF", "url": "http://127.0.0.1:1", "token": "t", "enabled": False})
        pc._record_ping(pc.ping_nodes().get("results", []))
        c = dbmod.connect(P.ping_history_path(pc.data_dir))
        n = c.execute("SELECT COUNT(*) FROM ping_samples WHERE node_id='OFF'").fetchone()[0]
        c.close()
        assert n == 0, n
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[Q4] OK  비활성 노드는 핑 이력에 '다운'으로 기록하지 않음")


# ------------------------------------------------------------------ 3회차(포탈)
def check_t1_t5_portal_round3() -> None:
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_t1_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        # T1: 잘못된 새 이름으로 같은 서버를 프로비저닝해도 옛 노드·복제본이 지워지지 않는다
        pc.upsert_node({"id": "keep", "url": "http://10.0.0.5:8765", "token": "t"})
        os.makedirs(os.path.join(pc.replicas_dir, "keep", "scans"))
        for bad in ("a/b", "..", "."):
            r = pc.provision_plan({"host": "10.0.0.5", "port": 8765, "name": bad})
            assert r["ok"] is False, r
        assert [n["id"] for n in pc.nodes] == ["keep"] and os.path.isdir(os.path.join(pc.replicas_dir, "keep"))
        r = pc.provision_plan({"host": "10.0.0.5", "port": 8765, "name": "new1"})     # 정상 교체는 그대로
        assert r["ok"] and r["registered"] and r["replaced_prev"] == "keep", r
        assert [n["id"] for n in pc.nodes] == ["new1"]
        # T2: Infinity 주기 → 상한, 응답이 표준 JSON
        res = pc.set_backup_config(os.path.join(d, "bk"), float("inf"), 3)
        assert res["backup_every_hours"] == 8760.0
        json.loads(json.dumps(res, allow_nan=False))
        pc2 = P.PortalController(os.path.join(d, "hq"))
        assert pc2.settings["backup_every_hours"] == 8760.0
        # T3: 끊긴 노드의 옛 '진행 중' 루트는 처리량·스캔 중에서 제외
        now = time.time()
        pc.upsert_node({"id": "dead", "url": "http://10.0.0.9:8765", "token": "t"})
        pc._cache["dead"] = {"online": False, "ts": now - 9999, "edge_now": now, "overall": P._sanitize_overall(
            {"roots": [{"root_path": "/a", "status": "sizing", "phase": "sizing", "started_at": now - 10000,
                        "updated_at": now - 9999, "scanned_bytes": 1e12}]})}
        tw = {x["id"]: x for x in pc.throughput_windows()["nodes"]}
        assert tw["dead"]["scanning"] is False and not tw["dead"].get("since_start_bph"), tw["dead"]
        # T4: 문자열이 아닌 필드 → 사유(500 아님)
        for body in ({"url": 5}, {"url": "http://x:1", "id": 7}, {"url": "http://x:1", "region": 3}):
            assert pc.enroll_node(body)["ok"] is False
        assert pc.provision_plan({"host": 5})["ok"] is False
        # T5: 비교 API 는 상대 경로 거부(포탈 작업 폴더 기준으로 조용히 바뀌던 것)
        assert _read("../isilon_usage/portal.py").count('if not qp.startswith("/"):') == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[T1~T4] OK  프로비저닝 이름 선검증·Infinity 상한·끊긴 노드 처리량 제외·문자열 필드 검증")


SECTIONS = [check_e1_non_utf8_root, check_e2_stagger_kept, check_e3_pscan_partial_exit, check_e4_eta_active_time,
            check_p1_p2_p4_portal, check_p3_p6_p7_d1_d5_static, check_d2_ko_count, check_p5_d3_d4_http,
            check_f1_discovery_eta, check_r1_to_r5_edge,
            check_s1_sizing_window, check_c2_prune_keeps_active, check_c3_c4_upgrade_bundle, check_c5_dst_schedule,
            check_c6_c7_cli_inputs, check_q1_q2_replica_compare, check_q3_nested_roots, check_q4_disabled_ping,
            check_t1_t5_portal_round3]


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

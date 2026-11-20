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
    assert re.search(r"^\.card\{ overflow-x:auto; \}$", css, re.M)          # D5(3회차에 전 폭으로 확장)
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


def check_u1_u3_ui_round3() -> None:
    """U1 영어 모드가 폴더·노드 이름(데이터)까지 번역 U2 긴 지역명이 노드 id 를 가림 U3 데스크톱 폭 가로 넘침."""
    js = _read("i18n_en.js")
    assert js.count("closest('[translate=\"no\"]')") >= 2                     # U1: 데이터 요소는 건너뜀
    dh, ph = _read("dashboard.html"), _read("portal.html")
    for frag in ('class="piecrumb" data-i="${i}" translate="no"', 'class="topcrumb" data-i="${i}" translate="no"',
                 'class="piecell" data-i="${i}" style="flex:1;" translate="no"'):
        assert frag in dh, frag
    assert '<span class="nid" translate="no"' in ph and 'class="link cmpdrill" data-name="${escAttr(k.name)}" translate="no"' in ph
    assert ".portal .nitem .nreg{ flex:0 1 auto; max-width:40%;" in ph                # U2
    css = _read("theme.css")
    assert re.search(r"^\.card\{ overflow-x:auto; \}$", css, re.M), "U3: 카드 가로 스크롤이 전 폭에 적용돼야 함"
    print("[U1~U3] OK  데이터 이름은 번역 제외·긴 지역명 말줄임·데스크톱 폭 카드 안 가로 스크롤")


def check_v1_v6_edge_round3() -> None:
    """V1 분석 워커 상한 V2/V3 Infinity 500 V4 비밀값 타입 500 V5 1자 비밀번호 V6 "false" 문자열이 참."""
    from isilon_usage import server as SV
    assert SV._truthy("false") is False and SV._truthy("no") is False and SV._truthy("1") is True
    assert SV._truthy(True) is True and SV._truthy(0) is False
    d = tempfile.mkdtemp(prefix="iu_v1_")
    tree = os.path.join(d, "t")
    os.makedirs(os.path.join(tree, "a"))
    httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                     initial_settings={"mount_bases": [d]}, enable_scan=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    c = httpd.RequestHandlerClass.controller

    def post(path, raw, tok=""):
        rq = urllib.request.Request(base + path, data=raw.encode(), method="POST",
                                    headers={"Content-Type": "application/json", "X-Op-Token": tok})
        try:
            r = urllib.request.urlopen(rq)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, None
    try:
        before = threading.active_count()
        st, j = post("/api/analyze/start", json.dumps({"path": tree, "workers": 20000}))
        assert st == 200, st
        peak = 0
        for _ in range(40):
            peak = max(peak, threading.active_count() - before)
            if not (c._analyze or {}).get("running"):
                break
            time.sleep(0.05)
        assert peak <= 70, peak                                                       # V1: 20000 스레드 아님
        st, _j = post("/api/analyze/start", '{"path": "%s", "max_depth": 1e999}' % tree)
        assert st != 500, st                                                          # V2
        c.setup_op_password("test1234")
        tok = c.unlock("test1234")["token"]
        st, _j = post("/api/scan/start", '{"path": "%s", "fold_depth": 1e999}' % tree, tok)
        assert st != 500, st                                                          # V3
        st, _j = post("/api/settings", json.dumps({"settings": {"api_token": 123}}), tok)
        assert st == 400, st                                                          # V4
        st, _j = post("/api/settings", json.dumps({"settings": {"op_lock": True, "op_password": "x"}}), tok)
        assert st == 400, st                                                          # V5
        assert c.unlock("test1234").get("token")                                      # 비밀번호 그대로
    finally:
        httpd.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    print("[V1~V6] OK  분석 워커 상한·Infinity 400/기본값·비밀값 타입·비밀번호 4자·문자열 불리언")


# ------------------------------------------------------------------ 4회차
def check_w1_w2_round4() -> None:
    """W1 포탈 비밀번호 길이 규칙 없음·encrypt 문자열 "false" 가 참 W2 atime CSV 수식 주입."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_w1_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        for bad in (" ", "a", "abc", "   x   "):
            r = pc.set_password(bad, False)
            assert r["ok"] is False and r.get("_status") == 400, (bad, r)
        r = pc.set_password("abcd1234", "false")
        assert r["ok"] and r["encrypted"] is False, r
        assert pc.set_password("abcd1234", True)["encrypted"] is True
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    m = re.search(r"function atimeCsv\(\)[\s\S]*?const esc = (s => \{[\s\S]*?\});", dh)
    assert m, "atimeCsv esc 없음"
    js = "const esc = %s; console.log(JSON.stringify([esc('=HYPERLINK(1)'), esc('@x'), esc('-2+3'), esc('a\"b')]));" % m.group(1)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30).stdout.strip()
    assert json.loads(out) == ['"\'=HYPERLINK(1)"', '"\'@x"', '"\'-2+3"', '"a""b"'], out
    print("[W1·W2] OK  포탈 비밀번호 4자·encrypt 문자열 해석·atime CSV 수식 주입 방지")


def check_x1_x8_settings_round4() -> None:
    """X1 주소가 바뀌어도 옛 비밀번호 승계 X2 업그레이드 확인 주기 60 으로 되돌림 X3 소스 주소 칸 두 개
    X4 시간 단위 시각 표시(무시됨) X5 범위 밖 숫자 조용히 보정 X6 시차 배치가 저장 안 한 칸을 되돌림
    X7 API 토큰을 화면에서 지울 수 없음 X8 노드 수정 시 저장된 상태 초기화."""
    from isilon_usage import server as SV
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_x1_")
    try:
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0, enable_scan=True)
        c = httpd.RequestHandlerClass.controller
        arr = {"id": "u1", "type": "isilon", "name": "u1", "url": "https://u1", "user": "a", "password": "apw"}
        assert c.update_settings({"storage_arrays": [arr], "isilon_url": "https://i1", "isilon_password": "ipw",
                                  "upgrade_check_secs": 300})["ok"]
        c.update_settings({"storage_arrays": [dict(arr, password="")], "isilon_url": "https://i1",
                           "isilon_password": ""})                                  # 같은 주소·빈칸 → 유지
        assert c.settings["storage_arrays"][0]["password"] == "apw" and c.settings["isilon_password"] == "ipw"
        c.update_settings({"storage_arrays": [dict(arr, url="https://other", password="")],
                           "isilon_url": "https://i2", "isilon_password": ""})        # X1: 주소 바뀜 → 승계 안 함
        assert c.settings["storage_arrays"][0]["password"] == "" and c.settings["isilon_password"] == "", \
            (c.settings["storage_arrays"], c.settings["isilon_password"])
        assert c.upgrade_status()["check_secs"] == 300                                 # X2
        c.update_settings({"api_token": "tok1"})
        c.update_settings({"api_token": ""})
        assert c.settings["api_token"] == "tok1"
        c.update_settings({"api_token": "", "api_token_clear": True})                  # X7
        assert c.settings["api_token"] == "" and "api_token_clear" not in c.settings
        httpd.server_close()
        pc = P.PortalController(os.path.join(d, "hq"))                                 # X8
        pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "token": "t"})
        with pc._lock:
            pc._get("e1")["last_status"] = "online"
            pc._get("e1")["last_error"] = "x"
        pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "token": "", "region": "서울"})
        assert pc._get("e1")["last_status"] == "online" and pc._get("e1")["last_error"] == "x"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh, ph = _read("dashboard.html"), _read("portal.html")
    assert "setUpgUrl" not in dh and 'id="goUpgSrc"' in dh                             # X3: 한 곳에서만
    assert '$("upgSecs").value = j.check_secs' in dh
    assert 'if (u === "hour") return `매 ${y}시간마다`;' in dh and 'if(u==="hour") return `매 ${y}시간마다`;' in ph   # X4
    assert '(u !== "minute" && u !== "hour") ? "inline-block"' in dh and '(u!=="minute"&&u!=="hour")?"inline-block"' in ph
    assert "const bad = settingsNumberProblem();" in dh                                  # X5
    m = re.search(r"async function staggerSchedules\(\) \{[\s\S]*?\n\}", dh)          # X6
    assert m and "loadSettings()" not in m.group(0) and "_schedLoaded" in m.group(0)
    print("[X1~X8] OK  주소 바뀌면 비밀번호 재입력·확인 주기 유지·소스 주소 단일화·시간 단위 표시·숫자 검사·시차 배치·토큰 지우기·노드 상태 유지")


# ------------------------------------------------------------------ 5회차(연동·CLI·알림)
def check_y1_y6_integrations_round5() -> None:
    """Y1 gentest --size inf traceback Y2 메일 메시지 만들기 예외(줄바꿈 제목·비 UTF-8) Y3 로컬 경로에 엉뚱한 NFS 진단
    Y4 빈 장비 사용률 없음 Y5 PowerStore 마지막 표본이 비면 용량 버림 Y6 PowerStore 오류 문구 "0"."""
    from isilon_usage import cli, notify, systune, storage_status
    from isilon_usage.powerstore_api import PowerStoreClient
    for bad in ("inf", "1e999", "1e400T", "abc", "nan"):
        assert cli._parse_size(bad) is None, bad
    assert cli._parse_size("4K") == 4096 and cli._parse_size("0") == 0
    d = tempfile.mkdtemp(prefix="iu_y1_")
    try:
        r = subprocess.run([sys.executable, "-m", "isilon_usage", "gentest", os.path.join(d, "g"), "--size", "inf",
                            "--yes"], capture_output=True, text=True, cwd=os.path.dirname(HERE), timeout=60)
        assert r.returncode == 2 and "Traceback" not in r.stderr, (r.returncode, r.stderr[-300:])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    st = {"notify_email": "a@b.c", "smtp_host": "127.0.0.1", "smtp_port": 1}
    assert notify.send_email(st, "scan /data/x\ny", "b", timeout=0.5) is False                 # Y2
    assert notify.send_email(st, "s", "b \udcff", timeout=0.5) is False
    orig = systune.parse_nfs_mounts                                                                # Y3
    systune.parse_nfs_mounts = lambda: [{"mount": "/mnt/nas", "fstype": "nfs", "src": "x:/y",
                                         "options": {"soft": True, "nconnect": "1"}}]
    try:
        assert systune._pick_target(systune.parse_nfs_mounts(), "/data/local") is None
        assert systune._pick_target(systune.parse_nfs_mounts(), "/mnt/nas/a")["mount"] == "/mnt/nas"
    finally:
        systune.parse_nfs_mounts = orig
    assert storage_status._cap(1000, 0)["used_pct"] == 0.0                                         # Y4
    c = PowerStoreClient("http://127.0.0.1:1", "u", "p")
    c._get = lambda path: ({"error": "x"} if path.startswith("/cluster") else [])
    out = c.status()
    assert out["error"] != "0" and "예상 밖" in out["error"], out                                  # Y6
    c._get = lambda path: ([{"id": "c1", "name": "ps", "state": "ok"}] if path.startswith("/cluster") else [])
    c._post = lambda path, body: [{"physical_total": 1000, "physical_used": 10}, {"physical_total": None}]
    out = c.status()
    assert out.get("capacity", {}).get("total") == 1000 and out["capacity"]["used_pct"] == 1.0, out   # Y5
    print("[Y1~Y6] OK  gentest 크기 거부·메일 예외 없음·로컬 경로 NFS 진단 없음·0% 사용률·PowerStore 표본·오류 문구")


def check_z1_z8_portal_ui_round5() -> None:
    """Z1 CSV 내보내기→가져오기에서 사용 여부·주기 유실 Z2 비활성 노드가 DOWN Z3 요청 중 기간 버튼 무시
    Z4 '분 전'·차트가 PC 시계 Z5 펼친 행이 5초마다 닫힘 Z6 '마지막 응답'이 실패 시도 시각 Z7 감사 로그 대상 없음
    Z8 CSV BOM·수식 주입."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_z1_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "n1", "url": "http://10.0.0.1:8765", "token": "t", "mode": "poll", "enabled": False,
                        "unit": "week", "every": 2, "at": "04:10", "weekdays": [1, 3], "region": "서울"})
        # 화면 내보내기와 같은 열(헤더·BOM·' 접두)로 만든 CSV — 지우고 가져오면 같은 노드가 돌아와야 한다
        csv_text = ("\ufeffid,url,region,token,alias_local,alias_logical,mode,enabled,unit,every,at,weekdays,"
                    "start_month,start_day\r\nn1,http://10.0.0.1:8765,서울,,'=cmd,,poll,false,week,2,04:10,1;3,1,1")
        pc.delete_node("n1")
        r = pc.import_nodes_csv(csv_text)
        assert r["ok"] and r["added"] == 1, r
        n = pc._get("n1")
        assert (n["mode"], n["enabled"], n["unit"], n["every"], n["at"], n["weekdays"], n["alias_local"]) == \
            ("poll", False, "week", 2, "04:10", [1, 3], "=cmd"), n                                # Z1·Z8
        assert P.sanitize_node({"id": "a", "url": "http://x", "enabled": "false"})["enabled"] is False
        assert P._audit_target({"id": "n1", "token": "secret", "password": "pw"}) == "id=n1"     # Z7(비밀값 제외)
        assert "last_ok" in P.sanitize_node({"id": "a", "url": "http://x"})                       # Z6
        ph = pc.ping_history(86400)
        assert "server_time" in ph                                                                # Z4
        rt = {"status": "done", "finished_at": 1000.0, "updated_at": 990.0, "started_at": 900.0}
        moved = P.PortalController._edge_to_portal_clock(rt, recv_ts=20000.0, edge_now=9200.0)   # 엣지 3시간 늦음
        assert moved["finished_at"] == 11800.0 and rt["finished_at"] == 1000.0, moved            # 원본은 그대로
        assert P.PortalController._edge_to_portal_clock(rt, 1000.5, 1000.0) is rt                 # 전송 지연 수준은 무시
    finally:
        shutil.rmtree(d, ignore_errors=True)
    h = _read("portal.html")
    assert "Date.now()/1000-ts" not in h and "Date.now()/1000 - n.last_poll" not in h               # Z4
    assert "down=nodes.filter(n=>!n.online && !n.disabled).length" in h                              # Z2
    assert "if(phInflight){ _phAgain=true; return; }" in h                                         # Z3
    assert "_openNodeRows.has(n.id)" in h and "const _openNodeRows = new Set();" in h               # Z5
    assert 'n.last_ok?("마지막 응답 "+ago(n.last_ok))' in h                                       # Z6
    assert 'new Blob(["\\uFEFF"+lines.join' in h and '"enabled","unit","every"' in h               # Z8·Z1
    print("[Z1~Z8] OK  CSV 왕복 보존·비활성≠DOWN·기간 버튼 대기열·포탈 시계·펼침 유지·마지막 성공 응답·감사 대상·BOM")


# ------------------------------------------------------------------ 6회차(예약·복제·분석)
def check_aa_round6() -> None:
    """A1 순차 예약의 중간 경로 실패가 뒤 경로를 이번 주기 내내 건너뜀 A2 DB 가 안 온 스캔을 복제 완료로 보고 워터마크 전진
    A3 질문 기능: 0일→365일·연도를 기간으로·'next' 를 확장자 질문으로."""
    from isilon_usage import server as SV, ask
    from isilon_usage import manager as M
    d = tempfile.mkdtemp(prefix="iu_aa_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        roots = []
        for nm in ("A", "B", "C"):
            p = os.path.join(d, "m", nm)
            os.makedirs(p)
            with open(os.path.join(p, "f"), "wb") as fh:
                fh.write(b"x" * 100)
            roots.append(p)
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [os.path.join(d, "m")]}, enable_scan=True)
        c = httpd.RequestHandlerClass.controller
        _orig = c._chain_start
        c._chain_start = lambda sc, path, running, rp: (None if path == roots[1] else _orig(sc, path, running, rp))
        now = time.time()
        c.update_settings({"schedules": [{"paths": roots, "unit": "day", "every": 1, "at": "00:00", "enabled": True,
                                          "anchor": now - 2 * 86400}]}, merge_runtime=False)
        started = set()
        for _ in range(60):
            c._check_schedules()
            conn = __import__("isilon_usage.db", fromlist=["x"]).connect(M.manager_db_path(os.path.join(d, "data")))
            started = {r[0] for r in conn.execute("SELECT root_path FROM scans")}
            conn.close()
            if roots[2] in started:
                break
            time.sleep(0.2)
        assert roots[0] in started and roots[2] in started, started                  # A1: B 를 건너뛰고 C 진행
        httpd.server_close()
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    src = _read("server.py")
    assert "and s.get(\"db_path\") and os.path.exists(s.get(\"db_path\"))]" in src                 # A2(엣지)
    # A2(포탈): meta 에 2건, DB 는 1건만 온 복제 묶음 → 워터마크는 빠진 스캔 앞에서 멈추고 오류 표시
    import tarfile
    from isilon_usage import portal as P
    d2 = tempfile.mkdtemp(prefix="iu_aa2_")
    try:
        meta = {"scans": [{"id": 1, "root_path": "/a", "status": "done", "finished_at": 100.0, "db_filename": "s1.db"},
                          {"id": 2, "root_path": "/b", "status": "done", "finished_at": 200.0, "db_filename": "s2.db"},
                          {"id": 3, "root_path": "/c", "status": "done", "finished_at": 300.0, "db_filename": "s3.db"}]}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in (("meta.json", json.dumps(meta).encode()), ("scans/s1.db", b"x"), ("scans/s3.db", b"z")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        pc = P.PortalController(os.path.join(d2, "hq"))
        pc.upsert_node({"id": "n1", "url": "http://10.0.0.1:8765", "token": "t", "mode": "replicate"})
        orig = P._http_get
        P._http_get = lambda url, token, timeout=0: io.BytesIO(buf.getvalue())
        try:
            pc._replicate_one("n1")
        finally:
            P._http_get = orig
        ls = pc._get("n1")["last_sync"]
        assert 100.0 <= ls < 200.0, ls                       # 예전엔 300(빠진 #2 를 넘어 다시 받지 않음)
        assert "받지 못함" in pc._rep_err.get("n1", ""), pc._rep_err
        # 같은 누락이 또 오면(구버전 엣지) 건너뛰고 진행 — 매번 뒤의 DB 전부를 다시 받지 않게
        P._http_get = lambda url, token, timeout=0: io.BytesIO(buf.getvalue())
        try:
            pc._replicate_one("n1")
        finally:
            P._http_get = orig
        assert pc._get("n1")["last_sync"] == 300.0 and not pc._rep_err.get("n1"), (pc._get("n1"), pc._rep_err)
    finally:
        shutil.rmtree(d2, ignore_errors=True)
    assert ask.parse_threshold_days("2024년 이후 안 바뀐") is None and ask.parse_threshold_days("0일") == 0   # A3
    data = {"overall": {"roots": []}}
    out = ask.answer("다음 스캔은 언제? next", data)
    assert out.get("intent") != "extensions", out.get("intent")
    print("[A1~A3] OK  순차 예약 실패 경로 건너뛰고 계속·DB 없는 스캔 미복제·질문 기능 기간/키워드")


def check_ab_edge_views_round6() -> None:
    """B1 중복 id 로 튜닝 점검 결과가 안 보임 B2 미완료 스캔과 비교·변화 Top(모든 폴더가 '삭제') B3 지운 스캔이 선택된 채 남음
    B4 스캔 전환 후 비교 결과 잔존 B5 재개 스캔 소요 시간에 쉬던 시간 포함 B6 빈 검색어로 트리 복귀 안 됨 B7 일시정지 스캔이 추세 점."""
    from collections import Counter
    for name in ("dashboard.html", "portal.html"):                                  # B1: 같은 id 는 한 번만
        ids = re.findall(r'\sid="([^"$`{}]+)"', _read(name))
        dup = [k for k, v in Counter(ids).items() if v > 1]
        assert not dup, (name, dup)
    from isilon_usage import server as SV, db as dbmod
    from isilon_usage import manager as M
    d = tempfile.mkdtemp(prefix="iu_ab_")
    try:
        M.init_manager(d)
        ids = []
        for st in ("done", "paused"):
            sid = M.register_scan(d, root_path="/r", db_path=os.path.join(d, "s%d.db" % len(ids)),
                                  backend="native", size_mode="disk")
            dbmod.init_db(os.path.join(d, "s%d.db" % len(ids)))
            c = dbmod.connect(M.manager_db_path(d))
            M.update_scan(c, sid, status=st, active_elapsed=0.9, started_at=time.time() - 800)
            c.commit()
            c.close()
            ids.append(sid)
        r = SV.diff_scans(d, ids[0], ids[1])                                         # B2
        assert r["ok"] is False and r.get("not_done"), r
        c = dbmod.connect(M.manager_db_path(d))
        roots = M.overall_capacity(c)["roots"]
        c.close()
        assert all("active_elapsed" in x for x in roots)                            # B5(홈 표에 활성 시간)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert 'const out = $("setTuneResult");' in dh
    assert 's.status === "done" || s.partial_agg_at' in dh                          # B2(화면)
    assert "if (_lostSel) setTimeout(poll, 0);" in dh                                # B3
    assert '$("fhPath").value = "";' in dh                                           # B4
    assert "function scanActiveSec(r, running)" in dh and "endt - r.started_at) : null;\n      const running" not in dh   # B5
    assert "if (!q) { treeStack = []; loadTree(null, []); return; }" in dh           # B6
    assert 's.scanned_bytes != null && s.status === "done")' in dh                   # B7
    print("[B1~B7] OK  중복 id 없음·미완료 비교 거부·지운 스캔 선택 해제·비교 초기화·활성 소요 시간·검색 복귀·추세 완료만")


# ------------------------------------------------------------------ 7회차(업그레이드·설치·동시성)
def check_ac_round7() -> None:
    """C1 SSH 업그레이드·프로비저닝: 잘린 묶음을 운영 폴더에 바로 풀고 다운그레이드·되돌리기 없음 C2 재구성 시 토큰·경로 무시
    C3 설치 스크립트: cp 경로의 낡은 바이트코드·데이터 폴더 검사 전에 서비스 중지 C4 make_release --help 가 발행본을 덮음
    C5 심볼릭 링크로 같은 루트 중복 스캔 C6 일괄 푸시에서 이미 최신 노드를 실패로 기록."""
    from isilon_usage import portal as P
    up = P.build_upgrade_script(hq_base="http://h:1")
    pv = P.build_provision_script(host="10.0.0.1", port=8765, token="T", path="/mnt/x", hq_base="http://h:1",
                                  install="systemd")
    for sc in (up, pv):                                                              # C1: 공용 안전 설치 조각
        assert "gzip -t" in sc and "ALLOW_DOWNGRADE" in sc and "되돌림" in sc and "| sudo tar -xz" not in sc
        assert "@@STAGED@@" not in sc
        r = subprocess.run(["bash", "-n"], input=sc, text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
    assert 's["api_token"] = tok' in pv                                               # C2
    for f in ("install_edge.sh", "install_portal.sh"):                               # C3
        sh = open(os.path.join(os.path.dirname(HERE), "tools", f), encoding="utf-8").read()
        assert sh.index("이 설치 폴더($INSTALL_DIR) 안에 있습니다") < sh.index('systemctl stop "$SERVICE"'), f
        assert "-name __pycache__ -type d -prune -exec rm -rf" in sh, f
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "tools", "make_release.py"), "--x"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.returncode                                           # C4: 인자 오면 아무것도 안 만듦
    from isilon_usage import server as SV                                            # C5
    d = tempfile.mkdtemp(prefix="iu_ac_")
    try:
        tree = os.path.join(d, "m", "t")
        os.makedirs(tree)
        os.symlink(tree, os.path.join(d, "m", "link"))
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [os.path.join(d, "m")]}, enable_scan=True)
        c = httpd.RequestHandlerClass.controller
        with c._lock:
            c._scans[999] = {"path": tree}
        try:
            assert c._active_scan_on(os.path.join(d, "m", "link")) == 999
        finally:
            with c._lock:
                c._scans.pop(999, None)
        httpd.server_close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert 'okn + curn == len(nodes)' in _read("portal.py")                         # C6(11회차: 전부 최신도 성공)
    print("[C1~C6] OK  안전한 SSH 업그레이드·재구성 토큰 반영·설치 순서/캐시·릴리스 인자·심볼릭 중복·이미 최신 집계")


# ------------------------------------------------------------------ 8회차(포탈 백업·설정)
def check_ad_portal_round8() -> None:
    """D1 깨진 노드 파일 → 빈 목록 → 다음 저장이 덮어 모든 노드 유실 D2 로그인 없이 설정 조회로 서버 경로 노출
    D3 로그인 없이 외부 LLM 호출 D4 백업 다운로드 캐시 D5 세션·백업·LLM 설정의 조용한 초기화."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_ad_")
    try:
        hq = os.path.join(d, "hq")
        pc = P.PortalController(hq)
        for i in range(3):
            pc.upsert_node({"id": "n%d" % i, "url": "http://10.0.0.%d:8765" % (i + 1), "token": "t%d" % i})
        pc.upsert_node({"id": "n0", "url": "http://10.0.0.1:8765", "token": "", "region": "서울"})   # .bak 생성
        np = P.nodes_path(hq)
        raw = open(np, "rb").read()
        with open(np, "wb") as fh:
            fh.write(raw[: len(raw) // 2])                                             # D1: 잘림
        pc2 = P.PortalController(hq)
        assert sorted(n["id"] for n in pc2.nodes) == ["n0", "n1", "n2"], pc2.nodes
        assert os.path.exists(np + ".broken")
        pc2.upsert_node({"id": "new", "url": "http://10.0.0.9:8765", "token": "x"})
        assert len(P.load_nodes(hq)) == 4
        assert (os.stat(np).st_mode & 0o777) == 0o600
        r = pc2.set_session_prefs({"op_ttl_minutes": 120})                           # D5
        r = pc2.set_session_prefs({"op_ttl_minutes": "abc"})
        assert r["ok"] is False and pc2.settings["op_ttl_minutes"] == 120, r
        pc2.set_backup_config(os.path.join(d, "bk"), 2, 3)
        pc2.set_backup_config(None, 5, None)
        assert pc2.settings["backup_dir"] == os.path.join(d, "bk") and pc2.settings["backup_every_hours"] == 5
        assert pc2.set_backup_config("rel/bk", 1, 1)["ok"] is False
        pc2.set_ask_llm({"ask_llm_key": "K"})
        pc2.set_ask_llm({"ask_llm_key": None})
        assert pc2.settings["ask_llm_key"] == "K"
        # D2·D3·D4: HTTP
        httpd = P.serve_portal(hq, host="127.0.0.1", port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        c = httpd.RequestHandlerClass.controller
        c.set_password("test1234", False)
        c.set_backup_config(os.path.join(d, "secretbk"), 1, 3)
        calls = []
        orig = P.askmod.respond
        P.askmod.respond = lambda q, data, cfg: (calls.append(cfg["enabled"]) or {"ok": True, "answer": "x"})
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        try:
            c.set_ask_llm({"ask_llm_enabled": True, "ask_llm_endpoint": "http://127.0.0.1:1/v1", "ask_llm_key": "K"})
            anon = json.loads(urllib.request.urlopen(base + "/api/portal/settings").read().decode())
            assert anon.get("redacted") and "backup_dir" not in anon and "ask_llm_endpoint" not in anon, anon
            urllib.request.urlopen(base + "/api/portal/ask?q=" + urllib.request.quote("용량"))
            tok = c._auth.issue_token()
            rq = urllib.request.Request(base + "/api/portal/settings", headers={"X-Op-Token": tok})
            full = json.loads(urllib.request.urlopen(rq).read().decode())
            assert full.get("backup_dir") == os.path.join(d, "secretbk"), full.get("backup_dir")
            rq = urllib.request.Request(base + "/api/portal/ask?q=x", headers={"X-Op-Token": tok})
            urllib.request.urlopen(rq)
            assert calls == [False, True], calls                                     # 익명은 LLM 안 씀
            rq = urllib.request.Request(base + "/api/portal/backup/download", data=b"{}", method="POST",
                                        headers={"Content-Type": "application/json", "X-Op-Token": tok})
            resp = urllib.request.urlopen(rq)
            assert resp.headers.get("Cache-Control") == "no-store"                     # D4
        finally:
            P.askmod.respond = orig
            httpd.shutdown()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[D1~D5] OK  노드 파일 손상 복구·익명 설정 감춤·익명 LLM 차단·백업 no-store·설정 조용한 초기화 없음")


def check_ae_regress_round8() -> None:
    """E1 'ext별' 같은 붙은 조사로 확장자 질문 인식 E2 CLI 재개가 옛 관리 DB 에 새 칸을 못 써 기록 누락
    E3 업그레이드 스크립트가 백업이 없어도 백업 경로 표시 E4 느린 응답을 시계 차이로 보정."""
    from isilon_usage import ask, portal as P, manager as M, db as dbmod
    assert ask._h_extensions("ext별 용량", {"detail": {"extensions": [{"key": ".txt", "bytes": 1, "files": 1}]}})
    assert ask._h_extensions("next scan", {"detail": {"extensions": [{"key": ".txt", "bytes": 1, "files": 1}]}}) is None
    d = tempfile.mkdtemp(prefix="iu_ae_")
    try:
        M.init_manager(d)
        c = dbmod.connect(M.manager_db_path(d))
        cols = [r[1] for r in c.execute("PRAGMA table_info(scans)")]
        c.close()
        assert "active_elapsed" in cols
        assert "mgrmod.init_manager(data_dir)" in _read("cli.py").split("def cmd_resume", 1)[1][:900]   # E2
    finally:
        shutil.rmtree(d, ignore_errors=True)
    up = P.build_upgrade_script(hq_base="http://h:1")
    assert 'BK=""' in up and 'if [ -d "${TARGET}/isilon_usage" ]; then BK=' in up                     # E3
    rt = {"status": "done", "finished_at": 1000.0}
    assert P.PortalController._edge_to_portal_clock(rt, 1030.0, 1000.0) is rt                       # E4: 30초 지연
    assert P.PortalController._edge_to_portal_clock(rt, 11800.0, 1000.0)["finished_at"] == 11800.0
    print("[E1~E4] OK  붙은 조사 확장자·CLI 재개 스키마·백업 표시·느린 응답 보정 안 함")


# ------------------------------------------------------------------ 9회차(엣지 2대+포탈 연동)
def check_af_multinode_round9() -> None:
    """F1 같은 루트를 다시 스캔해 지운 폴더가 옛 복제 스캔에서 '있음'으로 보임(2회차 후보 순회의 부작용)
    F2 비활성으로 바꾼 노드의 옛 '온라인·스캔 중'이 합계·진행 목록에 남음."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_af_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "B", "url": "http://10.0.0.2:8765", "token": "t"})
        rep = os.path.join(pc.replicas_dir, "B")
        os.makedirs(os.path.join(rep, "scans"))
        _mk_replica_db(os.path.join(rep, "scans", "old.db"), "/p",
                       [("/p", 0, 2, 900), ("/p/d5", 1, 1, 300), ("/p/d5/s0", 2, 0, 300), ("/p/a", 1, 0, 600)])
        _mk_replica_db(os.path.join(rep, "scans", "new.db"), "/p",
                       [("/p", 0, 2, 700), ("/p/a", 1, 0, 600), ("/p/newdir", 1, 0, 100)])
        with open(os.path.join(rep, "meta.json"), "w") as fh:
            json.dump({"scans": [
                {"root_path": "/p", "status": "done", "finished_at": 100, "db_filename": "old.db"},
                {"root_path": "/p", "status": "done", "finished_at": 200, "db_filename": "new.db"}]}, fh)
        row = pc.compare_path("/p/d5")["rows"][0]
        assert row["found"] is False and not row.get("folded_at"), row             # F1: 지운 폴더는 없음
        cm = pc.compare_matrix("/p/d5")
        assert cm["children"] == [] and cm["folded_nodes"] == [], cm
        names = sorted(e["name"] for e in pc.compare_matrix("/p")["children"])
        assert names == ["a", "newdir"], names                                    # 부모와 자식 화면이 일치
        now = time.time()                                                          # F2
        pc._cache["B"] = {"online": True, "ts": now, "edge_now": now, "overall": P._sanitize_overall(
            {"root_count": 1, "active_scans": 1, "roots": [{"root_path": "/p", "status": "sizing", "phase": "sizing",
             "started_at": now - 100, "updated_at": now - 1, "processed_dirs": 5, "total_dirs": 10}]})}
        assert pc.overview()["totals"]["active_scans"] == 1
        pc.upsert_node({"id": "B", "url": "http://10.0.0.2:8765", "token": "", "enabled": False})
        ov = pc.overview()
        assert ov["totals"]["active_scans"] == 0 and ov["running"] == [], (ov["totals"], ov["running"])
        assert not ov["nodes"][0]["online"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[F1·F2] OK  다시 스캔해 지운 폴더는 '없음'(옛 스캔 안 봄)·비활성 노드는 온라인/진행에서 제외")


def check_ag_edge_flow_round9() -> None:
    """G1 끝난 오토튜닝 결과가 새로고침마다 스캔 선택을 빼앗음 G2 끝난 pscan 이 '워커 9개 탐색 중·150/3151·사용 0 B'
    G3 스캔 화면에 중지 버튼 없음."""
    from isilon_usage import pscan
    d = tempfile.mkdtemp(prefix="iu_ag_")
    try:
        root = os.path.join(d, "t")
        for i in range(5):
            os.makedirs(os.path.join(root, "d%d" % i, "s"))
            with open(os.path.join(root, "d%d" % i, "s", "f"), "wb") as fh:
                fh.write(b"x" * 100)
        res = pscan.parallel_scan(root, processes=2, threads_per_proc=1)
        dbp = os.path.join(d, "r.db")
        from isilon_usage import db as dbmod
        dbmod.init_db(dbp)
        c = dbmod.connect(dbp)
        rid = dbmod.create_run(c, root, backend="pscan", size_mode="disk", scanner_pid=0)
        dbmod.update_run(c, rid, worker_dirs=json.dumps(["/x/a", "/x/b"]), current_dir="/x/a", active_workers=2)
        c.commit()
        c.close()
        pscan.write_run_db(dbp, root, res, run_id=rid)
        c = dbmod.connect(dbp)
        r = dict(c.execute("SELECT * FROM scan_runs WHERE id=?", (rid,)).fetchone())
        c.close()
        assert r["processed_dirs"] == r["total_dirs"] and not r["worker_dirs"] and not r["current_dir"], r   # G2
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "processed_dirs=int(res[\"total_dirs\"]),   # 끝났으면 전부 처리" in _read("server.py")
    dh = _read("dashboard.html")
    assert "if (j.scan_id && _autotuneSawRunning && _autotuneTracked !== j.scan_id)" in dh            # G1
    assert 'if (name === "scanrun" || name === "summary") mountRunsCard(name);' in dh                  # G3
    print("[G1~G3] OK  옛 오토튜닝이 선택 안 빼앗음·끝난 pscan 진행 기록 정리·스캔 화면에 진행 카드(중지)")


# ------------------------------------------------------------------ 10회차(포탈 연동 심화)
def check_ah_clock_rewind_round10() -> None:
    """H1 엣지 시계가 뒤로 가면(NTP 보정) 복제 워터마크가 미래에 걸려 새 스캔을 영영 안 받음
    H2 보관 정리·비교가 미래로 찍힌 옛 스캔을 '최신'으로 보고 새 스캔을 버림."""
    import tarfile
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_ah_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.set_replica_keep(1)
        now = time.time()
        pc.upsert_node({"id": "B", "url": "http://10.0.0.2:8765", "token": "t", "mode": "replicate"})
        rep = os.path.join(pc.replicas_dir, "B")
        os.makedirs(os.path.join(rep, "scans"))
        _mk_replica_db(os.path.join(rep, "scans", "old.db"), "/x", [("/x", 0, 0, 111)])
        future = now + 3 * 3600
        with open(os.path.join(rep, "meta.json"), "w") as fh:
            json.dump({"scans": [{"id": 4, "root_path": "/x", "status": "done", "finished_at": future,
                                  "db_filename": "old.db", "received_at": now - 600}]}, fh)
        with pc._lock:
            pc._get("B")["last_sync"] = future                        # 시계가 +3h 이던 때 받은 워터마크
        pc._cache["B"] = {"online": True, "ts": now, "edge_now": now, "overall": P._sanitize_overall(
            {"roots": [{"root_path": "/x", "status": "done", "finished_at": now - 10, "scan_id": 5}]})}
        assert pc._effective_since("B", future) == (0.0, True)       # 기준을 되돌림
        newdb = os.path.join(d, "new.db")
        _mk_replica_db(newdb, "/x", [("/x", 0, 0, 222)])
        meta = {"scans": [{"id": 5, "root_path": "/x", "status": "done", "finished_at": now - 10,
                           "db_filename": "new.db"},
                          {"id": 4, "root_path": "/x", "status": "done", "finished_at": future,
                           "db_filename": "old.db"}]}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, data in (("meta.json", json.dumps(meta).encode()), ("scans/new.db", open(newdb, "rb").read()),
                               ("scans/old.db", open(os.path.join(rep, "scans", "old.db"), "rb").read())):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        orig = P._http_get
        P._http_get = lambda url, token, timeout=0: io.BytesIO(buf.getvalue())
        try:
            pc._replicate_one("B")
        finally:
            P._http_get = orig
        ls = pc._get("B")["last_sync"]
        assert ls <= now + 1, (ls, now)                                      # H1: 미래 워터마크가 풀림
        assert "시계" in pc._rep_err.get("B", ""), pc._rep_err
        assert os.path.exists(os.path.join(rep, "scans", "new.db"))
        row = pc.compare_path("/x")["rows"][0]
        assert row["found"] and row["total_bytes"] == 222, row                # H2: 새 스캔이 최신
        kept = [x["db_filename"] for x in json.load(open(os.path.join(rep, "meta.json")))["scans"]]
        assert kept == ["new.db"], kept                                        # 보관 1개 = 새 스캔
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[H1·H2] OK  엣지 시계가 뒤로 가도 복제 계속·'최신'은 포탈이 받은 시각 기준")


def check_ai_edge_round10() -> None:
    """I1 여러 경로 예약이 첫 경로만 '예약됨' I2 건너뛴 예약 경로의 흔적 없음 I3 임시 비밀번호 안내가 키보드로 못 씀
    I4 영어 모드 날짜가 한국어 형식."""
    from isilon_usage import settings as S
    from isilon_usage.server import ScanController as _SC
    sc = {"paths": ["/a", "/b"]}
    assert _SC._mark_chain_skip(sc, "/b", True) is True and "/b" in sc["chain_skipped"]           # I2
    assert _SC._mark_chain_skip(sc, "/b", True) is False                                        # 처음 시각 유지
    assert _SC._mark_chain_skip(sc, "/b", False) is True and sc["chain_skipped"] == {}
    out = S._sanitize_schedules([{"paths": ["/a", "/b"], "unit": "minute", "every": 5,
                                  "chain_skipped": {"/b": 123.0, "/zz": 1.0}}])
    assert out[0]["chain_skipped"] == {"/b": 123.0}, out[0]                                       # 저장에도 남음
    dh, ph = _read("dashboard.html"), _read("portal.html")
    assert "ps.forEach(p => { if (p && !(p in schByPath)) schByPath[p] = s; });" in dh             # I1
    assert "⚠ 건너뜀(" in dh and "개 건너뜀</span>" in dh
    assert 'ov.setAttribute("role", "dialog")' in dh and 'ov.setAttribute("role","dialog")' in ph   # I3
    for h in (dh, ph):                                                                            # I4
        assert h.count('"ko-KR"') == 2 and "const _LOC = " in h
    print("[I1~I4] OK  여러 경로 예약 표시·건너뜀 표시·안내 모달 키보드·영어 모드 날짜")


# ------------------------------------------------------------------ 11회차(악조건 파일시스템)
def check_aj_adverse_fs_round11() -> None:
    """J1 du 백엔드: UTF-8 아닌 폴더 이름 하나로 스캔 전체 error J2 pscan: 오류 수만 있고 오류 목록이 빔·같은 폴더 이중 계산
    J3 native: lstat 실패 파일이 오류 없이 빠짐 J4 pscan: 심볼릭 링크 루트 크기가 스레드 엔진과 다름."""
    from isilon_usage import db as dbmod, pscan, scanner
    from isilon_usage.scanner import run_scan
    d = tempfile.mkdtemp(prefix="iu_aj_")
    try:
        root = os.path.join(d, "t")
        os.makedirs(root)
        bad = os.fsencode(root) + b"/b\xff"
        os.makedirs(bad)
        with open(bad + b"/f", "wb") as fh:
            fh.write(b"x" * 5000)
        if shutil.which("du"):                                                        # J1
            dbp = os.path.join(d, "du.db")
            run_scan(dbp, root, backend="du", size_mode="disk", workers=1)
            c = dbmod.connect(dbp)
            st = c.execute("SELECT status, scanned_bytes FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
            c.close()
            assert st["status"] == "done" and st["scanned_bytes"] >= 5000, dict(st)
        # J2: 한 폴더의 stat·scandir 이 둘 다 실패해도 오류 1개, 오류 목록에 1단계 폴더 이름
        sub = os.path.join(root, "u")
        os.makedirs(os.path.join(sub, "deny"))
        deny = os.path.join(sub, "deny")
        o_stat, o_scandir = pscan.os.stat, pscan.os.scandir

        def f_stat(p, *a, **k):
            if os.fsdecode(p) == deny:
                raise PermissionError(13, "denied", p)
            return o_stat(p, *a, **k)

        def f_scandir(p="."):
            if os.fsdecode(p) == deny:
                raise PermissionError(13, "denied", p)
            return o_scandir(p)
        pscan.os.stat, pscan.os.scandir = f_stat, f_scandir
        try:
            r = pscan.scan_subtree(sub, "disk")
        finally:
            pscan.os.stat, pscan.os.scandir = o_stat, o_scandir
        assert r[4] == 1, r                                                            # 이중 계산 없음
        res = {"per_top": [{"path": sub, "bytes": 10, "files": 1, "dirs": 2, "failed_units": 0, "errors": 1}],
               "total_bytes": 10, "total_files": 1, "total_dirs": 3, "error_count": 1}
        dbp = os.path.join(d, "p.db")
        pscan.write_run_db(dbp, root, res)
        c = dbmod.connect(dbp)
        errs = [dict(x) for x in c.execute("SELECT path, error FROM directories WHERE error IS NOT NULL")]
        c.close()
        assert len(errs) == 1 and errs[0]["path"] == sub and "읽기 오류 1개" in errs[0]["error"], errs
        # J4: 링크 루트
        real = os.path.join(d, "real")
        os.makedirs(os.path.join(real, "a"))
        with open(os.path.join(real, "a", "f"), "wb") as fh:
            fh.write(b"y" * 3000)
        link = os.path.join(d, "rootlink")
        os.symlink(real, link)
        assert pscan.scan_subtree(link, "apparent")[1] == pscan.scan_subtree(real, "apparent")[1]
        assert pscan._scan_shallow(link, "apparent")[1] == pscan._scan_shallow(real, "apparent")[1]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    src = _read("scanner.py")                                                          # J3
    assert "self._stat_tls.err = getattr(self._stat_tls, \"err\", 0) + 1" in src and "acc.errors += n_stat_err" in src
    print("[J1~J4] OK  du 비 UTF-8 이름·pscan 오류 목록/이중 계산·stat 실패 오류 집계·링크 루트 크기")


def check_ak_round11_misc() -> None:
    """K1 멈춘 직후 '멈춘 자료 집계'·'재개'가 '실행 중'으로 거절되는 경합 K2 일괄 푸시 모달이 지난 결과에서 멈춤
    K3 전부 이미 최신인 푸시가 '실패'로 기록 K4 주기만 보낸 설정이 감시 폴더를 비움."""
    from isilon_usage import server as SV, portal as P
    d = tempfile.mkdtemp(prefix="iu_ak_")
    try:
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0, enable_scan=True)
        c = httpd.RequestHandlerClass.controller
        stop = threading.Event()
        stop.set()
        th = threading.Thread(target=lambda: time.sleep(0.4))
        th.start()
        with c._lock:
            c._scans[777] = {"stop": stop, "thread": th, "path": "/x"}
        t0 = time.time()

        def _cleanup():
            th.join()
            with c._lock:
                c._scans.pop(777, None)
        threading.Thread(target=_cleanup).start()
        c._wait_stopping(777)
        assert time.time() - t0 >= 0.3 and not th.is_alive()                         # K1: 끝날 때까지 기다림
        httpd.server_close()
        pc = P.PortalController(os.path.join(d, "hq"))                                 # K4
        pc.set_upgrade_watch("/w1", 60)
        pc.set_upgrade_watch(None, 120)
        assert pc.settings["upgrade_watch_dir"] == "/w1" and pc.settings["upgrade_check_secs"] == 120
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # L1(12회차): 이 기능 전 복제본이 다시 와도 '지금'으로 찍혀 더 새 스캔을 앞지르지 않음
    old = [{"db_filename": "X.db", "root_path": "/d", "finished_at": 100, "status": "done"},
           {"db_filename": "Y.db", "root_path": "/d", "finished_at": 200, "status": "done"}]
    merged = P.PortalController._merge_scan_lists(old, [dict(old[0])])
    order = [x["db_filename"] for x in sorted(merged, key=P.PortalController._scan_recency, reverse=True)]
    assert order == ["Y.db", "X.db"], order
    sv = _read("server.py")                                                             # L2: 잠금 밖에서 대기
    assert sv.index("self._wait_stopping(scan_id)   # 멈추는 중이면") < sv.index("return self._resume_scan_locked(scan_id)")
    src, ph = _read("portal.py"), _read("portal.html")
    assert "ok=(len(nodes) > 0 and okn + curn == len(nodes))" in src                   # K3
    assert "모든 노드가 이미 v%s 입니다" in src
    i = ph.index("function openPushModal(){")
    assert "pollPush();" not in ph[i:ph.index("}", ph.index("_pushTimer=null;}", i)) + 200].split("// 시작 요청이")[0]
    assert '(j.total||0)+"대";\n      pollPush(); }' in ph                                   # K2
    print("[K1~K4] OK  멈추는 중 대기·푸시 모달은 시작 뒤 조회·이미 최신은 성공·감시 폴더 유지")


# ------------------------------------------------------------------ 12회차(엣지 도구 화면)
def check_al_tools_round12() -> None:
    """M1 테스트 데이터 생성이 허용 경로 밖·시스템 경로 아래(/etc/x)에도 만들 수 있었음 M2 두 권장 결과가 함께 남아 서로 다른 값."""
    from isilon_usage import gentest as G, server as SV
    for p in ("/etc/x", "/proc/1/x", "/boot/x"):                                     # (14회차: /usr·/run·/dev 아래는 허용)
        assert G.validate(p, 1, 1, 1, 1)[0] is False, p                               # M1(시스템 트리 아래)
    d = tempfile.mkdtemp(prefix="iu_al_")
    try:
        m = os.path.join(d, "m")
        os.makedirs(m)
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [m]}, enable_scan=True)
        c = httpd.RequestHandlerClass.controller
        kw = dict(n_dirs=1, n_subdirs=1, n_files=1, file_size=1)
        assert c.gentest_validate(path=os.path.join(m, "g"), **kw)["ok"] is True
        for bad in (os.path.join(d, "outside"), os.path.join(m, "..", "escape")):
            r = c.gentest_validate(path=bad, **kw)
            assert r["ok"] is False and "허용된 마운트 경로 밖" in (r["error"] or ""), (bad, r)
            assert c.gentest_start(path=bad, **kw)["ok"] is False
        httpd.server_close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")                                                       # M2
    assert 'if ($("setTuneResult")) $("setTuneResult").innerHTML = "";' in dh
    assert 'if ($("benchResult") && !_benchTimer) $("benchResult").innerHTML = "";' in dh
    print("[M1·M2] OK  테스트 데이터는 허용 경로 안·시스템 트리 밖에만·권장 결과는 하나만")


# ------------------------------------------------------------------ 13회차(포탈→엣지 원격 조작)
def check_am_remote_round13() -> None:
    """N1 비밀번호 푸시가 전부 실패해도 '완료' N2 포탈에서 시작한 직후 중지가 '진행 중 스캔 없음'
    N3 엣지 거절 사유가 'HTTP 400' 으로 바뀜 N4 403(엣지 토큰 미설정)을 '토큰 불일치'로 안내 N5 감사 기록에 대상 없음."""
    from isilon_usage import server as SV, portal as P, scanner as scanmod
    from isilon_usage import audit as auditmod
    d = tempfile.mkdtemp(prefix="iu_am_")
    cwd = os.getcwd()
    os.chdir(d)
    orig = scanmod.Scanner._safe_stat
    try:
        m = os.path.join(d, "m")
        tree = os.path.join(m, "t")
        for i in range(40):
            os.makedirs(os.path.join(tree, "d%02d" % i))
            for j in range(5):
                with open(os.path.join(tree, "d%02d" % i, "f%d" % j), "wb") as fh:
                    fh.write(b"x" * 100)
        edge = SV.serve(os.path.join(d, "edata"), host="127.0.0.1", port=0,
                        initial_settings={"mount_bases": [m], "api_token": "TOK"}, enable_scan=True)
        threading.Thread(target=edge.serve_forever, daemon=True).start()
        ec = edge.RequestHandlerClass.controller
        r = ec.start_scan(tree)
        for _ in range(100):
            if r["scan_id"] not in ec.running_ids():
                break
            time.sleep(0.1)
        edge2 = SV.serve(os.path.join(d, "edata2"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [m]}, enable_scan=True)          # api_token 없음
        threading.Thread(target=edge2.serve_forever, daemon=True).start()
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "e1", "url": "http://127.0.0.1:%d" % edge.server_address[1], "token": "TOK"})
        pc.upsert_node({"id": "e2", "url": "http://127.0.0.1:%d" % edge2.server_address[1], "token": "X"})
        r = pc.set_node_password(["e2"], "abcd1234")                                   # N1·N4
        assert r["ok"] is False and r.get("failed") == 1 and "api_token" in r["reason"], r
        assert "불일치" not in r["results"][0]["reason"], r["results"]
        pc._poll_one("e1")
        scanmod.Scanner._safe_stat = lambda self, e: (time.sleep(0.02), orig(self, e))[1]  # 스캔을 느리게
        r = pc.node_scan("e1")
        assert r["ok"], r
        r2 = pc.node_scan("e1")                                                        # N3: 엣지 사유 그대로
        assert r2["ok"] is False and "HTTP 400" not in (r2.get("reason") or ""), r2
        st = pc.node_stop("e1")                                                        # N2: 캐시가 옛것이어도 중지
        assert st["stopped"] >= 1, st
        scanmod.Scanner._safe_stat = orig
        time.sleep(0.5)
        ev = auditmod.tail(os.path.join(d, "edata"), 50)
        assert any("path=" in (e.get("detail") or "") for e in ev if "scan/start" in (e.get("action") or "")), ev[-5:]
        edge.shutdown()
        edge2.shutdown()
    finally:
        scanmod.Scanner._safe_stat = orig
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    ph = _read("portal.html")
    assert "alert(id+\" — 스캔을 시작하지 못했습니다" in ph and "모두 ${clear?\"초기화\":\"설정\"}하지 못했습니다" in ph
    print("[N1~N5] OK  비밀번호 푸시 실패 표시·시작 직후 중지·엣지 사유 전달·403 안내·감사 대상")


def check_an_fuzz_round13() -> None:
    """O1 짝 없는 서로게이트(\\ud800)가 감사 로그·저장에서 터져 500·연결 끊김·노드 목록 저장 불능
    O2 SQLite 범위 밖 scan_id 500 O3 포탈 ids 가 목록이 아니면 500."""
    from isilon_usage import server as SV, portal as P, audit as auditmod
    d = tempfile.mkdtemp(prefix="iu_an_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        m = os.path.join(d, "m")
        os.makedirs(m)
        edge = SV.serve(os.path.join(d, "e"), host="127.0.0.1", port=0,
                        initial_settings={"mount_bases": [m]}, enable_scan=True)
        threading.Thread(target=edge.serve_forever, daemon=True).start()
        portal = P.serve_portal(os.path.join(d, "hq"), host="127.0.0.1", port=0)
        threading.Thread(target=portal.serve_forever, daemon=True).start()
        pcn = portal.RequestHandlerClass.controller
        pcn.set_password("test1234", False)
        ptok = pcn._auth.issue_token()

        def post(port, path, raw, tok=""):
            rq = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=raw.encode(), method="POST",
                                        headers={"Content-Type": "application/json", "X-Op-Token": tok})
            try:
                return urllib.request.urlopen(rq, timeout=10).status
            except urllib.error.HTTPError as e:
                return e.code
        ep, pp = edge.server_address[1], portal.server_address[1]
        assert post(ep, "/api/scan/start", '{"path": "\\ud800"}') == 400                       # O1
        assert post(pp, "/api/portal/sync", '{"id": "\\ud800"}') == 400
        assert post(pp, "/api/portal/nodes", '{"id": "\\ud800", "url": "http://127.0.0.1:1"}', ptok) == 400
        assert post(pp, "/api/portal/nodes", '{"id": "ok2", "url": "http://127.0.0.1:1"}', ptok) == 200
        st = post(ep, "/api/scan/start", '{"path": "%s/b\\udcff"}' % m)                         # 비 UTF-8 이름 값은 통과
        assert st in (200, 400), st
        assert post(ep, "/api/scan/partial-aggregate", '{"scan_id": 1000000000000000000000000000000}') == 400   # O2
        assert P.safe_node_id("\ud800") is False and P.safe_node_id("a\x01") is False and P.safe_node_id("한글") is True
        assert pcn.set_node_password(5, "abcd1234")["ok"] is False                                   # O3
        assert pcn.set_node_token(True, "x")["ok"] is False
        auditmod.record(d, action="x", ok=True, detail="/a/b\udcff")                               # 감사 기록 예외 없음
        assert auditmod.tail(d, 5)[0]["detail"] == "/a/b\udcff"
        edge.shutdown()
        portal.shutdown()
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[O1~O3] OK  짝 없는 서로게이트 400·비 UTF-8 이름은 통과·scan_id 범위·ids 형식")


# ------------------------------------------------------------------ 14회차(12~13회차 수정분 회귀)
def check_ao_regress_round14() -> None:
    """P1 테스트 데이터: /run/media·/dev/shm·/usr/local 까지 막던 과잉 차단 P2 저장된 노드 id 에 제어 문자가 있으면
    불러올 때 조용히 빠지고 다음 저장에서 사라짐."""
    from isilon_usage import gentest as G, portal as P
    for p in ("/run/media/u/disk/x", "/dev/shm/t", "/usr/local/data/x"):
        ok, why, _plan = G.validate(p, 1, 1, 1, 1)
        assert "안전하지 않은" not in (why or ""), (p, why)                               # P1
    for p in ("/etc/x", "/proc/1/x", "/usr", "/run", "/dev"):
        assert G.validate(p, 1, 1, 1, 1)[0] is False, p
    d = tempfile.mkdtemp(prefix="iu_ao_")
    try:
        hq = os.path.join(d, "hq")
        os.makedirs(hq)
        with open(P.nodes_path(hq), "w") as fh:
            json.dump({"nodes": [{"id": "a\tb", "url": "http://10.0.0.1:1"}, {"id": "ok", "url": "http://10.0.0.2:1"}]}, fh)
        pc = P.PortalController(hq)
        assert sorted(n["id"] for n in pc.nodes) == ["a\tb", "ok"], pc.nodes                # P2
        pc.upsert_node({"id": "new", "url": "http://10.0.0.3:1"})
        assert sorted(n["id"] for n in P.load_nodes(hq)) == ["a\tb", "new", "ok"]
        assert pc.upsert_node({"id": "x\ty", "url": "http://10.0.0.4:1"})["ok"] is False   # 새 등록은 여전히 거부
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[P1·P2] OK  테스트 데이터 경로 과잉 차단 해소·저장된 노드는 id 규칙 완화로 보존")


def check_ap_explore_round14() -> None:
    """Q1 다른 탭에서 먼저 저장한 설정을 오래된 탭이 폼 전체로 조용히 되돌림 Q2 방금 등록한 노드가 1초쯤 '끊김'·오류로 보임."""
    dh, ph = _read("dashboard.html"), _read("portal.html")
    assert "function _collectSettingsPayload() {" in dh and "_setBase = JSON.parse(JSON.stringify(_collectSettingsPayload()));" in dh   # Q1(16회차: 깊은 복사)
    assert "JSON.stringify(_full[k]) !== JSON.stringify(_setBase[k])" in dh
    assert dh.index("let _setBase = null;") < dh.index("async function loadSettings()")               # 선언이 먼저
    assert 'if(!n.online && !n.last_poll && !n.last_error) return {t:"확인 중"' in ph                    # Q2
    assert "if(n.enabled && !st.pending && (!n.online || n.error)){" in ph
    print("[Q1·Q2] OK  설정은 바뀐 칸만 저장(브라우저 두 탭 확인)·새 노드는 '확인 중'")


# ------------------------------------------------------------------ 사용자 요청: 스캔을 멈추지 않고 지금까지 결과 보기
def check_aq_live_snapshot() -> None:
    """R1 진행 중 스캔의 폴더별 용량이 0 → 스냅샷(일관 복사+부분 집계)으로 하한값을 본다. 스캔은 계속, 끝나면 스냅샷 삭제."""
    from isilon_usage import server as SV, scanner as scanmod
    d = tempfile.mkdtemp(prefix="iu_aq_")
    orig = scanmod.Scanner._safe_stat
    try:
        m = os.path.join(d, "m")
        for a in range(4):
            for b in range(6):
                p = os.path.join(m, "t", "a%d" % a, "b%d" % b)
                os.makedirs(p)
                for f in range(6):
                    with open(os.path.join(p, "f%d" % f), "wb") as fh:
                        fh.write(b"x" * 1000)
        scanmod.Scanner._safe_stat = lambda self, e: (time.sleep(0.03), orig(self, e))[1]
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [m]}, enable_scan=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        c = httpd.RequestHandlerClass.controller

        def g(path):
            return json.loads(urllib.request.urlopen(base + path).read().decode())
        sid = c.start_scan(os.path.join(m, "t"))["scan_id"]
        for _ in range(100):
            if any(r["total_bytes"] == 0 for r in g("/api/topdirs?scan=%d&limit=5" % sid)["rows"]):
                break
            time.sleep(0.05)
        assert c.live_snapshot_start(sid)["ok"]
        for _ in range(200):
            if not c.live_snapshot_info(sid)["building"]:
                break
            time.sleep(0.05)
        info = c.live_snapshot_info(sid)
        assert info["at"] and not info["error"], info
        row = [x for x in g("/api/scans")["scans"] if x["id"] == sid][0]
        assert row["status"] in ("discovering", "sizing") and row["live_snap"]["at"], row       # 스캔은 계속
        tops = g("/api/topdirs?scan=%d&limit=5" % sid)["rows"]
        assert tops and max(r["total_bytes"] for r in tops) > 0, tops                          # 하한값이 보임
        assert c.live_snapshot_start(10 ** 6)["ok"] is False
        scanmod.Scanner._safe_stat = orig
        for _ in range(400):
            if sid not in c.running_ids():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        assert not os.path.exists(c.live_snapshot_path(sid))                                   # 끝나면 지움
        assert c.live_snapshot_start(sid)["ok"] is False                                        # 끝난 스캔은 거부
        httpd.shutdown()
    finally:
        scanmod.Scanner._safe_stat = orig
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "button[data-livesnap]" in dh and "function liveSnapHtml(ls, sid)" in dh
    print("[R1] OK  진행 중 스캔 '지금까지 결과' — 멈추지 않고 하한값·끝나면 스냅샷 정리")


# ------------------------------------------------------------------ 15회차(강제 종료)
def check_ar_crash_round15() -> None:
    """S1 빠른 스캔 중 서버를 kill -9 하면 남은 워커가 듣기 소켓을 쥐어 재기동 실패 S2 저장 중 강제 종료로 .tmp 가 쌓임."""
    from isilon_usage import settings as setmod
    d = tempfile.mkdtemp(prefix="iu_ar_")
    try:
        for a in range(6):
            for b in range(10):
                p = os.path.join(d, "m", "t", "a%d" % a, "b%d" % b)
                os.makedirs(p)
                with open(os.path.join(p, "f"), "wb") as fh:
                    fh.write(b"x")
        srv = os.path.join(d, "srv.py")
        with open(srv, "w") as fh:
            fh.write("""import os, sys, time
sys.path.insert(0, %r)
from isilon_usage import pscan, server as SV
_o = os.scandir
pscan.os.scandir = lambda p=".": (time.sleep(0.2), _o(p))[1]
d = sys.argv[1]
h = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0, initial_settings={"mount_bases": [os.path.join(d, "m")]}, enable_scan=True)
h.RequestHandlerClass.controller.start_scan(os.path.join(d, "m", "t"), engine="pscan")
open(os.path.join(d, "ready"), "w").write(str(os.getpid()))
h.serve_forever()
""" % os.path.dirname(HERE))
        proc = subprocess.Popen([sys.executable, srv, d], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(os.path.join(d, "ready")):
                break
            time.sleep(0.1)
        time.sleep(1.5)
        kids = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
        assert kids, "pscan 워커가 떠 있어야 함"
        proc.kill()
        proc.wait()
        time.sleep(1.0)
        def _live(pid):   # 좀비(Z, 이미 죽었고 거둬 가기만 남음)는 살아 있는 것이 아니다
            try:
                with open("/proc/%s/stat" % pid) as fh:
                    return fh.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
            except OSError:
                return False
        alive = [k for k in kids if _live(k)]
        for k in alive:
            try:
                os.kill(int(k), 9)
            except OSError:
                pass
        assert not alive, "부모가 죽은 뒤에도 남은 워커: %s" % alive                       # S1
        stale = os.path.join(d, "settings.json.abc123.tmp")                                    # S2
        with open(stale, "w") as fh:
            fh.write("{")
        os.utime(stale, (time.time() - 3600, time.time() - 3600))
        fresh = os.path.join(d, "portal_nodes.json.zzz.tmp")
        with open(fresh, "w") as fh:
            fh.write("{")
        assert setmod.clean_stale_tmp(d) == 1 and not os.path.exists(stale) and os.path.exists(fresh)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[S1·S2] OK  강제 종료 시 pscan 워커도 함께 종료·오래된 임시 파일 정리")


# ------------------------------------------------------------------ 16회차(13~15회차 수정분 회귀)
def check_as_regress_round16() -> None:
    """T1 '바뀐 칸만 저장'의 기준값이 예약·장비 배열을 참조로 들고 있어 추가·삭제가 저장되지 않음
    T2 서버가 PID 1(컨테이너)일 때 pscan 워커가 전부 스스로 종료해 스캔이 빔."""
    dh = _read("dashboard.html")
    assert "_setBase = JSON.parse(JSON.stringify(_collectSettingsPayload()));" in dh             # T1(브라우저로도 확인)
    script = ("let schedules=[{a:1}]; const base=JSON.parse(JSON.stringify({schedules}));"
              "schedules.push({b:2}); console.log(JSON.stringify(schedules)!==JSON.stringify(base.schedules));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == "true", out
    assert "or ppid == 1" not in _read("pscan.py")                                               # T2
    if shutil.which("unshare"):
        d = tempfile.mkdtemp(prefix="iu_as_")
        try:
            for a in range(5):
                os.makedirs(os.path.join(d, "t", "a%d" % a, "s"))
                with open(os.path.join(d, "t", "a%d" % a, "s", "f"), "wb") as fh:
                    fh.write(b"x")
            code = ("import sys; sys.path.insert(0, %r); from isilon_usage import pscan;"
                    "r = pscan.parallel_scan(%r, processes=2); print(r.get('complete'), r['total_files'])"
                    % (os.path.dirname(HERE), os.path.join(d, "t")))
            r = subprocess.run(["unshare", "--pid", "--fork", "--mount-proc", sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=60)
            if r.returncode == 0:                                 # 권한이 없으면(일반 사용자) 건너뜀
                assert r.stdout.strip().splitlines()[-1] == "True 5", r.stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)
    print("[T1·T2] OK  예약·장비 추가/삭제 저장·PID 1 에서도 pscan 정상")


def check_at_livesnap_round16() -> None:
    """U1 스캔이 끝난 뒤에도 폴더 트리가 진행 중(또는 스냅샷 하한값) 숫자로 남음 U2 만드는 사이 스캔이 멈추면 옛 스냅샷이
    놓여 재개 뒤 '지금 결과'처럼 보임."""
    from isilon_usage import livesnap, db as dbmod
    d = tempfile.mkdtemp(prefix="iu_at_")
    try:
        src = os.path.join(d, "src.db")
        _mk_replica_db(src, "/x", [("/x", 0, 1, 0), ("/x/a", 1, 0, 0)])
        c = dbmod.connect(src)
        c.execute("UPDATE scan_runs SET status='discovering'")
        c.commit()
        c.close()
        dst = os.path.join(d, "snap.db")
        r = livesnap.build(src, dst, still_running=lambda: False)                         # U2
        assert r["ok"] is False and not os.path.exists(dst), r
        r = livesnap.build(src, dst, still_running=lambda: True)
        assert r["ok"] and os.path.exists(dst), r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    sv = _read("server.py")
    assert "self._live_snaps.get(int(scan_id)) is job" in sv and "and self._scan_is_live(scan_id)))" in sv
    assert "livesnap.remove(self.live_snapshot_path(scan_id))\n            with self._lock:\n                self._live_snaps.pop(int(scan_id), None)" in sv
    assert "window._selRunPrev.running && !_runNow) refreshTreeInPlace();" in _read("dashboard.html")   # U1(17회차: 위치 유지)
    print("[U1·U2] OK  완료 후 트리 갱신·멈춘 스캔엔 스냅샷을 놓지 않음·재개 때 옛 스냅샷 삭제")


# ------------------------------------------------------------------ 17회차(15~16회차·새 기능 회귀)
def check_au_regress_round17() -> None:
    """V1 다른 프로세스(CLI)가 돌리는 스캔의 스냅샷이 매번 끝까지 만든 뒤 버려짐 V2 완료·새 스냅샷 때 트리 위치가 맨 위로
    V3 기동 정리가 다른 서버의 진행 중 스냅샷까지 지움 V4 재개 전 시작된 스냅샷 작업이 재개 뒤 놓임."""
    from isilon_usage import server as SV, manager as M, db as dbmod
    d = tempfile.mkdtemp(prefix="iu_au_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        ctl = SV.ScanController(data)          # 서버가 먼저 떠 있고(기동 정리는 이때), 그 뒤 다른 프로세스가 스캔
        sid = M.register_scan(data, root_path="/x", db_path=os.path.join(d, "x.db"), backend="native", size_mode="disk")
        c = dbmod.connect(M.manager_db_path(data))
        M.update_scan(c, sid, status="sizing", phase="sizing", updated_at=time.time())   # 다른 프로세스가 진행 중(하트비트 살아 있음)
        c.commit()
        sid2 = M.register_scan(data, root_path="/y", db_path=os.path.join(d, "y.db"), backend="native", size_mode="disk")
        c.execute("UPDATE scans SET status='sizing', phase='sizing', updated_at=? WHERE id=?",
                  (time.time() - 99999, sid2))                                     # 소식 끊김(하트비트 오래됨)
        c.commit()
        c.close()
        assert ctl._scan_is_live(sid) is True and ctl._scan_is_live(sid2) is False                # V1
        r = ctl.live_snapshot_start(sid2)
        assert r["ok"] is False and "진행 중이 아닙니다" in r["reason"], r
        snaps = os.path.join(data, "snapshots")                                                   # V3
        os.makedirs(snaps, exist_ok=True)
        for i in (sid, sid2):
            with open(os.path.join(snaps, "scan_%d_live.db" % i), "w") as fh:
                fh.write("x")
        ctl._clean_snapshots()
        assert sorted(os.listdir(snaps)) == ["scan_%d_live.db" % sid], os.listdir(snaps)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")                                                                  # V2
    assert "function refreshTreeInPlace()" in dh and "lastDetailScan = null;" not in dh.replace("let lastDetailScan = null;", "")
    assert "self._live_snaps.get(int(scan_id)) is job" in _read("server.py")                     # V4
    print("[V1~V4] OK  다른 프로세스 스캔 스냅샷·위치 유지 갱신·진행 중 스냅샷 보존·재개 뒤 옛 작업 무시")


def check_av_round17_flow() -> None:
    """W1 1단계 남은 시간이 폴더 수만 봐서 끝 무렵 수십 초에 멈춤 → 파일 기준 추정도 함께(긴 쪽)
    W2 멈춘 스캔 분석 화면에 '아직 완성된 값이 아님' 안내 W3 도는 스캔이 없으면 진행 패널은 최근 스캔
    W4 재개한 1단계의 total_dirs 가 옛 값에 멈춤 W5 배너를 매 폴링 다시 그려 버튼 클릭이 사라짐."""
    from isilon_usage import server as SV, manager as M, db as dbmod
    # W1: 이전 완료 스캔 313폴더/30,000파일, 지금 290폴더(정체)·14,000파일·240초
    eta = M.estimate_eta_seconds("discovering", 0, 0, 290, 240.0, 313, prev_total_files=30000, files_done=14000)
    assert eta is not None and eta > 200, eta                     # 폴더만이면 약 19초
    assert M.estimate_eta_seconds("discovering", 0, 0, 290, 240.0, 313) < 30   # 파일 정보가 없으면 예전과 같음
    assert M.estimate_eta_seconds("discovering", 0, 0, 0, 240.0, 313, prev_total_files=30000, files_done=14000) > 200
    assert M.estimate_eta_seconds("discovering", 0, 0, 290, 240.0, 313, prev_total_files=30000, files_done=50) < 30   # 파일 50개로는 추정 안 함
    d = tempfile.mkdtemp(prefix="iu_av_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        c = dbmod.connect(M.manager_db_path(data))
        s1 = M.register_scan(data, root_path="/big", db_path=os.path.join(d, "a.db"), backend="native", size_mode="disk")
        c.execute("UPDATE scans SET status='done', phase='done', total_dirs=313, total_files=30000 WHERE id=?", (s1,))
        c.commit()
        s2 = M.register_scan(data, root_path="/big", db_path=os.path.join(d, "b.db"), backend="native", size_mode="disk")
        now = time.time()
        c.execute("UPDATE scans SET status='discovering', phase='discovering', discovered_dirs=290, total_dirs=290, "
                  "total_files=14000, started_at=?, updated_at=?, active_elapsed=240 WHERE id=?", (now - 240, now, s2))
        c.commit()
        assert M.prev_totals(c, "/big", s2) == (313, 30000)
        ov = M.overall_capacity(c)
        rt = [x for x in ov["roots"] if x.get("scan_id") == s2 or x.get("root_path") == "/big"][0]
        assert rt.get("eta_seconds") and rt["eta_seconds"] > 200, rt          # 홈·포탈(같은 계산)
        c.close()
        # W4·W1: /api/status 의 build_status — 재개한 1단계(per-run total_dirs 가 멈출 때의 200)
        pdb = os.path.join(d, "b.db")
        dbmod.init_db(pdb)
        pc = dbmod.connect(pdb)
        rid = dbmod.create_run(pc, "/big", backend="native", size_mode="disk", scanner_pid=0)
        pc.execute("UPDATE scan_runs SET status='discovering', phase='discovering', total_dirs=200, discovered_dirs=290, "
                   "total_files=14000, started_at=?, elapsed_accum=240, updated_at=? WHERE id=?", (now - 240, now, rid))
        pc.commit()
        st = SV.build_status(pc, None, top=0, prev_total_dirs=313, prev_total_files=30000)
        pc.close()
        assert st["run"]["total_dirs"] == 290, st["run"]["total_dirs"]                               # W4
        assert st["progress"]["eta_seconds"] and st["progress"]["eta_seconds"] > 200, st["progress"]   # W1
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "data-anpartial" in dh and "스캔 — 아직 완성된 값이 아닙니다" in dh                        # W2
    assert "else if (!userPicked && allScans && allScans.length) statusScan = allScans[0].id;" in dh  # W3
    assert "_an._lastHtml !== html" in dh and "_an.innerHTML = window._anRunningHtml" not in dh       # W5
    print("[W1~W5] OK  1단계 남은 시간 파일 기준·멈춘 스캔 안내·진행 패널 최근 스캔·1단계 전체 수·배너 클릭 유지")


def check_aw_round18_regress() -> None:
    """X1 재개로 버려진 스냅샷 작업과 새 작업이 같은 임시 파일을 써 새 작업이 실패 X2 거절된 재개가 진행 중 스냅샷을 지움
    X3 멈춘 자료 집계가 끝나도 트리가 집계 전 값 X4 'DB 파일' 라벨이 고른 스캔이 아니라 진행 패널 스캔."""
    from isilon_usage import server as SV, manager as M, db as dbmod, livesnap as LS
    d = tempfile.mkdtemp(prefix="iu_aw_")
    try:
        seen = []
        orig = LS.copy_consistent
        LS.copy_consistent = lambda src, dst: (seen.append(dst), open(dst, "w").close(), 0)[2]
        try:
            dst = os.path.join(d, "snap.db")
            LS.build("/nonexist", dst)
            LS.build("/nonexist", dst)
        finally:
            LS.copy_consistent = orig
        assert len(seen) == 2 and seen[0] != seen[1], seen                                         # X1
        assert not [n for n in os.listdir(d) if ".building" in n], os.listdir(d)
        open(dst + ".building.9.9", "w").close()
        LS.remove(dst)
        assert not os.path.exists(dst + ".building.9.9")
        data = os.path.join(d, "data")                                                             # X2
        M.init_manager(data)
        ctl = SV.ScanController(data)
        sid = M.register_scan(data, root_path="/x", db_path=os.path.join(d, "x.db"), backend="native", size_mode="disk")
        c = dbmod.connect(M.manager_db_path(data))
        c.execute("UPDATE scans SET status='sizing', phase='sizing', updated_at=? WHERE id=?", (time.time(), sid))
        c.commit()
        c.close()
        snap = ctl.live_snapshot_path(sid)
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        open(snap, "w").close()
        r = ctl.resume_scan(sid)
        assert r["ok"] is False and os.path.exists(snap), (r, os.path.exists(snap))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "window._paSeen = _paKey;" in dh                                                        # X3
    assert '$("scanDbInfo").textContent = "DB 파일: " + (data.scan_meta.db_filename' not in dh     # X4
    print("[X1~X4] OK  스냅샷 임시 파일 작업별·거절된 재개는 스냅샷 보존·부분 집계 뒤 트리 갱신·DB 파일 라벨")


def check_ax_round18_data() -> None:
    """Y1 du 실행의 부분 집계/스냅샷이 하드링크를 두 번 세어 '하한값'이 실제보다 큼 Y2 하위·검색·오류 목록이
    200/1000 개에서 말없이 잘림 Y3 --json 표준출력에 머리말·진행 표시가 섞임 Y4 죽은 스캔에 CLI partial 거절."""
    import contextlib
    import io
    import json as _json
    import subprocess as _sp
    from isilon_usage import server as SV, manager as M, db as dbmod, cli as C
    from isilon_usage.scanner import partial_aggregate
    d = tempfile.mkdtemp(prefix="iu_ax_")
    try:
        H = 1000000                                    # 하드링크 파일: 탐색은 루트 own_bytes 에서 한 번만 셈
        pdb = os.path.join(d, "du.db")
        dbmod.init_db(pdb)
        c = dbmod.connect(pdb)
        rid = dbmod.create_run(c, "/r", backend="du", size_mode="disk", scanner_pid=0)
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, own_bytes, total_bytes, file_count, total_files) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (rid, "/r", "r", 0, "discovered", 4096 + H, 0, 1, 0))
        root_id = c.execute("SELECT id FROM directories WHERE path='/r'").fetchone()[0]
        c.execute("INSERT INTO directories (run_id, parent_id, path, name, depth, status, own_bytes, total_bytes, file_count, total_files) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, root_id, "/r/x", "x", 1, "done", 4096, 4096 + H, 0, 1))  # du: 링크 포함
        c.execute("UPDATE scan_runs SET status='paused' WHERE id=?", (rid,))
        c.commit()
        c.close()
        assert partial_aggregate(pdb)["ok"]
        c = dbmod.connect(pdb)
        tb, tf = c.execute("SELECT total_bytes, total_files FROM directories WHERE path='/r'").fetchone()
        c.close()
        assert tb <= 8192 + H and tb == 8192 + H, tb                                               # Y1 (예전 8192+2H)
        assert tf == 2, tf
        pdb2 = os.path.join(d, "many.db")                                                          # Y2
        dbmod.init_db(pdb2)
        c = dbmod.connect(pdb2)
        rid2 = dbmod.create_run(c, "/m", backend="native", size_mode="disk", scanner_pid=0)
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, own_bytes, total_bytes) VALUES (?,?,?,?,?,?,?)",
                  (rid2, "/m", "m", 0, "done", 0, 300 * 100))
        pid_ = c.execute("SELECT id FROM directories WHERE path='/m'").fetchone()[0]
        for i in range(300):
            c.execute("INSERT INTO directories (run_id, parent_id, path, name, depth, status, own_bytes, total_bytes, error) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (rid2, pid_, "/m/c%03d" % i, "c%03d" % i, 1, "done", 100, 100,
                                                      "EACCES" if i < 250 else None))
        c.commit()
        r = SV.list_children(c, rid2, pid_)
        assert len(r["children"]) == 200 and r["truncated"] and r["total_count"] == 300 and r["rest_bytes"] == 100 * 100, r.keys()
        assert "truncated" not in SV.list_children(c, rid2, None)
        sr = SV.search_dirs(c, rid2, "c")
        assert len(sr["results"]) == 200 and sr["truncated"] is True
        er = SV.list_errors(c, rid2, limit=100)
        assert len(er["errors"]) == 100 and er["truncated"] and er["total_count"] == 250, er.get("total_count")
        c.close()
        dh = _read("dashboard.html")
        assert "meta.truncated" in dh and "j.truncated ?" in dh
        tree = os.path.join(d, "t", "a", "b")                                                       # Y3
        os.makedirs(tree)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            assert C.main(["analyze", os.path.join(d, "t"), "--json"]) == 0
        assert _json.loads(buf.getvalue())["total_dirs"] == 3, buf.getvalue()[:200]
        data = os.path.join(d, "data")                                                             # Y4
        M.init_manager(data)
        dead = _sp.Popen(["true"])
        dead.wait()
        pdb3 = os.path.join(d, "k.db")
        dbmod.init_db(pdb3)
        c = dbmod.connect(pdb3)
        rid3 = dbmod.create_run(c, "/k", backend="native", size_mode="disk", scanner_pid=dead.pid)
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, own_bytes) VALUES (?,?,?,?,?,?)",
                  (rid3, "/k", "k", 0, "discovered", 4096))
        c.execute("UPDATE scan_runs SET status='discovering', phase='discovering' WHERE id=?", (rid3,))
        c.commit()
        c.close()
        sid = M.register_scan(data, root_path="/k", db_path=pdb3, backend="native", size_mode="disk")
        mc = dbmod.connect(M.manager_db_path(data))
        mc.execute("UPDATE scans SET status='discovering', phase='discovering' WHERE id=?", (sid,))
        mc.commit()
        mc.close()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = C.main(["partial", str(sid), "--data-dir", data])
        assert rc == 0, rc
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[Y1~Y4] OK  du 부분 집계 하한값·잘린 목록 표시·--json 표준출력·죽은 스캔 CLI 부분 집계")


def check_ay_round18_portal_delete() -> None:
    """Z1 엣지에서 지운 스캔이 포탈 복제본에 남아 비교 화면에서 계속 '최신'으로 보임."""
    import json as _json
    from isilon_usage import server as SV, portal as P
    d = tempfile.mkdtemp(prefix="iu_ay_")
    cwd = os.getcwd()
    os.chdir(d)
    edge = None
    try:
        m = os.path.join(d, "m")
        tree = os.path.join(m, "t")
        os.makedirs(os.path.join(tree, "a"))
        with open(os.path.join(tree, "a", "f"), "wb") as fh:
            fh.write(b"x" * 1000)
        edge = SV.serve(os.path.join(d, "edata"), host="127.0.0.1", port=0,
                        initial_settings={"mount_bases": [m], "api_token": "TOK"}, enable_scan=True)
        threading.Thread(target=edge.serve_forever, daemon=True).start()
        ec = edge.RequestHandlerClass.controller
        ids = []
        for _ in range(2):
            r = ec.start_scan(tree)
            ids.append(r["scan_id"])
            for _ in range(200):
                if r["scan_id"] not in ec.running_ids():
                    break
                time.sleep(0.05)
            time.sleep(0.05)
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.upsert_node({"id": "e1", "url": "http://127.0.0.1:%d" % edge.server_address[1], "token": "TOK"})
        pc._sync_one("e1", True)
        rep = os.path.join(d, "hq", "replicas", "e1")
        with open(os.path.join(rep, "meta.json"), encoding="utf-8") as fh:
            before = [s["db_filename"] for s in _json.load(fh)["scans"]]
        assert len(before) == 2, before
        assert ec.delete_scan(ids[1])["ok"]
        assert len(ec.deleted_db_filenames()) == 1
        pc._sync_one("e1", True)
        with open(os.path.join(rep, "meta.json"), encoding="utf-8") as fh:
            after = [s["db_filename"] for s in _json.load(fh)["scans"]]
        gone = [x for x in before if x not in after]
        assert len(after) == 1 and len(gone) == 1, (before, after)
        assert not os.path.exists(os.path.join(rep, "scans", gone[0])), os.listdir(os.path.join(rep, "scans"))
    finally:
        if edge is not None:
            edge.shutdown()
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[Z1] OK  엣지에서 지운 스캔은 포탈 복제본에서도 지움")


def check_az_round19_regress() -> None:
    """AA1 du+최대 깊이 스캔의 부분 집계가 부모를 자식보다 작게 AA2 같은 DB 이름으로 다시 등록해도 삭제 표시가 남아 포탈이
    새 스캔까지 지움 AA3 다른 호스트의 살아 있는 스캔을 CLI partial 이 '일시정지'로 AA4 하위 목록 정렬·나머지 합계."""
    import contextlib
    import io
    from isilon_usage import server as SV, manager as M, db as dbmod, cli as C
    from isilon_usage.scanner import partial_aggregate
    d = tempfile.mkdtemp(prefix="iu_az_")
    try:
        pdb = os.path.join(d, "du.db")                                                             # AA1
        dbmod.init_db(pdb)
        c = dbmod.connect(pdb)
        rid = dbmod.create_run(c, "/r", backend="du", size_mode="disk", scanner_pid=0)
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, own_bytes) VALUES (?,?,?,?,?,?)",
                  (rid, "/r", "r", 0, "discovered", 4096))
        rt = c.execute("SELECT id FROM directories WHERE path='/r'").fetchone()[0]
        c.execute("INSERT INTO directories (run_id, parent_id, path, name, depth, status, own_bytes, total_bytes, subdir_count) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (rid, rt, "/r/x", "x", 1, "done", 4096, 6000000, 3))  # 아래는 du 만 잼
        c.execute("UPDATE scan_runs SET status='paused' WHERE id=?", (rid,))
        c.commit()
        c.close()
        assert partial_aggregate(pdb)["ok"]
        c = dbmod.connect(pdb)
        tb = c.execute("SELECT total_bytes FROM directories WHERE path='/r'").fetchone()[0]
        c.close()
        assert tb == 4096 + 6000000, tb
        data = os.path.join(d, "data")                                                             # AA2
        M.init_manager(data)
        M.record_deleted(data, "nightly.db")
        assert M.deleted_db_filenames(data) == ["nightly.db"]
        M.register_scan(data, root_path="/n", db_path=os.path.join(d, "nightly.db"), backend="native", size_mode="disk")
        assert M.deleted_db_filenames(data) == [], M.deleted_db_filenames(data)
        pdb3 = os.path.join(d, "remote.db")                                                        # AA3
        dbmod.init_db(pdb3)
        c = dbmod.connect(pdb3)
        rid3 = dbmod.create_run(c, "/k", backend="native", size_mode="disk", scanner_pid=4000000)
        c.execute("UPDATE scan_runs SET status='sizing', phase='sizing', hostname='other-host' WHERE id=?", (rid3,))
        c.commit()
        c.close()
        sid = M.register_scan(data, root_path="/k", db_path=pdb3, backend="native", size_mode="disk")
        mc = dbmod.connect(M.manager_db_path(data))
        mc.execute("UPDATE scans SET status='sizing', phase='sizing' WHERE id=?", (sid,))
        mc.commit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = C.main(["partial", str(sid), "--data-dir", data])
        st = mc.execute("SELECT status FROM scans WHERE id=?", (sid,)).fetchone()[0]
        mc.close()
        assert rc == 2 and st == "sizing", (rc, st)
        pdb4 = os.path.join(d, "many.db")                                                          # AA4
        dbmod.init_db(pdb4)
        c = dbmod.connect(pdb4)
        rid4 = dbmod.create_run(c, "/m", backend="native", size_mode="disk", scanner_pid=0)
        c.execute("INSERT INTO directories (run_id, path, name, depth, status, own_bytes, total_bytes, subdir_count) "
                  "VALUES (?,?,?,?,?,?,?,?)", (rid4, "/m", "m", 0, "done", 0, 300 * 100 + 99999, 301))
        pm = c.execute("SELECT id FROM directories WHERE path='/m'").fetchone()[0]
        for i in range(300):
            c.execute("INSERT INTO directories (run_id, parent_id, path, name, depth, status, own_bytes, total_bytes) "
                      "VALUES (?,?,?,?,?,?,?,?)", (rid4, pm, "/m/c%03d" % i, "c%03d" % i, 1, "done", 100, 100))
        c.execute("INSERT INTO directories (run_id, parent_id, path, name, depth, status, own_bytes, total_bytes) "
                  "VALUES (?,?,?,?,?,?,?,?)", (rid4, pm, "/m/big", "big", 1, "pending", 99999, 0))
        c.commit()
        r = SV.list_children(c, rid4, pm)
        c.close()
        assert r["children"][0]["name"] == "big", r["children"][0]
        assert r["total_count"] == 301 and r["rest_bytes"] == 101 * 100, (r["total_count"], r["rest_bytes"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[AA1~AA4] OK  du 최대 깊이 부분 집계·재등록 시 삭제 표시 해제·다른 호스트 스캔 보호·하위 목록 정렬")


def check_ba_round19_portal_edge() -> None:
    """BB1 새 노드가 같은 이름의 노드를 말없이 덮어씀·URL/포트/길이 검증 없음 BB2 닿지 않는 노드 중지를 '성공'으로·순차 처리
    BB3 낡은 캐시로 끝난 노드 스캔을 '진행 중'으로 거부 BB4 설정 값 조용한 변환·거짓 성공
    CC1~CC5 엣지: 진행 카드 경과에 일시정지 포함·예약이 기본값 무시·예약 끄기 불가·'사용' 오역·알림 두 번."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_ba_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        assert pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "token": "t1", "region": "A"})["ok"]
        r = pc.upsert_node({"id": "e1", "url": "http://10.0.0.9:8765", "region": "X", "create_only": True})   # BB1
        assert r["ok"] is False and "이미 있는" in r["reason"], r
        assert pc._get("e1")["url"] == "http://10.0.0.1:8765" and pc._get("e1")["region"] == "A"
        assert pc.upsert_node({"id": "e1", "url": "http://10.0.0.1:8765", "region": "B"})["ok"]           # 수정은 됨
        for bad in ("javascript:alert(1)", "ftp://x", "http://", "not a url", "http://127.0.0.1:99999"):
            r = pc.upsert_node({"id": "z", "url": bad})
            assert r["ok"] is False and pc._get("z") is None, (bad, r)
        assert pc.upsert_node({"id": "z" * 65, "url": "10.0.0.2"})["ok"] is False
        assert pc.upsert_node({"id": "z", "url": "10.0.0.2", "region": "r" * 81})["ok"] is False
        assert pc.upsert_node({"id": "z", "url": "10.0.0.2:8765"})["ok"]
        for port in (99999, "abc", -1):
            r = pc.provision_plan({"host": "h", "port": port})
            assert r["ok"] is False and "port" in r["reason"], (port, r)
        r = pc.provision_plan({"host": "10.9.9.6$(id)", "port": 8765})
        assert r["ok"] is False and "host" in r["reason"], r
        pc.upsert_node({"id": "down", "url": "http://127.0.0.1:1", "token": "t"})                          # BB2
        r = pc.node_stop("down")
        assert r["ok"] is False and "연결할 수 없어" in r["reason"], r
        t0 = time.time()
        ra = pc.node_stop_all()
        assert ra["failed"] >= 1 and any(x["id"] == "down" and not x["ok"] for x in ra["results"]), ra
        assert time.time() - t0 < 30
        with pc._lock:                                                                                    # BB3
            pc._cache["e1"] = {"overall": {"roots": [{"scan_id": 1, "root_path": "/x", "status": "sizing"}]}}

        def _fresh(nid):
            with pc._lock:
                pc._cache[nid] = {"overall": {"roots": [{"scan_id": 1, "root_path": "/x", "status": "done"}]}}
            return True
        pc._poll_one = _fresh
        r = pc.node_scan("e1")
        assert "이미 스캔 진행 중" not in (r.get("reason") or ""), r
        assert pc.set_replica_keep(True)["ok"] is False and pc.set_replica_keep(2.7)["ok"] is False        # BB4
        assert pc.set_replica_keep("3")["ok"] is True
        assert pc.set_nav_hidden("x")["ok"] is False and pc.set_nav_hidden(["dash"])["ok"] is True
        assert pc.set_upgrade_watch(None, "q")["ok"] is False
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ph = _read("portal.html")
    assert "pl.create_only=true" in ph and 'j401.reason==="locked"' in ph and "개 노드 확인 실패" in ph
    dh = _read("dashboard.html")
    assert "const el = scanActiveSec(r, true);" in dh                                                    # CC1
    assert "size_mode: (_setBase && _setBase.default_size_mode)" in dh                                  # CC2
    assert "data-schen" in dh                                                                           # CC3
    assert '>사용함<' in dh and '"사용함": "Enabled"' in _read("i18n_en.js")                             # CC4
    assert "auth_cancelled: _alerted" in dh and "!j.ok && !j.auth_cancelled" in dh                       # CC5
    print("[BB1~BB4·CC1~CC5] OK  포탈 노드 입력 검증·중지 실패 보고·낡은 캐시 재확인·설정 검증 / 엣지 화면 5건")


def check_bb_round20_regress() -> None:
    """DD1 저장된 긴 이름·지역 노드의 수정·재등록·CSV 가 영영 막힘 DD2 대문자 HTTP:// 가 'http://HTTP://' 로 저장
    DD3 맨 IPv6 원격 구성이 등록 없이 '성공' DD4 설정 여러 칸 저장 때 실패가 성공에 덮임 DD5 CLI resume 원격 검사 없음
    DD6 로그인 '취소'에 아무 안내 없음."""
    import contextlib
    import io
    import json as _json
    from isilon_usage import portal as P, manager as M, db as dbmod, cli as C
    d = tempfile.mkdtemp(prefix="iu_bb_")
    try:
        hq = os.path.join(d, "hq")
        os.makedirs(hq)
        with open(P.nodes_path(hq), "w", encoding="utf-8") as fh:
            _json.dump([{"id": "L" * 100, "url": "http://10.0.0.1:8765", "region": "r" * 200}], fh)
        pc = P.PortalController(hq)
        assert pc._get("L" * 100) is not None
        r = pc.upsert_node({"id": "L" * 100, "url": "http://10.0.0.1:8765", "region": "r" * 200, "token": "t"})   # DD1
        assert r["ok"], r
        assert pc.upsert_node({"id": "L" * 100, "url": "http://10.0.0.1:8765", "region": "x" * 81})["ok"] is False
        assert pc.upsert_node({"id": "L" * 100, "url": "http://10.0.0.1:8765", "region": "short"})["ok"]
        r = pc.upsert_node({"id": "a", "url": "HTTP://Foo/"})                                                    # DD2
        assert r["ok"] and pc._get("a")["url"] == "http://Foo", pc._get("a")
        r = pc.provision_plan({"host": "fe80::1", "path": "/mnt/x"})                                             # DD3
        assert r["ok"] and r["registered"] and r["edge_url"] == "http://[fe80::1]:8765" and r["node"]["id"], r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    src = _read("portal.py")
    assert "def _mg(r):" in src and src.count("res.update(c.set_") == 0                                          # DD4
    d = tempfile.mkdtemp(prefix="iu_bb2_")                                                                       # DD5
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        pdb = os.path.join(d, "remote.db")
        dbmod.init_db(pdb)
        c = dbmod.connect(pdb)
        rid = dbmod.create_run(c, "/k", backend="native", size_mode="disk", scanner_pid=4000000)
        c.execute("UPDATE scan_runs SET status='sizing', phase='sizing', hostname='other-host' WHERE id=?", (rid,))
        c.commit()
        c.close()
        sid = M.register_scan(data, root_path="/k", db_path=pdb, backend="native", size_mode="disk")
        mc = dbmod.connect(M.manager_db_path(data))
        mc.execute("UPDATE scans SET status='paused', phase='sizing' WHERE id=?", (sid,))
        mc.commit()
        mc.close()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = C.main(["resume", str(sid), "--data-dir", data])
        assert rc == 2 and "다른 호스트" in err.getvalue(), (rc, err.getvalue())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "window._unlockAlerted = true;" in dh and "로그인을 취소해 작업하지 않았습니다." in dh                # DD6
    print("[DD1~DD6] OK  저장된 긴 노드 수정·대문자 scheme·IPv6 원격 구성·설정 실패 보존·CLI resume 원격 검사·로그인 취소 안내")


def check_bc_round20_upgrade() -> None:
    """EE1 깨진 묶음이 매 점검마다 실제 코드 자리에 잠깐 들어갔다 빠지며 .bad 폴더 누적 EE2 쓰다 실패하면 반쯤 쓴 .new 폴더
    남음 EE3 인터넷 자동 설치 실패를 매 주기 재시도 EE4 설치 스크립트가 멈춘 뒤 복사하다 실패하면 서비스가 꺼진 채
    EE5 오류로 멈췄다 재개해 끝난 스캔의 옛 오류 문구."""
    from isilon_usage import upgrade as U
    d = tempfile.mkdtemp(prefix="iu_bc_")
    try:
        pkg_src = HERE
        code = os.path.join(d, "code")
        shutil.copytree(pkg_src, os.path.join(code, "isilon_usage"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        members = {}
        for dp, dn, fn in os.walk(pkg_src):
            dn[:] = [x for x in dn if x != "__pycache__"]
            for f in fn:
                full = os.path.join(dp, f)
                with open(full, "rb") as fh:
                    members[os.path.relpath(full, pkg_src)] = fh.read()
        live = os.path.join(code, "isilon_usage", "server.py")
        before = open(live, "rb").read()
        bad = dict(members, **{"server.py": b"def broken(:\n"})
        try:
            U.apply_package(bad, code)
            raise AssertionError("깨진 묶음이 설치됨")
        except OSError as exc:
            assert "설치하지 않음" in str(exc), exc
        assert open(live, "rb").read() == before                                                   # EE1
        left = [n for n in os.listdir(code) if n not in ("isilon_usage", ".isilon_usage.install.lock")]
        assert not left, left
        os.makedirs(os.path.join(code, "isilon_usage.bad.123"))                                    # 옛 버전이 남긴 것
        os.makedirs(os.path.join(code, "isilon_usage.new.124"))
        conflict = dict(members, **{"zz": b"x", "zz/inner": b"y"})                                 # EE2 쓰기 실패
        try:
            U.apply_package(conflict, code)
            raise AssertionError("쓰기 실패가 성공으로")
        except OSError:
            pass
        left = [n for n in os.listdir(code) if n not in ("isilon_usage", ".isilon_usage.install.lock")]
        assert not left, left
        assert open(live, "rb").read() == before
    finally:
        shutil.rmtree(d, ignore_errors=True)
    st = {}                                                                                        # EE3
    k = U.net_fail_key({"latest": "1.99.49", "sha256": "ab"})
    assert not U.net_fail_should_skip(st, k, 1000.0)
    w1 = U.net_fail_record(st, k, 1000.0)
    assert U.net_fail_should_skip(st, k, 1000.0 + w1 - 1) and not U.net_fail_should_skip(st, k, 1000.0 + w1 + 1)
    w2 = U.net_fail_record(st, k, 2000.0)
    assert w2 == 2 * w1 and not U.net_fail_should_skip(st, U.net_fail_key({"latest": "1.99.50"}), 2000.0)
    assert "net_fail_should_skip(_nb, _nk)" in _read("server.py") and "net_fail_should_skip(_nb, _nk)" in _read("portal.py")
    for f in ("install_edge.sh", "install_portal.sh"):                                             # EE4
        sh = open(os.path.join(os.path.dirname(HERE), "tools", f), encoding="utf-8").read()
        assert sh.index('cp -a "$TMP_DIR"/. "$NEW_DIR"/') < sh.index('systemctl stop "$SERVICE"'), f
        assert sh.index('echo "→ 임시 작업: $TMP_DIR"') > sh.index('mkdir -p "$TMP_DIR"; fi'), f
    assert 'fields["error"] = None' in _read("scanner.py")                                        # EE5
    print("[EE1~EE5] OK  깨진 묶음은 단계 폴더에서 거부·쓰기 실패 정리·자동 설치 실패 간격·설치 스크립트 단계 복사·옛 오류 문구")


def check_bd_round20_stress() -> None:
    """FF1 느린 NAS 에서 '중지' 뒤 청크(2000개)를 다 stat 할 때까지 멈추지 않음 FF2 아무것도 안 보내는 연결이 스레드·fd 를
    끝없이 잡음 FF3 테이블 준비 전 조회가 500 'no such table' FF4 로그가 늘 scan#1."""
    import json as _json
    import urllib.request
    from isilon_usage import server as SV, manager as M, scanner as scanmod, portal as P
    d = tempfile.mkdtemp(prefix="iu_bd_")
    orig = scanmod.Scanner._safe_stat
    try:
        flat = os.path.join(d, "flat")
        os.makedirs(flat)
        for i in range(3000):
            open(os.path.join(flat, "f%04d" % i), "w").close()
        scanmod.Scanner._safe_stat = lambda self, e: (time.sleep(0.005), orig(self, e))[1]
        ev = threading.Event()
        t = threading.Thread(target=lambda: scanmod.run_scan(os.path.join(d, "s.db"), flat, workers=1,
                                                              stop_event=ev, with_monitor=False), daemon=True)
        t.start()
        time.sleep(1.0)
        t0 = time.time()
        ev.set()
        t.join(30)
        took = time.time() - t0
        assert not t.is_alive() and took < 3.0, took                       # FF1 (예전: 최대 2000×5ms=10초)
    finally:
        scanmod.Scanner._safe_stat = orig
    assert SV.DashboardHandler.timeout and P.PortalHandler.timeout                                   # FF2
    httpd = None
    try:                                                                                             # FF3
        data = os.path.join(d, "data")
        M.init_manager(data)
        empty = os.path.join(d, "empty.db")
        open(empty, "w").close()
        sid = M.register_scan(data, root_path="/e", db_path=empty, backend="native", size_mode="disk")
        httpd = SV.serve(data, host="127.0.0.1", port=0, initial_settings={"mount_bases": [d]}, enable_scan=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        for ep in ("/api/children", "/api/export", "/api/search?q=a&", "/api/errors", "/api/stats"):
            url = base + ep + ("&" if "?" in ep else "?") + "scan=%d" % sid
            url = url.replace("&&", "&")
            r = urllib.request.urlopen(url)
            assert r.status == 200, (ep, r.status)
            j = _json.loads(r.read().decode() or "{}")
            assert j.get("reason") == "initializing" or j.get("ok") is not False, (ep, j)
    finally:
        if httpd is not None:
            httpd.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    assert 'getattr(self, "manager_scan_id", None) or self.run_id' in _read("scanner.py")          # FF4
    print("[FF1~FF4] OK  느린 NAS 중지 즉시·연결 시간 제한·준비 전 조회 '초기화 중'·로그 스캔 번호")


def check_be_round21_sec_regress() -> None:
    """GG1 CSRF: 'text/plain; x=application/json'·Content-Type 없음 통과, 다른 사이트 Origin GG2 로그인 없이 지정 경로 밖 스캔
    GG3 벤치마크 표시 미이스케이프·로그 토큰·감사 로그 0644 GG4 동시 설치가 서로의 단계 폴더를 지움 GG5 큰 응답 한 번 쓰기
    GG6 끝난 스캔의 'no such table' 을 '초기화 중'으로 숨김 GG7 설치 경로 심볼릭 링크·지워진 작업 폴더."""
    import io
    import json as _json
    import urllib.request
    import urllib.error
    from isilon_usage import server as SV, manager as M, upgrade as U, audit as A
    assert SV.is_json_ctype("application/json; charset=utf-8") and not SV.is_json_ctype("text/plain; x=application/json")
    assert not SV.is_json_ctype("") and not SV.is_json_ctype(None)
    d = tempfile.mkdtemp(prefix="iu_be_")
    cwd = os.getcwd()
    os.chdir(d)
    httpd = None
    try:
        m = os.path.join(d, "m")
        out = os.path.join(d, "outside")
        os.makedirs(m)
        os.makedirs(out)
        httpd = SV.serve(os.path.join(d, "data"), host="127.0.0.1", port=0,
                         initial_settings={"mount_bases": [m]}, enable_scan=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def post(path, body, headers):
            rq = urllib.request.Request(base + path, data=body, method="POST", headers=headers)
            try:
                r = urllib.request.urlopen(rq)
                return r.status, _json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                return e.code, _json.loads(e.read().decode() or "{}")
        b = _json.dumps({"scan_id": 999}).encode()
        assert post("/api/scan/stop", b, {"Content-Type": "text/plain; x=application/json"})[0] == 415      # GG1
        assert post("/api/scan/stop", b, {})[0] == 415
        st, _ = post("/api/scan/stop", b, {"Content-Type": "application/json", "Origin": "http://evil.example"})
        assert st == 403, st
        st, _ = post("/api/scan/stop", b"", {"Origin": "null"})
        assert st == 403, st
        st, _ = post("/api/scan/stop", b, {"Content-Type": "application/json",
                                            "Origin": base})
        assert st != 403, st
        ctl = httpd.RequestHandlerClass.controller                                                          # GG2
        ctl.setup_op_password("test1234")
        st, j = post("/api/scan/start", _json.dumps({"path": out, "confirm_outside": True}).encode(),
                     {"Content-Type": "application/json"})
        assert st == 401 and j.get("op_required"), (st, j)
        tok = ctl.unlock("test1234")["token"]
        st, j = post("/api/scan/start", _json.dumps({"path": out, "confirm_outside": True}).encode(),
                     {"Content-Type": "application/json", "X-Op-Token": tok})
        assert st == 200 and j.get("ok"), (st, j)
        for _ in range(100):
            if not ctl.running_ids():
                break
            time.sleep(0.05)
        data = os.path.join(d, "data")                                                                      # GG6
        mc = __import__("isilon_usage.db", fromlist=["x"]).connect(M.manager_db_path(data))
        mc.execute("UPDATE scans SET status='done' WHERE id=?", (j["scan_id"],))
        mc.commit()
        dbp = mc.execute("SELECT db_path FROM scans WHERE id=?", (j["scan_id"],)).fetchone()[0]
        mc.close()
        c = __import__("isilon_usage.db", fromlist=["x"]).connect(dbp)
        c.execute("DROP TABLE scan_stats")
        c.commit()
        c.close()
        os.utime(dbp, (time.time() - 3600, time.time() - 3600))   # 오래된(끝난) DB
        try:
            urllib.request.urlopen(base + "/api/stats?scan=%d" % j["scan_id"])
            raise AssertionError("손상된 끝난 스캔이 정상 응답")
        except urllib.error.HTTPError as e:
            assert e.code == 500 and "no such table" in e.read().decode(), e
    finally:
        if httpd is not None:
            httpd.shutdown()
        os.chdir(cwd)
    dh = _read("dashboard.html")                                                                            # GG3
    assert "${escHtml(j.sample_path" in dh and "${escHtml(j.note)}" in dh and "${escHtml(j.error)}" in dh
    import re as _re
    sv = _read("server.py")
    assert "(?:token|api_token|key|password)=" in sv
    A.record(d, action="x", ok=True)
    ap = [os.path.join(dp, f) for dp, dn, fn in os.walk(d) for f in fn if f.startswith("audit")][0]
    assert (os.stat(ap).st_mode & 0o777) == 0o600, oct(os.stat(ap).st_mode)
    code = os.path.join(d, "code")                                                                          # GG4
    pkg_src = HERE
    shutil.copytree(pkg_src, os.path.join(code, "isilon_usage"), ignore=shutil.ignore_patterns("__pycache__"))
    members = {}
    for dp, dn, fn in os.walk(pkg_src):
        dn[:] = [x for x in dn if x != "__pycache__"]
        for f in fn:
            with open(os.path.join(dp, f), "rb") as fh:
                members[os.path.relpath(os.path.join(dp, f), pkg_src)] = fh.read()
    orig = U._smoke_test
    U._smoke_test = lambda cd: (time.sleep(1.5), orig(cd))[1]
    res = []
    try:
        ts = [threading.Thread(target=lambda: res.append(_try(U.apply_package, members, code))) for _ in range(2)]
        ts[0].start()
        time.sleep(0.5)
        ts[1].start()
        for t in ts:
            t.join(60)
    finally:
        U._smoke_test = orig
    assert len(res) == 2 and all(r == "ok" for r in res), res
    shutil.rmtree(d, ignore_errors=True)
    buf = io.BytesIO()                                                                                     # GG5
    calls = []

    class _W:
        def write(self, b):
            calls.append(len(b))
            buf.write(b)
            return len(b)
    w = SV._ChunkedWriter(_W())
    w.write(b"x" * (3 * (1 << 20) + 5))
    assert calls == [1 << 20, 1 << 20, 1 << 20, 5] and buf.getbuffer().nbytes == 3 * (1 << 20) + 5
    for f in ("install_edge.sh", "install_portal.sh"):                                                      # GG7
        sh = open(os.path.join(os.path.dirname(HERE), "tools", f), encoding="utf-8").read()
        assert 'INSTALL_DIR="$(_canon "$INSTALL_DIR")"' in sh and "\ncd /" in sh, f   # 28회차: 모든 단계 정규화
    assert "safe_cwd()" in sv and 'os.path.join(os.getcwd(), "CHANGELOG.md")' not in _read("portal.py")
    print("[GG1~GG7] OK  CSRF 미디어 타입·Origin·밖 스캔 로그인·이스케이프·로그 토큰·감사 0600·동시 설치·큰 응답 조각·끝난 스캔 오류·설치 경로")


def _try(fn, *a):
    try:
        fn(*a)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        return "err: %s" % exc


def check_bf_round21_modules() -> None:
    """HH1 스토리지 조회가 요청마다 겹쳐 쌓임·설정 바꿔도 옛 결과 HH2 이벤트 403·노드 다운에도 '정상'
    HH3 콜드 스크립트 심볼릭 루트·잘못된 기준 조용히 바꿈 HH4 리다이렉트로 VPLEX 비밀번호·큰 응답
    HH5 질의응답(1.5년·숫자 지어냄·라우팅) HH6 감사 로그 동시 회전 유실 HH7 마운트 공백·다른 경로 비교·짧은 추세·폴더 이력."""
    from isilon_usage import server as SV, isilon_api as IA, coldtier as CT, netutil as NU, ask as AK, audit as AU
    from isilon_usage import systune as ST
    d = tempfile.mkdtemp(prefix="iu_bf_")
    try:
        ctl = SV.ScanController(os.path.join(d, "data"))                                                 # HH1
        calls = []
        orig_q = SV.storagemod.query
        SV.storagemod.query = lambda arr: (calls.append(1), time.sleep(1.0), {"ok": True, "name": arr.get("name")})[2]
        try:
            ctl.settings["storage_arrays"] = [{"name": "A"}]
            outs = []
            ts = [threading.Thread(target=lambda: outs.append(ctl.storage_status())) for _ in range(3)]
            for t in ts:
                t.start()
                time.sleep(0.05)
            for t in ts:
                t.join(10)
            assert len(calls) == 1, calls
            ctl.settings["storage_arrays"] = []
            assert ctl.storage_status() == [], "설정 바꿔도 옛 결과"
        finally:
            SV.storagemod.query = orig_q
        import urllib.error

        class FakeIsi(IA.IsilonClient):                                                                  # HH2
            def _get(self, path):
                if "config" in path:
                    return {"name": "c", "onefs_version": {"release": "9"}}
                if "nodes" in path:
                    return {"nodes": [{"status": "up"}, {"status": "down"}], "total": 2}
                if "event" in path:
                    raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
                return {"stats": []}
        st = FakeIsi("http://x", "u", "p").status()
        assert st["health"] == "attention" and st.get("events_error"), st
        ev = {"eventgroup-occurrences": [{"severity": "critical", "time_noticed": "x", "message": "m"},
                                         {"severity": "critical", "time_noticed": 5, "message": "n"}]}
        total, by_sev, alarms = IA._summarize_events(ev)
        assert len(alarms) == 2, alarms
        real = os.path.join(d, "real")                                                                   # HH3
        os.makedirs(real)
        open(os.path.join(real, "f"), "w").close()
        os.symlink(real, os.path.join(d, "lnk"))
        r = CT.plan_cold_move(os.path.join(d, "lnk"), days=0)
        assert r["ok"] and r["root"] == os.path.realpath(real), r.get("root")
        assert CT.plan_cold_move(real, field="ctime")["ok"] is False
        assert CT.plan_cold_move(real, days="abc")["ok"] is False
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert {"username", "password"} <= NU._AUTH_HEADERS                                                   # HH4
    import io as _io
    try:
        NU.read_json(_io.BytesIO(b"x" * 20), limit=10)
        raise AssertionError("상한 없음")
    except ValueError as e:
        assert "너무 큽니다" in str(e)
    try:
        NU.read_json(_io.BytesIO(b"<html>login</html>"))
        raise AssertionError("HTML 통과")
    except ValueError as e:
        assert "JSON 이 아닌" in str(e)
    assert AK.parse_threshold_days("1.5년 이상") == 548 and AK.parse_threshold_days("2024년") is None   # HH5
    assert not AK._numbers_grounded("숫자 9999 TB 입니다", {"총": "200.46 KB"}, "200.46 KB")
    assert AK._numbers_grounded("총 200.46 KB 입니다", {"총": "200.46 KB"}, "200.46 KB")
    orig_lp = AK.llm_phrase
    seen = []
    AK.llm_phrase = lambda q, f, c: (seen.append(1), "숫자 9999 TB 입니다")[1]
    try:
        data = {"overall": {"roots": [{"root_path": "/a", "scanned_bytes": 1000, "status": "done"}],
                            "scan_count": 1, "active_scans": 0}}
        r = AK.respond("전체 용량 얼마야?", data, {"enabled": True, "endpoint": "http://x"})
        assert "9999" not in r["answer"], r
    finally:
        AK.llm_phrase = orig_lp
    assert AK._h_scan_status("파일 개수 알려줘", {"overall": {}}) is None
    assert AK._h_forecast("디스크 꽉 찼어?", {}) is None
    d = tempfile.mkdtemp(prefix="iu_bf2_")                                                               # HH6
    orig_mb = AU.MAX_BYTES
    AU.MAX_BYTES = 20000
    try:
        def w():
            for i in range(150):
                AU.record(d, action="a", ok=True, detail="x" * 50)
        ts = [threading.Thread(target=w) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        n = 0
        for f in os.listdir(d):
            with open(os.path.join(d, f), encoding="utf-8") as fh:
                n += sum(1 for _ in fh)
        files = sorted(os.listdir(d))
        assert files == sorted(["audit.log"] + ["audit.log.%d" % i for i in range(1, len(files))]), files
    finally:
        AU.MAX_BYTES = orig_mb
        shutil.rmtree(d, ignore_errors=True)
    assert ST._mount_unescape("/mnt/my\\040share") == "/mnt/my share"                                    # HH7
    sv = _read("server.py")
    assert '"root_mismatch": True' in sv and "max(xs) - min(xs) < 86400" in sv and '"absent": True' in sv
    print("[HH1~HH7] OK  스토리지 조회 한 번·설정 반영·상태 판정·콜드 심볼릭·리다이렉트 헤더·응답 상한·질의응답·감사 회전·마운트·비교·추세·이력")


def check_bg_round22_regress() -> None:
    """II1 Host 를 바꾸는 프록시 뒤에서 모든 브라우저 POST(로그인 포함) 403 II2 묶은(fold) 스캔에 있는 폴더를 이력에서 0 B 로
    II3 추세 부족 이유가 화면·질의응답에서 '3회뿐, 2회 이상 필요'로 모순 II4 숫자 검증이 다듬은 문장 거의 전부를 버림
    II5 설치 스크립트 상대 경로."""
    from isilon_usage import server as SV, ask as AK

    class H:
        def __init__(self, hd):
            self.headers = hd
    proxied = {"Origin": "http://nas.corp.example", "Host": "127.0.0.1:8765"}
    assert SV.cross_site_post(H(proxied)) is True
    assert SV.cross_site_post(H(dict(proxied, **{"X-Requested-With": "isilon-usage"}))) is False      # II1
    assert SV.cross_site_post(H(dict(proxied, **{"X-Forwarded-Host": "nas.corp.example, internal"}))) is False
    for f in ("dashboard.html", "portal.html"):
        assert '"X-Requested-With": "isilon-usage"' in _read(f), f
    sv = _read("server.py")
    assert '_fold and _fdepth > _fold' in sv                                                        # II2
    assert "fc.reason ||" in _read("dashboard.html")                                                  # II3
    r = AK._h_forecast("언제 가득 차?", {"scope": "edge", "detail": {"forecast": {"ok": True, "enough": False, "points": 3,
                                                                                 "reason": "하루 이상 걸쳐 있어야"}}})
    assert r is None or "3회뿐" not in r["answer"], r
    facts = {"pct": 45.2, "files": 1234567}                                                             # II4
    rule = "/a 2.00 KB (총 210.00 KB, 사용률 45.2%)"
    for ok_txt in ("약 210 KB", "45%", "약 123만 개", "1. a\n2. b", "상위 5개"):
        assert AK._numbers_grounded(ok_txt, facts, rule), ok_txt
    assert not AK._numbers_grounded("9999 TB", facts, rule) and not AK._numbers_grounded("300 GB", facts, rule)
    for f in ("install_edge.sh", "install_portal.sh"):                                                  # II5
        sh = open(os.path.join(os.path.dirname(HERE), "tools", f), encoding="utf-8").read()
        assert 'INSTALL_DIR="$(pwd)/$INSTALL_DIR"' in sh and 'DATA_DIR="$(pwd)/$DATA_DIR"' in sh, f
    print("[II1~II5] OK  프록시 뒤 POST·묶은 스캔 이력·추세 부족 이유·숫자 검증 완화·설치 상대 경로")


def check_bh_round22_fuzz() -> None:
    """JJ1 아주 큰 upgrade_check_secs 가 업그레이드 감시 스레드를 죽임 JJ2 설정의 1e999·401자리 수 500 JJ3 900단계 중첩 본문에
    응답 없이 끊김 JJ4 벤치마크 후보 형식 500 JJ5 어레이 비밀번호 비문자열 500 JJ6·JJ7 포탈 비문자열 칸 500·스레드 예외
    JJ8 scan_id true·2.9 가 1·2번 스캔을 지움."""
    import io
    import json as _json
    from isilon_usage import server as SV, settings as SE, tuning as TU, portal as P
    out = SE.sanitize(dict(SE.DEFAULTS, upgrade_check_secs=10 ** 13, sample_interval=10 ** 400, batch_size=True))
    assert out["upgrade_check_secs"] <= 7 * 86400 and out["batch_size"] == 500                      # JJ1·JJ2
    out = SE.sanitize(dict(SE.DEFAULTS, upgrade_check_secs=float("inf")))
    assert out["upgrade_check_secs"] == 60

    class H:
        def __init__(self, body):
            self.rfile = io.BytesIO(body)
            self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/json"}
    deep = ('{"a":' + "[" * 900 + "]" * 900 + "}").encode()
    _b, err = SV.read_json_body(H(deep))                                                             # JJ3
    assert err and err[0] == 400, err
    assert TU._normalize_candidates(True) and TU._normalize_candidates([1e999, 2]) == [2]              # JJ4
    d = tempfile.mkdtemp(prefix="iu_bh_")
    try:
        ctl = SV.ScanController(os.path.join(d, "data"))                                             # JJ5
        r = ctl.update_settings({"storage_arrays": [{"type": "isilon", "url": "http://x", "password": {"a": 1}}]})
        assert r["ok"] is False, r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert P._non_str_field({"id": [1]}, ("id",)) == "id"                                              # JJ6·JJ7
    assert "_non_str_field(body, (\"id\", \"dir\"" in _read("portal.py")
    for v, want in ((True, None), (2.9, None), ("3 ", None), (3, 3), ("3", 3), (None, None), ([1], None)):   # JJ8
        assert SV.parse_scan_id(v) == want, (v, SV.parse_scan_id(v))
    assert "parse_scan_id(body.get(\"scan_id\"))" in _read("server.py")
    print("[JJ1~JJ8] OK  업그레이드 주기 상한·설정 큰 수·깊은 중첩·벤치 후보·어레이 비번·포탈 비문자열·scan_id 엄격")


def check_bi_round22_browser() -> None:
    """KK1 NAS별 표 파일 수가 멈춘 스캔 값(다른 화면은 완료값) KK2 새 설치에 '지난 실행 설정 불러옴 — 엔진 ?'
    KK3 지난 완료 스캔이 있어도 1단계 초반에 '완료 스캔 기록이 없어' KK4 '반년'."""
    from isilon_usage import manager as M, db as dbmod, ask as AK
    dh = _read("dashboard.html")
    assert "fmtNum((r.capacity_files != null && !r.capacity_partial) ? r.capacity_files : r.total_files)" in dh   # KK1
    assert "Object.keys(s.last_scan).length" in dh                                                                # KK2
    assert "(disc && !r.eta_prev_available)" in dh                                                               # KK3
    d = tempfile.mkdtemp(prefix="iu_bi_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        c = dbmod.connect(M.manager_db_path(data))
        s1 = M.register_scan(data, root_path="/r", db_path=os.path.join(d, "a.db"), backend="native", size_mode="disk")
        c.execute("UPDATE scans SET status='done', phase='done', total_dirs=1000, total_files=5000 WHERE id=?", (s1,))
        c.commit()
        s2 = M.register_scan(data, root_path="/r", db_path=os.path.join(d, "b.db"), backend="native", size_mode="disk")
        c.execute("UPDATE scans SET status='discovering', phase='discovering', discovered_dirs=2, updated_at=? WHERE id=?",
                  (time.time(), s2))
        c.commit()
        rt = [x for x in M.overall_capacity(c)["roots"] if x.get("root_path") == "/r"][0]
        c.close()
        assert rt["eta_seconds"] is None and rt["eta_prev_available"] is True, rt
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert AK.parse_threshold_days("반년 이상 안 쓴 파일") == 182                                                   # KK4
    print("[KK1~KK4] OK  NAS별 파일 수 통일·새 설치 안내·지난 스캔 있을 때 '계산 중'·반년")


def check_bj_round23_regress() -> None:
    """LL1 사실의 날짜·연도를 쓴 LLM 문장 거부 LL2 작은 정수(3대 다운·21 TB)·0.5 TB 지어냄 통과 LL3 설치 스크립트가 현재 폴더 경로의
    공백을 걸러내지 못함(절대 경로 변환이 검사 뒤)."""
    from isilon_usage import ask as AK
    f = {"노드": {"전체": 13, "다운": 1}, "용량": "210.00 TB", "마지막": "2026-11-19 05:40"}
    assert AK._numbers_grounded("2026년 11월 19일 5시 40분에 마지막 스캔", f, "")                      # LL1
    assert AK._numbers_grounded("노드 13대 중 1대 다운, 용량 약 210 TB", f, "")
    for bad in ("노드 13대 중 3대가 다운", "용량 21 TB", "0.5 TB", "파일 99개"):                       # LL2
        assert not AK._numbers_grounded(bad, f, ""), bad
    assert AK._numbers_grounded("가장 큰 폴더 3개", {"순위": [1, 2, 3]}, "")
    for fn in ("install_edge.sh", "install_portal.sh"):                                               # LL3
        sh = open(os.path.join(os.path.dirname(HERE), "tools", fn), encoding="utf-8").read()
        assert sh.index('INSTALL_DIR="$(pwd)/$INSTALL_DIR"') < sh.index("공백/따옴표/역슬래시는 쓸 수 없습니다"), fn
    print("[LL1~LL3] OK  숫자 검증(날짜 허용·작은 정수 정확히)·설치 경로 검사 순서")


def check_bk_round23_portal() -> None:
    """MM1 쓰는 중인 핑/처리량 DB 를 바이트째 묶어 깨진 백업 MM2 재등록·원격 구성이 운영자의 논리 경로(alias_logical) 덮어씀
    MM3 별칭 '/' 무시 MM4 설치 폴더 권한 0700 MM5 지운 노드의 처리량 이력 남음 MM6 한글 토큰."""
    import io
    import sqlite3
    import tarfile as _tar
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_bk_")
    try:
        hq = os.path.join(d, "hq")
        pc = P.PortalController(hq)
        db = P.ping_history_path(hq)                                                                 # MM1
        c = sqlite3.connect(db)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE IF NOT EXISTS ping_samples (ts INTEGER, node_id TEXT, ok INTEGER, ms REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ping_ts2 ON ping_samples(ts)")
        c.executemany("INSERT INTO ping_samples VALUES (?,?,?,?)", ((i, "n%d" % (i % 13), 1, 1.0) for i in range(200000)))
        c.commit()
        c.close()
        stop = threading.Event()

        def writer():
            w = sqlite3.connect(db, timeout=30)
            i = 10 ** 6
            while not stop.is_set():
                w.executemany("INSERT INTO ping_samples VALUES (?,?,?,?)", ((i + k, "w", 1, 1.0) for k in range(50)))
                w.commit()
                i += 50
                time.sleep(0.02)
            w.close()
        t = threading.Thread(target=writer)
        t.start()
        bad = 0
        try:
            for _ in range(5):
                data, _fn = pc.make_backup_bytes()
                with _tar.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                    raw = tf.extractfile("portal_backup/ping_history.db").read()
                chk = os.path.join(d, "chk.db")
                with open(chk, "wb") as fh:
                    fh.write(raw)
                cc = sqlite3.connect(chk)
                if cc.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    bad += 1
                cc.close()
                os.remove(chk)
        finally:
            stop.set()
            t.join()
        assert bad == 0, bad
        assert not [n for n in os.listdir(hq) if n.startswith("portal_bk_")]
        pc.upsert_node({"id": "B", "url": "http://10.0.0.2:8765", "token": "tk", "alias_local": "/ifs/data",   # MM2
                        "alias_logical": "/data"})
        pc.enroll_node({"id": "B", "url": "http://10.0.0.2:8765", "path": "/ifs/data", "token": "tk"})
        assert pc._get("B")["alias_logical"] == "/data", pc._get("B")
        pc.upsert_node({"id": "R", "url": "http://10.0.0.3:8765", "alias_local": "/mnt/isilon", "alias_logical": "/"})  # MM3
        assert pc._get("R")["alias_logical"] == "/"
        assert P.PortalController._to_local(pc._get("R"), "/data") == "/mnt/isilon/data"
        sh = P._staged_install_sh("EDGE")                                                            # MM4
        assert '_TGM=$(stat -c %a' in sh and 'chmod "$_TGM"' in sh
        pc._record_throughput("B", {"total_scanned_bytes": 5, "roots": []})                          # MM5
        pc.delete_node("B")
        c = sqlite3.connect(P.throughput_history_path(hq))
        n = c.execute("SELECT COUNT(*) FROM throughput_samples WHERE node_id='B'").fetchone()[0]
        c.close()
        assert n == 0 and "B" not in pc._thru_last, n
        assert pc.upsert_node({"id": "K", "url": "http://10.0.0.4:8765", "token": "비밀토큰"})["ok"] is False   # MM6
        assert pc.set_node_token([], "한글")["ok"] is False and pc.set_export_token("한글")["ok"] is False
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[MM1~MM6] OK  백업 SQLite 일관 사본·재등록 논리 경로 유지·별칭 '/'·설치 폴더 권한·지운 노드 처리량·토큰 ASCII")


def check_bl_round23_engine() -> None:
    """NN1 pscan 이 루트 하위 폴더 수에 대해 O(N²) NN2 du 가 최대 깊이를 무시해 합계 부풀림 NN3 du 가 하드링크 중복 제거 끔을 무시
    NN4 fold<최대 깊이면 최대 깊이가 무시됨 NN5 재개해 끝나도 옛 정지 사유 NN6 pscan 링크 루트 크기 NN7 디스크 가득 시 상태."""
    import random
    from isilon_usage import scanner as S, db as dbmod, pscan as PS
    d = tempfile.mkdtemp(prefix="iu_bl_")
    try:
        random.seed(3)
        T = os.path.join(d, "tree")

        def mk(p, dep):
            os.makedirs(p, exist_ok=True)
            for i in range(3):
                with open(os.path.join(p, "f%d" % i), "wb") as fh:
                    fh.write(b"x" * random.randint(1, 9000))
            if dep < 5:
                for i in range(2):
                    mk(os.path.join(p, "d%d" % i), dep + 1)
        mk(T, 0)
        os.link(os.path.join(T, "f0"), os.path.join(T, "d0", "d1", "hl"))

        def root_tot(db):
            c = dbmod.connect(db)
            r = c.execute("SELECT total_bytes, total_files FROM directories WHERE depth=0").fetchone()
            c.close()
            return tuple(r)
        n = 0

        def scan(**kw):
            nonlocal n
            n += 1
            db = os.path.join(d, "s%d.db" % n)
            S.run_scan(db, T, with_monitor=False, **kw)
            return root_tot(db)
        base3 = scan(max_depth=3)
        if shutil.which("du"):
            assert scan(max_depth=3, backend="du") == base3                                           # NN2
            assert scan(hardlink_dedup=False, backend="du") == scan(hardlink_dedup=False)            # NN3
        assert scan(fold_depth=2, max_depth=3) == base3 and scan(fold_depth=1, max_depth=4) == scan(max_depth=4)   # NN4
        db = os.path.join(d, "g.db")                                                                  # NN5
        S.run_scan(db, T, db_max_bytes=1, with_monitor=False)
        c = dbmod.connect(db)
        st, err = c.execute("SELECT status, error FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        assert st == "paused" and err, (st, err)
        S.run_scan(db, T, resume=True, with_monitor=False)
        c = dbmod.connect(db)
        st, err = c.execute("SELECT status, error FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        assert st == "done" and not err, (st, err)
        lnk = os.path.join(d, "lnk")                                                                  # NN6
        os.symlink(T, lnk)
        r = PS.parallel_scan(lnk, processes=2, size_mode="apparent")
        r2 = PS.parallel_scan(T, processes=2, size_mode="apparent")
        assert r["total_bytes"] == r2["total_bytes"], (r["total_bytes"], r2["total_bytes"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ps = _read("pscan.py")                                                                           # NN1
    assert "doneq" in ps and "wait(pending" not in ps and "window = max(64, procs * 8)" in ps
    assert "실패해도 아래 관리 DB 기록은 시도한다" in _read("scanner.py")                                # NN7
    print("[NN1~NN7] OK  pscan 선형·du 최대 깊이/중복 제거·fold 와 최대 깊이·옛 정지 사유·링크 루트·디스크 가득")


def check_bm_round24_regress() -> None:
    """OO1 데이터 폴더에 '#'·'?'·'%' 가 있으면 백업 DB 가 비거나 빠짐 OO2 pscan 워커가 죽으면 전체 중단(부분 결과 대신)
    OO3 토큰 앞뒤 공백·줄바꿈 저장·업그레이드 토큰 검사 없음 OO4 이름 바꿔 원격 구성하면 논리 경로 유실
    OO5 du 가 native 로 대체돼도 기록·화면은 'du'."""
    import io
    import sqlite3
    import tarfile as _tar
    from isilon_usage import portal as P, scanner as S, db as dbmod, pscan as PS
    d = tempfile.mkdtemp(prefix="iu_bm_")
    try:
        for odd in ("pd#3", "pd?3", "pd%41"):                                                       # OO1
            hq = os.path.join(d, odd)
            pc = P.PortalController(hq)
            c = sqlite3.connect(P.ping_history_path(hq))
            c.execute("CREATE TABLE IF NOT EXISTS t (x)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
            c.close()
            data, _fn = pc.make_backup_bytes()
            with _tar.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                raw = tf.extractfile("portal_backup/ping_history.db").read()
            chk = os.path.join(d, "chk.db")
            with open(chk, "wb") as fh:
                fh.write(raw)
            cc = sqlite3.connect(chk)
            assert cc.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1, odd
            cc.close()
        assert not os.path.exists(os.path.join(d, "pd")), "옆에 빈 DB 를 만듦"
        pc = P.PortalController(os.path.join(d, "hq2"))                                             # OO3
        assert pc.upsert_node({"id": "n1", "url": "http://x:1", "token": "abc\n"})["ok"]
        assert pc._get("n1")["token"] == "abc"
        assert pc.set_upgrade_net("github", "http://x", False, token="한글")["ok"] is False
        pc.upsert_node({"id": "e1", "url": "http://10.0.0.9:8765", "token": "t", "alias_local": "/mnt/new",  # OO4
                        "alias_logical": "/data"})
        r = pc.provision_plan({"host": "10.0.0.9", "name": "e1-renamed", "path": "/mnt/new"})
        assert r["ok"] and pc._get("e1-renamed")["alias_logical"] == "/data", pc._get("e1-renamed")
        T = os.path.join(d, "tree")                                                                  # OO5
        os.makedirs(os.path.join(T, "a", "b"))
        open(os.path.join(T, "a", "b", "f"), "w").close()
        if shutil.which("du"):
            db = os.path.join(d, "du.db")
            S.run_scan(db, T, backend="du", max_depth=1, with_monitor=False)
            c = dbmod.connect(db)
            assert c.execute("SELECT backend FROM scan_runs").fetchone()[0] == "native"
            c.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ps = _read("pscan.py")                                                                           # OO2
    assert "except (RuntimeError, OSError) as exc:   # BrokenProcessPool" in ps
    print("[OO1~OO5] OK  백업 경로 특수 문자·pscan 풀 깨짐 부분 결과·토큰 공백·이름 바꾼 구성 논리 경로·du 대체 기록")


def check_bn_round24_consistency() -> None:
    """화면 간 일관성 PP1 경과가 브라우저 시계 기준 PP2 질의응답 전체 용량·순위가 진행 바이트 PP3 홈 파일 수 중첩 루트 이중 셈
    PP4 FS 사용량 엣지(최근)·포탈(최대) 다름 PP5 사용량 API 바이트·파일 수 다른 스캔 PP6 질의응답이 스냅샷 무시
    PP7 비교에 부분 값 표시 없음 PP8 '결과 복제 N전' 엣지 시계 PP9 깊이 제한 스캔 표시 없음."""
    from isilon_usage import manager as M, db as dbmod, ask as AK, portal as P
    d = tempfile.mkdtemp(prefix="iu_bn_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        c = dbmod.connect(M.manager_db_path(data))

        def reg(root, status, files, byts, upd, fs_used=100, md=None):
            sid = M.register_scan(data, root_path=root, db_path=os.path.join(d, "%s_%s.db" % (root.strip("/").replace("/", "_"), upd)),
                                  backend="native", size_mode="disk")
            c.execute("UPDATE scans SET status=?, phase=?, total_files=?, scanned_bytes=?, updated_at=?, "
                      "fs_total_bytes=1000, fs_used_bytes=?, fs_source='nas:/x', opt_max_depth=? WHERE id=?",
                      (status, status, files, byts, upd, fs_used, md, sid))
            c.commit()
            return sid
        reg("/n", "done", 820, 5000, 100, fs_used=300)
        reg("/n/sub", "done", 360, 2000, 200, fs_used=200)                 # 같은 FS, 더 최근 값 200
        reg("/e2", "done", 960, 4000, 150, fs_used=250, md=2)
        reg("/e2", "paused", 100, 999, 300, fs_used=210)
        ov = M.overall_capacity(c)
        c.close()
        assert ov["total_capacity_files"] == 820 + 960, ov["total_capacity_files"]                   # PP3
        assert ov["fs_total_bytes"] == 1000 and ov["fs_used_bytes"] == 210, (ov["fs_total_bytes"], ov["fs_used_bytes"])  # PP4
        e2 = [r for r in ov["roots"] if r["root_path"] == "/e2"][0]
        assert e2["capacity_depth_limit"] == 2                                                       # PP9
        r = AK._h_total("전체 용량 얼마야", {"overall": ov})                                          # PP2
        assert AK.fmt_bytes(ov["total_capacity_bytes"]) in r["answer"], r["answer"]
        rk = AK._rank_roots({"overall": ov})
        assert rk[0]["root_path"] == "/n" and AK._root_cap(rk[0]) == 5000
        assert P.PortalController._fs_capacity(ov["roots"])[1] == 210                                # PP4(포탈 같은 값)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "function _srvNow()" in dh and "const now = _srvNow(), act" in dh                       # PP1
    assert "o.total_capacity_files" in dh and "o.fs_total_bytes" in dh and "깊이 ${r.capacity_depth_limit}까지만" in dh
    pt = _read("portal.py")
    assert '"latest_scan_id": r.get("scan_id")' in pt                                                # PP5
    assert "_resolve_scan_db(mconn, scan_id, analysis=True)" in _read("server.py")                   # PP6
    assert '"partial": _partial' in pt and "부분 값</span>" in _read("portal.html")                    # PP7
    assert '"capacity_finished_at")' in pt and "n.last_repl_at||n.last_sync" in _read("portal.html")  # PP8
    print("[PP1~PP9] OK  화면 간 일관성: 서버 시계 경과·질의응답 조사 용량·파일 수 중첩 제외·FS 단일 규칙·사용량 API·스냅샷·부분 값·복제 시각·깊이 제한")


def check_bo_round24_perf() -> None:
    """QQ1 검색 정렬을 화면 크기 식으로 바꾼 뒤 완료 스캔의 흔한 검색어가 인덱스를 못 써 1ms → 100ms(24회차 성능 점검)."""
    import random
    from isilon_usage import db as dbmod, server as SV
    d = tempfile.mkdtemp(prefix="iu_bo_")
    try:
        random.seed(1)
        db = os.path.join(d, "x.db")
        dbmod.init_db(db)
        c = dbmod.connect(db)
        rid = dbmod.create_run(c, "/r", backend="native", size_mode="disk", scanner_pid=0)
        c.executemany("INSERT INTO directories (run_id, path, name, depth, status, own_bytes, total_bytes) "
                      "VALUES (?,?,?,?,?,?,?)",
                      ((rid, "/r/s%06d" % i, "s%06d" % i, 1, "done", 1, random.randint(1, 10 ** 9)) for i in range(100000)))
        c.execute("UPDATE scan_runs SET status='done' WHERE id=?", (rid,))
        c.commit()
        plan = " ".join(str(tuple(r)) for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM directories WHERE run_id=? AND path LIKE ? ESCAPE '\\' "
            "ORDER BY total_bytes DESC LIMIT 201", (rid, "%s0%")))
        r = SV.search_dirs(c, rid, "s0")
        assert len(r["results"]) == 200 and r["truncated"]
        sizes = [x["total_bytes"] for x in r["results"]]
        assert sizes == sorted(sizes, reverse=True)
        c.close()
        assert "TEMP B-TREE" not in plan.upper(), plan            # 정렬용 임시 B-트리 없이 인덱스 순서
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert '_order = "total_bytes" if _done else' in _read("server.py")
    print("[QQ1] OK  완료 스캔 검색은 total_bytes 인덱스 순서(흔한 검색어 1ms 대)")


def check_bp_round25_regress() -> None:
    """RR1 연결 테스트가 토큰 앞뒤 공백·줄바꿈을 그대로 보내 실패 RR2 사용량 API status 가 최신 스캔 상태
    RR3 새 칸(capacity_files 등) 숫자 검사 없음 → 엣지가 이상한 값을 보내면 500 RR4 파일 시각 '며칠 전'이 브라우저 시계."""
    import http.server
    from isilon_usage import portal as P
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("X-Auth-Token"))
            ok = self.headers.get("X-Auth-Token") == "tok"
            body = b'{"ok": true, "overall": {"roots": []}, "scans": []}' if ok else b'{"ok": false}'
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = tempfile.mkdtemp(prefix="iu_bp_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        url = "http://127.0.0.1:%d" % srv.server_address[1]
        r = pc.test_node({"url": url, "token": "tok\n"})                                            # RR1
        assert "tok" in seen and "Invalid header" not in str(r.get("reason")), (r, seen)
        assert pc.test_node({"url": url, "token": "한글"})["ok"] is False
        ov = {"roots": [{"root_path": "/a", "scan_id": 6, "status": "sizing", "capacity_scan_id": 5,     # RR3
                         "capacity_files": "abc", "capacity_finished_at": 111, "capacity_partial": False}]}
        clean = P._sanitize_overall(ov)
        assert isinstance(clean["roots"][0].get("capacity_files", 0), (int, float)) or clean["roots"][0].get("capacity_files") is None
    finally:
        srv.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    pt = _read("portal.py")
    assert 'else "done"),' in pt and '"latest_status": r.get("status")' in pt                         # RR2
    assert "const days = Math.floor((_srvNow() - ts) / 86400);" in _read("dashboard.html")            # RR4
    print("[RR1~RR4] OK  연결 테스트 토큰 정리·사용량 API 상태·새 칸 숫자 검사·파일 시각 서버 시계")


def check_bq_round25_schedule() -> None:
    """SS1 시계가 뒤로 가면 그만큼 예약·폴링·복제가 멈춤 SS2 포탈 재시작마다 모든 노드 즉시 재복제 SS3 업그레이드 점검·백업이
    스케줄러를 붙잡음 SS4 '매 1개월'인데 시작일이 지났으면 1년 대기 SS5 N주마다 묶음이 만든 요일 기준 SS6 빈 요일·삭제 경합."""
    import datetime as _d
    from isilon_usage import settings as SE, portal as P

    def ts(y, mo, dd, h=0, mi=0):
        return _d.datetime(y, mo, dd, h, mi).timestamp()
    now = ts(2026, 11, 20, 12)
    # SS1: 간격형은 미래 기록이면 바로 차례, 달력형은 같은 날짜 기록이면 오늘 슬롯은 돈 것으로(26회차: 같은 슬롯 두 번 방지)
    assert SE.schedule_due({"unit": "minute", "every": 5, "last_run": now + 7200}, now)               # SS1
    assert not SE.schedule_due({"unit": "day", "every": 1, "at": "03:00", "anchor": ts(2026, 11, 1),
                                "last_run": ts(2026, 11, 20, 3, 10)}, ts(2026, 11, 20, 3, 5))
    assert SE.schedule_due({"unit": "day", "every": 1, "at": "03:00", "anchor": ts(2026, 11, 1),
                            "last_run": now + 2 * 86400}, now)
    sc = {"unit": "month", "every": 1, "start_month": 11, "start_day": 5, "at": "03:00",               # SS4
          "anchor": ts(2026, 11, 19, 10)}
    fired = []
    t = ts(2026, 11, 19, 10)
    while t < ts(2027, 3, 1):
        if SE.schedule_due(dict(sc, last_run=fired[-1] if fired else 0), t):
            fired.append(t)
        t += 3600
    days = [_d.datetime.fromtimestamp(x).strftime("%Y-%m-%d") for x in fired]
    assert days[:3] == ["2026-12-05", "2027-01-05", "2027-02-05"], days
    wk = {"unit": "week", "every": 2, "weekdays": [1, 5], "at": "03:00", "anchor": ts(2026, 11, 18, 10)}   # SS5
    fired = []
    t = ts(2026, 11, 18, 10)
    while t < ts(2026, 12, 15):
        if SE.schedule_due(dict(wk, last_run=fired[-1] if fired else 0), t):
            fired.append(t)
        t += 3600
    days = [_d.datetime.fromtimestamp(x).strftime("%m-%d") for x in fired]
    assert days[:4] == ["11-20", "11-30", "12-04", "12-14"], days
    e = {"unit": "week", "every": 1, "weekdays": [], "at": "03:00", "anchor": ts(2026, 11, 18, 10)}      # SS6 빈 요일
    assert any(SE.schedule_due(dict(e, last_run=0), ts(2026, 11, 25, h)) for h in range(3, 24))
    d = tempfile.mkdtemp(prefix="iu_bq_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))                                                 # SS2
        pc.upsert_node({"id": "a", "url": "http://10.0.0.1:8765", "token": "t"})
        pc._rep_at["a"] = 12345.0
        pc._save_rep_state()
        pc2 = P.PortalController(os.path.join(d, "hq"))
        assert pc2._rep_at.get("a") == 12345.0, pc2._rep_at
        pc2.delete_node("a")
        assert "a" not in pc2._rep_at
    finally:
        shutil.rmtree(d, ignore_errors=True)
    pt = _read("portal.py")
    assert "def _bg_jobs(self, now: float, do_upg: bool)" in pt                                        # SS3
    assert pt.count("if self._get(nid) is None:   # 폴링 도중 지워진 노드") == 1 and "폴링 도중 지워진 노드 — 캐시·처리량" in pt
    print("[SS1~SS6] OK  시계 역행·복제 상태 저장·별도 스레드·월 첫 발생·주 묶음(일요일)·빈 요일·삭제 경합")


def check_br_round25_client() -> None:
    """대시보드 장시간 세션 TT1 서버 재시작으로 사라진 생성·분석·실측 작업에 버튼이 막힌 채 끝없이 폴링 TT2 서버가 멈추면
    스토리지·오토튜닝·개요 요청이 주기마다 쌓임 TT3 처리용량 팝업을 버튼으로 닫으면 Esc 처리기·DOM 누수 TT4 늦은 트리·검색
    응답이 새 화면을 덮음 TT5 개요도 실패인데 '개요는 정상' TT6 로그인 창 여러 겹."""
    dh = _read("dashboard.html")
    assert dh.count("j.running === false") >= 3 and "생성이 중단됐습니다" in dh and "분석이 중단됐습니다" in dh   # TT1
    for g in ("window._scansInFlight", "window._atInFlight", "window._stInFlight"):                   # TT2
        assert dh.count(g) >= 3, g
    assert 'fetchJsonTimeout("/api/storage", 20000)' in dh and 'fetchJsonTimeout("/api/autotune/status", 8000)' in dh
    assert "const shut = () => { ov.remove(); document.removeEventListener(\"keydown\", esc); };" in dh   # TT3
    assert dh.count("if (my !== _treeSeq) return;") >= 2                                              # TT4(28회차: 트리 오류 처리에도 씀)
    assert "서버에 연결할 수 없습니다" in dh                                                            # TT5
    assert "if (window._unlockPending) return window._unlockPending;" in dh                            # TT6
    print("[TT1~TT6] OK  사라진 작업 감지·요청 쌓임 방지·팝업 처리기 정리·트리 응답 순서·연결 끊김 안내·로그인 창 하나")


def check_bs_round26_regress() -> None:
    """UU1 시계 역행 뒤 미래 시각 백업 파일 때문에 매 틱 백업 UU2 같은 슬롯 두 번 실행(26회차 SS1 보강)
    UU3 자가 업그레이드 설치 중 새 복제 시작 UU4 복제 도중 지운 노드 상태 되살림 UU5 '연결 끊김' 오안내."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_bs_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))                                                  # UU1
        bdir = os.path.join(d, "bk")
        os.makedirs(bdir)
        fut = os.path.join(bdir, "isilon_portal_backup-20991231-000000.tar.gz")
        open(fut, "wb").close()
        now = time.time()
        os.utime(fut, (now + 3600, now + 3600))
        pc.settings["backup_dir"] = bdir
        pc.settings["backup_every_hours"] = 1
        for i in range(5):
            pc._maybe_scheduled_backup(now + i * 5)
        n = len([x for x in os.listdir(bdir) if x.endswith(".tar.gz")])
        assert n == 2, n                     # 미래 파일 + 첫 틱 백업 1개(그 뒤 틱은 기다림)
        pc.settings["backup_keep"] = 1
        pc._prune_backups(bdir, 1)
        assert not os.path.exists(fut), os.listdir(bdir)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    pt = _read("portal.py")
    assert 'not self._upg_state.get("installing")' in pt                                                # UU3
    assert pt.count("복제 도중 지워진 노드") == 2                                                         # UU4
    dh = _read("dashboard.html")
    assert "window._ovFail = false;                    // 응답이 왔다" in dh                               # UU5
    print("[UU1~UU5] OK  미래 백업 파일·같은 슬롯·설치 중 복제·삭제 경합·연결 끊김 안내")


def check_bt_round26_cli() -> None:
    """VV1 바쁜·잘못된 포트에 traceback VV2 재개 안내에 --data-dir 없음 VV3 디스크 가드 정지 사유가 CLI 에 안 보임
    VV4 영속화 표식이 데이터 폴더 하나만 VV5 reset-password 뒤 serve 의 --mount-base 무시 VV6 숫자 인자·메시지 검사
    VV7 livesnap 의 3.6 비호환 import VV8 문서 예시 줄 잇기 주석."""
    import contextlib
    import io
    import socket
    from isilon_usage import cli as C, settings as SE, manager as M, db as dbmod
    d = tempfile.mkdtemp(prefix="iu_bt_")
    cwd = os.getcwd()
    os.chdir(d)
    try:
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        sk.listen(1)
        port = sk.getsockname()[1]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):           # VV1
            rc = C.main(["serve", "--data-dir", os.path.join(d, "e"), "--host", "127.0.0.1", "--port", str(port)])
        sk.close()
        assert rc == 2 and "이미 다른 프로그램" in err.getvalue(), (rc, err.getvalue()[-300:])
        t = os.path.join(d, "t")                                                                     # VV2·VV3
        os.makedirs(t)
        open(os.path.join(t, "f"), "w").close()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = C.main(["scan", t, "--data-dir", os.path.join(d, "dd"), "--min-free-gb", "99999999"])
        out = err.getvalue()
        assert rc == 3 and "--data-dir " + os.path.join(d, "dd") in out and "디스크 공간 부족" in out, out[-400:]
        code = os.path.join(d, "code")                                                               # VV4
        os.makedirs(code)
        for nm in ("A", "B"):
            os.makedirs(os.path.join(d, nm))
            open(os.path.join(d, nm, "settings.json"), "w").close()
            SE.mark_initialized(os.path.join(d, nm), code, ("settings.json",))
        shutil.rmtree(os.path.join(d, "A"))
        os.makedirs(os.path.join(d, "A"))
        assert SE.check_persistence(os.path.join(d, "A"), code, ("settings.json",))[0] is False
        z = os.path.join(d, "Z")                                                                     # VV5
        os.makedirs(z)
        with open(os.path.join(z, "settings.json"), "w") as fh:
            fh.write('{"op_password": "x"}')
        st = SE.seed_if_absent(z, {"mount_bases": ["/mnt/x"]})
        assert st.get("mount_bases") == ["/mnt/x"] and st.get("op_password") == "x", st.get("mount_bases")
        for argv in (["scan", t, "--workers", "0"], ["pscan", t, "-P", "-2"],                         # VV6
                     ["prune", "--data-dir", os.path.join(d, "dd"), "--keep-per-root", "0"]):
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    C.main(argv)
                raise AssertionError("받아들여짐: %s" % argv)
            except SystemExit as e:
                assert e.code == 2, argv
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            assert C.main(["tunecheck", os.path.join(d, "nope")]) == 2
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            C.main(["scan", t, "--data-dir", os.path.join(d, "d2")])
            rc = C.main(["partial", "1", "--data-dir", os.path.join(d, "d2")])
        assert rc == 2 and "이미 완료된" in err.getvalue()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = C.main(["stats", "--scan", "999", "--data-dir", os.path.join(d, "d2")])
        assert rc == 2 and "scan #999 없음" in err.getvalue(), err.getvalue()
    finally:
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    assert "from __future__ import annotations" not in _read("livesnap.py")                          # VV7
    root = os.path.dirname(HERE)
    for f in ("docs/INSTALL.md", "docs/DOCKER.md"):                                                   # VV8
        txt = open(os.path.join(root, f), encoding="utf-8").read()
        assert not re.search(r"\\[ \t]+#", txt), f
    print("[VV1~VV8] OK  포트 안내·재개 안내·정지 사유·영속 표식 여러 폴더·reset-password 뒤 초기값·CLI 범위·3.6·문서 예시")


def check_bu_round27_regress() -> None:
    """WW1 영속 표식 50개 넘으면 쓰는 폴더가 밀려남 WW2 동시 표식 기록 유실 WW3 재개 안내 경로 공백 WW4 autotune --procs 범위
    WW5 미래 백업 파일이 '마지막 백업'으로."""
    import contextlib
    import io
    import multiprocessing as mp
    from isilon_usage import settings as SE, cli as C, portal as P
    d = tempfile.mkdtemp(prefix="iu_bu_")
    try:
        code = os.path.join(d, "code")
        os.makedirs(code)

        def mk(nm):
            p = os.path.join(d, nm)
            os.makedirs(p, exist_ok=True)
            open(os.path.join(p, "settings.json"), "w").close()
            return p
        prod = mk("prod")
        SE.mark_initialized(prod, code, ("settings.json",))
        for i in range(55):                                                                          # WW1
            SE.mark_initialized(mk("o%d" % i), code, ("settings.json",))
            if i % 10 == 0:
                SE.mark_initialized(prod, code, ("settings.json",))     # 운영 폴더는 계속 다시 뜬다
        os.remove(os.path.join(prod, "settings.json"))
        assert SE.check_persistence(prod, code, ("settings.json",))[0] is False
        code2 = os.path.join(d, "code2")                                                             # WW2
        os.makedirs(code2)
        dirs = [mk("c%d" % i) for i in range(6)]
        ps = [mp.Process(target=_mark_many, args=(x, code2)) for x in dirs]
        for p_ in ps:
            p_.start()
        for p_ in ps:
            p_.join(60)
        with open(os.path.join(code2, ".iu_data_persist.json"), encoding="utf-8") as fh:
            got = __import__("json").load(fh)["data_dirs"]
        assert all(x in got for x in dirs), (len(got), sorted(got))
        try:                                                                                          # WW4
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                C.main(["autotune", d, "--procs", "0"])
            raise AssertionError("--procs 0 받아들여짐")
        except SystemExit as e:
            assert e.code == 2
        pc = P.PortalController(os.path.join(d, "hq"))                                               # WW5
        bdir = os.path.join(d, "bk")
        os.makedirs(bdir)
        fut = os.path.join(bdir, "isilon_portal_backup-20991231-000000.tar.gz")
        open(fut, "wb").close()
        os.utime(fut, (time.time() + 3600, time.time() + 3600))
        pc.settings["backup_dir"] = bdir
        assert pc.backup_info()["backup_last"] == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "shlex.quote(os.path.dirname(os.path.abspath(manager_db)))" in _read("cli.py")             # WW3
    print("[WW1~WW5] OK  영속 표식 LRU·동시 기록 잠금·재개 안내 따옴표·autotune 범위·마지막 백업 표시")


def check_bv_round27_security() -> None:
    """XX1 큰 JSON 본문 깊이 검사 한 번에 XX2 복제 meta.json 크기·목록 상한 XX3 db_filename 경로로 복제본 밖 DB 열기
    XX4 설정 응답의 URL 비밀값(웹훅·사용자 정보·쿼리 토큰)과 가린 값 되돌림 XX5 로그인 없이 외부 LLM 호출
    XX6 클릭재킹·MIME 스니핑 헤더 XX7 enroll_token 무제한 추측."""
    import json as _json
    import urllib.request
    from isilon_usage import server as SV, portal as P, manager as M, netutil as NU, ask as AK
    assert SV.MAX_JSON_BODY <= 4 * 1024 * 1024                                                       # XX1
    deep = cur = []
    for _ in range(SV.MAX_JSON_DEPTH + 5):
        nxt = []
        cur.append(nxt)
        cur = nxt
    assert SV._scan_body(deep)[0] > SV.MAX_JSON_DEPTH
    assert SV._scan_body({"a": ["x\x00"]})[1] is True
    src = _read("portal.py")                                                                         # XX2
    assert "src.read(META_MAX_BYTES + 1)" in src and "[:20000]" in src and "[:5000]" in src
    assert "sc = dict(sc, db_filename=dbf)" in src
    d = tempfile.mkdtemp(prefix="iu_bv_")
    httpd = None
    try:
        pc = P.PortalController(os.path.join(d, "hq"))                                               # XX3
        rep = os.path.join(pc.replicas_dir, "n1")
        os.makedirs(os.path.join(rep, "scans"))
        outside = os.path.join(d, "outside.db")
        open(outside, "w").close()
        os.symlink(outside, os.path.join(rep, "scans", "link.db"))
        open(os.path.join(rep, "scans", "ok.db"), "w").close()
        meta = {"scans": [{"root_path": "/r", "db_filename": outside, "status": "done", "finished_at": 3},
                          {"root_path": "/r", "db_filename": "../../outside.db", "status": "done", "finished_at": 2},
                          {"root_path": "/r", "db_filename": "link.db", "status": "done", "finished_at": 4},
                          {"root_path": "/r", "db_filename": "ok.db", "status": "done", "finished_at": 1}]}
        with open(os.path.join(rep, "meta.json"), "w", encoding="utf-8") as fh:
            _json.dump(meta, fh)
        got = pc._replica_dbs_for("n1", "/r/a")
        assert got == [os.path.join(rep, "scans", "ok.db")], got
        assert [x for x, _r in pc._replica_cands("n1", "/r/a")] == got
        s0 = {"notify_webhook": "https://hooks.example.com/services/T1/B2/SECRET",                   # XX4
              "ask_llm_endpoint": "https://user:pw@llm.example.com/v1?key=SECRET",
              "upgrade_url": "https://mirror.example.com/dl?token=SECRET",
              "storage_arrays": [{"id": "a1", "type": "unity", "url": "https://adm:SECRET@u.example.com",
                                  "password": "p"}]}
        pub = SV._public_settings(s0)
        assert "SECRET" not in _json.dumps(pub), pub
        ctl = SV.ScanController(os.path.join(d, "edge"))
        ctl.update_settings(dict(s0))
        res = ctl.update_settings(_json.loads(_json.dumps(SV._public_settings(ctl.settings))))
        assert res.get("ok"), res
        for k in ("notify_webhook", "ask_llm_endpoint", "upgrade_url"):
            assert ctl.settings[k] == s0[k], (k, ctl.settings[k])
        assert ctl.settings["storage_arrays"][0]["url"] == s0["storage_arrays"][0]["url"]
        assert "SECRET" not in _json.dumps(ctl.upgrade_status())
        ctl.update_settings({"notify_webhook": "https://hooks.example.com/new"})          # 새 값은 그대로 저장
        assert ctl.settings["notify_webhook"] == "https://hooks.example.com/new"
        ctl.update_settings({"notify_webhook": ""})                                       # 비우면 지워진다
        assert ctl.settings["notify_webhook"] == ""
        assert NU.keep_masked("https://hooks.example.com/***", "") == "https://hooks.example.com/***"
        pc.set_upgrade_net("github", "https://mirror.example.com/dl?token=SECRET", False)
        st = pc.upgrade_status()
        assert "SECRET" not in _json.dumps(st)
        pc.set_upgrade_net("github", st["url_custom"], False)
        assert pc.settings["upgrade_url"] == "https://mirror.example.com/dl?token=SECRET"
        pc.set_ask_llm({"ask_llm_endpoint": "https://u:SECRET@l.example.com/v1"})
        assert "SECRET" not in _json.dumps(pc.upgrade_config(), default=str)
        pc.set_ask_llm({"ask_llm_endpoint": pc.upgrade_config()["ask_llm_endpoint"]})
        assert pc.settings["ask_llm_endpoint"] == "https://u:SECRET@l.example.com/v1"
        # XX5·XX6: 비밀번호 있는 엣지 — 로그인 없는 /api/ask 는 LLM 을 끈 채로
        data = os.path.join(d, "data")
        M.init_manager(data)
        httpd = SV.serve(data, host="127.0.0.1", port=0,
                         initial_settings={"ask_llm_enabled": True, "ask_llm_endpoint": "http://127.0.0.1:9/v1"})
        httpd.controller.update_settings({"op_lock": True, "op_password": "pw1234"})
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        seen = []
        orig = AK.respond
        SV.askmod.respond = lambda q, data_, cfg: (seen.append(bool(cfg.get("enabled"))) or {"ok": True})
        try:
            urllib.request.urlopen(base + "/api/ask?q=total").read()
            tok = httpd.controller.unlock("pw1234", "t")["token"]
            urllib.request.urlopen(urllib.request.Request(base + "/api/ask?q=total",
                                                          headers={"X-Op-Token": tok})).read()
        finally:
            SV.askmod.respond = orig
        assert seen == [False, True], seen
        r = urllib.request.urlopen(base + "/")
        assert r.headers.get("X-Frame-Options") == "DENY" and r.headers.get("X-Content-Type-Options") == "nosniff"
        assert "frame-ancestors 'none'" in (r.headers.get("Content-Security-Policy") or "")
        r = urllib.request.urlopen(base + "/api/scans")
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "_security_headers(self)" in src
        pc.settings["enroll_token"] = "right-token"                                                  # XX7
        for _ in range(5):
            assert pc.enroll_node({"enroll_token": "bad", "url": "http://e:8765"}, "10.0.0.9")["_status"] == 401
        r7 = pc.enroll_node({"enroll_token": "right-token", "url": "http://e:8765"}, "10.0.0.9")
        assert r7.get("_status") == 429, r7                  # 잠김 — 맞는 토큰도 잠시 거부
        r8 = pc.enroll_node({"enroll_token": "bad", "url": "http://e:8765"}, "10.0.0.10")
        assert r8.get("_status") == 401                      # 다른 IP 는 영향 없음
    finally:
        if httpd is not None:
            httpd.shutdown()
        shutil.rmtree(d, ignore_errors=True)
    assert "_tk ? {\"X-Op-Token\": _tk}" in _read("dashboard.html")
    print("[XX1~XX7] OK  본문 검사·복제 meta 상한·복제본 밖 DB 차단·URL 비밀값 가림·로그인 없는 LLM 차단·보안 헤더·enroll 잠금")


def check_bw_round26_portal_ui() -> None:
    """YY1 포탈 5초 조회가 멈춘 포탈에 쌓임 YY2 연결 끊김에도 '방금 갱신' YY3 버튼(테스트·가져오기·저장·삭제)이 연결 실패에 멈춤
    YY4 늦은 응답이 새 비교·핑 차트·질의 답을 덮음 YY5 잠김·연결 실패도 '비밀번호가 올바르지 않습니다' YY6 동시 401 에 로그인 창 여러 개
    YY7 개별 스캔 버튼이 다시 그리기로 되살아남·전체 시작/중단 중복 YY8 푸시 진행 조회가 한 번 실패로 멈춤 YY9 릴리스 버전 미이스케이프.
    (브라우저 재현·확인은 Playwright 로 — 고친 뒤 동시 요청 최대 4, 각 항목 정상)"""
    ph = _read("portal.html")
    assert 'setInterval(()=>{ _poll1("auth",loadAuth); _poll1("ov",loadOverview); _poll1("nodes",loadNodes); }, 5000);' in ph  # YY1
    assert '_fetchJT("/api/portal/overview", 8000)' in ph and '_fetchJT("/api/portal/nodes", 8000)' in ph   # 28회차: 본문까지 시간 제한
    assert "포탈 연결 끊김" in ph and '"마지막 갱신: 방금" + (regionFilter' not in ph                       # YY2
    assert ph.count("포탈에 연결할 수 없습니다 (") >= 4                                                    # YY3
    assert "seq!==window._cmpSeq" in ph and "seq!==window._phSeq" in ph and "seq!==_askSeq" in ph          # YY4
    assert "window.alert(j.reason || " in ph and "포탈 서버에 연결할 수 없습니다" in ph                       # YY5
    assert "await _loginOnce()" in ph                                                                     # YY6
    assert "_scanPending.has(n.id)" in ph and "if(_allBusy) return;" in ph                                 # YY7
    assert "_pushErr<=30" in ph and 'if(m.style.display==="none") return;' in ph                          # YY8
    assert 'v${_he(j.release_content_version||"?")}' in ph                                                # YY9
    print("[YY1~YY9] OK  포탈 화면 조회 단일화·끊김 표시·버튼 실패 처리·응답 순서·로그인 사유·중복 방지·푸시 재시도")


def check_bx_round28_regress() -> None:
    """ZZ1 저장소·Isilon 상태의 base 에 URL 비밀값 ZZ2 업그레이드 상태 source·download_url·로그에 비밀값 ZZ3 포탈 조회 시간 제한이
    본문 읽기 전에 풀림(본문이 멈추면 주기 조회 영구 정지) ZZ4 가린 주소 일부만 고쳐 '***' 저장 ZZ5 가린 주소로 옛 어레이 비밀 물려받음."""
    import json as _json
    from isilon_usage import server as SV, portal as P, netutil as NU
    d = tempfile.mkdtemp(prefix="iu_bx_")
    try:
        ctl = SV.ScanController(os.path.join(d, "edge"))                                              # ZZ1
        ctl._storage_status_raw = lambda force=False: [{"type": "isilon", "base": "https://u:PW1@h:8080/p?tok=Z",
                                                         "error": "<urlopen error https://h/x?tok=Z>"}]
        out = _json.dumps(ctl.storage_status()) + _json.dumps(ctl.isilon_status())
        assert "PW1" not in out and "tok=Z" not in out, out
        ctl.settings["upgrade_url"] = "http://m.example/base?token=SECRETQ"                         # ZZ2
        ctl._upg_state.update({"source": "http://m.example/base/versions.json?token=SECRETQ",
                               "download_url": "http://usr:SECRETPW@m.example/x.tar.gz",
                               "check_error": "fail http://m.example/b?token=SECRETQ"})
        out = _json.dumps(ctl.upgrade_status())
        assert "SECRETQ" not in out and "SECRETPW" not in out, out
        pc = P.PortalController(os.path.join(d, "hq"))
        pc._upg_state["source"] = "http://m.example/base/versions.json?token=SECRETQ"
        assert "SECRETQ" not in _json.dumps(pc.upgrade_status())
        ctl.update_settings({"notify_webhook": "https://hooks.example.com/services/T/B/SEC"})       # ZZ4
        r = ctl.update_settings({"notify_webhook": "https://other.example.com/***"})
        assert r.get("ok") is False and ctl.settings["notify_webhook"].endswith("/SEC"), r
        pc.set_upgrade_net("github", "https://m.example/dl?token=SECRETQ", False)
        r = pc.set_upgrade_net("github", "https://other.example/dl?***", True)
        assert r.get("ok") is False and pc.settings["upgrade_url"].endswith("SECRETQ") and not pc.settings.get("upgrade_auto")
        assert NU.is_masked_leftover("https://x/***", "") and not NU.is_masked_leftover("https://x/a", "")
        ctl.update_settings({"storage_arrays": [{"id": "a1", "type": "unity", "url": "https://adm:SEC@u.example.com",
                                                 "password": "pw"}]})                                 # ZZ5
        r = ctl.update_settings({"storage_arrays": [
            {"id": "a1", "type": "unity", "url": "https://***@u.example.com", "password": ""},
            {"type": "unity", "url": "https://***@u.example.com", "password": ""}]})
        assert r.get("ok") is False, r                      # 새 어레이가 가린 주소로 옛 비밀을 물려받지 않음
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ph = _read("portal.html")                                                                       # ZZ3
    assert "return await r.json(); }\n  finally{ if(t) clearTimeout(t); }" in ph and "_fetchT(" not in ph
    assert '_fetchJT("/api/portal/auth", 8000, null, _origFetch)' in ph and "_pushErr=0;\n" in ph
    print("[ZZ1~ZZ5] OK  상태 응답 URL 비밀값 가림·포탈 조회 본문 시간 제한·가린 주소 일부 수정 거부·어레이 비밀 승계 차단")


def check_by_round28_install() -> None:
    """AAA1 링크·'..'로 데이터 폴더가 설치 폴더 안을 가리키면 검사를 지나쳐 업그레이드가 데이터 삭제
    AAA2 설치 폴더가 데이터 폴더 안 AAA3 되돌리기 실패도 '되돌렸습니다' AAA4 등록 실패에도 '자기등록했습니다'
    AAA5 중간 실패 시 임시 폴더 남음 AAA6 systemd 없으면 요약·등록 전에 오류 종료 AAA7 --port 값 없음/0."""
    root = os.path.dirname(HERE)
    for fn in ("install_edge.sh", "install_portal.sh"):
        path = os.path.join(root, "tools", fn)
        sh = open(path, encoding="utf-8").read()
        assert 'if [ -L "$INSTALL_DIR" ]' not in sh                                                  # AAA1
        assert '"${DATA_DIR%/}"/*) echo "✗ 설치 폴더' in sh                                             # AAA2
        assert "옛 설치도 되돌리지 못했습니다" in sh                                                     # AAA3
        assert "trap 'rm -rf \"${TMP_DIR:-}\" \"${NEW_DIR:-}\"" in sh                                # AAA5
        assert "if [ -d /run/systemd/system ]; then" in sh                                             # AAA6
        assert '--port)        PORT="${2:-}"' in sh and '"$PORT" -gt 65535' in sh                       # AAA7
        # 실제 정규화 동작: 스크립트 앞부분(경로 처리)만 떼어 실행 — 링크·'..'가 실제 경로로 풀린다
        start = sh.index('case "$INSTALL_DIR" in /*)')
        end = sh.index("for _v in", start)
        d = tempfile.mkdtemp(prefix="iu_by_")
        try:
            os.makedirs(os.path.join(d, "e3", "inner"))
            os.symlink(os.path.join(d, "e3", "inner"), os.path.join(d, "dlink"))
            os.symlink(d, os.path.join(d, "par"))
            snippet = ('INSTALL_DIR="%s/par/e3/../e3"; DATA_DIR="%s/dlink"\n' % (d, d)) + sh[start:end] + \
                'printf "%s|%s" "$INSTALL_DIR" "$DATA_DIR"\n'
            out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, timeout=20).stdout
            real = os.path.realpath(d)
            assert out == "%s/e3|%s/e3/inner" % (real, real), (fn, out)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    edge = open(os.path.join(root, "tools", "install_edge.sh"), encoding="utf-8").read()
    assert 'if [ -n "$HQ" ] && [ "${OK:-0}" = "1" ]; then' in edge                                    # AAA4
    print("[AAA1~AAA7] OK  설치 경로 정규화(링크·..)·데이터/설치 폴더 포함 거부·되돌리기 실패 안내·등록 실패 안내·임시 폴더 정리·systemd 없음·포트 검사")


def check_bz_round28_dashboard() -> None:
    """BBB1 시작 요청 중 갱신이 버튼을 다시 켜 같은 경로 스캔 두 개 BBB2 행 동작(중지·재개·삭제) 중복·거짓 실패 알림
    BBB3 설정 저장 두 번 BBB4 중복 찾기·테스트 데이터 생성 중복 BBB5 진행 조회가 멈춘 서버에 쌓임 BBB6 늦은 응답이 새 화면 덮음
    BBB7 트리 오류가 서버 복구 뒤에도 남음 BBB8 로그인 실패 사유 BBB9 개요만 실패하면 표시 없음·끊김 안내가 홈에만
    BBB10 홈 '지금 스캔' 결과 안 보임 BBB11 스냅샷 확인이 한 번 실패로 멈춤 BBB12 누르는 중 표가 바뀌어 클릭 사라짐.
    (Playwright 로 재현·확인: 시작 POST 1회·재개 POST 1회·저장 중 버튼 꺼짐·끊김 배너·진행 조회 동시 1개)"""
    dh = _read("dashboard.html")
    assert "btn.disabled = !canScan || _startBusy;" in dh and "async function _startScanImpl()" in dh       # BBB1
    assert "const _rowBusy = new Set();" in dh and "_rowGuard(id, () => _scanActionReq(action, id))" in dh  # BBB2
    assert '(id === "saveSettingsBtn" && window._saveBusy === true)' in dh                                 # BBB3
    assert "if (_dupsBusy) return;" in dh and dh.count('$("gtStart").disabled = true;') >= 2               # BBB4
    for flag in ("_gtInFlight", "_depInFlight", "_benchInFlight"):                                         # BBB5
        assert "if (window.%s) return;" % flag in dh, flag
    for k in ("err", "fh", "diff", "tp", "ask", "browse"):                                                 # BBB6
        assert '_seqIs("%s", sq)' % k in dh, k
    assert "window._treeErr = {parentId: parentId, crumb: crumb};" in dh                                   # BBB7
    assert 'window.alert(j.reason || "비밀번호가 올바르지 않습니다.");' in dh                                  # BBB8
    assert "function _updNetBanner()" in dh and "서버 응답 없음" in dh                                      # BBB9
    assert "m.offsetParent === null && m.textContent" in dh                                                # BBB10
    assert "if (++fails >= 10) throw e2;" in dh                                                            # BBB11
    assert "if (body._ovHtml !== _ovHtml) {" in dh                                                         # BBB12
    print("[BBB1~BBB12] OK  엣지 화면 중복 요청 방지·진행 조회 단일화·응답 순서·트리 복구·로그인 사유·공통 끊김 배너·스냅샷 재시도")


def check_ca_round29_regress() -> None:
    """CCC1 행 동작 실패 뒤 버튼이 영영 꺼짐(그리기 캐시) CCC2 어레이 id(=주소)로 토큰 노출 CCC3 실패한 트리 재조회가 검색 결과를 덮음
    CCC4 URL 가리기가 끝 문장부호를 삼킴·긴 문자열에서 느림 CCC5 설치·데이터 폴더 '/' 통과 CCC6 systemd 없음에도 '서비스 등록·재시작' 완료."""
    import json as _json
    from isilon_usage import server as SV, settings as SE, netutil as NU
    dh = _read("dashboard.html")
    assert "finally { _rowBusy.delete(k); const ob = $(\"ovBody\"); if (ob) ob._ovHtml = null; }" in dh       # CCC1
    assert "id: (name || url)" not in dh                                                                  # CCC2
    a = SE._sanitize_storage_arrays([{"type": "unity", "url": "https://a2/api?k=SECRET2"}])[0]
    assert "SECRET2" not in a["id"], a
    legacy = {"storage_arrays": [{"id": "https://a2/api?k=SECRET2", "type": "unity", "url": "https://a2/api?k=SECRET2",
                                  "password": "p"}]}
    pub = SV._public_settings(legacy)
    assert "SECRET2" not in _json.dumps(pub), pub
    d = tempfile.mkdtemp(prefix="iu_ca_")
    try:
        ctl = SV.ScanController(os.path.join(d, "edge"))
        ctl.update_settings(dict(legacy))
        r = ctl.update_settings(_json.loads(_json.dumps(SV._public_settings(ctl.settings))))
        assert r.get("ok"), r
        sa = ctl.settings["storage_arrays"][0]
        assert sa["id"] == sa["url"] == "https://a2/api?k=SECRET2" and sa["password"] == "p", sa
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "window._treeErr = null;      // 검색으로" in dh and "window._treeErr = null;      // 다른 스캔" in dh  # CCC3
    assert NU.redact_urls("(https://h/p?q=1), end") == "(https://h/p?***), end"                        # CCC4
    t0 = time.time()
    NU.redact_urls("a" * 100000 + " http://x/?a=1")
    assert time.time() - t0 < 0.5
    for fn in ("install_edge.sh", "install_portal.sh"):                                                # CCC5·CCC6
        sh = open(os.path.join(os.path.dirname(HERE), "tools", fn), encoding="utf-8").read()
        assert 'if [ "$INSTALL_DIR" = "/" ] || [ "$DATA_DIR" = "/" ]; then' in sh and "systemd 없음 — 서비스 등록·재시작은 건너뜀" in sh, fn
    print("[CCC1~CCC6] OK  행 버튼 복구·어레이 id 가림·트리 재조회 정리·URL 가리기 정규식·설치 루트 거부·systemd 없음 안내")


def check_cb_round29_numbers() -> None:
    """DDD1 CLI status 합계가 최신(멈춘 것 포함)·중첩 루트 포함으로 다른 값 DDD2 깊이 제한·접근 불가 폴더로 덜 센 합계에 표시 없음
    DDD3 크기 기준이 다른 스캔 비교 시 엉터리 변화·얕게 저장한 스캔과 비교하면 '삭제' DDD4 같은 값 형식이 화면마다 다름
    DDD5 pscan 행의 하위 폴더 수(들어갈 수 없는 폴더에 '하위 7개') DDD6 리포트 합계가 트리보다 작은 이유 표시 없음."""
    import contextlib
    import io
    from isilon_usage import manager as M, db as dbmod, cli as C, ask as AK, server as SV
    from isilon_usage.scanner import run_scan
    d = tempfile.mkdtemp(prefix="iu_cb_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        t = os.path.join(d, "T")
        os.makedirs(os.path.join(t, "sub", "a", "b"))
        for p_, n in ((t, 3000), (os.path.join(t, "sub"), 1000), (os.path.join(t, "sub", "a", "b"), 500)):
            with open(os.path.join(p_, "f"), "wb") as fh:
                fh.write(b"x" * n)
        ids = []
        for root, mode in ((t, "apparent"), (os.path.join(t, "sub"), "apparent"), (t, "disk")):
            dbp = os.path.join(data, "scans", "s%d.db" % len(ids))
            os.makedirs(os.path.dirname(dbp), exist_ok=True)
            sid = M.register_scan(data, root_path=root, db_path=dbp, backend="native", size_mode=mode)
            run_scan(dbp, root, workers=2, size_mode=mode, with_monitor=False,
                     manager_db=M.manager_db_path(data), manager_scan_id=sid)
            ids.append(sid)
        c = dbmod.connect(M.manager_db_path(data))
        c.execute("UPDATE scans SET error_dirs=2 WHERE id=?", (ids[2],))
        c.commit()
        ov = M.overall_capacity(c)
        c.close()
        rt = [r for r in ov["roots"] if r["root_path"] == t][0]
        assert rt["capacity_error_dirs"] == 2 and "읽기 오류 2건" in ov["capacity_caveat"], (rt, ov["capacity_caveat"])   # DDD2
        buf = io.StringIO()                                                                                # DDD1
        with contextlib.redirect_stdout(buf):
            C.main(["status", "--data-dir", data])
        out = buf.getvalue()
        assert C._human(ov["total_capacity_bytes"]) in out.split("총 조사 용량:")[1].splitlines()[0], out
        assert "합계 제외" in out and "읽기 오류 2건" in out, out
        r = SV.diff_scans(data, ids[0], ids[2])                                                            # DDD3
        assert r.get("ok") is False and r.get("size_mode_mismatch"), r
        r = SV.diff_scans(data, ids[0], ids[0])
        assert r.get("ok") and "compared_depth" in r, r          # 30회차: 잘린 스캔이 아니면 None(전체 비교)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert AK.fmt_bytes(518864672) == "494.8 MB" == C._human(518864672)                                  # DDD4
    dh = _read("dashboard.html")
    assert 'return n.toFixed(1) + " " + u[i];' in dh
    assert "int(c.get(\"files\", 0)), 0," in _read("pscan.py")                                        # DDD5
    assert "분포 합계는 파일만" in dh                                                                       # DDD6
    assert "t.capacity_caveat" in _read("portal.html") and "o.capacity_caveat" in dh
    print("[DDD1~DDD6] OK  CLI 합계 통일·덜 센 이유 공통 문구·비교 기준/깊이·용량 형식 통일·pscan 하위 수·리포트 합계 안내")


def check_cc_round29_flows() -> None:
    """EEE1 주소(호스트)만 바꾸면 저장된 LLM 키·업그레이드 토큰·노드 토큰이 새 호스트로 감 EEE2 포탈 노드 anchor 이상값으로 복제 멈춤
    EEE3 두 탭 예약 저장 — 늦은 쪽이 다른 쪽 예약을 지움 EEE4 이름 같은 어레이 하나를 지우면 다른 어레이 비밀번호 사라짐
    EEE5 '이름 <주소>' 받는 사람이면 메일 전부 실패 EEE6 같은 경로의 다른 예약을 지우면 남은 예약이 마지막 실행을 물려받음 EEE7 시차 배치 개수."""
    from isilon_usage import server as SV, portal as P, notify as NT, settings as SE
    d = tempfile.mkdtemp(prefix="iu_cc_")
    try:
        ctl = SV.ScanController(os.path.join(d, "edge"))
        ctl.update_settings({"ask_llm_endpoint": "http://llm-a:1/v1", "ask_llm_key": "K1",
                             "upgrade_url": "https://m-a.example/dl", "upgrade_token": "T1"})            # EEE1
        ctl.update_settings({"ask_llm_endpoint": "http://llm-a:1/v2"})           # 같은 호스트 경로만 → 유지
        assert ctl.settings["ask_llm_key"] == "K1"
        ctl.update_settings({"ask_llm_endpoint": "http://evil:1/v1", "upgrade_url": ""})
        assert ctl.settings["ask_llm_key"] == "" and ctl.settings["upgrade_token"] == "", ctl.settings
        pc = P.PortalController(os.path.join(d, "hq"))
        assert pc.upsert_node({"id": "n1", "url": "http://e1:8765", "token": "tokA"})["ok"]
        r = pc.upsert_node({"id": "n1", "url": "http://e1:8765/x"})
        assert pc._get("n1")["token"] == "tokA", r
        r = pc.upsert_node({"id": "n1", "url": "http://e2:8765"})
        assert pc._get("n1")["token"] == "" and r.get("token_dropped"), r
        pc.set_ask_llm({"ask_llm_endpoint": "http://l1:1/v1", "ask_llm_key": "PK"})
        pc.set_ask_llm({"ask_llm_endpoint": "http://l2:1/v1"})
        assert pc.settings.get("ask_llm_key") == ""
        assert pc.upsert_node({"id": "n2", "url": "http://e3:8765", "token": "t", "unit": "day", "every": 1,
                               "at": "00:01", "anchor": 1e300})["ok"]                                 # EEE2
        assert pc._get("n2")["anchor"] < time.time() + 10
        pc._get("n2")["anchor"] = 1e300
        pc._rep_due(pc._get("n2"), time.time())                    # 예외 없이 돈다
        base = [{"paths": ["/a"], "unit": "day", "at": "03:00"}]                                     # EEE3
        ctl.update_settings({"schedules": base})
        r = ctl.update_settings({"schedules": base + [{"paths": ["/b"], "unit": "day", "at": "04:00"}],
                                 "_list_base": {"schedules": []}})
        assert r.get("ok") is False and r.get("conflict"), r
        r = ctl.update_settings({"schedules": base + [{"paths": ["/b"], "unit": "day", "at": "04:00"}],
                                 "_list_base": {"schedules": base}})
        assert r.get("ok"), r
        arrs = [{"id": "U1", "name": "U1", "type": "unity", "url": "https://unity.example", "password": "pw1"},
                {"id": "U1", "name": "U1", "type": "unity", "url": "https://other.example", "password": "pw3"}]   # EEE4
        ctl.update_settings({"storage_arrays": arrs})
        pub = SV._public_settings(ctl.settings)["storage_arrays"][1:]
        assert ctl.update_settings({"storage_arrays": pub})["ok"]
        assert ctl.settings["storage_arrays"][0]["password"] == "pw3", ctl.settings["storage_arrays"]
        assert NT.parse_recipients('a@x.com, 김철수 <b@x.com>; c@x.com d@y.com') == \
            ["a@x.com", "b@x.com", "c@x.com", "d@y.com"]                                                 # EEE5
        now = time.time()                                                                              # EEE6
        ctl.settings["schedules"] = [{"paths": ["/d"], "unit": "minute", "every": 5, "last_run": now - 10, "anchor": now - 999},
                                     {"paths": ["/d"], "unit": "day", "at": "03:00", "last_run": 0, "anchor": now - 5}]
        out = ctl._merge_sched_runtime([{"paths": ["/d"], "unit": "day", "at": "03:00"}])
        assert out[0].get("last_run") in (0, None), out
        out = ctl._merge_sched_runtime([{"paths": ["/d", "/e"], "unit": "day", "at": "03:00"}])
        assert out[0].get("last_run") == 0 and "chain_i" not in out[0], out
        assert '"minute", "hour"))' in _read("server.py")                                              # EEE7
        assert SE.schedule_due is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "_list_base" in _read("dashboard.html")
    print("[EEE1~EEE7] OK  호스트 바뀌면 비밀 삭제·포탈 anchor 검사·목록 충돌 409·같은 이름 어레이·받는 사람 형식·예약 짝짓기·시차 개수")


def check_cd_running_block() -> None:
    """FFF1 (사용자 요청) 스캔이 도는 경로의 '지금 스캔'을 눌러 다시 시작할 수 있었다 — 화면에서 버튼을 끄고, 어느 입구로
    시작하든(새 스캔·NAS 표·확인 필요) 확인 창 전에 막는다(서버 중복 거부는 그대로). Playwright 로 '스캔 중 #N' 꺼짐·시작 요청 0회 확인."""
    dh = _read("dashboard.html")
    assert "function _scanRunningOn(path)" in dh and "_ovRootsLatest = o.roots || [];" in dh
    assert "const runId = _scanRunningOn(row.path);" in dh and "스캔 중 #${runId}</button>" in dh
    i = dh.index("async function _startScanImpl()")
    assert dh.index("_scanRunningOn(path)", i) < dh.index("confirmScanStart(", i)
    assert "이미 같은 경로를 스캔 중입니다" in _read("server.py")
    print("[FFF1] OK  진행 중 경로의 '지금 스캔' 차단(화면 버튼 끔·확인 전 차단·서버 중복 거부)")


def check_ce_round30_regress() -> None:
    """GGG1 정상 스캔끼리 비교해도 저장된 최대 깊이로 잘라 지운 깊은 폴더가 '삭제'에 안 나옴 GGG2 'prod@dc1' 같은 어레이 이름(id)이
    '***' 로 가려져 저장 시 두 어레이가 합쳐짐 GGG3 'Kim <a@x> b@y' 에서 b@y 누락 GGG4 '접근 불가 폴더'에 파일 오류까지 셈(문구)."""
    import json as _json
    from isilon_usage import manager as M, server as SV, notify as NT
    from isilon_usage.scanner import run_scan
    d = tempfile.mkdtemp(prefix="iu_ce_")
    try:
        data = os.path.join(d, "data")
        M.init_manager(data)
        t = os.path.join(d, "tree")
        os.makedirs(os.path.join(t, "a", "b", "c"))
        os.makedirs(os.path.join(t, "d"))
        with open(os.path.join(t, "a", "b", "c", "f"), "wb") as fh:
            fh.write(b"x" * 5000)
        ids = []
        for n in range(2):
            if n == 1:
                shutil.rmtree(os.path.join(t, "a", "b"))
            dbp = os.path.join(data, "scans", "s%d.db" % n)
            os.makedirs(os.path.dirname(dbp), exist_ok=True)
            sid = M.register_scan(data, root_path=t, db_path=dbp, backend="native", size_mode="apparent")
            run_scan(dbp, t, workers=1, size_mode="apparent", with_monitor=False,
                     manager_db=M.manager_db_path(data), manager_scan_id=sid)
            ids.append(sid)
        r = SV.diff_scans(data, ids[0], ids[1], limit=100)                                            # GGG1
        removed = [x["path"] for x in r["rows"] if x["target_bytes"] == 0 and x["base_bytes"] > 0]
        assert any(p.endswith("/a/b/c") for p in removed) and r.get("compared_depth") is None, r
        ctl = SV.ScanController(os.path.join(d, "edge"))                                              # GGG2
        arrs = [{"id": "prod@dc1", "name": "prod@dc1", "type": "unity", "url": "https://h1", "password": "p1"},
                {"id": "prod@dc2", "name": "prod@dc2", "type": "unity", "url": "https://h1", "password": "p2"}]
        ctl.update_settings({"storage_arrays": arrs})
        pub = _json.loads(_json.dumps(SV._public_settings(ctl.settings)))["storage_arrays"]
        assert [a["id"] for a in pub] == ["prod@dc1", "prod@dc2"], pub
        pub[1]["user"] = "u2"
        assert ctl.update_settings({"storage_arrays": pub})["ok"]
        sa = ctl.settings["storage_arrays"]
        assert [a["id"] for a in sa] == ["prod@dc1", "prod@dc2"] and [a["password"] for a in sa] == ["p1", "p2"], sa
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert NT.parse_recipients("Kim <a@x.com> b@y.com") == ["a@x.com", "b@y.com"]                     # GGG3
    assert "접근 불가 폴더" not in _read("dashboard.html") and "읽기 오류" in M.capacity_caveats([{"capacity_error_dirs": 1}])   # GGG4
    print("[GGG1~GGG4] OK  비교는 잘린 스캔 깊이만·어레이 이름 가림 안 함·받는 사람 섞인 형식·읽기 오류 문구")


def check_cf_round30_features() -> None:
    """HHH1 '가장 오래된/작은 파일'·'가장 많은 확장자'까지 '가장 큰 파일'로 답함 HHH2 '1,000일'=0일·'1년 6개월'=1년·'한 달'·'90 days' 못 읽음
    HHH3 예측이 크기 기준(apparent/disk)을 섞어 엉뚱한 추세 HHH4 심볼릭 링크 경로의 마운트 판정 HHH5 콜드 이동 매니페스트가 심어 둔 링크를 따라 씀
    HHH6 테스트 데이터 폴더 수 상한 없음 HHH7 중복 찾기·콜드 후보가 읽지 못한 폴더를 숨김 HHH8 숫자 인자(음수 limit 등)·내보내기 형식·폴더 이력 root 끝 '/'·부분 집계 비교 표시·CLI 음수 깊이."""
    from isilon_usage import ask as AK, server as SV, manager as M, systune as ST, coldtier as CT, gentest as GT, dups as DP, db as dbmod
    data = {"scope": "edge", "overall": {"roots": []}, "detail": {
        "top_files": [{"path": "/a", "bytes": 5}], "age": [{"key": "5년+", "bytes": 1, "files": 1}],
        "extensions": [{"key": ".txt", "bytes": 1, "files": 1}], "sizes": [{"key": "<4KB", "bytes": 1, "files": 1}]}}
    for q in ("가장 오래된 파일은?", "가장 작은 파일 알려줘", "가장 많은 파일 확장자는?"):               # HHH1
        assert AK.respond(q, data, {"enabled": False}).get("intent") != "big_files", q
    assert AK.respond("제일 큰 파일은?", data, {"enabled": False}).get("intent") == "big_files"
    P = AK.parse_threshold_days                                                                        # HHH2
    assert (P("1,000일"), P("1년 6개월"), P("한 달 넘게"), P("older than 90 days"), P("일주일"), P("1,5년"), P("2024년 이후")) \
        == (1000, 545, 30, 90, 7, 548, None)
    d = tempfile.mkdtemp(prefix="iu_cf_")
    try:
        data_dir = os.path.join(d, "data")                                                            # HHH3
        M.init_manager(data_dir)
        spec = (("apparent", 130), ("apparent", 130), ("apparent", 130), ("disk", 100), ("disk", 100))
        sids = [M.register_scan(data_dir, root_path="/r", db_path=os.path.join(d, "x%d.db" % i), backend="native",
                                size_mode=mode) for i, (mode, _b) in enumerate(spec)]
        c = dbmod.connect(M.manager_db_path(data_dir))
        for i, (sid, (_m, b)) in enumerate(zip(sids, spec)):
            c.execute("UPDATE scans SET status='done', scanned_bytes=?, finished_at=? WHERE id=?",
                      (b * 1024 ** 3, time.time() - (5 - i) * 86400, sid))
        c.commit()
        f = SV.forecast_capacity(c, M.get_scan(c, sid))
        c.close()
        assert f["points"] == 2 and abs(f["growth_per_day"] or 0) < 1024, f
        real = os.path.join(d, "real")                                                                # HHH4
        os.makedirs(real)
        os.symlink(real, os.path.join(d, "lnk"))
        mounts = [{"mount": "/", "fstype": "ext4", "opts": {}}, {"mount": real, "fstype": "nfs", "opts": {}}]
        assert ST._pick_target(mounts, os.path.join(d, "lnk"))["mount"] == real
        src = _read("coldtier.py")                                                                    # HHH5
        assert '[ -e "$MANIFEST" ] || [ -L "$MANIFEST" ]' in src and "set -C" in src
        ok, why, _plan = GT.validate(os.path.join(d, "gen"), 10 ** 9, 0, 0, 0)
        assert ok is False and "디렉터리" in why, why                                                   # HHH6
        t = os.path.join(d, "t")                                                                       # HHH7
        os.makedirs(os.path.join(t, "deny"))
        os.chmod(os.path.join(t, "deny"), 0)
        r = DP.find_duplicates(t)
        os.chmod(os.path.join(t, "deny"), 0o755)
        assert "unreadable_dirs" in r, r
        if os.geteuid() != 0:
            assert r.get("unreadable_dirs", 0) >= 1, r
        assert '"unreadable_dirs": len(unread)' in _read("dups.py") and '"unreadable_dirs": len(unread)' in src
    finally:
        shutil.rmtree(d, ignore_errors=True)
    sv = _read("server.py")                                                                            # HHH8
    assert 'max(1, min(1000, self._query_int(qs, "limit") or 15))' in sv and 'format 은 csv 또는 json 입니다' in sv
    assert 'root = root.rstrip("/") or "/"' in sv and '"partial": bool(' in sv
    assert '("max_depth", "fold_depth")' in _read("cli.py")
    assert sv.count("if len(buf) >= 2000:") == 2          # HHH9 내보내기 묶어 쓰기(40만 행 CSV 13초→1.2초, 같은 바이트)
    print("[HHH1~HHH9] OK  질의 규칙·기간 해석·예측 기준·실제 경로 마운트·매니페스트 O_EXCL·폴더 상한·읽지 못한 폴더 수·숫자 인자")


def check_cg_round31_regress() -> None:
    """III1 강제 종료된 스캔의 잔여 '진행' 상태로 '지금 스캔'이 영영 막힘 III2 '해외'·'주소'·'해당' 같은 낱말을 기간으로 읽음·
    '90일(90 days)' 두 번 더함 III3 '최근 가장 큰 파일'이 큰 파일 규칙에서 빠짐 III4 옵션 기록 전 스캔은 깊이 제한 없이 비교
    III5 스킴 없는 주소형 어레이 id 노출 III6 받는 사람 따옴표 이름·괄호·중복 III7 growers 음수 깊이."""
    from isilon_usage import ask as AK, server as SV, notify as NT
    dh = _read("dashboard.html")
    assert "const fresh = r.updated_at && (_srvNow() - r.updated_at) < 900;" in dh                         # III1
    P = AK.parse_threshold_days                                                                          # III2
    for q in ("오래된 데이터 상위 5 해외 폴더", "세 주소 폴더", "10 해당 경로 오래된 데이터", "한 주요 폴더", "두 해결 안 된 파일"):
        assert P(q) is None, q
    assert (P("콜드 데이터 90일(90 days)"), P("30일간"), P("1년이상"), P("6개월전"), P("한달넘게")) == (90, 30, 365, 180, 30)
    data = {"scope": "edge", "overall": {"roots": []}, "detail": {"top_files": [{"path": "/a", "bytes": 5}]}}
    assert AK.respond("최근 가장 큰 파일", data, {"enabled": False}).get("intent") == "big_files"          # III3
    assert "opt_max_depth\") is None and sc.get(\"opt_fold_depth\") is None" in _read("server.py")      # III4
    pub = SV._public_settings({"storage_arrays": [{"id": "10.0.0.5/api?token=SECRET", "type": "unity",     # III5
                                                    "url": "10.0.0.5/api?token=SECRET", "password": "p"}]})
    assert "SECRET" not in str(pub), pub
    assert NT.parse_recipients('"a@b.com" <c@d.com>') == ["c@d.com"]                                    # III6
    assert NT.parse_recipients("a@x.com <a@x.com>") == ["a@x.com"]
    assert NT.parse_recipients("Kim <a@x.com> (cc: b@y.com)") == ["a@x.com"]
    assert "max_depth=(None if (_md or 0) < 0 else _md)" in _read("server.py")                          # III7
    print("[III1~III7] OK  잔여 진행 상태·기간 낱말 경계·최근 큰 파일·옛 스캔 비교 깊이·주소형 id·받는 사람·growers 깊이")


def check_ch_round31_flows() -> None:
    """JJJ1 포탈 등록 토큰·중앙 DB 경로 칸이 로그인 전 값으로 남음(없는 탭 id) JJJ2 끝난 pscan 스캔에 '완료되면 채워집니다'
    JJJ3 희소 파일이 '0 (빈 파일)'로 분류 JJJ4 같은 디스크 사용률이 화면마다 다른 식(96.8% vs 12%) JJJ5 설정 체크박스 세로 배치·
    부분 집계 배지가 표를 넓힘·3단계 번호 배지가 제목에 붙음."""
    from isilon_usage import scanner as SC, ask as AK, server as SV
    ph = _read("portal.html")
    assert 'id==="np-security"' not in ph and 'id==="np-account" || id==="np-monitor"' in ph                  # JJJ1
    assert "updateLoginUI(); try{ pLoadUpgradeCfg(); }catch(e){}" in ph
    assert "pscan(빠른 멀티프로세스 스캔)" in AK._detail_unavailable({"scope": "edge", "detail_note": "pscan"})   # JJJ2
    assert "function _repEmptyMsg()" in _read("dashboard.html")
    class _St:                                                                                          # JJJ3
        st_size, st_blocks, st_mtime, st_atime, st_uid = 100 * 1024 ** 2, 0, time.time(), time.time(), 0
    l_size: dict = {}
    SC._local_accum(time.time(), {}, {}, {}, {}, l_size, "sparse.img", _St(), 0)
    assert "0 (빈 파일)" not in l_size and "100MB~1GB" in l_size, l_size
    d = tempfile.mkdtemp(prefix="iu_ch_")                                                              # JJJ4
    try:
        info = SV._dir_disk_free(d)
        v = os.statvfs(d)
        if info is not None:
            assert info["used_bytes"] == v.f_blocks * v.f_frsize - v.f_bfree * v.f_frsize, info
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "used = max(0, total - v.f_bfree * v.f_frsize)" in _read("server.py")
    th = _read("theme.css")                                                                             # JJJ5
    assert '#settingsCard label:has(> input[type=checkbox])' in th and "summary.step-title .stepno" in th
    assert 'month: "numeric", day: "numeric", hour: "2-digit"' in _read("dashboard.html")
    print("[JJJ1~JJJ5] OK  포탈 설정 칸 다시 채움·pscan 리포트 안내·희소 파일 크기 분류·디스크 사용률 한 식·설정/단계 배치")


def check_ci_round32_regress() -> None:
    """KKK1 31회차 기간 규칙이 '3개월된'·'30일지난'·'1년안에' 같은 붙여 쓴 표현을 못 읽음(1년 기본으로 답함)
    KKK2 포탈 설정 탭을 오가면 입력 중(저장 안 한) 중앙 DB 경로·백업 폴더·감시 폴더가 지워짐."""
    from isilon_usage import ask as AK
    P = AK.parse_threshold_days
    good = {"1년된 파일": 365, "3개월된 파일": 90, "7일지난": 7, "30일지난": 30, "1년경과": 365, "7일묵은": 7, "1년미만": 365,
            "1년안에": 365, "7일뒤": 7, "1년입니다": 365, "30일로": 30, "30일마다": 30, "7일단위": 7, "7일기준": 7,
            "30일정도": 30, "2주간": 14, "1년도 안 된": 365, "한달넘게": 30, "1년 6개월": 545, "90일(90 days)": 90}
    bad = ["상위 5 해외 폴더", "세 주소 폴더", "10 해당 경로", "한 주요 폴더", "두 해결 안 된 파일", "열 주변", "2024년 이후", "3 일부 폴더"]
    wrong = [(k, P(k), v) for k, v in good.items() if P(k) != v] + [(k, P(k)) for k in bad if P(k) is not None]
    assert not wrong, wrong
    ph = _read("portal.html")                                                                             # KKK2
    for fid in ("ugWatchDir", "dbDir", "bkDir"):
        assert '_fillIdle($("%s")' % fid in ph and 'delete $("%s").dataset.dirty' % fid in ph, fid
    print("[KKK1~KKK2] OK  붙여 쓴 기간 표현·단위 뒤 낱말 차단·포탈 탭 전환 시 입력 보존")


def check_cj_round32_portal() -> None:
    """LLL1 업그레이드 설정 저장이 거부돼도 '저장됨' LLL2 업그레이드 주소의 계정 비밀번호가 상태 응답(로그인 없이)에 오류 문구로 노출·
    계정 든 주소는 아예 동작 안 함 LLL3 노드 비밀번호 푸시 3자 허용 LLL4 다른 곳에서 세션이 끊겨도 '로그아웃(29분)' 유지 LLL5 새로고침하면
    늘 현황·숨긴 현황이 열림 LLL6 빈 비교 경로가 이전 경로 재조회 LLL7 이미 최신 엣지를 '실패'로 LLL8 LLM 키 삭제 안내 없음."""
    import json as _json
    from isilon_usage import netutil as NU, upgrade as U, portal as P, server as SV
    ph = _read("portal.html")
    assert 'if(!j || j.ok===false){ $("nuMsg").textContent="✗ "' in ph                                   # LLL1
    sv = NU.secret_values("https://user:s3cr3t@mirror.example.com/rel?token=abc123", "tokXYZ")             # LLL2
    red = NU.redact_values({"e": "nonnumeric port: 's3cr3t@mirror.example.com'", "l": ["abc123 tokXYZ"]}, sv)
    assert "s3cr3t" not in _json.dumps(red) and "abc123" not in _json.dumps(red), red
    r = U._auth_request("https://user:s3cr3t@m.example/v.json", None)
    assert r.full_url == "https://m.example/v.json" and r.headers.get("Authorization", "").startswith("Basic ")
    d = tempfile.mkdtemp(prefix="iu_cj_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        pc.settings["upgrade_url"] = "https://user:s3cr3t@mirror.example.com/rel"
        pc._upg_state["check_error"] = "버전 정보 조회 실패: nonnumeric port: 's3cr3t@mirror.example.com'"
        assert "s3cr3t" not in _json.dumps(pc.upgrade_status())
        assert pc.upsert_node({"id": "n1", "url": "http://e1:8765", "token": "t"})["ok"]                  # LLL3
        r = pc.set_node_password(["n1"], "abc")
        assert r.get("ok") is False and "4자" in r.get("reason", ""), r
        ctl = SV.ScanController(os.path.join(d, "edge"))
        assert ctl.set_op_password("abc").get("ok") is False
        pc.set_ask_llm({"ask_llm_endpoint": "http://l1:1/v1", "ask_llm_key": "K"})                         # LLL8
        assert pc.set_ask_llm({"ask_llm_endpoint": "http://l2:1/v1"}).get("ask_llm_key_dropped") is True
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert 'if(_opToken() && !j401.need_setup){ _opExpiry=0;' in ph                                       # LLL4
    assert 'history.replaceState(null,"","#"+v)' in ph and "if(cur && cur!==\"nodes\" && hset.has(cur))" in ph   # LLL5
    assert '"비교할 경로를 입력하세요."' in ph                                                                # LLL6
    assert 'if(s==="same")' in ph and '"same"' in _read("portal.py")                                      # LLL7
    print("[LLL1~LLL8] OK  저장 실패 표시·업그레이드 주소 비밀 가림/Basic 인증·비밀번호 4자·세션 끊김·화면 기억·빈 비교·이미 최신·키 삭제 안내")


def check_ck_round32_tools() -> None:
    """MMM1 처리량이 다른 스캔 표본과의 시간차로 적분돼 부풀음 MMM2 시범 탐색 두 번 누르면 진행 표시를 잃음 MMM3 예약 추가 입력줄 값이
    설정 저장 전체를 막음 MMM4 대상 분석 사용자 중지를 '시간 초과'로 MMM5 조사·탐색 오류 코드 MMM6 질의 'N개' 무시 MMM7 콜드 0일·중복 최소 0이
    말없이 기본값 MMM8 어레이 주소 형식·이름 중복."""
    import sqlite3
    from isilon_usage import troubleshoot as TS, analyzer as AN, ask as AK
    d = tempfile.mkdtemp(prefix="iu_ck_")
    try:
        dbp = TS.metrics_db_path(d)                                                                    # MMM1
        c = TS._metrics_conn(d)
        c.execute("DELETE FROM rate_samples")
        now = time.time()
        for ts, sid in ((now - 50, 2), (now - 10, 3)):          # 서로 다른 스캔의 첫 표본만 — 적분할 구간이 없다
            c.execute("INSERT INTO rate_samples(ts, rd, rf, rb, scan_id) VALUES(?,?,?,?,?)", (ts, 1.0, 25000.0, 1e8, sid))
        c.execute("INSERT INTO rate_samples(ts, rd, rf, rb, scan_id) VALUES(?,?,?,?,?)", (now - 5, 1.0, 1000.0, 1e6, 3))
        c.commit()
        c.close()
        b = TS.throughput_buckets(d, 3600)
        files = sum(x["files"] for x in b)
        assert abs(files - 5000) < 1, (files, b)                  # 같은 스캔 3 의 5초 × 1000/s 만
        assert os.path.exists(dbp)
        stop = threading.Event()                                                                       # MMM4
        stop.set()
        r = AN.analyze_depth(d, stop_event=stop)
        assert r.get("stopped") is True, r
    finally:
        shutil.rmtree(d, ignore_errors=True)
    dh = _read("dashboard.html")
    assert "if (_benchTimer) return;" in dh and "!/이미/.test(st.error" in dh                              # MMM2
    assert "if (/^(sch|arr)/.test(el.id)) continue;" in dh                                              # MMM3
    assert '(r.stopped ? "사용자가 중지함" : "시간 초과로 중단")' in dh                                    # MMM4
    assert "을(를) 자동 시작했습니다" in dh and "폴더가 아니거나 없는 경로입니다" in dh                          # MMM5
    data = {"scope": "edge", "overall": {"roots": []},
            "detail": {"top_files": [{"path": "/f%d" % i, "bytes": 100 - i} for i in range(20)]}}
    assert AK.respond("가장 큰 파일 상위 3개", data, {"enabled": False})["answer"].count("\n") == 3        # MMM6
    assert 'const days = _dv === "" ? 365' in dh and 'const min = _mv === "" ? 4096' in dh               # MMM7
    assert "어레이 주소는 http:// 또는 https:// 로 시작해야 합니다" in dh                                    # MMM8
    print("[MMM1~MMM8] OK  처리량 스캔별 적분·시범 탐색 중복·예약 입력줄 제외·중지 표시·문구·질의 N개·0 값·어레이 검사")


def check_cl_round33_regress() -> None:
    """NNN1 '6개월'·'5개년'의 '개'를 개수로 읽어 목록이 잘림 NNN2 비밀 조각 가리기가 너무 넓어(사용자 이름만·일반 쿼리 값·짧은 토큰)
    'main'·'true'·'admin'·숫자까지 로그에서 지움 NNN3 시범 탐색 확인이 계속 실패하면 버튼이 영영 꺼짐."""
    from isilon_usage import ask as AK, netutil as NU
    for q, n in (("6개월 넘은 큰 파일", 10), ("3개월 이상 안 쓴 큰 파일", 10), ("5개년 큰 폴더", 10), ("큰 폴더 3개", 3)):   # NNN1
        assert AK._top_n(q) == n, (q, AK._top_n(q))
    assert NU.secret_values("https://x@host/a") == [] and NU.secret_values("12345") == []                       # NNN2
    sv = NU.secret_values("https://user:s3cr3t@m.example/r?token=abc123&ref=main&a=true")
    out = NU.redact_values("error at main true admin s3cr3t abc123", sv)
    assert out == "error at main true admin *** ***", out
    assert "if (tries > 180) { clearInterval(_benchTimer);" in _read("dashboard.html")                           # NNN3
    # NNN4 비밀번호에 '/'·'#'·공백 든 주소가 가려지지 않고 로그인 없이 노출(33회차 보안 점검)
    for u in ("https://admin:SECRET_PW/xx@10.0.0.20:8080", "https://admin:SECRET_PW#x@host:8080",
              "https://u:SECRET_PW@bad host/?t=Q", "https://10.0.0.20:80 80/?token=SECRET_PW"):
        assert "SECRET_PW" not in NU.mask_url_secret(u), u
        assert NU.keep_masked(NU.mask_url_secret(u), u) == u
    assert "SECRET_PW" not in str(NU.redact_values("nonnumeric port: 'SECRET_PW/xx'",
                                                     NU.secret_values("https://admin:SECRET_PW/xx@h:1")))
    from isilon_usage import server as SV
    pub = SV._public_settings({"isilon_url": "https://admin:SECRET_PW/xx@10.0.0.20:8080"})
    assert "SECRET_PW" not in str(pub), pub
    assert '"isilon": netutil.redact_urls(c.get("isilon")' in _read("portal.py")
    print("[NNN1~NNN4] OK  기간의 '개' 제외·비밀 조각 범위·시범 탐색 버튼 복구·특수문자 비밀번호 주소 가림")


def _mark_many(x, code):
    from isilon_usage import settings as SE
    for _ in range(30):
        SE.mark_initialized(x, code, ("settings.json",))


SECTIONS = [check_e1_non_utf8_root, check_e2_stagger_kept, check_e3_pscan_partial_exit, check_e4_eta_active_time,
            check_p1_p2_p4_portal, check_p3_p6_p7_d1_d5_static, check_d2_ko_count, check_p5_d3_d4_http,
            check_f1_discovery_eta, check_r1_to_r5_edge,
            check_s1_sizing_window, check_c2_prune_keeps_active, check_c3_c4_upgrade_bundle, check_c5_dst_schedule,
            check_c6_c7_cli_inputs, check_q1_q2_replica_compare, check_q3_nested_roots, check_q4_disabled_ping,
            check_t1_t5_portal_round3, check_u1_u3_ui_round3,
            check_v1_v6_edge_round3, check_w1_w2_round4, check_x1_x8_settings_round4,
            check_y1_y6_integrations_round5, check_z1_z8_portal_ui_round5,
            check_aa_round6, check_ab_edge_views_round6, check_ac_round7,
            check_ad_portal_round8, check_ae_regress_round8, check_af_multinode_round9,
            check_ag_edge_flow_round9, check_ah_clock_rewind_round10,
            check_ai_edge_round10, check_aj_adverse_fs_round11,
            check_ak_round11_misc, check_al_tools_round12, check_am_remote_round13,
            check_an_fuzz_round13, check_ao_regress_round14,
            check_ap_explore_round14, check_aq_live_snapshot,
            check_ar_crash_round15, check_as_regress_round16,
            check_at_livesnap_round16, check_au_regress_round17,
            check_av_round17_flow, check_aw_round18_regress,
            check_ax_round18_data, check_ay_round18_portal_delete,
            check_az_round19_regress, check_ba_round19_portal_edge,
            check_bb_round20_regress, check_bc_round20_upgrade,
            check_bd_round20_stress, check_be_round21_sec_regress,
            check_bf_round21_modules, check_bg_round22_regress, check_bh_round22_fuzz, check_bi_round22_browser, check_bj_round23_regress, check_bk_round23_portal, check_bl_round23_engine, check_bm_round24_regress,
            check_bn_round24_consistency, check_bo_round24_perf, check_bp_round25_regress, check_bq_round25_schedule, check_br_round25_client,
            check_bs_round26_regress, check_bt_round26_cli, check_bu_round27_regress,
            check_bv_round27_security, check_bw_round26_portal_ui,
            check_bx_round28_regress, check_by_round28_install,
            check_bz_round28_dashboard, check_ca_round29_regress,
            check_cb_round29_numbers, check_cc_round29_flows, check_cd_running_block, check_ce_round30_regress,
            check_cf_round30_features, check_cg_round31_regress, check_ch_round31_flows, check_ci_round32_regress, check_cj_round32_portal, check_ck_round32_tools, check_cl_round33_regress]


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

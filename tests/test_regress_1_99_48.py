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
        left = [n for n in os.listdir(code) if n != "isilon_usage"]
        assert not left, left
        os.makedirs(os.path.join(code, "isilon_usage.bad.123"))                                    # 옛 버전이 남긴 것
        os.makedirs(os.path.join(code, "isilon_usage.new.124"))
        conflict = dict(members, **{"zz": b"x", "zz/inner": b"y"})                                 # EE2 쓰기 실패
        try:
            U.apply_package(conflict, code)
            raise AssertionError("쓰기 실패가 성공으로")
        except OSError:
            pass
        left = [n for n in os.listdir(code) if n != "isilon_usage"]
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
            check_bb_round20_regress, check_bc_round20_upgrade]


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

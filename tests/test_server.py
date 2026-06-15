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


def main() -> int:
    _check_insecure_warning()
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
        up = c.post("/api/settings", {"settings": {"top_n": 3, "scan_workers": 2, "check_readonly": False}})
        assert up["ok"] and up["settings"]["top_n"] == 3 and up["settings"]["scan_workers"] == 2
        assert up["settings"]["check_readonly"] is False, up["settings"]

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

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

from isilon_usage.server import serve  # noqa: E402


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

    def post(self, path, data):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            return json.load(urllib.request.urlopen(req, timeout=10))
        except urllib.error.HTTPError as e:
            return json.loads(e.read())


def _wait_done(client, scan_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/status?scan={scan_id}")
        if d.get("ok") and d["run"]["status"] in ("done", "error", "paused"):
            return d
        time.sleep(0.2)
    raise AssertionError("스캔이 시간 내에 끝나지 않음")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="isilon_srv_")
    root = os.path.join(tmp, "tree")
    _make_tree(root)
    data_dir = os.path.join(tmp, "data")

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

        # 경로 제한: 허용 밖 거부
        bad = c.post("/api/scan/start", {"path": "/etc"})
        assert not bad["ok"], bad

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

        # 스캔 2 → diff
        r2 = c.post("/api/scan/start", {"path": root})
        _wait_done(c, r2["scan_id"])
        df = c.get(f"/api/diff?base={s1}&target={r2['scan_id']}")
        assert df["ok"] and df["total_delta"] == 0, df

        # prune keep_per_root=1 → 오래된 s1 삭제
        pr = c.post("/api/prune", {"keep_per_root": 1})
        assert s1 in pr["deleted"], pr
        remaining = [s["id"] for s in c.get("/api/scans")["scans"]]
        assert s1 not in remaining and r2["scan_id"] in remaining, remaining

        # 삭제
        dl = c.post("/api/scan/delete", {"scan_id": r2["scan_id"]})
        assert dl["ok"], dl
        assert c.get("/api/scans")["scans"] == []

        print("[server] OK  모든 API 통과")
        print("모든 테스트 통과 ✅")
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

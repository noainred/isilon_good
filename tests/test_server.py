"""웹 서버 API 통합 테스트.

serve() 로 실제 HTTP 서버를 띄우고 주요 엔드포인트를 호출해 검증한다:
스캔 시작/상태, 설정 저장, 폴더 탐색, 검색, 오류, 비교(diff), 내보내기,
정리(prune)/삭제, 경로 제한 거부.
"""

from __future__ import annotations

import json
import os
import sys
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
        s1 = r1["scan_id"]
        d1 = _wait_done(c, s1)
        assert d1["run"]["status"] == "done"
        assert d1["run"]["total_files"] == 4, d1["run"]["total_files"]

        # 드릴다운: 루트 → 자식
        ch = c.get(f"/api/children?scan={s1}")
        assert ch["children"], ch
        root_id = ch["children"][0]["id"]
        kids = c.get(f"/api/children?scan={s1}&parent={root_id}")
        names = {k["name"] for k in kids["children"]}
        assert {"sub1", "sub2"} <= names, names

        # 검색 / 오류
        sr = c.get(f"/api/search?scan={s1}&q=deep")
        assert any("deep" in r["path"] for r in sr["results"]), sr
        assert c.get(f"/api/errors?scan={s1}")["ok"]

        # 내보내기 CSV
        csv = c.get_raw(f"/api/export?scan={s1}&format=csv").decode("utf-8")
        assert "path,depth" in csv and root in csv

        # 설정 저장 라운드트립
        up = c.post("/api/settings", {"settings": {"top_n": 3, "scan_workers": 2}})
        assert up["ok"] and up["settings"]["top_n"] == 3 and up["settings"]["scan_workers"] == 2

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

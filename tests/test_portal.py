"""글로벌 포탈(Phase 2) 통합 테스트.

진짜 엣지 서버(serve, api_token)를 띄워 작은 트리를 스캔한 뒤, PortalController 로
노드를 등록/연결테스트/동기화(폴링+복제)하고 글로벌 롤업과 복제본을 검증한다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage.server import serve  # noqa: E402
from isilon_usage import portal as portalmod  # noqa: E402


def _make_tree(root: str) -> None:
    os.makedirs(os.path.join(root, "sub1"), exist_ok=True)
    for rel, n in [("a.bin", 4000), ("sub1/b.bin", 5000)]:
        with open(os.path.join(root, rel), "wb") as fh:
            fh.write(b"\0" * n)


def _post(base, path, data):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=10))


def _wait_done(base, sid, timeout=15):
    import urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            d = json.load(urllib.request.urlopen(base + "/api/status?scan=%d" % sid, timeout=5))
        except urllib.error.HTTPError:
            time.sleep(0.2)
            continue
        if d.get("ok") and d["run"]["status"] in ("done", "error"):
            return d
        time.sleep(0.2)
    raise AssertionError("스캔 미완료")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="portal_")
    root = os.path.join(tmp, "tree")
    _make_tree(root)

    # 1) 엣지 서버(토큰 보호) + 스캔
    edge_data = os.path.join(tmp, "edge")
    edge = serve(edge_data, host="127.0.0.1", port=0,
                 initial_settings={"mount_bases": [tmp], "api_token": "tok"}, enable_scan=True)
    eport = edge.server_address[1]
    threading.Thread(target=edge.serve_forever, daemon=True).start()
    ebase = "http://127.0.0.1:%d" % eport
    sid = _post(ebase, "/api/scan/start", {"path": root})["scan_id"]
    _wait_done(ebase, sid)

    try:
        # 2) 포탈 컨트롤러
        portal_data = os.path.join(tmp, "hq")
        pc = portalmod.PortalController(portal_data)

        # 3) 연결 테스트: 잘못된 토큰 → 실패, 올바른 토큰 → 성공
        bad = pc.test_node({"url": ebase, "token": "WRONG"})
        assert not bad["ok"], bad
        good = pc.test_node({"url": ebase, "token": "tok"})
        assert good["ok"] and good["storages"] >= 1 and good["used_bytes"] > 0, good

        # 4) 노드 등록(토큰 마스킹 확인)
        up = pc.upsert_node({"id": "dc-test", "region": "아시아/서울",
                             "url": ebase, "token": "tok", "interval_minutes": 1})
        assert up["ok"] and up["node"]["token"] == "" and up["node"]["token_set"] is True, up
        # 반복주기 back-compat: interval_minutes 만 줘도 unit=minute/every 로 채워짐
        assert up["node"]["unit"] == "minute" and up["node"]["every"] == 1, up["node"]

        # 5) 동기화(폴링 + 복제) 동기 실행
        pc._sync_one("dc-test", True)

        # 6) 글로벌 롤업
        ov = pc.overview()
        assert ov["totals"]["nodes_online"] == 1, ov["totals"]
        assert ov["totals"]["storages"] >= 1 and ov["totals"]["used_bytes"] > 0, ov["totals"]
        node = ov["nodes"][0]
        assert node["online"] and node["region"] == "아시아/서울", node

        # 7) 복제본(완료 DB + meta.json) 존재
        rep = os.path.join(portal_data, "replicas", "dc-test")
        assert os.path.exists(os.path.join(rep, "meta.json")), rep
        dbs = [f for f in os.listdir(os.path.join(rep, "scans")) if f.endswith(".db")]
        assert dbs, "복제된 per-run DB 없음"

        # 7b) 경로 비교(Cross-DC): 복제본에서 같은 경로 조회 + 디렉터리 매트릭스
        cmp = pc.compare_path(root)
        assert cmp["rows"] and cmp["rows"][0]["found"] and cmp["rows"][0]["total_bytes"] > 0, cmp
        mat = pc.compare_matrix(root)
        assert "sub1" in [c["name"] for c in mat["children"]], mat

        # 7c) 경로 별칭: 로컬 root 를 논리 /L 로 매핑 → 논리 경로로 비교/매트릭스
        pc.upsert_node({"id": "dc-test", "url": ebase, "region": "아시아/서울",
                        "alias_local": root, "alias_logical": "/L"})
        cmpa = pc.compare_path("/L")
        assert cmpa["rows"][0]["found"] and cmpa["rows"][0]["total_bytes"] > 0, cmpa
        assert "sub1" in [c["name"] for c in pc.compare_matrix("/L")["children"]]
        # 별칭 없이 논리 경로로 조회하면 못 찾음(절대경로 불일치)
        pc.upsert_node({"id": "dc-test", "url": ebase, "region": "아시아/서울"})
        assert not pc.compare_path("/L")["rows"][0]["found"]

        # 8) 토큰 비우고 저장 → 기존 토큰 유지(동기화 여전히 성공)
        pc.upsert_node({"id": "dc-test", "url": ebase, "token": "", "region": "아시아/서울"})
        pc._sync_one("dc-test", False)
        assert pc.overview()["nodes"][0]["online"], "토큰 보존 실패로 오프라인"

        # 9) 삭제
        assert pc.delete_node("dc-test")["ok"]
        assert pc.overview()["totals"]["nodes_total"] == 0

        print("[portal] OK  연결테스트·등록·폴링·복제·롤업·삭제 통과")
        print("모든 테스트 통과 ✅")
        return 0
    finally:
        edge.shutdown()
        edge.server_close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

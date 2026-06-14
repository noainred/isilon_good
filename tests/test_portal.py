"""글로벌 포탈(Phase 2) 통합 테스트.

진짜 엣지 서버(serve, api_token)를 띄워 작은 트리를 스캔한 뒤, PortalController 로
노드를 등록/연결테스트/동기화(폴링+복제)하고 글로벌 롤업과 복제본을 검증한다.
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


def _test_save_nodes_concurrent() -> None:
    """병렬 저장 레이스 회귀: 여러 스레드가 save_nodes 를 동시에 호출해도 에러/임시파일 누수 없음."""
    import shutil
    d = tempfile.mkdtemp(prefix="portal_race_")
    try:
        nodes = [{"id": "n%d" % i, "url": "http://x", "last_poll": 0} for i in range(20)]
        errors = []

        def worker():
            for _ in range(40):
                try:
                    portalmod.save_nodes(d, nodes)
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))

        ts = [threading.Thread(target=worker) for _ in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors, errors[:3]
        assert not [f for f in os.listdir(d) if ".tmp" in f], "임시파일 누수"
        data = json.load(open(os.path.join(d, "portal_nodes.json"), encoding="utf-8"))
        assert len(data["nodes"]) == 20
        print("[portal] save_nodes 동시성 OK (10스레드×40회, 에러/임시파일 누수 0)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _test_ping_history() -> None:
    """인프라 체크(서버 Ping 이력): 노드별 시계열·중앙값·다운샘플·지역정렬 회귀."""
    import shutil

    from isilon_usage import db as dbmod
    d = tempfile.mkdtemp(prefix="portal_ping_")
    try:
        pc = portalmod.PortalController(d)
        pc.nodes = [{"id": "seoul-01", "region": "OC2", "url": "http://x"},
                    {"id": "fra-01", "region": "DMZ", "url": "http://y"}]
        pc._init_ping_db()
        # 빈 이력도 ok=True (아직 표본 없음)
        empty = pc.ping_history(604800)
        assert empty["ok"] and empty["nodes"] == [], empty

        now = int(time.time())
        rows = []
        for i in range(600):                       # 10시간치, 1분 간격
            ts = now - i * 60
            rows.append((ts, "seoul-01", 12.0 + (8 if i % 50 == 0 else 0), 1))
            rows.append((ts, "fra-01", 95.0 + (60 if i % 30 == 0 else 0), 1))
        conn = dbmod.connect(portalmod.ping_history_path(d))
        conn.executemany(
            "INSERT INTO ping_samples(ts,node_id,latency_ms,up) VALUES(?,?,?,?)", rows)
        conn.commit()
        conn.close()

        r = pc.ping_history(86400, max_points=240)
        assert r["ok"] and r["range"] == 86400 and r["bucket"] >= 60, r
        # 지역 오름차순 정렬(DMZ < OC2)
        assert [n["region"] for n in r["nodes"]] == ["DMZ", "OC2"], r["nodes"]
        for n in r["nodes"]:
            assert n["median"] is not None and n["series"], n
            # 다운샘플: max_points(240) 이하
            assert len(n["series"]) <= 240, len(n["series"])
            assert all({"t", "ms", "up"} <= set(p) for p in n["series"]), n["series"][0]
        med = {n["id"]: n["median"] for n in r["nodes"]}
        assert 11 <= med["seoul-01"] <= 14 and 90 <= med["fra-01"] <= 100, med
        # 잘못된 range(0/None)는 하한 보정
        assert pc.ping_history(0)["range"] == 86400
        print("[portal] ping_history OK (지역정렬·중앙값·다운샘플·빈이력)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    _test_save_nodes_concurrent()
    _test_ping_history()
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
        # 6b) 용량(fs 사용/전체) + 진행 요약 필드 노출
        for k in ("fs_total_bytes", "fs_used_bytes", "fs_used_pct", "running"):
            assert k in node, (k, node)
        assert "fs_total_bytes" in ov["totals"] and ov["totals"]["fs_total_bytes"] >= 0, ov["totals"]

        # 6c) 노드 응답시간(핑) — 로컬 엣지라 빠르게 응답
        pg = pc.ping_nodes()
        assert pg["ok"] and pg["results"], pg
        pr = pg["results"][0]
        assert pr["id"] == "dc-test" and pr["online"] and pr["latency_ms"] is not None, pr

        # 6d) 원격 스캔 시작(마지막 경로) — 방어 분기 + 라이브 성공
        assert not pc.node_scan("nope")["ok"]                # 없는 노드
        ns = pc.node_scan("dc-test")                         # 엣지에 op 비번 없음 → 시작됨
        assert ns["ok"] and ns.get("scan_id") and ns.get("path"), ns
        _wait_done(ebase, ns["scan_id"])                     # 재시작 스캔도 완료까지 대기

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

        # 10) 원격 자동 구성(A): 스크립트 생성 + 노드 자동 등록 + 에이전트 번들 + SSH 옵션
        pv = pc.provision_plan({"host": "10.9.9.9", "port": 8765, "path": "/mnt/hadoop",
                                "region": "EU", "name": "auto1", "hq_base": "http://hq:8800"})
        assert pv["ok"] and pv["registered"] and len(pv["token"]) == 32, pv
        assert pv["script"].startswith("#!/usr/bin/env bash") and "agent-bundle" in pv["script"], pv
        assert any(n["id"] == "auto1" for n in pc.list_nodes()["nodes"]), "노드 자동 등록 실패"
        names = tarfile.open(fileobj=io.BytesIO(portalmod.agent_bundle_bytes()),
                             mode="r:gz").getnames()
        assert "isilon_usage/portal.py" in names and "isilon_usage/__init__.py" in names, names
        # SSH 푸시는 옵션 — ssh 없으면 스크립트를 동봉해 우아하게 실패해야 함
        ssh = pc.provision_ssh({"host": "10.9.9.9", "ssh_user": "root",
                                "ssh_password": "x", "name": "auto1"})
        assert "script" in ssh and isinstance(ssh["ok"], bool), ssh
        assert pc.delete_node("auto1")["ok"]

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

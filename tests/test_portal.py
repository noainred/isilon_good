"""글로벌 포탈(Phase 2) 통합 테스트.

진짜 엣지 서버(serve, api_token)를 띄워 작은 트리를 스캔한 뒤, PortalController 로
노드를 등록/연결테스트/동기화(폴링+복제)하고 글로벌 롤업과 복제본을 검증한다.
"""

from __future__ import annotations

import io
import json
import os
import shutil
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


def _test_csv_import() -> None:
    """CSV 일괄 노드 등록: 추가/수정/오류 집계 + url 보정 + 헤더 유무 처리."""
    d = tempfile.mkdtemp(prefix="portal_csv_")
    try:
        pc = portalmod.PortalController(d)
        csv = ("id,url,region,token\n"
               "seoul-01,http://10.1.1.5:8765,아시아/서울,TOK1\n"
               "tokyo-01,10.2.2.6:8765,아시아/도쿄,\n"     # url 자동 http:// , 토큰 빈값
               "bad-row,,지역,\n")                          # url 누락 → 오류 1건
        r = pc.import_nodes_csv(csv)
        assert r["ok"] and r["added"] == 2, r
        assert len(r["errors"]) == 1 and r["errors"][0]["line"] == 4, r
        ids = {n["id"] for n in pc.nodes}
        assert {"seoul-01", "tokyo-01"} <= ids, ids
        tok = next(n for n in pc.nodes if n["id"] == "tokyo-01")
        assert tok["url"] == "http://10.2.2.6:8765", tok["url"]   # 스킴 자동 보정
        # 재import = 수정(같은 id)
        r2 = pc.import_nodes_csv("id,url,region\nseoul-01,http://10.1.1.9:8765,부산\n")
        assert r2["updated"] == 1 and r2["added"] == 0, r2
        # 한글/별칭 헤더 + 헤더 없는 고정순서
        r3 = pc.import_nodes_csv("이름,주소,지역\nosaka,http://10.3.3.3:8765,간사이\n")
        assert r3["added"] == 1, r3
        r4 = pc.import_nodes_csv("nagoya,http://10.4.4.4:8765,주부\n")   # 헤더 없음
        assert r4["added"] == 1, r4
        assert pc.import_nodes_csv("")["ok"] is False                    # 빈 입력
        print("[csv-import] OK  added/updated/오류 집계 · url 보정 · 한글·헤더없음 처리")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    _test_save_nodes_concurrent()
    _test_ping_history()
    _test_csv_import()
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

        # 6b-2) 자연어 질의응답(규칙 기반) — 노드 롤업을 공통 형태로 정규화해 답
        aq = pc.ask("전체 용량 얼마야?")
        assert aq["ok"] and aq["intent"] == "total" and aq["source"] == "rules", aq
        # 포탈엔 디렉터리 상세가 없으므로 '노드(엣지)에서' 안내
        ad = pc.ask("제일 큰 디렉터리?")
        assert ad["intent"] == "top_dirs" and "노드" in ad["answer"], ad

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

        # 6e) 포탈에서 엣지 작업 비밀번호 설정/해제(api_token 인증으로 푸시)
        assert not edge.controller.op_required()
        sp = pc.set_node_password(["dc-test"], "edgepw")
        assert sp["ok"] and sp["ok_count"] == 1, sp
        assert edge.controller.op_required()                 # 엣지에 비번 설정됨
        cp = pc.set_node_password(["dc-test"], "", clear=True)
        assert cp["ok"] and cp["ok_count"] == 1, cp
        assert not edge.controller.op_required()             # 해제됨

        # 6f) 포탈 제목(브랜딩) 설정 → auth_status/upgrade_config 에 노출, 비우면 기본 폴백
        assert pc.set_title("테스트 관제센터", "DC-OC2")["ok"]
        a = pc.auth_status()
        assert a["portal_title"] == "테스트 관제센터" and a["portal_subtitle"] == "DC-OC2", a
        assert pc.upgrade_config()["portal_title"] == "테스트 관제센터"
        assert pc.set_title("", "")["ok"] and pc.auth_status()["portal_title"] == ""

        # 6g) 상단 메뉴 숨김 — 화이트리스트(dash/netmon/compare)만, 'nodes'는 숨길 수 없음
        assert pc.set_nav_hidden(["compare", "netmon", "nodes", "bad"])["nav_hidden"] == ["compare", "netmon"]
        assert pc.auth_status()["nav_hidden"] == ["compare", "netmon"]
        assert pc.set_nav_hidden([])["nav_hidden"] == []

        # 6h) 외부 사용량 API(/api/portal/usage) — 안정 스키마 + 토큰 게이트
        ux = pc.usage_export()
        assert ux["ok"] and ux["schema"] == "isilon_usage.usage/v1", ux
        assert ux["totals"]["nodes_total"] >= 1 and ux["totals"]["scanned_bytes"] > 0, ux["totals"]
        assert ux["nodes"] and ux["nodes"][0]["roots"] and ux["nodes"][0]["roots"][0]["root_path"], ux["nodes"]
        assert pc.export_token_ok("anything")                       # 미설정이면 공개
        pc.set_export_token("SECRET")
        assert (not pc.export_token_ok("wrong")) and pc.export_token_ok("SECRET")
        assert pc.set_export_token("")["export_token_set"] is False and pc.export_token_ok("x")  # 해제

        # 6i) 로그인 세션 유지 시간 · 업데이트 팝업 표시(set_session_prefs) — 기본/범위/반영
        assert pc.auth_status()["op_ttl_minutes"] == 30          # 기본 30분
        assert pc.auth_status()["show_update_popup"] is False     # 기본 끔
        r = pc.set_session_prefs({"op_ttl_minutes": 120, "show_update_popup": True})
        assert r["ok"] and r["op_ttl_minutes"] == 120 and r["show_update_popup"] is True
        assert pc.auth_status()["op_ttl_minutes"] == 120 and pc.auth_status()["show_update_popup"] is True
        assert pc.set_session_prefs({"op_ttl_minutes": 0})["op_ttl_minutes"] == 1        # 하한 클램프
        assert pc.set_session_prefs({"op_ttl_minutes": 99999})["op_ttl_minutes"] == 10080  # 상한 클램프
        assert pc.set_session_prefs({"op_ttl_minutes": "x"})["op_ttl_minutes"] == 30        # 잘못된 값 → 기본
        # 세션 prefs 저장이 다른 설정(감시 폴더)을 건드리지 않음
        pc.set_upgrade_watch("/tmp/iso_watch")
        pc.set_session_prefs({"show_update_popup": False})
        assert pc.settings.get("upgrade_watch_dir") == "/tmp/iso_watch"
        # 콜러블 ttl 이 분→초로 반영(120분 = 7200초)
        pc.set_session_prefs({"op_ttl_minutes": 120})
        assert int(pc._auth.ttl) == 7200

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
        # 서비스/데이터 경로는 install_edge.sh 와 통일(isilon-edge, /data/isilon_edge_data) — 401/중복 서비스 방지
        assert "isilon-edge.service" in pv["script"] and "/data/isilon_edge_data" in pv["script"], pv
        # 새 유닛은 isilon-edge 로만 '생성'해야 한다(옛 isilon_usage 유닛을 만들면 401/중복).
        assert "tee /etc/systemd/system/isilon_usage.service" not in pv["script"], \
            "옛 서비스명(isilon_usage)으로 유닛을 만들면 안 됨"
        # 설치 전: 기존 프로세스·서비스 중단 단계(포트 충돌 방지) — 구버전 isilon_usage 정리 포함
        assert "기존 엣지 프로세스·서비스 중단" in pv["script"], "중단 단계 누락"
        assert "disable --now" in pv["script"], "기존 서비스 중단 누락"
        assert "rm -f /etc/systemd/system/isilon_usage.service" in pv["script"], "구버전 유닛 제거 누락"
        assert 'pkill -f "isilon_usage serve"' in pv["script"], "잔여 프로세스 정리 누락"
        assert any(n["id"] == "auto1" for n in pc.list_nodes()["nodes"]), "노드 자동 등록 실패"
        names = tarfile.open(fileobj=io.BytesIO(portalmod.agent_bundle_bytes()),
                             mode="r:gz").getnames()
        assert "isilon_usage/portal.py" in names and "isilon_usage/__init__.py" in names, names
        # SSH 푸시는 옵션 — ssh 없으면 스크립트를 동봉해 우아하게 실패해야 함
        ssh = pc.provision_ssh({"host": "10.9.9.9", "ssh_user": "root",
                                "ssh_password": "x", "name": "auto1"})
        assert "script" in ssh and isinstance(ssh["ok"], bool), ssh
        assert pc.delete_node("auto1")["ok"]

        # 11) 엣지 자기등록(enroll): 비번 없으면 LAN 개방 허용, enroll_token 설정 시 일치 필요
        en = pc.enroll_node({"url": ebase, "token": "tok", "region": "KR", "id": "self1"})
        assert en["ok"] and en.get("enrolled") == "self1", en
        assert any(n["id"] == "self1" for n in pc.list_nodes()["nodes"]), "자기등록 실패"
        pc.set_enroll_token("S3CRET")
        bad = pc.enroll_node({"url": ebase, "token": "tok", "id": "self2"})
        assert not bad["ok"] and bad.get("_status") == 401, bad
        assert pc.enroll_node({"url": ebase, "token": "tok", "id": "self2",
                               "enroll_token": "S3CRET"})["ok"]
        pc.set_enroll_token("")
        assert pc.delete_node("self1")["ok"] and pc.delete_node("self2")["ok"]

        # 12) 릴리스 폴더(엣지 오프라인 업그레이드): 최신 패키지 버전 선택 + 정보 노출
        reld = os.path.join(tmp, "rel"); os.makedirs(reld, exist_ok=True)
        for fn in ("isilon_usage-1.2.0.tar.gz", "isilon_usage-1.10.0.tar.gz",
                   "isilon_usage-latest.tar.gz"):
            with open(os.path.join(reld, fn), "wb") as fh:
                fh.write(b"x")
        assert os.path.basename(portalmod.newest_release_archive(reld)) == "isilon_usage-1.10.0.tar.gz"
        assert portalmod.newest_release_archive(os.path.join(tmp, "nope")) is None
        ri = pc.set_release_dir(reld)
        assert ri["ok"] and ri["release_available"] and ri["release_file"] == "isilon_usage-1.10.0.tar.gz", ri
        assert pc.upgrade_config().get("release_dir") == reld

        # 12-b) 사용자 보고 시나리오: 1.69.4 vs 1.70.1 → 반드시 '최신(1.70.1)' 선택(구버전 금지).
        #       구버전 mtime 을 더 최신으로 만들어도 '버전'이 이겨야 한다.
        reld2 = os.path.join(tmp, "rel2"); os.makedirs(reld2, exist_ok=True)
        for fn in ("isilon_usage-1.69.4.tar.gz", "isilon_usage-1.70.1.tar.gz"):
            with open(os.path.join(reld2, fn), "wb") as fh:
                fh.write(b"x")
        os.utime(os.path.join(reld2, "isilon_usage-1.69.4.tar.gz"), None)   # 구버전을 최신 mtime 으로
        info2 = pc.set_release_dir(reld2)
        assert info2["release_available"] and info2["release_file"] == "isilon_usage-1.70.1.tar.gz", info2
        assert info2["release_version"] == "1.70.1", info2
        assert "isilon_usage-1.70.1.tar.gz" in info2["release_seen"] and info2.get("release_host"), info2

        # 12-c) 진단: 빈 폴더·없는 폴더는 '왜 없는지' 사유를 노출(호스트/경로 착오를 바로 알게)
        de = pc.set_release_dir(os.path.join(tmp, "empty_rel"))
        os.makedirs(os.path.join(tmp, "empty_rel"), exist_ok=True)
        de = pc.release_info()
        assert not de["release_available"] and de["release_reason"], de
        dn = pc.set_release_dir(os.path.join(tmp, "no_such_dir"))
        assert (not dn["release_available"] and not dn["release_dir_exists"]
                and "폴더가 없습니다" in dn["release_reason"]), dn

        # 12-d) '파일명 ≠ 내용' 잘못 라벨된 tar.gz 적발 — 엣지가 받는 건 파일명이 아니라 '내용' 버전.
        reld3 = os.path.join(tmp, "rel3"); os.makedirs(reld3, exist_ok=True)
        with tarfile.open(os.path.join(reld3, "isilon_usage-9.9.9.tar.gz"), "w:gz") as tf:
            data = b'__version__ = "1.0.0"\n'   # 파일명은 9.9.9, 내용은 1.0.0
            ti = tarfile.TarInfo("isilon_usage-9.9.9/isilon_usage/__init__.py"); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        mm = pc.set_release_dir(reld3)
        assert mm["release_available"] and mm["release_version"] == "9.9.9", mm
        assert mm["release_content_version"] == "1.0.0" and mm["release_version_mismatch"], mm
        assert "잘못" in mm["release_reason"], mm
        pc.set_release_dir(reld)   # 원복

        # 12-e) 원격 업그레이드 스크립트: 낡은 바이트코드(__pycache__) 정리 + 서비스명 자동 탐색 포함
        #        (tar 덮어쓰기 후 옛 .pyc 가 남아 구버전이 보고/실행되거나, 서비스명 불일치로 재시작
        #         안 되는 문제를 방지)
        scr = portalmod.build_upgrade_script(hq_base="http://hq:8800")
        assert "agent-bundle" in scr and "__pycache__" in scr, scr
        assert 'for s in isilon-edge isilon_usage' in scr, scr

        # 12-f) 전 노드 푸시: 배포 코드 버전 노출 + 모달용 진행 상태(노드 없는 새 컨트롤러로 격리)
        assert portalmod.agent_bundle_version()                       # 디스크 __init__ 의 버전
        pc2 = portalmod.PortalController(os.path.join(tmp, "pc2"))
        sp = pc2.start_push_all()
        assert sp["ok"] and sp.get("started") and sp.get("bundle_version"), sp
        pst = pc2.upgrade_status().get("push") or {}
        assert pst.get("total") == 0 and "nodes" in pst and pst.get("bundle_version"), pst
        cfg = pc2.upgrade_config()
        assert "bundle_version" in cfg and "bundle_stale" in cfg, cfg

        # 12-g) 백업: 포탈 설정+노드 목록 tar.gz (다운로드 바이트 / 서버 디렉터리 저장)
        import io as _io
        import tarfile as _tf
        bdata, bname = pc.make_backup_bytes()
        assert bname.startswith("isilon_portal_backup-") and bname.endswith(".tar.gz"), bname
        with _tf.open(fileobj=_io.BytesIO(bdata), mode="r:gz") as t:
            bnames = t.getnames()
        assert any(n.endswith("backup_manifest.json") for n in bnames), bnames
        assert any(n.endswith("portal_nodes.json") for n in bnames), bnames    # 노드 목록 포함
        sv = pc.save_backup(os.path.join(tmp, "bk"))
        assert sv["ok"] and os.path.isfile(sv["path"]) and sv["size"] > 0, sv
        assert not pc.save_backup("")["ok"]      # 빈 경로 거부

        # 12-h) 자동 백업 일정/보관: 설정 저장 + 최대 보관 수 초과분 정리(prune)
        abk = os.path.join(tmp, "autobk")
        cbk = pc.set_backup_config(abk, 2, 3)    # 2시간마다, 최대 3개
        assert (cbk["ok"] and cbk["backup_dir"] == abk and cbk["backup_every_hours"] == 2
                and cbk["backup_keep"] == 3), cbk
        os.makedirs(abk, exist_ok=True)
        for i in range(5):                       # 더미 백업 5개(오래된→최신 mtime)
            p = os.path.join(abk, "isilon_portal_backup-2026010%d-000000.tar.gz" % (i + 1))
            with open(p, "wb") as fh:
                fh.write(b"x")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
        assert len(pc._list_backups(abk)) == 5
        assert pc._prune_backups(abk, 3) == 2 and len(pc._list_backups(abk)) == 3   # 최신 3개만
        bi = pc.backup_info()
        assert bi["backup_count"] == 3 and bi["backup_keep"] == 3, bi
        pc.set_backup_config("", 1, 100)         # 자동 백업 끔(원복)

        # 12-i) 업그레이드 기록(History): 기록 후 최신순 조회 + 세부(엣지별 결과) 보존
        pc._record_upgrade(kind="인터넷", ok=True, frm="1.0.0", to="2.0.0",
                           push=[{"id": "e1", "ok": True, "version": "2.0.0"},
                                 {"id": "e2", "ok": False, "reason": "거부"}])
        pc._record_upgrade(kind="전 노드 푸시", ok=False, to="2.0.0",
                           push=[{"id": "e1", "ok": False, "reason": "HTTP 400"}])
        hist = pc.upgrade_history(50)
        assert len(hist) >= 2 and hist[0]["kind"] == "전 노드 푸시", hist[0]   # 최신순
        hi = next(h for h in hist if h["kind"] == "인터넷")
        assert hi["to"] == "2.0.0" and hi["push_ok"] == 1 and hi["push_total"] == 2, hi
        assert any(n["id"] == "e2" for n in hi["nodes"]), hi                   # 세부 노드 보존

        # 12-j) 핑 이력: 등록 노드만 조회 + 옛 id(유령) prune/노드삭제 시 정리
        from isilon_usage import db as _db
        pc.upsert_node({"id": "pingnode", "url": "http://10.0.0.9:8765", "token": "t"})
        cx = _db.connect(portalmod.ping_history_path(pc.data_dir))
        cx.executemany("INSERT INTO ping_samples (ts,node_id,latency_ms,up) VALUES (?,?,?,?)",
                       [(int(time.time()), "pingnode", 5.0, 1),
                        (int(time.time()), "192.168.84.221", 9.0, 1)])         # 등록 안 된 유령
        cx.commit(); cx.close()
        seen = {n["id"] for n in pc.ping_history(86400)["nodes"]}
        assert "pingnode" in seen and "192.168.84.221" not in seen, seen       # 유령은 안 보임
        pc._prune_ping_history()
        cx = _db.connect(portalmod.ping_history_path(pc.data_dir))
        left = {r[0] for r in cx.execute("SELECT DISTINCT node_id FROM ping_samples").fetchall()}
        cx.close()
        assert "192.168.84.221" not in left and "pingnode" in left, left       # DB 에서도 정리
        pc.delete_node("pingnode")
        cx = _db.connect(portalmod.ping_history_path(pc.data_dir))
        left2 = {r[0] for r in cx.execute("SELECT DISTINCT node_id FROM ping_samples").fetchall()}
        cx.close()
        assert "pingnode" not in left2, left2                                  # 노드삭제 시 핑도 삭제

        # 12-k) 기동 시 포탈 버전 변경(셸 재설치 등) 자동 기록 + 같은 버전 중복 방지
        import isilon_usage as _iu
        pc._set_version_marker("0.0.1")          # 직전 버전을 다르게 만들어 둠
        n0 = len(pc.upgrade_history(200))
        pc._record_startup_version()
        h = pc.upgrade_history(200)
        assert len(h) == n0 + 1 and h[0]["kind"] == "포탈 기동" and h[0]["to"] == _iu.__version__, h[0]
        pc._record_startup_version()             # 같은 버전이면 추가 기록 없음
        assert len(pc.upgrade_history(200)) == n0 + 1, "same version must not re-record"

        # 13) 업그레이드 상태: 노드 버전이 HQ보다 낮으면 '구버전'으로 집계('모두 최신' 착시 방지)
        assert pc.upsert_node({"id": "old-node", "url": "http://10.9.9.1:8765", "token": "x"})["ok"]
        with pc._lock:
            pc._cache["old-node"] = {"version": "1.0.0"}
        us = pc.upgrade_status()
        assert us["ok"] and us.get("edges_outdated", 0) >= 1, us
        assert any(e["id"] == "old-node" and e["version"] == "1.0.0"
                   for e in us.get("edges_outdated_list", [])), us
        assert pc.delete_node("old-node")["ok"]

        # 14) 업그레이드 동시 실행 방지(뮤텍스) — 자동/수동 중복 푸시 차단
        assert pc._try_begin_install() is True
        assert pc._try_begin_install() is False         # 이미 installing → 거부
        with pc._lock:
            pc._upg_state["installing"] = False
        assert pc._try_begin_install() is True           # 풀리면 다시 가능
        with pc._lock:
            pc._upg_state["installing"] = False

        # 15) provision 덮어쓰기: 재등록 시 기존 노드 전체 갱신 + 빈 토큰이면 기존 토큰 재사용
        p1 = pc.provision_plan({"host": "10.5.5.5", "port": 8765, "name": "ov1", "region": "A"})
        assert p1["ok"] and not p1["overwritten"], p1
        tok1 = p1["token"]
        p2 = pc.provision_plan({"host": "10.5.5.5", "port": 8765, "name": "ov1", "region": "B"})
        assert p2["ok"] and p2["overwritten"] and p2["token_reused"] and p2["token"] == tok1, p2
        assert any(n["id"] == "ov1" and n["region"] == "B" for n in pc.list_nodes()["nodes"]), "덮어쓰기 안 됨"
        # 같은 서버(url)를 다른 이름으로 → 옛 항목 대체(중복 제거)
        p3 = pc.provision_plan({"host": "10.5.5.5", "port": 8765, "name": "ov1-new"})
        assert p3["ok"] and p3["replaced_prev"] == "ov1", p3
        assert not any(n["id"] == "ov1" for n in pc.list_nodes()["nodes"]), "옛 이름 항목이 남음"
        # overwrite=False 면 거부
        p4 = pc.provision_plan({"host": "10.5.5.5", "port": 8765, "name": "ov1-new", "overwrite": False})
        assert not p4["ok"], p4
        assert pc.delete_node("ov1-new")["ok"]

        # 16) 폴링 401 → 원시 메시지 대신 '토큰 불일치' 안내(엣지는 'tok' 요구)
        pc.upsert_node({"id": "wrongtok", "url": ebase, "token": "BADTOKEN"})
        pc._sync_one("wrongtok", False)
        wt = next(n for n in pc.list_nodes()["nodes"] if n["id"] == "wrongtok")
        assert "토큰 불일치" in (wt.get("error") or ""), wt
        assert pc.delete_node("wrongtok")["ok"]

        print("[portal] OK  연결테스트·등록·폴링·복제·롤업·삭제·enroll·release·구버전집계 통과")
        print("모든 테스트 통과 ✅")
        return 0
    finally:
        edge.shutdown()
        edge.server_close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

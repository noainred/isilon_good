"""화면 재디자인(1.99.43) 회귀 테스트 — 공통 테마·메뉴 구조·서빙 경로.

- 테마 원본은 theme.css 하나(단일 소스)이고, 두 HTML 안의 테마 블록은 그 원본과 정확히 같다(tools/sync_theme.py).
  HTML 이 별도 요청 없이 스스로 모양을 갖춰야 한다 — 1.99.43 은 /theme.css 를 따로 불러, 업그레이드 경로에 따라
  엣지 화면이 스타일 없이 깨졌다(새 HTML + 옛 서버 프로세스, 또는 css 가 빠진 구버전 포탈 번들).
- 에이전트 번들(포탈 → 엣지 배포)에 theme.css 가 들어간다(빠지면 업그레이드 후 화면이 깨진다).
- 엣지의 모든 화면(VIEWS)은 왼쪽 메뉴 그룹(VIEW_GROUP)과 하위 탭 버튼을 가진다(갈 수 없는 화면이 없게).
- 어두운 테마용 하드코딩 색이 남지 않는다(글자색 잉크만 허용).
"""

import colorsys
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "isilon_usage")
INK_OK = {"#111827", "#1F2937", "#0F172A", "#000000"}   # 진한 글자색(배경 아님)


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def _strip_theme(s):
    from tools import sync_theme as st
    return st._BLOCK.sub("", s)


def check_shared_theme_link() -> None:
    from tools import sync_theme as st
    css = _read("theme.css")
    assert "--bg:#F4F5F7" in css and "#sidebar" in css and ".portal " in css
    blk = st.block()
    for page in ("dashboard.html", "portal.html"):
        s = _read(page)
        assert s.count(blk) == 1, "%s: 내장 테마가 theme.css 와 다름 — python3 tools/sync_theme.py" % page
        rest = _strip_theme(s)
        assert "<style>" not in rest, "%s: 테마 블록 밖에 스타일 블록이 있음(원본은 theme.css 하나)" % page
        assert '<link rel="stylesheet"' not in rest, "%s: 외부 스타일시트 의존(별도 요청 실패 시 화면이 깨짐)" % page
        assert 'id="sidebar"' in s and 'id="topbar"' in s and 'id="askOut"' in s, page
    print("[theme] OK  두 HTML 의 내장 테마 = theme.css(단일 원본), 외부 스타일시트 의존 없음")


def check_no_dark_hardcoded_colors() -> None:
    for page in ("dashboard.html", "portal.html"):
        s = _strip_theme(_read(page))   # 테마 블록(사이드바 남색 등 의도된 색)은 제외
        bad = set()
        for m in re.finditer(r"#([0-9a-fA-F]{6})\b", s):
            h = m.group(1)
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
            if colorsys.rgb_to_hls(r, g, b)[1] < 0.2 and ("#" + h.upper()) not in INK_OK:
                bad.add("#" + h.upper())
        assert not bad, "%s: 어두운 테마용 색이 남음 %s" % (page, sorted(bad))
        assert "rgba(255,255,255," not in s.replace(" ", ""), "%s: 어두운 바탕용 흰 반투명 오버레이" % page
    print("[colors] OK  어두운 테마용 하드코딩 색 없음(글자 잉크만)")


def check_every_view_reachable() -> None:
    s = _read("dashboard.html")
    views = re.findall(r'"([a-z]+)"', re.search(r"const VIEWS = \[(.*?)\];", s).group(1))
    grp_src = re.search(r"const VIEW_GROUP = \{(.*?)\};", s, re.S).group(1)
    groups = dict(re.findall(r"([a-z]+): \"([a-z]+)\"", grp_src))
    side = set(re.findall(r'class="sidebtn" data-group="([a-z]+)"', s))
    for v in views:
        assert 'id="view-%s"' % v in s, "화면 없음: " + v
        assert v in groups, "메뉴 그룹 없음: " + v
        assert groups[v] in side, "왼쪽 메뉴 버튼 없음: %s(%s)" % (groups[v], v)
        assert re.search(r'class="navbtn" data-group="%s" data-view="%s"' % (groups[v], v), s), "하위 탭 없음: " + v
    assert len(side) == 6, side      # 홈·스캔·탐색·리포트·도구 + 설정
    print("[nav] OK  화면 %d개 모두 메뉴(%d개 그룹)·하위 탭으로 갈 수 있음" % (len(views), len(side)))


def check_theme_served_and_bundled() -> None:
    from isilon_usage.server import serve
    from isilon_usage.portal import serve_portal, agent_bundle_bytes
    import io
    import tarfile
    names = tarfile.open(fileobj=io.BytesIO(agent_bundle_bytes()), mode="r:gz").getnames()
    assert "isilon_usage/theme.css" in names, "에이전트 번들에 theme.css 없음"
    d = tempfile.mkdtemp(prefix="iu_ui_")
    cwd = os.getcwd()
    os.chdir(d)
    servers = []
    try:
        servers.append(serve(os.path.join(d, "edge"), host="127.0.0.1", port=0, enable_scan=False))
        servers.append(serve_portal(os.path.join(d, "hq"), host="127.0.0.1", port=0))
        for h in servers:
            threading.Thread(target=h.serve_forever, daemon=True).start()
            r = urllib.request.urlopen("http://127.0.0.1:%d/theme.css" % h.server_address[1])
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/css")
            assert b"--bg:#F4F5F7" in r.read()
    finally:
        for h in servers:
            h.shutdown()
        os.chdir(cwd)
        shutil.rmtree(d, ignore_errors=True)
    print("[serve] OK  엣지·포탈 모두 /theme.css 응답(text/css), 에이전트 번들에 포함")


def check_eta_single_source() -> None:
    """완료 예상: 상태 화면 '요약'·홈 카드·포탈이 같은 계산(manager.estimate_eta_seconds)을 쓰고, 저장소 목록에 실린다."""
    import time
    from isilon_usage import db as dbmod
    from isilon_usage import manager as M
    assert M.estimate_eta_seconds("sizing", 100, 1000, 0, 50) == 450.0
    assert M.estimate_eta_seconds("discovering", 100, 1000, 0, 50) is None     # 폴더 찾기 단계는 계산 안 함
    assert M.estimate_eta_seconds("sizing", 0, 1000, 0, 50) is None
    d = tempfile.mkdtemp(prefix="iu_eta_")
    try:
        M.init_manager(d)
        sid = M.register_scan(d, root_path="/x", db_path=os.path.join(d, "a.db"), backend="native", size_mode="disk")
        c = dbmod.connect(M.manager_db_path(d))
        M.update_scan(c, sid, status="sizing", phase="sizing", processed_dirs=250, total_dirs=1000,
                      started_at=time.time() - 100)
        c.commit()
        r = M.overall_capacity(c)["roots"][0]
        c.close()
        assert r["eta_seconds"] is not None and 290 <= r["eta_seconds"] <= 310, r["eta_seconds"]
        assert abs(r["eta_at"] - (time.time() + r["eta_seconds"])) < 5
    finally:
        shutil.rmtree(d, ignore_errors=True)
    src = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    assert "mgrmod.estimate_eta_seconds(" in src, "상태 화면 ETA 가 공용 함수를 쓰지 않음"
    dash = _read("dashboard.html")
    assert dash.count("fmtEtaClock(") >= 3, "요약·홈 카드가 같은 완료 시각 표기를 쓰지 않음"
    print("[eta] OK  남은 시간·완료 예상 = 공용 계산 1개, 저장소 목록(eta_seconds·eta_at)에 실림")


def check_portal_all_edges_eta() -> None:
    """포탈 현황: 전체 엣지의 진행 중 스캔과 예상 종료 — 새 엣지는 보낸 eta_at, 구버전 엣지는 포탈이 같은 계산으로 채움."""
    from isilon_usage import portal as P
    d = tempfile.mkdtemp(prefix="iu_peta_")
    try:
        pc = P.PortalController(os.path.join(d, "hq"))
        base = {"fs_total_bytes": 10, "fs_used_bytes": 5}
        nodes = {
            "new": {"status": "sizing", "phase": "sizing", "processed_dirs": 50, "total_dirs": 100,
                    "started_at": 1000, "updated_at": 1100, "eta_at": 5000.0},
            "old": {"status": "sizing", "phase": "sizing", "processed_dirs": 25, "total_dirs": 100,
                    "started_at": 1000, "updated_at": 1100},                       # 구버전 — eta 없음
            "disc": {"status": "discovering", "phase": "discovering", "discovered_dirs": 9,
                     "started_at": 1000, "updated_at": 1100},
            "off": {"status": "sizing", "phase": "sizing", "processed_dirs": 1, "total_dirs": 100,
                    "started_at": 1000, "updated_at": 1100, "eta_at": 9e9},
        }
        for nid, rt in nodes.items():
            pc.upsert_node({"id": nid, "url": "http://10.0.0.%d:8765" % (len(pc.nodes) + 1), "token": "t"})
            pc._cache[nid] = {"online": nid != "off", "overall": P._sanitize_overall(
                {"roots": [dict(base, root_path="/" + nid, **rt)], "active_scans": 1})}
        ov = pc.overview()
        run = {r["node"]: r for r in ov["running"]}
        assert set(run) == {"new", "old", "disc"}, run.keys()            # 끊긴 노드(옛 값)는 제외
        assert run["new"]["eta_at"] == 5000.0 and not run["new"]["eta_by_portal"]
        assert run["old"]["eta_by_portal"] and abs(run["old"]["eta_at"] - (1100 + 300)) < 1e-6, run["old"]
        assert run["disc"]["eta_at"] is None
        assert ov["totals"]["eta_at"] == 5000.0 and ov["totals"]["running_no_eta"] == 1
        assert [r["node"] for r in ov["running"]][-1] == "disc"              # 계산 전은 뒤로
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert "renderRunAll(j)" in _read("portal.html")
    print("[portal-eta] OK  전체 엣지 진행 스캔·예상 종료(구버전 엣지는 포탈이 같은 계산으로 채움)")


def check_inline_js_syntax() -> None:
    """화면 HTML 안의 스크립트 문법 검사(node 가 있으면) — 한 줄 실수로 화면 전체 JS 가 멈추는 것을 배포 전에 잡는다."""
    import subprocess
    node = shutil.which("node")
    if not node:
        print("[js] SKIP  node 없음")
        return
    d = tempfile.mkdtemp(prefix="iu_js_")
    try:
        for page in ("dashboard.html", "portal.html"):
            blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _read(page), re.S)
            p = os.path.join(d, page + ".js")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("\n;\n".join(blocks))
            r = subprocess.run([node, "--check", p], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert r.returncode == 0, "%s 스크립트 문법 오류:\n%s" % (page, r.stdout.decode()[-600:])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("[js] OK  dashboard·portal 인라인 스크립트 문법 정상")


def main() -> int:
    check_shared_theme_link()
    check_no_dark_hardcoded_colors()
    check_every_view_reachable()
    check_theme_served_and_bundled()
    check_inline_js_syntax()
    check_eta_single_source()
    check_portal_all_edges_eta()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

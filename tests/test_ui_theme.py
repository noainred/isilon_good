"""화면 재디자인(1.99.43) 회귀 테스트 — 공통 테마·메뉴 구조·서빙 경로.

- 엣지·포탈이 같은 /theme.css(단일 소스)를 쓰고, 두 서버 모두 그 파일을 내려준다.
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


def check_shared_theme_link() -> None:
    css = _read("theme.css")
    assert "--bg:#F4F5F7" in css and "#sidebar" in css and ".portal " in css
    for page in ("dashboard.html", "portal.html"):
        s = _read(page)
        assert '<link rel="stylesheet" href="/theme.css"' in s, page
        assert "<style>" not in s, "%s: 페이지 안 스타일 블록이 남아 있음(테마는 theme.css 하나로)" % page
        assert 'id="sidebar"' in s and 'id="topbar"' in s and 'id="askOut"' in s, page
    print("[theme] OK  엣지·포탈 모두 /theme.css 하나를 씀, 사이드바·상단바 구조")


def check_no_dark_hardcoded_colors() -> None:
    for page in ("dashboard.html", "portal.html"):
        s = _read(page)
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


def main() -> int:
    check_shared_theme_link()
    check_no_dark_hardcoded_colors()
    check_every_view_reachable()
    check_theme_served_and_bundled()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

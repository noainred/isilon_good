"""자동 업그레이드(upgrade) — 버전 비교·새 압축본 탐지·패키지 교체(백업)·번들 적용 검증."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import upgrade  # noqa: E402


def _make_archive(path: str, version: str) -> None:
    """isilon_usage-<version>/isilon_usage/ 를 담은 tar.gz 생성."""
    with tarfile.open(path, "w:gz") as tf:
        for rel, body in (("__init__.py", '__version__ = "%s"\n' % version),
                          ("server.py", "# new\n")):
            data = body.encode()
            ti = tarfile.TarInfo("isilon_usage-%s/isilon_usage/%s" % (version, rel))
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def main() -> int:
    # 버전 파싱/비교
    assert upgrade.parse_version("1.2.3") == (1, 2, 3)
    assert upgrade.parse_version("v1.40.0") == (1, 40, 0)
    assert upgrade.parse_version("bad") is None

    wd = tempfile.mkdtemp(prefix="iu_up_")
    code = tempfile.mkdtemp(prefix="iu_code_")
    try:
        for v in ("1.39.0", "1.41.0", "1.40.0"):
            _make_archive(os.path.join(wd, "isilon_usage-%s.tar.gz" % v), v)
        # 탐지: 현재(1.40)보다 새것 중 최신(1.41)
        found = upgrade.find_newer_archive(wd, "1.40.0")
        assert found and found[1] == (1, 41, 0), found
        assert upgrade.find_newer_archive(wd, "1.41.0") is None   # 더 새것 없음

        # 교체(apply): 기존 코드 → 백업, 새 코드 설치
        os.makedirs(os.path.join(code, "isilon_usage"))
        with open(os.path.join(code, "isilon_usage", "__init__.py"), "w") as fh:
            fh.write('__version__ = "1.40.0"\n')
        with open(os.path.join(code, "isilon_usage", "OLD.txt"), "w") as fh:
            fh.write("old")
        res = upgrade.upgrade_from_archive(found[0], code, "1.40.0")
        assert res["ok"] and res["version"] == "1.41.0", res
        with open(os.path.join(code, "isilon_usage", "__init__.py")) as fh:
            assert "1.41.0" in fh.read()
        assert os.path.exists(os.path.join(code, "isilon_usage", "server.py"))   # 새 파일
        assert not os.path.exists(os.path.join(code, "isilon_usage", "OLD.txt"))  # 교체됨
        assert res["backup"] and os.path.isdir(res["backup"])                     # 기존 백업
        assert os.path.exists(os.path.join(res["backup"], "OLD.txt"))             # 백업에 옛 파일
        # 같은/낮은 버전은 거부
        assert not upgrade.upgrade_from_archive(found[0], code, "1.41.0")["ok"]
    finally:
        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(code, ignore_errors=True)

    # 번들 bytes 적용(포탈 푸시 경로)
    code2 = tempfile.mkdtemp(prefix="iu_code2_")
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b'__version__ = "2.0.0"\n'
            ti = tarfile.TarInfo("isilon_usage/__init__.py")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        os.makedirs(os.path.join(code2, "isilon_usage"))
        with open(os.path.join(code2, "isilon_usage", "__init__.py"), "w") as fh:
            fh.write('__version__ = "1.0.0"\n')
        r = upgrade.upgrade_from_bundle_bytes(buf.getvalue(), code2, "1.0.0")
        assert r["ok"] and r["version"] == "2.0.0", r
        # 경로 탈출 시도(../) 는 무시되는지: 멤버명에 .. 포함
        buf2 = io.BytesIO()
        with tarfile.open(fileobj=buf2, mode="w:gz") as tf:
            evil = b"evil"
            ti = tarfile.TarInfo("isilon_usage/../escape.py")
            ti.size = len(evil)
            tf.addfile(ti, io.BytesIO(evil))
        # __init__ 없으니 적용 거부(버전 미확인) — 그리고 escape.py 는 code2 밖에 생기면 안 됨
        upgrade.upgrade_from_bundle_bytes(buf2.getvalue(), code2, "1.0.0")
        assert not os.path.exists(os.path.join(os.path.dirname(code2), "escape.py"))
    finally:
        shutil.rmtree(code2, ignore_errors=True)

    print("[upgrade] OK  버전 비교·새 압축본 탐지·패키지 교체(백업)·번들 적용·경로탈출 차단")

    # --- 인터넷(원격) 자동 업그레이드: 로컬 HTTP 서버로 versions.json+tar.gz 서빙(네트워크 비의존) ---
    import json as _json
    import threading as _th
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    srv = tempfile.mkdtemp(prefix="iu_up_srv_")
    rcode = tempfile.mkdtemp(prefix="iu_up_code_")
    rdl = tempfile.mkdtemp(prefix="iu_up_dl_")
    try:
        _arc = os.path.join(srv, "isilon_usage-9.9.9.tar.gz")
        _make_archive(_arc, "9.9.9")
        import hashlib as _hl
        _sha = _hl.sha256(open(_arc, "rb").read()).hexdigest()
        with open(os.path.join(srv, "versions.json"), "w") as fh:
            _json.dump({"latest": "9.9.9", "versions": [
                {"version": "9.9.9", "tar_gz": "isilon_usage-9.9.9.tar.gz",
                 "size_bytes": 100, "sha256": _sha}]}, fh)
        os.makedirs(os.path.join(rcode, "isilon_usage"))
        with open(os.path.join(rcode, "isilon_usage", "__init__.py"), "w") as fh:
            fh.write('__version__ = "1.0.0"\n')

        class _Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        httpd = HTTPServer(("127.0.0.1", 0),
                           lambda *a, **k: _Quiet(*a, directory=srv, **k))
        _th.Thread(target=httpd.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % httpd.server_address[1]
        info = upgrade.check_remote(base, "1.0.0", timeout=5)
        assert info["ok"] and info["available"] and info["latest"] == "9.9.9", info
        assert str(info.get("download_url", "")).endswith("isilon_usage-9.9.9.tar.gz"), info
        assert not upgrade.check_remote(base, "9.9.9")["available"]    # 이미 최신이면 안 알림
        res = upgrade.upgrade_from_remote(base, rcode, "1.0.0", rdl, timeout=10)
        assert res["ok"] and res["version"] == "9.9.9", res
        with open(os.path.join(rcode, "isilon_usage", "__init__.py")) as fh:
            assert "9.9.9" in fh.read()
        # 잘못된 파일명/다운그레이드 거부
        assert not upgrade.download_archive(base + "/evil.sh", rdl)["ok"]
        # sha256 무결성: 일치 통과, 불일치 거부(변조/탈취 미러 차단)
        assert upgrade.download_archive(base + "/isilon_usage-9.9.9.tar.gz", rdl,
                                        expected_sha256=_sha)["ok"]
        _bad = upgrade.download_archive(base + "/isilon_usage-9.9.9.tar.gz", rdl,
                                        expected_sha256="deadbeef")
        assert not _bad["ok"] and "무결성" in _bad["reason"], _bad
        httpd.shutdown()
        print("[remote-upgrade] OK  원격 확인·다운로드·설치·최신판정·파일명·sha256 무결성")
    finally:
        for d in (srv, rcode, rdl):
            shutil.rmtree(d, ignore_errors=True)

    # --- URL 조립·GitHub raw→API 변환(브랜치에 '/' 있어도 안전) ---
    assert upgrade._join_url("http://h/a/download", "versions.json") == "http://h/a/download/versions.json"
    assert (upgrade._join_url("http://h/a/contents/download?ref=cl/x", "versions.json")
            == "http://h/a/contents/download/versions.json?ref=cl/x")
    assert (upgrade._to_github_api("https://raw.githubusercontent.com/o/r/claude/upbeat-bell-cXX8f/download")
            == "https://api.github.com/repos/o/r/contents/download?ref=claude/upbeat-bell-cXX8f")
    assert (upgrade._to_github_api("https://github.com/o/r/raw/main/download")
            == "https://api.github.com/repos/o/r/contents/download?ref=main")
    assert upgrade._to_github_api("http://mirror.local/iu/download") == "http://mirror.local/iu/download"
    assert "raw.githubusercontent.com" in upgrade._resolve_base(
        "https://raw.githubusercontent.com/o/r/main/download", None)   # 토큰 없으면 raw 그대로
    assert "api.github.com" in upgrade._resolve_base(
        "https://raw.githubusercontent.com/o/r/main/download", "tok")  # 토큰 있으면 contents API
    print("[upgrade-url] OK  versions.json 조립·raw→contents API 변환·토큰 조건부 변환")

    # --- 사설(비공개) 소스: Authorization 토큰이 있어야만 받아지는 서버로 검증 ---
    from http.server import BaseHTTPRequestHandler
    from http.server import HTTPServer as _HTTPServer
    srv2 = tempfile.mkdtemp(prefix="iu_up_auth_")
    rcode2 = tempfile.mkdtemp(prefix="iu_up_acode_")
    rdl2 = tempfile.mkdtemp(prefix="iu_up_adl_")
    TOKEN = "secret-pat-123"
    try:
        _make_archive(os.path.join(srv2, "isilon_usage-9.9.9.tar.gz"), "9.9.9")
        with open(os.path.join(srv2, "versions.json"), "w") as fh:
            _json.dump({"latest": "9.9.9", "versions": [
                {"version": "9.9.9", "tar_gz": "isilon_usage-9.9.9.tar.gz", "size_bytes": 100}]}, fh)
        os.makedirs(os.path.join(rcode2, "isilon_usage"))
        with open(os.path.join(rcode2, "isilon_usage", "__init__.py"), "w") as fh:
            fh.write('__version__ = "1.0.0"\n')

        class _Auth(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"unauthorized")
                    return
                fn = os.path.basename(self.path.split("?")[0])
                try:
                    with open(os.path.join(srv2, fn), "rb") as fh:
                        body = fh.read()
                except OSError:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd2 = _HTTPServer(("127.0.0.1", 0), _Auth)
        _th.Thread(target=httpd2.serve_forever, daemon=True).start()
        base2 = "http://127.0.0.1:%d" % httpd2.server_address[1]
        assert not upgrade.check_remote(base2, "1.0.0", timeout=5)["ok"]          # 토큰 없으면 거부
        info = upgrade.check_remote(base2, "1.0.0", token=TOKEN, timeout=5)       # 토큰 주면 확인
        assert info["ok"] and info["available"] and info["latest"] == "9.9.9", info
        res = upgrade.upgrade_from_remote(base2, rcode2, "1.0.0", rdl2, token=TOKEN, timeout=10)
        assert res["ok"] and res["version"] == "9.9.9", res
        with open(os.path.join(rcode2, "isilon_usage", "__init__.py")) as fh:
            assert "9.9.9" in fh.read()
        httpd2.shutdown()
        print("[remote-auth] OK  토큰 없으면 거부·토큰 있으면 확인/다운로드/설치(사설 소스)")
    finally:
        for d in (srv2, rcode2, rdl2):
            shutil.rmtree(d, ignore_errors=True)

    # --- 자동설치 상태 사유(왜 자동이 안 되는지 한 줄 설명) ---
    b, r = upgrade.auto_status_reason(source_mode="off", auto=False, available=True)
    assert b and "소스" in r, r                                  # 소스 꺼짐 → 차단
    b, r = upgrade.auto_status_reason(source_mode="github", auto=False, available=True)
    assert b and "자동설치" in r, r                              # 자동설치 꺼짐(알림만) → 차단
    b, r = upgrade.auto_status_reason(source_mode="github", auto=True, available=True, busy=True)
    assert b and "스캔" in r, r                                  # 스캔 중 → 보류
    b, r = upgrade.auto_status_reason(source_mode="github", auto=True, available=True)
    assert (not b) and "자동 설치" in r, r                       # 셋 다 OK → 곧 설치
    b, r = upgrade.auto_status_reason(source_mode="github", auto=True, available=False)
    assert not b, r                                              # 켜짐·현재 최신 → 대기(차단 아님)
    b, r = upgrade.auto_status_reason(source_mode="off", auto=False, available=False, watch_dir="/x")
    assert not b, r                                              # 감시폴더만으로도 자동 경로 살아있음
    b, r = upgrade.auto_status_reason(source_mode="github", auto=True, available=True, installing=True)
    assert (not b) and "설치" in r, r                            # 설치 중
    print("[auto-reason] OK  자동설치 차단 사유 설명(소스off/자동off/스캔중/예정/최신)")

    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

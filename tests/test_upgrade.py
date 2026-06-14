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
        _make_archive(os.path.join(srv, "isilon_usage-9.9.9.tar.gz"), "9.9.9")
        with open(os.path.join(srv, "versions.json"), "w") as fh:
            _json.dump({"latest": "9.9.9", "versions": [
                {"version": "9.9.9", "tar_gz": "isilon_usage-9.9.9.tar.gz", "size_bytes": 100}]}, fh)
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
        httpd.shutdown()
        print("[remote-upgrade] OK  원격 확인·다운로드·설치(1.0.0→9.9.9)·최신판정·파일명검증")
    finally:
        for d in (srv, rcode, rdl):
            shutil.rmtree(d, ignore_errors=True)

    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

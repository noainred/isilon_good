"""아이실론 PAPI 클라이언트 + /api/isilon 테스트(모의 OneFS 서버).

실제 클러스터 없이, 표준 PAPI 응답을 흉내 내는 작은 HTTP 서버를 띄워
IsilonClient.status() 파싱과 엣지 /api/isilon 엔드포인트를 검증한다.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import isilon_api  # noqa: E402
from isilon_usage import powerstore_api  # noqa: E402
from isilon_usage.server import serve  # noqa: E402


class _PAPI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        p = self.path
        if "cluster/config" in p:
            body = {"name": "isi-seoul", "onefs_version": {"release": "9.5.0.0"}}
        elif "statistics/current" in p:
            body = {"stats": [
                {"key": "ifs.bytes.total", "value": 1000},
                {"key": "ifs.bytes.used", "value": 750},
                {"key": "ifs.bytes.avail", "value": 250}]}
        elif "cluster/nodes" in p:
            body = {"total": 4, "nodes": [{"status": "up"}] * 4}
        elif "eventgroup-occurrences" in p:
            body = {"total": 2}
        else:
            body = {}
        b = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class _PowerStore(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("DELL-EMC-TOKEN", "tok123")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        p = self.path
        if "/cluster" in p:
            self._send([{"id": "C1", "name": "ps-tokyo", "state": "Configured"}])
        elif "/appliance" in p:
            self._send([{"id": "A1", "model": "PowerStore 1000T"}])
        elif "/node" in p:
            self._send([{"id": "N1"}, {"id": "N2"}])
        elif "/alert" in p:
            self._send([{"id": "x", "severity": "Major", "is_active": True}])
        else:
            self._send([])

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self._send([{"physical_total": 2000, "physical_used": 900}])


def _srv(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _papi():
    return _srv(_PAPI)


def main() -> int:
    # 1) 미설정
    assert isilon_api.cluster_status("", "", "")["configured"] is False

    # 2) 클라이언트 파싱
    papi, url = _papi()
    try:
        st = isilon_api.cluster_status(url, "u", "p")
        assert st["ok"] and st["configured"], st
        assert st["name"] == "isi-seoul" and st["version"] == "9.5.0.0", st
        assert st["capacity"]["used_pct"] == 75.0, st["capacity"]
        assert st["nodes"]["total"] == 4, st["nodes"]
        assert st["events_unresolved"] == 2 and st["health"] == "attention", st
    finally:
        papi.shutdown()

    # 3) PowerStore 클라이언트 파싱
    ps, psurl = _srv(_PowerStore)
    try:
        st = powerstore_api.cluster_status(psurl, "u", "p")
        assert st["ok"] and st["type"] == "powerstore" and st["name"] == "ps-tokyo", st
        assert st["version"] == "PowerStore 1000T" and st["nodes"]["total"] == 2, st
        assert st["capacity"]["used_pct"] == 45.0 and st["events_unresolved"] == 1, st
    finally:
        ps.shutdown()

    # 4) 엣지 /api/storage (아이실론 + PowerStore 동시) + /api/isilon + 마스킹
    papi2, url2 = _papi()
    ps2, psurl2 = _srv(_PowerStore)
    tmp = tempfile.mkdtemp(prefix="isi_")
    httpd = serve(tmp, host="127.0.0.1", port=0, enable_scan=True,
                  initial_settings={"isilon_url": url2, "isilon_user": "u", "isilon_password": "p",
                                    "powerstore_url": psurl2, "powerstore_user": "u",
                                    "powerstore_password": "p"})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        j = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/isilon" % port, timeout=5))
        assert j["ok"] and j["name"] == "isi-seoul", j
        sj = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/storage" % port, timeout=5))
        types = sorted(a["type"] for a in sj["arrays"])
        assert types == ["isilon", "powerstore"], sj
        # 설정 응답에 두 비밀번호 모두 마스킹
        s = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/settings" % port, timeout=5))["settings"]
        assert s["isilon_password"] == "" and s["isilon_password_set"] is True
        assert s["powerstore_password"] == "" and s["powerstore_password_set"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        papi2.shutdown()
        ps2.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("[isilon] OK  PAPI + PowerStore + /api/storage + 마스킹 통과")
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

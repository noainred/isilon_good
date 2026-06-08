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


def _papi():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _PAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


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

    # 3) 엣지 /api/isilon (설정 주입)
    papi2, url2 = _papi()
    tmp = tempfile.mkdtemp(prefix="isi_")
    httpd = serve(tmp, host="127.0.0.1", port=0, enable_scan=True,
                  initial_settings={"isilon_url": url2, "isilon_user": "u", "isilon_password": "p"})
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        j = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/isilon" % port, timeout=5))
        assert j["ok"] and j["name"] == "isi-seoul", j
        # 설정 응답에 비밀번호는 마스킹
        s = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/settings" % port, timeout=5))
        assert s["settings"]["isilon_password"] == "" and s["settings"]["isilon_password_set"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()
        papi2.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("[isilon] OK  PAPI 파싱 + /api/isilon + 마스킹 통과")
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

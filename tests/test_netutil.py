"""netutil.urlopen: 다른 origin 으로 리다이렉트될 때 인증 헤더를 넘기지 않는지(토큰·PAT·Basic 유출 방지)."""

import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from isilon_usage import netutil  # noqa: E402


def _server(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    class Echo(BaseHTTPRequestHandler):       # 받은 헤더를 그대로 돌려준다
        def do_GET(self):
            if self.path.startswith("/same"):   # 같은 origin 안 리다이렉트
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
                return
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    other = _server(Echo)                     # 리다이렉트 대상(다른 포트 = 다른 origin)
    other_url = "http://127.0.0.1:%d/final" % other.server_address[1]

    class Redirect(Echo):
        def do_GET(self):
            if self.path.startswith("/cross"):
                self.send_response(302)
                self.send_header("Location", other_url)
                self.end_headers()
                return
            Echo.do_GET(self)

    origin = _server(Redirect)
    base = "http://127.0.0.1:%d" % origin.server_address[1]
    try:
        def get(path):
            req = urllib.request.Request(base + path)
            req.add_header("Authorization", "Bearer SECRET")
            req.add_header("X-Auth-Token", "EDGE-TOKEN")
            req.add_header("X-Trace", "keep")
            with netutil.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode())

        cross = get("/cross")
        assert "authorization" not in cross and "x-auth-token" not in cross, cross
        assert cross.get("x-trace") == "keep", cross          # 인증 외 헤더는 유지
        same = get("/same")
        assert same.get("authorization") == "Bearer SECRET" and same.get("x-auth-token") == "EDGE-TOKEN", same
        print("[netutil] OK  다른 origin 리다이렉트 → 인증 헤더 제거 · 같은 origin 은 유지")
    finally:
        origin.shutdown()
        other.shutdown()
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

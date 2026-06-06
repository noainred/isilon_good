"""대시보드 웹 서버 (표준 라이브러리만 사용).

외부 프레임워크 없이 http.server 로 동작하므로 패키지 설치가 제한적인
서버에서도 그대로 띄울 수 있다. 스캐너가 SQLite(WAL)에 적재한 진행 상태와
자원 샘플을 읽어 JSON API 로 제공하고, 단일 페이지 대시보드를 서빙한다.

엔드포인트:
  GET /                  대시보드 HTML
  GET /api/status        현재 스캔 진행 상태 + 최신 자원 샘플 + 시계열 + 상위 디렉터리
  GET /api/children      특정 디렉터리의 하위 디렉터리 목록(드릴다운)
  GET /api/runs          스캔 실행 목록
"""

from __future__ import annotations

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import db as dbmod
from . import monitor as monmod


HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(HERE, "dashboard.html")


def _safe_pct(num: float, den: float) -> float:
    if not den:
        return 0.0
    return round(num / den * 100.0, 2)


def build_status(conn, run_id: int | None, *, samples: int = 150, top: int = 20) -> dict:
    """대시보드가 한 번의 폴링으로 쓸 수 있는 통합 상태 객체를 만든다."""
    if run_id is None:
        run_id = dbmod.latest_run_id(conn)
    if run_id is None:
        return {"ok": False, "reason": "no_runs", "have_psutil": monmod.have_psutil()}

    run = dbmod.get_run(conn, run_id)
    if run is None:
        return {"ok": False, "reason": "run_not_found", "run_id": run_id}

    r = dict(run)
    now = time.time()
    started = r.get("started_at") or now
    finished = r.get("finished_at")
    elapsed = (finished or now) - started

    total_dirs = r.get("total_dirs") or 0
    discovered = r.get("discovered_dirs") or 0
    processed = r.get("processed_dirs") or 0
    scanned_bytes = r.get("scanned_bytes") or 0
    fs_total = r.get("fs_total_bytes") or 0
    fs_used = r.get("fs_used_bytes") or 0

    # 진행률 계산
    if r["phase"] == "discovering":
        # 탐색 단계: 전체 디렉터리 수를 아직 모르므로 사용량(%)으로 진행 표현
        dir_progress = None
    else:
        dir_progress = _safe_pct(processed, total_dirs or discovered)

    # 처리 속도 / ETA (집계 단계 기준)
    eta = None
    if r["phase"] == "sizing" and processed > 0 and elapsed > 0:
        rate = processed / elapsed
        remaining = max((total_dirs or discovered) - processed, 0)
        if rate > 0:
            eta = remaining / rate

    # 최신 자원 샘플 + 시계열(스파크라인용)
    sample_rows = conn.execute(
        """SELECT ts, mem_percent, mem_used, mem_total, swap_used, cpu_percent,
                  load1, scanner_rss, du_rss, du_pid
           FROM resource_samples WHERE run_id=? ORDER BY id DESC LIMIT ?""",
        (run_id, samples),
    ).fetchall()
    series = [dict(s) for s in reversed(sample_rows)]
    recorded_latest = series[-1] if series else None
    latest = recorded_latest

    # 스캔이 "진행 중"이고 대시보드 서버가 스캐너와 같은 호스트에서 돌고 있으면,
    # 헤드라인 게이지(시스템 메모리/CPU/스캐너 RSS)를 매 폴링마다 실시간으로
    # 즉석 수집한다. SQLite 락 경합과 무관하게 항상 최신 값을 보여주기 위함이다.
    # (du 프로세스 메모리는 서버가 실시간으로 알 수 없어 최근 기록값을 사용)
    # 다른 호스트이거나 스캔이 끝난 경우에는 기록된 마지막 샘플을 그대로 쓴다.
    active = r["status"] in ("discovering", "sizing")
    same_host = bool(r.get("hostname")) and r.get("hostname") == socket.gethostname()
    if (active and same_host) or recorded_latest is None:
        try:
            pid = r.get("scanner_pid") or os.getpid()
            live = monmod.collect(int(pid), None).as_dict()
            if recorded_latest:
                live["du_rss"] = recorded_latest.get("du_rss", 0)
                live["du_pid"] = recorded_latest.get("du_pid")
            latest = live
            if recorded_latest is not None:
                series = series + [live]  # 차트의 마지막 점도 실시간으로
        except Exception:
            latest = recorded_latest

    peak = conn.execute(
        """SELECT COALESCE(MAX(scanner_rss),0) AS s, COALESCE(MAX(du_rss),0) AS d,
                  COALESCE(MAX(mem_percent),0) AS m
           FROM resource_samples WHERE run_id=?""",
        (run_id,),
    ).fetchone()

    # 상위 디렉터리(용량순). 집계 전이면 own_bytes 기준으로 대체 표시.
    top_rows = conn.execute(
        """SELECT id, path, name, depth, file_count, subdir_count,
                  own_bytes, total_bytes, total_files, status
           FROM directories WHERE run_id=?
           ORDER BY (CASE WHEN total_bytes>0 THEN total_bytes ELSE own_bytes END) DESC
           LIMIT ?""",
        (run_id, top),
    ).fetchall()
    top_dirs = [dict(t) for t in top_rows]

    return {
        "ok": True,
        "run_id": run_id,
        "have_psutil": monmod.have_psutil(),
        "server_time": now,
        "run": {
            "root_path": r["root_path"],
            "status": r["status"],
            "phase": r["phase"],
            "backend": r["backend"],
            "size_mode": r["size_mode"],
            "started_at": started,
            "finished_at": finished,
            "elapsed": elapsed,
            "discovered_dirs": discovered,
            "total_dirs": total_dirs,
            "processed_dirs": processed,
            "error_dirs": r.get("error_dirs") or 0,
            "scanned_bytes": scanned_bytes,
            "total_files": r.get("total_files") or 0,
            "current_dir": r.get("current_dir"),
            "current_depth": r.get("current_depth") or 0,
            "max_depth": r.get("max_depth") or 0,
            "fs_total_bytes": fs_total,
            "fs_used_bytes": fs_used,
            "fs_free_bytes": r.get("fs_free_bytes") or 0,
            "scanner_pid": r.get("scanner_pid"),
            "error": r.get("error"),
        },
        "progress": {
            "dir_progress_pct": dir_progress,
            "disk_verified_pct_of_used": _safe_pct(scanned_bytes, fs_used),
            "disk_verified_pct_of_total": _safe_pct(scanned_bytes, fs_total),
            "eta_seconds": eta,
        },
        "resources": {
            "latest": latest,
            "series": series,
            "peak_scanner_rss": int(peak["s"]) if peak else 0,
            "peak_du_rss": int(peak["d"]) if peak else 0,
            "peak_mem_percent": float(peak["m"]) if peak else 0.0,
        },
        "top_dirs": top_dirs,
    }


def list_children(conn, run_id: int, parent_id: int | None, *, limit: int = 200) -> dict:
    if parent_id is None:
        rows = conn.execute(
            """SELECT id, path, name, depth, file_count, subdir_count,
                      own_bytes, total_bytes, total_files, status
               FROM directories WHERE run_id=? AND parent_id IS NULL
               ORDER BY total_bytes DESC LIMIT ?""",
            (run_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, path, name, depth, file_count, subdir_count,
                      own_bytes, total_bytes, total_files, status
               FROM directories WHERE run_id=? AND parent_id=?
               ORDER BY total_bytes DESC LIMIT ?""",
            (run_id, parent_id, limit),
        ).fetchall()
    return {"ok": True, "children": [dict(r) for r in rows]}


def list_runs(conn) -> dict:
    rows = conn.execute(
        """SELECT id, root_path, status, phase, started_at, finished_at,
                  total_dirs, processed_dirs, scanned_bytes
           FROM scan_runs ORDER BY id DESC LIMIT 50"""
    ).fetchall()
    return {"ok": True, "runs": [dict(r) for r in rows]}


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = ""  # 서버 생성 시 클래스 속성으로 주입

    # 표준 로깅을 조용히(대시보드 폴링이 잦아 콘솔이 시끄러워짐)
    def log_message(self, fmt, *args):  # noqa: D401
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "dashboard.html not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query_int(self, qs: dict, key: str):
        if key in qs and qs[key]:
            try:
                return int(qs[key][0])
            except ValueError:
                return None
        return None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_html(DASHBOARD_HTML)
            return

        if not path.startswith("/api/"):
            self.send_error(404, "not found")
            return

        conn = dbmod.connect(self.db_path)
        try:
            if path == "/api/status":
                run_id = self._query_int(qs, "run_id")
                payload = build_status(conn, run_id)
                self._send_json(payload)
            elif path == "/api/children":
                run_id = self._query_int(qs, "run_id") or dbmod.latest_run_id(conn)
                parent_id = self._query_int(qs, "parent")
                if run_id is None:
                    self._send_json({"ok": False, "reason": "no_runs"})
                else:
                    self._send_json(list_children(conn, run_id, parent_id))
            elif path == "/api/runs":
                self._send_json(list_runs(conn))
            else:
                self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except Exception as exc:  # API 오류가 서버를 죽이지 않도록
            self._send_json({"ok": False, "error": str(exc)}, status=500)
        finally:
            conn.close()


def serve(db_path: str, host: str = "0.0.0.0", port: int = 8765) -> ThreadingHTTPServer:
    """대시보드 HTTP 서버를 만들고 반환한다(호출 측에서 serve_forever)."""
    dbmod.init_db(db_path)
    handler = type("BoundHandler", (DashboardHandler,), {"db_path": db_path})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd

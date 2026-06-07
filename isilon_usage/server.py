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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import db as dbmod
from . import monitor as monmod
from . import manager as mgrmod
from .scanner import run_scan


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
        "version": __version__,
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


# ---------------------------------------------------------------------------
# 마운트 목록 / 폴더 탐색 (웹에서 스캔할 디렉터리를 고르기 위함)
# ---------------------------------------------------------------------------

# 아이실론을 다른 서버에 붙일 때 흔한 마운트 종류
_NET_FSTYPES = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.glusterfs",
                "fuse", "autofs", "lustre", "beegfs"}


def list_mounts() -> list:
    """/proc/mounts 를 읽어 마운트 지점을 돌려준다(네트워크 FS 우선 표시)."""
    out = []
    try:
        with open("/proc/mounts", "r") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    seen = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if mnt in seen:
            continue
        seen.add(mnt)
        is_net = fstype in _NET_FSTYPES or fstype.startswith("fuse.")
        # 의사 파일시스템은 제외(스캔 대상이 아님)
        if fstype in ("proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup",
                      "cgroup2", "mqueue", "securityfs", "debugfs", "tracefs",
                      "pstore", "bpf", "configfs", "hugetlbfs", "ramfs", "rpc_pipefs"):
            continue
        out.append({"device": dev, "mountpoint": mnt, "fstype": fstype, "is_network": is_net})
    # 네트워크 FS 를 먼저, 그다음 경로순
    out.sort(key=lambda m: (not m["is_network"], m["mountpoint"]))
    return out


def browse_dir(path: str | None) -> dict:
    """주어진 경로의 바로 아래 하위 디렉터리 목록을 돌려준다(폴더 선택용).

    파일 내용은 읽지 않고 디렉터리 이름만 나열한다. 경로가 없으면 루트(/)부터.
    """
    if not path:
        path = "/"
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"ok": False, "reason": "not_a_directory", "path": path}
    parent = os.path.dirname(path.rstrip("/")) or "/"
    dirs = []
    err = None
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
    except OSError as exc:
        err = f"{type(exc).__name__}: {exc}"
    dirs.sort(key=lambda d: d["name"].lower())
    return {"ok": True, "path": path, "parent": parent, "dirs": dirs, "error": err}


class ScanController:
    """웹에서 시작/중지하는 스캔을 관리한다(서버 프로세스 안에서 스레드로 실행).

    mount_bases 가 지정되면, 그 경로(들) 아래의 디렉터리만 웹에서 스캔할 수 있다.
    지정하지 않으면(빈 목록) 어떤 디렉터리든 허용한다.
    """

    def __init__(self, data_dir: str, *, mount_bases=None, sample_interval: float = 2.0,
                 batch_size: int = 500):
        self.data_dir = data_dir
        self.mount_bases = [os.path.abspath(b) for b in (mount_bases or [])]
        self.sample_interval = sample_interval
        self.batch_size = batch_size
        self._scans: dict[int, dict] = {}   # manager scan_id -> {stop, thread}
        self._lock = threading.Lock()

    # ----- 경로 허용 검사 -----
    def path_allowed(self, path: str):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            return False, "디렉터리가 아니거나 접근할 수 없습니다."
        if self.mount_bases:
            for base in self.mount_bases:
                try:
                    if os.path.commonpath([path, base]) == base:
                        return True, None
                except ValueError:
                    continue
            return False, ("허용된 마운트 경로 밖입니다. 허용: "
                           + ", ".join(self.mount_bases))
        return True, None

    # ----- 시작 -----
    def start_scan(self, path: str, *, backend="native", size_mode="disk",
                   one_file_system=False) -> dict:
        path = os.path.abspath(path)
        ok, reason = self.path_allowed(path)
        if not ok:
            return {"ok": False, "reason": reason}
        if backend not in ("native", "du"):
            backend = "native"
        if size_mode not in ("disk", "apparent"):
            size_mode = "disk"

        mgrmod.init_manager(self.data_dir)
        db_path = mgrmod.make_run_db_path(self.data_dir, path)
        scan_id = mgrmod.register_scan(
            self.data_dir, root_path=path, db_path=db_path,
            backend=backend, size_mode=size_mode,
        )
        stop = threading.Event()

        def worker():
            try:
                run_scan(
                    db_path, path,
                    backend=backend, size_mode=size_mode,
                    one_file_system=one_file_system,
                    batch_size=self.batch_size,
                    sample_interval=self.sample_interval,
                    stop_event=stop, with_monitor=True,
                    manager_db=mgrmod.manager_db_path(self.data_dir),
                    manager_scan_id=scan_id,
                )
            except Exception as exc:
                try:
                    mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
                    mgrmod.update_scan(mconn, scan_id, status="error",
                                       phase="error", error=str(exc),
                                       finished_at=time.time())
                    mconn.commit(); mconn.close()
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._scans.pop(scan_id, None)

        t = threading.Thread(target=worker, name=f"scan-{scan_id}", daemon=True)
        with self._lock:
            self._scans[scan_id] = {"stop": stop, "thread": t}
        t.start()
        return {"ok": True, "scan_id": scan_id, "db_path": db_path,
                "root_path": path, "backend": backend, "size_mode": size_mode}

    # ----- 중지 -----
    def stop_scan(self, scan_id: int) -> dict:
        with self._lock:
            rec = self._scans.get(scan_id)
        if not rec:
            return {"ok": False, "reason": "실행 중인 스캔이 아닙니다(이미 끝났을 수 있음)."}
        rec["stop"].set()
        return {"ok": True, "scan_id": scan_id}

    def running_ids(self) -> list:
        with self._lock:
            return sorted(self._scans.keys())

    def stop_all(self):
        with self._lock:
            recs = list(self._scans.values())
        for rec in recs:
            rec["stop"].set()


class DashboardHandler(BaseHTTPRequestHandler):
    data_dir = ""  # 서버 생성 시 클래스 속성으로 주입(manager.db + scans/ 의 상위 폴더)
    controller = None  # ScanController (웹에서 스캔 시작/중지)

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

    def _resolve_scan_db(self, mconn, scan_id):
        """scan_id(없으면 기본 스캔)에 해당하는 per-run DB 경로와 manager 행을 반환."""
        if scan_id is None:
            scan_id = mgrmod.pick_default_scan(mconn)
        if scan_id is None:
            return None, None
        row = mgrmod.get_scan(mconn, scan_id)
        if row is None:
            return None, None
        return int(row["id"]), row

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

        mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
        try:
            if path == "/api/scans":
                ctrl = self.controller
                self._send_json({
                    "ok": True,
                    "version": __version__,
                    "overall": mgrmod.overall_capacity(mconn),
                    "scans": mgrmod.list_scans(mconn),
                    "running": ctrl.running_ids() if ctrl else [],
                    "mount_bases": ctrl.mount_bases if ctrl else [],
                    "can_scan": ctrl is not None,
                    "have_psutil": monmod.have_psutil(),
                })
                return

            if path == "/api/mounts":
                self._send_json({
                    "ok": True,
                    "mounts": list_mounts(),
                    "mount_bases": self.controller.mount_bases if self.controller else [],
                })
                return

            if path == "/api/browse":
                qpath = qs.get("path", [""])[0]
                if not qpath and self.controller and self.controller.mount_bases:
                    qpath = self.controller.mount_bases[0]
                self._send_json(browse_dir(qpath))
                return

            if path == "/api/status":
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None:
                    self._send_json({"ok": False, "reason": "no_runs",
                                     "have_psutil": monmod.have_psutil()})
                    return
                scan_meta = dict(row)
                db_path = scan_meta["db_path"]
                if not os.path.exists(db_path):
                    self._send_json({"ok": False, "reason": "initializing",
                                     "scan_id": scan_id, "scan_meta": scan_meta})
                    return
                pconn = dbmod.connect(db_path)
                try:
                    payload = build_status(pconn, None)
                finally:
                    pconn.close()
                payload["scan_id"] = scan_id
                payload["scan_meta"] = scan_meta
                self._send_json(payload)
                return

            if path == "/api/children":
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None:
                    self._send_json({"ok": False, "reason": "no_runs"})
                    return
                db_path = row["db_path"]
                if not os.path.exists(db_path):
                    self._send_json({"ok": True, "children": []})
                    return
                pconn = dbmod.connect(db_path)
                try:
                    run_id = dbmod.latest_run_id(pconn)
                    parent_id = self._query_int(qs, "parent")
                    if run_id is None:
                        self._send_json({"ok": True, "children": []})
                    else:
                        self._send_json(list_children(pconn, run_id, parent_id))
                finally:
                    pconn.close()
                return

            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except Exception as exc:  # API 오류가 서버를 죽이지 않도록
            self._send_json({"ok": False, "error": str(exc)}, status=500)
        finally:
            mconn.close()

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if not length:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if self.controller is None:
            self._send_json({"ok": False,
                             "reason": "이 서버는 웹 스캔이 비활성화되어 있습니다."},
                            status=403)
            return
        body = self._read_json_body()
        try:
            if path == "/api/scan/start":
                target = (body.get("path") or "").strip()
                if not target:
                    self._send_json({"ok": False, "reason": "경로를 입력하세요."},
                                    status=400)
                    return
                result = self.controller.start_scan(
                    target,
                    backend=body.get("backend", "native"),
                    size_mode=body.get("size_mode", "disk"),
                    one_file_system=bool(body.get("one_file_system", False)),
                )
                self._send_json(result, status=200 if result.get("ok") else 400)
                return

            if path == "/api/scan/stop":
                scan_id = body.get("scan_id")
                try:
                    scan_id = int(scan_id)
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "reason": "scan_id 필요"}, status=400)
                    return
                self._send_json(self.controller.stop_scan(scan_id))
                return

            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def serve(data_dir: str, host: str = "0.0.0.0", port: int = 8765, *,
          mount_bases=None, enable_scan: bool = True,
          sample_interval: float = 2.0, batch_size: int = 500) -> ThreadingHTTPServer:
    """대시보드 HTTP 서버를 만들고 반환한다(호출 측에서 serve_forever).

    data_dir 아래의 manager.db(전체 관리)와 scans/ 의 per-run DB(상세)를 읽는다.
    enable_scan=True 이면 웹에서 스캔을 시작/중지할 수 있고, mount_bases 로
    스캔 허용 경로를 제한할 수 있다. httpd.controller 로 컨트롤러에 접근한다.
    """
    mgrmod.init_manager(data_dir)
    controller = (ScanController(data_dir, mount_bases=mount_bases,
                                 sample_interval=sample_interval,
                                 batch_size=batch_size)
                  if enable_scan else None)
    handler = type("BoundHandler", (DashboardHandler,),
                   {"data_dir": data_dir, "controller": controller})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller  # run 모드에서 초기 스캔 시작/정리에 사용
    return httpd

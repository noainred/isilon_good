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

from typing import Dict, Optional

import gzip
import hmac
import io
import json
import os
import socket
import sqlite3
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

try:  # ThreadingHTTPServer 는 Python 3.7+ 에만 있음 — 3.6 폴백
    from http.server import ThreadingHTTPServer  # novermin
except ImportError:  # pragma: no cover
    import socketserver

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

from . import __version__, SCHEMA_VERSION
from . import db as dbmod
from . import monitor as monmod
from . import manager as mgrmod
from . import settings as setmod
from . import isilon_api as isilonmod
from . import powerstore_api as powerstoremod
from . import storage_status as storagemod
from .scanner import run_scan


HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(HERE, "dashboard.html")


def _safe_pct(num: float, den: float) -> float:
    if not den:
        return 0.0
    return round(num / den * 100.0, 2)


def _human_bytes(n) -> str:
    n = float(n or 0)
    for u in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024 or u == "PB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def _path_readonly(path: str) -> bool:
    """경로가 속한 마운트가 읽기 전용(ro)인지 확인한다(statvfs ST_RDONLY)."""
    try:
        return bool(os.statvfs(path).f_flag & getattr(os, "ST_RDONLY", 1))
    except OSError:
        return False


def _parse_worker_dirs(s):
    """scan_runs.worker_dirs(JSON 문자열) → 리스트(안전 파싱)."""
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _db_disk_usage(db_path: str) -> dict:
    """per-run DB 의 디스크 사용량(.db + -wal + -shm)을 바이트로 반환.

    이 값이 폴링마다 커지면 스캐너가 실제로 DB 에 쓰는 중(=살아있음)이라는
    구체적 지표가 된다. db_max_gb 한도와 함께 보면 DB 폭증도 감지할 수 있다.
    """
    def _sz(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    db = _sz(db_path)
    wal = _sz(db_path + "-wal")
    shm = _sz(db_path + "-shm")
    return {"db_bytes": db, "wal_bytes": wal, "shm_bytes": shm,
            "total_bytes": db + wal + shm}


def _dir_disk_free(path: str) -> Optional[dict]:
    """경로가 속한 (로컬) 파일시스템의 총/여유/사용률을 반환. 실패 시 None."""
    try:
        # 파일 경로면 그 디렉터리, 없으면 가장 가까운 상위 존재 경로로 statvfs
        p = path
        for _ in range(8):
            if p and os.path.exists(p):
                break
            p = os.path.dirname(p) or "/"
        v = os.statvfs(p or "/")
        total = v.f_blocks * v.f_frsize
        free = v.f_bavail * v.f_frsize
        used = max(0, total - free)
        return {"path": path, "total_bytes": total, "free_bytes": free,
                "used_bytes": used,
                "used_pct": round(used / total * 100, 1) if total else 0.0}
    except OSError:
        return None


def _public_settings(s: dict) -> dict:
    """화면/응답용 설정 — 비밀번호는 노출하지 않고 설정 여부만 알린다."""
    out = dict(s)
    out["smtp_password"] = ""
    out["smtp_password_set"] = bool((s or {}).get("smtp_password"))
    out["api_token"] = ""
    out["api_token_set"] = bool((s or {}).get("api_token"))
    out["isilon_password"] = ""
    out["isilon_password_set"] = bool((s or {}).get("isilon_password"))
    out["powerstore_password"] = ""
    out["powerstore_password_set"] = bool((s or {}).get("powerstore_password"))
    sa = []
    for a in ((s or {}).get("storage_arrays") or []):
        b = dict(a)
        b["password"] = ""
        b["password_set"] = bool(a.get("password"))
        sa.append(b)
    out["storage_arrays"] = sa
    return out


def build_status(conn, run_id: Optional[int], *, samples: int = 150, top: int = 20) -> dict:
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

    # 최신 자원 샘플 + 시계열(스파크라인용). 구버전 DB(scanner_cpu/du_cpu 없음) 호환.
    _cols = ("ts, mem_percent, mem_used, mem_total, swap_used, cpu_percent, "
             "load1, scanner_rss, du_rss, du_pid, scanner_cpu, du_cpu")
    try:
        sample_rows = conn.execute(
            f"SELECT {_cols} FROM resource_samples WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, samples),
        ).fetchall()
    except Exception:
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
                # 프로세스별 CPU% 는 델타 기반이라 즉석 수집이 어려워 최근 기록값 사용
                live["scanner_cpu"] = recorded_latest.get("scanner_cpu", 0)
                live["du_cpu"] = recorded_latest.get("du_cpu", 0)
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
    # top<=0 이면 건너뛴다(대시보드는 /api/topdirs 를 따로 쓰므로, 거대 DB 에서
    # 매 폴링마다 전체 정렬하는 이 쿼리를 생략해 /api/status 를 빠르게 유지).
    if top and int(top) > 0:
        top_rows = conn.execute(
            """SELECT id, path, name, depth, file_count, subdir_count,
                      own_bytes, total_bytes, total_files, status
               FROM directories WHERE run_id=?
               ORDER BY (CASE WHEN total_bytes>0 THEN total_bytes ELSE own_bytes END) DESC
               LIMIT ?""",
            (run_id, int(top)),
        ).fetchall()
        top_dirs = [dict(t) for t in top_rows]
    else:
        top_dirs = []

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
            "updated_at": r.get("updated_at"),   # 마지막 진행 갱신(하트비트)
            "session_started_at": r.get("session_started_at"),  # 이번(재시작) 세션 시작
            "elapsed_accum": r.get("elapsed_accum") or 0,       # 모든 세션 누적 활성시간
            "elapsed": elapsed,
            "discovered_dirs": discovered,
            "total_dirs": total_dirs,
            "processed_dirs": processed,
            "error_dirs": r.get("error_dirs") or 0,
            "scanned_bytes": scanned_bytes,
            "total_files": r.get("total_files") or 0,
            "current_dir": r.get("current_dir"),
            "worker_dirs": _parse_worker_dirs(r.get("worker_dirs")),
            "current_depth": r.get("current_depth") or 0,
            "max_depth": r.get("max_depth") or 0,
            "workers": r.get("workers") or 0,             # 설정된 동시 스캔 스레드 수
            "active_workers": r.get("active_workers") or 0,  # 현재 병렬 처리 중인 워커 수
            # 마운트 읽기전용: -1=확인 안 함(설정 끔), 0=쓰기가능, 1=읽기전용
            "mount_readonly": (r.get("mount_readonly") is not None
                               and int(r.get("mount_readonly")) == 1),
            "readonly_checked": (r.get("mount_readonly") is not None
                                 and int(r.get("mount_readonly")) >= 0),
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


def list_children(conn, run_id: int, parent_id: Optional[int], *, limit: int = 200) -> dict:
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


# 상위 디렉터리 표의 정렬 가능한 컬럼(화이트리스트 — SQL 주입 방지)
_TOP_SORT_COLS = {
    "size": "(CASE WHEN total_bytes>0 THEN total_bytes ELSE own_bytes END)",
    "own": "own_bytes",
    "files": "(CASE WHEN total_files>0 THEN total_files ELSE file_count END)",
    "subdirs": "subdir_count",
    "depth": "depth",
    "path": "path",
}


def list_top_dirs(conn, run_id, *, under_id=None, rel_depth=None,
                  sort="size", order="desc", limit=50) -> dict:
    """용량 상위 디렉터리를 조건에 맞춰 돌려준다.

    - under_id: 이 디렉터리 하위로만 한정(없으면 스캔 루트 전체).
    - rel_depth: 기준(루트 또는 under) 대비 상대 깊이. 1=직속 자식, 2=손자… .
                 0/None 이면 깊이 제한 없이 하위 전체.
    - sort/order: 정렬 컬럼(_TOP_SORT_COLS)과 방향(asc/desc).
    """
    sort_expr = _TOP_SORT_COLS.get(str(sort), _TOP_SORT_COLS["size"])
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    _cols = ("id, path, name, depth, file_count, subdir_count, "
             "own_bytes, total_bytes, total_files, status")

    where = ["run_id=?"]
    params = [run_id]
    under_depth = 0
    under_path = None
    parent = None   # 자식들을 나열하는 기준 디렉터리(파이/요약에서 개수·자기파일 용량 사용)
    if under_id is not None:
        urow = conn.execute(
            "SELECT " + _cols + " FROM directories WHERE run_id=? AND id=?",
            (run_id, under_id),
        ).fetchone()
        if urow is not None:
            parent = dict(urow)
            under_depth = int(urow["depth"])
            under_path = urow["path"]
            like = (under_path.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_")).rstrip("/")
            where.append("path LIKE ? ESCAPE '\\'")
            params.append(like + "/%")
    else:
        rrow = conn.execute(
            "SELECT " + _cols + " FROM directories WHERE run_id=? AND parent_id IS NULL "
            "ORDER BY depth ASC, id ASC LIMIT 1", (run_id,),
        ).fetchone()
        if rrow is not None:
            parent = dict(rrow)
    if rel_depth:
        try:
            where.append("depth=?")
            params.append(under_depth + int(rel_depth))
        except (TypeError, ValueError):
            pass

    params.append(int(limit))
    rows = conn.execute(
        "SELECT " + _cols + " FROM directories "
        "WHERE " + " AND ".join(where) +
        " ORDER BY " + sort_expr + " " + order_sql + ", id ASC LIMIT ?",
        params,
    ).fetchall()
    return {"ok": True, "rows": [dict(r) for r in rows], "parent": parent,
            "under_id": under_id, "under_path": under_path,
            "under_depth": under_depth, "sort": sort, "order": order_sql.lower()}


def search_dirs(pconn, run_id: int, q: str, *, limit: int = 200) -> dict:
    rows = pconn.execute(
        """SELECT id, path, name, depth, file_count, subdir_count,
                  own_bytes, total_bytes, total_files, status
           FROM directories WHERE run_id=? AND path LIKE ? ESCAPE '\\'
           ORDER BY total_bytes DESC LIMIT ?""",
        (run_id, "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", limit),
    ).fetchall()
    return {"ok": True, "results": [dict(r) for r in rows]}


def list_errors(pconn, run_id: int, *, limit: int = 1000) -> dict:
    rows = pconn.execute(
        """SELECT path, error, depth FROM directories
           WHERE run_id=? AND error IS NOT NULL ORDER BY path LIMIT ?""",
        (run_id, limit),
    ).fetchall()
    return {"ok": True, "errors": [dict(r) for r in rows]}


def diff_scans(data_dir: str, base_id: int, target_id: int, *,
               limit: int = 100, max_depth: Optional[int] = None) -> dict:
    """두 스캔(같은/다른 per-run DB)의 디렉터리별 재귀 용량 변화를 비교한다."""
    mconn = dbmod.connect(mgrmod.manager_db_path(data_dir))
    try:
        base = mgrmod.get_scan(mconn, base_id)
        target = mgrmod.get_scan(mconn, target_id)
    finally:
        mconn.close()
    if not base or not target:
        return {"ok": False, "reason": "scan 을 찾을 수 없습니다."}
    if not (os.path.exists(base["db_path"]) and os.path.exists(target["db_path"])):
        return {"ok": False, "reason": "per-run DB 가 없습니다."}

    conn = dbmod.connect(":memory:")
    try:
        conn.execute("ATTACH DATABASE ? AS a", (base["db_path"],))
        conn.execute("ATTACH DATABASE ? AS b", (target["db_path"],))
        a_run = conn.execute("SELECT MAX(id) AS i FROM a.scan_runs").fetchone()["i"]
        b_run = conn.execute("SELECT MAX(id) AS i FROM b.scan_runs").fetchone()["i"]
        depth_a = f"AND depth<={int(max_depth)}" if max_depth is not None else ""
        depth_b = depth_a
        rows = conn.execute(
            f"""
            SELECT path, SUM(ab) AS ab, SUM(bb) AS bb FROM (
                SELECT path, total_bytes AS ab, 0 AS bb
                  FROM a.directories WHERE run_id=? {depth_a}
                UNION ALL
                SELECT path, 0 AS ab, total_bytes AS bb
                  FROM b.directories WHERE run_id=? {depth_b}
            ) GROUP BY path
            ORDER BY ABS(SUM(bb)-SUM(ab)) DESC LIMIT ?
            """,
            (a_run, b_run, limit),
        ).fetchall()
        result = [{"path": r["path"], "base_bytes": int(r["ab"]),
                   "target_bytes": int(r["bb"]),
                   "delta": int(r["bb"]) - int(r["ab"])} for r in rows]
        return {
            "ok": True,
            "base": {"scan_id": base_id, "scanned_bytes": base["scanned_bytes"],
                     "started_at": base["started_at"]},
            "target": {"scan_id": target_id, "scanned_bytes": target["scanned_bytes"],
                       "started_at": target["started_at"]},
            "total_delta": (target["scanned_bytes"] or 0) - (base["scanned_bytes"] or 0),
            "rows": result,
        }
    finally:
        conn.close()


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


def browse_dir(path: Optional[str]) -> dict:
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

    설정(mount_bases, 기본 백엔드, 배치 크기, 샘플링 주기 등)은 settings.py 가
    `<data-dir>/settings.json` 에 보관하며 대시보드에서 수정한다.
    mount_bases 가 비어 있으면 어떤 디렉터리든 스캔 허용.
    """

    def __init__(self, data_dir: str, *, lock_settings: bool = False):
        self.data_dir = data_dir
        self.lock_settings = lock_settings
        self.settings = setmod.load(data_dir)
        self._scans: Dict[int, dict] = {}   # manager scan_id -> {stop, thread}
        self._lock = threading.Lock()
        self._storage_cache = None          # 스토리지 어레이(아이실론/PowerStore) 상태 캐시
        self._storage_ts = 0.0
        self._reconcile_orphans()           # 이전 프로세스가 남긴 고아 스캔 정리

    def storage_status(self, force: bool = False) -> list:
        """설정된 스토리지 어레이(아이실론/PowerStore) 상태 목록을 캐시(60초)와 함께 반환."""
        now = time.time()
        if (not force) and self._storage_cache is not None and (now - self._storage_ts) < 60:
            return self._storage_cache
        s = self.settings
        arrays = []
        if (s.get("isilon_url") or "").strip():
            arrays.append(isilonmod.cluster_status(
                s.get("isilon_url", ""), s.get("isilon_user", ""), s.get("isilon_password", ""),
                verify_ssl=bool(s.get("isilon_verify_ssl", False)), timeout=10.0))
        if (s.get("powerstore_url") or "").strip():
            arrays.append(powerstoremod.cluster_status(
                s.get("powerstore_url", ""), s.get("powerstore_user", ""),
                s.get("powerstore_password", ""),
                verify_ssl=bool(s.get("powerstore_verify_ssl", False)), timeout=10.0))
        for arr in (s.get("storage_arrays") or []):   # Unity/PowerMax/VMAX/XtremIO/VPLEX 등
            arrays.append(storagemod.query(arr))
        self._storage_cache = arrays
        self._storage_ts = now
        return arrays

    def isilon_status(self, force: bool = False) -> dict:
        """back-compat: 아이실론 항목만 반환(/api/isilon)."""
        for a in self.storage_status(force=force):
            if a.get("type") == "isilon":
                return a
        return {"ok": False, "configured": False, "type": "isilon", "error": "미설정"}

    def _reconcile_orphans(self) -> None:
        """서버 시작 시: 실제로 실행 중이 아닌데 상태가 '진행 중'(discovering/sizing)
        으로 남은 스캔을 '일시정지'로 정리한다.

        대시보드 서버를 재시작하면 이전 프로세스에서 돌던 스캔 스레드가 죽지만 DB
        상태는 '탐색중'으로 남아, 개요에는 탐색중인데 실제로는 아무것도 안 도는
        모순이 생긴다. 시작 시 한 번 정리해 일관성을 맞추고 '재개'로 이어갈 수 있게 한다.
        """
        try:
            mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
        except Exception:
            return
        try:
            for s in mgrmod.list_scans(mconn):
                if s.get("status") not in ("discovering", "sizing"):
                    continue
                sid = int(s["id"])
                if sid in self._scans:   # 이 컨트롤러가 실제로 돌리는 중이면 건드리지 않음
                    continue
                mgrmod.update_scan(mconn, sid, status="paused", phase="paused")
                dbp = s.get("db_path")
                if dbp and os.path.exists(dbp):
                    try:
                        pc = dbmod.connect(dbp)
                        rid = dbmod.latest_run_id(pc)
                        if rid is not None:
                            dbmod.update_run(pc, rid, status="paused", phase="paused")
                            pc.commit()
                        pc.close()
                    except Exception:
                        pass
            mconn.commit()
        except Exception:
            pass
        finally:
            mconn.close()

    # 설정에서 파생되는 값들(편집되면 즉시 반영)
    @property
    def mount_bases(self):
        return self.settings.get("mount_bases", [])

    @property
    def sample_interval(self) -> float:
        return float(self.settings.get("sample_interval", 2.0))

    @property
    def batch_size(self) -> int:
        return int(self.settings.get("batch_size", 500))

    def update_settings(self, new: dict) -> dict:
        """설정을 저장하고 갱신된 설정을 반환한다(잠겨 있으면 거부)."""
        if self.lock_settings:
            return {"ok": False, "reason": "설정이 잠겨 있습니다(--lock-settings)."}
        new = dict(new or {})
        # 비밀번호/토큰은 화면에 노출하지 않으므로(마스킹), 빈 값으로 오면 기존 값 유지
        if not (new.get("smtp_password") or "").strip():
            new.pop("smtp_password", None)
        if not (new.get("api_token") or "").strip():
            new.pop("api_token", None)
        if not (new.get("isilon_password") or "").strip():
            new.pop("isilon_password", None)
        if not (new.get("powerstore_password") or "").strip():
            new.pop("powerstore_password", None)
        # 어레이 목록: 빈 비밀번호는 기존 값(같은 id) 유지
        if isinstance(new.get("storage_arrays"), list):
            cur = {a.get("id"): a for a in (self.settings.get("storage_arrays") or [])}
            for a in new["storage_arrays"]:
                if isinstance(a, dict) and not (a.get("password") or "").strip():
                    old = cur.get(a.get("id")) or cur.get(a.get("name")) or cur.get(a.get("url"))
                    if old:
                        a["password"] = old.get("password", "")
        merged = dict(self.settings)
        merged.update({k: v for k, v in new.items() if k in setmod.EDITABLE_KEYS})
        self.settings = setmod.save(self.data_dir, merged)
        return {"ok": True, "settings": _public_settings(self.settings)}

    def _log(self, msg: str) -> None:
        """log_path 가 설정돼 있으면 한 줄 추가한다(베스트 에포트)."""
        p = self.settings.get("log_path")
        if not p:
            return
        try:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except Exception:
            pass

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

    # ----- 실측 워커 보정(시범 탐색) -----
    def benchmark_workers(self, *, path=None, candidates=None, budget=5.0) -> dict:
        """대상 경로에서 후보 스레드 수로 짧게 시범 탐색해 처리량을 비교한다."""
        from . import tuning as tunmod
        if not path:
            path = self.mount_bases[0] if self.mount_bases else None
        if not path:
            return {"ok": False, "error": "측정할 경로를 지정하세요(허용 경로 없음)."}
        ok, why = self.path_allowed(path)
        if not ok:
            return {"ok": False, "error": why}
        if isinstance(candidates, str):
            candidates = [c for c in candidates.split(",") if c.strip()]
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            budget = 5.0
        return tunmod.benchmark_workers(path, candidates=candidates, budget_sec=budget)

    # ----- 시작 -----
    def start_scan(self, path: str, *, backend=None, size_mode=None,
                   one_file_system=None) -> dict:
        path = os.path.abspath(path)
        ok, reason = self.path_allowed(path)
        if not ok:
            return {"ok": False, "reason": reason}
        # 지정하지 않은 옵션은 설정의 기본값을 사용
        if backend is None:
            backend = self.settings.get("default_backend", "native")
        if size_mode is None:
            size_mode = self.settings.get("default_size_mode", "disk")
        if one_file_system is None:
            one_file_system = bool(self.settings.get("default_one_file_system", False))
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
        readonly = _path_readonly(path) if self.settings.get("check_readonly", True) else None
        self._log("scan #%d start: %s (backend=%s, size=%s, x=%s, ro=%s)" % (
            scan_id, path, backend, size_mode, one_file_system, readonly))
        self._launch(scan_id, db_path, path, backend, size_mode,
                     one_file_system, resume=False)
        return {"ok": True, "scan_id": scan_id, "db_path": db_path,
                "root_path": path, "backend": backend, "size_mode": size_mode,
                "mount_readonly": readonly}

    def _launch(self, scan_id, db_path, path, backend, size_mode,
                one_file_system, *, resume: bool) -> None:
        """워커 스레드에서 스캔을 실행하고, 끝나면 알림/보존 정리를 수행한다."""
        stop = threading.Event()

        def worker():
            try:
                run_scan(
                    db_path, path,
                    backend=backend, size_mode=size_mode,
                    one_file_system=one_file_system,
                    batch_size=self.batch_size, workers=self.workers,
                    check_readonly=bool(self.settings.get("check_readonly", True)),
                    max_depth=int(self.settings.get("scan_max_depth", 0) or 0),
                    fold_depth=int(self.settings.get("fold_depth", 0) or 0),
                    db_max_bytes=int(self.settings.get("db_max_gb", 0) or 0) * (1024 ** 3),
                    hardlink_dedup=bool(self.settings.get("hardlink_dedup", True)),
                    min_free_bytes=int(self.settings.get("min_free_gb", 0) or 0) * (1024 ** 3),
                    resume=resume, sample_interval=self.sample_interval,
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
                self._on_scan_finished(scan_id)

        t = threading.Thread(target=worker, name=f"scan-{scan_id}", daemon=True)
        with self._lock:
            self._scans[scan_id] = {"stop": stop, "thread": t, "path": path}
        t.start()

    @property
    def workers(self) -> int:
        return int(self.settings.get("scan_workers", 1))

    def _on_scan_finished(self, scan_id: int) -> None:
        """스캔 종료 후: 웹훅 알림 + 보존(retention) 자동 정리."""
        try:
            mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
            try:
                row = mgrmod.get_scan(mconn, scan_id)
            finally:
                mconn.close()
            if row is None:
                return
            human = _human_bytes(row["scanned_bytes"])
            self._log("scan #%d %s: %s (dirs=%s files=%s, %s)" % (
                scan_id, row["status"], row["root_path"],
                row["total_dirs"], row["total_files"], human))
            from . import notify as notifymod
            webhook = self.settings.get("notify_webhook", "")
            if webhook:
                notifymod.send(webhook, {
                    "event": "scan_finished",
                    "scan_id": scan_id,
                    "root_path": row["root_path"],
                    "status": row["status"],
                    "total_dirs": row["total_dirs"],
                    "total_files": row["total_files"],
                    "scanned_bytes": row["scanned_bytes"],
                    "scanned_human": human,
                    "hostname": row["hostname"],
                })
            # 완료/오류 메일
            if self.settings.get("notify_email") and self.settings.get("smtp_host"):
                subject = "[isilon_usage] 스캔 %s: %s" % (row["status"], row["root_path"])
                bodytxt = (
                    "스캔 #%d 결과\n\n"
                    "경로     : %s\n호스트   : %s\n상태     : %s\n"
                    "디렉터리 : %s\n파일 수  : %s\n조사 용량: %s\n"
                ) % (scan_id, row["root_path"], row["hostname"], row["status"],
                     row["total_dirs"], row["total_files"], human)
                notifymod.send_email(self.settings, subject, bodytxt)
            keep = int(self.settings.get("retention_per_root", 0))
            if keep > 0 and row["status"] in ("done", "error"):
                mgrmod.prune_scans(self.data_dir, keep_per_root=keep,
                                   running_ids=self.running_ids())
        except Exception:
            pass

    # ----- 재개 -----
    def resume_scan(self, scan_id: int) -> dict:
        if scan_id in self.running_ids():
            return {"ok": False, "reason": "이미 실행 중입니다."}
        mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
        try:
            row = mgrmod.get_scan(mconn, scan_id)
        finally:
            mconn.close()
        if row is None:
            return {"ok": False, "reason": "없는 scan"}
        if not os.path.exists(row["db_path"]):
            return {"ok": False, "reason": "per-run DB 가 없어 재개할 수 없습니다."}
        self._launch(scan_id, row["db_path"], row["root_path"], row["backend"],
                     row["size_mode"], False, resume=True)
        return {"ok": True, "scan_id": scan_id}

    # ----- 삭제 / 정리 -----
    def delete_scan(self, scan_id: int) -> dict:
        if scan_id in self.running_ids():
            return {"ok": False, "reason": "실행 중인 스캔은 삭제할 수 없습니다(먼저 중지)."}
        return mgrmod.delete_scan(self.data_dir, scan_id)

    def prune(self, *, keep_per_root=None, older_than_days=None) -> dict:
        return mgrmod.prune_scans(
            self.data_dir, keep_per_root=keep_per_root,
            older_than_days=older_than_days, running_ids=self.running_ids())

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

    def running_paths(self) -> set:
        with self._lock:
            return {rec.get("path") for rec in self._scans.values()}

    # ----- 예약 스캔 스케줄러 -----
    def start_scheduler(self) -> None:
        self._sched_stop = threading.Event()
        self._sched_thread = threading.Thread(
            target=self._scheduler_loop, name="scheduler", daemon=True)
        self._sched_thread.start()

    def _scheduler_loop(self) -> None:
        while not self._sched_stop.wait(20):
            try:
                self._check_schedules()
            except Exception:
                pass

    def _check_schedules(self) -> None:
        schedules = self.settings.get("schedules", [])
        if not schedules:
            return
        now = time.time()
        running_paths = self.running_paths()
        changed = False
        for sc in schedules:
            if not sc.get("enabled", True):
                continue
            if not setmod.schedule_due(sc, now):
                continue
            path = sc["path"]
            if path in running_paths:
                continue
            ok, _ = self.path_allowed(path)
            if not ok:
                continue
            res = self.start_scan(path, backend=sc.get("backend"),
                                  size_mode=sc.get("size_mode"),
                                  one_file_system=sc.get("one_file_system"))
            if res.get("ok"):
                sc["last_run"] = now
                changed = True
        if changed:
            # last_run 갱신을 settings.json 에 반영
            self.settings = setmod.save(self.data_dir, self.settings)

    def stop_all(self):
        sched_stop = getattr(self, "_sched_stop", None)
        if sched_stop is not None:
            sched_stop.set()
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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 브라우저가 응답 도중 연결을 끊음(잦은 폴링·새로고침·페이지 이동).
            # BrokenPipe/ConnectionReset 등은 정상 동작이므로 조용히 무시한다.
            pass

    def _send_html(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "dashboard.html not found")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 클라이언트 연결 끊김 — 무시

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

    def _current_settings(self) -> dict:
        if self.controller is not None:
            return self.controller.settings
        return setmod.load(self.data_dir)

    def _authorized(self, qs: dict) -> bool:
        """api_token 이 설정돼 있으면 X-Auth-Token 헤더(또는 ?token=)를 검증한다.

        토큰이 비어 있으면(미설정) 공개로 간주한다(기존 동작 유지).
        """
        tok = (self._current_settings().get("api_token") or "").strip()
        if not tok:
            return True
        given = self.headers.get("X-Auth-Token") or (qs.get("token", [""])[0] or "")
        return hmac.compare_digest(str(given), tok)

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

            if path == "/api/isilon":
                if self.controller is None:
                    self._send_json({"ok": False, "configured": False,
                                     "reason": "이 서버는 웹 스캔/설정이 비활성입니다."})
                else:
                    self._send_json(self.controller.isilon_status(
                        force=(qs.get("force", ["0"])[0] == "1")))
                return

            if path == "/api/storage":
                arrays = (self.controller.storage_status(force=(qs.get("force", ["0"])[0] == "1"))
                          if self.controller else [])
                self._send_json({"ok": True, "arrays": arrays})
                return

            if path == "/api/recommend-workers":
                # 서버 사양(CPU/메모리)을 보고 권장 동시 스캔 스레드 수를 계산.
                specs = monmod.system_specs()
                backend = (self._current_settings() or {}).get("default_backend", "native")
                rec = monmod.recommend_workers(specs, backend=backend)
                rec["ok"] = True
                self._send_json(rec)
                return

            if path == "/api/settings":
                self._send_json({
                    "ok": True,
                    "settings": _public_settings(self._current_settings()),
                    "editable": (self.controller is not None
                                 and not self.controller.lock_settings),
                    "locked": bool(self.controller and self.controller.lock_settings),
                    "server": {
                        "version": __version__,
                        "schema_version": SCHEMA_VERSION,
                        "data_dir": os.path.abspath(self.data_dir),
                        "host": getattr(self, "bound_host", None),
                        "port": getattr(self, "bound_port", None),
                        "have_psutil": monmod.have_psutil(),
                        "can_scan": self.controller is not None,
                    },
                })
                return

            if path == "/api/changelog":
                text = None
                for cand in (os.path.join(os.path.dirname(HERE), "CHANGELOG.md"),
                             os.path.join(os.getcwd(), "CHANGELOG.md")):
                    try:
                        with open(cand, "r", encoding="utf-8") as fh:
                            text = fh.read()
                        break
                    except OSError:
                        continue
                self._send_json({"ok": True, "version": __version__,
                                 "schema_version": SCHEMA_VERSION, "changelog": text})
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
                    # top=0: 상위 디렉터리 정렬(거대 DB 에서 느림)은 생략한다.
                    # 대시보드는 /api/topdirs(깊이 필터, 빠름)를 따로 쓰므로 불필요.
                    payload = build_status(pconn, None, top=0)
                except sqlite3.OperationalError:
                    # per-run DB 파일은 생겼지만 아직 테이블 생성 전(스캔 시작 직후 레이스).
                    # 500 대신 "초기화 중"으로 응답해 다음 폴링에 정상 표시되게 한다.
                    self._send_json({"ok": False, "reason": "initializing",
                                     "scan_id": scan_id, "scan_meta": scan_meta})
                    return
                finally:
                    pconn.close()
                payload["scan_id"] = scan_id
                payload["scan_meta"] = scan_meta
                # DB 디스크 사용량 + 한도 — 'DB 살아있는지' 지표로 요약에 표시
                payload["db_usage"] = _db_disk_usage(db_path)
                _st = self._current_settings() or {}
                payload["db_usage"]["limit_bytes"] = (
                    int(_st.get("db_max_gb", 0) or 0) * (1024 ** 3))
                # 로컬 저장 공간 여유: DB(데이터 폴더) + 로그 파일 위치
                local = {"db": _dir_disk_free(self.data_dir)}
                log_path = str(_st.get("log_path") or "").strip()
                if log_path:
                    log_disk = _dir_disk_free(log_path)
                    # DB 와 같은 파일시스템이면 중복 표시 안 함
                    if (log_disk and local["db"]
                            and log_disk["total_bytes"] == local["db"]["total_bytes"]
                            and log_disk["free_bytes"] == local["db"]["free_bytes"]):
                        log_disk = None
                    local["log"] = log_disk
                payload["local_disk"] = local
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

            if path == "/api/topdirs":
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None or not os.path.exists(row["db_path"]):
                    self._send_json({"ok": True, "rows": []})
                    return
                pconn = dbmod.connect(row["db_path"])
                try:
                    run_id = dbmod.latest_run_id(pconn)
                    if run_id is None:
                        self._send_json({"ok": True, "rows": []})
                    else:
                        top_n = int(self._current_settings().get("top_n", 20))
                        self._send_json(list_top_dirs(
                            pconn, run_id,
                            under_id=self._query_int(qs, "under"),
                            rel_depth=self._query_int(qs, "rel"),
                            sort=(qs.get("sort", ["size"])[0]),
                            order=(qs.get("order", ["desc"])[0]),
                            limit=top_n,
                        ))
                except sqlite3.OperationalError:
                    self._send_json({"ok": True, "rows": []})  # 초기화 전 레이스 — 빈 목록
                finally:
                    pconn.close()
                return

            if path in ("/api/search", "/api/errors"):
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None or not os.path.exists(row["db_path"]):
                    self._send_json({"ok": True, "results": [], "errors": []})
                    return
                pconn = dbmod.connect(row["db_path"])
                try:
                    run_id = dbmod.latest_run_id(pconn)
                    if path == "/api/search":
                        q = (qs.get("q", [""])[0] or "").strip()
                        self._send_json(search_dirs(pconn, run_id, q) if q
                                        else {"ok": True, "results": []})
                    else:
                        self._send_json(list_errors(pconn, run_id))
                finally:
                    pconn.close()
                return

            if path == "/api/diff":
                base = self._query_int(qs, "base")
                target = self._query_int(qs, "target")
                if base is None or target is None:
                    self._send_json({"ok": False, "reason": "base/target 필요"}, status=400)
                    return
                self._send_json(diff_scans(self.data_dir, base, target,
                                           max_depth=self._query_int(qs, "max_depth")))
                return

            if path == "/api/export":
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None or not os.path.exists(row["db_path"]):
                    self._send_json({"ok": False, "reason": "no_runs"}, status=404)
                    return
                self._export(row, fmt=(qs.get("format", ["csv"])[0]))
                return

            if path == "/api/dbexport":
                # 글로벌 포탈 복제용: 완료된 per-run DB + 노드 요약(meta.json)을 tar.gz 로.
                if not self._authorized(qs):
                    self._send_json({"ok": False, "reason": "unauthorized"}, status=401)
                    return
                try:
                    since = float(qs.get("since", ["0"])[0] or 0)
                except (TypeError, ValueError):
                    since = 0.0
                self._export_dbs(mconn, since=since)
                return

            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass  # 클라이언트가 응답 도중 연결을 끊음 — 무시
        except Exception as exc:  # API 오류가 서버를 죽이지 않도록
            self._send_json({"ok": False, "error": str(exc)}, status=500)
        finally:
            mconn.close()

    def _export(self, row, *, fmt: str = "csv") -> None:
        """선택한 스캔의 디렉터리 집계를 CSV/JSON 으로 스트리밍 다운로드.

        커서로 한 행씩 흘려보내 대용량에서도 메모리를 적게 쓴다.
        """
        pconn = dbmod.connect(row["db_path"])
        try:
            run_id = dbmod.latest_run_id(pconn)
            cur = pconn.execute(
                """SELECT path, depth, file_count, subdir_count, own_bytes,
                          total_bytes, total_files, status
                   FROM directories WHERE run_id=? ORDER BY total_bytes DESC""",
                (run_id,),
            )
            base = f"scan{row['id']}_{os.path.basename(row['db_filename'])}"
            if fmt == "json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{base}.json"')
                self.end_headers()
                self.wfile.write(b'{"directories":[')
                first = True
                for r in cur:
                    chunk = ("" if first else ",") + json.dumps(dict(r), ensure_ascii=False)
                    first = False
                    self.wfile.write(chunk.encode("utf-8"))
                self.wfile.write(b"]}")
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{base}.csv"')
                self.end_headers()
                self.wfile.write("﻿".encode("utf-8"))  # 엑셀 한글용 BOM
                self.wfile.write(b"path,depth,file_count,subdir_count,own_bytes,total_bytes,total_files,status\n")
                for r in cur:
                    p = '"' + str(r["path"]).replace('"', '""') + '"'
                    line = f'{p},{r["depth"]},{r["file_count"]},{r["subdir_count"]},{r["own_bytes"]},{r["total_bytes"]},{r["total_files"]},{r["status"]}\n'
                    self.wfile.write(line.encode("utf-8"))
        except OSError:
            pass  # 다운로드 도중 클라이언트가 연결을 끊음 — 무시
        finally:
            pconn.close()

    def _export_dbs(self, mconn, *, since: float = 0.0) -> None:
        """완료된 per-run DB(불변) + 노드 요약(meta.json)을 tar.gz 로 스트리밍한다.

        글로벌 포탈(HQ)이 이 번들을 받아 복제한다. 진행 중 스캔의 DB 는
        쓰기 중이라 제외하고, status 가 done/paused/error 인 것만(또는 since
        이후 완료분만) 보낸다. 완료된 DB 는 불변이라 그대로 복사해도 안전하다.
        (Python 3.6·구버전 SQLite 호환: backup API/VACUUM INTO 를 쓰지 않는다.)
        """
        scans = mgrmod.list_scans(mconn)
        overall = mgrmod.overall_capacity(mconn)
        terminal = ("done", "paused", "error")
        finished = [s for s in scans
                    if s.get("status") in terminal
                    and float(s.get("finished_at") or 0) > since]
        meta = {
            "hostname": socket.gethostname(),
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "exported_at": time.time(),
            "since": since,
            "overall": overall,
            "isilon": (self.controller.isilon_status() if self.controller else
                       {"ok": False, "configured": False}),
            "storage": (self.controller.storage_status() if self.controller else []),
            "scans": [dict(s, db_filename=os.path.basename(s.get("db_path") or ""))
                      for s in finished],
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Disposition", 'attachment; filename="dbexport.tar.gz"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            gz = gzip.GzipFile(fileobj=self.wfile, mode="wb")
            tar = tarfile.open(fileobj=gz, mode="w")
            mb = json.dumps(meta, ensure_ascii=False, default=str).encode("utf-8")
            ti = tarfile.TarInfo("meta.json")
            ti.size = len(mb)
            ti.mtime = int(time.time())
            tar.addfile(ti, io.BytesIO(mb))
            for s in finished:
                dbp = s.get("db_path")
                if not dbp or not os.path.exists(dbp):
                    continue
                # 완료 DB 의 WAL 을 본 파일로 합쳐 단일 파일로 복사(안전·일관)
                try:
                    c = dbmod.connect(dbp)
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    c.close()
                except Exception:
                    pass
                try:
                    tar.add(dbp, arcname="scans/" + os.path.basename(dbp))
                except OSError:
                    continue
            tar.close()
            gz.close()
        except OSError:
            pass  # 전송 중 클라이언트 연결 끊김 — 무시

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
                ofs = body.get("one_file_system")
                result = self.controller.start_scan(
                    target,
                    backend=body.get("backend"),       # None 이면 설정 기본값 사용
                    size_mode=body.get("size_mode"),
                    one_file_system=None if ofs is None else bool(ofs),
                )
                self._send_json(result, status=200 if result.get("ok") else 400)
                return

            if path == "/api/benchmark-workers":
                # 후보 스레드 수로 짧게 시범 탐색(블로킹 ~수십 초, 스레드 서버라 무방)
                res = self.controller.benchmark_workers(
                    path=(body.get("path") or "").strip() or None,
                    candidates=body.get("candidates"),
                    budget=body.get("budget", 5.0),
                )
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path in ("/api/scan/stop", "/api/scan/resume", "/api/scan/delete"):
                try:
                    scan_id = int(body.get("scan_id"))
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "reason": "scan_id 필요"}, status=400)
                    return
                if path == "/api/scan/stop":
                    res = self.controller.stop_scan(scan_id)
                elif path == "/api/scan/resume":
                    res = self.controller.resume_scan(scan_id)
                else:
                    res = self.controller.delete_scan(scan_id)
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path == "/api/prune":
                res = self.controller.prune(
                    keep_per_root=body.get("keep_per_root"),
                    older_than_days=body.get("older_than_days"))
                self._send_json(res)
                return

            if path == "/api/settings":
                result = self.controller.update_settings(body.get("settings") or body)
                self._send_json(result, status=200 if result.get("ok") else 403)
                return

            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass  # 클라이언트가 응답 도중 연결을 끊음 — 무시
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def serve(data_dir: str, host: str = "0.0.0.0", port: int = 8765, *,
          initial_settings=None, enable_scan: bool = True,
          lock_settings: bool = False) -> ThreadingHTTPServer:
    """대시보드 HTTP 서버를 만들고 반환한다(호출 측에서 serve_forever).

    data_dir 아래의 manager.db(전체 관리)와 scans/ 의 per-run DB(상세)를 읽는다.
    enable_scan=True 이면 웹에서 스캔을 시작/중지할 수 있다.
    initial_settings(CLI 플래그 등)는 settings.json 이 아직 없을 때만 초기값으로
    저장되며, 이후에는 대시보드에서 편집한 settings.json 이 우선한다.
    lock_settings=True 이면 웹에서 설정 편집을 막는다.
    httpd.controller 로 컨트롤러에 접근한다.
    """
    mgrmod.init_manager(data_dir)
    if initial_settings:
        setmod.seed_if_absent(data_dir, initial_settings)
    controller = (ScanController(data_dir, lock_settings=lock_settings)
                  if enable_scan else None)
    if controller is not None:
        controller.start_scheduler()   # 예약 스캔 스케줄러 시작
    handler = type("BoundHandler", (DashboardHandler,),
                   {"data_dir": data_dir, "controller": controller,
                    "bound_host": host, "bound_port": port})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller  # run 모드에서 초기 스캔 시작/정리에 사용
    return httpd

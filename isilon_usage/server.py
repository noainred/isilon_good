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
import sys
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
from . import auth as authmod
from . import audit as auditmod
from . import upgrade as upgrademod
from . import ask as askmod
from .scanner import run_scan


HERE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_HTML = os.path.join(HERE, "dashboard.html")
OP_TOKEN_TTL = 1800   # 로그인 세션 유효시간(초) — 기본 30분(이후 자동 로그아웃).


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


_UID_NAME_CACHE: dict = {}


def _uid_name(uid) -> Optional[str]:
    """uid → 사용자 이름(있으면). NFS uid 가 로컬에 없으면 None."""
    try:
        u = int(uid)
    except (TypeError, ValueError):
        return None
    if u in _UID_NAME_CACHE:
        return _UID_NAME_CACHE[u]
    name = None
    try:
        import pwd
        name = pwd.getpwuid(u).pw_name
    except (KeyError, ImportError, OSError):
        name = None
    _UID_NAME_CACHE[u] = name
    return name


_AGE_ORDER = ["30일 이내", "30~90일", "90일~1년", "1~2년", "2~5년", "5년+"]


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
    out["op_password"] = ""
    out["op_password_set"] = bool((s or {}).get("op_password"))
    out["isilon_password"] = ""
    out["isilon_password_set"] = bool((s or {}).get("isilon_password"))
    out["powerstore_password"] = ""
    out["powerstore_password_set"] = bool((s or {}).get("powerstore_password"))
    out["ask_llm_key"] = ""
    out["ask_llm_key_set"] = bool((s or {}).get("ask_llm_key"))
    sa = []
    for a in ((s or {}).get("storage_arrays") or []):
        b = dict(a)
        b["password"] = ""
        b["password_set"] = bool(a.get("password"))
        sa.append(b)
    out["storage_arrays"] = sa
    return out


def _idle_resources() -> dict:
    """스캔이 없을 때(유휴) 대시보드에 보여줄 라이브 시스템 자원(메모리·CPU·디스크)."""
    out: dict = {}
    try:
        samp = monmod.collect(os.getpid(), None).as_dict()
        out["resources"] = {"latest": samp, "series": [samp], "peak_scanner_rss": 0,
                            "peak_du_rss": 0, "peak_mem_percent": samp.get("mem_percent", 0)}
        ft = fu = ff = 0
        try:
            v = os.statvfs("/")
            ft = v.f_blocks * v.f_frsize
            ff = v.f_bavail * v.f_frsize
            fu = ft - (v.f_bfree * v.f_frsize)
        except OSError:
            pass
        out["idle_fs"] = {"fs_total_bytes": ft, "fs_used_bytes": fu, "fs_free_bytes": ff}
    except Exception:  # noqa: BLE001 — 자원 수집 실패가 상태 응답을 막지 않음
        pass
    return out


def build_status(conn, run_id: Optional[int], *, samples: int = 150, top: int = 20) -> dict:
    """대시보드가 한 번의 폴링으로 쓸 수 있는 통합 상태 객체를 만든다."""
    if run_id is None:
        run_id = dbmod.latest_run_id(conn)
    if run_id is None:
        return dict({"ok": False, "reason": "no_runs",
                     "have_psutil": monmod.have_psutil()}, **_idle_resources())

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


def forecast_capacity(mconn, scan_row) -> dict:
    """같은 루트의 완료 스캔 이력으로 증가 추세(선형 회귀)와 소진 예상일을 계산."""
    root = scan_row["root_path"]
    pts = [dict(r) for r in mconn.execute(
        """SELECT id, COALESCE(finished_at, started_at) AS ts, scanned_bytes
           FROM scans WHERE root_path=? AND status='done' AND scanned_bytes>0
           ORDER BY id""", (root,))]
    latest = mconn.execute(
        "SELECT fs_total_bytes, fs_used_bytes, fs_free_bytes FROM scans "
        "WHERE root_path=? ORDER BY id DESC LIMIT 1", (root,)).fetchone()
    fs_total = int(latest["fs_total_bytes"] or 0) if latest else 0
    fs_used = int(latest["fs_used_bytes"] or 0) if latest else 0
    fs_free = int(latest["fs_free_bytes"] or 0) if latest else 0
    out = {"ok": True, "root_path": root, "points": len(pts),
           "history": [{"ts": p["ts"], "bytes": p["scanned_bytes"]} for p in pts[-50:]],
           "fs_total_bytes": fs_total, "fs_used_bytes": fs_used,
           "fs_free_bytes": fs_free,
           "enough": len(pts) >= 2, "growth_per_day": None,
           "days_to_90pct": None, "days_to_full": None}
    if len(pts) < 2:
        return out
    # 최소제곱 기울기(bytes/sec). 시각 범위가 0 이면 추세 없음.
    n = len(pts)
    xs = [float(p["ts"]) for p in pts]
    ys = [float(p["scanned_bytes"]) for p in pts]
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return out
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom  # bytes/sec
    per_day = slope * 86400.0
    out["growth_per_day"] = int(per_day)
    if per_day > 0 and fs_total > 0:
        to90 = (fs_total * 0.9 - fs_used) / per_day
        out["days_to_90pct"] = round(max(0.0, to90), 1)
        if fs_free > 0:
            out["days_to_full"] = round(max(0.0, fs_free / per_day), 1)
    return out


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
            SELECT path, SUM(ab) AS ab, SUM(bb) AS bb,
                         SUM(af) AS af, SUM(bf) AS bf FROM (
                SELECT path, total_bytes AS ab, 0 AS bb, total_files AS af, 0 AS bf
                  FROM a.directories WHERE run_id=? {depth_a}
                UNION ALL
                SELECT path, 0 AS ab, total_bytes AS bb, 0 AS af, total_files AS bf
                  FROM b.directories WHERE run_id=? {depth_b}
            ) GROUP BY path
            ORDER BY ABS(SUM(bb)-SUM(ab)) DESC LIMIT ?
            """,
            (a_run, b_run, limit),
        ).fetchall()
        result = [{"path": r["path"], "base_bytes": int(r["ab"]),
                   "target_bytes": int(r["bb"]),
                   "delta": int(r["bb"]) - int(r["ab"]),
                   "base_files": int(r["af"]), "target_files": int(r["bf"]),
                   "files_delta": int(r["bf"]) - int(r["af"])} for r in rows]
        return {
            "ok": True,
            "root_path": target["root_path"],
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
        self._pause_watch: Dict[int, dict] = {}   # 중단 후 N분 미재시작 감시: scan_id -> {due, root}
        self._auto_restart: Dict[int, dict] = {}  # 완료 후 자동 재시작(반복): scan_id -> {경로·설정}
        self._storage_cache = None          # 스토리지 어레이(아이실론/PowerStore) 상태 캐시
        self._storage_ts = 0.0
        self._bench = None                  # 실측 보정(시범 스캔) 진행 상태(단계별 표시용)
        self._bench_lock = threading.Lock()
        self._gen = None                    # 테스트 데이터 생성 진행 상태
        self._gen_lock = threading.Lock()
        self._gen_stop = None
        self._analyze = None                # 대상 분석(깊이 구조 측정) 진행 상태
        self._analyze_lock = threading.Lock()
        self._analyze_stop = None
        self._autotune = None               # 오토튜닝(최적 procs×threads 측정) 진행 상태
        self._autotune_lock = threading.Lock()
        self._autotune_stop = None
        self._upg_state = {"log": [], "installing": False, "available": False,
                           "latest": None, "last_check": None}   # 인터넷 자동 업그레이드 상태
        self._ts_hist_lock = threading.Lock()  # 트러블슈팅 진단 이력 파일 보호
        self._rate_lock = threading.Lock()     # 처리량 표본 DB 보호
        self._rate_prev = None                 # (scan_id, ts, discovered, files)
        self._rate_stop = threading.Event()
        self._rate_thread = threading.Thread(
            target=self._rate_sampler_loop, name="rate-sampler", daemon=True)
        self._rate_thread.start()
        self._upgrade_thread = threading.Thread(
            target=self._upgrade_watch_loop, name="upgrade-watch", daemon=True)
        self._upgrade_thread.start()
        self._auth = authmod.AuthGuard(
            lambda: self.settings.get("op_password"),
            ttl=lambda: float(self.settings.get("op_ttl_minutes", 30) or 30) * 60)
        self._launch_cwd = os.getcwd()      # info.MD 를 저장할 '실행한 디렉터리'
        self._reconcile_orphans()           # 이전 프로세스가 남긴 고아 스캔 정리
        self._load_auto_restart()           # 저장된 반복(자동 재시작) 설정 복원 — 재개될 스캔이 끝나면 이어서 반복
        self._resume_after_upgrade()        # 업그레이드 재시작 직전 돌던 스캔을 자동 재개

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

    # ----- 업그레이드 재시작 ↔ 진행 중 스캔 자동 재개 -----
    _RESUME_MARK = "resume_after_upgrade.json"

    def _mark_running_scans_for_resume(self) -> None:
        """업그레이드로 재시작하기 직전, 지금 돌고 있는 스캔 id 를 마커 파일(data-dir)에 적어둔다.

        새 프로세스가 시작할 때 이 마커를 읽어 자동 재개한다. 돌던 스캔이 없으면 남은 마커를 지운다.
        (data-dir 은 코드 교체와 무관하게 보존되므로 업그레이드 후에도 마커가 살아남는다.)
        """
        ids = self.running_ids()
        path = os.path.join(self.data_dir, self._RESUME_MARK)
        if not ids:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"scan_ids": ids, "ts": time.time()}, fh)
            os.replace(tmp, path)
            self._log("[upgrade] 재시작 후 자동 재개할 스캔 표시: %s" % ids)
        except OSError:
            pass

    def _restart_for_upgrade(self) -> None:
        """진행 중 스캔을 자동 재개 대상으로 표시한 뒤 프로세스를 재시작한다(돌아오지 않음)."""
        try:
            self._mark_running_scans_for_resume()
        except Exception:   # noqa: BLE001 — 표시 실패해도 업그레이드 재시작은 그대로 진행
            pass
        upgrademod.restart_process()

    def _resume_after_upgrade(self) -> None:
        """업그레이드 재시작 직전에 표시해 둔 스캔을 자동 재개한다(시작 시 1회). 마커는 즉시 삭제.

        _reconcile_orphans() 가 먼저 그 스캔을 'paused' 로 정리한 뒤 호출되므로, 여기서 곧바로
        이어서 재개한다. 마커는 비업그레이드(일반) 재시작에서는 쓰이지 않으므로 자동 재개되지 않는다.
        """
        path = os.path.join(self.data_dir, self._RESUME_MARK)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        try:
            os.remove(path)     # 한 번만 — 다음(비업그레이드) 재시작에서 반복 재개 방지
        except OSError:
            pass
        for sid in (data.get("scan_ids") or []):
            try:
                res = self.resume_scan(int(sid))
                self._log("[upgrade] 업그레이드 후 자동 재개 scan #%s: %s"
                          % (sid, "재개" if res.get("ok") else res.get("reason")))
            except Exception as exc:   # noqa: BLE001
                self._log("[upgrade] 자동 재개 실패 scan #%s: %s" % (sid, exc))

    # ----- 반복(완료 후 자동 재시작) 상태 영속화 -----
    _AUTO_RESTART_MARK = "auto_restart.json"

    def _save_auto_restart(self) -> None:
        """반복(자동 재시작) 설정을 data-dir 에 저장한다.

        _auto_restart 는 메모리 dict 라 프로세스가 죽으면(재시작·업그레이드) 사라진다.
        그러면 돌던 스캔이 한 번 끝난 뒤 반복이 끊긴다. 그래서 디스크에도 남겨, 시작 시
        복원해 반복을 이어간다(data-dir 은 코드 교체와 무관하게 보존된다).
        """
        path = os.path.join(self.data_dir, self._AUTO_RESTART_MARK)
        with self._lock:                       # 스냅샷만 락 안에서(호출부는 락을 풀고 부른다)
            data = {str(k): v for k, v in self._auto_restart.items()}
        try:
            if not data:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def _load_auto_restart(self) -> None:
        """저장해 둔 반복 설정을 복원한다(시작 시 1회). 이미 끝난(done/error) 스캔은 버린다."""
        path = os.path.join(self.data_dir, self._AUTO_RESTART_MARK)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not data:
            return
        restored: Dict[int, dict] = {}
        try:
            mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
            try:
                for k, cfg in data.items():
                    try:
                        sid = int(k)
                    except (TypeError, ValueError):
                        continue
                    row = mgrmod.get_scan(mconn, sid)
                    if row is None or row["status"] in ("done", "error"):
                        continue          # 이미 끝난 반복은 복원하지 않음(재개 대상만)
                    restored[sid] = cfg
            finally:
                mconn.close()
        except Exception:                  # noqa: BLE001 — DB 를 못 읽으면 보수적으로 전부 복원
            for k, cfg in data.items():
                try:
                    restored[int(k)] = cfg
                except (TypeError, ValueError):
                    pass
        if restored:
            with self._lock:
                self._auto_restart.update(restored)
            self._log("[loop] 반복(자동 재시작) 복원: %s" % sorted(restored.keys()))
        self._save_auto_restart()          # 정리(prune)된 상태를 디스크에 반영

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

    # ----- 작업 보호(비밀번호) -----
    def op_required(self) -> bool:
        """작업(POST)에 로그인이 필요한가(= op_password 가 설정됨)."""
        return self._auth.required()

    def unlock(self, password: str) -> dict:
        """비밀번호를 확인하고 맞으면 로그인 토큰을 발급한다(무차별 대입 시 일시 잠금)."""
        return self._auth.login(password)

    def op_token_valid(self, token) -> bool:
        return self._auth.token_valid(token)

    def _upgrade_watch_loop(self) -> None:
        """새 버전을 자동 적용한다 — ① 로컬 감시 폴더 ② 인터넷(GitHub) 소스.

        옵트인(설정 비우면 끔). 더 새 버전만 적용하고 기존 코드는 백업·롤백 가능. 진행 중
        스캔이 있으면 미룬다. 인터넷 소스는 주기적으로 확인해 상태를 기록하고(상세 표시용),
        upgrade_auto 가 켜져 있으면 자동 설치 후 재시작한다.
        """
        code_dir = upgrademod.code_dir_of(__file__)
        dest = os.path.join(self.data_dir, "upgrades")
        while not self._rate_stop.is_set():
            secs = 60
            try:
                secs = int(self.settings.get("upgrade_check_secs", 60) or 60)
                with self._lock:
                    busy = bool(self._scans)   # 스캔 중이면 업그레이드 보류
                # ① 로컬 감시 폴더
                wd = (self.settings.get("upgrade_watch_dir") or "").strip()
                if wd and not busy:
                    found = upgrademod.find_newer_archive(wd, __version__)
                    if found:
                        res = upgrademod.upgrade_from_archive(found[0], code_dir, __version__)
                        if res.get("ok"):
                            self._upg_log("감시 폴더 자동 업그레이드 %s → %s" % (
                                res.get("from"), res["version"]))
                            auditmod.record(self.data_dir, action="self_upgrade", ok=True,
                                            detail="%s -> %s" % (res.get("from"), res["version"]))
                            self._restart_for_upgrade()   # 돌던 스캔 표시 후 재시작(돌아오지 않음)
                # ② 인터넷(GitHub) 소스
                if (self.settings.get("upgrade_source") or "off").strip() == "github":
                    info = upgrademod.check_remote(self.settings.get("upgrade_url") or "", __version__,
                                                   token=self.settings.get("upgrade_token") or None)
                    self._upg_set_check(info)
                    if info.get("available") and not busy and bool(self.settings.get("upgrade_auto")):
                        self._upg_log("새 버전 %s 발견 — 자동 설치" % info.get("latest"))
                        res = upgrademod.upgrade_from_remote(
                            self.settings.get("upgrade_url") or "", code_dir, __version__, dest,
                            token=self.settings.get("upgrade_token") or None)
                        if res.get("ok"):
                            self._upg_log("자동 업그레이드 %s → %s 완료, 재시작" % (
                                res.get("from"), res["version"]))
                            auditmod.record(self.data_dir, action="self_upgrade_net", ok=True,
                                            detail="%s -> %s" % (res.get("from"), res["version"]))
                            self._restart_for_upgrade()
                        else:
                            self._upg_log("자동 설치 실패: %s" % res.get("reason"))
            except Exception:  # noqa: BLE001
                pass
            self._rate_stop.wait(max(10, secs))

    # ----- 자동 업그레이드 상태/조작(인터넷 소스, 상세 표시용) -----
    def _upg_log(self, msg: str) -> None:
        with self._lock:
            self._upg_state.setdefault("log", []).append({"t": time.time(), "msg": msg})
            self._upg_state["log"] = self._upg_state["log"][-60:]
        self._log("[upgrade] " + msg)

    def _upg_set_check(self, info: dict) -> None:
        with self._lock:
            self._upg_state.update({
                "last_check": info.get("checked_at"), "latest": info.get("latest"),
                "available": bool(info.get("available")), "source": info.get("source"),
                "size_bytes": info.get("size_bytes"), "download_url": info.get("download_url"),
                "check_error": info.get("error"),
            })

    def upgrade_status(self) -> dict:
        with self._lock:
            st = dict(self._upg_state)
        st["current"] = __version__
        st["source_mode"] = self.settings.get("upgrade_source", "off")
        st["auto"] = bool(self.settings.get("upgrade_auto"))
        st["url"] = (self.settings.get("upgrade_url") or upgrademod.DEFAULT_UPGRADE_BASE)
        st["url_custom"] = self.settings.get("upgrade_url") or ""
        st["token_set"] = bool(self.settings.get("upgrade_token"))   # 값 비노출, 설정 여부만
        st["watch_dir"] = self.settings.get("upgrade_watch_dir", "")
        return {"ok": True, **st}

    def upgrade_check(self) -> dict:
        info = upgrademod.check_remote(self.settings.get("upgrade_url") or "", __version__,
                                       token=self.settings.get("upgrade_token") or None)
        self._upg_set_check(info)
        if info.get("error"):
            self._upg_log("확인 실패: %s" % info["error"])
        else:
            self._upg_log("확인 — 현재 %s · 최신 %s%s" % (
                __version__, info.get("latest"),
                " · 업데이트 가능" if info.get("available") else " · 최신"))
        return {"ok": bool(info.get("ok")), **self.upgrade_status()}

    def upgrade_install(self) -> dict:
        with self._lock:
            if self._upg_state.get("installing"):
                return {"ok": False, "error": "이미 설치 중입니다."}
            self._upg_state["installing"] = True
        try:
            code_dir = upgrademod.code_dir_of(__file__)
            dest = os.path.join(self.data_dir, "upgrades")
            self._upg_log("수동 업그레이드 시작…")
            res = upgrademod.upgrade_from_remote(
                self.settings.get("upgrade_url") or "", code_dir, __version__, dest,
                token=self.settings.get("upgrade_token") or None)
        finally:
            with self._lock:
                self._upg_state["installing"] = False
        if res.get("ok"):
            self._upg_log("설치 완료 %s → %s — 곧 재시작" % (res.get("from"), res.get("version")))
            auditmod.record(self.data_dir, action="self_upgrade_manual", ok=True,
                            detail="-> %s" % res.get("version"))
            threading.Thread(target=lambda: (time.sleep(1.2), self._restart_for_upgrade()),
                             daemon=True).start()
            return {"ok": True, "version": res.get("version"), "restarting": True}
        self._upg_log("설치 실패: %s" % res.get("reason"))
        return {"ok": False, "error": res.get("reason"),
                "up_to_date": bool(res.get("up_to_date"))}

    def _write_info_md(self, password: str) -> None:
        """설정한 비밀번호를 실행한 디렉터리의 info.MD 로 저장(권한 600)."""
        from . import __version__
        try:
            path = os.path.join(self._launch_cwd, "info.MD")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            body = (
                "# isilon_usage 운영 정보\n\n"
                "이 파일은 작업 보호 비밀번호를 기록합니다. **민감 정보이므로 공유하지 마세요.**\n\n"
                "| 항목 | 값 |\n|---|---|\n"
                "| 버전 | %s |\n| 데이터 폴더 | %s |\n| 실행 폴더 | %s |\n"
                "| 작업 비밀번호 | `%s` |\n| 갱신 시각 | %s |\n\n"
                "> 보기는 비밀번호 없이 가능하고, 버튼/설정 변경 등 작업에는 위 비밀번호가 필요합니다.\n"
                "> 비밀번호를 잊었다면 이 파일에서 확인하거나, settings.json 의 op_password 를 비우세요.\n"
            ) % (__version__, os.path.abspath(self.data_dir), self._launch_cwd,
                 password, ts)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            pass

    def update_settings(self, new: dict) -> dict:
        """설정을 저장하고 갱신된 설정을 반환한다(잠겨 있으면 거부)."""
        if self.lock_settings:
            return {"ok": False, "reason": "설정이 잠겨 있습니다(--lock-settings)."}
        new = dict(new or {})
        # 작업 보호 비밀번호: op_lock(체크) + op_password(새 비번)
        #  - op_lock 거짓 → 보호 해제(비밀번호 제거)
        #  - op_lock 참 + 새 비번 있음 → 설정/변경(+info.MD 기록)
        #  - op_lock 참 + 빈칸 → 기존 유지
        op_changed = None
        if "op_lock" in new or "op_password" in new:
            want_lock = bool(new.get("op_lock", True))
            newpw = str(new.get("op_password") or "")
            if not want_lock:
                if self.settings.get("op_password"):
                    op_changed = ""   # 해제
            elif newpw.strip():
                op_changed = newpw    # 설정/변경
            new.pop("op_lock", None)
            new.pop("op_password", None)
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
        if op_changed is not None:        # 작업 비밀번호 설정/변경/해제
            enc = bool(merged.get("op_password_encrypted"))
            merged["op_password"] = authmod.store_password(op_changed, encrypt=enc)
        self.settings = setmod.save(self.data_dir, merged)
        # 평문 저장일 때만 복구용 info.MD 기록(암호화 시 평문을 남기지 않는다)
        if op_changed and not bool(self.settings.get("op_password_encrypted")):
            self._write_info_md(op_changed)
        return {"ok": True, "settings": _public_settings(self.settings)}

    def set_op_password(self, password: str, *, clear: bool = False) -> dict:
        """작업 보호 비밀번호를 설정/변경/해제한다(포탈이 api_token 으로 푸시).

        update_settings 의 op_password 처리와 같은 저장 규칙(암호화 여부·info.MD)을 따른다.
        clear=True 면 보호 해제(비밀번호 제거).
        """
        if self.lock_settings:
            return {"ok": False, "reason": "설정이 잠겨 있습니다(--lock-settings)."}
        pw = "" if clear else str(password or "")
        if not clear and not pw.strip():
            return {"ok": False, "reason": "빈 비밀번호는 설정할 수 없습니다."}
        enc = bool(self.settings.get("op_password_encrypted"))
        merged = dict(self.settings)
        merged["op_password"] = authmod.store_password(pw, encrypt=enc)
        self.settings = setmod.save(self.data_dir, merged)
        if pw and not enc:                 # 평문 저장 + 새로 설정/변경 → 복구용 info.MD
            self._write_info_md(pw)
        return {"ok": True, "cleared": not merged["op_password"],
                "op_required": bool(merged["op_password"])}

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

    def browse_allowed(self, path: str) -> bool:
        """폴더 나열(browse)을 허용할 경로인지 검사한다.

        mount_bases 가 설정돼 있으면 그 하위(또는 동일) 경로만 허용해, 허용
        경로 밖의 디렉터리 구조가 노출되지 않게 한다. mount_bases 가 비어
        있으면 기존 동작대로 어떤 경로든 허용한다(스캔 허용과 같은 정책).
        """
        if not self.mount_bases:
            return True
        ap = os.path.abspath(path or "/")
        for base in self.mount_bases:
            try:
                b = os.path.abspath(base)
                if os.path.commonpath([ap, b]) == b:
                    return True
            except ValueError:
                continue
        return False

    def scan_path_check(self, path: str, confirm_outside: bool = False):
        """스캔/측정 대상 경로를 검사한다.

        - 디렉터리가 아니거나 접근 불가 → 하드 거부(되돌릴 수 없음).
        - mount_bases(지정 경로)가 설정돼 있고 그 '밖'이며 confirm_outside 가
          아니면 → reason="outside_base"(사용자 컨펌으로 진행 가능한 '소프트' 신호).
          지정 경로는 기본값/가드일 뿐, 사용자가 컨펌하면 다른 경로도 허용한다.
        반환: (ok, err_dict_or_None). err_dict 는 그대로 응답으로 보낼 수 있다.
        """
        ap = os.path.abspath(path)
        if not os.path.isdir(ap):
            return False, {"ok": False,
                           "reason": "디렉터리가 아니거나 접근할 수 없습니다."}
        if self.mount_bases and not self.browse_allowed(ap) and not confirm_outside:
            return False, {"ok": False, "reason": "outside_base",
                           "path": ap, "allowed": self.mount_bases}
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

    def benchmark_start(self, *, path=None, candidates=None, budget=5.0,
                        mem_factor=2.0) -> dict:
        """백그라운드로 시범 탐색을 시작한다. 각 단계가 끝날 때마다 부분 결과를
        쌓아 두고, benchmark_status() 로 폴링해 단계별로 보여줄 수 있게 한다."""
        from . import tuning as tunmod
        if not path:
            path = self.mount_bases[0] if self.mount_bases else None
        if not path:
            return {"ok": False, "error": "측정할 경로를 지정하세요(허용 경로 없음)."}
        ok, why = self.path_allowed(path)
        if not ok:
            return {"ok": False, "error": why}
        with self._bench_lock:
            if self._bench and self._bench.get("running"):
                return {"ok": False, "error": "이미 측정이 진행 중입니다."}
            self._bench = {"running": True, "done": False, "results": [],
                           "sample_path": os.path.abspath(path), "error": None,
                           "recommended": None, "note": "", "started_at": time.time()}
        if isinstance(candidates, str):
            candidates = [c for c in candidates.split(",") if c.strip()]
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            budget = 5.0

        def _on_result(r):
            with self._bench_lock:
                if self._bench is not None:
                    self._bench["results"].append(r)

        def _run():
            try:
                res = tunmod.benchmark_workers(
                    path, candidates=candidates, budget_sec=budget,
                    mem_factor=mem_factor, on_result=_on_result)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": "측정 오류: %s" % exc}
            with self._bench_lock:
                if self._bench is not None:
                    self._bench["running"] = False
                    self._bench["done"] = True
                    self._bench["recommended"] = res.get("recommended")
                    self._bench["note"] = res.get("note", "")
                    self._bench["mem_factor"] = res.get("mem_factor")
                    self._bench["base_rss_bytes"] = res.get("base_rss_bytes")
                    if not res.get("ok"):
                        self._bench["error"] = res.get("error")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "started": True}

    def benchmark_status(self) -> dict:
        """진행 중/완료된 시범 탐색의 현재 스냅샷(단계별 결과 포함)."""
        with self._bench_lock:
            if self._bench is None:
                return {"ok": True, "running": False, "done": False, "results": []}
            return {"ok": True, **{k: v for k, v in self._bench.items()}}

    # ----- 오토튜닝(최적 프로세스×스레드 자동 측정 → 본 스캔 자동 시작) -----
    def autotune_start(self, *, path=None, secs=None, then_scan=True,
                       confirm_outside=False) -> dict:
        """실제 엔진(pscan)을 짧게 측정해 최적 procs×threads 를 고르고, then_scan 이면
        그 설정으로 본 스캔을 자동 시작한다. 진행은 autotune_status 로 폴링한다."""
        if not path:
            return {"ok": False, "error": "측정할 경로를 지정하세요."}
        ok, err = self.scan_path_check(path, confirm_outside)
        if not ok:
            # 지정 경로 밖이면 컨펌 신호(outside_base)를 그대로 전달해 클라가 묻게 한다.
            if err.get("reason") == "outside_base":
                return err
            return {"ok": False, "error": err.get("reason")}
        path = os.path.abspath(path)
        try:
            secs = float(secs) if secs else float(self.settings.get("autotune_secs", 8) or 8)
        except (TypeError, ValueError):
            secs = 8.0
        secs = min(60.0, max(2.0, secs))
        with self._autotune_lock:
            if self._autotune and self._autotune.get("running"):
                return {"ok": False, "error": "이미 오토튜닝이 진행 중입니다."}
            self._autotune_stop = threading.Event()
            self._autotune = {"running": True, "done": False, "error": None,
                              "path": path, "results": [], "best": None,
                              "phase": "measuring", "current": None, "scan_id": None,
                              "then_scan": bool(then_scan), "secs": secs,
                              "started_at": time.time()}
        stop_event = self._autotune_stop
        maxp = max(1, int(self.settings.get("scan_workers", 8) or 8))
        maxt = max(1, int(self.settings.get("pscan_threads", 8) or 8))
        if maxt < 2:
            maxt = 8     # 측정에선 2단 병렬도 시험(설정이 1이어도)
        size_mode = self.settings.get("default_size_mode", "disk")

        def _prog(p):
            with self._autotune_lock:
                if self._autotune is None:
                    return
                self._autotune["phase"] = p.get("phase", "measuring")
                self._autotune["results"] = p.get("results", self._autotune["results"])
                if p.get("phase") == "measuring":
                    self._autotune["current"] = {
                        "procs": p.get("procs"), "threads": p.get("threads"),
                        "label": p.get("label"), "index": p.get("index"),
                        "total": p.get("total")}
                if p.get("best"):
                    self._autotune["best"] = p.get("best")

        def _run():
            scan_id = None
            try:
                from . import autotune as atmod
                r = atmod.autotune(path, secs=secs, size_mode=size_mode,
                                   max_procs=maxp, max_threads=maxt,
                                   stop_event=stop_event, on_progress=_prog)
            except Exception as exc:  # noqa: BLE001
                r = {"ok": False, "error": "오토튜닝 오류: %s" % exc}
            best = r.get("best") if r.get("ok") else None
            if r.get("ok") and then_scan and best and not stop_event.is_set():
                pn = int(best.get("procs", 1) or 1)
                tn = int(best.get("threads", 1) or 1)
                try:
                    if pn > 1:        # 병렬이 빠름 → pscan(빠른 용량, 1단계 드릴다운)
                        sr = self.start_scan(path, size_mode=size_mode, engine="pscan",
                                             processes=pn, threads=tn)
                    else:             # 단일이 빠름(빠른 저장소) → threads(상세 트리)
                        sr = self.start_scan(path, size_mode=size_mode, engine="threads")
                    scan_id = sr.get("scan_id")
                except Exception:  # noqa: BLE001
                    scan_id = None
            with self._autotune_lock:
                if self._autotune is not None:
                    self._autotune["running"] = False
                    self._autotune["done"] = True
                    self._autotune["phase"] = "done"
                    self._autotune["results"] = r.get("results", [])
                    self._autotune["best"] = best
                    self._autotune["note"] = r.get("note", "")
                    self._autotune["scan_id"] = scan_id
                    if not r.get("ok"):
                        self._autotune["error"] = r.get("error")

        threading.Thread(target=_run, name="autotune", daemon=True).start()
        return {"ok": True, "started": True}

    def autotune_status(self) -> dict:
        """진행 중/완료된 오토튜닝의 현재 스냅샷(단계별 결과 + best + 시작된 스캔)."""
        with self._autotune_lock:
            if self._autotune is None:
                return {"ok": True, "running": False, "done": False, "results": []}
            return {"ok": True, **{k: v for k, v in self._autotune.items()}}

    def autotune_stop(self) -> dict:
        with self._autotune_lock:
            if self._autotune_stop is not None:
                self._autotune_stop.set()
        return {"ok": True}

    # ----- 대상 분석(디렉터리 깊이 구조 측정) -----
    def analyze_start(self, *, path=None, max_depth=0, dir_limit=0,
                      time_budget=0.0, workers=8) -> dict:
        """백그라운드로 깊이 구조 분석을 시작한다(파일 stat·DB 없이, 빠름).

        analyze_status() 로 폴링해 진행/결과를 보여준다.
        """
        from . import analyzer as anamod
        if not path:
            path = self.mount_bases[0] if self.mount_bases else None
        if not path:
            return {"ok": False, "error": "분석할 경로를 지정하세요(허용 경로 없음)."}
        ok, why = self.path_allowed(path)
        if not ok:
            return {"ok": False, "error": why}
        try:
            max_depth = max(0, int(max_depth or 0))
            dir_limit = max(0, int(dir_limit or 0))
            time_budget = max(0.0, float(time_budget or 0.0))
            workers = max(1, int(workers or 8))
        except (TypeError, ValueError):
            max_depth, dir_limit, time_budget, workers = 0, 0, 0.0, 8
        with self._analyze_lock:
            if self._analyze and self._analyze.get("running"):
                return {"ok": False, "error": "이미 분석이 진행 중입니다."}
            self._analyze_stop = threading.Event()
            self._analyze = {"running": True, "done": False, "error": None,
                             "root": os.path.abspath(path), "started_at": time.time(),
                             "total_dirs": 0, "total_files": 0, "max_depth": 0,
                             "error_dirs": 0, "elapsed": 0.0, "result": None}
        stop_event = self._analyze_stop

        def _on_progress(p):
            with self._analyze_lock:
                if self._analyze is not None and self._analyze.get("running"):
                    self._analyze.update(
                        total_dirs=p.get("total", 0), total_files=p.get("files", 0),
                        max_depth=p.get("max_depth", 0), error_dirs=p.get("errors", 0),
                        elapsed=p.get("elapsed", 0.0))

        def _run():
            try:
                res = anamod.analyze_depth(
                    path, max_depth=max_depth, dir_limit=dir_limit,
                    time_budget=time_budget, workers=workers,
                    stop_event=stop_event, on_progress=_on_progress)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": "분석 오류: %s" % exc}
            with self._analyze_lock:
                if self._analyze is not None:
                    self._analyze["running"] = False
                    self._analyze["done"] = True
                    if res.get("ok"):
                        self._analyze["result"] = res
                        self._analyze.update(
                            total_dirs=res.get("total_dirs", 0),
                            total_files=res.get("total_files", 0),
                            max_depth=res.get("max_depth", 0),
                            error_dirs=res.get("error_dirs", 0),
                            elapsed=res.get("elapsed", 0.0))
                    else:
                        self._analyze["error"] = res.get("error")

        threading.Thread(target=_run, name="analyze", daemon=True).start()
        return {"ok": True, "started": True}

    def analyze_status(self) -> dict:
        """진행 중/완료된 대상 분석의 현재 스냅샷(완료 시 result 포함)."""
        with self._analyze_lock:
            if self._analyze is None:
                return {"ok": True, "running": False, "done": False, "result": None}
            return {"ok": True, **{k: v for k, v in self._analyze.items()}}

    def analyze_stop(self) -> dict:
        with self._analyze_lock:
            if self._analyze_stop is not None:
                self._analyze_stop.set()
        return {"ok": True}

    # ----- 튜닝 점검(OS 커널/마운트 설정 분석) -----
    def systune_check(self, *, scan_root=None) -> dict:
        """OS 커널/NFS 마운트 설정을 분석해 스캔 가속 튜닝 포인트를 점검한다."""
        from . import systune as sysmod
        if not scan_root:
            scan_root = self.mount_bases[0] if self.mount_bases else None
        try:
            return sysmod.check(scan_root=scan_root, data_dir=self.data_dir)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "점검 오류: %s" % exc}

    # ----- 트러블슈팅(실행 중 스캔 병목 진단) -----
    def troubleshoot(self, *, sample_sec: float = 1.2) -> dict:
        """실행 중인 스캔의 '어느 구간이 느린지'를 진단한다(워커 구간·처리량·프론티어).

        OK 가 아닌 결과는 자동으로 진단 이력(History)에 저장한다.
        """
        from . import troubleshoot as troubmod
        try:
            res = troubmod.diagnose(self, sample_sec=sample_sec)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "진단 오류: %s" % exc}
        try:
            with self._ts_hist_lock:
                troubmod.record_if_problem(self.data_dir, res)
        except Exception:  # noqa: BLE001
            pass
        return res

    def troubleshoot_history(self, limit: int = 200) -> dict:
        from . import troubleshoot as troubmod
        with self._ts_hist_lock:
            return {"ok": True, "events": troubmod.load_history(self.data_dir, limit=limit)}

    def troubleshoot_history_clear(self) -> dict:
        from . import troubleshoot as troubmod
        with self._ts_hist_lock:
            return troubmod.clear_history(self.data_dir)

    # ----- 처리량 표본 24시간 저장(전용 metrics.db) -----
    def _rate_sampler_loop(self) -> None:
        """10초마다 진행값 델타로 처리량(초당 디렉터리/파일)을 계산해 metrics.db 에
        기록한다(대시보드가 닫혀 있어도 24시간치가 쌓인다)."""
        from . import troubleshoot as troubmod
        n = 0
        while not self._rate_stop.wait(10):
            try:
                prog = troubmod.latest_progress(self.data_dir)
                now = time.time()
                if prog and prog["status"] in ("discovering", "sizing", "running"):
                    prev = self._rate_prev
                    if prev and prev[0] == prog["scan_id"]:
                        dt = now - prev[1]
                        if dt >= 1:
                            rd = max(0, prog["discovered"] - prev[2]) / dt
                            rf = max(0, prog["files"] - prev[3]) / dt
                            rb = max(0, prog["bytes"] - prev[4]) / dt   # 처리 용량(B/s)
                            with self._rate_lock:
                                troubmod.append_rate_sample(
                                    self.data_dir, now, rd, rf, rb, prog["scan_id"])
                    self._rate_prev = (prog["scan_id"], now, prog["discovered"],
                                       prog["files"], prog["bytes"])
                else:
                    self._rate_prev = None
                n += 1
                if n % 30 == 0:            # 약 5분마다 24시간 초과분 정리
                    with self._rate_lock:
                        troubmod.trim_rate_samples(self.data_dir, keep_seconds=86400)
            except Exception:             # noqa: BLE001 — 샘플러가 죽으면 안 됨
                pass

    def rate_samples(self, *, seconds: int = 86400, max_points: int = 600) -> dict:
        from . import troubleshoot as troubmod
        with self._rate_lock:
            return {"ok": True, "seconds": seconds,
                    "samples": troubmod.load_rate_samples(
                        self.data_dir, seconds=seconds, max_points=max_points)}

    def throughput(self, *, bucket_sec: int = 60, max_buckets: int = 60) -> dict:
        """버킷별(1분/5분/10분/1시간) 처리량(용량·파일·디렉터리)."""
        from . import troubleshoot as troubmod
        with self._rate_lock:
            buckets = troubmod.throughput_buckets(
                self.data_dir, bucket_sec=bucket_sec, max_buckets=max_buckets)
        tb = sum(b["bytes"] for b in buckets)
        tf = sum(b["files"] for b in buckets)
        td = sum(b["dirs"] for b in buckets)
        return {"ok": True, "bucket_sec": bucket_sec, "buckets": buckets,
                "total_bytes": tb, "total_files": tf, "total_dirs": td}

    # ----- 테스트 데이터 생성 -----
    def gentest_validate(self, *, path, n_dirs, n_subdirs, n_files, file_size) -> dict:
        from . import gentest as genmod
        ok, why, plan = genmod.validate(path, n_dirs, n_subdirs, n_files, file_size)
        return {"ok": ok, "error": why, "plan": plan}

    def gentest_start(self, *, path, n_dirs, n_subdirs, n_files, file_size) -> dict:
        """백그라운드로 샘플 디렉터리/파일을 생성한다(진행 폴링용)."""
        from . import gentest as genmod
        ok, why, plan = genmod.validate(path, n_dirs, n_subdirs, n_files, file_size)
        if not ok:
            return {"ok": False, "error": why, "plan": plan}
        with self._gen_lock:
            if self._gen and self._gen.get("running"):
                return {"ok": False, "error": "이미 생성이 진행 중입니다."}
            self._gen_stop = threading.Event()
            self._gen = {"running": True, "done": False, "error": None,
                         "base": plan["base"], "plan": plan,
                         "created_dirs": 0, "created_files": 0, "written_bytes": 0,
                         "started_at": time.time()}
        stop = self._gen_stop

        def _progress(st):
            with self._gen_lock:
                if self._gen is not None:
                    self._gen.update(st)

        def _run():
            try:
                res = genmod.generate(
                    plan["base"], plan["n_dirs"], plan["n_subdirs"],
                    plan["n_files"], plan["file_size"],
                    progress_cb=_progress, stop_event=stop)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": "생성 오류: %s" % exc}
            with self._gen_lock:
                if self._gen is not None:
                    self._gen.update({k: res[k] for k in
                                      ("created_dirs", "created_files", "written_bytes")
                                      if k in res})
                    self._gen["running"] = False
                    self._gen["done"] = True
                    self._gen["stopped"] = bool(res.get("stopped"))
                    if not res.get("ok"):
                        self._gen["error"] = res.get("error")

        threading.Thread(target=_run, name="gentest", daemon=True).start()
        return {"ok": True, "started": True, "plan": plan}

    def gentest_status(self) -> dict:
        with self._gen_lock:
            if self._gen is None:
                return {"ok": True, "running": False, "done": False}
            return {"ok": True, **{k: v for k, v in self._gen.items()}}

    def gentest_stop(self) -> dict:
        with self._gen_lock:
            if self._gen_stop is not None:
                self._gen_stop.set()
        return {"ok": True}

    # ----- 시작 -----
    def start_scan(self, path: str, *, backend=None, size_mode=None,
                   one_file_system=None, engine=None,
                   processes=None, threads=None, confirm_outside=False,
                   auto_restart=False) -> dict:
        path = os.path.abspath(path)
        ok, err = self.scan_path_check(path, confirm_outside)
        if not ok:
            return err
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
        if engine is None:
            engine = self.settings.get("default_engine") or "threads"
        if engine not in ("threads", "pscan"):
            engine = "threads"

        mgrmod.init_manager(self.data_dir)
        db_path = mgrmod.make_run_db_path(self.data_dir, path)
        scan_id = mgrmod.register_scan(
            self.data_dir, root_path=path, db_path=db_path,
            backend=("pscan" if engine == "pscan" else backend), size_mode=size_mode,
        )
        readonly = _path_readonly(path) if self.settings.get("check_readonly", True) else None
        if auto_restart:                    # 완료(done) 시 같은 설정으로 자동 재시작(반복)
            with self._lock:
                self._auto_restart[scan_id] = {
                    "path": path, "backend": backend, "size_mode": size_mode,
                    "one_file_system": one_file_system, "engine": engine,
                    "processes": processes, "threads": threads,
                }
            self._save_auto_restart()       # 재시작·업그레이드 후에도 반복이 살아남도록 디스크에 기록
        self._log("scan #%d start: %s (engine=%s, backend=%s, size=%s, x=%s, ro=%s, loop=%s)" % (
            scan_id, path, engine, backend, size_mode, one_file_system, readonly, auto_restart))
        if engine == "pscan":
            self._launch_pscan(scan_id, db_path, path, size_mode,
                               processes=processes, threads=threads)
        else:
            self._launch(scan_id, db_path, path, backend, size_mode,
                         one_file_system, resume=False)
        return {"ok": True, "scan_id": scan_id, "db_path": db_path,
                "root_path": path, "backend": backend, "size_mode": size_mode,
                "engine": engine, "processes": processes, "threads": threads,
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

    def _launch_pscan(self, scan_id, db_path, path, size_mode,
                      processes=None, threads=None) -> None:
        """멀티프로세스(pscan) 엔진으로 '빠른 용량' 스캔을 실행한다.

        GIL 우회로 스레드 대비 처리량이 크게 오른다(측정 4워커 5.48배). 깊은 트리는
        만들지 않으므로 용량 개요 + 1단계 드릴다운까지만 표시된다(중지는 즉시 반영 안 됨).
        processes/threads 를 주면(오토튜닝 결과) 설정 대신 그 값으로 돈다.
        """
        from . import pscan as pscanmod
        stop = threading.Event()
        procs = max(1, int(processes or self.settings.get("scan_workers", 4) or 4))
        # 프로세스당 stat 스레드(고지연 NAS 왕복 은닉). 오토튜닝 값이 있으면 그것, 없으면 설정.
        tpp = max(1, int(threads or self.settings.get("pscan_threads", 1) or 1))
        mdb = mgrmod.manager_db_path(self.data_dir)

        def worker():
            started = time.time()
            mon_stop = None
            try:
                mc = dbmod.connect(mdb)
                mgrmod.update_scan(mc, scan_id, status="sizing", phase="sizing")
                mc.commit(); mc.close()
                # per-run DB 에 run 을 미리 만들고(sizing) 자원 모니터를 붙인다 →
                # pscan 중에도 /api/status 가 run 을 찾아 자원 패널·진행이 보인다(끝나면 갱신).
                import socket as _sock
                dbmod.init_db(db_path)
                pc0 = dbmod.connect(db_path)
                pc0.execute(
                    "INSERT INTO scan_runs (root_path,status,phase,backend,size_mode,"
                    "started_at,updated_at,scanner_pid,hostname,app_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (os.path.abspath(path), "sizing", "sizing", "pscan", size_mode,
                     started, started, os.getpid(), _sock.gethostname(), __version__))
                run_id = pc0.execute(
                    "SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
                pc0.commit(); pc0.close()
                mon_stop = threading.Event()
                monmod.ResourceMonitor(db_path, run_id, os.getpid(),
                                       interval=self.sample_interval,
                                       stop_event=mon_stop).start()

                def _prog(p):
                    try:
                        c = dbmod.connect(mdb)
                        mgrmod.update_scan(c, scan_id, processed_dirs=int(p.get("done", 0)),
                                           total_dirs=int(p.get("units", 0)),
                                           discovered_dirs=int(p.get("units", 0)))
                        c.commit(); c.close()
                    except Exception:  # noqa: BLE001
                        pass

                res = pscanmod.parallel_scan(path, processes=procs, threads_per_proc=tpp,
                                             size_mode=size_mode, on_progress=_prog)
                if not res.get("ok"):
                    raise RuntimeError(res.get("error") or "pscan 실패")
                pscanmod.write_run_db(db_path, path, res, size_mode=size_mode,
                                      started=started, run_id=run_id)
                fs_total = fs_used = fs_free = 0
                try:
                    v = os.statvfs(path)
                    fs_total = v.f_blocks * v.f_frsize
                    fs_free = v.f_bavail * v.f_frsize
                    fs_used = fs_total - (v.f_bfree * v.f_frsize)
                except OSError:
                    pass
                c = dbmod.connect(mdb)
                mgrmod.update_scan(c, scan_id, status="done", phase="done",
                                   scanned_bytes=int(res["total_bytes"]),
                                   total_files=int(res["total_files"]),
                                   total_dirs=int(res["total_dirs"]),
                                   processed_dirs=int(res["units"]),
                                   discovered_dirs=int(res["total_dirs"]),
                                   error_dirs=int(res.get("error_count", 0)),
                                   fs_total_bytes=fs_total, fs_used_bytes=fs_used,
                                   fs_free_bytes=fs_free, finished_at=time.time())
                c.commit(); c.close()
                self._log("pscan #%d done: %s (%d procs, %.0f files/s)" % (
                    scan_id, path, res.get("processes", 0), res.get("files_per_sec", 0)))
            except Exception as exc:  # noqa: BLE001
                try:
                    c = dbmod.connect(mdb)
                    mgrmod.update_scan(c, scan_id, status="error", phase="error",
                                       error=str(exc), finished_at=time.time())
                    c.commit(); c.close()
                except Exception:  # noqa: BLE001
                    pass
            finally:
                if mon_stop:
                    mon_stop.set()
                with self._lock:
                    self._scans.pop(scan_id, None)
                self._on_scan_finished(scan_id)

        t = threading.Thread(target=worker, name="pscan-%d" % scan_id, daemon=True)
        with self._lock:
            self._scans[scan_id] = {"stop": stop, "thread": t, "path": path,
                                    "engine": "pscan"}
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
            # 완료 후 자동 재시작(반복): 사용자가 켰고 'done'(정상 완료)이면 같은 설정으로 새 스캔.
            # 중지(paused)·오류(error)면 반복을 멈춘다(꺼내기만). 경로가 사라졌으면 재시작이 실패해 자연히 멈춘다.
            with self._lock:
                ar_cfg = self._auto_restart.pop(scan_id, None)
            if ar_cfg is not None:
                self._save_auto_restart()   # 이 scan_id 의 반복 항목 제거를 디스크에 반영
            if ar_cfg and row["status"] == "done":
                self._log("scan #%d done → 자동 재시작(반복): %s" % (scan_id, ar_cfg["path"]))
                self.start_scan(ar_cfg["path"], backend=ar_cfg["backend"],
                                size_mode=ar_cfg["size_mode"],
                                one_file_system=ar_cfg["one_file_system"],
                                engine=ar_cfg["engine"], processes=ar_cfg["processes"],
                                threads=ar_cfg["threads"], confirm_outside=True,
                                auto_restart=True)
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
            # 메일 알림 — 이벤트(완료/장애/중단)별로 켜진 조건만 발송
            status = row["status"]
            label = {"done": "스캔 완료", "error": "스캔 오류(장애)",
                     "paused": "스캔 중단"}.get(status, "스캔 종료(%s)" % status)
            gate = {"done": ("notify_on_done", True), "error": ("notify_on_error", True),
                    "paused": ("notify_on_stopped", False)}.get(status)
            if gate and self.settings.get(gate[0], gate[1]):
                self._email_scan(scan_id, row, label)
            # 중단(paused) 후 N분간 재시작/재개가 없으면 별도 알림 — 감시 등록
            if status == "paused" and self.settings.get("notify_on_stalled", False):
                mins = max(1, int(self.settings.get("notify_stall_minutes", 10) or 10))
                with self._lock:
                    self._pause_watch[scan_id] = {"due": time.time() + mins * 60,
                                                  "root": row["root_path"]}
            keep = int(self.settings.get("retention_per_root", 0))
            if keep > 0 and row["status"] in ("done", "error"):
                mgrmod.prune_scans(self.data_dir, keep_per_root=keep,
                                   running_ids=self.running_ids())
        except Exception:
            pass

    def _email_scan(self, scan_id, row, label: str) -> None:
        """스캔 한 건에 대한 알림 메일을 보낸다(받는 메일+SMTP 설정 있을 때만)."""
        if not (self.settings.get("notify_email") and self.settings.get("smtp_host")):
            return
        human = _human_bytes(row["scanned_bytes"])
        subject = "[isilon_usage] %s: %s" % (label, row["root_path"])
        bodytxt = (
            "%s\n\n스캔 #%d\n"
            "경로     : %s\n호스트   : %s\n상태     : %s\n"
            "디렉터리 : %s\n파일 수  : %s\n조사 용량: %s\n"
        ) % (label, scan_id, row["root_path"], row["hostname"], row["status"],
             row["total_dirs"], row["total_files"], human)
        from . import notify as notifymod
        notifymod.send_email(self.settings, subject, bodytxt)

    def _check_pause_watch(self, now: float) -> None:
        """중단(paused)된 스캔이 N분간 재시작/재개되지 않으면 알림 메일을 보낸다.

        같은 스캔이 재개되었거나(running_ids), 같은 루트로 새 스캔이 돌면(running_paths)
        '재시작됨'으로 보고 감시를 해제한다.
        """
        if not self._pause_watch:
            return
        running = set(self.running_ids())
        rpaths = self.running_paths()
        with self._lock:
            items = list(self._pause_watch.items())
        for sid, info in items:
            if sid in running or info.get("root") in rpaths:
                self._pause_watch.pop(sid, None)        # 재시작/재개됨 → 알림 안 함
                continue
            if now < info.get("due", 0):
                continue
            self._pause_watch.pop(sid, None)
            if self._scan_status(sid) != "paused":      # 그새 상태가 바뀌었으면 보류
                continue
            try:
                mc = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
                row = mgrmod.get_scan(mc, sid)
                mc.close()
            except Exception:
                row = None
            if row is not None:
                mins = max(1, int(self.settings.get("notify_stall_minutes", 10) or 10))
                self._email_scan(sid, row, "스캔 중단 후 %d분간 재시작 없음" % mins)

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
        # 재개 즉시 매니저 상태를 '진행 중'으로 되돌린다 — 안 그러면 'paused' 로 남아
        # 화면엔 '일시정지'로 보이고 active_scans(포탈 집계)에서도 빠진다(스캐너가 곧 정확히 갱신).
        try:
            mc = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
            mgrmod.update_scan(mc, scan_id, status="discovering", phase="discovering")
            mc.commit(); mc.close()
        except Exception:
            pass
        if row["backend"] == "pscan":           # pscan 은 부분 재개가 없으므로 전체 재스캔
            try:
                os.remove(row["db_path"])       # 깨끗한 per-run DB 로 다시 기록(혼선 방지)
            except OSError:
                pass
            self._launch_pscan(scan_id, row["db_path"], row["root_path"], row["size_mode"])
        else:
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
        # pscan(멀티프로세스)은 자식 프로세스가 끝까지 돌고 결과를 마지막에 한 번에
        # 기록한다 — stop 이벤트를 parallel_scan 이 보지 않으므로 중간 중지가 안 된다.
        # ok:True 로 거짓 성공을 알리지 말고 사실대로 알려준다(2026-06-15).
        if rec.get("engine") == "pscan":
            return {"ok": False, "engine": "pscan", "reason":
                    "pscan(멀티프로세스)은 시작하면 중간에 멈출 수 없습니다. "
                    "끝날 때까지 기다리거나, 다음 스캔부터 threads(스레드) 엔진을 쓰세요."}
        rec["stop"].set()
        return {"ok": True, "scan_id": scan_id}

    def running_ids(self) -> list:
        with self._lock:
            return sorted(self._scans.keys())

    def auto_restart_ids(self) -> list:
        """완료 후 자동 재시작(반복)이 켜진 채 도는 스캔 id 목록."""
        with self._lock:
            return sorted(self._auto_restart.keys())

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
            try:
                self._check_pause_watch(time.time())
            except Exception:
                pass

    def _check_schedules(self) -> None:
        schedules = self.settings.get("schedules", [])
        if not schedules:
            return
        now = time.time()
        running = set(self.running_ids())
        running_paths = self.running_paths()
        changed = False
        for sc in schedules:
            # 경로 목록(순차 스캔). 구버전 호환: paths 없으면 [path].
            paths = sc.get("paths") or ([sc["path"]] if sc.get("path") else [])
            if not paths:
                continue
            csid = sc.get("chain_scan_id")
            # (1) 진행 중인 체인: 현재 단계가 끝났으면 다음 경로로 넘어간다(A 끝나면 B…).
            if csid:
                if csid in running:
                    continue                       # 현재 단계 아직 진행 중 → 대기
                if self._scan_status(csid) == "paused":
                    continue                       # 사용자가 멈춤 → 자동 진행 안 함
                nxt = int(sc.get("chain_i", 0) or 0) + 1
                if nxt < len(paths):
                    sc["chain_scan_id"] = self._chain_start(
                        sc, paths[nxt], running, running_paths)  # 실패 시 None → 체인 종료
                    sc["chain_i"] = nxt
                else:
                    sc["chain_i"] = 0
                    sc["chain_scan_id"] = None      # 체인 완료
                changed = True
                continue
            # (2) 진행 중 체인 없음: 활성 + 차례면 첫 경로부터 체인 시작.
            if not sc.get("enabled", True):
                continue
            if not setmod.schedule_due(sc, now):
                continue
            sid = self._chain_start(sc, paths[0], running, running_paths)
            if sid is not None:
                sc["chain_i"] = 0
                sc["chain_scan_id"] = sid
                sc["last_run"] = now
                changed = True
        if changed:
            # last_run / 체인 진행 상태를 settings.json 에 반영
            self.settings = setmod.save(self.data_dir, self.settings)

    def _chain_start(self, sc, path, running, running_paths):
        """예약 체인의 한 단계를 시작한다. 성공 시 scan_id, 실패 시 None."""
        if path in running_paths:
            return None
        # 예약은 관리자가 미리 지정한 경로라 '지정 경로 밖'이어도 컨펌 없이 진행
        ok, _ = self.scan_path_check(path, confirm_outside=True)
        if not ok:
            return None
        res = self.start_scan(path, backend=sc.get("backend"),
                              size_mode=sc.get("size_mode"),
                              one_file_system=sc.get("one_file_system"),
                              confirm_outside=True)
        if not res.get("ok"):
            return None
        sid = res.get("scan_id")
        running_paths.add(path)             # 같은 틱에서 중복 시작 방지
        if sid is not None:
            running.add(sid)
        return sid

    def _scan_status(self, scan_id):
        """매니저 DB 에서 스캔 상태 문자열을 읽는다(없으면 None)."""
        try:
            mc = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
            row = mgrmod.get_scan(mc, scan_id)
            mc.close()
            return row["status"] if row else None
        except Exception:
            return None

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

    def _send_metrics(self) -> None:
        """Prometheus 텍스트 포맷(/metrics) — 루트별 최신 스캔 지표 + 서버 정보."""
        def esc(v):
            return str(v).replace("\\", r"\\").replace('"', r'\"').replace("\n", r"\n")

        lines = [
            "# HELP isilon_usage_info 앱 정보(버전 라벨)",
            "# TYPE isilon_usage_info gauge",
            'isilon_usage_info{version="%s"} 1' % esc(__version__),
        ]
        try:
            mconn = dbmod.connect(mgrmod.manager_db_path(self.data_dir))
            try:
                now = time.time()
                seen = set()
                metr = {
                    "scanned_bytes": [], "total_files": [], "running": [],
                    "heartbeat_age_seconds": [], "fs_total_bytes": [],
                    "fs_used_bytes": [], "fs_free_bytes": [],
                }
                for s in mgrmod.list_scans(mconn):
                    root = s["root_path"]
                    if root in seen:    # 루트별 최신 스캔 1개만
                        continue
                    seen.add(root)
                    lbl = '{root="%s"}' % esc(root)
                    running = 1 if s["status"] in ("discovering", "sizing") else 0
                    metr["scanned_bytes"].append((lbl, int(s["scanned_bytes"] or 0)))
                    metr["total_files"].append((lbl, int(s["total_files"] or 0)))
                    metr["running"].append((lbl, running))
                    if s.get("updated_at"):
                        metr["heartbeat_age_seconds"].append(
                            (lbl, max(0, int(now - s["updated_at"]))))
                    metr["fs_total_bytes"].append((lbl, int(s["fs_total_bytes"] or 0)))
                    metr["fs_used_bytes"].append((lbl, int(s["fs_used_bytes"] or 0)))
                    metr["fs_free_bytes"].append((lbl, int(s["fs_free_bytes"] or 0)))
                helps = {
                    "scanned_bytes": "루트별 최신 스캔의 조사 용량(바이트)",
                    "total_files": "루트별 최신 스캔의 총 파일 수",
                    "running": "스캔 진행 중 여부(1=진행)",
                    "heartbeat_age_seconds": "마지막 진행 갱신 경과(초)",
                    "fs_total_bytes": "대상 FS 전체 크기",
                    "fs_used_bytes": "대상 FS 사용량",
                    "fs_free_bytes": "대상 FS 여유",
                }
                for name, rows in metr.items():
                    full = "isilon_usage_root_" + name
                    lines.append("# HELP %s %s" % (full, helps[name]))
                    lines.append("# TYPE %s gauge" % full)
                    for lbl, val in rows:
                        lines.append("%s%s %d" % (full, lbl, val))
            finally:
                mconn.close()
        except Exception:
            pass
        body = ("\n".join(lines) + "\n").encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

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

    def _ask_llm_cfg(self) -> dict:
        """설정에서 로컬 LLM(선택) 구성을 모은다. enabled=False 면 규칙 기반만."""
        s = self._current_settings()
        return {
            "enabled": bool(s.get("ask_llm_enabled")),
            "endpoint": (s.get("ask_llm_endpoint") or "").strip(),
            "model": (s.get("ask_llm_model") or "").strip(),
            "key": (s.get("ask_llm_key") or "").strip(),
            "timeout": float(s.get("ask_llm_timeout") or 20),
        }

    def _gather_ask_data(self, mconn, scan_id) -> dict:
        """질의응답 엔진에 줄 공통 데이터(overall + 선택 스캔 상세)를 모은다."""
        overall = mgrmod.overall_capacity(mconn)
        for r in overall.get("roots", []):
            host = r.get("hostname")
            r["label"] = (host + ":" + r["root_path"]) if host else r["root_path"]
        data = {"scope": "edge", "overall": overall, "detail": None}
        scan_id, row = self._resolve_scan_db(mconn, scan_id)
        if row is None or not os.path.exists(row["db_path"]):
            return data
        pconn = dbmod.connect(row["db_path"])
        try:
            rid = dbmod.latest_run_id(pconn)
            if rid is None:
                return data
            owners = dbmod.get_scan_stats(pconn, rid, "owner", limit=50)
            for o in owners:
                o["name"] = _uid_name(o["key"])
            topf = dbmod.get_top_files(pconn, rid, limit=30)
            for f in topf:
                f["owner"] = _uid_name(f.get("uid"))
            topdirs = list_top_dirs(pconn, rid, sort="size", order="desc", limit=20).get("rows", [])
            data["detail"] = {
                "scan_id": scan_id, "root_path": row["root_path"],
                "age": dbmod.get_scan_stats(pconn, rid, "age"),
                "atime_age": dbmod.get_scan_stats(pconn, rid, "atime_age"),
                "owners": owners,
                "extensions": dbmod.get_scan_stats(pconn, rid, "ext", limit=50),
                "sizes": dbmod.get_scan_stats(pconn, rid, "size"),
                "top_files": topf,
                "topdirs": [{"path": d.get("path"), "bytes": d.get("total_bytes"),
                             "files": d.get("total_files")} for d in topdirs],
                "forecast": forecast_capacity(mconn, row),
            }
        except sqlite3.OperationalError:
            pass        # 초기화 전 레이스 — 상세 없이 공통만
        finally:
            pconn.close()
        return data

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

        if path == "/metrics":
            self._send_metrics()
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
                    "auto_restart": ctrl.auto_restart_ids() if ctrl else [],
                    "mount_bases": ctrl.mount_bases if ctrl else [],
                    "can_scan": ctrl is not None,
                    "have_psutil": monmod.have_psutil(),
                    "op_required": bool(ctrl and ctrl.op_required()),
                    "show_update_popup": bool(ctrl and ctrl.settings.get("show_update_popup")),
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

            if path == "/api/benchmark-workers/status":
                # 진행 중/완료된 시범 탐색의 단계별 결과 스냅샷(폴링용)
                self._send_json(self.controller.benchmark_status()
                                if self.controller else
                                {"ok": True, "running": False, "results": []})
                return

            if path == "/api/autotune/status":
                # 진행 중/완료된 오토튜닝 스냅샷(첫 화면 실시간 표시용 폴링)
                self._send_json(self.controller.autotune_status()
                                if self.controller else
                                {"ok": True, "running": False, "results": []})
                return

            if path == "/api/upgrade/status":
                # 인터넷 자동 업그레이드 상태(현재/최신 버전·확인시각·로그 — 상세 표시용)
                self._send_json(self.controller.upgrade_status()
                                if self.controller else {"ok": False})
                return

            if path == "/api/gentest/status":
                self._send_json(self.controller.gentest_status()
                                if self.controller else
                                {"ok": True, "running": False, "done": False})
                return

            if path == "/api/analyze/status":
                self._send_json(self.controller.analyze_status()
                                if self.controller else
                                {"ok": True, "running": False, "done": False, "result": None})
                return

            if path == "/api/systune":
                if not self.controller:
                    self._send_json({"ok": False, "error": "웹 스캔 비활성(점검 불가)."})
                    return
                self._send_json(self.controller.systune_check(
                    scan_root=qs.get("path", [None])[0]))
                return

            if path == "/api/troubleshoot/history":
                self._send_json(self.controller.troubleshoot_history(
                    limit=int(qs.get("limit", ["200"])[0] or 200))
                    if self.controller else {"ok": True, "events": []})
                return

            if path == "/api/troubleshoot/samples":
                if not self.controller:
                    self._send_json({"ok": True, "samples": []})
                    return
                try:
                    secs = int(qs.get("seconds", ["86400"])[0])
                    mx = int(qs.get("max", ["600"])[0])
                except (TypeError, ValueError):
                    secs, mx = 86400, 600
                self._send_json(self.controller.rate_samples(
                    seconds=max(5, min(86400, secs)), max_points=max(10, min(2000, mx))))
                return

            if path == "/api/troubleshoot/throughput":
                if not self.controller:
                    self._send_json({"ok": True, "buckets": []})
                    return
                try:
                    bk = int(qs.get("bucket", ["60"])[0])
                    mx = int(qs.get("max", ["60"])[0])
                except (TypeError, ValueError):
                    bk, mx = 60, 60
                self._send_json(self.controller.throughput(
                    bucket_sec=max(60, min(3600, bk)), max_buckets=max(2, min(200, mx))))
                return

            if path == "/api/troubleshoot":
                if not self.controller:
                    self._send_json({"ok": False, "error": "웹 스캔 비활성(진단 불가)."})
                    return
                try:
                    secs = float(qs.get("sample", ["1.2"])[0])
                except (TypeError, ValueError):
                    secs = 1.2
                self._send_json(self.controller.troubleshoot(
                    sample_sec=max(0.2, min(5.0, secs))))
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
                confirm = qs.get("confirm", ["0"])[0] in ("1", "true", "yes", "on")
                ctrl = self.controller
                if not qpath and ctrl and ctrl.mount_bases:
                    qpath = ctrl.mount_bases[0]
                # 지정 경로(mount_bases) 밖이면 하드 차단 대신 '컨펌 가능' 신호를 준다.
                # 사용자가 컨펌하면 클라이언트가 confirm=1 로 다시 호출 → 자유 탐색 허용.
                if (ctrl and ctrl.mount_bases and not confirm
                        and not ctrl.browse_allowed(qpath or "/")):
                    self._send_json({"ok": False, "reason": "outside_base",
                                     "path": os.path.abspath(qpath or "/"),
                                     "allowed": ctrl.mount_bases})
                    return
                result = browse_dir(qpath)
                # 컨펌 전에는 지정 경로 밖으로 '위로' 올라가지 못하게 묶는다(컨펌하면 자유).
                if (ctrl and ctrl.mount_bases and not confirm and result.get("ok")
                        and result.get("parent")
                        and not ctrl.browse_allowed(result["parent"])):
                    result["parent"] = result["path"]
                self._send_json(result)
                return

            if path == "/api/status":
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None:
                    self._send_json(dict({"ok": False, "reason": "no_runs",
                                          "have_psutil": monmod.have_psutil()},
                                         **_idle_resources()))
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

            if path == "/api/stats":
                # 파일 나이/소유자/확장자별 집계 리포트
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None or not os.path.exists(row["db_path"]):
                    self._send_json({"ok": True, "age": [], "atime_age": [],
                                     "owners": [], "extensions": [], "sizes": []})
                    return
                pconn = dbmod.connect(row["db_path"])
                try:
                    rid = dbmod.latest_run_id(pconn)
                    age = dbmod.get_scan_stats(pconn, rid, "age")
                    age.sort(key=lambda r: _AGE_ORDER.index(r["key"])
                             if r["key"] in _AGE_ORDER else 99)
                    atime_age = dbmod.get_scan_stats(pconn, rid, "atime_age")
                    atime_age.sort(key=lambda r: _AGE_ORDER.index(r["key"])
                                   if r["key"] in _AGE_ORDER else 99)
                    owners = dbmod.get_scan_stats(pconn, rid, "owner", limit=200)
                    for o in owners:
                        o["name"] = _uid_name(o["key"])
                    exts = dbmod.get_scan_stats(pconn, rid, "ext", limit=200)
                    sizes = dbmod.get_scan_stats(pconn, rid, "size")
                    _so = ["0 (빈 파일)", "1B~1KB", "1KB~1MB", "1~10MB", "10~100MB",
                           "100MB~1GB", "1~10GB", "10GB+"]
                    sizes.sort(key=lambda r: _so.index(r["key"]) if r["key"] in _so else 99)
                    topf = dbmod.get_top_files(pconn, rid, limit=200)   # 저장된 Top-N 전부(‘더 보기’용)
                    for f in topf:
                        f["owner"] = _uid_name(f.get("uid"))
                    try:
                        from . import systune as sysmod
                        atime_info = sysmod.atime_policy(row["root_path"])
                    except Exception:   # noqa: BLE001
                        atime_info = {"opt": "unknown", "reliable": None}
                    self._send_json({"ok": True, "scan_id": scan_id, "age": age,
                                     "atime_age": atime_age, "atime_info": atime_info,
                                     "owners": owners, "extensions": exts,
                                     "sizes": sizes, "top_files": topf})
                finally:
                    pconn.close()
                return

            if path == "/api/ask":
                # 자연어 질의응답(규칙 기반 + 선택적 로컬 LLM). 읽기 전용.
                q = (qs.get("q", [""])[0] or "").strip()
                data = self._gather_ask_data(mconn, self._query_int(qs, "scan"))
                self._send_json(askmod.respond(q, data, self._ask_llm_cfg()))
                return

            if path == "/api/forecast":
                # 용량 소진 예측: 같은 루트의 완료 스캔 이력으로 선형 추세 계산
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None:
                    self._send_json({"ok": False, "reason": "no_runs"})
                    return
                self._send_json(forecast_capacity(mconn, row))
                return

            if path == "/api/growers":
                # 변화 리포트: 직전 완료 스캔 대비 가장 많이 커진/줄어든/신규/삭제
                scan_id, row = self._resolve_scan_db(mconn, self._query_int(qs, "scan"))
                if row is None:
                    self._send_json({"ok": False, "reason": "no_runs"})
                    return
                prev = mconn.execute(
                    """SELECT id FROM scans WHERE root_path=? AND id<? AND status='done'
                       ORDER BY id DESC LIMIT 1""",
                    (row["root_path"], scan_id)).fetchone()
                if not prev:
                    self._send_json({"ok": True, "has_base": False,
                                     "reason": "비교할 이전 완료 스캔이 없습니다."})
                    return
                limit = self._query_int(qs, "limit") or 15
                d = diff_scans(self.data_dir, int(prev["id"]), scan_id,
                               limit=400, max_depth=self._query_int(qs, "max_depth"))
                if not d.get("ok"):
                    self._send_json(d)
                    return
                rows = d["rows"]
                out = {
                    "ok": True, "has_base": True,
                    "base_scan": d["base"], "target_scan": d["target"],
                    "total_delta": d["total_delta"],
                    "growers": [r for r in rows if r["delta"] > 0][:limit],
                    "shrinkers": [r for r in rows if r["delta"] < 0][:limit],
                    "added": [r for r in rows if r["base_bytes"] == 0
                              and r["target_bytes"] > 0][:limit],
                    "removed": [r for r in rows if r["target_bytes"] == 0
                                and r["base_bytes"] > 0][:limit],
                }
                self._send_json(out)
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

            if path == "/api/folder-history":
                # 특정 폴더의 크기·파일수가 완료 스캔들에서 어떻게 변해왔는지(이력)
                root = (qs.get("root", [""])[0] or "").strip()
                dpath = (qs.get("path", [""])[0] or "").strip()
                if not root or not dpath:
                    self._send_json({"ok": False, "reason": "root/path 필요"}, status=400)
                    return
                limit = max(2, min(self._query_int(qs, "limit") or 30, 100))
                scans = mconn.execute(
                    "SELECT id, db_path, started_at FROM scans "
                    "WHERE root_path=? AND status='done' ORDER BY id DESC LIMIT ?",
                    (root, limit)).fetchall()
                points = []
                for s in scans:
                    dbp = s["db_path"]
                    if not dbp or not os.path.exists(dbp):
                        continue
                    try:
                        c = dbmod.connect(dbp)
                        try:
                            r = c.execute(
                                "SELECT total_bytes, total_files FROM directories "
                                "WHERE run_id=(SELECT MAX(id) FROM scan_runs) AND path=?",
                                (dpath,)).fetchone()
                        finally:
                            c.close()
                    except Exception:  # noqa: BLE001
                        r = None
                    if r is not None:
                        points.append({
                            "scan_id": int(s["id"]), "started_at": s["started_at"],
                            "total_bytes": int(r["total_bytes"]),
                            "total_files": int(r["total_files"])})
                points.reverse()  # 오래된→최신 순으로
                for i, p in enumerate(points):
                    prev = points[i - 1] if i else None
                    p["bytes_delta"] = (p["total_bytes"] - prev["total_bytes"]) if prev else 0
                    p["files_delta"] = (p["total_files"] - prev["total_files"]) if prev else 0
                self._send_json({"ok": True, "root": root, "path": dpath,
                                 "found": len(points), "points": points})
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
        # 반복(완료 후 자동 재시작) 중인 루트를 표시해 포탈도 '반복 동작 중'을 알 수 있게 한다.
        if self.controller:
            _ar = set(self.controller.auto_restart_ids())
            if _ar:
                for _r in overall.get("roots", []):
                    if _r.get("scan_id") in _ar:
                        _r["auto_restart"] = True
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

    def _audit(self, action: str, ok: bool, detail: str = "") -> None:
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:  # noqa: BLE001
            ip = ""
        auditmod.record(self.data_dir, action=action, ok=ok, ip=ip, detail=detail)

    def _handle_upgrade_push(self) -> None:
        """포탈이 푸시한 새 코드 번들(tar.gz)을 적용한다(api_token 인증, 적용 후 재시작)."""
        qs = parse_qs(urlparse(self.path).query)
        tok = (self._current_settings().get("api_token") or "").strip()
        if not tok:
            self._send_json({"ok": False,
                             "reason": "원격 업그레이드는 api_token 설정이 필요합니다."}, status=403)
            return
        if not self._authorized(qs):
            self._send_json({"ok": False, "reason": "unauthorized"}, status=401)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        data = self.rfile.read(length) if length > 0 else b""
        if not data:
            self._send_json({"ok": False, "reason": "번들 없음"}, status=400)
            return
        res = upgrademod.upgrade_from_bundle_bytes(
            data, upgrademod.code_dir_of(__file__), __version__)
        self._audit("upgrade_push", bool(res.get("ok")),
                    res.get("version") or res.get("reason") or "")
        self._send_json(res, status=200 if res.get("ok") else 400)
        if res.get("ok"):
            def _later():
                time.sleep(1.0)
                self.controller._restart_for_upgrade()
            threading.Thread(target=_later, name="upgrade-restart", daemon=True).start()

    def _handle_op_password_push(self) -> None:
        """포탈이 푸시한 작업 보호 비밀번호 설정/변경/해제(api_token 인증).

        op_password 가 아니라 api_token(X-Auth-Token)으로 인증한다 — 그래야 포탈이
        엣지 비밀번호를 모르는 상태에서도 설정/변경/해제할 수 있다(/api/upgrade 와 같은 권한 모델).
        """
        qs = parse_qs(urlparse(self.path).query)
        tok = (self._current_settings().get("api_token") or "").strip()
        if not tok:
            self._send_json({"ok": False,
                             "reason": "원격 비밀번호 관리는 api_token 설정이 필요합니다."}, status=403)
            return
        if not self._authorized(qs):
            self._send_json({"ok": False, "reason": "unauthorized"}, status=401)
            return
        body = self._read_json_body()
        res = self.controller.set_op_password(
            str(body.get("password") or ""), clear=bool(body.get("clear")))
        self._audit("op_password_push", bool(res.get("ok")), res.get("reason") or "")
        self._send_json(res, status=200 if res.get("ok") else 400)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if self.controller is None:
            self._send_json({"ok": False,
                             "reason": "이 서버는 웹 스캔이 비활성화되어 있습니다."},
                            status=403)
            return
        if path == "/api/upgrade":            # 포탈이 푸시하는 원격 업그레이드(api_token 인증)
            self._handle_upgrade_push()
            return
        if path == "/api/op-password":        # 포탈이 푸시하는 작업 비밀번호 관리(api_token 인증)
            self._handle_op_password_push()
            return
        body = self._read_json_body()
        try:
            # 작업 잠금 해제: 비밀번호 → 토큰
            if path == "/api/unlock":
                res = self.controller.unlock(body.get("password") or "")
                self._audit("login", res.get("ok"),
                            "" if res.get("ok") else (res.get("reason") or ""))
                self._send_json(res, status=200 if res.get("ok") else 401)
                return
            # 작업(POST) 보호: 비밀번호가 설정돼 있으면 유효한 토큰이 필요(보기=GET 은 자유)
            if self.controller.op_required() and not self.controller.op_token_valid(
                    self.headers.get("X-Op-Token")):
                self._audit(path, False, "locked")
                self._send_json({"ok": False, "reason": "locked", "op_required": True,
                                 "error": "작업하려면 비밀번호로 잠금을 해제하세요."},
                                status=401)
                return

            if path == "/api/audit":            # 감사 로그 조회(로그인 필요)
                self._send_json({"ok": True, "events": auditmod.tail(self.data_dir, 300)})
                return
            self._audit(path, True)             # 인증 통과한 변경 작업 기록

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
                    engine=body.get("engine"),
                    confirm_outside=bool(body.get("confirm_outside")),
                    auto_restart=bool(body.get("auto_restart")),
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

            if path == "/api/benchmark-workers/start":
                # 백그라운드 시작 → /api/benchmark-workers/status 로 단계별 폴링
                res = self.controller.benchmark_start(
                    path=(body.get("path") or "").strip() or None,
                    candidates=body.get("candidates"),
                    budget=body.get("budget", 5.0),
                    mem_factor=body.get("mem_factor", 2.0),
                )
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path in ("/api/gentest/validate", "/api/gentest/start"):
                kw = dict(
                    path=(body.get("path") or "").strip(),
                    n_dirs=body.get("n_dirs", 0), n_subdirs=body.get("n_subdirs", 0),
                    n_files=body.get("n_files", 0), file_size=body.get("file_size", 0),
                )
                if path == "/api/gentest/validate":
                    res = self.controller.gentest_validate(**kw)
                else:
                    res = self.controller.gentest_start(**kw)
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path == "/api/gentest/stop":
                self._send_json(self.controller.gentest_stop())
                return

            if path == "/api/autotune/start":
                # 오토튜닝 측정 시작 → 완료 후 best 로 본 스캔 자동 시작(then_scan)
                res = self.controller.autotune_start(
                    path=(body.get("path") or "").strip() or None,
                    secs=body.get("secs"),
                    then_scan=body.get("then_scan", True),
                    confirm_outside=bool(body.get("confirm_outside")),
                )
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path == "/api/autotune/stop":
                self._send_json(self.controller.autotune_stop())
                return

            if path == "/api/upgrade/check":     # 지금 인터넷에서 최신 버전 확인
                self._send_json(self.controller.upgrade_check()
                                if self.controller else {"ok": False})
                return
            if path == "/api/upgrade/install":   # 지금 최신 버전 다운로드·설치·재시작
                self._send_json(self.controller.upgrade_install()
                                if self.controller else {"ok": False})
                return

            if path == "/api/analyze/start":
                res = self.controller.analyze_start(
                    path=(body.get("path") or "").strip() or None,
                    max_depth=body.get("max_depth", 0),
                    dir_limit=body.get("limit", 0),
                    time_budget=body.get("timeout", 0.0),
                    workers=body.get("workers", 8),
                )
                self._send_json(res, status=200 if res.get("ok") else 400)
                return

            if path == "/api/analyze/stop":
                self._send_json(self.controller.analyze_stop())
                return

            if path == "/api/troubleshoot/history/clear":
                self._send_json(self.controller.troubleshoot_history_clear())
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

            if path == "/api/schedules/stagger":
                cur = list(self.controller.settings.get("schedules", []))
                staggered = setmod.stagger_schedules(cur)
                res = self.controller.update_settings({"schedules": staggered})
                if res.get("ok"):
                    res["staggered"] = sum(1 for s in staggered if s.get("enabled", True))
                self._send_json(res, status=200 if res.get("ok") else 403)
                return

            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass  # 클라이언트가 응답 도중 연결을 끊음 — 무시
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def _warn_if_insecure(host: str, controller: "ScanController") -> None:
    """비루프백 주소에 '무인증'으로 기동하면 stderr 로 보안 경고를 출력한다.

    --host 0.0.0.0(기본)처럼 외부에 노출되는데 작업 보호 비밀번호도 없으면
    네트워크에서 접근 가능한 누구나 스캔 시작·설정 변경을 할 수 있어 위험하다.
    경고만 하고 기동은 막지 않는다(신뢰망 LAN 운영을 방해하지 않기 위해).
    """
    if host in ("127.0.0.1", "localhost", "::1", ""):
        return
    if (controller.settings.get("op_password") or "").strip():
        return
    lines = [
        "",
        "  ⚠  보안 경고: 비루프백 주소(%s)에 '작업 보호 비밀번호 없이' 기동합니다." % host,
        "     네트워크에서 접근 가능한 누구나 스캔 시작·설정 변경을 할 수 있습니다.",
        "     공개망/비신뢰망이라면 다음을 권장합니다:",
        "       - --host 127.0.0.1 로 묶고 리버스 프록시(HTTPS/인증)나 SSH 터널 사용",
        "       - 대시보드 '설정'에서 작업 보호 비밀번호 지정",
        "       - 설정 편집을 막으려면 --lock-settings",
        "       - 스캔 허용 경로를 제한하려면 --mount-base <경로>",
        "",
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()


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
        _warn_if_insecure(host, controller)
    handler = type("BoundHandler", (DashboardHandler,),
                   {"data_dir": data_dir, "controller": controller,
                    "bound_host": host, "bound_port": port})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller  # run 모드에서 초기 스캔 시작/정리에 사용
    return httpd

"""관리(매니저) DB — 여러 번의 스캔을 카탈로그로 묶어 전체 용량을 관리한다.

구조:
  <data-dir>/
    manager.db            ← 이 모듈이 다루는 관리 DB (모든 스캔의 요약 카탈로그)
    scans/
      scan_<시각>_<경로>.db   ← 실행마다 만들어지는 per-run DB(상세 데이터)

per-run DB 에는 그 스캔의 상세(디렉터리별 집계, 자원 시계열)가 들어가고,
manager.db 에는 각 스캔의 "요약 한 줄"이 들어가 전체를 한눈에 관리한다.
스캐너가 진행하면서 manager.db 의 해당 행을 주기적으로 갱신한다.
"""

from typing import Dict, Optional

import os
import re
import secrets
import socket
import time

from . import db as dbmod


DEFAULT_DATA_DIR = "isilon_data"
MANAGER_DB_NAME = "manager.db"
SCANS_SUBDIR = "scans"


MANAGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    db_path         TEXT    NOT NULL,
    db_filename     TEXT    NOT NULL,
    root_path       TEXT    NOT NULL,
    hostname        TEXT,
    backend         TEXT,
    size_mode       TEXT,
    status          TEXT    NOT NULL DEFAULT 'discovering',
    phase           TEXT    NOT NULL DEFAULT 'discovering',
    started_at      REAL,
    updated_at      REAL,
    finished_at     REAL,
    discovered_dirs INTEGER NOT NULL DEFAULT 0,
    total_dirs      INTEGER NOT NULL DEFAULT 0,
    processed_dirs  INTEGER NOT NULL DEFAULT 0,
    total_files     INTEGER NOT NULL DEFAULT 0,
    error_dirs      INTEGER NOT NULL DEFAULT 0,
    scanned_bytes   INTEGER NOT NULL DEFAULT 0,
    fs_total_bytes  INTEGER NOT NULL DEFAULT 0,
    fs_used_bytes   INTEGER NOT NULL DEFAULT 0,
    fs_free_bytes   INTEGER NOT NULL DEFAULT 0,
    current_dir     TEXT,
    app_version     TEXT,
    note            TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_scans_root ON scans(root_path, id);
CREATE INDEX IF NOT EXISTS idx_scans_status ON scans(status);
"""


# ---------------------------------------------------------------- 경로 헬퍼

def manager_db_path(data_dir: str) -> str:
    return os.path.join(data_dir, MANAGER_DB_NAME)


def scans_dir(data_dir: str) -> str:
    return os.path.join(data_dir, SCANS_SUBDIR)


def init_manager(data_dir: str) -> str:
    """data-dir 레이아웃과 manager.db 를 준비하고 manager.db 경로를 반환."""
    from . import SCHEMA_VERSION

    os.makedirs(scans_dir(data_dir), exist_ok=True)
    mpath = manager_db_path(data_dir)
    conn = dbmod.connect(mpath)
    try:
        conn.executescript(MANAGER_SCHEMA)
        dbmod.ensure_column(conn, "scans", "app_version", "TEXT")
        conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
        conn.commit()
    finally:
        conn.close()
    return mpath


def make_run_db_path(data_dir: str, root_path: str) -> str:
    """루트 경로 + 현재 시각 + 고유 토큰으로 per-run DB 파일 경로를 생성.

    같은 루트를 같은 초에 두 번 시작해도 파일명이 충돌하지 않도록 짧은 난수
    토큰을 붙인다(충돌 시 두 스캔이 같은 DB 를 공유하는 문제 방지).
    """
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", root_path.strip("/")) or "root"
    base = base[-60:].strip("_") or "root"
    ts = time.strftime("%Y%m%d-%H%M%S")
    token = secrets.token_hex(3)
    return os.path.join(scans_dir(data_dir), f"scan_{ts}_{token}_{base}.db")


# ---------------------------------------------------------------- CRUD

def register_scan(
    data_dir: str,
    *,
    root_path: str,
    db_path: str,
    backend: str,
    size_mode: str,
) -> int:
    """새 스캔을 관리 DB 에 등록하고 manager 측 scan_id 를 반환."""
    from . import __version__

    conn = dbmod.connect(manager_db_path(data_dir))
    try:
        now = time.time()
        try:
            host = socket.gethostname()
        except Exception:
            host = None
        cur = conn.execute(
            """
            INSERT INTO scans
                (db_path, db_filename, root_path, hostname, backend, size_mode,
                 status, phase, started_at, updated_at, app_version)
            VALUES (?,?,?,?,?,?,'discovering','discovering',?,?,?)
            """,
            (os.path.abspath(db_path), os.path.basename(db_path), root_path,
             host, backend, size_mode, now, now, __version__),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def update_scan(conn, scan_id: int, **fields) -> None:
    """관리 DB 의 스캔 요약 한 줄을 갱신(updated_at 자동)."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE scans SET {cols} WHERE id=?",
                 list(fields.values()) + [scan_id])


def get_scan(conn, scan_id: int):
    return conn.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()


def list_scans(conn, limit: int = 200) -> list:
    rows = conn.execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def pick_default_scan(conn) -> Optional[int]:
    """대시보드 기본 표시 대상: 진행 중인 스캔 우선, 없으면 가장 최근."""
    row = conn.execute(
        "SELECT id FROM scans WHERE status IN ('discovering','sizing') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return int(row["id"])
    row = conn.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def scans_for_root(conn, root_path: str) -> list:
    """특정 루트의 모든 스캔(오래된→최신). 용량 추세 그래프용."""
    rows = conn.execute(
        "SELECT * FROM scans WHERE root_path=? ORDER BY id ASC", (root_path,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_scan(data_dir: str, scan_id: int) -> dict:
    """스캔의 per-run DB 파일과 관리 DB 행을 삭제한다."""
    conn = dbmod.connect(manager_db_path(data_dir))
    try:
        row = get_scan(conn, scan_id)
        if row is None:
            return {"ok": False, "reason": "없는 scan"}
        db_path = row["db_path"]
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
        conn.commit()
        return {"ok": True, "scan_id": scan_id}
    finally:
        conn.close()


def prune_scans(data_dir: str, *, keep_per_root: Optional[int] = None,
                older_than_days: Optional[float] = None,
                running_ids=None) -> dict:
    """오래된 스캔을 정리한다.

    keep_per_root: 루트별로 최신 N개만 남기고 나머지 삭제.
    older_than_days: 그보다 오래된(완료/오류) 스캔 삭제.
    실행 중(running_ids)인 스캔은 건너뛴다.
    """
    running = set(running_ids or [])
    conn = dbmod.connect(manager_db_path(data_dir))
    try:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    finally:
        conn.close()

    to_delete = set()
    if keep_per_root and keep_per_root > 0:
        seen: Dict[str, int] = {}
        for r in rows:
            rp = r["root_path"]
            seen[rp] = seen.get(rp, 0) + 1
            if seen[rp] > keep_per_root:
                to_delete.add(r["id"])
    if older_than_days and older_than_days > 0:
        cutoff = time.time() - older_than_days * 86400
        for r in rows:
            ts = r["finished_at"] or r["started_at"] or 0
            if ts and ts < cutoff and r["status"] in ("done", "error", "paused"):
                to_delete.add(r["id"])

    deleted = []
    for sid in to_delete:
        if sid in running:
            continue
        if delete_scan(data_dir, sid).get("ok"):
            deleted.append(sid)
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


def overall_capacity(conn) -> dict:
    """전체 용량 관리 집계.

    같은 루트를 여러 번 스캔했을 수 있으므로, 루트별로 "가장 최근 스캔"만
    골라 합산한다(과거 스캔 중복 합산 방지). 루트별 최신 요약 목록도 함께 준다.
    """
    scans = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    latest_by_root: Dict[str, dict] = {}
    for s in scans:
        rp = s["root_path"]
        if rp not in latest_by_root:  # id 내림차순이므로 처음 본 게 최신
            latest_by_root[rp] = dict(s)

    roots = []
    total_scanned = 0
    for rp, s in latest_by_root.items():
        total_scanned += s.get("scanned_bytes") or 0
        roots.append({
            "scan_id": s["id"],
            "root_path": rp,
            "hostname": s.get("hostname"),
            "status": s["status"],
            "phase": s["phase"],
            "scanned_bytes": s.get("scanned_bytes") or 0,
            "total_dirs": s.get("total_dirs") or 0,
            "total_files": s.get("total_files") or 0,
            "fs_total_bytes": s.get("fs_total_bytes") or 0,
            "fs_used_bytes": s.get("fs_used_bytes") or 0,
            "fs_free_bytes": s.get("fs_free_bytes") or 0,
            "finished_at": s.get("finished_at"),
            "updated_at": s.get("updated_at"),
        })
    roots.sort(key=lambda x: x["scanned_bytes"], reverse=True)

    active = sum(1 for s in scans if s["status"] in ("discovering", "sizing"))
    return {
        "total_scanned_bytes": total_scanned,
        "root_count": len(latest_by_root),
        "scan_count": len(scans),
        "active_scans": active,
        "roots": roots,
    }

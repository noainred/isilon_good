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
import shutil
import socket
import sys
import threading
import time

from . import db as dbmod


# 모든 영속 데이터/설정의 기본 위치 — 절대경로로 고정해 '실행 위치'나 '코드 업그레이드'
# 와 무관하게 항상 같은 곳에 저장한다(업그레이드 시 설정 유실 방지). --data-dir 로 변경 가능.
DEFAULT_DATA_DIR = "/data/isilon_usage"
MANAGER_DB_NAME = "manager.db"
SCANS_SUBDIR = "scans"

# 레거시(이전 기본/예시) data-dir — 기본 위치로 설정을 1회 이관해 업그레이드 보존.
LEGACY_DATA_DIRS = (
    "/var/lib/isilon_usage", "/var/lib/isilon_portal",
    "isilon_data", "isilon_portal_data",
)
# 이관 대상은 '설정'만(스캔/복제 DB 는 재생성 가능하므로 옮기지 않는다).
MIGRATE_CONFIG_FILES = ("settings.json", "portal_nodes.json")


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


def migrate_legacy_config(data_dir: str) -> None:
    """업그레이드로 설정이 사라지지 않도록, 기본 data-dir 로 레거시 설정을 1회 복사한다.

    - 기본(canonical) data-dir 일 때만 동작한다(사용자가 직접 지정한 경로는 건드리지 않음).
    - 새 위치에 그 설정이 아직 없을 때만 레거시에서 복사한다(비파괴: 원본은 남긴다).
    - 베스트에포트 — 어떤 오류도 기동을 막지 않는다.
    """
    try:
        target = os.path.abspath(data_dir)
        if target != os.path.abspath(DEFAULT_DATA_DIR):
            return
        os.makedirs(target, exist_ok=True)
        for fname in MIGRATE_CONFIG_FILES:
            dst = os.path.join(target, fname)
            if os.path.exists(dst):
                continue
            for legacy in LEGACY_DATA_DIRS:
                src_dir = os.path.abspath(legacy)
                if src_dir == target:
                    continue
                src = os.path.join(src_dir, fname)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    sys.stderr.write(
                        "[isilon_usage] 설정 이관(업그레이드 보존): %s -> %s\n" % (src, dst)
                    )
                    break
    except OSError:
        pass


def init_manager(data_dir: str) -> str:
    """data-dir 레이아웃과 manager.db 를 준비하고 manager.db 경로를 반환."""
    from . import SCHEMA_VERSION

    os.makedirs(scans_dir(data_dir), exist_ok=True)
    mpath = manager_db_path(data_dir)
    conn = dbmod.connect(mpath)
    try:
        conn.executescript(MANAGER_SCHEMA)
        dbmod.ensure_column(conn, "scans", "app_version", "TEXT")
        dbmod.ensure_column(conn, "scans", "partial_agg_at", "REAL")   # 멈춘 자료 부분 집계 시각
        # 시작 옵션(재개 때 그대로 복원 — 다르면 재개분이 다른 마운트로 넘어가거나 fold 깊이 체계가 어긋남)
        dbmod.ensure_column(conn, "scans", "opt_one_fs", "INTEGER")
        dbmod.ensure_column(conn, "scans", "opt_fold_depth", "INTEGER")
        dbmod.ensure_column(conn, "scans", "opt_max_depth", "INTEGER")
        # 멈춘 이유: 'orphan' = 실행 중 프로세스가 죽어 재기동 정리로 멈춤(예약 스캔은 이어서 재개)
        dbmod.ensure_column(conn, "scans", "paused_by", "TEXT")
        # 마운트 출처(포탈이 같은 파일시스템 식별 — 크기만 같은 다른 볼륨을 하나로 합치지 않게)
        dbmod.ensure_column(conn, "scans", "fs_source", "TEXT")
        # 모든 세션의 '활성' 작업 시간(일시정지 갭 제외) — 남은 시간 추정이 쉬던 시간까지 경과로 치지 않게
        dbmod.ensure_column(conn, "scans", "active_elapsed", "REAL")
        dbmod.ensure_column(conn, "scans", "sizing_from_active", "REAL")
        dbmod.ensure_column(conn, "scans", "sizing_from_processed", "INTEGER")
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
            (os.path.abspath(db_path), os.path.basename(db_path), dbmod.db_text(root_path),
             host, backend, size_mode, now, now, __version__),
        )
        conn.commit()
        sid = int(cur.lastrowid)
    finally:
        conn.close()
    try:
        unrecord_deleted(data_dir, os.path.basename(db_path))
    except Exception:  # noqa: BLE001
        pass
    return sid


# ---- 사용자가 지운 스캔의 표시(포탈 복제본도 지우게) ----
# 포탈은 증분 복제라 엣지에서 지운 것을 알 길이 없어, 지운 스캔이 복제본에 남아 비교 화면에서 계속 '최신'으로
# 보였다(18회차 점검). 보존 정리(prune)는 표시하지 않는다 — 포탈은 자기 보관 규칙으로 이력을 따로 둔다.
# 같은 DB 파일 이름으로 다시 등록하면(CLI scan --db 고정 경로) 표시를 지운다 — 남아 있으면 포탈이 새 스캔까지
# 지웠다(19회차 점검).
_TOMB_LOCK = threading.Lock()


def _deleted_path(data_dir: str) -> str:
    return os.path.join(data_dir, "deleted_scans.json")


def _edit_deleted(data_dir: str, fn) -> None:
    from . import settings as setmod
    with _TOMB_LOCK:
        d = setmod.read_json_with_backup(_deleted_path(data_dir)) or {}
        lst = [x for x in (d.get("deleted") or []) if isinstance(x, dict) and isinstance(x.get("db_filename"), str)]
        new = fn(lst)
        if new is not None:
            setmod.write_json_atomic(_deleted_path(data_dir), {"deleted": new[-2000:]})


def record_deleted(data_dir: str, db_filename: str) -> None:
    _edit_deleted(data_dir, lambda lst: lst + [{"db_filename": db_filename, "at": time.time()}])


def unrecord_deleted(data_dir: str, db_filename: str) -> None:
    if not os.path.exists(_deleted_path(data_dir)):
        return

    def _f(lst):
        kept = [x for x in lst if x["db_filename"] != db_filename]
        return kept if len(kept) != len(lst) else None
    _edit_deleted(data_dir, _f)


def deleted_db_filenames(data_dir: str) -> list:
    from . import settings as setmod
    d = setmod.read_json_with_backup(_deleted_path(data_dir)) or {}
    return [str(x["db_filename"]) for x in (d.get("deleted") or [])
            if isinstance(x, dict) and isinstance(x.get("db_filename"), str)]


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


def active_scan_on(data_dir: str, path: str, *, exclude=(), stale_secs: float = 900) -> Optional[int]:
    """매니저 DB 에서 루트 path 를 지금 스캔 중인(진행 상태 + 최근 하트비트) 스캔 id — 서버 시작·CLI scan 이 같은
    규칙을 쓴다(단일 정책, 40회차 점검: CLI 는 서버가 돌리는 루트를 또 스캔해 NAS 부하가 두 배였다).
    심볼릭 링크로 가리킨 같은 트리도 같은 루트로 본다. 오래된(죽은) 진행 상태는 무시한다."""
    path = os.path.abspath(path)

    def _same(a):
        a = os.path.abspath(a or "")
        if a == path:
            return True
        try:
            return os.path.realpath(a) == os.path.realpath(path)
        except (OSError, ValueError):
            return False
    try:
        mc = dbmod.connect(manager_db_path(data_dir))
        try:
            rows = [r for r in mc.execute(
                "SELECT id, updated_at, root_path FROM scans "
                "WHERE status IN ('discovering','sizing')").fetchall()
                    if _same(dbmod.fs_text(r["root_path"]))]
        finally:
            mc.close()
    except Exception:  # noqa: BLE001 — 매니저 DB 가 없으면(첫 실행) 중복도 없다
        return None
    now = time.time()
    for r in rows:
        sid = int(r["id"])
        if sid in exclude:
            continue
        if now - float(r["updated_at"] or 0) < stale_secs:
            return sid
    return None


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
        # 같은 per-run DB 를 다른 스캔 행도 쓰면(--db 고정 경로로 다시 스캔) 파일은 지우지 않는다 — 예전엔 옛 스캔을 지우거나
        # 정리하면 최신 스캔의 DB 까지 지워져 결과가 사라졌다(33회차 점검)
        shared = conn.execute("SELECT 1 FROM scans WHERE db_path=? AND id<>? LIMIT 1", (db_path, scan_id)).fetchone()
        if not shared:
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
    실행 중(running_ids)인 스캔과, 상태가 진행 중(discovering/sizing — 다른 프로세스가 돌리는 것 포함)·
    일시정지(paused — 이어서 할 수 있음)인 스캔은 어떤 규칙에서도 지우지 않는다(예전엔 keep_per_root 가
    상태를 보지 않아 진행 중·일시정지 스캔의 DB 까지 지웠다).
    """
    running = set(running_ids or [])
    conn = dbmod.connect(manager_db_path(data_dir))
    try:
        rows = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    finally:
        conn.close()

    # 루트별 '마지막 완료(done)' 스캔은 어떤 정리 규칙에서도 지우지 않는다 — 예전엔 id 순으로만 남겨,
    # 최근 스캔이 멈춤/오류면 마지막 정상 결과가 지워지고 부분 결과만 남았다.
    protected = set()
    _done_seen = set()
    for r in rows:
        if r["status"] == "done" and r["root_path"] not in _done_seen:
            _done_seen.add(r["root_path"])
            protected.add(r["id"])

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
            if ts and ts < cutoff and r["status"] in ("done", "error"):
                to_delete.add(r["id"])

    for r in rows:
        if r["status"] in ("discovering", "sizing", "paused"):
            protected.add(r["id"])
    deleted = []
    for sid in sorted(to_delete - protected):
        if sid in running:
            continue
        if delete_scan(data_dir, sid).get("ok"):
            deleted.append(sid)
    return {"ok": True, "deleted": deleted, "count": len(deleted)}


def estimate_eta_seconds(phase, processed, total_dirs, discovered, elapsed, prev_total_dirs=None, already=0,
                         prev_total_files=None, files_done=None):
    """남은 시간(초) 추정 — 집계(sizing) 단계에서만: 지금까지의 처리 속도로 남은 디렉터리를 나눈다.

    대시보드 '요약'·홈 카드·포탈이 모두 이 함수 하나를 쓴다(단일 소스). 폴더 찾기(discovering) 단계는
    전체 디렉터리 수를 아직 모르므로 None. 경과(elapsed)는 스캔 시작부터라 탐색 시간이 포함돼,
    추정은 보수적(실제보다 길게)으로 나온다."""
    try:
        processed = int(processed or 0)
        elapsed = float(elapsed or 0)
        total = int(total_dirs or 0) or int(discovered or 0)
    except (TypeError, ValueError):
        return None
    if phase == "discovering":
        # 1단계(폴더 찾기)는 전체 폴더 수를 아직 모른다. 같은 경로를 전에 끝까지 스캔했으면 그때의 전체 폴더 수를
        # 기준으로 추정한다(실제 스캔은 시간 대부분이 이 단계라, 이게 없으면 남은 시간을 거의 볼 수 없었다).
        # 지금까지 훑은 폴더가 이전 전체보다 많으면(트리가 커짐) 추정하지 않는다. 2단계(집계)는 짧아 더하지 않는다.
        try:
            prev = int(prev_total_dirs or 0)
            done_dirs = int(discovered or 0)
        except (TypeError, ValueError):
            return None
        if elapsed <= 0:
            return None
        ests = []
        if 0 < done_dirs < prev and done_dirs >= max(20, prev * 0.005):   # 시작 직후 몇 개로 낸 추정은 크게 튄다
            ests.append((prev - done_dirs) / (done_dirs / elapsed))
        # 파일 기준도 함께 본다 — '찾은 폴더'는 실제로 훑기 전에 빨리 늘어 끝 무렵 정체되는데, 그동안 남은 시간이
        # 수십 초에 멈춰 줄지 않았다(17회차 점검: 파일 14k→24k/30k 동안 계속 25~50초). 둘 중 긴 쪽(보수적)을 쓴다.
        try:
            pf = int(prev_total_files or 0)
            fd = int(files_done or 0)
        except (TypeError, ValueError):
            pf = fd = 0
        if 0 < fd < pf and fd >= max(100, pf * 0.005):
            ests.append((pf - fd) / (fd / elapsed))
        return max(ests) if ests else None
    if phase != "sizing" or processed <= 0 or elapsed <= 0:
        return None
    # processed·elapsed 는 집계 단계 구간(sizing_window)일 수 있다 — 남은 수는 단계 시작 전에 이미 처리한 것(already)까지 뺀다
    rate = processed / elapsed
    return max(total - processed - int(already or 0), 0) / rate if rate > 0 else None


def sizing_window(s: dict, active: float):
    """집계 단계면 (그 단계에서 처리한 수, 그 단계의 활성 경과)를, 아니면 (처리 수, 전체 활성 경과)를 돌려준다.

    1단계가 몇 시간 걸린 스캔에서 2단계 속도를 '스캔 시작부터'로 나누면 짧은 집계 단계에 '약 50시간' 같은
    값이 나왔다 — 집계에 들어설 때의 활성 시간·처리 수를 빼고 계산한다(기록이 없으면 예전 방식)."""
    processed = int(s.get("processed_dirs") or 0)
    if s.get("phase") == "sizing" and s.get("sizing_from_active") is not None:
        try:
            return (max(0, processed - int(s.get("sizing_from_processed") or 0)),
                    max(0.0, float(active) - float(s.get("sizing_from_active") or 0)))
        except (TypeError, ValueError):
            pass
    return processed, active


def prev_totals(conn, root_path, exclude_id=None):
    """같은 루트의 마지막 완료 스캔의 (전체 폴더 수, 전체 파일 수) — 1단계 남은 시간 추정의 기준(없으면 (None, None))."""
    try:
        r = conn.execute("SELECT total_dirs, total_files FROM scans WHERE root_path=? AND status='done' AND id<>? "
                         "AND total_dirs>0 ORDER BY id DESC LIMIT 1", (root_path, int(exclude_id or -1))).fetchone()
    except Exception:  # noqa: BLE001
        return None, None
    return (int(r[0]), int(r[1] or 0) or None) if r else (None, None)


def prev_total_dirs(conn, root_path, exclude_id=None):
    """같은 루트의 마지막 완료 스캔의 전체 폴더 수(없으면 None)."""
    return prev_totals(conn, root_path, exclude_id)[0]


def fs_capacity(roots) -> tuple:
    """루트들의 파일시스템 (전체, 사용) — 같은 FS 는 한 번만, 그 FS 를 가장 최근에 본 루트의 값으로.

    같은 FS 판별: ① 마운트 출처(fs_source)가 같으면 ② 출처를 모르거나 다르면 전체 용량이 거의 같고(0.1% 또는
    4MiB 이내) 사용량도 비슷할 때(전체의 2% 이내). 엣지 홈·포탈(노드·전체)이 이 함수 하나를 쓴다(단일 소스) — 예전엔
    포탈은 최댓값, 엣지는 최근값이라 같은 노드가 5.6% / 5.4% 로 달랐다(24회차 점검)."""
    clusters = []   # [{"total", "used", "ts", "srcs"}]
    for rt in roots or []:
        if not isinstance(rt, dict):
            continue
        try:
            ftot = int(rt.get("fs_total_bytes") or 0)
            fused = int(rt.get("fs_used_bytes") or 0)
            ts = float(rt.get("updated_at") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if ftot <= 0:
            continue
        src = str(rt.get("fs_source") or "").strip()
        hit = None
        if src:
            hit = next((c for c in clusters if src in c["srcs"]), None)
        if hit is None:
            tol_t = max(4 * 1024 * 1024, ftot * 0.001)
            hit = next((c for c in clusters
                        if abs(c["total"] - ftot) <= tol_t and abs(c["used"] - fused) <= ftot * 0.02), None)
        if hit is None:
            clusters.append({"total": ftot, "used": fused, "ts": ts, "srcs": {src} if src else set()})
        else:
            if ts >= hit["ts"]:
                hit.update(total=ftot, used=fused, ts=ts)
            if src:
                hit["srcs"].add(src)
    return sum(c["total"] for c in clusters), sum(c["used"] for c in clusters)


def active_elapsed(s: dict, now_ts: float) -> float:
    """스캔의 활성 경과 시간(초). 스캐너가 기록한 active_elapsed(일시정지 갭 제외)가 있으면 그것 + 마지막 기록 뒤
    흐른 시간(진행 중일 때만), 없으면(구버전 기록) 시작부터 지금까지."""
    try:
        act = float(s.get("active_elapsed") or 0)
    except (TypeError, ValueError):
        act = 0.0
    if act > 0:
        up = float(s.get("updated_at") or now_ts)
        return act + (max(0.0, now_ts - up) if s.get("status") in ("discovering", "sizing") else 0.0)
    return max(0.0, now_ts - float(s.get("started_at") or now_ts))


def _nested_roots(paths, containers=None) -> Dict[str, str]:
    """다른 루트 안에 든 루트 → 그것을 품은 가장 바깥 루트. (/ifs/data 와 /ifs/data/proj 를 둘 다 스캔하면
    proj 용량이 합계에 두 번 들어갔다 — 2026-11 리뷰.) 경로 구분자 경계로만 판단한다(/ifs/data2 는 별개).

    containers 가 주어지면 그 루트만 '품는 쪽'이 될 수 있다 — 바깥 루트가 완료 스캔이 없거나 깊이 제한 스캔이면 안쪽 루트를
    실제로 세지 않았는데도 합계에서 빼 용량이 사라졌다(33회차 점검: 4.9 MB 가 108 KB 로)."""
    norm = {p: (str(p).rstrip("/") or "/") for p in paths}
    out: Dict[str, str] = {}
    for p, n in norm.items():
        best = None
        for q, m in norm.items():
            if q == p or m == n:
                continue
            if containers is not None and q not in containers:
                continue
            pref = "/" if m == "/" else m + "/"
            if n.startswith(pref) and (best is None or len(norm[best]) > len(m)):
                best = q
        if best is not None:
            out[p] = best
    return out


def _row_total(db_path, path, files: bool = False):
    """per-run DB 의 최신 실행에서 path 행의 재귀 합계(없거나 읽기 실패면 0). files=True 면 (바이트, 파일 수)."""
    z = (0, 0) if files else 0
    if not db_path or not os.path.exists(db_path):
        return z
    try:
        c = dbmod.connect(db_path)
        try:
            rid = dbmod.latest_run_id(c)
            if rid is None:
                return z
            r = c.execute("SELECT total_bytes, total_files FROM directories WHERE run_id=? AND path=?",
                          (rid, str(path).rstrip("/") or "/")).fetchone()
            if not r:
                return z
            return (int(r[0] or 0), int(r[1] or 0)) if files else int(r[0] or 0)
        finally:
            c.close()
    except Exception:  # noqa: BLE001 — 겹침 보정 실패는 보정 없이(예전 동작)
        return z


def capacity_caveats(roots) -> str:
    """합계가 덜 센 값인 이유를 한 줄로(화면·CLI·질의응답 공통 문구) — 깊이 제한·읽기 오류·완료 스캔 없음.
    예전엔 대시보드 표 한 줄에만 '깊이 N까지만'이 있고 다른 곳은 아무 표시 없이 적게 센 값을 보였다(29회차 점검)."""
    rs = [r for r in roots if not r.get("nested_in")]
    out = []
    dl = [r for r in rs if int(r.get("capacity_depth_limit") or 0) > 0]
    er = sum(int(r.get("capacity_error_dirs") or 0) for r in rs)
    pa = [r for r in rs if r.get("capacity_partial")]
    if dl:
        out.append("깊이 제한 스캔 %d개 루트는 그 아래 용량 미포함" % len(dl))
    if er:
        out.append("읽기 오류 %s건(그 항목의 용량 미포함)" % "{:,}".format(er))   # 폴더·파일 읽기 오류 모두(30회차 점검: '폴더'라 했지만 파일 오류도 셈)
    if pa:
        out.append("완료 스캔이 없는 루트 %d개는 부분 값" % len(pa))
    return (" ※ " + " · ".join(out) + ".") if out else ""


def overall_capacity(conn) -> dict:
    """전체 용량 관리 집계.

    같은 루트를 여러 번 스캔했을 수 있으므로 루트별로 한 번만 센다. 루트마다 두 가지를 준다:
      - 활동(status·phase·진행 카운터·scanned_bytes·시각) = 그 루트의 '가장 최근' 스캔 — 진행 중 표시·처리량용
      - 용량(capacity_*) = 그 루트의 '마지막 완료(done)' 스캔 — 완료 스캔이 없으면 최신 스캔 값(capacity_partial)
    예전엔 최신 스캔을 상태와 무관하게 용량으로 써서, 멈춤·오류·진행 중인 부분 결과(예: 7바이트)가 그 루트의
    용량으로 표시·합산됐다. total_capacity_bytes = 용량 합(다른 루트 안에 든 루트 제외 · 화면·포탈의 '조사 용량'), total_scanned_bytes = 최신
    스캔 합(진행량 — 처리량 시계열이 쓰므로 의미를 바꾸지 않는다).
    """
    scans = conn.execute("SELECT * FROM scans ORDER BY id DESC").fetchall()
    latest_by_root: Dict[str, dict] = {}
    done_by_root: Dict[str, dict] = {}
    for s in scans:
        rp = s["root_path"]
        if rp not in latest_by_root:  # id 내림차순이므로 처음 본 게 최신
            latest_by_root[rp] = dict(s)
        if rp not in done_by_root and s["status"] == "done":
            done_by_root[rp] = dict(s)

    roots = []
    total_scanned = 0
    total_capacity = 0
    now_ts = time.time()
    _full = {rp for rp, d in done_by_root.items() if not int(d.get("opt_max_depth") or 0)}
    nested = _nested_roots(latest_by_root.keys(), containers=_full)
    for rp, s in latest_by_root.items():
        cap = done_by_root.get(rp) or s
        cap_bytes = int(cap.get("scanned_bytes") or 0)
        _prev_ok = cap is not s and cap.get("status") == "done"
        prev_total = (int(cap.get("total_dirs") or 0) or None) if _prev_ok else None
        prev_files = (int(cap.get("total_files") or 0) or None) if _prev_ok else None
        _p, _e = sizing_window(s, active_elapsed(s, now_ts))
        eta = (estimate_eta_seconds(s.get("phase"), _p, s.get("total_dirs"), s.get("discovered_dirs"), _e, prev_total,
                                    already=int(s.get("sizing_from_processed") or 0),
                                    prev_total_files=prev_files, files_done=s.get("total_files"))
               if s["status"] in ("discovering", "sizing") else None)
        eta_basis = (("prev_scan" if s.get("phase") == "discovering" else "rate") if eta is not None else None)
        eta_prev_available = bool(prev_total or prev_files)   # 지난 완료 스캔 있음 — 화면이 '기록 없음' 대신 '계산 중'(22회차 점검)
        total_scanned += s.get("scanned_bytes") or 0
        if rp not in nested:      # 다른 루트 안에 든 루트는 바깥 루트 용량에 이미 포함 — 두 번 세지 않는다
            total_capacity += cap_bytes
        roots.append({
            "scan_id": s["id"],
            "root_path": rp,
            "hostname": s.get("hostname"),
            "status": s["status"],
            "phase": s["phase"],
            "scanned_bytes": s.get("scanned_bytes") or 0,
            "total_dirs": s.get("total_dirs") or 0,
            "discovered_dirs": s.get("discovered_dirs") or 0,
            "processed_dirs": s.get("processed_dirs") or 0,
            "total_files": s.get("total_files") or 0,
            "fs_total_bytes": s.get("fs_total_bytes") or 0,
            "fs_used_bytes": s.get("fs_used_bytes") or 0,
            "fs_free_bytes": s.get("fs_free_bytes") or 0,
            "fs_source": s.get("fs_source") or cap.get("fs_source"),
            "started_at": s.get("started_at"),
            "finished_at": s.get("finished_at"),
            "updated_at": s.get("updated_at"),
            "partial_agg_at": s.get("partial_agg_at"),   # 멈춘 자료 부분 집계 시각(이력 표 배지)
            "active_elapsed": s.get("active_elapsed"),   # 활성 시간(일시정지 제외) — 화면의 소요 시간
            "capacity_bytes": cap_bytes,
            "capacity_files": int(cap.get("total_files") or 0),
            "capacity_scan_id": cap["id"],
            "capacity_finished_at": cap.get("finished_at") if cap.get("status") == "done" else None,
            "capacity_partial": rp not in done_by_root,     # 완료 스캔이 아직 없어 부분 값
            # 최대 깊이로 자른 스캔이면 그 깊이(그 아래 용량은 합계에 없음) — 화면이 '깊이 N까지'로 표시(24회차 점검)
            "capacity_depth_limit": int(cap.get("opt_max_depth") or 0),
            # 그 스캔에서 접근하지 못한 폴더 수 — 그 안의 용량은 합계에 없다. 예전엔 '완료'로만 보여 조용히 적게 셌다(29회차 점검)
            "capacity_error_dirs": int(cap.get("error_dirs") or 0),
            # 진행 중이면 남은 시간(초)과 끝날 시각(epoch) 추정 — 상태 화면 '요약'과 같은 계산
            "eta_seconds": eta,
            "eta_at": (now_ts + eta) if eta is not None else None,
            "eta_basis": eta_basis,     # prev_scan = 폴더 찾기 중, 이전 완료 스캔의 전체 폴더 수 기준 추정
            "eta_prev_available": eta_prev_available,
            "nested_in": nested.get(rp),   # 이 루트를 품은 다른 스캔 루트(있으면 합계에서 빠짐)
        })
    # 바깥 루트가 깊이 제한·미완료라 안쪽 루트를 따로 더할 때, 바깥 스캔이 그 경로에서 이미 센 몫(윗부분)은 빼야 두 번 세지
    # 않는다(34회차 점검: 깊이 2 바깥 + 안쪽 전체 → 실제 53 KB 가 74 KB). 바깥 스캔 DB 의 그 경로 행 합계만큼 뺀다.
    # 따로 세는 루트마다 '가장 가까운' 따로 세는 바깥 루트의 몫을 뺀다 — 가장 바깥 루트만 보면 3단 중첩(깊이 1 ⊃ 깊이 2 ⊃ 전체)에서
    # 가운데 스캔이 센 몫이 남아 두 번 셌다(35회차 점검). 파일 수도 같은 규칙(바이트만 빼 파일 수가 어긋났다).
    _counted = [r for r in latest_by_root if r not in nested]
    _norm = {r: (str(r).rstrip("/") or "/") for r in _counted}
    overlap_files = 0
    for rp in _counted:
        n_rp = _norm[rp]
        anc = [q for q in _counted if q != rp and _norm[q] != n_rp
               and n_rp.startswith("/" if _norm[q] == "/" else _norm[q] + "/")]
        if not anc:
            continue
        q = max(anc, key=lambda x: len(_norm[x]))
        ocap = done_by_root.get(q) or latest_by_root.get(q) or {}
        ov_b, ov_f = _row_total(ocap.get("db_path"), rp, files=True)
        if ov_b > 0:
            total_capacity -= min(ov_b, int(ocap.get("scanned_bytes") or 0))
        if ov_f > 0:
            overlap_files += ov_f
    roots.sort(key=lambda x: x["capacity_bytes"], reverse=True)

    active = sum(1 for s in scans if s["status"] in ("discovering", "sizing"))
    fs_t, fs_u = fs_capacity(roots)
    return {
        "total_scanned_bytes": total_scanned,
        "total_capacity_bytes": total_capacity,
        # 파일 수 합(다른 루트 안에 든 루트 제외 — 용량 합과 같은 규칙, 24회차 점검: 홈 파일 수가 중첩 루트를 두 번 셈)
        "total_capacity_files": max(0, sum((int(r.get("capacity_files") or 0) if not r.get("capacity_partial")
                                            else int(r.get("total_files") or 0)) for r in roots if not r.get("nested_in"))
                                    - overlap_files),
        # 파일시스템 사용량(같은 FS 한 번, 가장 최근 값) — 엣지 홈·포탈이 같은 값(24회차 점검: 5.4% vs 5.6%)
        "fs_total_bytes": fs_t,
        "fs_used_bytes": fs_u,
        "capacity_caveat": capacity_caveats(roots).strip(),   # 합계가 덜 센 이유(화면·CLI·질의응답 같은 문구)
        "root_count": len(latest_by_root),
        "scan_count": len(scans),
        "active_scans": active,
        "roots": roots,
    }

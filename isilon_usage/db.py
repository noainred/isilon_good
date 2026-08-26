"""SQLite 저장 계층.

대용량(수천만 파일) 환경에서 메모리를 최소로 쓰기 위해, 스캔 결과를
메모리에 쌓지 않고 SQLite에 흘려보낸다. WAL 모드를 사용해 스캐너가 쓰는
동안에도 대시보드(다른 프로세스/스레드)가 동시에 읽을 수 있게 한다.

테이블 구성:
  - scan_runs        : 스캔 1회에 대한 메타데이터 + 실시간 진행 상태
  - directories      : 디렉터리별 집계(파일 수, 자기 용량, 재귀 용량 등)
  - resource_samples : 서버/프로세스 자원 사용 샘플(메모리/CPU 등 시계열)
"""

from typing import Optional

import os
import socket
import sqlite3
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path       TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'discovering',
    phase           TEXT    NOT NULL DEFAULT 'discovering',
    backend         TEXT    NOT NULL DEFAULT 'native',
    size_mode       TEXT    NOT NULL DEFAULT 'disk',
    started_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    finished_at     REAL,
    session_started_at REAL,
    elapsed_accum   REAL    NOT NULL DEFAULT 0,
    discovered_dirs INTEGER NOT NULL DEFAULT 0,
    total_dirs      INTEGER NOT NULL DEFAULT 0,
    processed_dirs  INTEGER NOT NULL DEFAULT 0,
    scanned_bytes   INTEGER NOT NULL DEFAULT 0,
    total_files     INTEGER NOT NULL DEFAULT 0,
    error_dirs      INTEGER NOT NULL DEFAULT 0,
    current_dir     TEXT,
    worker_dirs     TEXT,
    current_depth   INTEGER NOT NULL DEFAULT 0,
    max_depth       INTEGER NOT NULL DEFAULT 0,
    workers         INTEGER NOT NULL DEFAULT 0,
    active_workers  INTEGER NOT NULL DEFAULT 0,
    mount_readonly  INTEGER NOT NULL DEFAULT 0,
    fs_total_bytes  INTEGER NOT NULL DEFAULT 0,
    fs_used_bytes   INTEGER NOT NULL DEFAULT 0,
    fs_free_bytes   INTEGER NOT NULL DEFAULT 0,
    scanner_pid     INTEGER,
    hostname        TEXT,
    app_version     TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS directories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    parent_id    INTEGER,
    path         TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    depth        INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    file_count   INTEGER NOT NULL DEFAULT 0,
    subdir_count INTEGER NOT NULL DEFAULT 0,
    own_bytes    INTEGER NOT NULL DEFAULT 0,
    total_bytes  INTEGER NOT NULL DEFAULT 0,
    total_files  INTEGER NOT NULL DEFAULT 0,
    scanned_at   REAL,
    error        TEXT,
    UNIQUE(run_id, path)
);

CREATE INDEX IF NOT EXISTS idx_dir_run_depth   ON directories(run_id, depth);
CREATE INDEX IF NOT EXISTS idx_dir_run_parent  ON directories(run_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_dir_run_status  ON directories(run_id, status);
CREATE INDEX IF NOT EXISTS idx_dir_run_total   ON directories(run_id, total_bytes);

CREATE TABLE IF NOT EXISTS resource_samples (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    ts           REAL    NOT NULL,
    mem_total    INTEGER NOT NULL DEFAULT 0,
    mem_used     INTEGER NOT NULL DEFAULT 0,
    mem_percent  REAL    NOT NULL DEFAULT 0,
    swap_used    INTEGER NOT NULL DEFAULT 0,
    cpu_percent  REAL    NOT NULL DEFAULT 0,
    load1        REAL    NOT NULL DEFAULT 0,
    scanner_rss  INTEGER NOT NULL DEFAULT 0,
    du_rss       INTEGER NOT NULL DEFAULT 0,
    du_pid       INTEGER,
    scanner_cpu  REAL    NOT NULL DEFAULT 0,
    du_cpu       REAL    NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sample_run_ts ON resource_samples(run_id, ts);

-- 집계 리포트: 파일 나이(age)/소유자(owner)/확장자(ext)/접근나이(atime_age)별 용량·개수
CREATE TABLE IF NOT EXISTS scan_stats (
    run_id  INTEGER NOT NULL,
    kind    TEXT    NOT NULL,   -- 'age' | 'owner' | 'ext' | 'atime_age'
    key     TEXT    NOT NULL,
    bytes   INTEGER NOT NULL DEFAULT 0,
    files   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, kind, key)
);

-- 최대 파일 Top-N (스캔 중 힙으로 유지한 상위 파일)
CREATE TABLE IF NOT EXISTS top_files (
    run_id  INTEGER NOT NULL,
    path    TEXT    NOT NULL,
    bytes   INTEGER NOT NULL DEFAULT 0,
    mtime   REAL,
    atime   REAL,
    uid     INTEGER,
    PRIMARY KEY (run_id, path)
);

-- 단계별 진단 로그(집계 레벨별 속도, WAL 체크포인트 차단 등).
-- 개선 포인트를 실측으로 찾기 위한 기록 — journalctl(stderr)에도 같은 줄이 나간다.
CREATE TABLE IF NOT EXISTS scan_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL,
    ts      REAL    NOT NULL,
    phase   TEXT,
    message TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_log_run_ts ON scan_log(run_id, ts);
"""


def connect(db_path: str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """스레드별/용도별로 호출하는 SQLite 연결 팩토리.

    sqlite3 연결은 기본적으로 스레드 안전하지 않으므로, 스캐너/모니터/HTTP
    핸들러가 각자 자신의 연결을 만들어 쓴다. WAL + busy_timeout 으로 동시
    읽기/쓰기를 허용한다.
    """
    conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_column(conn, table: str, column: str, decl: str) -> None:
    """테이블에 컬럼이 없으면 추가한다(구버전 DB 업그레이드용 간단 마이그레이션)."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(db_path: str) -> None:
    """스키마를 생성(존재하면 무시)하고 스키마 버전을 기록한다."""
    from . import SCHEMA_VERSION

    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # 구버전 DB 호환: 누락 컬럼 보강
        ensure_column(conn, "scan_runs", "hostname", "TEXT")
        ensure_column(conn, "scan_runs", "app_version", "TEXT")
        ensure_column(conn, "scan_runs", "workers", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "scan_runs", "active_workers", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "scan_runs", "mount_readonly", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "scan_runs", "worker_dirs", "TEXT")
        ensure_column(conn, "scan_runs", "session_started_at", "REAL")
        ensure_column(conn, "scan_runs", "elapsed_accum", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "resource_samples", "scanner_cpu", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "resource_samples", "du_cpu", "REAL NOT NULL DEFAULT 0")
        ensure_column(conn, "top_files", "atime", "REAL")
        # 집계(sizing) 행/초 — 스캐너 자체 측정(진단 API 의 짧은 표본이 0 으로 보이는 문제 보완)
        ensure_column(conn, "scan_runs", "agg_rate", "REAL NOT NULL DEFAULT 0")
        conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
        conn.commit()
    finally:
        conn.close()


def create_run(
    conn: sqlite3.Connection,
    root_path: str,
    *,
    backend: str,
    size_mode: str,
    scanner_pid: int,
) -> int:
    """새 스캔 실행 레코드를 만들고 run_id 를 돌려준다."""
    from . import __version__

    now = time.time()
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = None
    cur = conn.execute(
        """
        INSERT INTO scan_runs
            (root_path, status, phase, backend, size_mode,
             started_at, updated_at, scanner_pid, hostname, app_version)
        VALUES (?, 'discovering', 'discovering', ?, ?, ?, ?, ?, ?, ?)
        """,
        (root_path, backend, size_mode, now, now, scanner_pid, hostname, __version__),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_run_id(conn: sqlite3.Connection) -> Optional[int]:
    """가장 최근 스캔 실행의 id (대시보드 기본 표시용)."""
    row = conn.execute("SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM scan_runs WHERE id=?", (run_id,)).fetchone()


def update_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    """scan_runs 의 임의 컬럼들을 갱신한다(updated_at 자동 포함)."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [run_id]
    conn.execute(f"UPDATE scan_runs SET {cols} WHERE id=?", params)


def prune_samples(conn: sqlite3.Connection, run_id: int, keep: int = 2000) -> None:
    """오래된 자원 샘플을 정리해 DB가 무한정 커지지 않게 한다."""
    conn.execute(
        """
        DELETE FROM resource_samples
        WHERE run_id=? AND id NOT IN (
            SELECT id FROM resource_samples WHERE run_id=? ORDER BY id DESC LIMIT ?
        )
        """,
        (run_id, run_id, keep),
    )


def add_scan_log(conn, run_id, phase, message) -> None:
    """진단 로그 한 줄 저장(커밋은 호출자 몫 — 스캔의 주기 커밋에 얹혀 간다)."""
    conn.execute(
        "INSERT INTO scan_log(run_id, ts, phase, message) VALUES (?,?,?,?)",
        (run_id, time.time(), phase, message))


def get_scan_log(conn, run_id, limit: int = 300):
    """진단 로그 최근 limit 줄(시간순으로 반환)."""
    rows = conn.execute(
        "SELECT ts, phase, message FROM scan_log WHERE run_id=? ORDER BY id DESC LIMIT ?",
        (run_id, int(limit))).fetchall()
    return list(reversed(rows))


def prune_scan_log(conn, run_id, keep: int = 2000) -> None:
    """오래된 진단 로그 정리(DB 무한 성장 방지)."""
    conn.execute(
        "DELETE FROM scan_log WHERE run_id=? AND id NOT IN ("
        "SELECT id FROM scan_log WHERE run_id=? ORDER BY id DESC LIMIT ?)",
        (run_id, run_id, int(keep)))


def replace_scan_stats(conn, run_id, kind, items) -> None:
    """집계 통계(kind)의 이 run 행을 통째로 교체한다. items: [(key, bytes, files), ...]"""
    conn.execute("DELETE FROM scan_stats WHERE run_id=? AND kind=?", (run_id, kind))
    if items:
        conn.executemany(
            "INSERT INTO scan_stats(run_id, kind, key, bytes, files) VALUES (?,?,?,?,?)",
            [(run_id, kind, str(k), int(b), int(f)) for (k, b, f) in items])


def get_scan_stats(conn, run_id, kind, limit: int = 0):
    """집계 통계를 용량 내림차순으로 반환."""
    q = ("SELECT key, bytes, files FROM scan_stats WHERE run_id=? AND kind=? "
         "ORDER BY bytes DESC")
    if limit:
        q += " LIMIT %d" % int(limit)
    return [dict(r) for r in conn.execute(q, (run_id, kind))]


def replace_top_files(conn, run_id, items) -> None:
    """최대 파일 Top-N 행을 통째로 교체. items: [(path, bytes, mtime, uid, atime), ...]"""
    conn.execute("DELETE FROM top_files WHERE run_id=?", (run_id,))
    if items:
        conn.executemany(
            "INSERT OR REPLACE INTO top_files(run_id, path, bytes, mtime, uid, atime) "
            "VALUES (?,?,?,?,?,?)",
            [(run_id, str(p), int(b), float(m or 0), int(u or 0), float(a or 0))
             for (p, b, m, u, a) in items])


def get_top_files(conn, run_id, limit: int = 0):
    """최대 파일 목록(용량 내림차순)."""
    q = ("SELECT path, bytes, mtime, atime, uid FROM top_files "
         "WHERE run_id=? ORDER BY bytes DESC")
    if limit:
        q += " LIMIT %d" % int(limit)
    return [dict(r) for r in conn.execute(q, (run_id,))]


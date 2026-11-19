"""진행 중 스캔의 '지금까지 결과' 스냅샷 — 스캔을 멈추지 않고 폴더별 용량(하한값)을 보기 위한 복사본.

왜: 며칠 걸리는 스캔은 1단계(폴더 찾기) 동안 폴더별 재귀 용량이 아직 계산되지 않아(2단계에서 한 번에 계산)
분석 화면이 0 으로 보인다. 예전에는 스캔을 멈추고 '멈춘 자료 집계'를 해야 볼 수 있었다(2026-11 사용자 요청).

어떻게:
  1) per-run DB 를 **한 번의 읽기 트랜잭션**으로 새 파일에 복사한다. DB 가 WAL 이라 스캐너는 그동안에도 계속
     쓰고, 복사본은 한 시점의 일관된 모습이다(파일을 그냥 복사하면 쓰는 도중의 반쪽 상태가 될 수 있다).
     Python 3.6 호환을 위해 backup API·VACUUM INTO 를 쓰지 않고 ATTACH + INSERT…SELECT 로 복사한다.
  2) 복사본에 기존 '멈춘 자료 집계'(scanner.partial_aggregate)를 그대로 돌린다 — 같은 계산(단일 소스),
     아직 못 본 폴더는 0 이라 결과는 **하한값**이다.
  3) 다 만들어지면 임시 파일을 제자리로 원자적으로 바꾼다(보는 중인 화면이 반쪽 파일을 읽지 않게).
원본 DB 는 읽기만 한다(스캔 결과·재개에 영향 없음).
"""

from __future__ import annotations

import os
import time
from typing import Optional

from . import db as dbmod


def _tables(conn, schema: str) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM %s.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%%'" % schema)]


def _cols(conn, schema: str, table: str) -> list:
    return [r[1] for r in conn.execute('PRAGMA %s.table_info("%s")' % (schema, table.replace('"', '""')))]


def copy_consistent(src_db: str, dst_db: str) -> int:
    """src_db 를 한 시점의 일관된 모습으로 dst_db 에 복사한다(스키마는 현재 코드 기준). 복사한 디렉터리 행 수 반환."""
    if os.path.exists(dst_db):
        os.remove(dst_db)
    dbmod.init_db(dst_db)
    conn = dbmod.connect(dst_db)
    try:
        conn.isolation_level = None                     # BEGIN/COMMIT 를 직접(ATTACH 는 트랜잭션 밖에서)
        conn.execute("ATTACH DATABASE ? AS s", (src_db,))
        try:
            conn.execute("BEGIN")                       # 이 안의 모든 읽기 = 원본의 같은 시점
            n_dirs = 0
            dst_tables = set(_tables(conn, "main"))
            for t in _tables(conn, "s"):
                if t not in dst_tables:
                    continue
                dcols = _cols(conn, "main", t)
                scols = set(_cols(conn, "s", t))
                cols = [c for c in dcols if c in scols]
                if not cols:
                    continue
                q = ", ".join('"%s"' % c.replace('"', '""') for c in cols)
                tq = '"%s"' % t.replace('"', '""')
                conn.execute("DELETE FROM main.%s" % tq)  # init_db 가 넣은 기본 행(스키마 버전 등)은 원본 것으로
                conn.execute("INSERT INTO main.%s (%s) SELECT %s FROM s.%s" % (tq, q, q, tq))
                if t == "directories":
                    n_dirs = conn.execute("SELECT COUNT(*) FROM main.directories").fetchone()[0]
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            conn.execute("DETACH DATABASE s")
        return int(n_dirs)
    finally:
        conn.close()


def build(src_db: str, dst_db: str, *, stop_event=None, on_progress=None, still_running=None) -> dict:
    """스냅샷을 만든다: 임시 파일에 일관 복사 → 부분 집계 → 제자리로 교체. 반환 {ok, rows, at, elapsed} 또는 {ok:False, reason}."""
    from .scanner import partial_aggregate
    t0 = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(dst_db)), exist_ok=True)
    tmp = dst_db + ".building"
    try:
        rows = copy_consistent(src_db, tmp)
        if rows <= 0:
            return {"ok": False, "reason": "아직 찾은 폴더가 없습니다 — 잠시 뒤 다시 시도하세요."}
        c = dbmod.connect(tmp)                         # 복사본은 다시 이어 돌리지 않는다 — '멈춘' 것으로 표시해 집계 허용
        try:
            c.execute("UPDATE scan_runs SET status='paused' WHERE status IN ('discovering','sizing')")
            c.commit()
        finally:
            c.close()
        res = partial_aggregate(tmp, stop_event=stop_event, on_progress=on_progress)
        if not res.get("ok"):
            return {"ok": False, "reason": res.get("reason") or "부분 집계 실패"}
        c = dbmod.connect(tmp)                         # 교체 전에 임시 파일의 WAL 을 본문에 합친다
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            c.close()
        # 만드는 사이 스캔이 끝나거나 멈췄으면 놓지 않는다 — 놓으면 재개 뒤에 옛 스냅샷이 '지금 결과'로 보였다(16회차 점검)
        if still_running is not None and not still_running():
            return {"ok": False, "reason": "만드는 사이 스캔이 끝나거나 멈춰 버렸습니다(완료값·멈춘 자료 집계를 보세요)."}
        os.replace(tmp, dst_db)
        if still_running is not None and not still_running():   # 교체 직후에 끝난 경우도 정리
            remove(dst_db)
            return {"ok": False, "reason": "만드는 사이 스캔이 끝났습니다."}
        for sfx in ("-wal", "-shm"):                   # 옛 스냅샷의 WAL/SHM 이 새 본문과 섞이지 않게
            try:
                os.remove(dst_db + sfx)
            except OSError:
                pass
        return {"ok": True, "rows": rows, "at": time.time(), "elapsed": time.time() - t0}
    finally:
        for sfx in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + sfx)
            except OSError:
                pass


def remove(dst_db: Optional[str]) -> None:
    if not dst_db:
        return
    for sfx in ("", "-wal", "-shm", ".building", ".building-wal", ".building-shm"):
        try:
            os.remove(dst_db + sfx)
        except OSError:
            pass

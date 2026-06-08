"""디렉터리 사용량 스캐너 (메모리 최소화 설계).

문제: 아이실론처럼 한 디렉터리에 수천만 개의 파일이 있는 초대용량 NAS 에서
`du` 를 트리 전체에 한 번에 돌리면 결과/상태를 메모리에 쌓다가 OOM 으로
죽을 수 있다.

해결: 트리를 한 번에 다루지 않고 디렉터리 단위로 쪼개서 처리한다.

  1단계  디렉터리 탐색(discover)
      - 루트부터 내려가며 각 디렉터리를 os.scandir 로 "한 번만" 훑는다.
      - 그 디렉터리에 "직접" 들어있는 파일들의 용량 합(own_bytes)과 파일 수,
        하위 디렉터리 목록을 구해 즉시 DB 에 기록한다.
      - 다음에 방문할 디렉터리 목록(프론티어)도 메모리가 아니라 DB 에 둔다
        (status='pending'). 따라서 메모리에는 "지금 보는 디렉터리 하나의
        엔트리"만 올라온다.

  2단계  상향식 집계(aggregate)
      - 가장 깊은 레벨(maxdepth)부터 0 까지 레벨을 거슬러 올라가며,
        같은 레벨의 디렉터리를 모두 처리한 뒤 그 위 레벨로 이동한다
        (사용자가 요청한 "최하위 → 같은 레벨 → 상위" 순서 그대로).
      - 디렉터리의 재귀 용량 total_bytes = own_bytes + Σ(자식 total_bytes).
        자식은 이미 더 깊은 레벨에서 계산됐으므로 재귀 du 가 필요 없고,
        파일을 두 번 훑지 않는다(전체 디스크를 정확히 1회만 읽음).

백엔드:
  - native (기본): 위 방식. 파일을 정확히 1회만 읽어 가장 빠르고 메모리도
    가장 적다. 대시보드의 "측정 프로세스 메모리"는 스캐너 프로세스 RSS.
  - du           : 사용자의 본래 아이디어처럼 디렉터리마다 시스템 `du -s` 를
    별도 프로세스로 실행해 그 결과를 저장하고, 그 du 자식 프로세스의 메모리를
    대시보드에 보여준다. 정확하지만 상위 디렉터리에서 하위를 다시 훑으므로
    더 느리다(선택 사항).
"""

from typing import List, Optional, Tuple

import json
import os
import shutil
import subprocess
import threading
import time

from . import db as dbmod
from .monitor import ResourceMonitor


# 디스크 블록 크기(바이트). du 는 512B 블록 수(st_blocks)로 실제 점유 용량을
# 계산한다. apparent-size 모드에서는 st_size(논리 크기)를 쓴다.
BLOCK_UNIT = 512

# 한 디렉터리의 파일 stat 을 묶어서(동시에) 처리하는 청크 크기. 한 디렉터리에
# 수천만 파일이 있어도 메모리가 이 청크만큼만 쓰이도록 스트리밍 처리한다.
STAT_CHUNK = 2000


def _entry_bytes(stat_result, size_mode: str) -> int:
    """size_mode 에 따라 한 파일이 차지하는 바이트를 계산."""
    if size_mode == "apparent":
        return int(stat_result.st_size)
    # 'disk' (기본): du 와 동일하게 실제 점유 블록 기준
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:  # 일부 플랫폼은 st_blocks 미지원
        return int(stat_result.st_size)
    return int(blocks) * BLOCK_UNIT


class Scanner:
    def __init__(
        self,
        db_path: str,
        root_path: str,
        *,
        backend: str = "native",
        size_mode: str = "disk",
        one_file_system: bool = False,
        batch_size: int = 500,
        workers: int = 1,
        resume: bool = False,
        check_readonly: bool = True,
        stop_event: Optional[threading.Event] = None,
        monitor: Optional[ResourceMonitor] = None,
        manager_db: Optional[str] = None,
        manager_scan_id: Optional[int] = None,
        progress_every: float = 0.4,
    ) -> None:
        self.db_path = db_path
        self.root_path = os.path.abspath(root_path)
        self.backend = backend
        self.size_mode = size_mode
        self.one_file_system = one_file_system
        self.batch_size = batch_size
        self.workers = max(1, int(workers))
        self.resume = resume
        self.check_readonly = bool(check_readonly)
        self.stop_event = stop_event or threading.Event()
        self.monitor = monitor
        self.manager_db = manager_db
        self.manager_scan_id = manager_scan_id
        self.progress_every = progress_every

        self.run_id: Optional[int] = None
        self._root_dev: Optional[int] = None
        self._seen_inodes: set = set()   # 하드링크(st_nlink>1) 중복 제거용
        # 디렉터리 단위 병렬 탐색용: 단일 RLock 으로 DB(공유 conn)+카운터만 보호하고
        # scandir/stat(느린 NFS I/O)는 락 밖에서 병렬 실행해 데드락 없이 가속.
        self._dlock = threading.RLock()
        self._disc_conn = None
        self._disc_active = 0
        self._worker_dirs = {}   # 워커 인덱스 → 현재 보고 있는 디렉터리(병렬 표시용)
        self._last_progress = 0.0
        self._last_wal_ckpt = 0.0
        self._mgr_conn = None
        self._phase = "discovering"
        self._status = "discovering"
        self._fs_total = 0
        self._fs_used = 0
        self._fs_free = 0
        # 진행률 누적 카운터(메모리 상에서 빠르게 갱신, DB 에 주기적으로 반영)
        self._discovered = 0
        self._processed = 0
        self._scanned_bytes = 0
        self._total_files = 0
        self._error_dirs = 0

    # ------------------------------------------------------------------ utils
    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _statvfs(self) -> Tuple[int, int, int]:
        """대상 경로가 속한 파일시스템의 (total, used, free) 바이트."""
        try:
            v = os.statvfs(self.root_path)
        except OSError:
            return (0, 0, 0)
        total = v.f_blocks * v.f_frsize
        free = v.f_bavail * v.f_frsize
        used = (v.f_blocks - v.f_bfree) * v.f_frsize
        return (total, used, free)

    def _is_readonly(self) -> bool:
        """대상 마운트가 읽기 전용(ro)으로 마운트됐는지 확인한다(statvfs ST_RDONLY)."""
        try:
            flags = os.statvfs(self.root_path).f_flag
        except OSError:
            return False
        return bool(flags & getattr(os, "ST_RDONLY", 1))

    def _flush_progress(self, conn, *, force: bool = False, current_dir=None,
                        current_depth=None, phase=None, status=None) -> None:
        """진행 상태를 DB(scan_runs)에 반영한다(과도한 쓰기를 막기 위해 스로틀).

        병렬 탐색 시 여러 워커가 호출하므로 _dlock(RLock)으로 보호한다.
        """
        with self._dlock:
            self._flush_progress_locked(
                conn, force=force, current_dir=current_dir,
                current_depth=current_depth, phase=phase, status=status)

    def _flush_progress_locked(self, conn, *, force=False, current_dir=None,
                               current_depth=None, phase=None, status=None) -> None:
        now = time.time()
        if not force and (now - self._last_progress) < self.progress_every:
            return
        self._last_progress = now
        fields = {
            "discovered_dirs": self._discovered,
            "processed_dirs": self._processed,
            "scanned_bytes": self._scanned_bytes,
            "total_files": self._total_files,
            "error_dirs": self._error_dirs,
            "active_workers": self._disc_active,   # 지금 동시에 처리 중인 워커 수
        }
        try:   # 워커별 현재 디렉터리(병렬 표시용). 탐색 단계에서만 채워짐.
            fields["worker_dirs"] = json.dumps(
                [p for p in list(self._worker_dirs.values()) if p], ensure_ascii=False)
        except Exception:
            pass
        self._current_dir = current_dir
        if current_dir is not None:
            fields["current_dir"] = current_dir
        if current_depth is not None:
            fields["current_depth"] = current_depth
        if phase is not None:
            fields["phase"] = phase
            self._phase = phase
        if status is not None:
            fields["status"] = status
            self._status = status
        dbmod.update_run(conn, self.run_id, **fields)
        conn.commit()
        # WAL 이 무한정 커지지 않도록 주기적으로 체크포인트(읽기 락이 없을 때 잘림).
        # 느린 대시보드 조회가 긴 읽기 락을 잡으면 WAL 이 수 GB 까지 부푸는 것을 막는다.
        if (now - self._last_wal_ckpt) > 30.0:
            self._last_wal_ckpt = now
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
        self._update_manager()

    def _update_manager(self, *, force: bool = False, finished: bool = False) -> None:
        """관리(매니저) DB 의 이 스캔 요약 행을 갱신한다(파일이 달라 락 경합 없음)."""
        if not self.manager_db or not self.manager_scan_id:
            return
        from . import manager as mgr
        try:
            if self._mgr_conn is None:
                self._mgr_conn = dbmod.connect(self.manager_db)
            fields = {
                "status": self._status,
                "phase": self._phase,
                "discovered_dirs": self._discovered,
                "total_dirs": self._discovered,
                "processed_dirs": self._processed,
                "total_files": self._total_files,
                "error_dirs": self._error_dirs,
                "scanned_bytes": self._scanned_bytes,
                "fs_total_bytes": self._fs_total,
                "fs_used_bytes": self._fs_used,
                "fs_free_bytes": self._fs_free,
                "current_dir": getattr(self, "_current_dir", None),
            }
            if finished:
                fields["finished_at"] = time.time()
            mgr.update_scan(self._mgr_conn, self.manager_scan_id, **fields)
            self._mgr_conn.commit()
        except Exception:
            # 관리 DB 갱신 실패가 스캔을 멈추면 안 된다.
            pass

    # --------------------------------------------------------------- lifecycle
    def run(self) -> int:
        """스캔 전체(탐색 → 집계)를 수행하고 run_id 를 반환한다."""
        conn = dbmod.connect(self.db_path)
        try:
            total, used, free = self._statvfs()
            self._fs_total, self._fs_used, self._fs_free = total, used, free

            existing = dbmod.latest_run_id(conn) if self.resume else None
            if existing:
                # 중단된 스캔 이어하기: 기존 run 과 진행 카운터를 복원
                self.run_id = existing
                self._load_progress(conn)
                dbmod.update_run(conn, self.run_id, scanner_pid=os.getpid())
            else:
                self.resume = False
                self.run_id = dbmod.create_run(
                    conn, self.root_path,
                    backend=self.backend, size_mode=self.size_mode,
                    scanner_pid=os.getpid(),
                )
            dbmod.update_run(
                conn, self.run_id,
                fs_total_bytes=total, fs_used_bytes=used, fs_free_bytes=free,
                workers=self.workers,   # 설정된 동시 스캔 스레드 수(병렬도)
                # 마운트 읽기전용(ro) 여부 — 검사 끔이면 -1(확인 안 함)
                mount_readonly=(int(self._is_readonly()) if self.check_readonly else -1),
            )
            conn.commit()
            self._update_manager()  # fs 용량 등 초기 요약 반영
            if self.monitor is not None:
                self.monitor.run_id = self.run_id

            try:
                self._root_dev = os.stat(self.root_path).st_dev
            except OSError:
                self._root_dev = None

            # 루트 디렉터리를 프론티어에 등록
            root_name = os.path.basename(self.root_path.rstrip("/")) or self.root_path
            conn.execute(
                """INSERT OR IGNORE INTO directories
                   (run_id, parent_id, path, name, depth, status)
                   VALUES (?, NULL, ?, ?, 0, 'pending')""",
                (self.run_id, self.root_path, root_name),
            )
            conn.commit()

            self._discover(conn)
            if self._stopped():
                self._finish(conn, "paused")
                return self.run_id

            self._aggregate(conn)
            self._finish(conn, "paused" if self._stopped() else "done")
            return self.run_id
        except Exception as exc:  # 치명적 오류는 기록하고 종료
            if self.run_id is not None:
                dbmod.update_run(conn, self.run_id, status="error", error=str(exc),
                                 finished_at=time.time())
                conn.commit()
            self._status = "error"
            self._phase = "error"
            try:
                if self.manager_db and self.manager_scan_id:
                    from . import manager as mgr
                    if self._mgr_conn is None:
                        self._mgr_conn = dbmod.connect(self.manager_db)
                    mgr.update_scan(self._mgr_conn, self.manager_scan_id,
                                    status="error", phase="error", error=str(exc),
                                    finished_at=time.time())
                    self._mgr_conn.commit()
            except Exception:
                pass
            raise
        finally:
            conn.close()
            if self._mgr_conn is not None:
                try:
                    self._mgr_conn.close()
                except Exception:
                    pass

    def _load_progress(self, conn) -> None:
        """재개 시 기존 per-run DB 에서 진행 카운터를 복원한다."""
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status!='pending' THEN 1 ELSE 0 END),0) AS disc,
                 COALESCE(SUM(CASE WHEN status='done' THEN 1 ELSE 0 END),0)     AS done,
                 COALESCE(SUM(own_bytes),0)  AS b,
                 COALESCE(SUM(file_count),0) AS f,
                 COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END),0) AS e
               FROM directories WHERE run_id=?""",
            (self.run_id,),
        ).fetchone()
        self._discovered = int(row["disc"])
        self._processed = int(row["done"])
        self._scanned_bytes = int(row["b"])
        self._total_files = int(row["f"])
        self._error_dirs = int(row["e"])

    def _safe_stat(self, entry):
        try:
            return entry.stat(follow_symlinks=False)
        except OSError:
            return None

    def _safe_stat_path(self, path: str):
        try:
            return os.stat(path)
        except OSError:
            return None

    def _finish(self, conn, status: str) -> None:
        # 총 디렉터리 수가 아직 0 이면 탐색 수치로 맞춘다.
        self._status = status
        self._phase = status
        self._current_dir = None
        dbmod.update_run(
            conn, self.run_id,
            status=status, phase=status,
            finished_at=time.time(),
            discovered_dirs=self._discovered,
            total_dirs=self._discovered,
            processed_dirs=self._processed,
            scanned_bytes=self._scanned_bytes,
            total_files=self._total_files,
            error_dirs=self._error_dirs,
            current_dir=None,
        )
        conn.commit()
        self._update_manager(force=True, finished=True)

    # ------------------------------------------------------------- 1단계: 탐색
    def _discover(self, conn) -> None:
        """DB 를 프론티어로 사용하는 디렉터리 단위 병렬 탐색.

        workers 개의 워커 스레드가 DB 의 'pending' 디렉터리를 나눠 맡아 동시에
        scandir/stat 한다(느린 NFS I/O 가 병렬화됨). DB 접근과 진행 카운터는
        단일 RLock(_dlock)으로 보호하고, scandir/stat 은 락 밖에서 실행해
        데드락 없이 병렬성을 얻는다. 메모리에는 워커별로 한 디렉터리의 한 청크만
        올라온다.
        """
        dbmod.update_run(conn, self.run_id, status="discovering", phase="discovering")
        # 이전 실행에서 'claimed' 로 남은 것은 다시 pending 으로(재개 안전)
        conn.execute("UPDATE directories SET status='pending' "
                     "WHERE run_id=? AND status='claimed'", (self.run_id,))
        conn.commit()

        self._disc_conn = dbmod.connect(self.db_path)
        self._disc_active = 0
        n = max(1, int(self.workers))
        self._worker_dirs = {i: None for i in range(n)}   # 워커별 현재 디렉터리
        try:
            if n == 1:
                self._discover_worker(0)          # 현재 스레드에서 단독 실행
            else:
                threads = [threading.Thread(target=self._discover_worker, args=(i,),
                                            name="disc-%d" % i, daemon=True)
                           for i in range(n)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
        finally:
            with self._dlock:
                try:
                    self._disc_conn.commit()
                    self._disc_conn.close()
                except Exception:
                    pass
                self._disc_conn = None
            self._worker_dirs = {}   # 탐색 종료 → 비움(집계 단계는 단일 current_dir)

        self._flush_progress(conn, force=True)
        dbmod.update_run(conn, self.run_id, total_dirs=self._discovered)
        conn.commit()
        self._seen_inodes.clear()  # 집계 단계에선 불필요 — 메모리 회수

    def _claim_batch(self):
        """프론티어에서 pending 디렉터리 한 배치를 원자적으로 '맡는다'(claimed).

        반환: 행 목록(맡은 것) / [] (지금은 없지만 다른 워커가 작업 중) /
              None (더는 생길 일 없음 → 종료).
        """
        with self._dlock:
            rows = self._disc_conn.execute(
                """SELECT id, path, depth FROM directories
                   WHERE run_id=? AND status='pending'
                   ORDER BY depth ASC, id ASC LIMIT ?""",
                (self.run_id, self.batch_size),
            ).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                ph = ",".join("?" * len(ids))
                self._disc_conn.execute(
                    "UPDATE directories SET status='claimed' WHERE id IN (%s)" % ph, ids)
                self._disc_conn.commit()
                self._disc_active += 1
                return rows
            # pending 없음: 아무도 작업 중이 아니면 끝, 아니면 잠시 대기
            return None if self._disc_active == 0 else []

    def _discover_worker(self, widx: int = 0) -> None:
        while not self._stopped():
            batch = self._claim_batch()
            if batch is None:
                break
            if not batch:
                self.stop_event.wait(0.02)
                continue
            try:
                for row in batch:
                    if self._stopped():
                        break
                    self._worker_dirs[widx] = row["path"]   # 이 워커가 지금 보는 디렉터리
                    self._discover_one(row["id"], row["path"], row["depth"])
            finally:
                self._worker_dirs[widx] = None
                with self._dlock:
                    self._disc_active -= 1

    def _discover_one(self, dir_id: int, path: str, depth: int) -> None:
        """디렉터리 하나를 훑는다(병렬 워커가 호출).

        scandir/stat(느린 I/O)은 락 밖에서 하고, DB 쓰기와 공유 카운터/하드링크
        집합은 _dlock 안에서만 건드린다(데드락 없이 병렬 처리).
        """
        own_bytes = 0
        file_count = 0
        subdir_count = 0
        children: List[tuple] = []
        err: Optional[str] = None
        file_chunk: list = []

        def flush_files():
            # 청크를 stat(락 밖) 한 뒤, _dlock 안에서 용량/개수/하드링크집합/진행을 갱신.
            nonlocal own_bytes, file_count
            if not file_chunk:
                return
            chunk = file_chunk[:]
            file_chunk.clear()
            stats = [self._safe_stat(e) for e in chunk]   # I/O — 락 밖
            cb = 0
            cf = 0
            with self._dlock:
                for st in stats:
                    if st is None:
                        continue
                    cf += 1
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in self._seen_inodes:
                            continue  # 하드링크 중복 — 용량은 한 번만
                        self._seen_inodes.add(key)
                    cb += _entry_bytes(st, self.size_mode)
                own_bytes += cb
                file_count += cf
                self._scanned_bytes += cb
                self._total_files += cf
                self._flush_progress_locked(self._disc_conn,
                                            current_dir=path, current_depth=depth)

        try:
            with os.scandir(path) as it:                  # I/O — 락 밖
                for entry in it:
                    if self._stopped():
                        break
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        # 다른 파일시스템으로 넘어가지 않도록(옵션)
                        if self.one_file_system and self._root_dev is not None:
                            try:
                                if entry.stat(follow_symlinks=False).st_dev != self._root_dev:
                                    continue
                            except OSError:
                                continue
                        subdir_count += 1
                        children.append(
                            (self.run_id, dir_id, entry.path, entry.name, depth + 1))
                    else:
                        file_chunk.append(entry)
                        if len(file_chunk) >= STAT_CHUNK:
                            flush_files()
            flush_files()
        except OSError as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            with self._dlock:
                self._error_dirs += 1

        # 디렉터리 자기 inode 분(du 와 동일). 파일 분은 flush_files 에서 이미 반영됨.
        dstat = self._safe_stat_path(path)                # I/O — 락 밖
        dir_bytes = _entry_bytes(dstat, self.size_mode) if dstat is not None else 0

        with self._dlock:
            own_bytes += dir_bytes
            self._scanned_bytes += dir_bytes
            if children:
                self._disc_conn.executemany(
                    """INSERT OR IGNORE INTO directories
                       (run_id, parent_id, path, name, depth, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    children,
                )
            self._disc_conn.execute(
                """UPDATE directories
                   SET status='discovered', own_bytes=?, file_count=?, subdir_count=?, error=?
                   WHERE id=?""",
                (own_bytes, file_count, subdir_count, err, dir_id),
            )
            self._disc_conn.commit()
            self._discovered += 1
            if depth > 0:
                self._maybe_update_depth(self._disc_conn, depth)
            self._flush_progress_locked(self._disc_conn,
                                        current_dir=path, current_depth=depth)

    def _maybe_update_depth(self, conn, depth: int) -> None:
        # max_depth 는 가끔만 갱신해도 충분하다.
        if depth and depth % 1 == 0:
            conn.execute(
                "UPDATE scan_runs SET max_depth=MAX(max_depth, ?) WHERE id=?",
                (depth, self.run_id),
            )

    # --------------------------------------------------------- 2단계: 상향식 집계
    def _aggregate(self, conn) -> None:
        """가장 깊은 레벨부터 0 까지, 레벨별로 재귀 용량을 집계한다."""
        dbmod.update_run(conn, self.run_id, status="sizing", phase="sizing")
        conn.commit()

        row = conn.execute(
            "SELECT COALESCE(MAX(depth),0) AS d FROM directories WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        max_depth = int(row["d"])

        read_conn = dbmod.connect(self.db_path)
        du_path = shutil.which("du") if self.backend == "du" else None
        if self.backend == "du" and not du_path:
            # du 가 없으면 native 로 폴백
            self.backend = "native"

        try:
            for depth in range(max_depth, -1, -1):
                if self._stopped():
                    break
                cur = read_conn.execute(
                    """SELECT id, path, own_bytes, file_count FROM directories
                       WHERE run_id=? AND depth=? AND status!='done'
                       ORDER BY path ASC""",
                    (self.run_id, depth),
                )
                while True:
                    if self._stopped():
                        break
                    chunk = cur.fetchmany(self.batch_size)
                    if not chunk:
                        break
                    for r in chunk:
                        if self._stopped():
                            break
                        self._aggregate_one(conn, r, depth, du_path)
                    conn.commit()
                    self._flush_progress(conn)
            self._flush_progress(conn, force=True, current_dir=None)
        finally:
            read_conn.close()
            if self.monitor is not None:
                self.monitor.set_du_pid(None)

    def _aggregate_one(self, conn, row, depth: int, du_path: Optional[str]) -> None:
        dir_id = row["id"]
        path = row["path"]
        own_bytes = int(row["own_bytes"])
        file_count = int(row["file_count"])

        # 자식들의 재귀 합(이미 더 깊은 레벨에서 계산 완료됨)
        agg = conn.execute(
            """SELECT COALESCE(SUM(total_bytes),0) AS b,
                      COALESCE(SUM(total_files),0) AS f
               FROM directories WHERE run_id=? AND parent_id=?""",
            (self.run_id, dir_id),
        ).fetchone()
        child_bytes = int(agg["b"])
        child_files = int(agg["f"])

        if du_path is not None:
            total_bytes = self._run_du(du_path, path)
            if total_bytes < 0:  # du 실패 시 native 로 대체
                total_bytes = own_bytes + child_bytes
        else:
            total_bytes = own_bytes + child_bytes

        total_files = file_count + child_files

        conn.execute(
            """UPDATE directories
               SET total_bytes=?, total_files=?, status='done', scanned_at=?
               WHERE id=?""",
            (total_bytes, total_files, time.time(), dir_id),
        )
        self._processed += 1
        self._flush_progress(conn, current_dir=path, current_depth=depth)

    def _run_du(self, du_path: str, path: str) -> int:
        """디렉터리 하나에 대해 시스템 du 를 별도 프로세스로 실행.

        실행 중인 du 자식의 PID 를 모니터에 알려 그 프로세스의 메모리를
        대시보드에서 보여줄 수 있게 한다. 바이트 수를 반환(실패 시 -1).
        """
        # -s: 합계, -x: 한 파일시스템, --block-size=1: 바이트 단위
        args = [du_path, "-s", "--block-size=1"]
        if self.size_mode == "apparent":
            args.append("--apparent-size")
        if self.one_file_system:
            args.append("-x")
        args.append(path)
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True
            )
        except OSError:
            return -1
        if self.monitor is not None:
            self.monitor.set_du_pid(proc.pid)
        try:
            out, _ = proc.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return -1
        finally:
            if self.monitor is not None:
                self.monitor.set_du_pid(None)
        if proc.returncode not in (0, 1):  # du 는 일부 접근불가 시 1 반환
            return -1
        try:
            return int(out.split("\t", 1)[0].split()[0])
        except (ValueError, IndexError):
            return -1


def run_scan(
    db_path: str,
    root_path: str,
    *,
    backend: str = "native",
    size_mode: str = "disk",
    one_file_system: bool = False,
    batch_size: int = 500,
    workers: int = 1,
    resume: bool = False,
    check_readonly: bool = True,
    sample_interval: float = 2.0,
    stop_event: Optional[threading.Event] = None,
    with_monitor: bool = True,
    manager_db: Optional[str] = None,
    manager_scan_id: Optional[int] = None,
) -> int:
    """편의 함수: DB 초기화 → 모니터 시작 → 스캔(탐색+집계) 실행 → 모니터 정리.

    스캔과 자원 모니터링을 한 프로세스 안에서 함께 돌린다. 모니터는 스캔의
    run_id 가 만들어지기 전에는 기록을 보류하다가(run_id 가드), Scanner.run()
    이 run_id 를 설정해 주면 그때부터 샘플을 적재한다.

    manager_db/manager_scan_id 가 주어지면 진행 상황을 관리 DB 에도 반영한다.
    """
    dbmod.init_db(db_path)
    stop_event = stop_event or threading.Event()

    monitor: Optional[ResourceMonitor] = None
    if with_monitor:
        monitor = ResourceMonitor(
            db_path, run_id=0, scanner_pid=os.getpid(),
            interval=sample_interval, stop_event=threading.Event(),
        )

    scanner = Scanner(
        db_path, root_path,
        backend=backend, size_mode=size_mode,
        one_file_system=one_file_system, batch_size=batch_size,
        workers=workers, resume=resume, check_readonly=check_readonly,
        stop_event=stop_event, monitor=monitor,
        manager_db=manager_db, manager_scan_id=manager_scan_id,
    )

    if monitor is not None:
        monitor.start()
    try:
        return scanner.run()  # 내부에서 run 생성 후 monitor.run_id 를 설정한다.
    finally:
        if monitor is not None:
            monitor.stop_event.set()
            monitor.join(timeout=5)

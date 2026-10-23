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

import heapq
import json
import os
import queue
import shutil
import subprocess
import sys
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

# 깊이 접기(fold) 인라인 walk 예산 — 한 디렉터리 처리에서 인라인으로 걷는 양(파일+디렉터리 수)·시간을
# 이걸로 묶고, 넘치면 아직 안 훑은 하위 디렉터리를 프론티어 행으로 넘겨 다른 워커가 병렬로 이어받게
# 한다(= 체크포인트). 예전엔 fold 루트 하나의 하위 전체(수억 파일이면 수일)를 한 스레드가 통째로 걸어
# 병렬성이 사라졌고, 중단(업그레이드·재시작)되면 그 walk 전체를 처음부터 다시 해 거대 트리에서 스캔이
# 끝나지 않았다. 넘긴 행(깊이 > fold_depth)은 집계 후 정리돼 결과 DB 는 순수 접기와 같은 모양이다.
FOLD_BUDGET_ENTRIES = 200_000
FOLD_BUDGET_SECS = 300.0

# WAL 체크포인트: 공유 락(_dlock) 안에서 '기다리지 않고' 시도한다. 예전엔 busy_timeout(30초)만큼
# 읽기 트랜잭션이 끝나기를 락을 쥔 채 기다려 워커 전체가 멈췄다(재현: 읽기 4초 유지 → 스캔 정체 +4.1초,
# 현장은 대시보드 읽기가 계속돼 최대 30초씩 반복). 주기는 '완료 시각' 기준이고 busy 가 이어지면 2배씩
# 늘린다(최대 WAL_CKPT_MAX) — 대기 직후 또 실행되는 일이 없다.
WAL_CKPT_EVERY = 30.0
WAL_CKPT_MAX = 600.0
# 디스크 여유/DB 크기 가드 점검 주기(초) — 체크포인트 backoff 와 무관하게 유지
GUARD_EVERY = 30.0
# 청크 작업 나눔은 stat 이 I/O 대기형(파일당 이 µs 이상)일 때만 켠다. 로컬 SSD 처럼 stat 이 수 µs 면
# 나눠 봐야 GIL 경쟁만 늘어 되레 느리다(측정: 평면 3만 파일 로컬 — 워커 8 이 1보다 5배 느림).
# NFS 는 파일당 수십 µs~수백 ms 라 나눔이 켜져 거대 평면 폴더도 워커 수만큼 병렬이 된다.
SHARE_MIN_STAT_US = 25.0

# 파일 나이(mtime) 버킷 — (상한 일수, 라벨). 마지막은 상한 None(그 이상 전부).
AGE_BUCKETS = [
    (30, "30일 이내"), (90, "30~90일"), (365, "90일~1년"),
    (730, "1~2년"), (1825, "2~5년"), (None, "5년+"),
]
# 집계 dict 가 무한정 커지지 않도록 distinct 키 상한(초과분은 '(기타)'로 합산)
STAT_KEY_CAP = 5000

# 최대 파일 Top-N(힙으로 유지) — 메모리 약 N×(경로 길이) 수준
TOP_FILES_N = 200

# --- 집계(2단계) 일괄 UPDATE 튜닝 — 43만 행 재현 벤치로 확정(행 루프 27.9k → 158.4k 행/초) ---
# id 윈도우 폭: 윈도우마다 커밋해 하트비트·일시정지·재개(status='done')가 유지된다.
AGG_WINDOW = 50000
# 집계 중 페이지 캐시(KB): 기본 2MB 는 수십만 행의 자식 합 조회에 너무 작다(측정 1.4배 차).
AGG_CACHE_KB = 64 * 1024
# WAL 자동 체크포인트 간격(페이지): 기본 1000 이면 윈도우 커밋마다 백필이 돌아
# 되레 행 루프보다 느려진다(측정 23.7k 행/초 → 상향 시 158.4k). 30초 주기의
# wal_checkpoint(TRUNCATE) 가 별도로 WAL 크기를 계속 눌러 준다.
AGG_AUTOCKPT_PAGES = 25000


def _age_bucket(ref: float, mtime: float) -> str:
    days = (ref - mtime) / 86400.0
    for lim, label in AGE_BUCKETS:
        if lim is None or days < lim:
            return label
    return "5년+"


# 파일 크기 분포 버킷 — 작은 파일이 많으면 메타데이터 부담, 큰 파일은 정리 1순위 식별.
SIZE_BUCKETS = [
    (1, "0 (빈 파일)"), (1024, "1B~1KB"), (1024 ** 2, "1KB~1MB"),
    (10 * 1024 ** 2, "1~10MB"), (100 * 1024 ** 2, "10~100MB"),
    (1024 ** 3, "100MB~1GB"), (10 * 1024 ** 3, "1~10GB"), (None, "10GB+")]


def _size_bucket(n: int) -> str:
    for lim, label in SIZE_BUCKETS:
        if lim is None or n < lim:
            return label
    return "10GB+"


def _entry_bytes(stat_result, size_mode: str) -> int:
    """size_mode 에 따라 한 파일이 차지하는 바이트를 계산."""
    if size_mode == "apparent":
        return int(stat_result.st_size)
    # 'disk' (기본): du 와 동일하게 실제 점유 블록 기준
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:  # 일부 플랫폼은 st_blocks 미지원
        return int(stat_result.st_size)
    return int(blocks) * BLOCK_UNIT


# --- 스레드 로컬 집계 헬퍼(직렬 구간 축소) ---
# 파일당 파이썬 작업(나이/확장자/Top-N)을 _dlock '밖'에서 로컬에 모으고, 락 안에서는
# 짧게 병합만 한다. 워커들이 락에 줄 서지 않고 곧바로 다음 stat 을 발사 → 고지연 NAS 에서
# 동시 in-flight stat 이 늘어 처리량이 오른다(직렬화된 커밋/집계가 천장이던 문제 완화).
def _local_top(h: list, path: str, eb: int, st) -> None:
    """로컬 Top-N 후보 힙(크기 TOP_FILES_N 로 제한 → 병합 비용 작게)."""
    item = (eb, path, st.st_mtime, st.st_uid, st.st_atime)
    if len(h) < TOP_FILES_N:
        heapq.heappush(h, item)
    elif eb > h[0][0]:
        heapq.heapreplace(h, item)


def _local_accum(ref: float, l_age: dict, l_atime: dict, l_uid: dict, l_ext: dict,
                 l_size: dict, name: str, st, eb: int, cb: Optional[int] = None) -> None:
    """한 파일을 로컬 나이/접근나이/소유자/확장자/크기 dict 에 더한다(캡 없음 — 청크라 작음).

    eb=파일 원본 크기(크기 버킷 키), cb=실제로 더할 용량(하드링크 중복이면 0, 기본 eb).
    """
    counted = eb if cb is None else cb

    def bump(d, k):
        e = d.get(k)
        if e is None:
            d[k] = [counted, 1]
        else:
            e[0] += counted
            e[1] += 1
    bump(l_age, _age_bucket(ref, st.st_mtime))
    bump(l_atime, _age_bucket(ref, st.st_atime))
    bump(l_uid, str(st.st_uid))
    bump(l_size, _size_bucket(eb))     # 키는 원본 크기, 값은 counted
    ext = os.path.splitext(name)[1].lower() or "(없음)"
    if len(ext) > 24:
        ext = ext[:24]
    bump(l_ext, ext)


def _merge_counts(dst: dict, src: dict) -> None:
    """[bytes, files] dict 병합(캡 없음 — 공유 집계로 갈 때 _accum_merge 가 캡 적용)."""
    for k, v in src.items():
        e = dst.get(k)
        if e is None:
            dst[k] = [v[0], v[1]]
        else:
            e[0] += v[0]
            e[1] += v[1]


def _top_merge_into(h: list, items: list) -> None:
    """Top-N 후보 힙 items 를 힙 h 에 병합(크기 TOP_FILES_N 유지)."""
    for it in items:
        if len(h) < TOP_FILES_N:
            heapq.heappush(h, it)
        elif it[0] > h[0][0]:
            heapq.heapreplace(h, it)


class _TimedLock:
    """진단 계측이 붙은 재진입 락(_dlock 전용).

    with/acquire/release 는 RLock 과 같고, 바깥쪽 획득마다 '대기 시간'과 '보유 시간'을 누적한다
    (합·최대·횟수). 통계 갱신은 락을 쥔 채 하므로 별도 동기화가 필요 없다. 지금 기다리는 스레드
    집합(waiting)·보유 여부는 진단 스냅샷용 — 콜스택 함수 이름으로 추측하지 않고 '락 대기 중'을
    정확히 센다(락 대기는 C 레벨이라 프레임에 안 보인다).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0
        self._t_acq = 0.0
        self.waiting: set = set()
        self.count = 0
        self.wait_total = 0.0
        self.wait_max = 0.0
        self.hold_total = 0.0
        self.hold_max = 0.0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        if self._owner == me:                      # 재진입(소유자만 _owner 를 자기로 둘 수 있다)
            ok = self._lock.acquire(blocking, timeout)
            if ok:
                self._depth += 1
            return ok
        self.waiting.add(me)
        t0 = time.monotonic()
        try:
            ok = self._lock.acquire(blocking, timeout)
        finally:
            self.waiting.discard(me)
        if ok:
            now = time.monotonic()
            w = now - t0
            self.count += 1
            self.wait_total += w
            if w > self.wait_max:
                self.wait_max = w
            self._owner = me
            self._depth = 1
            self._t_acq = now
        return ok

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            h = time.monotonic() - self._t_acq
            self.hold_total += h
            if h > self.hold_max:
                self.hold_max = h
            self._owner = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def held(self) -> bool:
        return self._owner is not None

    def snapshot(self) -> dict:
        return {
            "acquisitions": self.count,
            "wait_total_s": round(self.wait_total, 3), "wait_max_s": round(self.wait_max, 3),
            "hold_total_s": round(self.hold_total, 3), "hold_max_s": round(self.hold_max, 3),
            "waiting_now": len(self.waiting), "held_now": self._owner is not None,
        }


class _DirAcc:
    """디렉터리(fold 루트 포함) 하나를 처리하는 동안의 누적분 — 완료 확정 시에만 공유 집계에 병합한다.

    파일별 통계(나이/소유자/확장자/크기)·Top-N·새 하드링크 inode·용량/파일 수를 여기 모아 두고,
    디렉터리 완료 트랜잭션과 함께 공유 집계·DB 에 반영한다. 중단으로 버려지면 통계도 같이
    버려져, 재개 후 그 디렉터리를 다시 훑어도 통계가 중복되지 않는다(예전엔 청크마다 공유
    집계에 바로 더해, 중단 시점의 부분 통계가 저장되고 재개 후 또 더해졌다 — 파일 25개가
    age 통계에선 26~50개). 청크를 다른 워커가 대신 처리할 수 있어 갱신은 _dlock 안에서 한다.
    """
    __slots__ = ("bytes", "files", "age", "atime", "uid", "ext", "size", "top",
                 "new_inodes", "pending", "abandoned")

    def __init__(self) -> None:
        self.bytes = 0
        self.files = 0
        self.age: dict = {}
        self.atime: dict = {}
        self.uid: dict = {}
        self.ext: dict = {}
        self.size: dict = {}
        self.top: list = []
        self.new_inodes: list = []
        self.pending = 0          # 다른 워커가 대신 처리 중인 청크 수
        self.abandoned = False    # 중단 등으로 버려짐 — 늦게 끝난 청크는 반영하지 않음


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
        max_depth: int = 0,
        fold_depth: int = 0,
        db_max_bytes: int = 0,
        hardlink_dedup: bool = True,
        min_free_bytes: int = 0,
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
        # 초대용량(수십억 파일) 안전장치
        self.max_depth = max(0, int(max_depth or 0))         # 0=무제한, N=그 깊이까지만 탐색(깊은 용량 미포함·빠른 컷)
        # 깊이 접기: 깊이 N 까지만 행을 저장하고, 그 아래는 내려가서 용량은 다 세되
        # N 디렉터리의 own_bytes 에 합산한다 → DB 행 수(=크기) 묶임 + 합계 정확.
        self.fold_depth = max(0, int(fold_depth or 0))       # 0=off
        # fold 인라인 walk 예산(넘치면 남은 하위를 행으로 넘김) — 인스턴스에서 조정 가능(테스트·튜닝)
        self.fold_budget = FOLD_BUDGET_ENTRIES
        self.fold_budget_secs = FOLD_BUDGET_SECS
        # DB 크기 가드: per-run DB(.db + -wal)가 이 바이트를 넘으면 자동 일시정지.
        self.db_max_bytes = max(0, int(db_max_bytes or 0))   # 0=off
        self.hardlink_dedup = bool(hardlink_dedup)           # False 면 메모리 절약(하드링크 중복 셈)
        self.min_free_bytes = max(0, int(min_free_bytes or 0))  # 0=off. 데이터 디스크 여유가 이 미만이면 자동 일시정지
        self.stop_event = stop_event or threading.Event()
        self.monitor = monitor
        self.manager_db = manager_db
        self.manager_scan_id = manager_scan_id
        self.progress_every = progress_every

        self.run_id: Optional[int] = None
        self._root_dev: Optional[int] = None
        self._stop_reason: Optional[str] = None   # 디스크 부족 등 자동 정지 사유
        self._seen_inodes: set = set()   # 하드링크(st_nlink>1) 중복 제거용
        # 디렉터리 단위 병렬 탐색용: 단일 RLock 으로 DB(공유 conn)+카운터만 보호하고
        # scandir/stat(느린 NFS I/O)는 락 밖에서 병렬 실행해 데드락 없이 가속.
        self._dlock = _TimedLock()   # 대기/보유 시간 계측(진단) 포함
        self._disc_conn = None
        self._disc_active = 0
        self._worker_dirs = {}   # 워커 인덱스 → 현재 보고 있는 디렉터리(병렬 표시용)
        self._last_progress = 0.0
        # WAL 체크포인트/가드 스케줄(완료 시각 기준) + 체크포인트 통계(진단 노출)
        self.wal_ckpt_every = WAL_CKPT_EVERY
        self.wal_ckpt_max = WAL_CKPT_MAX
        self.guard_every = GUARD_EVERY
        self._next_ckpt_at = 0.0
        self._next_guard_at = 0.0
        self._ckpt = {"count": 0, "busy_count": 0, "busy_streak": 0, "last_ms": 0.0,
                      "max_ms": 0.0, "last_busy": 0, "last_at": 0.0, "wal_mb": 0.0, "next_at": 0.0}
        self._advanced_at = 0.0        # 마지막으로 파일/디렉터리 수가 실제로 늘어난 시각
        self._adv_col = False          # scan_runs.advanced_at 컬럼 존재 여부(구버전 DB 호환)
        self._pagg_col = False         # scan_runs.partial_agg_at 컬럼 존재 여부
        # 워커 예외 전달: 한 워커가 죽으면 전체를 멈추고 run() 이 status=error 로 끝낸다.
        self._worker_exc: list = []
        self._abort = False
        # 청크 작업 나눔: 거대 평면 폴더(파일 수십만)를 한 워커가 혼자 훑지 않도록, 유휴 워커가
        # 파일 청크의 stat/집계를 대신한다. 큐는 메모리 제한(워커 수×2 청크).
        self._chunk_q: Optional["queue.Queue"] = None
        self._q_cv = threading.Condition()
        self._idle_workers = 0
        self._helping = 0
        self._stat_us_ema = 0.0        # 최근 파일당 stat 소요(µs, EMA) — 나눔 on/off 판단
        # 집계 리포트: 파일 나이/소유자(uid)/확장자별 [bytes, files]. 메모리 누적 후 DB 저장.
        self._stat_age: dict = {}
        self._stat_atime_age: dict = {}   # 마지막 접근(atime) 기준 나이 분포
        self._stat_uid: dict = {}
        self._stat_ext: dict = {}
        self._stat_size: dict = {}        # 파일 크기 버킷별 [bytes, files]
        self._stats_ref = 0.0       # 나이 계산 기준 시각(스캔 시작)
        self._last_stats_write = 0.0
        # 최대 파일 Top-N: (bytes, path, mtime, uid, atime) 최소힙 — 가장 작은 게 루트
        self._top_files: list = []
        # 재개 시간 추적: elapsed_accum=모든 세션 누적 '활성' 시간(일시정지 갭 제외),
        # _elapsed_mark=마지막으로 누적한 시각(kill -9 에도 마지막 flush 까지 보존).
        self._elapsed_accum = 0.0
        self._elapsed_mark = 0.0
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
        # 집계(sizing) 행/초 자체 측정 — (마지막 flush 시각, 그때의 완료 수) + EMA
        self._agg_mark: Optional[Tuple[float, int]] = None
        self._agg_rate = 0.0

    # ------------------------------------------------------------------ utils
    def _stopped(self) -> bool:
        return self._abort or self.stop_event.is_set()

    def _wal_mb(self) -> float:
        """per-run DB 의 현재 WAL 파일 크기(MB). 없으면 0."""
        try:
            return os.path.getsize(self.db_path + "-wal") / 1e6
        except OSError:
            return 0.0

    def _log(self, msg: str, conn=None) -> None:
        """진단 로그 한 줄 — stderr(journalctl)와 per-run DB(scan_log) 양쪽에 남긴다.

        병목을 추측이 아니라 실측으로 찾기 위한 기록. 실패해도 스캔에 영향 없어야 한다.
        conn 을 주면 scan_log 에도 저장(커밋은 스캔의 주기 커밋에 얹혀 간다).
        """
        try:
            sys.stderr.write("[scan#%s %s] %s\n" % (self.run_id, self._phase, msg))
            sys.stderr.flush()
        except Exception:
            pass
        if conn is not None:
            try:
                dbmod.add_scan_log(conn, self.run_id, self._phase, msg)
            except Exception:
                pass

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
        # 활성 작업 시간 누적(마지막 flush 이후 경과분). 일시정지 중엔 flush 가 없어
        # 멈추고, kill -9 에도 마지막 flush 까지는 보존된다.
        if self._elapsed_mark:
            self._elapsed_accum += max(0.0, now - self._elapsed_mark)
        self._elapsed_mark = now
        fields = {
            "discovered_dirs": self._discovered,
            "processed_dirs": self._processed,
            "scanned_bytes": self._scanned_bytes,
            "total_files": self._total_files,
            "error_dirs": self._error_dirs,
            "active_workers": self._disc_active + self._helping,   # 처리 중(청크 도움 포함) 워커 수
            "elapsed_accum": self._elapsed_accum,
        }
        if self._adv_col and self._advanced_at:
            fields["advanced_at"] = self._advanced_at
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
        if self._phase == "sizing":
            # 집계 행/초를 스캐너가 직접 측정해 기록 — 진단 API 의 1.2초 표본이
            # 윈도우 커밋 사이에 걸리면 0/초로 보이는 문제를 보완(EMA 로 완만하게).
            if self._agg_mark is not None:
                dt = now - self._agg_mark[0]
                if dt > 0:
                    inst = max(0.0, (self._processed - self._agg_mark[1]) / dt)
                    self._agg_rate = (
                        0.6 * self._agg_rate + 0.4 * inst) if self._agg_rate else inst
            self._agg_mark = (now, self._processed)
            fields["agg_rate"] = round(self._agg_rate, 1)
        dbmod.update_run(conn, self.run_id, **fields)
        conn.commit()
        # 집계 리포트(나이/소유자/확장자)를 주기적으로 저장(재개 안전).
        if (now - self._last_stats_write) > 20.0:
            self._last_stats_write = now
            self._write_stats(conn)
        # WAL 체크포인트: 기다리지 않고 시도(busy 면 다음 주기에 backoff). 아래 참고.
        if now >= self._next_ckpt_at:
            self._checkpoint_locked(conn)
        # 디스크 여유/DB 크기 가드 — 체크포인트 결과와 무관하게 주기적으로
        if now >= self._next_guard_at:
            self._next_guard_at = now + float(self.guard_every)
            self._check_guards()
        self._update_manager()

    def _checkpoint_locked(self, conn) -> None:
        """WAL 체크포인트(TRUNCATE)를 '기다리지 않고' 시도한다(_dlock 안, 공유 연결).

        읽기 스냅샷이 남아 있으면 즉시 busy=1 로 돌아오되 가능한 프레임은 백필한다(PASSIVE 와
        같은 양 — 실측). busy_timeout 은 이 호출 동안만 0 으로 두고 finally 에서 복구한다(일반
        쓰기의 대기 시간은 그대로). 다음 시각은 '완료 시각' 기준이며 busy 가 이어지면 2배씩
        늘려(최대 wal_ckpt_max) 직후 재실행·헛시도를 막는다. 읽기가 풀리면 다음 시도에서 WAL 이
        회수된다(회수 시 로그). 소요·busy·WAL 크기를 통계(_ckpt)와 진단 로그에 남긴다.
        """
        st = self._ckpt
        wal0 = self._wal_mb()
        t0 = time.monotonic()
        busy = 1
        frames = (0, 0)
        err = None
        try:
            conn.execute("PRAGMA busy_timeout=0")
            try:
                r = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if r is not None:
                    busy = int(r[0])
                    frames = (int(r[1]), int(r[2]))
            finally:
                conn.execute("PRAGMA busy_timeout=%d" % dbmod.BUSY_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — 체크포인트 실패는 스캔을 막지 않는다
            err = str(exc)
        dt_ms = (time.monotonic() - t0) * 1000.0
        wal1 = self._wal_mb()
        st["count"] += 1
        st["last_ms"] = round(dt_ms, 1)
        st["max_ms"] = max(st["max_ms"], round(dt_ms, 1))
        st["last_busy"] = busy
        st["last_at"] = time.time()
        st["wal_mb"] = round(wal1, 2)
        prev_streak = st["busy_streak"]
        if busy:
            st["busy_count"] += 1
            st["busy_streak"] += 1
        else:
            st["busy_streak"] = 0
        interval = min(float(self.wal_ckpt_max),
                       float(self.wal_ckpt_every) * (2 ** min(st["busy_streak"], 5)))
        self._next_ckpt_at = time.time() + interval   # 완료 시각 기준 — 직후 재실행 없음
        st["next_at"] = self._next_ckpt_at
        if err is not None:
            if prev_streak == 0:
                self._log("wal_checkpoint 실패: %s (WAL %.1fMB)" % (err, wal1), conn)
        elif busy:
            if st["busy_streak"] in (1, 4, 16, 64):   # 반복 busy 는 드문드문만 기록
                self._log("wal_checkpoint busy: %d/%d 프레임만 백필 — 긴 읽기(대시보드 조회 등)가 "
                          "WAL 잘라내기를 막음 (WAL %.1fMB, %.0fms, 연속 %d회, 다음 %.0f초 뒤)"
                          % (frames[1], frames[0], wal1, dt_ms, st["busy_streak"], interval), conn)
        elif prev_streak:
            self._log("wal_checkpoint 회수: 읽기 해제 후 WAL %.1fMB → %.1fMB (%.0fms, busy %d회 뒤)"
                      % (wal0, wal1, dt_ms, prev_streak), conn)
        elif dt_ms > 1000.0:
            self._log("wal_checkpoint 느림: %.0fms (WAL %.1fMB → %.1fMB)" % (dt_ms, wal0, wal1), conn)

    def _check_guards(self) -> None:
        """디스크 여유/DB 크기 가드(주기 점검) — 부족하면 깨지기 전에 자동 일시정지."""
        if self.min_free_bytes and not self.stop_event.is_set():
            try:
                v = os.statvfs(os.path.dirname(self.db_path) or ".")
                free = v.f_bavail * v.f_frsize
                if free < self.min_free_bytes:
                    self._stop_reason = (
                        "디스크 공간 부족(여유 %d < 기준 %d) — 자동 일시정지"
                        % (free, self.min_free_bytes))
                    self.stop_event.set()
            except OSError:
                pass
        if self.db_max_bytes and not self.stop_event.is_set():
            try:
                sz = os.path.getsize(self.db_path)
                wal = self.db_path + "-wal"
                if os.path.exists(wal):
                    sz += os.path.getsize(wal)
                if sz > self.db_max_bytes:
                    self._stop_reason = (
                        "DB 크기 초과(%d > 기준 %d) — 자동 일시정지(깊이 접기 권장)"
                        % (sz, self.db_max_bytes))
                    self.stop_event.set()
            except OSError:
                pass

    def diag_snapshot(self) -> dict:
        """진단용 실측 스냅샷: 락 대기/보유, 체크포인트, 마지막 전진 시각, 청크 나눔 상태."""
        lk = self._dlock
        q = self._chunk_q
        return {
            "lock": lk.snapshot() if isinstance(lk, _TimedLock) else {},
            "checkpoint": dict(self._ckpt),
            "advanced_at": self._advanced_at,
            "idle_helpers": self._idle_workers, "helping": self._helping,
            "chunk_queue": (q.qsize() if q is not None else 0),
            "stat_us_ema": round(self._stat_us_ema, 1),
            "sharing": bool(self._stat_us_ema >= SHARE_MIN_STAT_US),
            "worker_errors": list(self._worker_exc),
            "lock_waiting_idents": (set(lk.waiting) if isinstance(lk, _TimedLock) else set()),
        }

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
            try:   # 구버전 DB(컬럼 없음)에서도 진행 flush 가 실패하지 않게
                _cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_runs)")}
                self._adv_col = "advanced_at" in _cols
                self._pagg_col = "partial_agg_at" in _cols
            except Exception:  # noqa: BLE001
                self._adv_col = False
                self._pagg_col = False

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
            # 이번 세션(재시작) 시작 시각 기록 + 누적 시간 마크 초기화.
            # (재개면 _load_progress 가 이미 elapsed_accum 을 복원했고, 갭은 안 센다)
            self._elapsed_mark = time.time()
            if not self._stats_ref:
                self._stats_ref = self._elapsed_mark   # 파일 나이 계산 기준 시각
            dbmod.update_run(
                conn, self.run_id,
                fs_total_bytes=total, fs_used_bytes=used, fs_free_bytes=free,
                workers=self.workers,   # 설정된 동시 스캔 스레드 수(병렬도)
                session_started_at=self._elapsed_mark,
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
            # 탐색 완료 → 하드링크 기록은 더는 필요 없다(집계는 파일을 세지 않음) — 디스크 회수
            try:
                conn.execute("DELETE FROM seen_inodes WHERE run_id=?", (self.run_id,))
                conn.commit()
            except Exception:  # noqa: BLE001
                pass

            self._aggregate(conn)
            if not self._stopped() and self.fold_depth:
                self._prune_fold_overflow(conn)
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
                 -- claimed 는 _discover 가 pending 으로 되돌려 다시 세므로 제외(재개 때 이중 계수 방지)
                 COALESCE(SUM(CASE WHEN status NOT IN ('pending','claimed') THEN 1 ELSE 0 END),0) AS disc,
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
        # 이전 세션들의 누적 활성 시간 복원(이번 세션은 그 위에 더해진다)
        r2 = conn.execute(
            "SELECT elapsed_accum, max_depth FROM scan_runs WHERE id=?", (self.run_id,)).fetchone()
        self._elapsed_accum = float(r2["elapsed_accum"] or 0) if r2 else 0.0
        # 이미 기록된 최대 깊이를 복원 — 안 하면 재개 직후 얕은 깊이마다 불필요한
        # 'UPDATE scan_runs SET max_depth' 가 단일 _dlock 직렬 구간에서 반복된다(최적화).
        self._seen_max_depth = int((r2["max_depth"] or 0)) if r2 else 0
        # 이미 센 하드링크 inode 복원 — 안 하면 재개 후 같은 inode 의 다른 링크를 또 세어 용량이 부푼다.
        try:
            for r in conn.execute("SELECT dev, ino FROM seen_inodes WHERE run_id=?",
                                  (self.run_id,)):
                self._seen_inodes.add((int(r["dev"]), int(r["ino"])))
        except Exception:  # noqa: BLE001 — 옛 DB(테이블 없음)면 빈 집합으로 시작(예전 동작)
            pass
        self._load_stats(conn)   # 나이/소유자/확장자 집계도 복원

    # ----- 집계 리포트(나이/소유자/확장자) -----
    @staticmethod
    def _stat_bump(d: dict, key: str, eb: int, fcount: int, cap: int = STAT_KEY_CAP) -> None:
        e = d.get(key)
        if e is None:
            if len(d) >= cap:        # distinct 키 폭증 방지 — 초과분은 '(기타)'로
                key = "(기타)"
                e = d.get(key)
            if e is None:
                e = [0, 0]
                d[key] = e
        e[0] += eb
        e[1] += fcount

    def _accum_merge(self, l_age: dict, l_atime: dict, l_uid: dict, l_ext: dict,
                     l_size: dict) -> None:
        """스레드 로컬 집계 dict 들을 공유 집계에 병합(_dlock 안에서 호출, 캡은 여기서 적용)."""
        for k, v in l_age.items():
            self._stat_bump(self._stat_age, k, v[0], v[1])
        for k, v in l_atime.items():
            self._stat_bump(self._stat_atime_age, k, v[0], v[1])
        for k, v in l_uid.items():
            self._stat_bump(self._stat_uid, k, v[0], v[1], cap=50000)
        for k, v in l_ext.items():
            self._stat_bump(self._stat_ext, k, v[0], v[1])
        for k, v in l_size.items():
            self._stat_bump(self._stat_size, k, v[0], v[1])

    def _write_stats(self, conn) -> None:
        """집계를 scan_stats 에 저장(이 run 행 교체). 확장자는 상위 500개만."""
        if self.run_id is None:
            return
        try:
            dbmod.replace_scan_stats(conn, self.run_id, "age",
                                     [(k, v[0], v[1]) for k, v in self._stat_age.items()])
            dbmod.replace_scan_stats(conn, self.run_id, "atime_age",
                                     [(k, v[0], v[1]) for k, v in self._stat_atime_age.items()])
            dbmod.replace_scan_stats(conn, self.run_id, "owner",
                                     [(k, v[0], v[1]) for k, v in self._stat_uid.items()])
            ext_top = sorted(self._stat_ext.items(), key=lambda kv: kv[1][0],
                             reverse=True)[:500]
            dbmod.replace_scan_stats(conn, self.run_id, "ext",
                                     [(k, v[0], v[1]) for k, v in ext_top])
            dbmod.replace_scan_stats(conn, self.run_id, "size",
                                     [(k, v[0], v[1]) for k, v in self._stat_size.items()])
            dbmod.replace_top_files(
                conn, self.run_id,
                [(p, b, m, u, a) for (b, p, m, u, a) in self._top_files])
            conn.commit()
        except Exception:
            pass

    def _load_stats(self, conn) -> None:
        for kind, d in (("age", self._stat_age), ("owner", self._stat_uid),
                        ("ext", self._stat_ext), ("size", self._stat_size),
                        ("atime_age", self._stat_atime_age)):
            try:
                for r in dbmod.get_scan_stats(conn, self.run_id, kind):
                    d[r["key"]] = [int(r["bytes"]), int(r["files"])]
            except Exception:
                pass
        try:   # 최대 파일 힙 복원
            self._top_files = [
                (int(r["bytes"]), r["path"], r["mtime"], r["uid"],
                 float(r["atime"] or 0))
                for r in dbmod.get_top_files(conn, self.run_id)]
            heapq.heapify(self._top_files)
        except Exception:
            pass

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
        now = time.time()
        # 종료 시 마지막 활성 시간 확정 누적
        if self._elapsed_mark:
            self._elapsed_accum += max(0.0, now - self._elapsed_mark)
        self._elapsed_mark = now
        fields = dict(
            status=status, phase=status,
            finished_at=now,
            discovered_dirs=self._discovered,
            total_dirs=self._discovered,
        )
        if status == "done" and self._pagg_col:
            fields["partial_agg_at"] = None   # 완전 집계가 끝났으니 '부분 집계 값' 표시를 지운다
        fields.update(
            processed_dirs=self._processed,
            scanned_bytes=self._scanned_bytes,
            total_files=self._total_files,
            error_dirs=self._error_dirs,
            elapsed_accum=self._elapsed_accum,
            current_dir=None,
        )
        if self._stop_reason:   # 디스크 부족 등으로 자동 정지된 경우 사유 기록
            fields["error"] = self._stop_reason
        dbmod.update_run(conn, self.run_id, **fields)
        conn.commit()
        self._write_stats(conn)   # 집계 리포트 최종 저장
        self._log("종료: status=%s, 처리 %s개, 오류 %s개, 활성 %.0f초, WAL %.1fMB"
                  % (status, f"{self._processed:,}", f"{self._error_dirs:,}",
                     self._elapsed_accum, self._wal_mb()), conn)
        try:
            dbmod.prune_scan_log(conn, self.run_id)
            conn.commit()
        except Exception:
            pass
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
        self._status = "discovering"; self._phase = "discovering"
        dbmod.update_run(conn, self.run_id, status="discovering", phase="discovering")
        # 이전 실행에서 'claimed' 로 남은 것은 다시 pending 으로(재개 안전)
        conn.execute("UPDATE directories SET status='pending' "
                     "WHERE run_id=? AND status='claimed'", (self.run_id,))
        conn.commit()
        self._update_manager(force=True)   # 매니저 DB 도 즉시 '진행 중'으로(재개 직후 'paused' 잔류 방지)

        self._disc_conn = dbmod.connect(self.db_path)
        self._disc_active = 0
        self._worker_exc = []
        self._abort = False
        n = max(1, int(self.workers))
        self._worker_dirs = {i: None for i in range(n)}   # 워커별 현재 디렉터리
        # 청크 작업 나눔 큐(워커 2개 이상일 때) — 메모리 제한: 워커 수×2 청크(청크=최대 STAT_CHUNK 항목)
        self._chunk_q = queue.Queue(maxsize=max(2, 2 * n)) if n > 1 else None
        self._idle_workers = 0
        self._helping = 0
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
            self._chunk_q = None

        # 워커 예외는 '완료'로 덮지 않는다 — run() 이 status=error 로 기록하고 예외를 올린다.
        if self._worker_exc:
            raise RuntimeError("탐색 워커 예외: " + "; ".join(self._worker_exc[:3]))
        if not self._stopped():
            left = conn.execute(
                "SELECT COUNT(*) FROM directories WHERE run_id=? AND status IN ('pending','claimed')",
                (self.run_id,)).fetchone()[0]
            if left:
                raise RuntimeError("탐색이 끝났는데 미완료 디렉터리 %d개가 남음(내부 오류)" % int(left))

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
                rows = self._fair_share(rows)
                ids = [r["id"] for r in rows]
                ph = ",".join("?" * len(ids))
                self._disc_conn.execute(
                    "UPDATE directories SET status='claimed' WHERE id IN (%s)" % ph, ids)
                self._disc_conn.commit()
                self._disc_active += 1
                return rows
            # pending 없음: 아무도 작업 중이 아니면 끝, 아니면 잠시 대기
            return None if self._disc_active == 0 else []

    def _fair_share(self, rows):
        """한 번의 claim 에서 이 워커가 가져갈 몫 = 보이는 대기 목록(최대 batch_size)의 1/워커수.

        한 워커가 목록을 통째로 선점해 나머지 워커가 노는 것을 막는다(재현: fold 루트 40개를 8워커 중
        1개가 전부 처리). 항상 (n-1)/n 을 남기므로 목록이 작든 크든 워커 전원이 나눠 갖는다.

        (1.99.30 의 'fold 깊이 이상은 1개씩' 규칙은 뺐다: 깊이 4 디렉터리가 수만~수백만인 일반 NAS 에서
         claim 횟수가 수백 배로 늘고 claim 은 _dlock 안에서 직렬이라 스캔이 크게 느려졌다 — 측정: 1.6만
         디렉터리 트리 2.7초 → 26.4초. 무거운 행 하나의 비용은 fold 예산이 이미 묶는다.)
        """
        n = max(1, int(self.workers))
        return rows[:max(1, -(-len(rows) // n))]

    def _discover_worker(self, widx: int = 0) -> None:
        try:
            while not self._stopped():
                batch = self._claim_batch()
                if batch is None:
                    break
                if not batch:
                    # 지금은 맡을 디렉터리가 없다(다른 워커 작업 중) → 그 워커의 파일 청크를 돕는다
                    self._help_chunks(widx, 0.02)
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
        except Exception as exc:  # noqa: BLE001 — 워커 예외를 삼키면 미완료 행이 '완료'로 둔갑한다
            msg = "disc-%d: %s: %s" % (widx, type(exc).__name__, exc)
            with self._dlock:
                self._worker_exc.append(msg)
                self._abort = True        # 다른 워커도 멈춘다(_stopped) — 부분 결과는 완료 처리 안 됨
            self._log("워커 예외로 탐색 중단: " + msg)

    def _discover_one(self, dir_id: int, path: str, depth: int) -> None:
        """디렉터리 하나를 훑는다(병렬 워커가 호출).

        scandir/stat(느린 I/O)은 락 밖에서 하고, DB 쓰기와 공유 카운터/하드링크 집합은 _dlock
        안에서만 건드린다(데드락 없이 병렬 처리). 파일별 통계·Top-N 은 디렉터리 누적(_DirAcc)에
        모았다가 완료 확정 시 한 번에 병합한다 — 중단으로 버려지면 통계도 버려져 재개 중복이 없다.
        파일 청크는 유휴 워커가 있으면 그쪽에 넘겨(작업 나눔) 거대 평면 폴더도 병렬로 stat 된다.
        """
        acc = _DirAcc()
        subdir_count = 0
        children: List[tuple] = []
        err: Optional[str] = None
        file_chunk: list = []
        fold_dir_bytes = 0
        # fold 넘김 행(깊이 > fold_depth) = fold walk 의 연속. fold 는 max_depth 와 무관하게 끝까지 세므로
        # 이 행들엔 max_depth 컷을 적용하지 않는다(예전 인라인 fold 와 같은 결과).
        over_fold = bool(self.fold_depth) and depth > self.fold_depth
        # 이 디렉터리 한 번의 처리에서 인라인 fold 로 걸을 예산(넘치면 남은 하위를 행으로 넘김)
        fold_left = int(self.fold_budget)
        fold_deadline = time.time() + float(self.fold_budget_secs)

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
                        # 최대 깊이 제한(옵션): 그 아래는 탐색/저장하지 않아 DB 크기를 묶는다.
                        # (fold 넘김 행은 fold walk 의 연속이라 제외 — 위 over_fold 참고)
                        if self.max_depth and (depth + 1) > self.max_depth and not over_fold:
                            continue
                        # 깊이 접기(옵션): 깊이 N 초과 디렉터리는 행을 만들지 않고,
                        # 하위 전체 용량을 이 디렉터리 own_bytes 에 합산한다.
                        # → DB 행 수(=크기)는 묶이고 상위 합계는 정확하다(상세만 N까지).
                        # 단 인라인으로 걷는 양은 예산으로 묶는다: 예산 안이면 접어서 세고, 넘치면
                        # 남은 하위(미방문 디렉터리)를 프론티어 행으로 넘겨 다른 워커가 병렬로 이어받는다
                        # (체크포인트 — 중단돼도 그 조각만 다시). 넘긴 행은 집계 후 정리된다.
                        if self.fold_depth and (depth + 1) > self.fold_depth:
                            if fold_left > 0 and time.time() < fold_deadline:
                                fdb, over, used = self._fold_subtree(
                                    entry.path, fold_left, fold_deadline, acc)
                                fold_left -= used
                                fold_dir_bytes += fdb
                                for op in over:
                                    children.append((self.run_id, dir_id, op,
                                                     os.path.basename(op) or op, depth + 1))
                            else:
                                children.append(
                                    (self.run_id, dir_id, entry.path, entry.name, depth + 1))
                            continue
                        children.append(
                            (self.run_id, dir_id, entry.path, entry.name, depth + 1))
                    else:
                        file_chunk.append(entry)
                        if len(file_chunk) >= STAT_CHUNK:
                            chunk, file_chunk = file_chunk, []
                            self._dispatch_chunk(acc, chunk, path)
            if file_chunk:
                chunk, file_chunk = file_chunk, []
                self._dispatch_chunk(acc, chunk, path)
        except OSError as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            with self._dlock:
                self._error_dirs += 1

        # 다른 워커에게 넘긴 청크가 끝날 때까지(그동안 큐를 직접 돕는다 — 교착 없음)
        self._wait_chunks(acc)
        # 디렉터리 자기 inode 분(du 와 동일). 파일 분은 청크 처리에서 이미 acc 에 반영됨.
        dstat = self._safe_stat_path(path)                # I/O — 락 밖
        dir_bytes = _entry_bytes(dstat, self.size_mode) if dstat is not None else 0

        with self._dlock:
            # 중단(stop_event/워커 예외)됐으면 이 디렉터리를 완료('discovered')로 확정하지 않는다.
            # scandir 루프가 중간에 break 됐을 수 있어(아직 못 본 파일/하위 존재) 부분값으로 완료
            # 처리하면 재개 시 이 디렉터리를 다시 안 훑어 그 파일/서브트리가 영구 누락된다.
            # 'claimed' 상태로 남기면 재개 시 pending 으로 복원돼 처음부터 다시 훑는다(kill -9 안전성과 동일).
            # 누적분(acc)도 같이 버린다 — 재개 후 다시 훑을 때 통계가 중복되지 않는다.
            if self._stopped():
                acc.abandoned = True
                return
            own_bytes = acc.bytes + fold_dir_bytes + dir_bytes
            file_count = acc.files
            self._scanned_bytes += dir_bytes + fold_dir_bytes
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
            if acc.new_inodes:   # 완료 확정과 같은 트랜잭션 — 재개 후에도 하드링크 1회 계산 유지
                self._disc_conn.executemany(
                    "INSERT OR IGNORE INTO seen_inodes (run_id, dev, ino) VALUES (?, ?, ?)",
                    [(self.run_id, dv, ino) for dv, ino in acc.new_inodes])
            # 파일별 통계/Top-N 은 완료 확정과 함께 공유 집계로(같은 커밋에 실린다)
            _top_merge_into(self._top_files, acc.top)
            self._accum_merge(acc.age, acc.atime, acc.uid, acc.ext, acc.size)
            # 커밋 배칭: 디렉터리마다 fsync 하던 것을 제거하고, 아래 진행 flush(최대
            # 0.4s 스로틀)에서 한 번에 커밋한다. 같은 커넥션이라 미커밋 쓰기도 다른 워커의
            # claim 쿼리엔 보인다(정합성 유지). 종료 시 _discover 의 마지막 커밋이 꼬리를
            # 비우고, kill -9 면 claimed→pending 으로 되돌아 재스캔되어 안전하다.
            self._discovered += 1
            self._advanced_at = time.time()
            if depth > 0 and not over_fold:   # 넘김 행의 깊이는 논리값이라 최대 깊이 표시엔 반영 안 함
                self._maybe_update_depth(self._disc_conn, depth)
            self._flush_progress_locked(self._disc_conn,
                                        current_dir=path, current_depth=depth)

    def _fold_subtree(self, root: str, budget: int = 0, deadline: float = 0.0,
                      acc: Optional[_DirAcc] = None):
        """fold_depth 초과 하위 트리를 '행 없이' walk 하여 (dir_bytes, overflow, used) 를 반환.

        디렉터리 행을 만들지 않으므로 DB 가 커지지 않으면서, 용량/개수는 모두 세어 상위(깊이 N)
        own_bytes 에 합산된다 → 합계가 정확하다. 파일 청크는 호출 디렉터리의 누적(acc)으로
        처리되고(stat 은 락 밖, 유휴 워커가 대신 처리 가능), 여기선 디렉터리 자기 inode 분만 더한다.

        budget(파일+디렉터리 수)·deadline(시각)을 넘기면 디렉터리 경계에서 멈추고, 아직 안 훑은
        하위 디렉터리 경로 목록(overflow)을 돌려준다 — 호출부가 이를 프론티어 행으로 넘겨 다른
        워커가 병렬로 이어받는다(체크포인트). 각 디렉터리는 정확히 한 번만 세어진다: 훑은 쪽은 여기서,
        넘긴 쪽은 행으로 처리될 때. budget<=0 이면 제한 없음. used 는 소비한 예산(최소 1 — 루트는 항상 처리).
        예산은 디렉터리 경계에서만 적용되므로 거대 평면 디렉터리 하나는 끝까지 한 번에 훑는다
        (NFS 디렉터리 열거 위치는 재시작 후 유효하지 않아 중간 저장이 불가능하다).
        """
        if acc is None:
            acc = _DirAcc()
        dir_bytes = 0
        used = 0
        stack = [root]
        while stack:
            if self._stopped():
                break
            # 예산 초과 → 남은(미방문) 디렉터리를 넘긴다. 루트는 항상 먼저 처리(진행 보장).
            if used > 0 and ((budget > 0 and used >= budget)
                             or (deadline and time.time() >= deadline)):
                return dir_bytes, stack, used
            d = stack.pop()
            used += 1
            ds = self._safe_stat_path(d)            # 디렉터리 자기 inode 분(du 동일)
            if ds is not None:
                dir_bytes += _entry_bytes(ds, self.size_mode)
            files_chunk: list = []
            try:
                with os.scandir(d) as it:           # I/O — 락 밖
                    for entry in it:
                        if self._stopped():
                            break
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        if is_dir:
                            if self.one_file_system and self._root_dev is not None:
                                try:
                                    if entry.stat(follow_symlinks=False).st_dev != self._root_dev:
                                        continue
                                except OSError:
                                    continue
                            stack.append(entry.path)
                        else:
                            files_chunk.append(entry)
                            if len(files_chunk) >= STAT_CHUNK:
                                used += len(files_chunk)
                                self._dispatch_chunk(acc, files_chunk, d)
                                files_chunk = []
                if files_chunk:
                    used += len(files_chunk)
                    self._dispatch_chunk(acc, files_chunk, d)
            except OSError:
                with self._dlock:
                    self._error_dirs += 1
        return dir_bytes, [], used

    # ----- 파일 청크 처리(공통) + 작업 나눔 -----
    def _dispatch_chunk(self, acc: _DirAcc, chunk: list, cur_dir: str) -> None:
        """파일 청크를 유휴 워커가 있으면 큐에 넘기고, 없거나 큐가 차면 직접 처리한다."""
        q = self._chunk_q
        if (q is not None and self._idle_workers > 0 and not acc.abandoned
                and self._stat_us_ema >= SHARE_MIN_STAT_US):
            with self._q_cv:
                acc.pending += 1
            try:
                q.put_nowait((acc, chunk, cur_dir))
                return
            except queue.Full:
                with self._q_cv:
                    acc.pending -= 1
        self._stat_chunk(acc, chunk, cur_dir)

    def _process_chunk(self, acc: _DirAcc, chunk: list, cur_dir: str) -> None:
        """큐에서 받은 청크 처리(도우미/소유자 공통). 버려진 디렉터리의 청크는 건너뛴다."""
        try:
            if not acc.abandoned:
                self._stat_chunk(acc, chunk, cur_dir)
        finally:
            with self._q_cv:
                acc.pending -= 1
                self._q_cv.notify_all()

    def _wait_chunks(self, acc: _DirAcc) -> None:
        """이 디렉터리의 청크 중 다른 워커가 처리 중인 것이 끝날 때까지 — 기다리는 동안 큐를 직접
        돕는다(도우미가 사라져도 교착 없음). 중단이면 기다리지 않는다(acc 는 버려진다)."""
        q = self._chunk_q
        if q is None:
            return
        while not self._stopped():
            with self._q_cv:
                if acc.pending <= 0:
                    return
            try:
                job = q.get_nowait()
            except queue.Empty:
                with self._q_cv:
                    if acc.pending > 0:
                        self._q_cv.wait(0.05)
                continue
            self._process_chunk(*job)

    def _help_chunks(self, widx: int, timeout: float) -> None:
        """맡을 디렉터리가 없는 워커가 다른 워커의 파일 청크를 대신 처리한다(없으면 잠시 대기)."""
        q = self._chunk_q
        if q is None:
            self.stop_event.wait(timeout)
            return
        with self._q_cv:
            self._idle_workers += 1
        try:
            try:
                job = q.get(timeout=timeout)
            except queue.Empty:
                return
            with self._q_cv:
                self._helping += 1
            try:
                self._worker_dirs[widx] = job[2]
                self._process_chunk(*job)
            finally:
                self._worker_dirs[widx] = None
                with self._q_cv:
                    self._helping -= 1
        finally:
            with self._q_cv:
                self._idle_workers -= 1

    def _stat_chunk(self, acc: _DirAcc, entries: list, cur_dir: str) -> Tuple[int, int]:
        """파일 청크를 stat(락 밖)하고 파일별 집계를 디렉터리 누적(acc)에 더한다. 반환 (bytes, files).

        - stat·나이/확장자/크기/Top-N 계산: 락 밖(스레드 로컬) — 락 안에서 파일별 작업을 하지 않는다.
        - 하드링크(nlink>1) 판정: 공유 집합이라 락 안. 용량은 한 번만, 개수는 셈.
        - acc 병합·공유 진행 카운터·진행 flush: 락 안, 짧게(청크당 dict 몇십 개).
        (락 축소는 GIL 을 없애지 않는다 — 효과는 워커가 I/O 대기에 있을 때 나타난다.)
        """
        t0 = time.monotonic()
        stats = [self._safe_stat(e) for e in entries]   # I/O — 락 밖
        if entries:   # 파일당 stat 소요(EMA) — 청크 나눔을 I/O 대기형에서만 켜기 위한 측정
            us = (time.monotonic() - t0) * 1e6 / len(entries)
            self._stat_us_ema = us if not self._stat_us_ema else (0.7 * self._stat_us_ema + 0.3 * us)
        ref = self._stats_ref
        l_bytes = 0
        l_files = 0
        l_top: list = []
        l_age: dict = {}
        l_atime: dict = {}
        l_uid: dict = {}
        l_ext: dict = {}
        l_size: dict = {}
        deferred: list = []   # nlink>1: dedup 이 공유라 락 안에서 판정
        for entry, st in zip(entries, stats):     # 락 밖 — 파일당 작업
            if st is None:
                continue
            l_files += 1
            eb = _entry_bytes(st, self.size_mode)
            if self.hardlink_dedup and st.st_nlink > 1:
                deferred.append((entry.path, entry.name, eb, st))
                continue
            l_bytes += eb
            _local_top(l_top, entry.path, eb, st)
            _local_accum(ref, l_age, l_atime, l_uid, l_ext, l_size, entry.name, st, eb)
        with self._dlock:                       # 락 안 — 짧게 병합
            for p, name, eb, st in deferred:    # 하드링크 dedup(드묾)
                key = (st.st_dev, st.st_ino)
                if key in self._seen_inodes:
                    cb = 0                       # 용량은 한 번만(개수는 셈)
                else:
                    self._seen_inodes.add(key)
                    acc.new_inodes.append(key)
                    cb = eb
                if cb:
                    l_bytes += cb
                    _local_top(l_top, p, cb, st)
                _local_accum(ref, l_age, l_atime, l_uid, l_ext, l_size, name, st, eb, cb)
            _top_merge_into(acc.top, l_top)
            _merge_counts(acc.age, l_age)
            _merge_counts(acc.atime, l_atime)
            _merge_counts(acc.uid, l_uid)
            _merge_counts(acc.ext, l_ext)
            _merge_counts(acc.size, l_size)
            acc.bytes += l_bytes
            acc.files += l_files
            self._scanned_bytes += l_bytes
            self._total_files += l_files
            if l_files:
                self._advanced_at = time.time()
            self._flush_progress_locked(self._disc_conn, current_dir=cur_dir)
        return l_bytes, l_files

    def _prune_fold_overflow(self, conn) -> None:
        """집계를 다 마친 뒤 fold 넘김 행(깊이 > fold_depth)을 지운다.

        넘김 행의 용량은 집계에서 이미 fold 루트(깊이 N)의 total 에 합산됐으므로, 지워도 합계는
        그대로다. 결과 DB 는 순수 접기와 같은 모양(상세 행은 fold_depth 까지)이 되고, 트리 화면에
        깊은 경로가 fold 루트의 '직속 자식'처럼 보이는 혼동도 없다. 중단(미완료)이면 호출되지 않는다.
        """
        try:
            cur = conn.execute("DELETE FROM directories WHERE run_id=? AND depth>?",
                               (self.run_id, self.fold_depth))
            n = cur.rowcount or 0
            conn.commit()
            if n:
                self._log("fold 넘김 행 %s개 정리(용량은 fold 루트에 합산됨)" % f"{n:,}", conn)
        except Exception as exc:  # noqa: BLE001 — 정리 실패는 결과(합계)에 영향 없음
            self._log("fold 넘김 행 정리 실패(합계는 정상): %s" % exc, conn)

    def _maybe_update_depth(self, conn, depth: int) -> None:
        # 새 최대 깊이일 때만 DB 를 갱신한다(단일 _dlock 직렬 구간의 불필요한 쓰기 제거).
        if depth > getattr(self, "_seen_max_depth", 0):
            self._seen_max_depth = depth
            conn.execute(
                "UPDATE scan_runs SET max_depth=MAX(max_depth, ?) WHERE id=?",
                (depth, self.run_id),
            )

    # --------------------------------------------------------- 2단계: 상향식 집계
    def _aggregate(self, conn) -> None:
        """가장 깊은 레벨부터 0 까지, 레벨별로 재귀 용량을 집계한다.

        native 백엔드는 레벨을 id 윈도우로 잘라 SQL 일괄 UPDATE 로 처리한다 —
        디렉터리마다 파이썬에서 SELECT+UPDATE 를 돌리던 행 루프가 집계 단계의
        병목(1코어 포화·GIL)이었다(43만 행 재현 벤치 27.9k → 158.4k 행/초).
        SQL 실행 중엔 GIL 이 풀려 대시보드 응답도 같이 좋아진다.
        du 백엔드만 디렉터리별 du 실행이 필요해 행 루프를 유지한다.

        예전 구현이 레벨 전체를 여는 read_conn 커서를 유지해 30초 주기
        wal_checkpoint(TRUNCATE) 를 막았고 WAL 이 DB 의 몇 배(622MB 사례)로
        부풀었다 — 지금은 긴 읽기 스냅샷 자체가 없다.
        """
        self._status = "sizing"; self._phase = "sizing"
        dbmod.update_run(conn, self.run_id, status="sizing", phase="sizing")
        conn.commit()
        self._update_manager(force=True)   # 매니저 DB 도 '집계 중'으로 즉시 반영(self._status 동기화)

        row = conn.execute(
            "SELECT COALESCE(MAX(depth),0) AS d, "
            "       COALESCE(SUM(CASE WHEN status!='done' THEN 1 ELSE 0 END),0) AS remain "
            "FROM directories WHERE run_id=?",
            (self.run_id,),
        ).fetchone()
        max_depth = int(row["d"])

        du_path = shutil.which("du") if self.backend == "du" else None
        if self.backend == "du" and not du_path:
            # du 가 없으면 native 로 폴백
            self.backend = "native"

        # 집계 튜닝(이 연결에만 적용): 페이지 캐시 상향 + 자동 체크포인트 간격 상향.
        # 기본 자동 체크포인트(1000페이지)는 윈도우 커밋마다 백필을 돌려 되레 느리다.
        try:
            conn.execute("PRAGMA cache_size=-%d" % AGG_CACHE_KB)
            conn.execute("PRAGMA wal_autocheckpoint=%d" % AGG_AUTOCKPT_PAGES)
        except Exception:
            pass

        self._agg_mark = None
        self._agg_rate = 0.0
        self._log("집계 시작: 대상 %s개(최대 깊이 %d), 백엔드 %s, WAL %.1fMB"
                  % (f"{int(row['remain']):,}", max_depth,
                     "du" if du_path else "native", self._wal_mb()), conn)
        try:
            if du_path is not None:
                self._aggregate_du(conn, max_depth, du_path)
            else:
                self._aggregate_native(conn, max_depth)
            self._flush_progress(conn, force=True, current_dir=None)
        finally:
            if self.monitor is not None:
                self.monitor.set_du_pid(None)

    def _aggregate_native(self, conn, max_depth: int) -> None:
        """레벨(깊이)별 일괄 UPDATE — 자식 합산을 전부 SQLite C 코드에서 수행.

        윈도우(id 범위)마다 커밋하므로 하트비트가 살아 있고, 일시정지·강제종료
        후에도 status='done' 기준으로 이어서 재개된다(기존과 동일한 재개 규약).
        """
        # (run_id, depth, rowid) 인덱스 범위 탐색을 강제 — 플래너가 다른 인덱스를
        # 고르면 윈도우마다 레벨 전체를 훑게 된다. 인덱스가 없는 비정상 DB 에서만
        # 힌트 없는 동일 쿼리로 폴백한다.
        sql = """UPDATE directories INDEXED BY idx_dir_run_depth
                 SET total_bytes = own_bytes + COALESCE((
                         SELECT SUM(c.total_bytes) FROM directories c
                         WHERE c.run_id=directories.run_id
                           AND c.parent_id=directories.id), 0),
                     total_files = file_count + COALESCE((
                         SELECT SUM(c.total_files) FROM directories c
                         WHERE c.run_id=directories.run_id
                           AND c.parent_id=directories.id), 0),
                     status='done', scanned_at=?
                 WHERE run_id=? AND depth=? AND status!='done'
                   AND id>=? AND id<?"""
        for depth in range(max_depth, -1, -1):
            if self._stopped():
                break
            bound = conn.execute(
                "SELECT MIN(id) AS lo, MAX(id) AS hi FROM directories "
                "WHERE run_id=? AND depth=? AND status!='done'",
                (self.run_id, depth)).fetchone()
            if bound["lo"] is None:
                continue   # 이 레벨은 전부 완료(재개) 또는 없음
            lo, hi = int(bound["lo"]), int(bound["hi"])
            t_level = time.time()
            n_level = 0
            w = lo
            while w <= hi:
                if self._stopped():
                    break
                try:
                    cur = conn.execute(sql, (time.time(), self.run_id, depth,
                                             w, w + AGG_WINDOW))
                except Exception:
                    sql = sql.replace(" INDEXED BY idx_dir_run_depth", "")
                    cur = conn.execute(sql, (time.time(), self.run_id, depth,
                                             w, w + AGG_WINDOW))
                n_level += cur.rowcount
                self._processed += cur.rowcount
                w += AGG_WINDOW
                conn.commit()   # 윈도우 단위 커밋 → 하트비트/일시정지/재개 유지
                self._flush_progress(conn, current_depth=depth)
            el = time.time() - t_level
            self._log("집계 depth %d: %s행 %.1fs (%s행/초), WAL %.1fMB"
                      % (depth, f"{n_level:,}", el,
                         f"{int(n_level / el):,}" if el > 0 else "-",
                         self._wal_mb()), conn)

    def _aggregate_du(self, conn, max_depth: int, du_path: str) -> None:
        """du 백엔드: 디렉터리마다 시스템 du 를 돌려야 해 행 단위 루프를 유지.

        키셋(id>마지막) 페이지네이션으로 배치마다 읽기 스냅샷을 닫는다 — 레벨
        전체를 여는 커서가 WAL 체크포인트를 막던 문제(WAL 부풀림)를 없앤다.
        """
        for depth in range(max_depth, -1, -1):
            if self._stopped():
                break
            t_level = time.time()
            n_level = 0
            last_id = -1
            while True:
                if self._stopped():
                    break
                rows = conn.execute(
                    """SELECT id, path, own_bytes, file_count FROM directories
                       WHERE run_id=? AND depth=? AND status!='done' AND id>?
                       ORDER BY id ASC LIMIT ?""",
                    (self.run_id, depth, last_id, self.batch_size)).fetchall()
                if not rows:
                    break
                for r in rows:
                    if self._stopped():
                        break
                    self._aggregate_one(conn, r, depth, du_path)
                last_id = int(rows[-1]["id"])
                n_level += len(rows)
                conn.commit()
                self._flush_progress(conn)
            el = time.time() - t_level
            if n_level:
                self._log("집계(du) depth %d: %s행 %.1fs (%.1f행/초), WAL %.1fMB"
                          % (depth, f"{n_level:,}", el,
                             (n_level / el) if el > 0 else 0.0, self._wal_mb()), conn)

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
        args.extend(["--", path])   # '--': '-' 로 시작하는 경로를 du 옵션으로 오인하지 않도록
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


def partial_aggregate(db_path: str, run_id: Optional[int] = None, *, stop_event=None,
                      on_progress=None, window: int = AGG_WINDOW,
                      manager_db: Optional[str] = None, manager_scan_id: Optional[int] = None) -> dict:
    """'멈춘 자료 집계' — 일시정지/오류로 멈춘 스캔의 '지금까지 발견된 행'으로 재귀 합계(하한값)를 계산한다.

    total_bytes/total_files 만 쓰고 행 status 는 바꾸지 않는다: 재개하면 탐색이 이어지고 완전 집계가
    같은 행을 다시 계산해 덮어쓴다. 아직 안 훑은(pending/claimed) 행은 0 으로 들어가므로 결과는
    **하한값**이다. 완료 시 scan_runs.partial_agg_at(+관리 DB)에 시각을 기록해 화면이 '부분 집계 값'임을
    표시하고, 완전 집계(status=done)가 끝나면 스캐너가 지운다. fold 넘김 행은 정리하지 않는다(재개에 필요).
    비용은 행 수에 비례한다(레벨별 일괄 UPDATE, 윈도우마다 커밋 → 중단 가능).
    반환: {ok, run_id, rows, total_rows, max_depth, elapsed, rows_per_sec, partial_agg_at} 또는 {ok: False, reason}.
    """
    t0 = time.time()
    conn = dbmod.connect(db_path)
    try:
        dbmod.ensure_column(conn, "scan_runs", "partial_agg_at", "REAL")
        if run_id is None:
            run_id = dbmod.latest_run_id(conn)
        if run_id is None:
            return {"ok": False, "reason": "스캔 run 이 없습니다."}
        run = dbmod.get_run(conn, run_id)
        if run is None:
            return {"ok": False, "reason": "없는 run 입니다."}
        if run["status"] in ("discovering", "sizing"):
            return {"ok": False, "reason": "실행 중인 스캔입니다(먼저 중지하세요)."}
        phase = run["phase"] or run["status"] or "paused"
        try:   # 집계 튜닝(이 연결에만): 페이지 캐시 상향 + 자동 체크포인트 간격 상향(완전 집계와 동일)
            conn.execute("PRAGMA cache_size=-%d" % AGG_CACHE_KB)
            conn.execute("PRAGMA wal_autocheckpoint=%d" % AGG_AUTOCKPT_PAGES)
        except Exception:  # noqa: BLE001
            pass
        row = conn.execute("SELECT COALESCE(MAX(depth),0) AS d, COUNT(*) AS n FROM directories WHERE run_id=?",
                           (run_id,)).fetchone()
        max_depth = int(row["d"])
        total_rows = int(row["n"])
        # status 변경 없음 — 그 외는 _aggregate_native 와 같은 레벨별 일괄 UPDATE.
        # 'done' 행(완전 집계·du 로 확정된 값)은 건드리지 않는다 — 재개 후 완전 집계도 done 은
        # 다시 계산하지 않으므로, 덮어쓰면 하한값이 확정값처럼 굳는다.
        sql = """UPDATE directories INDEXED BY idx_dir_run_depth
                 SET total_bytes = own_bytes + COALESCE((
                         SELECT SUM(c.total_bytes) FROM directories c
                         WHERE c.run_id=directories.run_id AND c.parent_id=directories.id), 0),
                     total_files = file_count + COALESCE((
                         SELECT SUM(c.total_files) FROM directories c
                         WHERE c.run_id=directories.run_id AND c.parent_id=directories.id), 0)
                 WHERE run_id=? AND depth=? AND id>=? AND id<? AND status!='done'"""
        done_rows = 0
        stopped = False

        def _stopped():
            return stop_event is not None and stop_event.is_set()
        for depth in range(max_depth, -1, -1):
            if _stopped():
                stopped = True
                break
            bound = conn.execute("SELECT MIN(id) AS lo, MAX(id) AS hi FROM directories "
                                 "WHERE run_id=? AND depth=?", (run_id, depth)).fetchone()
            if bound["lo"] is None:
                continue
            w, hi = int(bound["lo"]), int(bound["hi"])
            while w <= hi:
                if _stopped():
                    stopped = True
                    break
                try:
                    cur = conn.execute(sql, (run_id, depth, w, w + int(window)))
                except Exception:  # noqa: BLE001 — 인덱스 없는 비정상 DB 는 힌트 없이 폴백
                    sql = sql.replace(" INDEXED BY idx_dir_run_depth", "")
                    cur = conn.execute(sql, (run_id, depth, w, w + int(window)))
                done_rows += cur.rowcount
                w += int(window)
                conn.commit()   # 윈도우마다 커밋 → 중단해도 DB 는 일관(값만 부분 갱신)
                if on_progress is not None:
                    try:
                        on_progress({"depth": depth, "rows": done_rows, "total_rows": total_rows,
                                     "elapsed": time.time() - t0})
                    except Exception:  # noqa: BLE001
                        pass
            if stopped:
                break
        el = time.time() - t0
        if stopped:
            try:
                dbmod.add_scan_log(conn, run_id, phase,
                                   "부분 집계 중단: %s행 %.1fs — 값은 일부만 갱신됨(다시 실행하거나 재개)"
                                   % (f"{done_rows:,}", el))
                conn.commit()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "reason": "중단됨", "stopped": True, "rows": done_rows,
                    "total_rows": total_rows, "elapsed": el}
        now = time.time()
        dbmod.update_run(conn, run_id, partial_agg_at=now)
        try:
            dbmod.add_scan_log(conn, run_id, phase,
                               "부분 집계(멈춘 자료): %s행 %.1fs (%s행/초) — 하한값, 재개하면 완전 집계가 다시 계산"
                               % (f"{done_rows:,}", el, f"{int(done_rows / el):,}" if el > 0 else "-"))
        except Exception:  # noqa: BLE001
            pass
        conn.commit()
    finally:
        conn.close()
    if manager_db and manager_scan_id:
        try:   # 관리 DB(스캔 목록) 에도 표시 — 컬럼은 init_manager 가 보강
            from . import manager as mgr
            mc = dbmod.connect(manager_db)
            try:
                mgr.update_scan(mc, manager_scan_id, partial_agg_at=now)
                mc.commit()
            finally:
                mc.close()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "run_id": run_id, "rows": done_rows, "total_rows": total_rows,
            "max_depth": max_depth, "elapsed": el,
            "rows_per_sec": (done_rows / el) if el > 0 else 0.0, "partial_agg_at": now}


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
    max_depth: int = 0,
    fold_depth: int = 0,
    db_max_bytes: int = 0,
    hardlink_dedup: bool = True,
    min_free_bytes: int = 0,
    sample_interval: float = 2.0,
    stop_event: Optional[threading.Event] = None,
    with_monitor: bool = True,
    manager_db: Optional[str] = None,
    manager_scan_id: Optional[int] = None,
    on_scanner=None,
) -> int:
    """편의 함수: DB 초기화 → 모니터 시작 → 스캔(탐색+집계) 실행 → 모니터 정리.

    on_scanner(scanner) 를 주면 생성된 Scanner 인스턴스를 넘긴다(서버가 진단 스냅샷을 읽기 위함).

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
        max_depth=max_depth, fold_depth=fold_depth, db_max_bytes=db_max_bytes,
        hardlink_dedup=hardlink_dedup, min_free_bytes=min_free_bytes,
        stop_event=stop_event, monitor=monitor,
        manager_db=manager_db, manager_scan_id=manager_scan_id,
    )

    if on_scanner is not None:
        try:
            on_scanner(scanner)
        except Exception:  # noqa: BLE001 — 진단 훅 실패가 스캔을 막지 않는다
            pass
    if monitor is not None:
        monitor.start()
    try:
        return scanner.run()  # 내부에서 run 생성 후 monitor.run_id 를 설정한다.
    finally:
        if monitor is not None:
            monitor.stop_event.set()
            monitor.join(timeout=5)

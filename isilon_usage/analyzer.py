"""대상 경로의 디렉터리 '깊이 구조'만 빠르게 측정한다(파일 stat·DB 없이).

풀스캔 전에 **"폴더가 몇 단계까지 있고, 각 깊이에 디렉터리가 몇 개"** 인지를 재서
적절한 `fold-depth` 를 고르도록 돕는다("대상 분석" 메뉴).

풀스캔이 느린 이유는 **파일을 일일이 stat** 하기 때문인데, 이 모듈은 디렉터리만
훑어(파일은 개수만 셈) DB 도 안 쓰므로 **훨씬 빠르다.** 거대 트리에서도 멈추도록
**시간/개수 예산**을 둔다(초과 시 truncated=참 으로 부분 결과 반환).
"""

import os
import threading
import time
from typing import Callable, Dict, Optional


def _suggest_fold_depth(depth_counts: Dict[int, int], max_depth_seen: int) -> int:
    """깊이별 디렉터리 수에서 '폭발이 시작되는 깊이 바로 위'를 추천 fold-depth 로.

    예) 깊이별 12 → 380 → 4,200 → 51,000 → 2,300,000 → 44,000,000 이면
    depth 6 에서 터지므로 5 를 추천(파티션까지 보존, 그 아래는 합산).
    """
    depths = sorted(d for d in depth_counts if depth_counts[d] > 0)
    if not depths:
        return 0
    best_d, best_ratio = None, 0.0
    for d in depths:
        if d == 0:
            continue
        prev = depth_counts.get(d - 1, 0) or 1
        ratio = depth_counts[d] / prev
        # '의미 있는 폭발'만: 절대 수가 크고(>=1000) 증가비가 가장 큰 깊이
        if depth_counts[d] >= 1000 and ratio > best_ratio:
            best_ratio, best_d = ratio, d
    if best_d and best_ratio >= 4.0:
        return max(1, best_d - 1)
    # 뚜렷한 knee 가 없으면 디렉터리가 가장 많은 깊이의 한 단계 위
    peak_d = max(depths, key=lambda d: depth_counts[d])
    if peak_d > 1:
        return max(1, peak_d - 1)
    return max(1, max_depth_seen - 1) if max_depth_seen > 1 else 0


def analyze_depth(path: str, *, max_depth: int = 0, dir_limit: int = 0,
                  time_budget: float = 0.0, workers: int = 8,
                  stop_event: Optional[threading.Event] = None,
                  on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """`path` 아래 디렉터리 깊이 구조를 측정한다.

    인자:
      max_depth   : 이 깊이까지만 내려감(0=무제한). 캡에 닿으면 capped=참.
      dir_limit   : 디렉터리 이만큼 방문하면 중단(0=무제한).
      time_budget : 이 초만큼 지나면 중단(0=무제한).
      workers     : 동시 디렉터리 워커 수(병렬 readdir).
      stop_event  : 외부 중지 신호.
      on_progress : 진행 콜백(total/max_depth/elapsed 등 dict 를 받음, 스로틀됨).

    반환: {ok, root, levels[{depth,dirs,cumulative,sample}], max_depth, total_dirs,
           total_files, error_dirs, truncated, capped, elapsed, suggested_fold_depth}
    """
    root = os.path.abspath(path)
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다: %s" % root}

    stop_event = stop_event or threading.Event()
    cnt_lock = threading.Lock()
    depth_counts: Dict[int, int] = {0: 1}     # 루트(depth 0)도 1개로 셈
    sample_paths: Dict[int, str] = {0: root}
    st = {"total": 1, "files": 0, "errors": 0, "max_depth": 0,
          "truncated": False, "capped": False}
    t0 = time.time()
    last_prog = [0.0]

    stack = [(root, 0)]                        # (경로, 깊이) — 깊이 우선
    cv = threading.Condition()
    active = [0]

    def over_budget() -> bool:
        if stop_event.is_set():
            return True
        if dir_limit and st["total"] >= dir_limit:
            return True
        if time_budget and (time.time() - t0) >= time_budget:
            return True
        return False

    def emit_progress() -> None:
        if not on_progress:
            return
        now = time.time()
        if now - last_prog[0] < 0.4:
            return
        last_prog[0] = now
        on_progress({"total": st["total"], "files": st["files"],
                     "max_depth": st["max_depth"], "errors": st["errors"],
                     "elapsed": now - t0})

    def scan_one(dpath: str, depth: int) -> None:
        child_depth = depth + 1
        subdirs = []
        files = 0
        try:
            with os.scandir(dpath) as it:
                for entry in it:
                    if over_budget():
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(entry.path)
                        else:
                            files += 1
                    except OSError:
                        continue
        except OSError:
            with cnt_lock:
                st["errors"] += 1
            return

        with cnt_lock:
            st["files"] += files
            if subdirs:
                count_them = (not max_depth) or (child_depth <= max_depth)
                if count_them:
                    depth_counts[child_depth] = depth_counts.get(child_depth, 0) + len(subdirs)
                    st["total"] += len(subdirs)
                    if child_depth > st["max_depth"]:
                        st["max_depth"] = child_depth
                    sample_paths.setdefault(child_depth, subdirs[0])
                if max_depth and child_depth >= max_depth:
                    st["capped"] = True   # 캡에서 멈춰 더 깊을 수 있음
            emit_progress()

        # 더 내려갈지: max_depth 안 넘을 때만 자식을 스택에 추가
        if subdirs and ((not max_depth) or (child_depth < max_depth)) and not over_budget():
            with cv:
                for c in subdirs:
                    stack.append((c, child_depth))
                cv.notify_all()

    def worker() -> None:
        while True:
            with cv:
                while not stack and active[0] > 0 and not over_budget():
                    cv.wait(0.1)
                if over_budget() and (stack or active[0] > 0):
                    st["truncated"] = True
                if not stack or over_budget():
                    cv.notify_all()
                    return
                dpath, depth = stack.pop()
                active[0] += 1
            try:
                scan_one(dpath, depth)
            finally:
                with cv:
                    active[0] -= 1
                    cv.notify_all()

    n = max(1, int(workers or 1))
    threads = [threading.Thread(target=worker, name="depth-%d" % i, daemon=True)
               for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 누적(깊이 N 까지 fold 시 보존되는 행 수) 계산 + 레벨 표 구성
    levels = []
    running = 0
    for d in sorted(depth_counts):
        running += depth_counts[d]
        levels.append({"depth": d, "dirs": depth_counts[d], "cumulative": running,
                       "sample": sample_paths.get(d, "")})
    return {
        "ok": True,
        "root": root,
        "levels": levels,
        "max_depth": st["max_depth"],
        "total_dirs": st["total"],
        "total_files": st["files"],
        "error_dirs": st["errors"],
        "truncated": bool(st["truncated"]),
        "capped": bool(st["capped"]),
        "elapsed": time.time() - t0,
        "suggested_fold_depth": _suggest_fold_depth(depth_counts, st["max_depth"]),
    }

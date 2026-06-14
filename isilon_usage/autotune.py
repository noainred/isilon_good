"""오토튜닝 — 새 루트 스캔 전, 실제 엔진(pscan)을 짧게 '시간상자'로 돌려 최적
프로세스×스레드 조합을 실측으로 고른다.

수동으로 bench_walk 를 돌려 procs×threads 를 고르던 과정을 자동화한 것. 측정은 본
스캔과 **같은 엔진(pscan)** 을 쓰므로 결과가 실제와 일치한다. 측정 후 그 설정으로
본 스캔을 시작한다(server 통합). 진행 콜백으로 대시보드에 단계별 실시간 표시.

정직성: 같은 영역을 후보별로 연속 측정하므로 '뒤 후보'가 NAS/OS 캐시 덕을 본다
(배속이 다소 부풀려질 수 있음). 그래도 '단일 vs 병렬'의 큰 차이를 가리는 데는 충분하다.
로컬 빠른 저장소에선 병렬이 이득 없어 단일이 선택될 수 있는데, 그것도 올바른 결론이다.
"""
from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Tuple

from . import pscan


def _label(p: int, t: int) -> str:
    if p <= 1 and t <= 1:
        return "단일(serial)"
    if t <= 1:
        return "%d프로세스" % p
    return "%d프로세스 × %d스레드" % (p, t)


def default_candidates(max_procs: int = 8, max_threads: int = 8) -> List[Tuple[int, int]]:
    """측정할 (프로세스, 스레드) 조합 — 단일 기준선 + 프로세스만 + 2단 병렬."""
    p = max(1, int(max_procs))
    t = max(1, int(max_threads))
    cands: List[Tuple[int, int]] = [(1, 1)]
    if p > 1:
        cands.append((p, 1))           # 프로세스만(GIL 회피 효과 확인)
    if p > 1 and t > 1:
        cands.append((p, t))           # 2단 병렬(NFS 왕복 지연 은닉)
    return cands


def autotune(root: str, *, secs: float = 8.0, size_mode: str = "disk",
             candidates: Optional[List[Tuple[int, int]]] = None,
             max_procs: int = 8, max_threads: int = 8,
             stop_event=None,
             on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """root 에서 후보 조합을 각 secs 초 측정해 최적 (procs, threads) 를 고른다.

    반환: {ok, root, results:[{procs,threads,files_per_sec,speedup,files,elapsed,label}],
           best:{...}, note}. on_progress 는 후보마다 phase="measuring", 끝에 "done".
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근 불가: %s" % root}
    cands = candidates or default_candidates(max_procs, max_threads)
    results: List[dict] = []
    base: Optional[float] = None
    t_start = time.time()
    for i, (p, t) in enumerate(cands):
        if stop_event is not None and stop_event.is_set():
            break
        if on_progress:
            on_progress({"phase": "measuring", "index": i, "total": len(cands),
                         "procs": p, "threads": t, "label": _label(p, t),
                         "results": list(results), "elapsed": time.time() - t_start})
        r = pscan.parallel_scan(root, processes=p, threads_per_proc=t,
                                size_mode=size_mode, max_seconds=secs)
        fps = float(r.get("files_per_sec", 0.0)) if r.get("ok") else 0.0
        if base is None:
            base = fps or 1.0
        results.append({
            "procs": p, "threads": t, "label": _label(p, t),
            "files_per_sec": round(fps, 1),
            "speedup": round(fps / base, 2) if base else 0.0,
            "files": int(r.get("total_files", 0)),
            "elapsed": round(float(r.get("elapsed", 0.0)), 2),
        })
    if results:
        best = max(results, key=lambda x: x["files_per_sec"])
    else:
        best = {"procs": 1, "threads": 1, "label": _label(1, 1),
                "files_per_sec": 0.0, "speedup": 1.0}
    out = {
        "ok": True, "root": root, "secs": secs,
        "results": results, "best": best,
        "elapsed": round(time.time() - t_start, 2),
        "note": "측정값은 캐시 영향이 있어 상대 비교용입니다(같은 영역 연속 측정).",
    }
    if on_progress:
        on_progress({"phase": "done", "results": list(results), "best": best,
                     "elapsed": out["elapsed"]})
    return out

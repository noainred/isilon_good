"""실측 기반 워커(동시 스캔 스레드) 수 보정.

서버 사양만 보는 heuristic(monitor.recommend_workers)과 달리, 실제 대상 경로에서
여러 후보 스레드 수로 **짧게 시범 탐색**해 초당 디렉터리 처리량(dirs/sec)을 비교하고
가장 빠른(또는 무릎점) 값을 추천한다. 시범 스캔은 로컬 임시 DB 에 기록하고 즉시
삭제하므로 관리 DB/실제 스캔에는 영향을 주지 않는다.

주의: 같은 경로를 반복 탐색하므로 NFS 메타데이터 캐시가 점차 따뜻해져 측정에 약간의
편향이 생길 수 있다(워밍업 + 후보 순서 무작위화로 완화). 따라서 결과는 '추정치'이며,
대표적인 하위 경로에서 측정하는 것을 권한다.
"""

import os
import random
import shutil
import tempfile
import threading
import time
from typing import List, Optional

from . import db as dbmod
from . import scanner as scannermod

DEFAULT_CANDIDATES = [8, 16, 32]
HARD_CAP = 64           # settings 의 scan_workers 상한과 일치
MAX_CANDIDATES = 5      # 한 번에 너무 오래 돌지 않도록
MAX_BUDGET = 20.0       # 후보당 최대 측정 시간(초)


def _normalize_candidates(candidates) -> List[int]:
    out = set()
    for c in (candidates or DEFAULT_CANDIDATES):
        try:
            v = int(c)
        except (TypeError, ValueError):
            continue
        if v >= 1:
            out.add(min(HARD_CAP, v))
    return sorted(out)[:MAX_CANDIDATES] if out else list(DEFAULT_CANDIDATES)


def _probe(sample_path: str, workers: int, budget_sec: float,
           stop_event: Optional[threading.Event]) -> Optional[dict]:
    """주어진 스레드 수로 budget_sec 동안 탐색만 하고 (dirs/sec 등)을 측정."""
    tmp = tempfile.mkdtemp(prefix="iu_bench_")
    db_path = os.path.join(tmp, "bench.db")
    probe_stop = threading.Event()

    def _timer():
        end = time.time() + budget_sec
        while time.time() < end:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(0.1)
        probe_stop.set()

    timer = threading.Thread(target=_timer, daemon=True)
    timer.start()
    t0 = time.time()
    try:
        rid = scannermod.run_scan(
            db_path, sample_path, workers=workers,
            check_readonly=False, with_monitor=False, stop_event=probe_stop,
        )
    except Exception:  # noqa: BLE001 - 시범 측정 실패는 그 후보만 건너뛴다
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    elapsed = max(1e-6, time.time() - t0)
    discovered = 0
    files = 0
    try:
        conn = dbmod.connect(db_path)
        row = conn.execute(
            "SELECT discovered_dirs, total_files FROM scan_runs WHERE id=?",
            (rid,),
        ).fetchone()
        if row:
            discovered = int(row["discovered_dirs"] or 0)
            files = int(row["total_files"] or 0)
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "workers": workers,
        "discovered": discovered,
        "files": files,
        "elapsed": round(elapsed, 2),
        "dirs_per_sec": round(discovered / elapsed, 1),
    }


def benchmark_workers(sample_path: str, candidates=None, budget_sec: float = 5.0,
                      warmup: bool = True, stop_event: Optional[threading.Event] = None,
                      progress_cb=None) -> dict:
    """후보 스레드 수로 짧게 시범 탐색해 처리량을 비교하고 권장값을 반환.

    반환: {ok, sample_path, budget_sec, results[{workers,discovered,files,elapsed,
    dirs_per_sec}], recommended, note, error}
    """
    sample_path = os.path.abspath(sample_path)
    if not os.path.isdir(sample_path):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다.",
                "sample_path": sample_path}
    budget_sec = float(max(1.0, min(MAX_BUDGET, budget_sec)))
    cands = _normalize_candidates(candidates)

    # 워밍업: 캐시 상태를 비슷하게 맞춰 후보 간 공정성을 높인다(결과는 버림).
    if warmup and not (stop_event is not None and stop_event.is_set()):
        if progress_cb:
            progress_cb("warmup", None)
        _probe(sample_path, max(cands), min(budget_sec, 3.0), stop_event)

    # 후보 순서를 무작위로 — 캐시가 따뜻해지는 순서 편향을 분산.
    order = list(cands)
    random.shuffle(order)
    results = []
    for w in order:
        if stop_event is not None and stop_event.is_set():
            break
        if progress_cb:
            progress_cb("probe", w)
        r = _probe(sample_path, w, budget_sec, stop_event)
        if r is not None:
            results.append(r)
    results.sort(key=lambda r: r["workers"])

    # 추천: 최고 처리량. 단 더 적은 스레드가 최고의 90% 이상이면 그쪽(무릎점).
    recommended = None
    reliable = [r for r in results if r["discovered"] >= 50]  # 표본 부족 제외
    pool = reliable or results
    if pool:
        best = max(pool, key=lambda r: r["dirs_per_sec"])
        recommended = best["workers"]
        for r in sorted(pool, key=lambda r: r["workers"]):
            if best["dirs_per_sec"] > 0 and r["dirs_per_sec"] >= 0.9 * best["dirs_per_sec"]:
                recommended = r["workers"]
                break

    note = ("같은 경로를 반복 탐색하므로 NFS 캐시 영향이 있을 수 있는 추정치입니다. "
            "대표적인 하위 경로에서, 가급적 다른 스캔이 없을 때 측정하세요.")
    if reliable != results and results:
        note += " (표본이 적은 후보는 추천에서 제외했습니다.)"
    return {
        "ok": True,
        "sample_path": sample_path,
        "budget_sec": budget_sec,
        "candidates": cands,
        "results": results,
        "recommended": recommended,
        "note": note,
    }

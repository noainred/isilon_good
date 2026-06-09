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
import shutil
import tempfile
import threading
import time
from typing import List, Optional

from . import db as dbmod
from . import scanner as scannermod

# 메모리 2배 한도까지 단계적으로 올린다(8→16→32→48→64). 한도 도달 시 중단.
DEFAULT_CANDIDATES = [8, 16, 32, 48, 64]
HARD_CAP = 64           # settings 의 scan_workers 상한과 일치
MAX_CANDIDATES = 6      # 한 번에 너무 오래 돌지 않도록
MAX_BUDGET = 20.0       # 후보당 최대 측정 시간(초)
DEFAULT_MEM_FACTOR = 2.0  # 기준(시작) RSS 대비 이 배수에 도달하면 더 올리지 않음

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _self_rss() -> int:
    """현재 프로세스의 RSS(상주 메모리) 바이트. /proc/self/statm 기반(stdlib)."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * _PAGE
    except (OSError, IndexError, ValueError):
        return 0


def _normalize_candidates(candidates) -> List[int]:
    out = set()
    for c in (candidates or DEFAULT_CANDIDATES):
        try:
            v = int(c)
        except (TypeError, ValueError):
            continue
        if v >= 1:
            out.add(min(HARD_CAP, v))
    # 오름차순(메모리 2배 한도까지 단계적으로 올리기 위해 정렬 고정)
    return sorted(out)[:MAX_CANDIDATES] if out else list(DEFAULT_CANDIDATES)


def _probe(sample_path: str, workers: int, budget_sec: float,
           stop_event: Optional[threading.Event]) -> Optional[dict]:
    """주어진 스레드 수로 budget_sec 동안 탐색만 하고 (dirs/sec, peak RSS 등)을 측정."""
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

    # 측정 중 프로세스 RSS 최고치를 표본으로 추적(메모리 사용량)
    peak = [_self_rss()]
    samp_stop = threading.Event()

    def _sampler():
        while not samp_stop.is_set():
            r = _self_rss()
            if r > peak[0]:
                peak[0] = r
            samp_stop.wait(0.3)

    timer = threading.Thread(target=_timer, daemon=True)
    sampler = threading.Thread(target=_sampler, daemon=True)
    timer.start()
    sampler.start()
    t0 = time.time()
    try:
        rid = scannermod.run_scan(
            db_path, sample_path, workers=workers,
            check_readonly=False, with_monitor=False, stop_event=probe_stop,
        )
    except Exception:  # noqa: BLE001 - 시범 측정 실패는 그 후보만 건너뛴다
        samp_stop.set()
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    finally:
        samp_stop.set()
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
        "peak_rss_bytes": int(peak[0]),
    }


def benchmark_workers(sample_path: str, candidates=None, budget_sec: float = 5.0,
                      warmup: bool = True, mem_factor: float = DEFAULT_MEM_FACTOR,
                      stop_event: Optional[threading.Event] = None,
                      progress_cb=None, on_result=None) -> dict:
    """후보 스레드 수로 짧게 시범 탐색해 처리량을 비교하고 권장값을 반환.

    스레드를 오름차순으로 올리되, 프로세스 RSS 가 시작 시점 대비 mem_factor(기본 2배)에
    도달하면 더 올리지 않고 멈춘다. on_result(result) 가 주어지면 **각 후보 측정이
    끝날 때마다** 그 결과를 콜백으로 전달한다(단계별 표시용).

    반환: {ok, sample_path, budget_sec, base_rss_bytes, mem_factor,
    results[{workers,discovered,files,elapsed,dirs_per_sec,peak_rss_bytes,mem_ratio,
    mem_stop?}], recommended, note, error}
    """
    sample_path = os.path.abspath(sample_path)
    if not os.path.isdir(sample_path):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다.",
                "sample_path": sample_path}
    budget_sec = float(max(1.0, min(MAX_BUDGET, budget_sec)))
    try:
        mem_factor = float(mem_factor)
    except (TypeError, ValueError):
        mem_factor = DEFAULT_MEM_FACTOR
    mem_factor = max(1.1, min(8.0, mem_factor))
    cands = _normalize_candidates(candidates)

    base_rss = max(1, _self_rss())   # 메모리 2배 판정 기준(시작 시점)

    # 워밍업: 캐시 상태를 비슷하게 맞춰 후보 간 공정성을 높인다(결과는 버림).
    if warmup and not (stop_event is not None and stop_event.is_set()):
        if progress_cb:
            progress_cb("warmup", None)
        _probe(sample_path, cands[0], min(budget_sec, 3.0), stop_event)

    results = []
    mem_hit = False
    for w in cands:   # 오름차순: 메모리 2배 도달 시 더 안 올림
        if stop_event is not None and stop_event.is_set():
            break
        if progress_cb:
            progress_cb("probe", w)
        r = _probe(sample_path, w, budget_sec, stop_event)
        if r is None:
            continue
        r["mem_ratio"] = round(r["peak_rss_bytes"] / base_rss, 2)
        if r["mem_ratio"] >= mem_factor:
            r["mem_stop"] = True
            mem_hit = True
        results.append(r)
        if on_result:
            on_result(r)        # ← 단계별로 즉시 전달(한 번에 안 보여줌)
        if mem_hit:
            break

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
    if mem_hit:
        note += " 메모리 %.0f배 한도에 도달해 더 높은 스레드는 측정하지 않았습니다." % mem_factor
    if reliable != results and results:
        note += " (표본이 적은 후보는 추천에서 제외했습니다.)"
    return {
        "ok": True,
        "sample_path": sample_path,
        "budget_sec": budget_sec,
        "base_rss_bytes": base_rss,
        "mem_factor": mem_factor,
        "candidates": cands,
        "results": results,
        "recommended": recommended,
        "note": note,
    }


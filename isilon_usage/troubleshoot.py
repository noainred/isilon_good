"""트러블슈팅 — 실행 중인 스캔의 '어느 구간이 느린지'를 진단한다.

대시보드(serve) 에서 시작한 스캔은 서버 프로세스 안에서 스레드(`disc-*`)로 돌기
때문에, `sys._current_frames()` 로 **각 워커가 지금 어느 코드 구간**(디렉터리
읽기/파일 stat/용량 합산/DB 쓰기/유휴)에 있는지 분류할 수 있다(인프로세스 프로파일).

여기에 **처리량**(초당 디렉터리·파일), **하트비트**(진행 갱신 경과), **프론티어**
(대기 일감 추정), **DB/디스크** 상태를 합쳐 **병목 구간**과 **처방**을 낸다.
"""

import os
import sys
import threading
import time

from . import db as dbmod
from . import manager as mgrmod
from . import monitor as monmod

# 워커 콜스택에 나타나는 함수 → 구간 분류(안쪽 프레임부터 먼저 매칭)
_STAT = {"_safe_stat", "_safe_stat_path", "_entry_bytes"}      # 파일 stat(NAS)
_FOLD = {"_fold_subtree"}                                       # 깊은 하위 용량 합산(NAS)
_DB = {"_flush_progress_locked", "_flush_progress", "_write_stats"}  # DB 쓰기/커밋
_FLUSH = {"flush_files"}                                        # 락 보호 누적 구간(DB/락)
_CLAIM = {"_claim_batch"}                                       # 일감 가져오기
_READDIR = {"_discover_one"}                                    # scandir 루프(NAS readdir)
_IDLE = {"wait", "_discover_worker"}                            # 유휴 대기

_CAT_LABEL = {
    "readdir": "디렉터리 읽기(scandir·NAS)",
    "stat": "파일 stat(용량 측정·NAS)",
    "fold": "하위 용량 합산(fold·NAS)",
    "db": "DB 쓰기/커밋",
    "claim": "일감 가져오기",
    "idle": "유휴(대기)",
    "other": "기타",
}


def _classify(frame) -> str:
    """워커 프레임 체인에서 '현재 구간'을 분류한다(안쪽 프레임 우선)."""
    f = frame
    while f is not None:
        n = f.f_code.co_name
        if n in _STAT:
            return "stat"
        if n in _FOLD:
            return "fold"
        if n in _DB or n in _FLUSH:
            return "db"
        if n in _CLAIM:
            return "claim"
        if n in _READDIR:
            return "readdir"
        if n in _IDLE:
            return "idle"
        f = f.f_back
    return "other"


def worker_snapshot():
    """현재 스캔 워커(disc-*) 스레드들이 어느 구간에 있는지 스냅샷."""
    frames = sys._current_frames()
    cats = {k: 0 for k in ("readdir", "stat", "fold", "db", "claim", "idle", "other")}
    detail = []
    for t in threading.enumerate():
        if not t.name.startswith("disc-"):
            continue
        fr = frames.get(t.ident)
        cat = _classify(fr) if fr is not None else "other"
        cats[cat] += 1
        detail.append({"name": t.name, "cat": cat, "label": _CAT_LABEL[cat],
                       "fn": (fr.f_code.co_name if fr is not None else "?")})
    cats["total"] = sum(v for v in cats.values())
    detail.sort(key=lambda d: d["name"])
    return cats, detail


def _row(conn):
    cur = conn.execute(
        "SELECT root_path, status, phase, discovered_dirs, total_dirs, processed_dirs, "
        "total_files, error_dirs, updated_at, active_workers, workers, current_dir, "
        "worker_dirs FROM scan_runs ORDER BY id LIMIT 1")
    r = cur.fetchone()
    return dict(r) if r is not None else None


def _max_id(conn) -> int:
    try:
        v = conn.execute("SELECT max(id) FROM directories").fetchone()[0]
        return int(v or 0)
    except Exception:
        return 0


def _size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _disk_free(path) -> int:
    try:
        v = os.statvfs(path or ".")
        return v.f_bavail * v.f_frsize
    except OSError:
        return 0


def _cpu_times():
    """/proc/stat 의 (total, iowait) 누적 틱. iowait 비율 계산용."""
    try:
        with open("/proc/stat") as fh:
            parts = fh.readline().split()
        vals = [float(x) for x in parts[1:]]
        total = sum(vals)
        iowait = vals[4] if len(vals) > 4 else 0.0
        return total, iowait
    except OSError:
        return None, None


def _probe_nas_latency(path, timeout=3.0):
    """대상(NAS) 경로에서 메타데이터 응답 지연(ms)을 잰다 — 스레드+타임아웃으로
    행(hang) 걸려도 진단이 멈추지 않게 한다. 반환: (ms 또는 None, timed_out)."""
    result = {"ms": None}

    def _probe():
        t0 = time.time()
        try:
            with os.scandir(path) as it:
                for i, _ in enumerate(it):
                    if i >= 32:           # 앞쪽 몇 개만 — 거대 디렉터리도 빠르게
                        break
            result["ms"] = (time.time() - t0) * 1000.0
        except OSError:
            result["ms"] = -1.0           # 접근 불가
    th = threading.Thread(target=_probe, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return None, True                 # 타임아웃 — 응답 없음(행 의심)
    return result["ms"], False


def _lvl_worse(a, b):
    order = {"ok": 0, "warn": 1, "bad": 2}
    return a if order.get(a, 0) >= order.get(b, 0) else b


def _build_resources(*, proc_cpu, sys_cpu, ncpu, load1, mem_percent, mem_avail,
                     swap_used, disk_free, nas_ms, nas_timeout, iowait_pct):
    """CPU·메모리·로컬디스크·NAS 자원별 상태(ok/warn/bad) + 한 줄 설명."""
    res = []

    # CPU — 이 앱은 GIL 때문에 한 프로세스가 최대 ~1코어. 프로세스 cpu% 가 핵심.
    if proc_cpu is None:
        res.append({"name": "CPU", "level": "ok", "value": "측정 불가",
                    "note": "프로세스 CPU 시간을 읽지 못함"})
    else:
        lvl = "bad" if proc_cpu >= 85 else ("warn" if proc_cpu >= 60 else "ok")
        note = ("스캐너가 1코어를 거의 포화 — GIL/CPU 바운드(스레드 늘려도 안 빨라짐)"
                if proc_cpu >= 85 else
                ("CPU 사용 높음" if proc_cpu >= 60 else "CPU 여유 — CPU 병목 아님"))
        res.append({"name": "CPU", "level": lvl,
                    "value": "프로세스 %.0f%% (1코어 기준) · 시스템 %.0f%% · load1 %.2f/%d코어"
                             % (proc_cpu, sys_cpu if sys_cpu is not None else 0, load1, ncpu),
                    "note": note})

    # 메모리
    avail_gb = mem_avail / (1024 ** 3)
    mlvl = "bad" if (mem_percent >= 92 or swap_used > 0 and mem_percent >= 85) else (
        "warn" if mem_percent >= 82 else "ok")
    res.append({"name": "메모리", "level": mlvl,
                "value": "%.1f%% 사용 · 여유 %.1f GB · 스왑 %s"
                         % (mem_percent, avail_gb, "사용중" if swap_used else "없음"),
                "note": ("메모리 부족/스왑 — OOM·스왑 지연 위험" if mlvl == "bad" else
                         ("메모리 사용 높음" if mlvl == "warn" else "메모리 여유 — 병목 아님"))})

    # 로컬 디스크(per-run DB 가 올라탄 디스크): 여유 + iowait
    free_gb = disk_free / (1024 ** 3)
    dlvl = "bad" if free_gb < 5 else ("warn" if (free_gb < 15 or (iowait_pct or 0) >= 30) else "ok")
    iotxt = ("· iowait %.0f%%" % iowait_pct) if iowait_pct is not None else ""
    res.append({"name": "로컬 디스크(DB)", "level": dlvl,
                "value": "여유 %.1f GB %s" % (free_gb, iotxt),
                "note": ("디스크 거의 참 — DB 쓰기 실패·프로세스 죽음 위험" if free_gb < 5 else
                         ("디스크 여유 부족 또는 I/O 대기 높음" if dlvl == "warn" else
                          "로컬 디스크 여유·I/O 한가 — 병목 아님"))})

    # NAS(대상 마운트) — 메타데이터 응답 지연
    if nas_timeout:
        res.append({"name": "NAS(대상)", "level": "bad", "value": "응답 없음(>3초)",
                    "note": "메타데이터 응답이 없음 — NAS 행(hang)·네트워크 문제 강력 의심"})
    elif nas_ms is None or nas_ms < 0:
        res.append({"name": "NAS(대상)", "level": "warn", "value": "프로브 실패",
                    "note": "대상 경로 접근 불가(권한/언마운트?)"})
    else:
        nlvl = "bad" if nas_ms >= 500 else ("warn" if nas_ms >= 100 else "ok")
        res.append({"name": "NAS(대상)", "level": nlvl,
                    "value": "메타데이터 응답 %.0f ms" % nas_ms,
                    "note": ("NAS 메타데이터 매우 느림 — readdir/stat 지연이 병목" if nlvl == "bad" else
                             ("NAS 응답 다소 느림" if nlvl == "warn" else "NAS 응답 빠름 — 병목 아님"))})

    # 자원 병목 종합: 가장 나쁜 자원
    worst = "ok"
    worst_name = None
    for r in res:
        if _lvl_worse(r["level"], worst) != worst:
            worst, worst_name = r["level"], r["name"]
        elif r["level"] == worst and worst != "ok" and worst_name is None:
            worst_name = r["name"]
    if worst == "ok":
        summary = ("✅ CPU·메모리·디스크·NAS 모두 여유 — 병목은 '자원 포화'가 아닙니다. "
                   "위 '구간 진단'(직렬화/락/구조)을 보세요.")
    else:
        summary = "주의 자원: %s — 아래 표에서 빨강/노랑 항목이 현재 제약입니다." % worst_name
    return res, {"level": worst, "name": worst_name, "summary": summary}


def _verdict(w, rate_dirs, rate_files, hb_age, pending, db_bytes, disk_free):
    """구간 분포 + 처리량 + 하트비트 + 프론티어로 병목 구간과 처방을 낸다."""
    total = w.get("total", 0) or 1
    idle = w.get("idle", 0) + w.get("claim", 0)
    nas = w.get("readdir", 0) + w.get("stat", 0) + w.get("fold", 0)
    db = w.get("db", 0)
    recs = []

    if hb_age > 60 and nas > 0 and rate_dirs < 1 and rate_files < 5:
        recs = ["아래 '워커별 현재 경로'에서 멈춘 디렉터리 확인",
                "NAS/마운트·네트워크(NFS) 상태 점검 — 느린/행 걸린 경로 의심",
                "필요하면 재시작(프론티어가 DB에 있어 중단지점부터 재개)"]
        return ("danger", "🔴 NAS I/O 행(hang) 의심",
                "워커가 디렉터리 읽기/stat 구간에 있는데 진행 갱신이 %d초째 없습니다. "
                "NAS 응답 지연 또는 행(hang)으로 보입니다." % int(hb_age), recs)

    if idle >= total * 0.5 and pending > 100000:
        recs = ["▶ '대상 분석' 메뉴로 추천 fold-depth 를 구해 backlog 폭발 차단",
                "원인은 단일 락+GIL 직렬화 — DB 엔진 교체로는 해결 안 됨",
                "--min-free-gb / --db-max-gb 안전망 설정"]
        return ("warn", "🟠 직렬화·굶주림 — 워커가 일감을 못 빼가는 중",
                "대기 일감(pending≈%s)은 많은데 워커 %d/%d개가 유휴입니다. 모든 DB 접근이 "
                "단일 락+단일 커넥션으로 직렬화돼(+GIL) 처리량이 묶입니다." % (
                    f"{pending:,}", idle, total), recs)

    if idle >= total * 0.5 and pending < 10000:
        recs = ["거의 끝났습니다 — 남은 소수 디렉터리 처리 대기",
                "특정 거대 폴더 때문이면 '워커별 현재 경로'에서 확인"]
        return ("info", "🟡 롱테일(거의 끝남)",
                "일감이 거의 없어(pending≈%s) 워커 대부분이 유휴입니다. 남은 소수를 "
                "처리 중입니다." % f"{pending:,}", recs)

    if db >= max(1, total * 0.5):
        recs = ["DB 커밋이 잦음 — fold-depth 로 행 수/커밋량 축소",
                "per-run DB를 더 빠른 로컬 디스크(SSD)로"]
        return ("warn", "🟠 DB 쓰기/커밋 구간 집중",
                "워커 %d/%d개가 DB 쓰기/커밋 구간에 있습니다. 커밋이 병목일 수 있습니다." % (
                    db, total), recs)

    if w.get("stat", 0) >= max(1, w.get("readdir", 0)) and rate_files > 0:
        recs = ["정상 측정 중 — 파일 stat 가 주 작업",
                "더 빠르게: 거대 폴더는 '대상 분석'으로 깊이 확인 후 fold-depth"]
        return ("ok", "🟢 파일 stat 바운드(정상 측정 중)",
                "NAS 파일 용량을 측정 중입니다 · 처리량 약 %.0f 파일/초, %.1f 디렉터리/초." % (
                    rate_files, rate_dirs), recs)

    if w.get("readdir", 0) > 0:
        return ("ok", "🟢 디렉터리 읽기 중(정상)",
                "NAS readdir(디렉터리 나열) 중 · 약 %.1f 디렉터리/초." % rate_dirs,
                ["깊고 넓은 트리 — '대상 분석'으로 깊이 구조 확인 권장"])

    if rate_dirs < 0.5 and rate_files < 1 and hb_age > 30:
        return ("warn", "🟠 진행이 매우 느림",
                "처리량이 거의 0이고 하트비트가 %d초째입니다. 아래 구간/경로를 확인하세요." % int(hb_age),
                ["'워커별 현재 경로'와 '구간 분포'를 확인"])

    return ("ok", "🟢 진행 중",
            "처리량 약 %.1f 디렉터리/초, %.0f 파일/초." % (rate_dirs, rate_files), [])


def diagnose(controller, *, sample_sec: float = 1.2) -> dict:
    """실행 중인 스캔을 진단한다(약 sample_sec 동안 처리량을 측정)."""
    data_dir = controller.data_dir
    mpath = mgrmod.manager_db_path(data_dir)
    if not os.path.exists(mpath):
        return {"ok": False, "error": "관리 DB가 없습니다."}
    mconn = dbmod.connect(mpath)
    try:
        sid = mgrmod.pick_default_scan(mconn)
        scan = mgrmod.get_scan(mconn, sid) if sid else None
    finally:
        mconn.close()
    if not scan:
        return {"ok": True, "running": False, "msg": "실행 중인 스캔이 없습니다."}
    db_path = scan["db_path"]
    if not db_path or not os.path.exists(db_path):
        return {"ok": True, "running": False, "msg": "per-run DB 준비 중입니다."}

    pid = os.getpid()                 # serve 모드: 스캔은 이 프로세스의 스레드
    sample = max(0.2, float(sample_sec))
    conn = dbmod.connect(db_path)
    try:
        r1 = _row(conn)
        if r1 is None:
            return {"ok": True, "running": False, "msg": "스캔 행이 아직 없습니다."}
        total_known = _max_id(conn)
        workers, detail = worker_snapshot()
        # ── 자원 샘플(같은 창에서): 프로세스 CPU·시스템 iowait·메모리·NAS 지연 ──
        cpu_s1 = monmod._proc_cputime_seconds(pid)
        cpu_t1, io_t1 = _cpu_times()
        memsamp = monmod.collect(pid)
        nas_root = r1.get("current_dir") or r1["root_path"]
        nas_ms, nas_timeout = _probe_nas_latency(nas_root, timeout=3.0)
        time.sleep(sample)
        r2 = _row(conn) or r1
        cpu_s2 = monmod._proc_cputime_seconds(pid)
        cpu_t2, io_t2 = _cpu_times()
    finally:
        conn.close()

    now = time.time()
    rate_dirs = max(0, (r2["discovered_dirs"] or 0) - (r1["discovered_dirs"] or 0)) / sample
    rate_files = max(0, (r2["total_files"] or 0) - (r1["total_files"] or 0)) / sample
    hb_age = max(0.0, now - (r2["updated_at"] or now))
    discovered = r2["discovered_dirs"] or 0
    pending_est = max(0, total_known - discovered - (r2["active_workers"] or 0))

    db_bytes = _size(db_path)
    wal_bytes = _size(db_path + "-wal")
    disk_free = _disk_free(os.path.dirname(db_path))

    # ── 자원별(CPU/메모리/디스크/NAS) 병목 점검 ──
    ncpu = os.cpu_count() or 1
    proc_cpu = None
    if cpu_s1 is not None and cpu_s2 is not None:
        proc_cpu = max(0.0, (cpu_s2 - cpu_s1) / sample * 100.0)   # 1코어 기준 %
    sys_cpu = memsamp.cpu_percent if memsamp else None
    iowait_pct = None
    if None not in (cpu_t1, cpu_t2, io_t1, io_t2) and (cpu_t2 - cpu_t1) > 0:
        iowait_pct = max(0.0, min(100.0, (io_t2 - io_t1) / (cpu_t2 - cpu_t1) * 100.0))
    resources, resource_bottleneck = _build_resources(
        proc_cpu=proc_cpu, sys_cpu=sys_cpu, ncpu=ncpu,
        load1=(memsamp.load1 if memsamp else 0.0),
        mem_percent=(memsamp.mem_percent if memsamp else 0.0),
        mem_avail=((memsamp.mem_total - memsamp.mem_used) if memsamp else 0),
        swap_used=(memsamp.swap_used if memsamp else 0),
        disk_free=disk_free, nas_ms=nas_ms, nas_timeout=nas_timeout, iowait_pct=iowait_pct)

    level, title, desc, recs = _verdict(
        workers, rate_dirs, rate_files, hb_age, pending_est, db_bytes, disk_free)

    # 워커별 현재 경로(병렬 표시용 JSON) 파싱
    worker_dirs = []
    try:
        import json
        worker_dirs = json.loads(r2.get("worker_dirs") or "[]")
    except Exception:
        worker_dirs = []

    return {
        "ok": True, "running": (r2["status"] in ("discovering", "sizing", "running")),
        "root_path": r2["root_path"], "status": r2["status"], "phase": r2["phase"],
        "workers": workers, "worker_detail": detail,
        "rate_dirs": rate_dirs, "rate_files": rate_files,
        "heartbeat_age": hb_age, "sample_sec": sample,
        "discovered": discovered, "total_known": total_known, "pending_est": pending_est,
        "active_workers": r2.get("active_workers") or 0, "configured_workers": r2.get("workers") or 0,
        "db_bytes": db_bytes, "wal_bytes": wal_bytes, "disk_free": disk_free,
        "current_dir": r2.get("current_dir"), "worker_dirs": worker_dirs,
        "verdict": {"level": level, "title": title, "desc": desc},
        "recommendations": recs,
        "resources": resources, "resource_bottleneck": resource_bottleneck,
    }

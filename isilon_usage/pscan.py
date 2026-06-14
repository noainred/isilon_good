"""pscan — 멀티프로세스 병렬 스캔 PoC (GIL/단일 락 직렬화 회피).

진단 결론: 기존 스레드 스캐너는 **GIL + 단일 `_dlock` + 단일 DB 커넥션**으로
직렬화돼 8 워커가 사실상 1~2개 속도(≈1,000 stat/s)에 묶였다. 이 PoC는 **스레드가
아니라 프로세스**로 분리해 각 프로세스가 자기 GIL로 진짜 병렬 실행되는지(=처리량이
프로세스 수에 비례하는지)를 보인다.

핵심 설계:
  1) 루트의 **1단계 자식 디렉터리를 작업 단위**로 쪼갠다(서로 disjoint → 중복 없음).
  2) 각 단위를 **별도 프로세스**(ProcessPoolExecutor)가 재귀로 훑어 용량/파일/디렉터리
     수를 집계한다. 프로세스 간 공유 락이 없다(각자 결과만 반환).
  3) **멀티노드**: node_mounts 가 주어지면 각 단위 경로의 루트 접두사를 노드 마운트로
     라운드로빈 치환 → 같은 아이실론을 **여러 노드로 동시 접근**해 메타데이터 부하 분산.
  4) 코디네이터가 루트 직속 파일 + 단위별 결과를 합산한다.

한계(정직하게): 하드링크 중복 제거는 프로세스 경계를 넘지 못한다(단위 내에서만).
보통 하드링크는 같은 상위 트리 안이라 영향이 작지만, 스냅샷/백업 트리는 과대 계상될
수 있다. 1단계 분할이라 한 자식이 거대하면 그 프로세스가 straggler 가 된다(실제
구현은 작업 훔치기/더 깊은 분할로 개선).
"""

import os
import time
from typing import Callable, List, Optional


def _entry_bytes(st, size_mode: str) -> int:
    if size_mode == "apparent":
        return st.st_size
    # disk 점유(du 기본): 512B 블록 단위
    return getattr(st, "st_blocks", 0) * 512


def scan_subtree(path: str, size_mode: str = "disk", per_file_work: int = 0,
                 threads: int = 1, recursive: bool = True, deadline: float = 0.0) -> tuple:
    """path 아래 전체(자기 포함)를 재귀로 훑어 (path, bytes, files, dirs, errors).

    프로세스 워커가 호출하는 모듈 레벨 함수(피클 가능). 스택 기반 반복(재귀 깊이 무제한).
    per_file_work>0 이면 파일마다 그만큼 CPU 연산을 더 한다(벤치마크 전용 — 실제
    스캐너의 '파일당 파이썬 처리 비용'을 모사해 GIL 효과를 보이기 위함).
    threads>1 이면 이 단위를 프로세스 안에서 다시 T 스레드로 훑어 **NFS 왕복 지연을
    은닉**한다(os.stat/scandir 는 syscall 동안 GIL 을 풀어 다른 스레드가 진행). 고지연
    NAS 에서 프로세스(=GIL 회피)×스레드(=지연 은닉) 2단 병렬의 핵심 레버.
    recursive=False 면 직속 파일만 센다(적응형 깊이 분할에서 '펼친 부모' 단위 — 자식
    디렉터리는 각각 별도 단위로 처리되므로 부모는 내려가지 않는다).
    deadline>0(절대 epoch 시각)이면 그 시각에 멈춘다(오토튜닝의 시간상자 측정 — 부분
    결과 + files_per_sec 만 쓰고 합계는 버린다).
    """
    if not recursive:
        return _scan_shallow(path, size_mode)
    if threads and threads > 1 and not per_file_work:
        return _scan_subtree_threaded(path, size_mode, threads, deadline)
    total_bytes = 0
    files = 0
    dirs = 0
    errors = 0
    acc = 0
    stack = [path]
    while stack:
        if deadline and time.time() >= deadline:   # 시간상자 만료(오토튜닝)
            break
        d = stack.pop()
        dirs += 1
        try:                                   # 디렉터리 자기 inode 분(du 와 동일)
            dst = os.stat(d, follow_symlinks=False)
            total_bytes += _entry_bytes(dst, size_mode)
        except OSError:
            errors += 1
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            st = e.stat(follow_symlinks=False)
                            total_bytes += _entry_bytes(st, size_mode)
                            files += 1
                            if per_file_work:          # 파일당 처리 비용 모사(벤치 전용)
                                for k in range(per_file_work):
                                    acc += k * k
                            if deadline and (files & 8191) == 0 and time.time() >= deadline:
                                break                  # 거대 평면 디렉터리 대비
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return (path, total_bytes, files, dirs, errors)


def _scan_subtree_threaded(path: str, size_mode: str, threads: int,
                           deadline: float = 0.0) -> tuple:
    """scan_subtree 의 스레드판 — 디렉터리 작업큐를 T 스레드가 당겨 지연을 은닉한다.

    임계구역은 카운터/큐 갱신뿐(작게) — stat/scandir(I/O)는 락 밖. 결과는 직렬판과
    동일한 (path, bytes, files, dirs, errors) 튜플. 프로세스 내부에서만 도는 스레드라
    프로세스 간 공유 상태는 없다(기존 pscan 정합성 그대로).
    deadline>0 이면 그 절대 시각에 워커가 멈춘다(오토튜닝의 시간상자 측정).
    """
    import queue
    import threading

    q: "queue.Queue[str]" = queue.Queue()
    q.put(path)
    lock = threading.Lock()
    tot = {"b": 0, "f": 0, "d": 0, "e": 0, "pending": 1}

    def worker():
        while True:
            if deadline and time.time() >= deadline:   # 시간상자 만료(오토튜닝)
                return
            try:
                d = q.get(timeout=0.1)
            except queue.Empty:
                with lock:
                    if tot["pending"] == 0:
                        return
                continue
            lb = lf = le = 0
            subs = []
            try:                                # 디렉터리 자기 inode 분
                dst = os.stat(d, follow_symlinks=False)
                lb += _entry_bytes(dst, size_mode)
            except OSError:
                le += 1
            try:
                with os.scandir(d) as it:       # I/O — 락 밖
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                subs.append(e.path)
                            else:
                                lb += _entry_bytes(e.stat(follow_symlinks=False), size_mode)
                                lf += 1
                        except OSError:
                            le += 1
            except OSError:
                le += 1
            with lock:                          # 카운터/큐만 — 짧게
                tot["b"] += lb
                tot["f"] += lf
                tot["d"] += 1
                tot["e"] += le
                for s in subs:
                    q.put(s)
                    tot["pending"] += 1
                tot["pending"] -= 1

    ts = [threading.Thread(target=worker, name="pscan-stat", daemon=True)
          for _ in range(max(2, int(threads)))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return (path, tot["b"], tot["f"], tot["d"], tot["e"])


def _remap(child: str, root: str, node_mount: str) -> str:
    """child 경로의 root 접두사를 node_mount 로 치환(멀티노드 분산용)."""
    rel = os.path.relpath(child, root)
    return os.path.join(node_mount, rel) if rel != "." else node_mount


def _scan_shallow(path: str, size_mode: str) -> tuple:
    """path 직속 파일만 센다(자식 디렉터리로 내려가지 않음 — 적응형 분할의 '부모' 단위).

    자식 디렉터리들은 각각 별도 단위로 재귀 스캔되므로, 부모는 자기 inode + 직속 파일만
    세면 누락/중복이 없다. dirs=1(자기 자신).
    """
    total_bytes = files = errors = 0
    try:
        dst = os.stat(path, follow_symlinks=False)
        total_bytes += _entry_bytes(dst, size_mode)
    except OSError:
        errors += 1
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        total_bytes += _entry_bytes(st, size_mode)
                        files += 1
                except OSError:
                    errors += 1
    except OSError:
        errors += 1
    return (path, total_bytes, files, 1, errors)


def _split_units(child_dirs: List[str], procs: int, target_factor: int = 4) -> list:
    """1단계 자식이 프로세스 수보다 적으면 더 깊이 펼쳐 병렬 단위를 늘린다.

    반환: [(scan_path, top_label, recursive)]. 펼친 부모는 recursive=False(직속 파일만),
    그 자식들은 recursive=True(재귀). top_label 은 원래 1단계 자식 — per_top(드릴다운)
    집계가 바뀌지 않게 유지한다. 단위가 잘게 쪼개져 자식 적은 영역의 작업 불균형이
    완화된다(예: 자식 16개 영역도 프로세스를 꽉 채움).
    """
    units = [(c, c, True) for c in child_dirs]
    target = max(int(procs) * max(1, int(target_factor)), int(procs))
    i = 0
    while len(units) < target and i < len(units):
        upath, top, rec = units[i]
        if not rec:
            i += 1
            continue
        try:
            subs = [e.path for e in os.scandir(upath)
                    if e.is_dir(follow_symlinks=False)]
        except OSError:
            subs = []
        if not subs:                 # 더 못 펼침(파일만 있는 잎 디렉터리)
            i += 1
            continue
        units[i] = (upath, top, False)                  # 부모: 직속 파일만
        units.extend((s, top, True) for s in subs)      # 자식: 재귀(top 라벨 유지)
        i += 1
    return units


def parallel_scan(root: str, *, processes: int = 4, size_mode: str = "disk",
                  node_mounts: Optional[List[str]] = None,
                  threads_per_proc: int = 1, max_seconds: float = 0.0,
                  on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """root 를 1단계 자식 단위로 쪼개 processes 개 프로세스로 병렬 스캔한다.

    threads_per_proc>1 이면 각 프로세스가 자기 단위를 다시 T 스레드로 훑어 **NFS 왕복
    지연을 은닉**한다(고지연 NAS 에서 프로세스×스레드 2단 병렬 = 동시 in-flight stat 을
    processes×T 로 키워 처리량을 끌어올림). 로컬 빠른 저장소에선 스레드가 이득 없으니 1 유지.
    """
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근 불가: %s" % root}
    t0 = time.time()
    tpp = max(1, int(threads_per_proc or 1))
    deadline = (t0 + max_seconds) if max_seconds else 0.0   # 오토튜닝 시간상자(0=무제한)

    # 루트 직속: 파일은 코디네이터가 직접 세고, 자식 디렉터리는 작업 단위로.
    total_bytes = 0
    total_files = 0
    errors = 0
    child_dirs: List[str] = []
    try:
        total_bytes += _entry_bytes(os.stat(root, follow_symlinks=False), size_mode)
    except OSError:
        pass
    try:
        with os.scandir(root) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        child_dirs.append(e.path)
                    else:
                        total_bytes += _entry_bytes(e.stat(follow_symlinks=False), size_mode)
                        total_files += 1
                except OSError:
                    errors += 1
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    total_dirs = 1   # 루트 자신
    per_top = []
    n_units = len(child_dirs)

    if child_dirs:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        procs0 = max(1, min(int(processes or 1), n_units))
        # 적응형 깊이 분할: 1단계 자식이 프로세스보다 적으면 더 깊이 펼쳐 병렬 단위를
        # 늘린다(자식 적은 영역의 작업 불균형 완화). 펼친 부모는 직속 파일만, 자식은
        # 재귀로 스캔 → 누락/중복 없음. per_top 은 원래 1단계 자식(top)으로 합산.
        units = _split_units(child_dirs, procs0)
        n_units = len(units)
        procs = max(1, min(int(processes or 1), n_units))
        top_acc: dict = {}
        with ProcessPoolExecutor(max_workers=procs) as ex:
            futs = {}
            for j, (upath, top, rec) in enumerate(units):
                sp = (_remap(upath, root, node_mounts[j % len(node_mounts)])
                      if node_mounts else upath)
                futs[ex.submit(scan_subtree, sp, size_mode, 0,
                               tpp if rec else 1, rec, deadline)] = top
            done = 0
            for fut in as_completed(futs):
                top = futs[fut]
                try:
                    _, b, f, d, e = fut.result()
                except Exception:           # noqa: BLE001 — 한 단위 실패가 전체를 막지 않음
                    b = f = d = 0
                    e = 1
                total_bytes += b
                total_files += f
                total_dirs += d
                errors += e
                a = top_acc.setdefault(top, [0, 0, 0])
                a[0] += b
                a[1] += f
                a[2] += d
                done += 1
                if on_progress:
                    on_progress({"done": done, "units": n_units,
                                 "elapsed": time.time() - t0})
        for top, v in top_acc.items():
            per_top.append({"path": top, "bytes": v[0], "files": v[1], "dirs": v[2]})
    else:
        procs = 1

    per_top.sort(key=lambda x: x["bytes"], reverse=True)
    elapsed = time.time() - t0
    return {
        "ok": True, "root": root, "processes": procs, "units": n_units,
        "threads_per_proc": tpp,
        "size_mode": size_mode, "node_mounts": list(node_mounts or []),
        "total_bytes": total_bytes, "total_files": total_files, "total_dirs": total_dirs,
        "error_count": errors, "elapsed": elapsed,
        "dirs_per_sec": (total_dirs / elapsed) if elapsed > 0 else 0,
        "files_per_sec": (total_files / elapsed) if elapsed > 0 else 0,
        "per_top": per_top,
    }


def write_run_db(db_path: str, root: str, result: dict, *,
                 size_mode: str = "disk", started: Optional[float] = None) -> int:
    """parallel_scan 결과(루트 + 1단계 자식)를 표준 per-run DB 로 기록한다.

    pscan 은 깊은 트리를 만들지 않으므로 directories 에 **루트 + 1단계 자식만** 넣는다
    (용량 개요 + 1단계 드릴다운까지 표시, 더 깊은 드릴다운은 없음). 대시보드/매니저가
    스레드 스캐너 결과와 동일하게 읽을 수 있다. 생성된 run_id 를 반환한다.
    """
    import socket

    from . import __version__
    from . import db as dbmod

    root = os.path.abspath(root)
    started = float(started or time.time())
    now = time.time()
    per_top = result.get("per_top", [])
    total_bytes = int(result.get("total_bytes", 0))
    total_files = int(result.get("total_files", 0))
    total_dirs = int(result.get("total_dirs", 0))
    child_bytes = sum(int(c.get("bytes", 0)) for c in per_top)
    child_files = sum(int(c.get("files", 0)) for c in per_top)
    root_own = max(0, total_bytes - child_bytes)
    root_files = max(0, total_files - child_files)
    _dcols = ("run_id,parent_id,path,name,depth,status,file_count,subdir_count,"
              "own_bytes,total_bytes,total_files,scanned_at")
    _ph = "?,?,?,?,?,?,?,?,?,?,?,?"

    dbmod.init_db(db_path)
    conn = dbmod.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO scan_runs (root_path,status,phase,backend,size_mode,started_at,"
            "updated_at,finished_at,total_dirs,processed_dirs,discovered_dirs,scanned_bytes,"
            "total_files,workers,hostname,app_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (root, "done", "done", "pscan", size_mode, started, now, now, total_dirs,
             len(per_top), total_dirs, total_bytes, total_files,
             int(result.get("processes", 0)), socket.gethostname(), __version__))
        run_id = conn.execute("SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO directories (%s) VALUES (%s)" % (_dcols, _ph),
            (run_id, None, root, os.path.basename(root.rstrip("/")) or root, 0, "done",
             root_files, len(per_top), root_own, total_bytes, total_files, now))
        root_id = conn.execute(
            "SELECT id FROM directories WHERE run_id=? AND parent_id IS NULL", (run_id,)
        ).fetchone()[0]
        for c in per_top:
            cp = c.get("path", "")
            conn.execute(
                "INSERT INTO directories (%s) VALUES (%s)" % (_dcols, _ph),
                (run_id, root_id, cp, os.path.basename(cp.rstrip("/")) or cp, 1, "done",
                 int(c.get("files", 0)), max(0, int(c.get("dirs", 0)) - 1),
                 int(c.get("bytes", 0)), int(c.get("bytes", 0)), int(c.get("files", 0)), now))
        conn.commit()
        return run_id
    finally:
        conn.close()

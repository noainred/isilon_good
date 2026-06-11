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


def scan_subtree(path: str, size_mode: str = "disk", per_file_work: int = 0) -> tuple:
    """path 아래 전체(자기 포함)를 재귀로 훑어 (path, bytes, files, dirs, errors).

    프로세스 워커가 호출하는 모듈 레벨 함수(피클 가능). 스택 기반 반복(재귀 깊이 무제한).
    per_file_work>0 이면 파일마다 그만큼 CPU 연산을 더 한다(벤치마크 전용 — 실제
    스캐너의 '파일당 파이썬 처리 비용'을 모사해 GIL 효과를 보이기 위함).
    """
    total_bytes = 0
    files = 0
    dirs = 0
    errors = 0
    acc = 0
    stack = [path]
    while stack:
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
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return (path, total_bytes, files, dirs, errors)


def _remap(child: str, root: str, node_mount: str) -> str:
    """child 경로의 root 접두사를 node_mount 로 치환(멀티노드 분산용)."""
    rel = os.path.relpath(child, root)
    return os.path.join(node_mount, rel) if rel != "." else node_mount


def parallel_scan(root: str, *, processes: int = 4, size_mode: str = "disk",
                  node_mounts: Optional[List[str]] = None,
                  on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """root 를 1단계 자식 단위로 쪼개 processes 개 프로세스로 병렬 스캔한다."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근 불가: %s" % root}
    t0 = time.time()

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
        procs = max(1, min(int(processes or 1), n_units))
        # 단위별 스캔 경로(멀티노드면 노드 마운트로 치환), 라벨은 표준 경로 유지
        submit_paths = []
        for i, c in enumerate(child_dirs):
            sp = _remap(c, root, node_mounts[i % len(node_mounts)]) if node_mounts else c
            submit_paths.append((sp, c))
        with ProcessPoolExecutor(max_workers=procs) as ex:
            futs = {ex.submit(scan_subtree, sp, size_mode): label
                    for sp, label in submit_paths}
            done = 0
            for fut in as_completed(futs):
                label = futs[fut]
                try:
                    _, b, f, d, e = fut.result()
                except Exception:           # noqa: BLE001 — 한 단위 실패가 전체를 막지 않음
                    b = f = d = 0
                    e = 1
                total_bytes += b
                total_files += f
                total_dirs += d
                errors += e
                per_top.append({"path": label, "bytes": b, "files": f, "dirs": d})
                done += 1
                if on_progress:
                    on_progress({"done": done, "units": n_units,
                                 "elapsed": time.time() - t0})
    else:
        procs = 1

    per_top.sort(key=lambda x: x["bytes"], reverse=True)
    elapsed = time.time() - t0
    return {
        "ok": True, "root": root, "processes": procs, "units": n_units,
        "size_mode": size_mode, "node_mounts": list(node_mounts or []),
        "total_bytes": total_bytes, "total_files": total_files, "total_dirs": total_dirs,
        "error_count": errors, "elapsed": elapsed,
        "dirs_per_sec": (total_dirs / elapsed) if elapsed > 0 else 0,
        "files_per_sec": (total_files / elapsed) if elapsed > 0 else 0,
        "per_top": per_top,
    }

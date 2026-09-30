"""테스트용 샘플 디렉터리/파일 생성기.

지정한 디렉터리 아래에 N개의 디렉터리, 각 디렉터리에 M개의 하위 디렉터리,
그리고 각 폴더(상위·하위 모두)에 K개의 파일(크기 S 바이트)을 만들어 스캐너
시험용 트리를 생성한다. 실수로 디스크를 채우거나 시스템 경로에 쓰는 것을 막기 위해
총량 상한·디스크 여유·시스템 경로 거부를 검증한다.
"""

import os
import threading
from typing import Optional

CHUNK = 1024 * 1024          # 파일 쓰기 버퍼(1MB)
MAX_TOTAL_FILES = 50_000_000  # 안전 상한(실수로 디스크/아이노드 소진 방지)

# 생성을 거부할 시스템 경로(정확히 이 경로면 거부)
_DENY = {"/", "/bin", "/sbin", "/usr", "/lib", "/lib64", "/etc", "/boot",
         "/sys", "/proc", "/dev", "/run", "/var", "/root", "/home", "/tmp"}


def plan_of(n_dirs: int, n_subdirs: int, n_files: int, file_size: int) -> dict:
    """총 디렉터리/파일/용량 계산. 각 폴더(상위 N + 하위 N*M)마다 K개 파일."""
    n_dirs = max(0, int(n_dirs))
    n_subdirs = max(0, int(n_subdirs))
    n_files = max(0, int(n_files))
    file_size = max(0, int(file_size))
    folders = n_dirs + n_dirs * n_subdirs
    total_files = folders * n_files
    return {
        "n_dirs": n_dirs, "n_subdirs": n_subdirs,
        "n_files": n_files, "file_size": file_size,
        "total_dirs": folders,
        "total_files": total_files,
        "total_bytes": total_files * file_size,
    }


def validate(base: str, n_dirs: int, n_subdirs: int, n_files: int, file_size: int):
    """(ok, error, plan) 반환. 안전/용량/권한을 점검한다."""
    base = os.path.abspath(base or "")
    plan = plan_of(n_dirs, n_subdirs, n_files, file_size)
    plan["base"] = base
    if not base or base in _DENY:
        return False, "안전하지 않은 경로입니다(시스템 경로에는 생성할 수 없습니다).", plan
    if plan["n_dirs"] < 1:
        return False, "디렉터리 수는 1 이상이어야 합니다.", plan
    if plan["total_files"] > MAX_TOTAL_FILES:
        return False, ("총 파일 수가 너무 많습니다(%d > 상한 %d)."
                       % (plan["total_files"], MAX_TOTAL_FILES)), plan
    # 부모 경로 쓰기 권한
    parent = base if os.path.isdir(base) else (os.path.dirname(base) or "/")
    if not os.path.isdir(parent):
        return False, "상위 디렉터리가 없습니다: %s" % parent, plan
    if not os.access(parent, os.W_OK):
        return False, "쓰기 권한이 없습니다: %s" % parent, plan
    # 디스크 여유
    try:
        v = os.statvfs(parent)
        free = v.f_bavail * v.f_frsize
        if plan["total_bytes"] > free:
            return False, ("디스크 여유 부족(필요 %d > 여유 %d)"
                           % (plan["total_bytes"], free)), plan
    except OSError:
        pass
    return True, None, plan


def generate(base: str, n_dirs: int, n_subdirs: int, n_files: int, file_size: int,
             *, prefix: str = "iutest", progress_cb=None,
             stop_event: Optional[threading.Event] = None) -> dict:
    """샘플 트리를 생성하고 결과(생성 수)를 반환한다."""
    ok, why, plan = validate(base, n_dirs, n_subdirs, n_files, file_size)
    if not ok:
        return {"ok": False, "error": why, "plan": plan}
    base = plan["base"]
    n_dirs, n_subdirs = plan["n_dirs"], plan["n_subdirs"]
    n_files, file_size = plan["n_files"], plan["file_size"]
    os.makedirs(base, exist_ok=True)
    st = {"created_dirs": 0, "created_files": 0, "written_bytes": 0}
    chunk = b"\0" * min(max(1, file_size), CHUNK) if file_size > 0 else b""

    def _stopped():
        return stop_event is not None and stop_event.is_set()

    def _write_files(folder):
        for fi in range(n_files):
            if _stopped():
                return
            fp = os.path.join(folder, "file_%05d.dat" % fi)
            remaining = file_size
            try:
                with open(fp, "wb") as fh:
                    while remaining > 0:
                        w = chunk if remaining >= len(chunk) else b"\0" * remaining
                        fh.write(w)
                        remaining -= len(w)
                st["created_files"] += 1
                st["written_bytes"] += file_size
            except OSError:
                pass

    for i in range(n_dirs):
        if _stopped():
            break
        d = os.path.join(base, "%s_%05d" % (prefix, i))
        os.makedirs(d, exist_ok=True)
        st["created_dirs"] += 1
        _write_files(d)
        for j in range(n_subdirs):
            if _stopped():
                break
            sd = os.path.join(d, "sub_%05d" % j)
            os.makedirs(sd, exist_ok=True)
            st["created_dirs"] += 1
            _write_files(sd)
        if progress_cb:
            progress_cb(dict(st))

    result = {"ok": True, "base": base, "plan": plan, "stopped": _stopped()}
    result.update(st)
    return result

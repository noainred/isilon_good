"""atimes.py — 특정 폴더와 그 안 항목의 '마지막 접근시각(atime)' 라이브 조회.

⚠ 용량 계산(scanner.py)과는 **완전히 분리된 별도 로직**이다(사용자 요청):
- 기존 용량 스캔은 최적화돼 있으므로 건드리지 않는다. 여기서는 지정한 '한 폴더'를 그 자리에서
  가볍게 stat 해 항목별 atime/mtime/크기/소유자(uid)만 모은다. 집계·DB·스캔 락을 쓰지 않는다.
- 스캔 락/커넥션을 잡지 않으므로, ThreadingHTTPServer 위에서 진행 중인 용량 스캔과 **동시에** 돌 수 있다.
- noatime 마운트면 atime 이 무의미하므로, 호출측이 systune.atime_policy 로 신뢰도를 함께 안내한다.
"""
from __future__ import annotations

import os

DEFAULT_LIMIT = 2000
MAX_LIMIT = 20000


def list_access_times(path: str, *, recursive: bool = False,
                      limit: int = DEFAULT_LIMIT) -> dict:
    """path 폴더(옵션: 하위 포함)의 항목을 atime(오래된 순)으로 정렬해 반환.

    반환: {ok, path, dir_atime, dir_mtime, entries:[{name,path,is_dir,size,atime,mtime,uid}],
           count, truncated, recursive}  또는 {ok:False, error, path}
    """
    path = os.path.abspath(path or "")
    if not os.path.isdir(path):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다.", "path": path}
    try:
        dst = os.stat(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": path}

    limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    entries: list = []
    truncated = False

    def _row(p: str, st, is_dir: bool) -> dict:
        return {"name": os.path.basename(p) or p, "path": p, "is_dir": bool(is_dir),
                "size": int(st.st_size), "atime": float(st.st_atime),
                "mtime": float(st.st_mtime), "uid": int(st.st_uid)}

    if recursive:
        # followlinks=False: 심볼릭 링크 디렉터리는 따라가지 않는다(루프·허용경로 이탈 방지).
        for root, dirs, files in os.walk(path):
            dirs.sort()
            for nm in sorted(files):
                fp = os.path.join(root, nm)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                entries.append(_row(fp, st, False))
                if len(entries) >= limit:
                    truncated = True
                    break
            if truncated:
                break
    else:
        try:
            with os.scandir(path) as it:
                for de in it:
                    try:
                        st = de.stat()
                    except OSError:
                        continue
                    entries.append(_row(de.path, st, de.is_dir(follow_symlinks=False)))
                    if len(entries) >= limit:
                        truncated = True
                        break
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": path}

    entries.sort(key=lambda e: e["atime"])   # 오래 접근 안 한(콜드) 항목이 위로
    return {"ok": True, "path": path, "dir_atime": float(dst.st_atime),
            "dir_mtime": float(dst.st_mtime), "entries": entries,
            "count": len(entries), "truncated": truncated, "recursive": bool(recursive)}

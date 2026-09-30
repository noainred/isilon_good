"""dups.py — 내용 기반 중복 파일 탐지(조회 전용·옵션).

용량 스캔(scanner.py)과 **완전히 분리된 별도 패스**다. 파일 내용을 read 하는
I/O 가 무거우므로 본 스캔에 통합하지 않는다(통합하면 모든 파일을 read 하게 되어
GIL+단일 _dlock 병목을 직격). 호출측이 '옵션(기본 off)'으로만 돌린다.

3단계 깔때기로 read I/O·메모리를 최소화한다:
  1) 크기별 '개수'만 집계 → 같은 크기 후보(2개 이상)만 남긴다(메모리 작음).
  2) 후보 크기 파일만 '부분 해시'(앞 partial_bytes) → 앞부분 다른 파일은 즉시 배제.
  3) 부분해시가 같은 그룹만 '전체 해시' → 내용이 진짜 같은 파일만 중복으로 본다.
하드링크(같은 dev,ino)는 디스크를 추가로 쓰지 않으므로 한 번만 센다(원본/사본 중복 아님).
회수 가능 용량 = Σ 그룹별 size*(중복수-1). 파일은 절대 변경/삭제하지 않는다(조회 전용).

표준 라이브러리만 사용(blake2b). noatime 무관(내용 해시).
"""
from __future__ import annotations

import hashlib
import os
import stat as _stat
from typing import Optional


def _hash_partial(path: str, n: int = 4096) -> Optional[bytes]:
    """앞 n바이트만 해시(빠른 1차 필터). 읽기 실패 시 None."""
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(n))
    except OSError:
        return None
    return h.digest()


def _hash_full(path: str, chunk: int = 1 << 20) -> Optional[str]:
    """파일 전체를 스트리밍 해시(메모리 상한 = chunk). 읽기 실패 시 None."""
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            for b in iter(lambda: fh.read(chunk), b""):
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def find_duplicates(root: str, *, min_size: int = 4096, partial_bytes: int = 4096,
                    max_files: int = 2_000_000, top_groups: int = 200,
                    stop_event=None) -> dict:
    """root 아래 '내용이 같은' 파일 그룹과 회수 가능 용량을 찾는다(파일 미변경·조회 전용).

    반환: {ok, root, total_recoverable, group_count, dup_file_count,
           groups:[{hash,size,count,recoverable,paths(최대10)}], truncated, scanned_files}
           또는 {ok:False, error, root}.
    """
    root = os.path.abspath(root or "")
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다.", "root": root}

    def _stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    # 1단계: 크기 → 개수(메모리 작음). 하드링크 제외. 대용량은 max_files 상한(초과 시 truncated).
    size_count: dict = {}
    seen: set = set()
    scanned = 0
    truncated = False
    for dp, dirs, files in os.walk(root):   # followlinks=False(기본): 심볼릭 디렉터리 안 따라감
        dirs.sort()
        for nm in sorted(files):
            fp = os.path.join(dp, nm)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode) or st.st_size < min_size:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:               # 하드링크는 한 번만(추가 디스크 사용 아님)
                continue
            seen.add(key)
            size_count[st.st_size] = size_count.get(st.st_size, 0) + 1
            scanned += 1
            if scanned >= max_files:
                truncated = True
                break
        if truncated or _stopped():
            break
    cand = {s for s, c in size_count.items() if c > 1}

    # 2단계: 후보 크기 파일만 부분 해시 → (size, 부분해시) 그룹.
    # 해시 I/O(open+read)도 max_files 로 상한을 둔다 — 1단계만 제한하면 6PB·수백만 후보에서
    # 2·3단계 read I/O 가 무제한 폭주(디스크·시간)한다. 상한 도달 시 truncated 로 정직히 표기.
    by_partial: dict = {}
    seen2: set = set()
    hashed = 0
    cap2 = False
    for dp, dirs, files in os.walk(root):
        if _stopped() or cap2:
            break
        dirs.sort()
        for nm in sorted(files):
            fp = os.path.join(dp, nm)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode) or st.st_size not in cand:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen2:
                continue
            seen2.add(key)
            if hashed >= max_files:      # 해시한 후보 수 상한(2·3단계 I/O 폭주 방지)
                cap2 = True
                truncated = True
                break
            ph = _hash_partial(fp, partial_bytes)
            hashed += 1
            if ph is None:
                continue
            by_partial.setdefault((st.st_size, ph), []).append(fp)

    # 3단계: 부분해시 그룹(2개 이상)만 전체 해시 → 진짜 중복.
    groups: list = []
    for (size, _ph), paths in by_partial.items():
        if len(paths) < 2 or _stopped():
            continue
        by_full: dict = {}
        for p in paths:
            fh = _hash_full(p)
            if fh is None:
                continue
            by_full.setdefault(fh, []).append(p)
        for fh, ps in by_full.items():
            if len(ps) >= 2:
                groups.append({
                    "hash": fh, "size": int(size), "count": len(ps),
                    "recoverable": int(size) * (len(ps) - 1),
                    "paths": sorted(ps)[:10],
                })
    groups.sort(key=lambda g: -g["recoverable"])
    total = sum(g["recoverable"] for g in groups)
    return {
        "ok": True, "root": root,
        "total_recoverable": int(total),
        "group_count": len(groups),
        "dup_file_count": sum(g["count"] for g in groups),
        "groups": groups[:top_groups],
        "truncated": truncated,
        "scanned_files": scanned,
    }

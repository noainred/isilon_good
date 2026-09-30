"""감사 로그 — 로그인/변경 작업을 ``<data-dir>/audit.log`` 에 JSONL 로 남긴다.

스캐너·포탈 공용. 누가(=출처 IP) 언제 무엇을(로그인·설정 변경·스캔 시작·노드 등록 등)
했는지, 성공/실패를 한 줄씩 기록한다. 베스트에포트 — 기록 실패가 동작을 막지 않는다.
"""

from __future__ import annotations

import json
import os
import time
from typing import List

AUDIT_FILE = "audit.log"
MAX_BYTES = 5 * 1024 * 1024   # 5MB 넘으면 한 번 회전(.1 로)


def audit_path(data_dir: str) -> str:
    return os.path.join(data_dir, AUDIT_FILE)


def record(data_dir: str, *, action: str, ok: bool, ip: str = "", detail: str = "") -> None:
    """감사 항목 한 줄 추가(JSONL)."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = audit_path(data_dir)
        try:
            if os.path.getsize(path) > MAX_BYTES:
                os.replace(path, path + ".1")     # 단순 1회 회전
        except OSError:
            pass
        line = json.dumps({"ts": round(time.time(), 3), "action": str(action),
                           "ok": bool(ok), "ip": str(ip or ""), "detail": str(detail or "")},
                          ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def tail(data_dir: str, limit: int = 200) -> List[dict]:
    """최근 limit 개 감사 항목을 최신순(내림차순)으로 돌려준다."""
    try:
        with open(audit_path(data_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-int(limit):]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out

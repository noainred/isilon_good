"""coldtier.py — 콜드데이터(오래 안 쓴 파일) '이동 계획·스크립트' 생성(조회 전용).

⚠ 이 도구는 **파일을 절대 옮기거나 지우지 않는다.** 지정한 루트를 가볍게 walk·stat 해
   '얼마나 콜드한지'(나이 기준 초과 용량/개수/상위 디렉터리)를 측정(미리보기)하고,
   관리자가 **검토 후 직접 실행**할 셸 스크립트(매니페스트·롤백 포함)를 텍스트로만 만든다.
   (읽기 전용 철학 유지 — 사용자 승인 2026-06-27 "계획·스크립트만 생성".)

설계(6PB·수백만 파일 규모에 맞춤):
- 미리보기: 용량 스캔(scanner.py)과 분리된 별도 walk(스캔 락 미사용 → 동시 실행 가능,
  dups/atimes 와 같은 패턴). 하드링크(같은 dev,ino)는 물리 블록 1회분이라 한 번만 센다.
  max_files 상한 초과 시 truncated 로 정직하게 표기. 콜드 판정은 생성 스크립트의 `find
  -mtime/-atime +N`(정수 일 단위, age>N) 의미와 **동일**하게 맞춘다(미리보기·스크립트 불일치 방지).
- 생성 스크립트: 경로를 인라인으로 박지 않는다. 실행 시점에 `find <SRC> -xdev -type f
  -<atime|mtime> +<DAYS> -printf '%P\\0'` 로 콜드 파일을 재발견 → `rsync -aH --ignore-existing
  --from0 --files-from=- --remove-source-files` 로 이동(임의 파일명 NUL 안전·트리/하드링크/메타 보존).
  **--ignore-existing**: 대상에 이미 같은 경로가 있으면 건드리지도 삭제하지도 않는다(다른 내용을
  덮어쓰거나 원본을 날리는 사고 방지). 기본 DRY_RUN=1(아무것도 안 옮김), set -euo pipefail,
  -xdev(마운트 경계 안 넘음). 매니페스트는 **플랜별 고유 절대경로**(여러 플랜·다른 CWD 충돌 방지).
- 롤백 스크립트: SRC↔DST 만 바꾼 동일 구조(같은 매니페스트로 이동된 트리를 그대로 되돌림).

표준 라이브러리만 사용.
"""
from __future__ import annotations

import hashlib
import os
import stat as _stat
import time

VALID_FIELDS = ("atime", "mtime")


def _shq(s: str) -> str:
    """문자열을 셸 단일따옴표로 안전하게 감싼다(임의 특수문자 허용, ' 만 이스케이프)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _find_flag(field: str) -> str:
    return "-atime" if field == "atime" else "-mtime"


def _manifest_path(root: str, target: str, field: str, days: int) -> str:
    """플랜(root·target·field·days)별 고유 절대 매니페스트 경로.

    CWD 상대·고정 이름이면 다른 플랜·다른 CWD 에서 서로 덮어써 롤백 충실성이 깨진다.
    플랜 입력으로 안정적 해시를 만들어 충돌을 없앤다(같은 플랜의 이동·롤백은 같은 파일을 가리킴).
    """
    raw = f"{root}\0{target}\0{field}\0{int(days)}".encode("utf-8", "surrogateescape")
    return "/var/tmp/coldmove_manifest_" + hashlib.sha1(raw).hexdigest()[:12] + ".lst"


def build_move_script(root: str, target: str, days: int, field: str,
                      min_size: int = 0) -> str:
    """콜드데이터 이동 셸 스크립트 텍스트 생성(기본 DRY-RUN·비파괴, 관리자가 검토 후 실행)."""
    src_q, dst_q = _shq(root), _shq(target)
    field = field if field in VALID_FIELDS else "mtime"
    # find -size +Nc 는 'N 초과'라 미리보기(>= min_size)와 1바이트 어긋난다 → +{min_size-1}c 로 맞춤.
    size_clause = f" -size +{int(min_size) - 1}c" if min_size and int(min_size) > 0 else ""
    mf = _manifest_path(root, target, field, days)
    return f"""#!/usr/bin/env bash
# 콜드데이터 이동 스크립트 — isilon_usage 가 생성(검토 후 실행). 기본 DRY-RUN(미리보기).
#   기준: {field} 가 {int(days)}일보다 오래된 정규 파일{(' / ' + str(int(min_size)) + 'B 이상') if size_clause else ''}.
#   실제로 옮기려면:  DRY_RUN=0 bash 이_스크립트.sh
#   되돌리려면 함께 받은 롤백 스크립트를 실행한다.
set -euo pipefail

SRC={src_q}
DST={dst_q}
DAYS={int(days)}
DRY_RUN="${{DRY_RUN:-1}}"                 # 1=미리보기(아무것도 안 옮김), 0=실제 이동
MANIFEST="${{MANIFEST:-{mf}}}"            # 플랜별 고유 경로(여러 플랜 충돌 방지)

[ -d "$SRC" ] || {{ echo "SRC 디렉터리 없음: $SRC" >&2; exit 1; }}
command -v rsync >/dev/null 2>&1 || {{ echo "rsync 필요(설치 후 재실행)" >&2; exit 1; }}
mkdir -p -- "$DST"

# 1) 콜드 파일 목록(매니페스트, NUL 구분) — SRC 기준 상대경로(%P).
#    미리보기(DRY-RUN)는 임시 매니페스트를 쓰고 끝나면 지운다(실제 기록·롤백 매니페스트를 남기지/막지 않음).
#    실제 이동은 플랜별 고유 매니페스트(MANIFEST)에 기록하고, 기존 기록이 있으면 보호 위해 덮어쓰지 않는다.
if [ "$DRY_RUN" = "1" ]; then
  WORK="$(mktemp "${{TMPDIR:-/tmp}}/coldmove_dry.XXXXXX")"
else
  [ -e "$MANIFEST" ] && {{ echo "기존 매니페스트 있음: $MANIFEST (이전 기록 보호). 지우거나 MANIFEST=... 지정 후 재실행." >&2; exit 1; }}
  WORK="$MANIFEST"
fi
find "$SRC" -xdev -type f {_find_flag(field)} +"$DAYS"{size_clause} -printf '%P\\0' > "$WORK"
COUNT=$(tr -cd '\\0' < "$WORK" | wc -c | tr -d ' ')
echo "콜드 파일 ${{COUNT}}개 (기준 {field} > ${{DAYS}}일).  매니페스트: $WORK"
[ "$COUNT" -gt 0 ] || {{ echo "옮길 콜드 파일 없음. 종료."; [ "$DRY_RUN" = "1" ] && rm -f -- "$WORK"; exit 0; }}

# 2) rsync 로 이동(하드링크/메타/트리 보존). --ignore-existing: 대상에 이미 있는 경로는
#    건드리지도 삭제하지도 않는다(다른 내용 덮어쓰기·원본 유실 방지). --remove-source-files 는
#    '전송 성공한 파일만' 삭제하므로 스킵된(이미 있는) 파일의 원본은 보존된다.
RSYNC=(rsync -aH --ignore-existing --from0 --files-from="$WORK" --remove-source-files)
if [ "$DRY_RUN" = "1" ]; then
  RSYNC+=(--dry-run --itemize-changes)
  echo "[DRY-RUN] 실제로는 아무것도 옮기지 않는다. 실행하려면 DRY_RUN=0."
fi
"${{RSYNC[@]}}" "$SRC"/ "$DST"/

if [ "$DRY_RUN" = "1" ]; then
  rm -f -- "$WORK"
else
  echo "이동 완료. 빈 디렉터리는 남을 수 있다(데이터 보존 위해 자동 삭제 안 함)."
  echo "되돌리려면 롤백 스크립트를 실행하라(같은 매니페스트 $MANIFEST 사용)."
fi
"""


def build_rollback_script(root: str, target: str, days: int = 0,
                          field: str = "mtime") -> str:
    """이동을 되돌리는 롤백 스크립트(DST→SRC). 같은(플랜별 고유) 매니페스트를 그대로 사용."""
    src_q, dst_q = _shq(root), _shq(target)
    field = field if field in VALID_FIELDS else "mtime"
    mf = _manifest_path(root, target, field, days)
    return f"""#!/usr/bin/env bash
# 콜드데이터 이동 롤백 — DST 의 파일을 원래 SRC 로 되돌린다(같은 매니페스트 사용).
#   되돌리려면:  DRY_RUN=0 bash 이_롤백.sh
set -euo pipefail

SRC={src_q}
DST={dst_q}
DRY_RUN="${{DRY_RUN:-1}}"
MANIFEST="${{MANIFEST:-{mf}}}"

[ -f "$MANIFEST" ] || {{ echo "매니페스트 없음: $MANIFEST (이동 때 만든 파일 필요)" >&2; exit 1; }}
command -v rsync >/dev/null 2>&1 || {{ echo "rsync 필요" >&2; exit 1; }}
mkdir -p -- "$SRC"

RSYNC=(rsync -aH --ignore-existing --from0 --files-from="$MANIFEST" --remove-source-files)
if [ "$DRY_RUN" = "1" ]; then
  RSYNC+=(--dry-run --itemize-changes)
  echo "[DRY-RUN] 미리보기. 실제 되돌리려면 DRY_RUN=0."
fi
"${{RSYNC[@]}}" "$DST"/ "$SRC"/
echo "롤백 처리 끝(DRY_RUN=$DRY_RUN)."
"""


def _overlaps(root: str, target: str) -> bool:
    """root 와 target 이 (심볼릭 링크 해소 후) 서로의 하위이거나 같은가 — 자기 자신으로 이동 위험."""
    a = os.path.realpath(root)
    b = os.path.realpath(target)
    if a == b:
        return True
    return (b + os.sep).startswith(a.rstrip(os.sep) + os.sep) or \
           (a + os.sep).startswith(b.rstrip(os.sep) + os.sep)


def plan_cold_move(root: str, *, days: int = 365, field: str = "mtime",
                   target: str = "", min_size: int = 0,
                   max_files: int = 2_000_000, top_dirs: int = 50,
                   stop_event=None) -> dict:
    """root 아래 '나이 기준 초과(콜드)' 파일의 용량/개수/상위 디렉터리와 이동 스크립트를 만든다.

    파일은 절대 변경/삭제하지 않는다(조회 전용·미리보기). 콜드 판정은 생성 스크립트의
    `find -mtime/-atime +N`(정수 일 단위, age>N) 의미와 동일. 하드링크는 한 번만 집계.
    반환:
      {ok, root, target, days, field, min_size,
       total_cold_bytes, cold_file_count, scanned_files, truncated,
       oldest_age_days, top_dirs:[{dir, bytes, count}],
       script, rollback}  또는 {ok:False, error, root}
    """
    root = os.path.abspath(root or "")
    field = field if field in VALID_FIELDS else "mtime"
    try:
        days = max(0, int(days))
    except (TypeError, ValueError):
        days = 365
    try:
        min_size = max(0, int(min_size))
    except (TypeError, ValueError):
        min_size = 0
    if not os.path.isdir(root):
        return {"ok": False, "error": "디렉터리가 아니거나 접근할 수 없습니다.", "root": root}

    target = os.path.abspath(target) if target else ""
    if target and _overlaps(root, target):
        # 타깃과 원본(루트)이 (심볼릭 링크 포함) 서로 겹치면 자기 자신으로 옮기는 위험 — 거부.
        return {"ok": False, "error": "타깃과 원본(루트)이 서로 겹치면 안 됩니다(하위·동일·심볼릭 포함).",
                "root": root}

    def _stopped() -> bool:
        return bool(stop_event is not None and stop_event.is_set())

    now = time.time()
    total_bytes = 0
    cold_count = 0
    scanned = 0
    truncated = False
    oldest = 0.0
    dir_bytes: dict = {}
    dir_count: dict = {}
    seen_inodes: set = set()
    rlen = len(root.rstrip(os.sep))

    for dp, dirs, files in os.walk(root):   # followlinks=False: 심볼릭 디렉터리 안 따라감
        if _stopped():
            break
        dirs.sort()
        for nm in sorted(files):
            fp = os.path.join(dp, nm)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            if scanned >= max_files:        # 상한 도달: 다음 파일 평가 전에 멈춤(scanned 와 평가수 일치)
                truncated = True
                break
            scanned += 1
            # 하드링크(같은 dev,ino)는 물리 블록 1회분 — 회수 가능 용량 과대계상 방지로 한 번만 센다.
            ikey = (st.st_dev, st.st_ino)
            if ikey in seen_inodes:
                continue
            seen_inodes.add(ikey)
            if min_size and st.st_size < min_size:
                continue
            t = float(st.st_atime if field == "atime" else st.st_mtime)
            # find -mtime/-atime +N 과 동일: 나이를 정수 일로 내림 후 'days 초과'만 콜드로 본다.
            if (now - t) // 86400 <= days:
                continue
            age = (now - t) / 86400.0
            if age > oldest:
                oldest = age
            total_bytes += int(st.st_size)
            cold_count += 1
            # 루트 바로 아래 1단계 하위 디렉터리로 집계(요약). 루트 직속 파일은 "(루트)".
            rel = dp[rlen:].lstrip(os.sep)
            top = rel.split(os.sep, 1)[0] if rel else "(루트)"
            dir_bytes[top] = dir_bytes.get(top, 0) + int(st.st_size)
            dir_count[top] = dir_count.get(top, 0) + 1
        if truncated or _stopped():
            break

    tops = sorted(({"dir": k, "bytes": dir_bytes[k], "count": dir_count[k]}
                   for k in dir_bytes), key=lambda d: -d["bytes"])[:max(1, int(top_dirs))]

    out = {
        "ok": True, "root": root, "target": target, "days": days, "field": field,
        "min_size": min_size, "total_cold_bytes": int(total_bytes),
        "cold_file_count": int(cold_count), "scanned_files": int(scanned),
        "truncated": truncated, "oldest_age_days": round(oldest, 1), "top_dirs": tops,
    }
    if target:
        out["script"] = build_move_script(root, target, days, field, min_size)
        out["rollback"] = build_rollback_script(root, target, days, field)
    return out

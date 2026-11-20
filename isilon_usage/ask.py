"""ask.py — 규칙 기반(오프라인) 자연어 질의응답 + 선택적 로컬 LLM 표현 레이어.

설계 원칙(저장소 일관 정책):
- **숫자는 규칙 엔진이 결정적으로 계산한다.** 용량·개수·Top 같은 값은 여기서 직접 집계하므로
  환각(지어낸 수치)이 끼어들 여지가 없다.
- **로컬 LLM은 선택적 '표현' 레이어다.** 같은 사실(facts)을 컨텍스트로 받아 문장만 자연스럽게
  다듬는다. 데이터는 사내 GPU(OpenAI 호환 엔드포인트)에서만 처리 — 외부로 나가지 않는다.
- **엣지/포탈이 같은 엔진을 쓴다.** 화면마다 로직을 복제하지 않고, 호출자가 '가진 데이터'만
  공통 형태(data)로 넘기면 된다(엣지=루트+선택 스캔 상세, 포탈=노드 롤업).

data 형태(호출자가 가진 것만 채운다):
  data = {
    "scope": "edge" | "portal",
    "overall": {                      # 둘 다(정규화된 공통 형태)
        "total_scanned_bytes": int, "root_count": int, "scan_count": int,
        "active_scans": int,
        "roots": [ {"label": str, "root_path": str, "scanned_bytes": int,
                    "fs_used_bytes": int, "fs_total_bytes": int,
                    "total_files": int, "status": str, "finished_at": float|None} ],
    },
    "detail": {                       # 엣지(선택 스캔)만 — 없으면 None
        "scan_id": int, "root_path": str,
        "age": [{"key": str, "bytes": int, "files": int}],
        "atime_age": [...], "owners": [{"key","name","bytes","files"}],
        "extensions": [...], "sizes": [...],
        "top_files": [{"path","bytes","mtime","owner"}],
        "topdirs": [{"path","bytes","files"}],
        "forecast": {...},            # forecast_capacity() 반환
    },
  }
"""

import json
import threading
import re
import time
import urllib.request
from typing import Optional

from . import netutil

# 파일 나이 버킷(오래된 순). server.py 의 _AGE_ORDER 와 같은 라벨을 쓴다(통일).
_AGE_ORDER = ["30일 이내", "30~90일", "90일~1년", "1~2년", "2~5년", "5년+"]
# 각 버킷의 '하한 일수'(이상 합산용 근사).
_AGE_LOWER_DAYS = {"30일 이내": 0, "30~90일": 30, "90일~1년": 90,
                   "1~2년": 365, "2~5년": 730, "5년+": 1825}
_UNIT_DAYS = {"일": 1, "주": 7, "주일": 7, "달": 30, "개월": 30, "년": 365, "연": 365}


def fmt_bytes(n) -> str:
    """사람이 읽기 좋은 용량 — 화면·CLI·포탈과 같은 규칙(1024, 소수 1자리). 예전엔 소수 2자리라 같은 값이
    '494.83 MB'(질의응답)·'494.8 MB'(포탈)로 달랐다(29회차 점검)."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}")
        n /= 1024
    return f"{n:.1f} PB"


def fmt_num(n) -> str:
    try:
        return f"{int(n or 0):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_when(ts) -> str:
    if not ts:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return "—"


_KO_NUM = {"한": 1, "하루": 1, "두": 2, "이틀": 2, "세": 3, "사흘": 3, "네": 4, "나흘": 4, "다섯": 5, "여섯": 6,
           "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
# 기간 단위 + '단위 글자로 시작하는 다른 낱말'만 막는 규칙(단위별) — 조사·접미사는 모두 허용('1년된'·'30일지난'·'7일안' 등).
# 31회차: '해외'·'주소'·'해당'·'주요'·'해결'·'주변'을 기간으로 읽던 문제 / 32회차: 허용 글자 목록이 좁아 '3개월된'·'30일지난'을 못 읽던 회귀
_KO_UNIT = (r"(개월|주일|일(?!부|반|치|자|괄|련|정(?!도))|주(?!소|요|변|세|인|목|문|식)|달(?!라|리|성|러|려)"
            r"|년|연(?!도|결|속|말|구|락|장|락))")
_HAE = r"해(?!외|당|결|서|야|주|봐|라|요|줘|놓|둔|버|석|명|제|소|산|변|양|안)"
_EN_UNIT_DAYS = {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365, "years": 365}


def parse_threshold_days(q: str):
    """'30일', '1년 6개월', '한 달', '일주일', '1,000일', '90 days' 같은 표현을 일수로. 없으면 None.

    30회차 점검: '1,000일'을 '000일'(0일)로, '1년 6개월'을 1년으로, '한 달'·'일주일'·'90 days'는 못 읽고 1년으로 답했다."""
    if "반년" in q:   # '반년 이상 안 쓴 파일'이 기본 1년으로 바뀌었다(22회차 점검)
        return 182
    q2 = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", q)          # 천 단위 쉼표
    q2 = re.sub(r"(?<![가-힣])일주일", "1주", q2)
    q2 = re.sub(r"(?<![가-힣])하루", "1일", q2)
    # 단위 뒤가 다른 낱말이면(해외·주소·해당·주요·해결·주변…) 기간이 아니다 — 조사·접미사(간·이상·넘게·전·째…)만 허용
    # (31회차 점검: '상위 5 해외 폴더'를 5년, '세 주소'를 21일로 읽어 엉뚱한 기준으로 답했다)
    for w, n in _KO_NUM.items():                               # '한 달'·'두 주'·'세 해' → 숫자
        q2 = re.sub(r"(?<![가-힣])%s\s*(개월|달(?!라|리|성|러|려)|주일|주(?!소|요|변|세|인|목|문|식)|%s|년)" % (w, _HAE),
                    lambda m, n=n: "%d%s" % (n, m.group(1)), q2)
    q2 = re.sub(r"(\d)\s*" + _HAE, r"\1년", q2)
    q2 = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", q2)      # '1,5년' = 1.5년(소수 쉼표)
    total, found = 0.0, False
    # 소수 허용 — '1.5년'의 '5년'만 읽어 5년으로 답했다(21회차 점검). 여러 단위는 더한다('1년 6개월').
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*" + _KO_UNIT, q2):
        val = float(m.group(1))
        if m.group(2) in ("년", "연") and val >= 1000:
            continue       # '2024년 이후' 같은 연도는 기간이 아니다(예전엔 2024년 = 73만 일로 읽었다)
        total += val * _UNIT_DAYS.get(m.group(2), 1)
        found = True
    if not found:     # 한국어 기간이 있으면 영어 표기는 같은 기간의 다른 표현으로 보고 더하지 않는다('90일(90 days)' → 180 이던 문제)
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(days?|weeks?|months?|years?)\b", q2.lower()):
            total += float(m.group(1)) * _EN_UNIT_DAYS[m.group(2)]
            found = True
    return int(round(total)) if found else None


def _has(q: str, *words: str) -> bool:
    return any(w in q for w in words)


def _sum_age_over(buckets, threshold_days):
    """나이 버킷에서 threshold_days '이상'인 것 합산 → (bytes, files, 포함된 라벨들, 걸친 버킷).

    기준이 버킷 경계가 아니면(예: 6개월=180일 → '90일~1년' 버킷 중간) 그 버킷은 나눌 수 없다.
    합계에는 넣지 않고(=하한) 걸친 버킷을 따로 돌려줘 답변이 '최소~최대' 범위를 밝히게 한다.
    """
    tot_b = tot_f = 0
    labels = []
    straddle = None
    lows = sorted(v for v in _AGE_LOWER_DAYS.values())
    for b in buckets or []:
        low = _AGE_LOWER_DAYS.get(b.get("key"), -1)
        if low < 0:
            continue
        # -1: 30일→'30~90일'부터 포함. 단 하한 0 인 '30일 이내'는 기준이 0일 때만 통째로 — '1일 이상'이 그 버킷 전체를
        # 범위 안내 없이 더했다(21회차 점검). 그 밖엔 아래에서 '걸친 버킷'으로 안내한다.
        if low >= threshold_days or (low > 0 and low >= threshold_days - 1):
            tot_b += int(b.get("bytes") or 0)
            tot_f += int(b.get("files") or 0)
            labels.append(b.get("key"))
        else:
            nxt = next((x for x in lows if x > low), None)   # 이 버킷의 상한(다음 버킷 하한)
            if nxt is not None and nxt > threshold_days:
                straddle = b
    return tot_b, tot_f, labels, straddle


def _range_note(b, straddle) -> str:
    """기준이 버킷 중간에 걸쳤을 때 '최소~최대' 안내 문구(없으면 빈 문자열)."""
    if not straddle or not int(straddle.get("bytes") or 0):
        return ""
    hi = b + int(straddle.get("bytes") or 0)
    return (f" ※ 기준이 ‘{straddle.get('key')}’ 버킷 중간이라 나눌 수 없어 **최소값**입니다 — "
            f"실제는 {fmt_bytes(b)} ~ {fmt_bytes(hi)} 사이.")


# ────────────────────────────── 규칙 기반 핸들러 ──────────────────────────────
# 각 핸들러: (q, data) -> dict | None.  반환 dict = {"answer": str, "facts": {...}}.
# None 이면 '의도 아님' 또는 '이 화면엔 데이터 없음'.

def _detail_unavailable(data):
    """엣지 상세가 필요한 질문인데 포탈/상세없음일 때 안내."""
    if data.get("scope") == "portal":
        return ("이 질문은 디렉터리 상세가 필요합니다 — 포탈은 노드별 합계만 모읍니다. "
                "각 노드(엣지) 대시보드의 ‘분석 리포트’에서 볼 수 있어요.")
    if data.get("detail_note") == "pscan":
        return ("선택된 스캔은 pscan(빠른 멀티프로세스 스캔)이라 루트·1단계 합계만 기록합니다 — 파일 나이·소유자·확장자·Top 파일은 "
                "일반(threads) 스캔에서 볼 수 있어요.")
    return "선택된 스캔의 상세 집계가 아직 없습니다(스캔이 완료되면 채워집니다)."


def _h_total(q, data):
    if not _has(q, "전체", "총", "다 합", "전부", "합계", "토탈", "모두"):
        return None
    if not _has(q, "용량", "사용량", "얼마", "크기", "쓰고", "차지"):
        return None
    ov = data.get("overall") or {}
    # 홈 KPI 와 같은 '조사 용량'(마지막 완료 스캔·중첩 루트 제외) — 예전엔 진행 중 스캔 바이트 합이라 달랐다(24회차 점검)
    b = int(ov.get("total_capacity_bytes") if ov.get("total_capacity_bytes") is not None
            else (ov.get("total_scanned_bytes") or 0))
    facts = {"전체_조사_용량_bytes": b, "전체_조사_용량": fmt_bytes(b),
             "루트_수": ov.get("root_count"), "스캔_수": ov.get("scan_count")}
    return {"answer": f"전체 조사 용량은 **{fmt_bytes(b)}** 입니다 "
                      f"(루트 {fmt_num(ov.get('root_count'))}개 · 스캔 "
                      f"{fmt_num(ov.get('scan_count'))}회 기준)." + capacity_caveats(ov.get("roots") or []),
            "facts": facts}



def capacity_caveats(roots) -> str:
    """합계가 덜 센 값인 이유(한 줄) — manager.capacity_caveats 와 같은 함수(단일 소스)."""
    from . import manager as _mgr
    return _mgr.capacity_caveats(roots)


def _root_cap(r) -> int:
    """루트 용량 — 마지막 완료 스캔 값(capacity_bytes, 화면과 같은 값). 없으면(포탈 등) scanned_bytes."""
    v = r.get("capacity_bytes")
    return int(v if v is not None else (r.get("scanned_bytes") or 0))


def _rank_roots(data, n=5):
    ov = data.get("overall") or {}
    roots = sorted(ov.get("roots") or [], key=_root_cap, reverse=True)
    return roots[:n]


def _h_biggest(q, data):
    # '파일'/'디렉터리'는 더 구체 핸들러가 먼저 처리하므로 여기선 루트/경로/노드/전반.
    if not _has(q, "제일", "가장", "최대", "큰", "어디", "어느", "top", "탑", "순위", "랭킹"):
        return None
    if _has(q, "파일") or _has(q, "디렉", "폴더"):
        return None
    roots = _rank_roots(data)
    if not roots:
        return None
    scope = data.get("scope")
    noun = "노드/루트" if scope == "portal" else "루트(경로)"
    lines = [f"{i + 1}. {r.get('label') or r.get('root_path')} — "
             f"**{fmt_bytes(_root_cap(r))}**" for i, r in enumerate(roots)]
    facts = {"순위": [{"대상": r.get("label") or r.get("root_path"),
                      "용량": fmt_bytes(_root_cap(r)),
                      "bytes": _root_cap(r)} for r in roots]}
    return {"answer": f"용량이 큰 {noun} 순위:\n" + "\n".join(lines), "facts": facts}


def _h_scan_status(q, data):
    if not _has(q, "진행", "돌고", "스캔 중", "스캔중", "지금 스캔", "몇 개", "몇개", "개수", "상태"):
        return None
    # '파일 개수'·'총 몇 개의 파일' 은 스캔 상태가 아니다(예전엔 '진행 중인 스캔 없음'으로 답했다, 21회차 점검)
    if not _has(q, "진행", "돌고", "스캔", "상태") and _has(q, "파일", "폴더", "디렉"):
        return None
    ov = data.get("overall") or {}
    act = int(ov.get("active_scans") or 0)
    running = [r for r in (ov.get("roots") or [])
               if r.get("status") in ("discovering", "sizing")]
    facts = {"진행중_스캔_수": act, "전체_스캔_수": ov.get("scan_count"),
             "진행중_루트": [r.get("label") or r.get("root_path") for r in running]}
    if act == 0:
        return {"answer": "지금 진행 중인 스캔은 **없습니다**. "
                          f"(누적 스캔 {fmt_num(ov.get('scan_count'))}회)", "facts": facts}
    who = ", ".join(r.get("label") or r.get("root_path") for r in running) or "—"
    return {"answer": f"진행 중인 스캔 **{act}개** — {who}.", "facts": facts}


def _h_last_scan(q, data):
    if not _has(q, "마지막", "최근", "언제"):
        return None
    if _has(q, "가득", "소진", "예측", "며칠"):   # forecast 로 양보
        return None
    ov = data.get("overall") or {}
    best = None
    for r in ov.get("roots") or []:
        fa = r.get("finished_at")
        if fa and (best is None or fa > best.get("finished_at", 0)):
            best = r
    if not best:
        return {"answer": "완료된 스캔 기록이 아직 없습니다.", "facts": {}}
    facts = {"마지막_스캔_대상": best.get("label") or best.get("root_path"),
             "마지막_스캔_시각": _fmt_when(best.get("finished_at"))}
    return {"answer": f"가장 최근 완료 스캔은 **{best.get('label') or best.get('root_path')}** "
                      f"— {_fmt_when(best.get('finished_at'))} 입니다.", "facts": facts}


def _h_fs_usage(q, data):
    if not _has(q, "디스크", "사용률", "여유", "남은", "꽉", "fs", "파일시스템", "점유율"):
        return None
    ov = data.get("overall") or {}
    rows = []
    for r in ov.get("roots") or []:
        used = int(r.get("fs_used_bytes") or 0)
        total = int(r.get("fs_total_bytes") or 0)
        pct = (used / total * 100) if total else 0
        rows.append((r.get("label") or r.get("root_path"), used, total, pct))
    if not rows:
        return None
    rows.sort(key=lambda x: x[3], reverse=True)
    lines = [f"{lbl} — {fmt_bytes(u)} / {fmt_bytes(t)} (**{p:.1f}%**)"
             for lbl, u, t, p in rows[:8]]
    facts = {"디스크": [{"대상": lbl, "사용": fmt_bytes(u), "전체": fmt_bytes(t),
                       "사용률%": round(p, 1)} for lbl, u, t, p in rows[:8]]}
    return {"answer": "파일시스템 디스크 사용률(높은 순):\n" + "\n".join(lines), "facts": facts}


# ── 엣지 상세(detail) 의도 ──
def _need_detail(data):
    d = data.get("detail")
    return d if isinstance(d, dict) else None


def _top_n(q, default=10, cap=30) -> int:
    """'3개만'·'상위 5'·'top 3'·'5개 디렉터리' 같은 개수 — 없으면 default(32회차 점검: 개수를 무시하고 10개를 답함)."""
    m = (re.search(r"(?:상위|top|탑|Top|TOP)\s*(\d{1,3})", q) or re.search(r"(\d{1,3})\s*(?:개|건|위)", q))
    if not m:
        return default
    try:
        return max(1, min(cap, int(m.group(1))))
    except ValueError:
        return default


def _h_top_dirs(q, data):
    if not (_has(q, "디렉", "폴더") and _has(q, "제일", "가장", "최대", "큰", "top", "탑", "순위", "상위")):
        return None
    d = _need_detail(data)
    if not d or not d.get("topdirs"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["topdirs"][:_top_n(q)]
    lines = [f"{i + 1}. {r.get('path')} — **{fmt_bytes(r.get('bytes'))}** "
             f"({fmt_num(r.get('files'))}개)" for i, r in enumerate(rows)]
    facts = {"큰_디렉터리": [{"경로": r.get("path"), "용량": fmt_bytes(r.get("bytes")),
                          "파일수": int(r.get("files") or 0)} for r in rows]}
    return {"answer": "용량이 큰 디렉터리 Top:\n" + "\n".join(lines), "facts": facts}


def _h_big_files(q, data):
    # '큰 파일'만 — '가장 오래된/작은/최근 파일', '가장 많은 파일 확장자'까지 '가장 큰 파일'로 답했다(30회차 점검)
    if not (_has(q, "파일") and _has(q, "큰", "대용량", "최대", "용량 많", "top", "탑", "상위")):
        return None
    if _has(q, "작은", "오래", "확장자", "분포", "종류", "형식"):   # '최근 가장 큰 파일'은 큰 파일 질문(31회차 점검)
        return None
    d = _need_detail(data)
    if not d or not d.get("top_files"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["top_files"][:_top_n(q)]
    lines = [f"{i + 1}. {r.get('path')} — **{fmt_bytes(r.get('bytes'))}**"
             + (f" ({r.get('owner')})" if r.get("owner") else "")
             for i, r in enumerate(rows)]
    facts = {"큰_파일": [{"경로": r.get("path"), "용량": fmt_bytes(r.get("bytes")),
                       "소유자": r.get("owner")} for r in rows]}
    return {"answer": "가장 큰 파일 Top:\n" + "\n".join(lines), "facts": facts}


def _h_old_data(q, data):
    if not _has(q, "오래", "묵은", "옛", "아카이브", "정리", "안 바", "안바", "안 쓴", "안쓴",
                "오래된", "낡은"):
        return None
    d = _need_detail(data)
    if not d or not d.get("age"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    thr = parse_threshold_days(q)
    _dflt = thr is None
    if thr is None:
        thr = 365     # 기본: 1년 이상 = 아카이브 후보(명시한 '0일'은 그대로 — 예전엔 0 이 365 로 바뀜)
    b, f, labels, strad = _sum_age_over(d["age"], thr)
    human_thr = (f"{thr // 365}년" if thr % 365 == 0 and thr >= 365
                 else (f"{thr // 30}개월" if thr % 30 == 0 and thr >= 30 else f"{thr}일"))
    facts = {"기준": f"{human_thr} 이상 미수정", "용량": fmt_bytes(b), "bytes": b,
             "파일수": f, "버킷": labels}
    return {"answer": f"마지막 수정(mtime) 기준 **{human_thr} 이상** 안 바뀐 데이터는 "
                      f"**{fmt_bytes(b)}** ({fmt_num(f)}개) 입니다 — 아카이브/정리 후보. "
                      f"(버킷: {', '.join(labels) or '—'})" + _range_note(b, strad)
                      + (" ※ 기간을 따로 말하지 않아(또는 읽지 못해) 1년 기준으로 답했습니다 — 예: '6개월', '90일'." if _dflt else ""),
            "facts": facts}


def _h_cold_data(q, data):
    if not _has(q, "접근 안", "접근안", "콜드", "cold", "atime", "안 열", "안열", "마지막 접근"):
        return None
    d = _need_detail(data)
    if not d or not d.get("atime_age"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    thr = parse_threshold_days(q)
    _dflt = thr is None
    if thr is None:
        thr = 365
    b, f, labels, strad = _sum_age_over(d["atime_age"], thr)
    human_thr = (f"{thr // 365}년" if thr % 365 == 0 and thr >= 365 else f"{thr}일")
    facts = {"기준": f"{human_thr} 이상 미접근", "용량": fmt_bytes(b), "파일수": f, "버킷": labels}
    return {"answer": f"마지막 접근(atime) 기준 **{human_thr} 이상** 안 열어본 콜드 데이터는 "
                      f"**{fmt_bytes(b)}** ({fmt_num(f)}개) 입니다. "
                      f"(atime 정책이 noatime 이면 값이 무의미할 수 있음)" + _range_note(b, strad)
                      + (" ※ 기간을 따로 말하지 않아(또는 읽지 못해) 1년 기준으로 답했습니다 — 예: '6개월', '90일'." if _dflt else ""),
            "facts": facts}


def _h_owners(q, data):
    if not _has(q, "누가", "소유자", "유저", "사용자", "차지백", "쇼백", "오너", "owner"):
        return None
    d = _need_detail(data)
    if not d or not d.get("owners"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["owners"][:10]
    def _name(r):
        return (r.get("name") or "") + (f" (uid {r.get('key')})" if r.get("name") else f"uid {r.get('key')}")
    lines = [f"{i + 1}. {_name(r)} — **{fmt_bytes(r.get('bytes'))}** "
             f"({fmt_num(r.get('files'))}개)" for i, r in enumerate(rows)]
    facts = {"소유자별": [{"소유자": _name(r), "용량": fmt_bytes(r.get("bytes"))} for r in rows]}
    return {"answer": "공간을 많이 쓰는 소유자 Top:\n" + "\n".join(lines), "facts": facts}


def _h_extensions(q, data):
    if not _has(q, "확장자", "무슨 파일", "어떤 파일", "무슨 종류", "어떤 종류", "파일 종류",
                "포맷", "형식") and not re.search(r"(?<![a-z])ext(ension)?s?(?![a-z])", q):
        # 'ext' 는 단어로만 — next·text·context 안의 글자에 걸려 확장자 질문으로 오인하던 문제(2026-11 리뷰)
        return None
    d = _need_detail(data)
    if not d or not d.get("extensions"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["extensions"][:10]
    lines = [f"{i + 1}. {r.get('key') or '(확장자 없음)'} — **{fmt_bytes(r.get('bytes'))}** "
             f"({fmt_num(r.get('files'))}개)" for i, r in enumerate(rows)]
    facts = {"확장자별": [{"확장자": r.get("key"), "용량": fmt_bytes(r.get("bytes"))} for r in rows]}
    return {"answer": "용량을 많이 쓰는 확장자 Top:\n" + "\n".join(lines), "facts": facts}


def _h_sizes(q, data):
    if not _has(q, "크기 분포", "크기별", "작은 파일", "사이즈 분포", "파일 크기"):
        return None
    d = _need_detail(data)
    if not d or not d.get("sizes"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["sizes"]
    lines = [f"· {r.get('key')} — {fmt_bytes(r.get('bytes'))} ({fmt_num(r.get('files'))}개)"
             for r in rows]
    facts = {"크기분포": [{"버킷": r.get("key"), "용량": fmt_bytes(r.get("bytes")),
                        "파일수": int(r.get("files") or 0)} for r in rows]}
    return {"answer": "파일 크기 분포:\n" + "\n".join(lines), "facts": facts}


def _h_forecast(q, data):
    if not _has(q, "며칠", "예측", "소진", "가득", "꽉", "추세", "증가", "얼마나 남", "언제 다 차",
                "언제 차"):
        return None
    # '디스크 꽉 찼어?'·'디스크 얼마나 남았어?' 는 지금 사용률 질문 — 시점·예측 말이 없으면 사용률로 양보(21회차 점검)
    if not _has(q, "며칠", "예측", "소진", "추세", "증가", "언제") and _has(q, "디스크", "찼", "남았", "여유", "사용률"):
        return None
    d = _need_detail(data)
    if not d or not d.get("forecast"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    fc = d["forecast"]
    if not fc.get("ok") or not fc.get("enough"):
        if fc.get("reason"):   # 서버가 준 이유(하루 이상 걸친 이력 필요 등) — 화면과 같은 문구(22회차 점검)
            return {"answer": "용량 증가 추세를 아직 계산할 수 없습니다 — %s (현재 완료 스캔 %s회)."
                              % (str(fc["reason"]).rstrip("."), fc.get("points", 0)), "facts": {}}
        return {"answer": "용량 증가 추세를 계산할 만큼 완료 스캔이 충분하지 않습니다 "
                          f"(같은 루트 2회 이상 필요, 현재 {fc.get('points', 0)}회).", "facts": {}}
    g = int(fc.get("growth_per_day") or 0)
    parts = [f"증가 추세 **{'+' if g >= 0 else '−'}{fmt_bytes(abs(g))}/일** "
             f"({fc.get('points')}회 스캔 기준)"]
    facts = {"증가_per_day": fmt_bytes(g)}
    if fc.get("days_to_90pct") is not None:
        parts.append(f"FS 90% 도달까지 약 **{round(fc['days_to_90pct'])}일**")
        facts["90%까지_일"] = round(fc["days_to_90pct"])
    if fc.get("days_to_full") is not None:
        parts.append(f"가득 차기까지 약 **{round(fc['days_to_full'])}일**")
        facts["full까지_일"] = round(fc["days_to_full"])
    return {"answer": " · ".join(parts) + ".", "facts": facts}


# 우선순위(구체적 → 일반적). 첫 매칭 채택.
_HANDLERS = [
    _h_forecast, _h_big_files, _h_top_dirs, _h_cold_data, _h_old_data,
    _h_owners, _h_extensions, _h_sizes, _h_scan_status, _h_last_scan,
    _h_fs_usage, _h_biggest, _h_total,
]


def _help(data) -> dict:
    edge = data.get("scope") != "portal"
    ex = ["전체 용량 얼마야?", "어느 경로가 제일 커?",
          "지금 스캔 진행 중이야?", "마지막 스캔 언제야?"]
    if edge:
        ex += ["제일 큰 디렉터리 알려줘", "30일 넘게 안 바뀐 데이터 얼마나 돼?",
               "누가 공간을 제일 많이 써?", "확장자별로 뭐가 제일 커?",
               "제일 큰 파일은?", "언제 디스크 가득 차?"]
    return {"ok": True, "intent": "help", "source": "rules",
            "answer": "무엇이든 문장으로 물어보세요. 예를 들면:\n"
                      + "\n".join(f"· {e}" for e in ex),
            "facts": {}, "suggestions": ex}


def answer(question: str, data: dict) -> dict:
    """규칙 기반 답변. 항상 dict 반환(ok=True)."""
    q = (question or "").strip().lower()
    if not q:
        return _help(data)
    for h in _HANDLERS:
        try:
            r = h(q, data)
        except Exception:   # noqa: BLE001 — 한 핸들러 오류가 전체를 막지 않게
            r = None
        if r:
            return {"ok": True, "intent": h.__name__[3:], "source": "rules",
                    "answer": r["answer"], "facts": r.get("facts") or {}}
    return _help(data)


# ────────────────────────────── 로컬 LLM(선택) ──────────────────────────────
def _llm_endpoint(cfg) -> str:
    ep = (cfg.get("endpoint") or "").strip().rstrip("/")
    if not ep:
        return ""
    if ep.endswith("/chat/completions"):
        return ep
    if ep.endswith("/v1"):
        return ep + "/chat/completions"
    return ep + "/v1/chat/completions"


def llm_phrase(question: str, facts: dict, cfg: dict):
    """로컬 LLM(OpenAI 호환)에게 '사실'만으로 문장을 다듬게 한다. 실패 시 None."""
    url = _llm_endpoint(cfg or {})
    if not url:
        return None
    sys = ("너는 NAS 용량 분석 비서다. 아래 [사실]에 있는 숫자/값만 사용해 한국어로 간결하고 "
           "정확하게 답하라. [사실]에 없는 수치는 절대 지어내지 말고 모른다고 답하라. "
           "표 대신 짧은 문장/불릿으로.")
    user = f"질문: {question}\n\n[사실]\n{json.dumps(facts, ensure_ascii=False)}"
    body = json.dumps({
        "model": cfg.get("model") or "default",
        "messages": [{"role": "system", "content": sys},
                     {"role": "user", "content": user}],
        "temperature": 0.2, "stream": False,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cfg.get("key"):
        headers["Authorization"] = "Bearer " + cfg["key"]
    tmo = float(cfg.get("timeout") or 20)
    box = {}

    def _call():
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with netutil.urlopen(req, timeout=tmo) as resp:
                raw = resp.read(4 * 1024 * 1024 + 1)          # 응답 크기 상한(4MB)
            if len(raw) > 4 * 1024 * 1024:
                return
            j = json.loads(raw.decode("utf-8"))
            txt = (j.get("choices") or [{}])[0].get("message", {}).get("content")
            box["txt"] = txt.strip() if isinstance(txt, str) and txt.strip() else None
        except Exception:   # noqa: BLE001 — LLM 실패는 조용히 규칙 답변으로 폴백
            pass
    # 전체 시간 상한 — 소켓 timeout 은 '읽기 한 번'마다라, 1초에 1바이트씩 보내는 서버면 60초를 붙잡혔다(21회차 점검)
    th = threading.Thread(target=_call, name="ask-llm", daemon=True)
    th.start()
    th.join(tmo + 1.0)
    return box.get("txt")


_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)*)\s*(만|억|%)?")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_LISTNUM_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
_RANKWORD_RE = re.compile(r"(상위|하위|top|TOP|Top)\s*\d{1,2}|\d{1,2}\s*(위|번째|등)")


def _num_tokens(text: str) -> list:
    """(값, 원문, 접미사) 목록."""
    out = []
    for m in _NUM_RE.finditer(text or ""):
        raw, suf = m.group(1).replace(",", ""), m.group(2)
        try:
            v = float(raw)
        except ValueError:
            continue
        out.append((v * (1e4 if suf == "만" else 1e8 if suf == "억" else 1), raw, suf))
    return out


def _max_list_len(o) -> int:
    best, stack = 0, [o]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            best = max(best, len(x))
            stack.extend(x)
    return best


def _numbers_grounded(phrased: str, facts: dict, rule_answer: str) -> bool:
    """LLM 문장의 숫자가 사실(facts)·규칙 답변과 맞는지 — 지어낸 수는 걸러 내고 반올림은 허용한다.

    - 100 미만 정수(소수점·%·만/억 없음)는 사실의 어떤 정수와 '정확히' 같아야 한다 — 예전 완화(22회차)는 작은 정수를
      검사하지 않아 '13대 중 3대 다운'(사실 1대)·'21 TB'(사실 210)를 통과시켰다(23회차 점검). 단 목록 번호(1.)·순위
      말(상위 5개·3위)·사실 목록 길이 이하의 개수는 허용.
    - 그 밖(소수·100 이상·%·만/억)은 자릿수에 맞는 반올림 오차(+0.6% 상대 오차) 안에서 사실의 수와 맞아야 한다.
    - 날짜·시각 숫자는 사실에도 있으면(그대로 토큰이 됨) 허용 — 예전엔 사실 쪽에서 날짜를 지워 연도가 거부됐다."""
    src = json.dumps(facts, ensure_ascii=False) + " " + (rule_answer or "")
    allowed = [v for v, _r, _s in _num_tokens(src)]
    allowed_int = {int(v) for v in allowed if float(v).is_integer()}
    list_max = _max_list_len(facts)
    text = _TIME_RE.sub(" ", _LISTNUM_RE.sub(" ", phrased or ""))
    text = _RANKWORD_RE.sub(" ", text)
    for v, raw, suf in _num_tokens(text):
        if "." not in raw and not suf and v < 100:
            if int(v) in allowed_int or (0 < v <= list_max):
                continue
            return False
        scale = 1e4 if suf == "만" else 1e8 if suf == "억" else 1
        dec = len(raw.split(".", 1)[1]) if "." in raw else 0
        half = 0.5 * (10 ** -dec) * scale
        if not any(abs(a - v) <= half + 0.006 * abs(a) for a in allowed):
            return False
    return True


def respond(question: str, data: dict, llm_cfg: Optional[dict] = None) -> dict:
    """규칙 기반 답을 만들고, 로컬 LLM 이 켜져 있으면 문장만 다듬는다(숫자는 규칙 값 유지)."""
    base = answer(question, data)
    # 사실(facts)이 없으면 LLM 을 부르지 않는다 — '[사실] {}' 로 부르면 규칙 안내문을 지어낸 문장으로 바꿨다(21회차 점검).
    # 다듬은 문장에 사실에 없는 숫자가 있으면 버리고 규칙 답변을 쓴다(설계: 숫자는 규칙 값만).
    if llm_cfg and llm_cfg.get("enabled") and base.get("facts"):
        phrased = llm_phrase(question, base.get("facts") or {}, llm_cfg)
        if phrased and _numbers_grounded(phrased, base.get("facts") or {}, base.get("answer") or ""):
            return {**base, "answer": phrased, "source": "rules+llm",
                    "rule_answer": base["answer"]}
    return base

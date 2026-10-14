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
from __future__ import annotations

import json
import re
import time
import urllib.request

# 파일 나이 버킷(오래된 순). server.py 의 _AGE_ORDER 와 같은 라벨을 쓴다(통일).
_AGE_ORDER = ["30일 이내", "30~90일", "90일~1년", "1~2년", "2~5년", "5년+"]
# 각 버킷의 '하한 일수'(이상 합산용 근사).
_AGE_LOWER_DAYS = {"30일 이내": 0, "30~90일": 30, "90일~1년": 90,
                   "1~2년": 365, "2~5년": 730, "5년+": 1825}
_UNIT_DAYS = {"일": 1, "주": 7, "주일": 7, "달": 30, "개월": 30, "년": 365, "연": 365}


def fmt_bytes(n) -> str:
    """사람이 읽기 좋은 용량(대시보드 fmtBytes 와 같은 기준: 1024, 소수 2자리)."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return (f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}")
        n /= 1024
    return f"{n:.2f} PB"


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


def parse_threshold_days(q: str):
    """'30일', '1년', '6개월' 같은 표현을 일수로. 없으면 None."""
    m = re.search(r"(\d+)\s*(개월|주일|일|주|달|년|연)", q)
    if not m:
        return None
    return int(m.group(1)) * _UNIT_DAYS.get(m.group(2), 1)


def _has(q: str, *words: str) -> bool:
    return any(w in q for w in words)


def _sum_age_over(buckets, threshold_days):
    """나이 버킷에서 threshold_days '이상'인 것 합산 → (bytes, files, 포함된 라벨들)."""
    tot_b = tot_f = 0
    labels = []
    for b in buckets or []:
        low = _AGE_LOWER_DAYS.get(b.get("key"), -1)
        if low >= 0 and low >= threshold_days - 1:   # -1: 30일→'30~90일'부터 포함
            tot_b += int(b.get("bytes") or 0)
            tot_f += int(b.get("files") or 0)
            labels.append(b.get("key"))
    return tot_b, tot_f, labels


# ────────────────────────────── 규칙 기반 핸들러 ──────────────────────────────
# 각 핸들러: (q, data) -> dict | None.  반환 dict = {"answer": str, "facts": {...}}.
# None 이면 '의도 아님' 또는 '이 화면엔 데이터 없음'.

def _detail_unavailable(data):
    """엣지 상세가 필요한 질문인데 포탈/상세없음일 때 안내."""
    if data.get("scope") == "portal":
        return ("이 질문은 디렉터리 상세가 필요합니다 — 포탈은 노드별 합계만 모읍니다. "
                "각 노드(엣지) 대시보드의 ‘분석 리포트’에서 볼 수 있어요.")
    return "선택된 스캔의 상세 집계가 아직 없습니다(스캔이 완료되면 채워집니다)."


def _h_total(q, data):
    if not _has(q, "전체", "총", "다 합", "전부", "합계", "토탈", "모두"):
        return None
    if not _has(q, "용량", "사용량", "얼마", "크기", "쓰고", "차지"):
        return None
    ov = data.get("overall") or {}
    b = int(ov.get("total_scanned_bytes") or 0)
    facts = {"전체_조사_용량_bytes": b, "전체_조사_용량": fmt_bytes(b),
             "루트_수": ov.get("root_count"), "스캔_수": ov.get("scan_count")}
    return {"answer": f"전체 조사 용량은 **{fmt_bytes(b)}** 입니다 "
                      f"(루트 {fmt_num(ov.get('root_count'))}개 · 스캔 "
                      f"{fmt_num(ov.get('scan_count'))}회 기준).",
            "facts": facts}


def _rank_roots(data, n=5):
    ov = data.get("overall") or {}
    roots = sorted(ov.get("roots") or [],
                   key=lambda r: int(r.get("scanned_bytes") or 0), reverse=True)
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
             f"**{fmt_bytes(r.get('scanned_bytes'))}**" for i, r in enumerate(roots)]
    facts = {"순위": [{"대상": r.get("label") or r.get("root_path"),
                      "용량": fmt_bytes(r.get("scanned_bytes")),
                      "bytes": int(r.get("scanned_bytes") or 0)} for r in roots]}
    return {"answer": f"용량이 큰 {noun} 순위:\n" + "\n".join(lines), "facts": facts}


def _h_scan_status(q, data):
    if not _has(q, "진행", "돌고", "스캔 중", "스캔중", "지금 스캔", "몇 개", "몇개", "개수", "상태"):
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


def _h_top_dirs(q, data):
    if not (_has(q, "디렉", "폴더") and _has(q, "제일", "가장", "최대", "큰", "top", "탑", "순위")):
        return None
    d = _need_detail(data)
    if not d or not d.get("topdirs"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["topdirs"][:10]
    lines = [f"{i + 1}. {r.get('path')} — **{fmt_bytes(r.get('bytes'))}** "
             f"({fmt_num(r.get('files'))}개)" for i, r in enumerate(rows)]
    facts = {"큰_디렉터리": [{"경로": r.get("path"), "용량": fmt_bytes(r.get("bytes")),
                          "파일수": int(r.get("files") or 0)} for r in rows]}
    return {"answer": "용량이 큰 디렉터리 Top:\n" + "\n".join(lines), "facts": facts}


def _h_big_files(q, data):
    if not (_has(q, "파일") and _has(q, "제일", "가장", "최대", "큰", "대용량", "top", "탑")):
        return None
    d = _need_detail(data)
    if not d or not d.get("top_files"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    rows = d["top_files"][:10]
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
    thr = parse_threshold_days(q) or 365     # 기본: 1년 이상 = 아카이브 후보
    b, f, labels = _sum_age_over(d["age"], thr)
    human_thr = (f"{thr // 365}년" if thr % 365 == 0 and thr >= 365
                 else (f"{thr // 30}개월" if thr % 30 == 0 and thr >= 30 else f"{thr}일"))
    facts = {"기준": f"{human_thr} 이상 미수정", "용량": fmt_bytes(b), "bytes": b,
             "파일수": f, "버킷": labels}
    return {"answer": f"마지막 수정(mtime) 기준 **{human_thr} 이상** 안 바뀐 데이터는 "
                      f"**{fmt_bytes(b)}** ({fmt_num(f)}개) 입니다 — 아카이브/정리 후보. "
                      f"(버킷: {', '.join(labels) or '—'})", "facts": facts}


def _h_cold_data(q, data):
    if not _has(q, "접근 안", "접근안", "콜드", "cold", "atime", "안 열", "안열", "마지막 접근"):
        return None
    d = _need_detail(data)
    if not d or not d.get("atime_age"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    thr = parse_threshold_days(q) or 365
    b, f, labels = _sum_age_over(d["atime_age"], thr)
    human_thr = (f"{thr // 365}년" if thr % 365 == 0 and thr >= 365 else f"{thr}일")
    facts = {"기준": f"{human_thr} 이상 미접근", "용량": fmt_bytes(b), "파일수": f, "버킷": labels}
    return {"answer": f"마지막 접근(atime) 기준 **{human_thr} 이상** 안 열어본 콜드 데이터는 "
                      f"**{fmt_bytes(b)}** ({fmt_num(f)}개) 입니다. "
                      f"(atime 정책이 noatime 이면 값이 무의미할 수 있음)", "facts": facts}


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
                "포맷", "형식", "ext"):
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
    d = _need_detail(data)
    if not d or not d.get("forecast"):
        return {"answer": _detail_unavailable(data), "facts": {}}
    fc = d["forecast"]
    if not fc.get("ok") or not fc.get("enough"):
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
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout") or 20)) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        txt = (j.get("choices") or [{}])[0].get("message", {}).get("content")
        return txt.strip() if txt else None
    except Exception:   # noqa: BLE001 — LLM 실패는 조용히 규칙 답변으로 폴백
        return None


def respond(question: str, data: dict, llm_cfg: dict | None = None) -> dict:
    """규칙 기반 답을 만들고, 로컬 LLM 이 켜져 있으면 문장만 다듬는다(숫자는 규칙 값 유지)."""
    base = answer(question, data)
    if llm_cfg and llm_cfg.get("enabled") and (base.get("facts") or base.get("intent") != "help"):
        phrased = llm_phrase(question, base.get("facts") or {}, llm_cfg)
        if phrased:
            return {**base, "answer": phrased, "source": "rules+llm",
                    "rule_answer": base["answer"]}
    return base

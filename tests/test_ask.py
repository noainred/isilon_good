"""ask — 규칙 기반 자연어 질의응답 엔진 검증(표준 라이브러리만).

숫자는 규칙 엔진이 결정적으로 계산하므로, 합성 데이터를 넣고 의도 분류·수치·
임계값 파싱·포탈(상세없음) 폴백·LLM 비활성 폴백을 점검한다. 네트워크 호출 없음.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import ask  # noqa: E402

GB = 1024 ** 3


def _edge():
    return {
        "scope": "edge",
        "overall": {
            "total_scanned_bytes": 1209 * GB, "root_count": 2,
            "scan_count": 3, "active_scans": 1,
            "roots": [
                {"label": "node1:/mnt/hadoop", "root_path": "/mnt/hadoop",
                 "scanned_bytes": 1200 * GB, "fs_used_bytes": 2700 * GB,
                 "fs_total_bytes": 3000 * GB, "total_files": 2_000_000,
                 "status": "sizing", "finished_at": None},
                {"label": "node1:/mnt/hadoopmes", "root_path": "/mnt/hadoopmes",
                 "scanned_bytes": 9 * GB, "fs_used_bytes": 1900 * GB,
                 "fs_total_bytes": 4000 * GB, "total_files": 50_000,
                 "status": "done", "finished_at": 1_750_000_000},
            ],
        },
        "detail": {
            "scan_id": 4, "root_path": "/mnt/hadoop",
            "age": [
                {"key": "30일 이내", "bytes": 100 * GB, "files": 1000},
                {"key": "30~90일", "bytes": 50 * GB, "files": 500},
                {"key": "90일~1년", "bytes": 30 * GB, "files": 300},
                {"key": "1~2년", "bytes": 20 * GB, "files": 200},
                {"key": "2~5년", "bytes": 10 * GB, "files": 100},
                {"key": "5년+", "bytes": 5 * GB, "files": 50},
            ],
            "atime_age": [
                {"key": "1~2년", "bytes": 40 * GB, "files": 400},
                {"key": "5년+", "bytes": 8 * GB, "files": 80},
            ],
            "owners": [
                {"key": 0, "name": "root", "bytes": 800 * GB, "files": 1_100_000},
                {"key": 1001, "name": "alice", "bytes": 300 * GB, "files": 50_000},
            ],
            "extensions": [
                {"key": ".vbk", "bytes": 700 * GB, "files": 125},
                {"key": ".log", "bytes": 200 * GB, "files": 90_000},
            ],
            "sizes": [
                {"key": "10GB+", "bytes": 600 * GB, "files": 50},
                {"key": "1~10MB", "bytes": 5 * GB, "files": 100_000},
            ],
            "top_files": [
                {"path": "/mnt/hadoop/backup.vbk", "bytes": 75 * GB, "owner": "root"},
            ],
            "topdirs": [
                {"path": "/mnt/hadoop/backups", "bytes": 700 * GB, "files": 1000},
                {"path": "/mnt/hadoop/logs", "bytes": 200 * GB, "files": 90_000},
            ],
            "forecast": {"ok": True, "enough": True, "growth_per_day": 10 * GB,
                         "days_to_90pct": 30, "days_to_full": 60, "points": 3},
        },
    }


def _portal():
    return {
        "scope": "portal",
        "overall": {
            "total_scanned_bytes": 1209 * GB, "root_count": 2,
            "scan_count": 5, "active_scans": 0,
            "roots": [
                {"label": "Seoul/node1:/mnt/hadoop", "root_path": "/mnt/hadoop",
                 "scanned_bytes": 1200 * GB, "fs_used_bytes": 2700 * GB,
                 "fs_total_bytes": 3000 * GB, "total_files": 2_000_000,
                 "status": "done", "finished_at": 1_750_000_000},
            ],
        },
        "detail": None,
    }


def run_common() -> None:
    e = _edge()
    # 전체 용량
    r = ask.answer("전체 용량 얼마야?", e)
    assert r["intent"] == "total", r
    assert ask.fmt_bytes(1209 * GB) in r["answer"], r["answer"]
    # 제일 큰 경로 → hadoop 이 hadoopmes 보다 먼저
    r = ask.answer("어느 경로가 제일 커?", e)
    assert r["intent"] == "biggest", r
    assert r["answer"].index("/mnt/hadoop") < r["answer"].index("/mnt/hadoopmes"), r["answer"]
    # 진행 중
    r = ask.answer("지금 스캔 진행 중이야?", e)
    assert r["intent"] == "scan_status" and "1개" in r["answer"], r
    # 마지막 스캔(‘언제’가 forecast 로 새지 않아야 함)
    r = ask.answer("마지막 스캔 언제야?", e)
    assert r["intent"] == "last_scan" and "/mnt/hadoopmes" in r["answer"], r
    # 디스크 사용률 — hadoop 90%
    r = ask.answer("디스크 사용률 알려줘", e)
    assert r["intent"] == "fs_usage" and "90.0%" in r["answer"], r["answer"]
    print("[common] OK  전체/제일큰/진행중/마지막스캔/디스크")


def run_detail() -> None:
    e = _edge()
    r = ask.answer("제일 큰 디렉터리 알려줘", e)
    assert r["intent"] == "top_dirs" and "/mnt/hadoop/backups" in r["answer"], r
    r = ask.answer("제일 큰 파일은?", e)
    assert r["intent"] == "big_files" and "backup.vbk" in r["answer"], r
    r = ask.answer("누가 공간을 제일 많이 써?", e)
    assert r["intent"] == "owners" and "root" in r["answer"], r
    r = ask.answer("확장자별로 뭐가 제일 커?", e)
    assert r["intent"] == "extensions" and ".vbk" in r["answer"], r
    r = ask.answer("파일 크기 분포 보여줘", e)
    assert r["intent"] == "sizes" and "10GB+" in r["answer"], r
    r = ask.answer("언제 디스크 가득 차?", e)
    assert r["intent"] == "forecast" and "60일" in r["answer"], r
    print("[detail] OK  디렉터리/파일/소유자/확장자/크기/예측")


def run_threshold() -> None:
    assert ask.parse_threshold_days("30일 넘게") == 30
    assert ask.parse_threshold_days("1년 이상") == 365
    assert ask.parse_threshold_days("6개월") == 180
    assert ask.parse_threshold_days("그냥 질문") is None
    e = _edge()
    # 30일 이상 = 30일이내 제외 → 50+30+20+10+5 = 115 GB
    r = ask.answer("30일 넘게 안 바뀐 데이터 얼마나 돼?", e)
    assert r["intent"] == "old_data", r
    assert ask.fmt_bytes(115 * GB) in r["answer"], r["answer"]
    # 1년 이상 = 1~2년+2~5년+5년+ → 20+10+5 = 35 GB
    r = ask.answer("1년 넘게 안 쓴 데이터는?", e)
    assert ask.fmt_bytes(35 * GB) in r["answer"], r["answer"]
    # 콜드(atime) 1년 이상 → 40+8 = 48 GB
    r = ask.answer("1년 넘게 접근 안 한 콜드 데이터?", e)
    assert r["intent"] == "cold_data" and ask.fmt_bytes(48 * GB) in r["answer"], r["answer"]
    print("[threshold] OK  일/개월/년 파싱 + 나이 버킷 이상합산(mtime·atime)")


def run_portal_fallback() -> None:
    p = _portal()
    # 공통 의도는 포탈에서도 동작
    r = ask.answer("전체 용량 얼마야?", p)
    assert r["intent"] == "total" and ask.fmt_bytes(1209 * GB) in r["answer"], r
    # 상세 의도는 '노드(엣지)에서' 안내
    r = ask.answer("제일 큰 디렉터리?", p)
    assert r["intent"] == "top_dirs" and "노드" in r["answer"], r["answer"]
    r = ask.answer("누가 제일 많이 써?", p)
    assert r["intent"] == "owners" and "노드" in r["answer"], r["answer"]
    print("[portal] OK  공통은 답, 디렉터리/소유자 상세는 엣지로 안내")


def run_help() -> None:
    e = _edge()
    assert ask.answer("", e)["intent"] == "help"
    assert ask.answer("안녕 반가워", e)["intent"] == "help"
    h = ask.answer("도와줘", e)
    assert h["intent"] == "help" and h.get("suggestions"), h
    print("[help] OK  빈 질문·미매칭 → 예시 안내")


def run_llm() -> None:
    # 엔드포인트 정규화
    assert ask._llm_endpoint({"endpoint": "http://h:11434"}) == "http://h:11434/v1/chat/completions"
    assert ask._llm_endpoint({"endpoint": "http://h:11434/v1"}) == "http://h:11434/v1/chat/completions"
    assert ask._llm_endpoint({"endpoint": "http://h:8000/v1/chat/completions"}) \
        == "http://h:8000/v1/chat/completions"
    assert ask._llm_endpoint({}) == ""
    # 엔드포인트 없으면 즉시 None(네트워크 호출 안 함)
    assert ask.llm_phrase("q", {"a": 1}, {}) is None
    assert ask.llm_phrase("q", {"a": 1}, {"endpoint": ""}) is None
    # respond: LLM 꺼짐/없음 → 규칙 답변 그대로(source=rules)
    e = _edge()
    base = ask.answer("전체 용량?", e)
    r1 = ask.respond("전체 용량?", e, None)
    r2 = ask.respond("전체 용량?", e, {"enabled": False})
    r3 = ask.respond("전체 용량?", e, {"enabled": True, "endpoint": ""})   # 켜도 EP 없으면 폴백
    for r in (r1, r2, r3):
        assert r["source"] == "rules" and r["answer"] == base["answer"], r
    print("[llm] OK  엔드포인트 정규화 · 미설정 None · 비활성/EP없음 → 규칙 폴백")


if __name__ == "__main__":
    run_common()
    run_detail()
    run_threshold()
    run_portal_fallback()
    run_help()
    run_llm()
    print("모든 테스트 통과 ✅")

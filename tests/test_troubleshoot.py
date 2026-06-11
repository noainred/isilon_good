"""트러블슈팅 — 처리량 표본 DB(metrics.db) + 진단 이력 판정 검증.

진단 자체는 실행 중 스캔이 필요하므로, 여기서는 순수 함수(표본 저장/조회/정리,
문제 판정)를 점검한다. 표준 라이브러리만 사용.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import troubleshoot as t  # noqa: E402


def run_rate_samples() -> None:
    dd = tempfile.mkdtemp(prefix="iu_ts_")
    try:
        now = time.time()
        for i in range(6):
            t.append_rate_sample(dd, now - (5 - i) * 10, 1000 + i * 50, 20000 + i, 1)
        t.append_rate_sample(dd, now - 90000, 1, 1, 1)        # 25시간 전(24h 창 밖)
        assert os.path.exists(t.metrics_db_path(dd)), "metrics.db 생성 안 됨"
        # 24h 창: 오래된 1건은 제외되어 6건
        assert len(t.load_rate_samples(dd, seconds=86400)) == 6, "24h 창 필터 오류"
        # 넓은 창: DB 에는 7건 모두 있음
        wide = t.load_rate_samples(dd, seconds=200000, max_points=600)
        assert len(wide) == 7, len(wide)
        assert wide[0]["t"] <= wide[-1]["t"], "시간 정렬 안 됨"
        # trim: 24시간 초과 1건을 DB 에서 실제 삭제
        t.trim_rate_samples(dd, keep_seconds=86400)
        assert len(t.load_rate_samples(dd, seconds=200000)) == 6, "trim 미동작"
        # 창(window) 필터: 최근 30초만
        recent = t.load_rate_samples(dd, seconds=35)
        assert all(now - x["t"] <= 36 for x in recent), recent
        # 다운샘플 상한
        for i in range(1000):
            t.append_rate_sample(dd, now - 3000 + i, i, i, 1)
        big = t.load_rate_samples(dd, seconds=86400, max_points=200)
        assert len(big) == 200, len(big)
        # 비우기
        t.clear_rate_samples(dd)
        assert t.load_rate_samples(dd) == [], "clear 후에도 남음"
        print("[rate_samples] OK  저장/창조회/trim/다운샘플/clear")
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def run_is_problem() -> None:
    base = {"ok": True, "running": True, "verdict": {"level": "ok"},
            "resource_bottleneck": {"level": "ok"}, "resources": []}
    assert not t.is_problem(base), "정상인데 문제로 판정"
    assert t.is_problem({**base, "verdict": {"level": "warn", "title": "x"}})
    assert t.is_problem({**base, "resources": [{"name": "CPU", "level": "bad"}]})
    assert not t.is_problem({**base, "running": False})        # 미실행은 기록 안 함
    # info(롱테일) 단독은 문제 아님
    assert not t.is_problem({**base, "verdict": {"level": "info", "title": "롱테일"}})
    print("[is_problem] OK  OK/warn/bad/info/미실행 판정")


def run_history() -> None:
    dd = tempfile.mkdtemp(prefix="iu_ts_")
    try:
        prob = {"ok": True, "running": True, "verdict": {"level": "warn", "title": "직렬화"},
                "resource_bottleneck": {"level": "warn", "name": "CPU"},
                "resources": [{"name": "CPU", "level": "bad", "value": "150%", "note": "x"}],
                "rate_dirs": 10, "rate_files": 100, "pending_est": 5, "root_path": "/m"}
        assert t.record_if_problem(dd, prob)
        assert t.record_if_problem(dd, prob)                  # 같은 문제 → 병합
        ev = t.load_history(dd)
        assert len(ev) == 1, len(ev)
        assert ev[0]["count"] == 2, ev[0]["count"]
        t.clear_history(dd)
        assert t.load_history(dd) == []
        print("[history] OK  문제 저장·병합(count)·clear")
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def run_throughput() -> None:
    dd = tempfile.mkdtemp(prefix="iu_ts_")
    try:
        now = time.time()
        gb = 1024 ** 3
        # 10초 간격 표본 60개: rb=1GB/s, rf=1000/s, rd=10/s
        for i in range(60):
            t.append_rate_sample(dd, now - (60 - i) * 10, 10, 1000, gb, 1)
        b = t.throughput_buckets(dd, bucket_sec=60, max_buckets=60)
        assert b, "버킷 없음"
        # 적분 총합 = 59구간 × 10초 × 1GB/s = 590 GB (±오차)
        total_gb = sum(x["bytes"] for x in b) / gb
        assert 580 <= total_gb <= 600, total_gb
        # 1시간 버킷이면 하나로 합쳐짐(데이터가 10분치라)
        b1h = t.throughput_buckets(dd, bucket_sec=3600, max_buckets=60)
        assert len(b1h) <= 2, len(b1h)
        print("[throughput] OK  버킷 적분 총 %.0f GB(이론 590), 1분/1시간 버킷" % total_gb)
    finally:
        shutil.rmtree(dd, ignore_errors=True)


def run_verdict() -> None:
    from isilon_usage.troubleshoot import _verdict
    busy = {"readdir": 1, "stat": 5, "fold": 0, "db": 2, "claim": 0, "idle": 0, "other": 0, "total": 8}
    # dirs/s≈0 + 대기 많음 + 파일 stat 도는 중 → '정상' 아니라 '정체(롱테일)'
    lvl, title, _d, _r = _verdict(busy, 0.0, 5000, 2, 229086, 0, 10 ** 11)
    assert lvl == "warn" and "정체" in title, (lvl, title)
    # dirs/s 가 실제로 진행되면 정상
    lvl2, title2, _, _ = _verdict(busy, 50, 8000, 1, 229000, 0, 10 ** 11)
    assert lvl2 == "ok" and "정상" in title2, (lvl2, title2)
    # 유휴 + 대기 많음 → 굶주림(정체)
    idlew = {"readdir": 0, "stat": 0, "fold": 0, "db": 0, "claim": 1, "idle": 6, "other": 1, "total": 8}
    lvl3, title3, _, _ = _verdict(idlew, 0.0, 0, 2, 500000, 0, 10 ** 11)
    assert lvl3 == "warn" and "굶주림" in title3, (lvl3, title3)
    # 대기 적음 + 유휴 → 거의 끝남(info)
    lvl4, _t, _, _ = _verdict(idlew, 0, 0, 2, 200, 0, 10 ** 11)
    assert lvl4 == "info", lvl4
    print("[verdict] OK  dirs/s≈0+대기많음=정체(warn), 진행=정상, 유휴+대기=굶주림, 대기적음=거의끝남")


if __name__ == "__main__":
    run_rate_samples()
    run_is_problem()
    run_history()
    run_throughput()
    run_verdict()
    print("모든 테스트 통과 ✅")

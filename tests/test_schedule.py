"""예약 스캔 반복주기(schedule_due) 판정 테스트.

분/시간(간격형)과 일/주/개월(달력형)의 '시작 + 반복주기' 동작을 검증한다.
시각은 로컬 타임존으로 datetime→timestamp 변환해 결정적으로 만든다.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import settings as setmod  # noqa: E402


def ts(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi).timestamp()


def sched(**kw):
    base = {"path": "/x", "unit": "minute", "every": 1, "at": "03:00",
            "weekdays": [], "start_month": 1, "start_day": 1,
            "anchor": ts(2026, 1, 1), "last_run": 0}
    base.update(kw)
    return base


def main() -> int:
    due = setmod.schedule_due

    # 분(간격형)
    assert due(sched(unit="minute", every=30, last_run=ts(2026, 1, 1, 0, 0)), ts(2026, 1, 1, 0, 31))
    assert not due(sched(unit="minute", every=30, last_run=ts(2026, 1, 1, 0, 0)), ts(2026, 1, 1, 0, 10))

    # 시간(간격형)
    assert due(sched(unit="hour", every=2, last_run=ts(2026, 1, 1, 0, 0)), ts(2026, 1, 1, 3, 0))
    assert not due(sched(unit="hour", every=2, last_run=ts(2026, 1, 1, 0, 0)), ts(2026, 1, 1, 1, 0))

    # 일(달력형): anchor=1/1, at=03:00
    assert due(sched(unit="day", every=1, at="03:00"), ts(2026, 1, 5, 3, 1))
    assert not due(sched(unit="day", every=1, at="03:00"), ts(2026, 1, 5, 2, 59))      # 시각 전
    assert not due(sched(unit="day", every=2, at="03:00"), ts(2026, 1, 6, 3, 1))       # 정렬 안 맞음(델타5)
    assert due(sched(unit="day", every=2, at="03:00"), ts(2026, 1, 5, 3, 1))           # 델타4 %2==0
    # 오늘 슬롯 이미 실행 → 재실행 안 함
    assert not due(sched(unit="day", every=1, at="03:00", last_run=ts(2026, 1, 5, 3, 0)),
                   ts(2026, 1, 5, 3, 30))

    # 주(달력형): 1/5=월요일. weekdays=[1=월]
    assert due(sched(unit="week", every=1, at="03:00", weekdays=[1]), ts(2026, 1, 5, 3, 1))
    assert not due(sched(unit="week", every=1, at="03:00", weekdays=[1]), ts(2026, 1, 6, 3, 1))  # 화요일
    assert not due(sched(unit="week", every=2, at="03:00", weekdays=[1]), ts(2026, 1, 12, 3, 1))  # 다음주(주1)
    assert due(sched(unit="week", every=2, at="03:00", weekdays=[1]), ts(2026, 1, 5, 3, 1))       # 주0

    # 개월(달력형): anchor=1/1, start 1월 15일, 매월
    assert due(sched(unit="month", every=1, at="04:00", start_month=1, start_day=15),
               ts(2026, 1, 15, 4, 1))
    assert not due(sched(unit="month", every=1, at="04:00", start_month=1, start_day=15),
                   ts(2026, 1, 16, 4, 1))                                              # 다른 날
    assert not due(sched(unit="month", every=2, at="04:00", start_month=1, start_day=15),
                   ts(2026, 2, 15, 4, 1))                                              # 2개월마다 → 2월 제외
    assert due(sched(unit="month", every=2, at="04:00", start_month=1, start_day=15),
               ts(2026, 3, 15, 4, 1))                                                  # 3월(months=2)

    # sanitize 구버전 호환: every_minutes 만 있어도 unit=minute/every 로 채워짐
    s = setmod.sanitize({"schedules": [{"path": "/p", "every_minutes": 45}]})
    sc = s["schedules"][0]
    assert sc["unit"] == "minute" and sc["every"] == 45, sc

    # ---- 시차 배치(stagger_schedules) ----
    scheds = [
        {"path": "/n1", "unit": "minute", "every": 60, "enabled": True, "last_run": 0},
        {"path": "/n2", "unit": "minute", "every": 60, "enabled": True, "last_run": 0},
        {"path": "/n3", "unit": "day", "every": 1, "at": "02:00", "enabled": True},
        {"path": "/n4", "unit": "day", "every": 1, "at": "02:00", "enabled": True},
        {"path": "/n5", "unit": "minute", "every": 60, "enabled": False, "last_run": 0},
    ]
    st = setmod.stagger_schedules(scheds)
    assert scheds[0]["last_run"] == 0                       # 원본 비변형
    lr = [s["last_run"] for s in st if s["path"] in ("/n1", "/n2")]
    assert lr[0] != lr[1], lr                               # 간격형 위상 분산
    ats = [s["at"] for s in st if s["path"] in ("/n3", "/n4")]
    assert ats == ["02:00", "02:00"], ats                  # 달력형 at 은 보존(같은 날 중복실행 방지)
    n5 = [s for s in st if s["path"] == "/n5"][0]
    assert n5["last_run"] == 0                              # 비활성은 건드리지 않음
    # 간격형 위상: 활성 2개의 다음 실행이 주기(60분) 안에서 분산 + 즉시 실행 안 함
    for s in st:
        if s["path"] in ("/n1", "/n2"):
            assert s["last_run"] < dt.datetime.now().timestamp()   # 과거(곧 실행 대기)
    print("[schedule] OK  stagger 시차 배치(간격형만 위상 분산, 달력형 at 보존)")

    print("[schedule] OK  분/시간/일/주/개월 반복주기 판정 통과")
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

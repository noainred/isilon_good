"""튜닝 점검(systune.check) 구조 검증.

값 자체는 환경마다 다르므로(컨테이너/VM), 보고서의 '형태'와 NFS 옵션 파싱·
대상 마운트 선택 로직을 점검한다. 표준 라이브러리만 사용.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import systune  # noqa: E402

_LEVELS = {"ok", "warn", "tip", "na"}


def run_structure() -> None:
    rep = systune.check(scan_root="/usr", data_dir="/tmp")
    assert rep["ok"], rep
    assert isinstance(rep["sections"], list) and rep["sections"], rep
    names = [s["name"] for s in rep["sections"]]
    assert any("NFS" in n for n in names), names
    assert any("커널" in n for n in names), names
    for s in rep["sections"]:
        for it in s["items"]:
            assert set(("key", "current", "recommended", "level", "note")) <= set(it), it
            assert it["level"] in _LEVELS, it["level"]
    assert set(rep["summary"]) == _LEVELS, rep["summary"]
    print("[structure] OK  섹션 %d개, 요약 %s" % (len(rep["sections"]), rep["summary"]))


def run_target_pick() -> None:
    mounts = [
        {"mount": "/", "options": {}, "device": "x", "fstype": "nfs"},
        {"mount": "/mnt/hadoop", "options": {"nconnect": "16"}, "device": "y", "fstype": "nfs4"},
    ]
    t = systune._pick_target(mounts, "/mnt/hadoop/esnb-prd/x")
    assert t["mount"] == "/mnt/hadoop", t          # 가장 긴 접두 마운트 선택
    t2 = systune._pick_target(mounts, "/var/log")
    assert t2["mount"] == "/", t2
    print("[target] OK  최장 접두 마운트 선택")


def run_nfs_opts() -> None:
    # nconnect 미설정 → warn, 설정(>=4) → ok
    sec_warn = systune._nfs_section({"mount": "/m", "options": {"vers": "4.1"}})
    nc = next(it for it in sec_warn["items"] if it["key"] == "nconnect")
    assert nc["level"] == "warn", nc
    sec_ok = systune._nfs_section({"mount": "/m", "options": {"nconnect": "16", "vers": "4.1"}})
    nc2 = next(it for it in sec_ok["items"] if it["key"] == "nconnect")
    assert nc2["level"] == "ok", nc2
    # noac → warn
    sec_noac = systune._nfs_section({"mount": "/m", "options": {"noac": True}})
    assert any(it["level"] == "warn" and "noac" in it["key"] for it in sec_noac["items"]), sec_noac
    print("[nfs] OK  nconnect/noac 판정")


if __name__ == "__main__":
    run_structure()
    run_target_pick()
    run_nfs_opts()
    print("모든 테스트 통과 ✅")

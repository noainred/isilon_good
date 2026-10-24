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
    # readdirplus: nordirplus 면 warn, 아니면 tip(자동)
    rp_off = systune._nfs_section({"mount": "/m", "options": {"nordirplus": True}})
    assert any(it["key"] == "readdirplus" and it["level"] == "warn" for it in rp_off["items"]), rp_off
    rp_auto = systune._nfs_section({"mount": "/m", "options": {"vers": "4.1"}})
    assert any(it["key"] == "readdirplus" and it["level"] == "tip" for it in rp_auto["items"]), rp_auto
    print("[nfs] OK  nconnect/noac/readdirplus 판정")


def run_apply_sysctls() -> None:
    # dry_run(기본)은 시스템을 바꾸지 않고 명령만 생성(ok=None) — 화이트리스트 키만.
    res = systune.apply_sysctls(dry_run=True)
    assert res["ok"] and res["dry_run"], res
    keys = {a["key"] for a in res["applied"]}
    assert "sunrpc.tcp_slot_table_entries" in keys, keys
    assert all(a["ok"] is None for a in res["applied"]), res            # 미실행
    assert all(a["cmd"].startswith("sysctl -w ") for a in res["applied"]), res
    # 화이트리스트 밖 키(주입 시도)는 무시된다
    res2 = systune.apply_sysctls({"evil; rm -rf /": "1"}, dry_run=True)
    assert all(";" not in a["key"] for a in res2["applied"]), res2
    print("[apply] OK  dry-run 명령 생성·화이트리스트 차단")


if __name__ == "__main__":
    run_structure()
    run_target_pick()
    run_nfs_opts()
    run_apply_sysctls()
    print("모든 테스트 통과 ✅")

"""관리(매니저) DB 통합 검증.

서로 다른 두 경로를 같은 data-dir 로 스캔했을 때:
  - 실행마다 별도의 per-run DB 파일이 scans/ 아래 생기는지
  - manager.db 에 각 스캔 요약이 등록/갱신되는지
  - 전체 용량 집계(overall_capacity)가 루트별 최신 합계로 맞는지
를 확인한다.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import db as dbmod        # noqa: E402
from isilon_usage import manager as mgr     # noqa: E402
from isilon_usage.scanner import Scanner    # noqa: E402


def _tree(root: str, files: int, size: int) -> int:
    os.makedirs(root, exist_ok=True)
    total = 0
    for i in range(files):
        p = os.path.join(root, f"f{i}.bin")
        with open(p, "wb") as fh:
            fh.write(b"\0" * size)
        total += size
    sub = os.path.join(root, "sub")
    os.makedirs(sub, exist_ok=True)
    for i in range(files):
        with open(os.path.join(sub, f"g{i}.bin"), "wb") as fh:
            fh.write(b"\0" * size)
    return total


def _run_one(data_dir: str, root: str) -> int:
    """CLI 의 _prepare_run + 스캔을 흉내내어 manager scan_id 를 반환."""
    mgr.init_manager(data_dir)
    db_path = mgr.make_run_db_path(data_dir, root)
    scan_id = mgr.register_scan(data_dir, root_path=root, db_path=db_path,
                                backend="native", size_mode="disk")
    dbmod.init_db(db_path)
    s = Scanner(db_path, root, backend="native", batch_size=4,
                manager_db=mgr.manager_db_path(data_dir), manager_scan_id=scan_id)
    s.run()
    return scan_id


def _test_migrate_legacy_config() -> None:
    """레거시 위치의 설정이 기본 data-dir 로 1회 이관되는지(비파괴·멱등) 검증."""
    tmp = tempfile.mkdtemp(prefix="isilon_mig_")
    try:
        legacy = os.path.join(tmp, "legacy")
        target = os.path.join(tmp, "data_isilon_usage")
        os.makedirs(legacy, exist_ok=True)
        with open(os.path.join(legacy, "settings.json"), "w") as fh:
            fh.write('{"api_token": "KEEP"}')
        with open(os.path.join(legacy, "portal_nodes.json"), "w") as fh:
            fh.write('{"nodes": []}')
        # 실제 /data 를 건드리지 않도록 기본/레거시 경로를 임시값으로 교체
        old_def, old_leg = mgr.DEFAULT_DATA_DIR, mgr.LEGACY_DATA_DIRS
        mgr.DEFAULT_DATA_DIR = target
        mgr.LEGACY_DATA_DIRS = (legacy,)
        try:
            # 1) 기본이 아닌 경로는 이관하지 않는다(가드)
            other = os.path.join(tmp, "other")
            mgr.migrate_legacy_config(other)
            assert not os.path.exists(os.path.join(other, "settings.json"))
            # 2) 기본 경로면 설정을 이관한다
            mgr.migrate_legacy_config(target)
            assert os.path.exists(os.path.join(target, "settings.json"))
            assert os.path.exists(os.path.join(target, "portal_nodes.json"))
            with open(os.path.join(target, "settings.json")) as fh:
                assert "KEEP" in fh.read()
            # 3) 원본(레거시)은 비파괴로 남는다
            assert os.path.exists(os.path.join(legacy, "settings.json"))
            # 4) 이미 있으면 덮어쓰지 않는다(멱등)
            with open(os.path.join(target, "settings.json"), "w") as fh:
                fh.write('{"api_token": "NEW"}')
            mgr.migrate_legacy_config(target)
            with open(os.path.join(target, "settings.json")) as fh:
                assert "NEW" in fh.read()
        finally:
            mgr.DEFAULT_DATA_DIR, mgr.LEGACY_DATA_DIRS = old_def, old_leg
        print("[manager] migrate_legacy_config OK (이관·비파괴·멱등)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    _test_migrate_legacy_config()
    tmp = tempfile.mkdtemp(prefix="isilon_mgr_")
    try:
        data_dir = os.path.join(tmp, "isidata")
        rootA = os.path.join(tmp, "A")
        rootB = os.path.join(tmp, "B")
        _tree(rootA, 5, 1000)
        _tree(rootB, 8, 2000)

        idA = _run_one(data_dir, rootA)
        idB = _run_one(data_dir, rootB)
        assert idA == 1 and idB == 2, (idA, idB)

        # per-run DB 파일이 2개 생겼는지
        scan_files = os.listdir(mgr.scans_dir(data_dir))
        assert len([f for f in scan_files if f.endswith(".db")]) == 2, scan_files

        mconn = dbmod.connect(mgr.manager_db_path(data_dir))
        try:
            rows = mgr.list_scans(mconn)
            assert len(rows) == 2, rows
            for r in rows:
                assert r["status"] == "done", r["status"]
                assert r["scanned_bytes"] > 0
                assert os.path.exists(r["db_path"]), r["db_path"]

            overall = mgr.overall_capacity(mconn)
            assert overall["scan_count"] == 2
            assert overall["root_count"] == 2
            assert overall["active_scans"] == 0
            # 두 루트의 scanned_bytes 합 == 총 조사 용량
            by_root = {x["root_path"]: x["scanned_bytes"] for x in overall["roots"]}
            assert overall["total_scanned_bytes"] == by_root[rootA] + by_root[rootB]

            # per-run DB 의 루트 재귀 용량 == manager 의 scanned_bytes
            ra = mgr.get_scan(mconn, idA)
            pconn = dbmod.connect(ra["db_path"])
            try:
                root_bytes = pconn.execute(
                    "SELECT total_bytes FROM directories WHERE parent_id IS NULL"
                ).fetchone()["total_bytes"]
            finally:
                pconn.close()
            assert root_bytes == ra["scanned_bytes"], (root_bytes, ra["scanned_bytes"])
        finally:
            mconn.close()

        # 같은 루트 재스캔 시 "최신만" 집계되는지(중복 합산 방지)
        _run_one(data_dir, rootA)
        mconn = dbmod.connect(mgr.manager_db_path(data_dir))
        try:
            overall2 = mgr.overall_capacity(mconn)
            assert overall2["scan_count"] == 3      # 스캔은 3개
            assert overall2["root_count"] == 2      # 루트는 여전히 2개
        finally:
            mconn.close()

        print("[manager] OK  scans=3 roots=2  총조사=%d" % overall["total_scanned_bytes"])
        print("모든 테스트 통과 ✅")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

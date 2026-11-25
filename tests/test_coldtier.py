"""coldtier.py — 콜드데이터 이동 '계획·스크립트' 생성 테스트(조회 전용·파일 미변경)."""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import coldtier  # noqa: E402


def _w(path: str, data: bytes, age_days: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    t = time.time() - age_days * 86400.0
    os.utime(path, (t, t))   # atime, mtime 모두 과거로


def main() -> int:
    d = tempfile.mkdtemp(prefix="iu_cold_")
    tgt = tempfile.mkdtemp(prefix="iu_coldtgt_")
    try:
        _w(os.path.join(d, "old/a.bin"), b"X" * 1000, age_days=400)   # 콜드(1000B)
        _w(os.path.join(d, "old/b.bin"), b"Y" * 2000, age_days=500)   # 콜드(2000B)
        _w(os.path.join(d, "warm/c.bin"), b"Z" * 9999, age_days=10)   # 핫 → 제외
        _w(os.path.join(d, "root_old.bin"), b"R" * 500, age_days=800)  # 루트 직속 콜드

        r = coldtier.plan_cold_move(d, days=365, field="mtime", target=tgt)
        assert r["ok"], r
        # 콜드 = a(1000) + b(2000) + root_old(500) = 3500B, 3개. warm 제외.
        assert r["cold_file_count"] == 3, r
        assert r["total_cold_bytes"] == 3500, r
        assert r["oldest_age_days"] >= 799, r          # root_old ~800일
        # 상위 디렉터리 집계: old(3000) > (루트)(500). warm 없음.
        tops = {t["dir"]: t["bytes"] for t in r["top_dirs"]}
        assert tops.get("old") == 3000 and tops.get("(루트)") == 500, r["top_dirs"]
        assert "warm" not in tops, r["top_dirs"]

        # 스크립트 안전장치: 기본 DRY-RUN, pipefail, -xdev, ignore-existing, find 기준 일치.
        s = r["script"]
        assert 'DRY_RUN="${DRY_RUN:-1}"' in s, s
        assert "set -euo pipefail" in s, s
        assert "-xdev" in s and "-mtime +" in s, s
        # 데이터 손실 방지: --ignore-existing(대상에 있는 건 안 건드림) + -H(하드링크 보존).
        assert "rsync -aH --ignore-existing" in s, s
        assert "--remove-source-files" in s and "--from0" in s, s
        # 매니페스트는 플랜별 고유 절대경로(여러 플랜 충돌 방지) + 덮어쓰기 보호.
        assert "/var/tmp/coldmove_manifest_" in s, s
        assert "기존 매니페스트 있음" in s, s
        # 루트/타깃이 단일따옴표로 감싸여 주입 안전.
        assert ("SRC='" + d + "'") in s, s
        # 롤백은 DST→SRC 로 같은(플랜별 고유) 매니페스트 사용 + 같은 안전 플래그.
        rb = r["rollback"]
        assert ("SRC='" + d + "'") in rb and ("DST='" + tgt + "'") in rb, rb
        assert "rsync -aH --ignore-existing" in rb and 'DRY_RUN="${DRY_RUN:-1}"' in rb, rb
        # 이동·롤백이 같은 매니페스트 경로를 가리켜야 롤백이 정확.
        import re
        mf_move = re.search(r"coldmove_manifest_(\w+)\.lst", s).group(1)
        mf_back = re.search(r"coldmove_manifest_(\w+)\.lst", rb).group(1)
        assert mf_move == mf_back, (mf_move, mf_back)
        # 다른 플랜(다른 days)은 다른 매니페스트 → 충돌·롤백 오염 방지.
        s2 = coldtier.plan_cold_move(d, days=100, field="mtime", target=tgt)["script"]
        assert re.search(r"coldmove_manifest_(\w+)\.lst", s2).group(1) != mf_move

        # min_size: 1000B 미만 제외 → root_old(500) 빠지고 a/b 만(3000B).
        r2 = coldtier.plan_cold_move(d, days=365, field="mtime", target=tgt, min_size=1000)
        assert r2["total_cold_bytes"] == 3000 and r2["cold_file_count"] == 2, r2
        # find -size +Nc 는 'N 초과'라 미리보기(>=1000)와 맞추려 +999c.
        assert "-size +999c" in r2["script"], r2["script"]

        # 하드링크는 물리 1회분 — 한 번만 집계(회수 가능 용량 과대계상 방지).
        try:
            os.link(os.path.join(d, "root_old.bin"), os.path.join(d, "root_old_hl.bin"))
            os.utime(os.path.join(d, "root_old_hl.bin"),
                     (time.time() - 800 * 86400, time.time() - 800 * 86400))
            r3 = coldtier.plan_cold_move(d, days=365, field="mtime")
            assert r3["total_cold_bytes"] == 3500, r3   # 변동 없음(500B 한 번만)
            assert r3["cold_file_count"] == 3, r3
        except OSError:
            pass   # 하드링크 미지원 파일시스템이면 건너뜀

        # 타깃이 루트 안이면 거부(자기 자신으로 이동 방지).
        bad = coldtier.plan_cold_move(d, days=365, target=os.path.join(d, "old"))
        assert not bad["ok"], bad
        # 역방향(루트가 타깃 안)도 거부.
        bad2 = coldtier.plan_cold_move(os.path.join(d, "old"), days=365, target=d)
        assert not bad2["ok"], bad2

        # target 없으면 스크립트 미생성(미리보기만).
        r4 = coldtier.plan_cold_move(d, days=365, field="atime")
        assert r4["ok"] and "script" not in r4, r4

        # 셸 인용 이스케이프: 작은따옴표 든 경로도 안전.
        weird = os.path.join(d, "it's")
        os.makedirs(weird, exist_ok=True)
        assert "'\\''" in coldtier.build_move_script(weird, tgt, 1, "mtime")

        # 디렉터리 아님 → ok:False
        assert not coldtier.plan_cold_move(os.path.join(d, "nope"))["ok"]

        # 도구는 파일을 옮기지 않았다(조회 전용) — 타깃 비어 있어야 함.
        assert not os.listdir(tgt), "coldtier 가 파일을 옮기면 안 됨(조회 전용)"

        print("[coldtier] 콜드데이터 이동 계획·스크립트(ignore-existing·하드링크1회·고유매니페스트·롤백·조회전용) OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(tgt, ignore_errors=True)
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

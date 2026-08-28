"""atimes — 특정 폴더 atime 라이브 조회(용량 계산과 분리) 검증. 표준 라이브러리만.

임시 트리에 atime 을 명시적으로 지정(os.utime)하고 정렬(오래된 순)·limit·재귀·오류를 점검한다.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import atimes  # noqa: E402


def run_basic() -> None:
    d = tempfile.mkdtemp(prefix="iu_at_")
    try:
        # 파일 3개 + 하위폴더 1개(그 안에 파일 1개)
        for nm, at in (("new.txt", 1_700_000_000), ("old.txt", 1_500_000_000),
                       ("mid.txt", 1_600_000_000)):
            p = os.path.join(d, nm)
            with open(p, "w") as fh:
                fh.write("x")
            os.utime(p, (at, at))
        sub = os.path.join(d, "sub")
        os.mkdir(sub)
        cp = os.path.join(sub, "deep.txt")
        with open(cp, "w") as fh:
            fh.write("y")
        os.utime(cp, (1_400_000_000, 1_400_000_000))

        # 비재귀: 직속 항목(파일 3 + 폴더 1) = 4, atime 오래된 순
        r = atimes.list_access_times(d, recursive=False)
        assert r["ok"], r
        assert r["count"] == 4, r["count"]
        files = [e for e in r["entries"] if not e["is_dir"]]
        names = [e["name"] for e in files]
        assert names == ["old.txt", "mid.txt", "new.txt"], names   # 오래된 접근 먼저
        assert any(e["is_dir"] and e["name"] == "sub" for e in r["entries"]), r["entries"]
        assert "deep.txt" not in [e["name"] for e in r["entries"]], "비재귀인데 하위 포함됨"
        print("[basic] OK  직속 항목·atime 오래된 순 정렬·폴더 포함")

        # 재귀: 하위 deep.txt 포함, 가장 오래된 atime 이라 맨 앞
        rr = atimes.list_access_times(d, recursive=True)
        assert rr["ok"] and any(e["name"] == "deep.txt" for e in rr["entries"]), rr
        assert rr["entries"][0]["name"] == "deep.txt", rr["entries"][0]   # 1.4e9 가 최소
        print("[recursive] OK  하위 파일 포함·전체 atime 정렬")

        # limit: 1개로 자르면 truncated
        rl = atimes.list_access_times(d, recursive=True, limit=1)
        assert rl["ok"] and rl["count"] == 1 and rl["truncated"], rl
        print("[limit] OK  최대치 도달 시 truncated")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_errors() -> None:
    assert not atimes.list_access_times("/no/such/dir/xyz")["ok"]
    # 파일을 경로로 주면 디렉터리 아님
    d = tempfile.mkdtemp(prefix="iu_at_")
    try:
        f = os.path.join(d, "f.txt")
        open(f, "w").close()
        assert not atimes.list_access_times(f)["ok"], "파일 경로는 거부해야"
        print("[errors] OK  없는 경로·파일 경로 거부")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    run_basic()
    run_errors()
    print("모든 테스트 통과 ✅")

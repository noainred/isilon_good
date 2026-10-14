"""dups.py — 내용 기반 중복 파일 탐지 테스트(깔때기·하드링크·부분해시 충돌 구분)."""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import dups  # noqa: E402


def _w(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def main() -> int:
    d = tempfile.mkdtemp(prefix="iu_dups_")
    try:
        same = b"X" * 10000
        _w(os.path.join(d, "a.bin"), same)          # 중복 그룹(3개)
        _w(os.path.join(d, "sub/b.bin"), same)
        _w(os.path.join(d, "c.bin"), same)
        _w(os.path.join(d, "uniq.bin"), b"Y" * 10000)   # 같은 크기·다른 내용
        _w(os.path.join(d, "tiny.bin"), b"Z" * 10)      # min_size 미만 → 제외
        # 부분해시(앞 4KB)는 같지만 전체는 다른 두 파일 → 중복 아님(전체해시 단계가 구분)
        _w(os.path.join(d, "p1.bin"), b"P" * 5000 + b"A" * 5000)
        _w(os.path.join(d, "p2.bin"), b"P" * 5000 + b"B" * 5000)

        r = dups.find_duplicates(d, min_size=4096)
        assert r["ok"], r
        # 진짜 중복은 a/b/c 한 그룹뿐(uniq·tiny 제외, p1/p2 는 전체해시 다름)
        assert r["group_count"] == 1, r["groups"]
        g = r["groups"][0]
        assert g["count"] == 3 and g["size"] == 10000, g
        assert g["recoverable"] == 10000 * 2, g                  # size*(n-1)
        assert r["total_recoverable"] == 20000, r
        assert not any("p1.bin" in p or "p2.bin" in p
                       for grp in r["groups"] for p in grp["paths"]), r  # 부분충돌 오탐 없음

        # 하드링크는 한 번만(중복 아님): a.bin 에 하드링크 → 그룹 수/회수 불변
        try:
            os.link(os.path.join(d, "a.bin"), os.path.join(d, "a_hl.bin"))
            r2 = dups.find_duplicates(d, min_size=4096)
            assert r2["group_count"] == 1 and r2["total_recoverable"] == 20000, r2
        except OSError:
            pass   # 하드링크 미지원 파일시스템이면 건너뜀

        # 디렉터리 아님
        assert not dups.find_duplicates(os.path.join(d, "a.bin"))["ok"]
        print("[dups] 중복 탐지(크기→부분→전체 깔때기·하드링크 제외·부분해시충돌 구분) OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""테스트/데모용 합성 디렉터리 트리 생성기.

실제 아이실론 없이도 스캐너/대시보드를 시험해 볼 수 있도록, 지정한 깊이/폭/
파일 수로 디렉터리 트리를 만든다.

예)
  python tools/make_tree.py /tmp/isilon_demo --depth 4 --breadth 4 --files 20 --size 4096
"""

from __future__ import annotations

import argparse
import os
import random


def build(root: str, depth: int, breadth: int, files: int, size: int,
          rng: random.Random) -> tuple[int, int, int]:
    """재귀적으로 트리를 만들고 (디렉터리수, 파일수, 총바이트) 반환."""
    os.makedirs(root, exist_ok=True)
    n_dirs, n_files, n_bytes = 1, 0, 0

    for i in range(files):
        fsize = max(0, int(rng.gauss(size, size / 2)))
        fpath = os.path.join(root, f"file_{i:03d}.bin")
        with open(fpath, "wb") as fh:
            if fsize:
                fh.write(b"\0" * fsize)
        n_files += 1
        n_bytes += fsize

    if depth > 0:
        for b in range(breadth):
            sub = os.path.join(root, f"dir_{b:02d}")
            d, f, by = build(sub, depth - 1, breadth, files, size, rng)
            n_dirs += d
            n_files += f
            n_bytes += by
    return n_dirs, n_files, n_bytes


def main() -> int:
    ap = argparse.ArgumentParser(description="합성 디렉터리 트리 생성기")
    ap.add_argument("root", help="생성할 루트 경로")
    ap.add_argument("--depth", type=int, default=3, help="디렉터리 깊이")
    ap.add_argument("--breadth", type=int, default=3, help="레벨당 하위 디렉터리 수")
    ap.add_argument("--files", type=int, default=10, help="디렉터리당 파일 수")
    ap.add_argument("--size", type=int, default=2048, help="파일 평균 크기(바이트)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    d, f, by = build(args.root, args.depth, args.breadth, args.files, args.size, rng)
    print(f"생성 완료: {args.root}")
    print(f"  디렉터리 {d:,}개, 파일 {f:,}개, 논리 총량 약 {by/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

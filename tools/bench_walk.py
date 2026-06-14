#!/usr/bin/env python3
"""bench_walk — 파일 탐색(walk) 동시성 처방을 '실 NAS'에서 측정하는 벤치마크.

핵심 질문: "더 많은 파일을 더 빨리 찾기" = 메타데이터 syscall(readdir/stat) 처리량.
os.stat/os.scandir 은 syscall 동안 GIL 을 푼다(=다른 스레드가 그 사이 진행 가능).
그래서 고지연 NAS 에서는 '동시에 떠 있는 stat 개수'가 곧 처리량이고, 처방은
프로세스(=GIL 회피) × 프로세스당 스레드(=왕복 지연 은닉)의 2단 병렬이다.

두 가지 모드:
  1) --path <실제경로>  : 진짜 NAS 디렉터리를 여러 전략으로 훑어 files/s 를 비교(읽기 전용).
  2) (경로 없음)        : 합성 트리 + 지연 주입(--lat)으로 메커니즘을 모사(로컬에서도 확인).

읽기 전용(스캔만) — 아무것도 쓰지 않는다. 결과는 전략별 소요/처리량/배속.
"정직성: 합성+주입은 메커니즘 증명용일 뿐, 실수치는 반드시 --path 로 실 NAS에서 측정."
"""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor

LAT = 0.0  # 파일당 주입 지연(초). 합성 모드에서 NFS 왕복 모사(fork 로 자식에 상속).


def _cost(e):
    if LAT:
        time.sleep(LAT)
    return e.stat(follow_symlinks=False).st_size


def walk_serial(root: str):
    files = b = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        b += _cost(e)
                        files += 1
        except OSError:
            pass
    return files, b


def walk_threads(root: str, t: int):
    q: "queue.Queue[str]" = queue.Queue()
    q.put(root)
    lock = threading.Lock()
    tot = [0, 0, 1]   # files, bytes, pending dirs

    def worker():
        while True:
            try:
                d = q.get(timeout=0.1)
            except queue.Empty:
                with lock:
                    if tot[2] == 0:
                        return
                continue
            lf = lb = 0
            subs = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            subs.append(e.path)
                        else:
                            lb += _cost(e)
                            lf += 1
            except OSError:
                pass
            with lock:
                tot[0] += lf
                tot[1] += lb
                for s in subs:
                    q.put(s)
                    tot[2] += 1
                tot[2] -= 1

    ts = [threading.Thread(target=worker) for _ in range(t)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    return tot[0], tot[1]


def _sub(d):
    return walk_serial(d)


def _sub_threaded(args):
    d, t = args
    return walk_threads(d, t)


def walk_procs(root: str, p: int, t: int = 1):
    children = [e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    if not children:
        return walk_serial(root)
    files = b = 0
    with ProcessPoolExecutor(max_workers=p) as ex:
        it = (ex.map(_sub_threaded, [(c, t) for c in children]) if t > 1
              else ex.map(_sub, children))
        for f, bb in it:
            files += f
            b += bb
    return files, b


def _build(root, top, mid, leaf, files):
    n = 0
    for a in range(top):
        for b in range(mid):
            for c in range(leaf):
                d = os.path.join(root, "t%d" % a, "m%d" % b, "l%d" % c)
                os.makedirs(d, exist_ok=True)
                for f in range(files):
                    open(os.path.join(d, "f%d" % f), "wb").close()
                    n += 1
    return n


def _run(root, strategies):
    base = None
    for name, fn, args in strategies:
        t0 = time.time()
        f, _ = fn(root, *args)
        dt = time.time() - t0
        rate = f / dt if dt else 0
        if base is None:
            base = rate or 1
        print(f"  {name:<16} {dt:7.3f}s  {rate:12,.0f} files/s  {rate / base:5.2f}x")


def main():
    global LAT
    ap = argparse.ArgumentParser(description="파일 탐색 동시성 벤치(읽기 전용)")
    ap.add_argument("--path", help="실제 측정할 NAS 디렉터리(주면 합성/지연주입 안 함)")
    ap.add_argument("--procs", type=int, default=8, help="프로세스 수(기본 8)")
    ap.add_argument("--threads", type=int, default=8, help="프로세스당/단독 스레드 수(기본 8)")
    ap.add_argument("--lat", type=float, default=0.0002,
                    help="합성 모드 파일당 주입 지연 초(기본 0.2ms=NFS 모사)")
    ap.add_argument("--tree", default="8,10,6,40",
                    help="합성 트리 top,mid,leaf,files (기본 8,10,6,40 ≈ 19,200 파일)")
    args = ap.parse_args()

    P, T = max(1, args.procs), max(1, args.threads)
    strategies = [
        ("serial", walk_serial, ()),
        ("threads x%d" % T, walk_threads, (T,)),
        ("threads x%d" % (T * 4), walk_threads, (T * 4,)),
        ("procs x%d" % P, walk_procs, (P, 1)),
        ("procs%d x thr%d" % (max(1, P // 2), T), walk_procs, (max(1, P // 2), T)),
        ("procs%d x thr%d" % (P, T), walk_procs, (P, T)),
    ]

    if args.path:
        root = os.path.abspath(args.path)
        if not os.path.isdir(root):
            raise SystemExit("디렉터리가 아님: %s" % root)
        LAT = 0.0
        print("실측: %s (읽기 전용, 캐시 영향 줄이려면 두 번째 실행 값 사용)\n" % root)
        _run(root, strategies)
        print("\n참고: 같은 경로를 반복하면 NAS/OS 캐시로 빨라집니다. 가능하면 cold 상태로,")
        print("또는 충분히 큰 트리로 측정하세요. 프로세스×스레드가 가장 빠르면 2단 병렬이 답.")
        return

    top, mid, leaf, files = (int(x) for x in args.tree.split(","))
    tmp = tempfile.mkdtemp(prefix="benchwalk_")
    try:
        n = _build(tmp, top, mid, leaf, files)
        print(f"합성 트리: {n:,} 파일 (top={top} mid={mid} leaf={leaf} files={files})\n")
        for lat in (0.0, args.lat):
            LAT = lat
            print("== %s ==" % ("L=0(로컬 빠름)" if lat == 0 else "L=%.1fms(NFS 모사)" % (lat * 1000)))
            _run(tmp, strategies)
            print()
        print("해석: 로컬(L=0)에선 스레드가 손해(GIL+락), NFS 영역(L>0)에선 동시성이 왕복을")
        print("숨겨 프로세스×스레드가 최고. 실수치는 --path 로 실 NAS에서 측정하세요.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

"""명령행 인터페이스.

서브커맨드:
  run    <경로>   스캔 + 대시보드를 한 번에 실행(권장).
  scan   <경로>   스캔만 수행(대시보드 없이, 자원 모니터링 포함).
  serve           대시보드 웹서버만 실행(이미 만들어진 DB 를 읽음).
  status          현재 진행 상태를 콘솔에 출력.

예)
  python -m isilon_usage run /ifs/data --port 8765
  python -m isilon_usage scan /ifs/data --backend du
  python -m isilon_usage serve --db ./isilon_scan.db --port 8765
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from . import db as dbmod
from . import monitor as monmod
from . import manager as mgrmod
from .scanner import run_scan
from .server import serve, build_status


DEFAULT_DATA_DIR = mgrmod.DEFAULT_DATA_DIR


def _add_scan_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                   help="관리 DB(manager.db)와 per-run DB(scans/)를 둘 상위 폴더 "
                        "(기본: %(default)s). 실행마다 scans/ 아래에 새 DB 가 생긴다.")
    p.add_argument("--db", default=None,
                   help="이 실행의 per-run DB 경로를 직접 지정(기본: data-dir/scans 아래 자동 생성)")
    p.add_argument("--backend", choices=["native", "du"], default="native",
                   help="용량 측정 방식. native=내장(빠름·저메모리, 기본), "
                        "du=디렉터리마다 시스템 du 실행(du 프로세스 메모리 표시)")
    p.add_argument("--size-mode", choices=["disk", "apparent"], default="disk",
                   help="disk=실제 디스크 점유(du 기본), apparent=논리 파일 크기")
    p.add_argument("--one-file-system", "-x", action="store_true",
                   help="다른 파일시스템으로 넘어가지 않음(du -x 와 동일)")
    p.add_argument("--batch-size", type=int, default=500,
                   help="DB 커밋 배치 크기 (기본: %(default)s)")
    p.add_argument("--sample-interval", type=float, default=2.0,
                   help="자원 샘플링 주기(초) (기본: %(default)s)")


def _human(n) -> str:
    n = float(n or 0)
    for u in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024 or u == "PB":
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def _dashboard_url(host: str, port: int) -> str:
    shown = "localhost" if host in ("0.0.0.0", "", "::") else host
    return f"http://{shown}:{port}/"


def _prepare_run(args, path: str):
    """data-dir 준비 → per-run DB 경로 결정 → 관리 DB 에 등록.

    반환: (data_dir, per_run_db_path, manager_db_path, manager_scan_id)
    """
    data_dir = os.path.abspath(args.data_dir)
    mgrmod.init_manager(data_dir)
    db_path = os.path.abspath(args.db) if args.db else mgrmod.make_run_db_path(data_dir, path)
    manager_db = mgrmod.manager_db_path(data_dir)
    scan_id = mgrmod.register_scan(
        data_dir, root_path=path, db_path=db_path,
        backend=args.backend, size_mode=args.size_mode,
    )
    return data_dir, db_path, manager_db, scan_id


def _run_server(args, *, initial_path: str | None) -> int:
    """serve/run 공통: 웹 스캔 가능한 대시보드 서버를 띄운다.

    initial_path 가 주어지면(=run) 그 경로 스캔을 즉시 시작한다. 어느 경우든
    웹페이지에서 디렉터리를 지정해 추가 스캔을 시작/중지할 수 있다.
    """
    data_dir = os.path.abspath(args.data_dir)
    mount_bases = [os.path.abspath(b) for b in (getattr(args, "mount_base", None) or [])]

    httpd = serve(
        data_dir, host=args.host, port=args.port,
        mount_bases=mount_bases, enable_scan=True,
        sample_interval=args.sample_interval, batch_size=args.batch_size,
    )
    url = _dashboard_url(args.host, args.port)

    print("=" * 64)
    print(f"  Isilon 사용량 대시보드 (웹에서 디렉터리 지정 스캔 가능)")
    print(f"  대시보드:  {url}")
    print(f"  데이터:    {data_dir}  (manager.db + scans/)")
    if mount_bases:
        print(f"  허용 경로: {', '.join(mount_bases)}")
    else:
        print(f"  허용 경로: (제한 없음 — --mount-base 로 제한 권장)")
    print(f"  psutil:    {'있음' if monmod.have_psutil() else '없음(/proc 폴백)'}")

    if initial_path:
        res = httpd.controller.start_scan(
            initial_path, backend=args.backend, size_mode=args.size_mode,
            one_file_system=args.one_file_system,
        )
        if res.get("ok"):
            print(f"  초기 스캔: {initial_path}  (scan #{res['scan_id']})")
        else:
            print(f"  초기 스캔 실패: {res.get('reason')}", file=sys.stderr)
    print("  중지하려면 Ctrl+C")
    print("=" * 64)

    def handle_sigint(signum, frame):
        print("\n중지 신호 수신 — 정리 중…", file=sys.stderr)
        if httpd.controller:
            httpd.controller.stop_all()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    try:
        httpd.serve_forever()
    finally:
        if httpd.controller:
            httpd.controller.stop_all()
        httpd.server_close()
    print("종료되었습니다.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"오류: 디렉터리가 아닙니다: {path}", file=sys.stderr)
        return 2
    return _run_server(args, initial_path=path)


def cmd_scan(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"오류: 디렉터리가 아닙니다: {path}", file=sys.stderr)
        return 2

    stop = threading.Event()

    def handle_sigint(signum, frame):
        print("\n중지 신호 수신 — 정리 중…", file=sys.stderr)
        stop.set()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    data_dir, db_path, manager_db, scan_id = _prepare_run(args, path)
    print(f"스캔 시작: {path} (scan #{scan_id}, backend={args.backend})")
    print(f"  이번 DB: {db_path}")
    t0 = time.time()
    run_scan(
        db_path, path,
        backend=args.backend, size_mode=args.size_mode,
        one_file_system=args.one_file_system,
        batch_size=args.batch_size, sample_interval=args.sample_interval,
        stop_event=stop, with_monitor=True,
        manager_db=manager_db, manager_scan_id=scan_id,
    )
    print(f"완료(scan #{scan_id}). 소요 {time.time()-t0:.1f}s. "
          f"대시보드로 보려면: python -m isilon_usage serve --data-dir {data_dir}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # serve 는 초기 스캔 없이 대시보드만 띄우되, 웹에서 디렉터리를 지정해 스캔할 수 있다.
    return _run_server(args, initial_path=None)


def cmd_status(args: argparse.Namespace) -> int:
    data_dir = os.path.abspath(args.data_dir)
    manager_db = mgrmod.manager_db_path(data_dir)
    if not os.path.exists(manager_db):
        print(f"관리 DB 가 없습니다: {manager_db}", file=sys.stderr)
        return 2
    mconn = dbmod.connect(manager_db)
    try:
        overall = mgrmod.overall_capacity(mconn)
        # ── 전체 용량 관리 개요 ──────────────────────────────────────
        print("=" * 70)
        print("전체 용량 관리 개요")
        print(f"  등록된 스캔 : {overall['scan_count']}개  "
              f"(루트 {overall['root_count']}개, 진행중 {overall['active_scans']}개)")
        print(f"  총 조사 용량: {_human(overall['total_scanned_bytes'])} "
              f"(루트별 최신 스캔 합계)")
        if overall["roots"]:
            print("  루트별 최신:")
            for r in overall["roots"]:
                pct = (r["scanned_bytes"] / r["fs_used_bytes"] * 100.0) if r["fs_used_bytes"] else 0
                print(f"    - {_human(r['scanned_bytes']):>10}  "
                      f"({pct:4.1f}% of used)  [{r['status']}]  {r['root_path']}")
        print("=" * 70)

        # ── 특정/최신 스캔 상세 ─────────────────────────────────────
        scan_id = getattr(args, "scan", None) or mgrmod.pick_default_scan(mconn)
        if scan_id is None:
            return 0
        row = mgrmod.get_scan(mconn, scan_id)
        if row is None:
            print(f"scan #{scan_id} 없음", file=sys.stderr)
            return 1
        db_path = row["db_path"]
    finally:
        mconn.close()

    print(f"\n[scan #{scan_id}] 상세")
    if not os.path.exists(db_path):
        print("  (초기화 중 — per-run DB 아직 없음)")
        return 0
    conn = dbmod.connect(db_path)
    try:
        st = build_status(conn, None, samples=1, top=10)
    finally:
        conn.close()
    if not st.get("ok"):
        print(f"  상태 없음: {st.get('reason') or st.get('error')}")
        return 1
    r, p = st["run"], st["progress"]
    print(f"  경로        : {r['root_path']}")
    print(f"  상태/단계   : {r['status']} / {r['phase']}  (backend={r['backend']})")
    print(f"  디렉터리    : 탐색 {r['discovered_dirs']:,}  집계 {r['processed_dirs']:,}"
          f" / 총 {r['total_dirs']:,}")
    print(f"  파일 수     : {r['total_files']:,}   오류 디렉터리: {r['error_dirs']:,}")
    print(f"  확인 사용량 : {_human(r['scanned_bytes'])} "
          f"(사용량의 {p['disk_verified_pct_of_used']:.1f}%, "
          f"전체의 {p['disk_verified_pct_of_total']:.1f}%)")
    print(f"  디스크      : 사용 {_human(r['fs_used_bytes'])} / 전체 {_human(r['fs_total_bytes'])}")
    print(f"  경과        : {r['elapsed']:.1f}s   현재: {r['current_dir'] or '-'}")
    L = st["resources"].get("latest")
    if L:
        print(f"  메모리      : {L['mem_percent']:.1f}%  "
              f"스캐너RSS {_human(L['scanner_rss'])}  du RSS {_human(L['du_rss'])}")
    if st["top_dirs"]:
        print("  상위 디렉터리:")
        for i, t in enumerate(st["top_dirs"][:10], 1):
            sz = t["total_bytes"] or t["own_bytes"]
            print(f"    {i:2}. {_human(sz):>10}  {t['path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isilon_usage",
        description="아이실론 등 초대용량 NAS 의 디렉터리별 사용량을 메모리 최소로 "
                    "조사하고 웹 대시보드로 진행 상황을 보여주는 도구.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="초기 스캔 + 대시보드 실행(웹에서 추가 스캔도 가능)")
    pr.add_argument("path", help="조사할 루트 디렉터리")
    _add_scan_opts(pr)
    pr.add_argument("--mount-base", action="append", default=[],
                    help="웹에서 스캔을 허용할 경로(여러 번 지정 가능). 예: 마운트 지점")
    pr.add_argument("--host", default="0.0.0.0")
    pr.add_argument("--port", type=int, default=8765)
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("scan", help="스캔만 수행(대시보드 없이)")
    ps.add_argument("path", help="조사할 루트 디렉터리")
    _add_scan_opts(ps)
    ps.set_defaults(func=cmd_scan)

    pv = sub.add_parser("serve", help="대시보드 실행 + 웹에서 디렉터리 지정 스캔(권장)")
    pv.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help="manager.db + scans/ 상위 폴더 (기본: %(default)s)")
    pv.add_argument("--mount-base", action="append", default=[],
                    help="웹에서 스캔을 허용할 경로(여러 번 지정 가능). 예: /mnt/isilon")
    pv.add_argument("--backend", choices=["native", "du"], default="native",
                    help="웹 스캔 기본 백엔드")
    pv.add_argument("--size-mode", choices=["disk", "apparent"], default="disk")
    pv.add_argument("--one-file-system", "-x", action="store_true")
    pv.add_argument("--batch-size", type=int, default=500)
    pv.add_argument("--sample-interval", type=float, default=2.0)
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=8765)
    pv.set_defaults(func=cmd_serve)

    pst = sub.add_parser("status", help="전체 관리 개요 + 스캔 상태를 콘솔에 출력")
    pst.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                     help="manager.db + scans/ 상위 폴더 (기본: %(default)s)")
    pst.add_argument("--scan", type=int, default=None,
                     help="상세를 볼 scan id(기본: 진행중/최신 스캔)")
    pst.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

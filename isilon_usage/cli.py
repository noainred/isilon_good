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
from .scanner import run_scan
from .server import serve, build_status


DEFAULT_DB = "isilon_scan.db"


def _add_scan_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite DB 경로 (기본: %(default)s)")
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


def cmd_run(args: argparse.Namespace) -> int:
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"오류: 디렉터리가 아닙니다: {path}", file=sys.stderr)
        return 2

    dbmod.init_db(args.db)
    stop = threading.Event()

    def scan_worker():
        try:
            run_scan(
                args.db, path,
                backend=args.backend, size_mode=args.size_mode,
                one_file_system=args.one_file_system,
                batch_size=args.batch_size, sample_interval=args.sample_interval,
                stop_event=stop, with_monitor=True,
            )
        except Exception as exc:  # 스캔 실패가 서버까지 죽이지 않게
            print(f"[scan] 오류: {exc}", file=sys.stderr)

    t = threading.Thread(target=scan_worker, name="scanner", daemon=True)
    t.start()

    httpd = serve(args.db, host=args.host, port=args.port)
    url = _dashboard_url(args.host, args.port)
    print("=" * 64)
    print(f"  Isilon 사용량 스캔 시작: {path}")
    print(f"  backend={args.backend}  size-mode={args.size_mode}  "
          f"psutil={'있음' if monmod.have_psutil() else '없음(/proc 폴백)'}")
    print(f"  대시보드:  {url}")
    print(f"  DB:        {os.path.abspath(args.db)}")
    print("  중지하려면 Ctrl+C")
    print("=" * 64)

    def handle_sigint(signum, frame):
        print("\n중지 신호 수신 — 정리 중…", file=sys.stderr)
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    try:
        httpd.serve_forever()
    finally:
        stop.set()
        t.join(timeout=10)
        httpd.server_close()
    print("종료되었습니다.")
    return 0


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

    print(f"스캔 시작: {path} (backend={args.backend})")
    t0 = time.time()
    run_id = run_scan(
        args.db, path,
        backend=args.backend, size_mode=args.size_mode,
        one_file_system=args.one_file_system,
        batch_size=args.batch_size, sample_interval=args.sample_interval,
        stop_event=stop, with_monitor=True,
    )
    print(f"완료(run_id={run_id}). 소요 {time.time()-t0:.1f}s. "
          f"대시보드로 보려면: python -m isilon_usage serve --db {args.db}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    dbmod.init_db(args.db)
    httpd = serve(args.db, host=args.host, port=args.port)
    url = _dashboard_url(args.host, args.port)
    print(f"대시보드 서버 실행: {url}  (DB: {os.path.abspath(args.db)})")
    print("중지하려면 Ctrl+C")

    def handle_sigint(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    print("\n종료되었습니다.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not os.path.exists(args.db):
        print(f"DB 가 없습니다: {args.db}", file=sys.stderr)
        return 2
    conn = dbmod.connect(args.db)
    try:
        st = build_status(conn, None, samples=1, top=10)
    finally:
        conn.close()
    if not st.get("ok"):
        print(f"상태 없음: {st.get('reason') or st.get('error')}")
        return 1
    r, p = st["run"], st["progress"]
    print(f"경로        : {r['root_path']}")
    print(f"상태/단계   : {r['status']} / {r['phase']}  (backend={r['backend']})")
    print(f"디렉터리    : 탐색 {r['discovered_dirs']:,}  집계 {r['processed_dirs']:,}"
          f" / 총 {r['total_dirs']:,}")
    print(f"파일 수     : {r['total_files']:,}   오류 디렉터리: {r['error_dirs']:,}")
    print(f"확인 사용량 : {_human(r['scanned_bytes'])} "
          f"(사용량의 {p['disk_verified_pct_of_used']:.1f}%, "
          f"전체의 {p['disk_verified_pct_of_total']:.1f}%)")
    print(f"디스크      : 사용 {_human(r['fs_used_bytes'])} / 전체 {_human(r['fs_total_bytes'])}")
    print(f"경과        : {r['elapsed']:.1f}s   현재: {r['current_dir'] or '-'}")
    L = st["resources"].get("latest")
    if L:
        print(f"메모리      : {L['mem_percent']:.1f}%  "
              f"스캐너RSS {_human(L['scanner_rss'])}  du RSS {_human(L['du_rss'])}")
    if st["top_dirs"]:
        print("상위 디렉터리:")
        for i, t in enumerate(st["top_dirs"][:10], 1):
            sz = t["total_bytes"] or t["own_bytes"]
            print(f"  {i:2}. {_human(sz):>10}  {t['path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isilon_usage",
        description="아이실론 등 초대용량 NAS 의 디렉터리별 사용량을 메모리 최소로 "
                    "조사하고 웹 대시보드로 진행 상황을 보여주는 도구.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="스캔 + 대시보드 동시 실행(권장)")
    pr.add_argument("path", help="조사할 루트 디렉터리")
    _add_scan_opts(pr)
    pr.add_argument("--host", default="0.0.0.0")
    pr.add_argument("--port", type=int, default=8765)
    pr.set_defaults(func=cmd_run)

    ps = sub.add_parser("scan", help="스캔만 수행")
    ps.add_argument("path", help="조사할 루트 디렉터리")
    _add_scan_opts(ps)
    ps.set_defaults(func=cmd_scan)

    pv = sub.add_parser("serve", help="대시보드 웹서버만 실행")
    pv.add_argument("--db", default=DEFAULT_DB)
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=8765)
    pv.set_defaults(func=cmd_serve)

    pst = sub.add_parser("status", help="현재 진행 상태를 콘솔에 출력")
    pst.add_argument("--db", default=DEFAULT_DB)
    pst.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

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

from typing import List, Optional

import argparse
import os
import signal
import sys
import threading
import time

from . import __version__, SCHEMA_VERSION
from . import db as dbmod
from . import monitor as monmod
from . import manager as mgrmod
from . import settings as setmod
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
    p.add_argument("--workers", type=int, default=4,
                   help="동시 스캔 스레드 수(디렉터리 병렬, NFS 가속, 기본: %(default)s)")
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


def _run_server(args, *, initial_path: Optional[str]) -> int:
    """serve/run 공통: 웹 스캔 가능한 대시보드 서버를 띄운다.

    initial_path 가 주어지면(=run) 그 경로 스캔을 즉시 시작한다. 어느 경우든
    웹페이지에서 디렉터리를 지정해 추가 스캔을 시작/중지할 수 있다.
    """
    data_dir = os.path.abspath(args.data_dir)
    mount_bases = [os.path.abspath(b) for b in (getattr(args, "mount_base", None) or [])]

    # CLI 플래그 → 초기 설정(settings.json 이 없을 때만 적용; 이후엔 웹 편집값 우선)
    initial_settings = {
        "default_backend": args.backend,
        "default_size_mode": args.size_mode,
        "default_one_file_system": args.one_file_system,
        "batch_size": args.batch_size,
        "scan_workers": getattr(args, "workers", 1),
        "sample_interval": args.sample_interval,
        "mount_bases": mount_bases,
    }
    if getattr(args, "reset_settings", False):
        mgrmod.init_manager(data_dir)
        setmod.save(data_dir, initial_settings)   # 기존 settings.json 을 CLI 값으로 덮어씀

    httpd = serve(
        data_dir, host=args.host, port=args.port,
        initial_settings=initial_settings, enable_scan=True,
        lock_settings=getattr(args, "lock_settings", False),
    )
    cur = httpd.controller.settings
    url = _dashboard_url(args.host, args.port)

    print("=" * 64)
    print(f"  Isilon 사용량 대시보드 (웹에서 디렉터리 지정 스캔 + 설정 편집)")
    print(f"  대시보드:  {url}")
    print(f"  데이터:    {data_dir}  (manager.db + scans/ + settings.json)")
    if cur.get("mount_bases"):
        print(f"  허용 경로: {', '.join(cur['mount_bases'])}")
    else:
        print(f"  허용 경로: (제한 없음 — 설정에서 mount_bases 지정 권장)")
    print(f"  설정 편집: {'잠김(--lock-settings)' if getattr(args,'lock_settings',False) else '웹에서 가능'}")
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
        batch_size=args.batch_size, workers=args.workers,
        sample_interval=args.sample_interval,
        stop_event=stop, with_monitor=True,
        manager_db=manager_db, manager_scan_id=scan_id,
    )
    print(f"완료(scan #{scan_id}). 소요 {time.time()-t0:.1f}s. "
          f"대시보드로 보려면: python -m isilon_usage serve --data-dir {data_dir}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    data_dir = os.path.abspath(args.data_dir)
    if not os.path.exists(mgrmod.manager_db_path(data_dir)):
        print(f"관리 DB 가 없습니다: {data_dir}", file=sys.stderr)
        return 2
    if not args.keep_per_root and not args.older_than_days:
        print("--keep-per-root 또는 --older-than-days 중 하나는 지정하세요.", file=sys.stderr)
        return 2
    res = mgrmod.prune_scans(data_dir, keep_per_root=args.keep_per_root,
                             older_than_days=args.older_than_days)
    print(f"삭제된 스캔 {res['count']}개: {res['deleted']}")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    data_dir = os.path.abspath(args.data_dir)
    mconn = dbmod.connect(mgrmod.manager_db_path(data_dir))
    try:
        row = mgrmod.get_scan(mconn, args.scan)
    finally:
        mconn.close()
    if row is None:
        print(f"scan #{args.scan} 없음", file=sys.stderr)
        return 2
    if not os.path.exists(row["db_path"]):
        print("per-run DB 가 없어 재개할 수 없습니다.", file=sys.stderr)
        return 2
    s = setmod.load(data_dir)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    print(f"재개: scan #{args.scan}  {row['root_path']}")
    t0 = time.time()
    run_scan(
        row["db_path"], row["root_path"],
        backend=row["backend"], size_mode=row["size_mode"],
        batch_size=s["batch_size"], workers=s["scan_workers"],
        sample_interval=s["sample_interval"], resume=True,
        stop_event=stop, with_monitor=True,
        manager_db=mgrmod.manager_db_path(data_dir), manager_scan_id=args.scan,
    )
    print(f"완료(scan #{args.scan}). 소요 {time.time()-t0:.1f}s.")
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


def cmd_version(args: argparse.Namespace) -> int:
    import platform
    print(f"isilon_usage {__version__}")
    print(f"  스키마 버전 : {SCHEMA_VERSION}")
    print(f"  Python      : {platform.python_version()} ({sys.executable})")
    print(f"  플랫폼      : {platform.system()} {platform.release()}")
    print(f"  자원 수집   : {'psutil' if monmod.have_psutil() else '/proc 폴백'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isilon_usage",
        description="아이실론 등 초대용량 NAS 의 디렉터리별 사용량을 메모리 최소로 "
                    "조사하고 웹 대시보드로 진행 상황을 보여주는 도구.",
    )
    p.add_argument("--version", action="version",
                   version=f"isilon_usage {__version__} (schema {SCHEMA_VERSION})")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True   # add_subparsers(required=) 는 3.7+ 이라 속성으로 설정(3.6 호환)

    pr = sub.add_parser("run", help="초기 스캔 + 대시보드 실행(웹에서 추가 스캔도 가능)")
    pr.add_argument("path", help="조사할 루트 디렉터리")
    _add_scan_opts(pr)
    pr.add_argument("--mount-base", action="append", default=[],
                    help="웹에서 스캔을 허용할 경로(여러 번 지정 가능). 예: 마운트 지점")
    pr.add_argument("--lock-settings", action="store_true",
                    help="웹에서 설정 편집을 막음(읽기 전용)")
    pr.add_argument("--reset-settings", action="store_true",
                    help="기존 settings.json 을 현재 CLI 옵션 값으로 덮어씀")
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
    pv.add_argument("--workers", type=int, default=4,
                    help="파일 stat 동시 처리 스레드 수(웹 스캔 기본값, NFS 가속)")
    pv.add_argument("--sample-interval", type=float, default=2.0)
    pv.add_argument("--lock-settings", action="store_true",
                    help="웹에서 설정 편집을 막음(읽기 전용)")
    pv.add_argument("--reset-settings", action="store_true",
                    help="기존 settings.json 을 현재 CLI 옵션 값으로 덮어씀")
    pv.add_argument("--host", default="0.0.0.0")
    pv.add_argument("--port", type=int, default=8765)
    pv.set_defaults(func=cmd_serve)

    pst = sub.add_parser("status", help="전체 관리 개요 + 스캔 상태를 콘솔에 출력")
    pst.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                     help="manager.db + scans/ 상위 폴더 (기본: %(default)s)")
    pst.add_argument("--scan", type=int, default=None,
                     help="상세를 볼 scan id(기본: 진행중/최신 스캔)")
    pst.set_defaults(func=cmd_status)

    ppr = sub.add_parser("prune", help="오래된 스캔 정리(per-run DB 삭제)")
    ppr.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ppr.add_argument("--keep-per-root", type=int, default=None,
                     help="루트별로 최신 N개만 보관")
    ppr.add_argument("--older-than-days", type=float, default=None,
                     help="N일보다 오래된 완료/오류 스캔 삭제")
    ppr.set_defaults(func=cmd_prune)

    prs = sub.add_parser("resume", help="중단된 스캔 이어하기")
    prs.add_argument("scan", type=int, help="재개할 scan id")
    prs.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    prs.set_defaults(func=cmd_resume)

    pvr = sub.add_parser("version", help="버전/환경 정보 출력")
    pvr.set_defaults(func=cmd_version)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

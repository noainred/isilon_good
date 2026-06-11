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
    p.add_argument("--workers", type=int, default=8,
                   help="동시 스캔 스레드 수(디렉터리 병렬, NFS 가속, 기본: %(default)s)")
    p.add_argument("--max-depth", type=int, default=0,
                   help="탐색 최대 깊이(0=무제한, 빠른 컷). 그 아래 용량은 합계에서 빠짐")
    p.add_argument("--fold-depth", type=int, default=0,
                   help="깊이 접기(0=끔). 깊이 N까지만 행 저장, 그 아래는 용량만 N에 "
                        "합산 → DB 크기 묶임 + 합계 정확(N 아래 디렉터리별 상세는 없음)")
    p.add_argument("--db-max-gb", type=int, default=0,
                   help="per-run DB(.db+-wal)가 이 GB 초과하면 자동 일시정지(0=끔)")
    p.add_argument("--min-free-gb", type=int, default=0,
                   help="데이터 디스크 여유가 이 GB 미만이면 자동 일시정지(0=끔). "
                        "DB/WAL 이 디스크를 채워 서버가 죽기 전에 안전하게 멈춘다")
    p.add_argument("--no-hardlink-dedup", action="store_true",
                   help="하드링크 중복 제거를 끔(메모리 절약·수십억 파일 대비, 용량은 중복 셈)")
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
        "scan_max_depth": getattr(args, "max_depth", 0),
        "fold_depth": getattr(args, "fold_depth", 0),
        "db_max_gb": getattr(args, "db_max_gb", 0),
        "min_free_gb": getattr(args, "min_free_gb", 0),
        "hardlink_dedup": not getattr(args, "no_hardlink_dedup", False),
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
        max_depth=args.max_depth,
        fold_depth=args.fold_depth,
        db_max_bytes=int(args.db_max_gb or 0) * (1024 ** 3),
        hardlink_dedup=not args.no_hardlink_dedup,
        min_free_bytes=int(args.min_free_gb or 0) * (1024 ** 3),
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
        max_depth=int(s.get("scan_max_depth", 0) or 0),
        fold_depth=int(s.get("fold_depth", 0) or 0),
        db_max_bytes=int(s.get("db_max_gb", 0) or 0) * (1024 ** 3),
        hardlink_dedup=bool(s.get("hardlink_dedup", True)),
        min_free_bytes=int(s.get("min_free_gb", 0) or 0) * (1024 ** 3),
        sample_interval=s["sample_interval"], resume=True,
        stop_event=stop, with_monitor=True,
        manager_db=mgrmod.manager_db_path(data_dir), manager_scan_id=args.scan,
    )
    print(f"완료(scan #{args.scan}). 소요 {time.time()-t0:.1f}s.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # serve 는 초기 스캔 없이 대시보드만 띄우되, 웹에서 디렉터리를 지정해 스캔할 수 있다.
    return _run_server(args, initial_path=None)


def cmd_portal(args: argparse.Namespace) -> int:
    """글로벌 통합 포탈(HQ) 실행 — 여러 데이터센터 엣지를 모아 조망."""
    from .portal import serve_portal

    data_dir = os.path.abspath(args.data_dir)
    httpd = serve_portal(data_dir, host=args.host, port=args.port)
    url = _dashboard_url(args.host, args.port)
    print("=" * 64)
    print("  글로벌 통합 포탈 (HQ) — 여러 데이터센터를 한 화면에서 조망")
    print(f"  포탈:    {url}")
    print(f"  데이터:  {data_dir}  (portal_nodes.json + replicas/)")
    print(f"  노드 추가/관리: 포탈의 '노드 설정' 화면에서")
    print("  중지하려면 Ctrl+C")
    print("=" * 64)

    def handle_sigint(signum, frame):
        print("\n중지 신호 수신 — 정리 중…", file=sys.stderr)
        if httpd.controller:
            httpd.controller.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)
    try:
        httpd.serve_forever()
    finally:
        if httpd.controller:
            httpd.controller.stop()
        httpd.server_close()
    print("종료되었습니다.")
    return 0


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


def cmd_analyze(args: argparse.Namespace) -> int:
    """대상 분석 — 풀스캔 전에 디렉터리 깊이 구조만 빠르게 측정(파일 stat·DB 없이)."""
    from . import analyzer as anamod
    path = os.path.abspath(args.path)
    print("대상 분석 — 디렉터리 깊이 구조 측정(파일 stat 없이)")
    print(f"  대상: {path}")
    budget = []
    if args.max_depth:
        budget.append(f"최대깊이 {args.max_depth}")
    if args.limit:
        budget.append(f"디렉터리 {args.limit:,}개")
    if args.timeout:
        budget.append(f"{args.timeout:g}초")
    print(f"  예산: {', '.join(budget) if budget else '무제한(끝까지)'}  ·  워커 {args.workers}")
    print("-" * 64)
    res = anamod.analyze_depth(path, max_depth=args.max_depth, dir_limit=args.limit,
                               time_budget=args.timeout, workers=args.workers)
    if not res.get("ok"):
        print(f"  실패: {res.get('error')}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("  깊이 | 디렉터리 수 | 누적(fold 시 보존) | 예시 경로")
    print("  " + "-" * 62)
    for L in res["levels"]:
        print("  %4d | %11s | %18s | %s" % (
            L["depth"], f"{L['dirs']:,}", f"{L['cumulative']:,}", L["sample"]))
    print("  " + "-" * 62)
    print(f"  최대 깊이      : {res['max_depth']}")
    print(f"  총 디렉터리    : {res['total_dirs']:,}   총 파일(개수): {res['total_files']:,}"
          f"   오류: {res['error_dirs']:,}")
    print(f"  소요 시간      : {res['elapsed']:.2f}s")
    if res["truncated"]:
        print("  ⚠ 예산 초과로 중단됨 — 결과는 '하한 추정치'입니다(실제는 더 큼).")
    if res["capped"]:
        print(f"  ⚠ 최대깊이({args.max_depth})에서 멈춤 — 더 깊은 단계가 있을 수 있습니다.")
    fold = res["suggested_fold_depth"]
    if fold:
        kept = next((L["cumulative"] for L in res["levels"] if L["depth"] == fold), None)
        print("=" * 64)
        print(f"  ▶ 추천 fold-depth : {fold}"
              + (f"  (보존 행 ≈ {kept:,}개)" if kept is not None else ""))
        print(f"  적용 예) python -m isilon_usage scan {path} \\")
        print(f"            --fold-depth {fold} --min-free-gb 5 --db-max-gb 30")
    print("=" * 64)
    return 0


def cmd_tunecheck(args: argparse.Namespace) -> int:
    """튜닝 점검 — OS 커널/마운트 설정을 보고 스캔 가속 튜닝 포인트를 체크."""
    from . import systune as sysmod
    rep = sysmod.check(scan_root=getattr(args, "path", None),
                       data_dir=os.path.abspath(args.data_dir))
    if getattr(args, "json", False):
        import json
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print("튜닝 점검 — OS 커널/마운트 설정 분석")
    if rep.get("nfs_mounts"):
        print("  NFS 마운트: " + ", ".join(m["mount"] for m in rep["nfs_mounts"]))
    c = rep["summary"]
    print(f"  요약: ok {c['ok']} · 주의 {c['warn']} · 권장 {c['tip']} · 측정불가 {c['na']}")
    glyph = {"ok": "🟢", "warn": "🟠", "tip": "🟡", "na": "⚪"}
    for s in rep["sections"]:
        print("\n[" + s["name"] + "]")
        for it in s["items"]:
            print("  %s %-30s 현재: %s" % (glyph.get(it["level"], "·"), it["key"], it["current"]))
            print("     권장: %s — %s" % (it["recommended"], it["note"]))
            if it.get("fix") and it["level"] in ("warn", "tip"):
                print("     적용: %s" % it["fix"])
    return 0


def cmd_pscan(args: argparse.Namespace) -> int:
    """pscan(PoC) — 멀티프로세스 병렬 스캔(GIL/단일 락 직렬화 회피)."""
    from . import pscan as psmod
    root = os.path.abspath(args.path)
    node_mounts = args.node_mount or None

    if getattr(args, "compare", False):
        print("멀티프로세스 확장성 비교(처리량) — %s" % root)
        print("-" * 56)
        print("  프로세스 | 소요(s) | 디렉터리/초 | 파일/초 | 1→N 속도향상")
        base = None
        for p in (1, 2, 4, 8):
            r = psmod.parallel_scan(root, processes=p, size_mode=args.size_mode)
            if not r.get("ok"):
                print("  실패: %s" % r.get("error"), file=sys.stderr)
                return 2
            base = base or r["elapsed"]
            print("  %8d | %7.2f | %11.0f | %8.0f | %.2f배" % (
                p, r["elapsed"], r["dirs_per_sec"], r["files_per_sec"],
                base / r["elapsed"] if r["elapsed"] else 0))
        print("-" * 56)
        print("  (NAS 에서는 프로세스 수만큼 메타데이터 처리량이 늘어야 정상)")
        return 0

    print("pscan(PoC) — 멀티프로세스 병렬 스캔")
    print("  대상: %s  ·  프로세스: %d  ·  size-mode: %s%s" % (
        root, args.processes, args.size_mode,
        ("  ·  노드마운트 %d개" % len(node_mounts)) if node_mounts else ""))

    def _prog(p):
        sys.stdout.write("\r  진행: %d/%d 단위  (%.1fs)" % (p["done"], p["units"], p["elapsed"]))
        sys.stdout.flush()
    res = psmod.parallel_scan(root, processes=args.processes, size_mode=args.size_mode,
                              node_mounts=node_mounts, on_progress=_prog)
    print()
    if not res.get("ok"):
        print("  실패: %s" % res.get("error"), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print("-" * 56)
    print("  총 용량      : %s" % _human(res["total_bytes"]))
    print("  총 파일/디렉터리: %d / %d   오류: %d" % (
        res["total_files"], res["total_dirs"], res["error_count"]))
    print("  소요/처리량  : %.2fs  ·  %.0f 디렉터리/초  ·  %.0f 파일/초" % (
        res["elapsed"], res["dirs_per_sec"], res["files_per_sec"]))
    if res["per_top"]:
        print("  상위 디렉터리(용량순):")
        for t in res["per_top"][:10]:
            print("    %10s  %s  (파일 %d)" % (_human(t["bytes"]), t["path"], t["files"]))
    print("-" * 56)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    import platform
    print(f"isilon_usage {__version__}")
    print(f"  스키마 버전 : {SCHEMA_VERSION}")
    print(f"  Python      : {platform.python_version()} ({sys.executable})")
    print(f"  플랫폼      : {platform.system()} {platform.release()}")
    print(f"  자원 수집   : {'psutil' if monmod.have_psutil() else '/proc 폴백'}")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    specs = monmod.system_specs()
    rec = monmod.recommend_workers(specs, backend=args.backend)
    print("서버 사양 기반 권장 동시 스캔 스레드 수")
    print("=" * 56)
    print(f"  논리 CPU    : {specs['cpu_count']} 개")
    print(f"  메모리      : 총 {_human(specs['mem_total_bytes'])} / "
          f"가용 {_human(specs['mem_avail_bytes'])}")
    print(f"  자원 수집   : {'psutil' if specs.get('have_psutil') else '/proc 폴백'}")
    print(f"  백엔드      : {rec['backend']}")
    print("-" * 56)
    print(f"  ▶ 권장 스레드 : {rec['recommended']}  (범위 {rec['min']}–{rec['max']})")
    print(f"  근거        : {rec['rationale']}")
    for n in rec["notes"]:
        print(f"   · {n}")
    print("=" * 56)
    print(f"  적용 예) python -m isilon_usage serve --workers {rec['recommended']}")
    print(f"          또는 설정 화면의 '동시 스캔 스레드'를 {rec['recommended']} 로 저장")

    # --benchmark: 실제 경로에서 후보 스레드 수로 짧게 시범 탐색해 처리량 비교.
    # 스레드를 메모리 2배 한도까지 단계적으로 올리며, 각 단계 결과를 즉시 출력한다.
    if getattr(args, "benchmark", None):
        from . import tuning as tunmod
        cands = None
        if args.candidates:
            cands = [c for c in args.candidates.split(",") if c.strip()]
        print("\n실측 보정(시범 탐색) — 단계별로 출력합니다(메모리 %.0f배 한도까지)…"
              % args.mem_factor)
        print(f"  대상   : {os.path.abspath(args.benchmark)}")
        print("-" * 64)
        print("  스레드 | 탐색 디렉터리 | 소요(s) | 초당 디렉터리 |  peak RSS  | mem×")
        print("-" * 64)

        def _on_result(r):
            print("  %6d | %12d | %7.2f | %12.1f | %7.1f MB | ×%.2f%s" % (
                r["workers"], r["discovered"], r["elapsed"], r["dirs_per_sec"],
                r["peak_rss_bytes"] / 1048576.0, r["mem_ratio"],
                "  ← 메모리 한도" if r.get("mem_stop") else ""))

        bench = tunmod.benchmark_workers(
            args.benchmark, candidates=cands, budget_sec=args.budget,
            mem_factor=args.mem_factor, on_result=_on_result)
        if not bench.get("ok"):
            print(f"  실패: {bench.get('error')}", file=sys.stderr)
            return 2
        print("-" * 64)
        print(f"  ▶ 실측 권장 스레드 : {bench['recommended']}")
        print(f"  {bench['note']}")
    return 0


def _parse_size(s) -> int:
    """'0', '4K', '1M', '10M', '2G' 등을 바이트로."""
    s = str(s).strip().upper().rstrip("B")
    mult = 1
    for suf, m in (("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3), ("T", 1024 ** 4)):
        if s.endswith(suf):
            mult = m
            s = s[:-1]
            break
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def cmd_gentest(args: argparse.Namespace) -> int:
    from . import gentest as genmod
    size = _parse_size(args.size)
    ok, why, plan = genmod.validate(args.path, args.dirs, args.subdirs, args.files, size)
    print("테스트 데이터 생성")
    print(f"  대상: {plan['base']}")
    print(f"  계획: 디렉터리 {plan['total_dirs']:,}개 · 파일 {plan['total_files']:,}개 "
          f"· 용량 {_human(plan['total_bytes'])} (파일당 {_human(plan['file_size'])})")
    if not ok:
        print(f"  거부: {why}", file=sys.stderr)
        return 2
    if not args.yes:
        try:
            ans = input("  생성할까요? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("  취소했습니다.")
            return 1
    t0 = time.time()
    res = genmod.generate(plan["base"], plan["n_dirs"], plan["n_subdirs"],
                          plan["n_files"], plan["file_size"])
    if not res.get("ok"):
        print(f"  실패: {res.get('error')}", file=sys.stderr)
        return 2
    print(f"  완료: 디렉터리 {res['created_dirs']:,} · 파일 {res['created_files']:,} "
          f"· {_human(res['written_bytes'])} ({time.time() - t0:.1f}s)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    data_dir = os.path.abspath(args.data_dir)
    mconn = dbmod.connect(mgrmod.manager_db_path(data_dir))
    try:
        if args.scan:
            row = mgrmod.get_scan(mconn, args.scan)
        else:
            scans = mgrmod.list_scans(mconn)
            row = mgrmod.get_scan(mconn, scans[0]["id"]) if scans else None
    finally:
        mconn.close()
    if not row or not os.path.exists(row["db_path"]):
        print("스캔/per-run DB 가 없습니다.", file=sys.stderr)
        return 2
    conn = dbmod.connect(row["db_path"])
    try:
        rid = dbmod.latest_run_id(conn)
        print(f"분석 리포트 — scan #{row['id']}  {row['root_path']}")

        def _show(title, kind, limit, label=lambda k: k):
            print("\n  " + title)
            rows = dbmod.get_scan_stats(conn, rid, kind, limit=limit)
            if not rows:
                print("    (데이터 없음)")
            for r in rows:
                print("    %-30s %12s  %s개"
                      % (str(label(r["key"]))[:30], _human(r["bytes"]), format(r["files"], ",")))

        def _owner(uid):
            try:
                import pwd
                return "%s (uid %s)" % (pwd.getpwuid(int(uid)).pw_name, uid)
            except Exception:  # noqa: BLE001
                return "uid %s" % uid

        _show("🕒 파일 나이(콜드 데이터)", "age", 0)
        _show("👤 소유자별 사용량 Top", "owner", 20, _owner)
        _show("🗂 확장자별 사용량 Top", "ext", 20)

        print("\n  🐘 최대 파일 Top")
        tops = dbmod.get_top_files(conn, rid, limit=args.top)
        if not tops:
            print("    (데이터 없음)")
        for f in tops:
            when = time.strftime("%Y-%m-%d", time.localtime(f["mtime"])) if f["mtime"] else "—"
            print("    %12s  %s  (수정 %s, uid %s)"
                  % (_human(f["bytes"]), f["path"], when, f["uid"]))
    finally:
        conn.close()
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
    pv.add_argument("--workers", type=int, default=8,
                    help="동시 스캔 스레드 수(디렉터리 병렬, 웹 스캔 기본값, NFS 가속)")
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

    pan = sub.add_parser("analyze", help="대상 분석 — 디렉터리 깊이 구조만 빠르게 측정"
                                         "(풀스캔 전 fold-depth 결정용)")
    pan.add_argument("path", help="분석할 대상 경로")
    pan.add_argument("--max-depth", type=int, default=0,
                     help="이 깊이까지만 측정(0=무제한). 거대 트리 빠른 컷")
    pan.add_argument("--limit", type=int, default=0,
                     help="디렉터리 이만큼 방문하면 중단(0=무제한)")
    pan.add_argument("--timeout", type=float, default=0.0,
                     help="이 초만큼 지나면 중단(0=무제한). 빠른 추정에 유용")
    pan.add_argument("--workers", type=int, default=8, help="동시 디렉터리 워커 수")
    pan.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    pan.set_defaults(func=cmd_analyze)

    ptc = sub.add_parser("tunecheck", help="튜닝 점검 — OS 커널/마운트 설정 분석"
                                           "(NFS nconnect·RPC 슬롯·캐시 등)")
    ptc.add_argument("path", nargs="?", default=None, help="대상 경로(NFS 마운트 식별용, 선택)")
    ptc.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ptc.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    ptc.set_defaults(func=cmd_tunecheck)

    pps = sub.add_parser("pscan", help="[PoC] 멀티프로세스 병렬 스캔"
                                       "(GIL/단일 락 회피 — 프로세스 수만큼 확장)")
    pps.add_argument("path", help="스캔할 루트 경로")
    pps.add_argument("--processes", "-P", type=int, default=4, help="동시 프로세스 수")
    pps.add_argument("--size-mode", choices=["disk", "apparent"], default="disk")
    pps.add_argument("--node-mount", action="append", default=None,
                     help="멀티노드: 같은 트리의 다른 노드 마운트(여러 번 지정 → 라운드로빈 분산)")
    pps.add_argument("--compare", action="store_true",
                     help="프로세스 1·2·4·8 로 확장성(처리량) 비교")
    pps.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    pps.set_defaults(func=cmd_pscan)

    pvr = sub.add_parser("version", help="버전/환경 정보 출력")
    pvr.set_defaults(func=cmd_version)

    ptu = sub.add_parser("tune", help="서버 사양을 보고 권장 동시 스캔 스레드 수 계산")
    ptu.add_argument("--backend", choices=["native", "du"], default="native",
                     help="대상 백엔드(기본: native)")
    ptu.add_argument("--benchmark", metavar="PATH", default=None,
                     help="이 경로에서 후보 스레드 수로 짧게 시범 탐색해 실측 처리량 비교")
    ptu.add_argument("--candidates", default=None,
                     help="시범할 스레드 후보(쉼표, 기본 8,16,32)")
    ptu.add_argument("--budget", type=float, default=5.0,
                     help="후보당 측정 시간(초, 기본 5)")
    ptu.add_argument("--mem-factor", type=float, default=2.0,
                     help="스레드를 올릴 때 시작 대비 RSS 이 배수에 도달하면 중단(기본 2배)")
    ptu.set_defaults(func=cmd_tune)

    pg = sub.add_parser("gentest", help="테스트용 샘플 디렉터리/파일 생성")
    pg.add_argument("path", help="생성할 대상 디렉터리")
    pg.add_argument("--dirs", type=int, default=10, help="대상에 만들 디렉터리 수(기본 10)")
    pg.add_argument("--subdirs", type=int, default=5,
                    help="각 디렉터리의 하위 디렉터리 수(기본 5)")
    pg.add_argument("--files", type=int, default=10, help="각 폴더의 파일 수(기본 10)")
    pg.add_argument("--size", default="1K", help="파일 크기(예: 0, 4K, 1M, 10M; 기본 1K)")
    pg.add_argument("-y", "--yes", action="store_true", help="확인 없이 바로 생성")
    pg.set_defaults(func=cmd_gentest)

    pst2 = sub.add_parser("stats", help="분석 리포트(파일 나이/소유자/확장자/최대 파일) 콘솔 출력")
    pst2.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    pst2.add_argument("--scan", type=int, default=None, help="스캔 id(생략 시 최신)")
    pst2.add_argument("--top", type=int, default=10, help="최대 파일 표시 개수(기본 10)")
    pst2.set_defaults(func=cmd_stats)

    ppo = sub.add_parser("portal", help="글로벌 통합 포탈(HQ) — 여러 DC 를 한 화면에서 조망")
    ppo.add_argument("--data-dir", default="isilon_portal_data",
                     help="portal_nodes.json + replicas/ 상위 폴더 (기본: %(default)s)")
    ppo.add_argument("--host", default="0.0.0.0")
    ppo.add_argument("--port", type=int, default=8800)
    ppo.set_defaults(func=cmd_portal)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)

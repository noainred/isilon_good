"""웹에서 보고 수정하는 런타임 설정.

설정은 `<data-dir>/settings.json` 에 저장되어 서버를 재시작해도 유지된다.
대시보드의 "설정" 카드(GET/POST /api/settings)에서 편집한다.

여기 담는 것은 "런타임에 바꿔도 되는" 설정들이다. host/port/data-dir 처럼
서버 구동에 고정되는 값은 읽기 전용 정보로만 보여준다.
"""


import datetime as _dt
import json
import os
import shutil
import tempfile
import threading
import time as _time

_SAVE_LOCK = threading.Lock()


def clean_stale_tmp(dir_path: str, *, older_than: float = 60.0) -> int:
    """write_json_atomic 이 남긴 임시 파일(<이름>.<무작위>.tmp)을 지운다 — 저장 도중 강제 종료되면 남아 쌓였다
    (2026-11 강제 종료 점검: 30번 종료에 18개). 다른 프로세스가 지금 쓰는 중일 수 있어 1분 넘은 것만."""
    n = 0
    try:
        names = os.listdir(dir_path)
    except OSError:
        return 0
    now = _time.time()
    for nm in names:
        if not (nm.endswith(".tmp") and ".json." in nm):
            continue
        p = os.path.join(dir_path, nm)
        try:
            if now - os.path.getmtime(p) >= older_than and os.path.isfile(p):
                os.remove(p)
                n += 1
        except OSError:
            continue
    return n


def write_json_atomic(path: str, obj, *, mode: int = 0o600) -> None:
    """JSON 을 원자적으로 저장한다 — 매번 고유한 임시 파일(동시 저장끼리 같은 .tmp 를 덮어써 반쯤 쓴
    파일이 교체되던 경합 방지), fsync 후 교체, 직전 정상본은 .bak 으로 남긴다(2026-11 리뷰)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with _SAVE_LOCK:
        fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=d)
        try:
            try:
                text = json.dumps(obj, ensure_ascii=False, indent=2)
                text.encode("utf-8")
            except UnicodeEncodeError:   # 비 UTF-8 경로(서로게이트)가 든 값 — \u 이스케이프로 저장(읽으면 같은 값)
                text = json.dumps(obj, ensure_ascii=True, indent=2)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        json.load(fh)             # 정상본일 때만 백업(깨진 파일로 .bak 을 덮지 않게)
                    shutil.copyfile(path, path + ".bak")
                    os.chmod(path + ".bak", mode)
                except (OSError, ValueError):
                    pass
            os.replace(tmp, path)
            tmp = None
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def read_json_with_backup(path: str):
    """JSON 을 읽는다. 본 파일이 깨졌으면 .bak 을 쓴다(둘 다 없거나 깨지면 None).

    깨진 설정을 '빈 기본값'으로 읽은 채 저장하면 비밀번호·토큰이 조용히 사라진다 — 그걸 막는다."""
    for p in (path, path + ".bak"):
        try:
            with open(p, encoding="utf-8") as fh:
                v = json.load(fh)
            if isinstance(v, dict):
                return v
        except (OSError, ValueError):
            continue
    return None


# 키: (기본값) — 새 설정 항목을 추가하면 여기에 등록한다.
DEFAULTS: dict = {
    "default_backend": "native",        # 새 스캔 기본 백엔드(native/du)
    "default_size_mode": "disk",        # 기본 용량 기준(disk/apparent)
    "default_one_file_system": False,   # 기본 -x(한 파일시스템)
    "check_readonly": True,             # 스캔 시작 시 대상 마운트 읽기전용(ro) 검사·표시
    "batch_size": 500,                  # DB 커밋 배치 크기
    "sample_interval": 2.0,             # 자원 샘플링 주기(초)
    "mount_bases": [],                  # 웹에서 스캔 허용할 경로 목록(빈 목록=전체 허용)
    "top_n": 20,                        # 상위 디렉터리 표시 개수
    "refresh_ms": 1500,                 # 대시보드 자동 새로고침 주기(ms)
    "scan_workers": 4,                  # 동시 스캔 스레드 수(디렉터리 단위 병렬, NFS 가속, 더 올릴수록 빠름)
    "scan_max_depth": 0,                # 탐색 최대 깊이(0=무제한, 빠른 컷). 깊은 용량은 합계에서 빠짐
    "fold_depth": 4,                    # 깊이 접기 기본 4(미지정 시): N까지만 행 저장, 그 아래는 용량만 N에 합산(합계 정확). 0=off
    "db_max_gb": 0,                     # per-run DB(.db+-wal)가 이 GB 초과하면 자동 일시정지(0=off)
    "hardlink_dedup": True,             # 하드링크 중복 제거(끄면 메모리 절약, 수십억 파일 대비)
    "min_free_gb": 0,                   # 데이터 디스크 여유가 이 GB 미만이면 자동 일시정지(0=off)
    "retention_per_root": 0,            # 루트별 보관 스캔 수(0=무제한). 완료 시 자동 정리
    "notify_webhook": "",               # 스캔 완료/오류 시 POST 할 웹훅 URL(빈값=사용 안 함)
    "schedules": [],                    # 예약 스캔 목록(아래 _sanitize_schedules 참고)
    "log_path": "",                     # 로그 파일 경로(비우면 파일 로깅 안 함)
    "notify_email": "",                 # 완료/오류 알림 받을 메일(쉼표로 여러 명)
    "smtp_host": "",                    # 메일 발송 SMTP 서버(비우면 메일 안 보냄)
    "smtp_port": 587,                   # SMTP 포트(465=SSL, 587/25=STARTTLS)
    "smtp_user": "",                    # SMTP 로그인 사용자(비우면 인증 안 함)
    "smtp_password": "",                # SMTP 비밀번호
    "smtp_from": "",                    # 보내는 사람(비우면 smtp_user)
    "smtp_tls": True,                   # STARTTLS 사용(465 포트는 자동 SSL)
    "api_token": "",                    # 글로벌 포탈 복제용 토큰(설정 시 /api/dbexport 인증 필요)
    "op_password": "",                  # 작업(버튼·설정변경) 보호 비밀번호(빈값=잠금 없음)
    "op_password_encrypted": False,     # 참이면 op_password 를 PBKDF2 해시로 저장(평문 미저장)
    "op_password_temp": False,          # 참이면 서버가 자동 생성한 임시 비밀번호(info.MD) — 사용자가 바꾸면 거짓
    "op_info_path": "",                 # 비밀번호를 기록한 info.MD 경로(임시 비밀번호 안내 팝업·서버 명령 초기화용)
    "op_ttl_minutes": 30,               # 로그인 세션 유지 시간(분) — 사용자가 지정
    "show_update_popup": False,         # 업그레이드 후 첫 접속 시 변경내용 팝업(기본 끔)
    "default_engine": "threads",        # 새 스캔 기본 엔진: threads(상세) | pscan(빠른 용량)
    "last_scan": {},                    # 마지막 스캔 시작 파라미터+적용변수(다음 시작 폼 복원·참고)
    "upgrade_watch_dir": "",            # 자동 업그레이드 감시 폴더(빈값=끔). 새 버전 압축본 감지
    "upgrade_check_secs": 60,          # 감시 폴더/인터넷 점검 주기(초)
    "upgrade_source": "off",           # 인터넷 자동 업그레이드 소스: off / github(raw versions.json)
    "upgrade_url": "",                 # versions.json base URL(빈값=기본 raw GitHub download/)
    "upgrade_token": "",               # 사설(비공개) 소스 인증 토큰(PAT). 빈값=공개 소스(인증 없음)
    "upgrade_auto": False,             # 새 버전 발견 시 자동 설치(켜면 무인 설치·재시작; 끄면 알림만)
    "isilon_url": "",                   # OneFS Platform API 주소(예: https://10.0.0.10:8080)
    "isilon_user": "",                  # PAPI 읽기 계정
    "isilon_password": "",              # PAPI 비밀번호
    "isilon_verify_ssl": False,         # 자체 서명 인증서면 False(검증 생략)
    "powerstore_url": "",               # PowerStore REST API 주소(예: https://10.0.0.20)
    "powerstore_user": "",              # PowerStore 읽기 계정
    "powerstore_password": "",          # PowerStore 비밀번호
    "powerstore_verify_ssl": False,     # 자체 서명 인증서면 False(검증 생략)
    "storage_arrays": [],               # 추가 스토리지 어레이(Unity/PowerMax/VMAX/XtremIO/VPLEX 등)
    # 메일 알림 이벤트 토글(EDITABLE_KEYS 에 포함돼야 저장/복원된다 — 누락 시 UI 변경이 무시됨).
    "notify_on_done": True,             # 작업 완료 시 알림
    "notify_on_error": True,            # 장애(오류) 발생 시 알림
    "notify_on_stopped": False,         # 작업 중단 시 알림
    "notify_on_stalled": False,         # 중단 후 N분간 재시작 없을 때 알림
    "notify_stall_minutes": 10,         # 위 stall 판정 분
    "ask_llm_enabled": False,           # 자연어 질의응답에 로컬 LLM 사용(끄면 규칙 기반만)
    "ask_llm_endpoint": "",             # 로컬 LLM(OpenAI 호환) 주소(예: http://127.0.0.1:11434/v1)
    "ask_llm_model": "",                # 모델 이름(예: qwen2.5:7b)
    "ask_llm_key": "",                  # 인증 토큰(로컬은 보통 불필요)
    "ask_llm_timeout": 20,              # LLM 응답 대기(초)
}

# 웹 설정 저장으로 직접 바꾸지 않는 키(서버가 관리 — 임시 비밀번호 상태)
EDITABLE_KEYS = set(DEFAULTS.keys()) - {"op_password_temp", "op_info_path"}


def settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, "settings.json")


def sanitize(raw: dict) -> dict:
    """알 수 없는 키 제거 + 타입/범위 보정."""
    s = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in DEFAULTS:
            s[k] = v

    if s["default_backend"] not in ("native", "du"):
        s["default_backend"] = "native"
    if s["default_size_mode"] not in ("disk", "apparent"):
        s["default_size_mode"] = "disk"
    s["default_one_file_system"] = bool(s["default_one_file_system"])
    s["check_readonly"] = bool(s.get("check_readonly", True))

    def _int(v, lo, hi, dflt):
        if isinstance(v, bool):        # true 를 1 로 저장하지 않게(22회차 점검) — 기본값
            return dflt
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError, OverflowError):   # 1e999·Infinity → 기본값(예전엔 serve 가 기동하지 못했다)
            return dflt

    def _float(v, lo, hi, dflt):
        try:
            f = float(v)
            if f != f:                 # NaN
                return dflt
            return max(lo, min(hi, f))
        except (TypeError, ValueError, OverflowError):   # 401자리 정수 → 500 이던 문제(22회차 점검)
            return dflt

    s["batch_size"] = _int(s["batch_size"], 1, 1_000_000, 500)
    s["sample_interval"] = _float(s["sample_interval"], 0.2, 60.0, 2.0)
    s["top_n"] = _int(s["top_n"], 1, 500, 20)
    s["refresh_ms"] = _int(s["refresh_ms"], 500, 600_000, 1500)
    s["scan_workers"] = _int(s["scan_workers"], 1, 64, 4)
    s["scan_max_depth"] = _int(s.get("scan_max_depth"), 0, 100000, 0)
    s["fold_depth"] = _int(s.get("fold_depth"), 0, 100000, 4)   # 미지정 시 기본 4
    s["db_max_gb"] = _int(s.get("db_max_gb"), 0, 1000000, 0)
    s["hardlink_dedup"] = bool(s.get("hardlink_dedup", True))
    s["min_free_gb"] = _int(s.get("min_free_gb"), 0, 1000000, 0)
    s["retention_per_root"] = _int(s["retention_per_root"], 0, 100_000, 0)
    s["notify_webhook"] = str(s.get("notify_webhook") or "").strip()
    s["log_path"] = str(s.get("log_path") or "").strip()
    s["notify_email"] = str(s.get("notify_email") or "").strip()
    s["smtp_host"] = str(s.get("smtp_host") or "").strip()
    s["smtp_port"] = _int(s["smtp_port"], 1, 65535, 587)
    s["smtp_user"] = str(s.get("smtp_user") or "").strip()
    s["smtp_password"] = str(s.get("smtp_password") or "")
    s["smtp_from"] = str(s.get("smtp_from") or "").strip()
    s["smtp_tls"] = bool(s.get("smtp_tls", True))
    # 메일 알림 조건(이벤트별 on/off). 다중 수신자는 notify_email 에 쉼표/공백으로 여러 개.
    s["notify_on_done"] = bool(s.get("notify_on_done", True))        # 작업 완료시
    s["notify_on_error"] = bool(s.get("notify_on_error", True))      # 장애(오류) 발생시
    s["notify_on_stopped"] = bool(s.get("notify_on_stopped", False))  # 작업 중단시
    s["notify_on_stalled"] = bool(s.get("notify_on_stalled", False))  # 중단 후 N분간 재시작 없을 때
    s["notify_stall_minutes"] = _int(s.get("notify_stall_minutes", 10), 1, 1440, 10)
    s["api_token"] = str(s.get("api_token") or "").strip()
    s["ask_llm_enabled"] = bool(s.get("ask_llm_enabled", False))
    s["ask_llm_endpoint"] = str(s.get("ask_llm_endpoint") or "").strip()
    s["ask_llm_model"] = str(s.get("ask_llm_model") or "").strip()
    s["ask_llm_key"] = str(s.get("ask_llm_key") or "")
    s["ask_llm_timeout"] = _int(s.get("ask_llm_timeout", 20), 1, 600, 20)
    s["op_password"] = str(s.get("op_password") or "")
    s["op_password_encrypted"] = bool(s.get("op_password_encrypted", False))
    s["op_password_temp"] = bool(s.get("op_password_temp", False)) and bool(s["op_password"])
    s["op_info_path"] = str(s.get("op_info_path") or "")
    s["op_ttl_minutes"] = _int(s.get("op_ttl_minutes", 30), 1, 10080, 30)   # 1분~7일
    s["show_update_popup"] = bool(s.get("show_update_popup", False))
    s["default_engine"] = (s.get("default_engine")
                           if s.get("default_engine") in ("threads", "pscan") else "threads")
    _ls = s.get("last_scan")            # 마지막 스캔 파라미터 스냅샷(dict 만 허용; 내부는 앱이 채움)
    s["last_scan"] = _ls if isinstance(_ls, dict) else {}
    s["upgrade_watch_dir"] = str(s.get("upgrade_watch_dir") or "").strip()
    # 위 한도(7일)도 — 아주 큰 값은 업그레이드 감시 스레드의 대기(wait)를 OverflowError 로 죽였다(22회차 점검)
    s["upgrade_check_secs"] = _int(s.get("upgrade_check_secs", 60) or 60, 10, 7 * 86400, 60)
    _usrc = str(s.get("upgrade_source") or "off").strip().lower()
    s["upgrade_source"] = _usrc if _usrc in ("off", "github") else "off"
    s["upgrade_url"] = str(s.get("upgrade_url") or "").strip()
    s["upgrade_token"] = str(s.get("upgrade_token") or "").strip()
    s["upgrade_auto"] = bool(s.get("upgrade_auto"))
    s["isilon_url"] = str(s.get("isilon_url") or "").strip()
    s["isilon_user"] = str(s.get("isilon_user") or "").strip()
    s["isilon_password"] = str(s.get("isilon_password") or "")
    s["isilon_verify_ssl"] = bool(s.get("isilon_verify_ssl", False))
    s["powerstore_url"] = str(s.get("powerstore_url") or "").strip()
    s["powerstore_user"] = str(s.get("powerstore_user") or "").strip()
    s["powerstore_password"] = str(s.get("powerstore_password") or "")
    s["powerstore_verify_ssl"] = bool(s.get("powerstore_verify_ssl", False))

    mb = s.get("mount_bases") or []
    if isinstance(mb, str):
        mb = mb.replace(",", "\n").splitlines()
    if not isinstance(mb, (list, tuple)):     # 숫자 등 잘못된 값 → 빈 목록(예전엔 serve 가 기동하지 못했다)
        mb = []
    s["mount_bases"] = [os.path.abspath(x.strip()) for x in mb if isinstance(x, str) and x.strip()]

    s["schedules"] = _sanitize_schedules(s.get("schedules"))
    s["storage_arrays"] = _sanitize_storage_arrays(s.get("storage_arrays"))
    return s


_STORAGE_TYPES = ("isilon", "powerstore", "unity", "powermax", "vmax", "xtremio", "vplex")


def _sanitize_storage_arrays(raw) -> list:
    """추가 스토리지 어레이 목록 보정. 각 항목:
    {id, type, name, url, user, password, verify_ssl}
    """
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        t = str(it.get("type") or "").strip().lower()
        url = str(it.get("url") or "").strip()
        if t not in _STORAGE_TYPES or not url:
            continue
        out.append({
            "id": str(it.get("id") or it.get("name") or url).strip(),
            "type": t,
            "name": str(it.get("name") or "").strip(),
            "url": url,
            "user": str(it.get("user") or "").strip(),
            "password": str(it.get("password") or ""),
            "verify_ssl": bool(it.get("verify_ssl", False)),
        })
    return out


_SCHED_UNITS = ("minute", "hour", "day", "week", "month")


def parse_at(s) -> tuple:
    """'HH:MM' → (hour, minute). 잘못되면 (3, 0)."""
    try:
        hh, mm = str(s or "03:00").split(":")[:2]
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except (TypeError, ValueError):
        return 3, 0


def _sanitize_schedules(raw) -> list:
    """예약 스캔 목록 보정. 각 항목(시작 + 반복주기 모델):
    {path, unit(minute/hour/day/week/month), every, at('HH:MM'),
     weekdays([0=일..6=토]), start_month(1-12), start_day(1-31),
     backend, size_mode, one_file_system, enabled, anchor, last_run, every_minutes}
    """
    out = []
    if not isinstance(raw, list):
        return out
    now = _time.time()
    for it in raw:
        if not isinstance(it, dict):
            continue
        # 경로: 단일 path(구버전) 또는 순차 스캔용 paths(여러 개, 순서대로 A→B→C).
        path = str(it.get("path") or "").strip()
        raw_paths = it.get("paths")
        plist = ([str(p).strip() for p in raw_paths if str(p or "").strip()]
                 if isinstance(raw_paths, list) else [])
        if not plist and path:
            plist = [path]
        if not plist:
            continue
        plist = [os.path.abspath(p) for p in plist]
        path = plist[0]

        def _int(key, lo, hi, dflt, _it=it):
            try:
                return max(lo, min(hi, int(_it.get(key, dflt))))
            except (TypeError, ValueError, OverflowError):
                return dflt

        unit = it.get("unit")
        if unit not in _SCHED_UNITS:
            unit = "minute"   # 구버전(every_minutes만 있던) 호환
        # every: 없으면 minute 은 every_minutes, 그 외엔 1
        if it.get("every") is not None:
            every = _int("every", 1, 100000, 1)
        elif unit == "minute":
            every = _int("every_minutes", 1, 100000, 60)
        else:
            every = 1
        _wd = it.get("weekdays")
        weekdays = sorted({d for d in (_wd if isinstance(_wd, (list, tuple)) else [])
                           if isinstance(d, int) and 0 <= d <= 6})
        backend = it.get("backend", "native")
        if backend not in ("native", "du"):
            backend = "native"
        size_mode = it.get("size_mode", "disk")
        if size_mode not in ("disk", "apparent"):
            size_mode = "disk"
        try:
            last_run = float(it.get("last_run", 0) or 0)
        except (TypeError, ValueError):
            last_run = 0.0
        if not (0.0 <= last_run < now + 86400 * 366):   # NaN·무한대·먼 미래(잘못된 값) → 미실행으로
            last_run = 0.0
        try:
            anchor = float(it.get("anchor", 0) or 0)
        except (TypeError, ValueError):
            anchor = 0.0
        # 생성 시각(주/월 반복주기 정렬 기준). 비정상 값(예: 1e300)은 날짜 변환에서 OverflowError 를 내
        # 매 틱 예약 검사 전체를 멈췄다 → 합리적 범위(1970~지금+100년) 밖이면 지금으로.
        if not (0.0 < anchor < now + 86400 * 365 * 100):
            anchor = now
        hh, mm = parse_at(it.get("at"))
        # minute 일 때 every_minutes 도 유지(구버전 읽기 호환)
        every_minutes = every if unit == "minute" else _int("every_minutes", 1, 100000, 60)
        # 순차 체인 진행 상태(런타임): chain_i=현재 단계 인덱스, chain_scan_id=그 단계의 스캔 id
        try:
            chain_i = max(0, int(it.get("chain_i", 0) or 0))
        except (TypeError, ValueError):
            chain_i = 0
        _csid = it.get("chain_scan_id")
        try:
            chain_scan_id = int(_csid) if _csid not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            chain_scan_id = None
        try:   # 비정상 종료로 멈춘 체인 스캔의 자동 재개 시도 횟수(런타임)
            chain_resumes = max(0, min(100, int(it.get("chain_resumes", 0) or 0)))
        except (TypeError, ValueError):
            chain_resumes = 0
        _sk = it.get("chain_skipped")          # 시작 못 해 건너뛴 경로 → 처음 건너뛴 시각(런타임, 화면 표시용)
        chain_skipped = {}
        if isinstance(_sk, dict):
            for _p, _t in _sk.items():
                if isinstance(_p, str) and _p in plist:
                    try:
                        chain_skipped[_p] = float(_t)
                    except (TypeError, ValueError, OverflowError):
                        pass
        out.append({
            "path": path,            # 호환: 첫 경로(paths[0])
            "paths": plist,          # 순차 스캔 대상(순서대로)
            "chain_i": chain_i,
            "chain_scan_id": chain_scan_id,
            "chain_resumes": chain_resumes,
            "chain_skipped": chain_skipped,
            "unit": unit,
            "every": every,
            "at": "%02d:%02d" % (hh, mm),
            "weekdays": weekdays,
            "start_month": _int("start_month", 1, 12, 1),
            "start_day": _int("start_day", 1, 31, 1),
            "backend": backend,
            "size_mode": size_mode,
            "one_file_system": bool(it.get("one_file_system", False)),
            "enabled": bool(it.get("enabled", True)),
            "anchor": anchor,
            "last_run": last_run,
            "every_minutes": every_minutes,
        })
    return out


def schedule_due(sc: dict, now: float) -> bool:
    """예약 스캔이 지금 실행될 차례인지 판정한다(시작 + 반복주기 모델).

    - minute/hour: 마지막 실행 후 every 간격이 지났으면 실행(간격형).
    - day/week/month: 그날의 지정 시각(at)을 지났고, 오늘이 반복 조건에 맞으며,
      오늘 슬롯을 아직 실행하지 않았으면 실행(달력형).
    """
    unit = sc.get("unit", "minute")
    every = max(1, int(sc.get("every", 1) or 1))
    last = float(sc.get("last_run", 0) or 0)
    if last > now + 60:
        # 시계가 뒤로 갔다(NTP 보정 등) — 미래의 '마지막 실행'은 그대로 믿지 않는다. 예전엔 그 차이만큼(예: 1시간·이틀)
        # 실행이 멈췄다(25회차 점검). (저장된 last_run 은 실제로 돌 때만 바뀌므로, 여기서 '지금'으로 고쳐 보면 계속 미래라
        # 결국 같은 침묵이 된다.) 간격형은 바로 차례로(한 번 더 도는 정도), 달력형은 '오늘 날짜'의 기록이면 오늘 슬롯은
        # 이미 돈 것으로(같은 슬롯 두 번 방지, 26회차 점검), 다른 날짜(하루 넘게 앞섬)면 기록 없음으로.
        if unit in ("minute", "hour"):
            last = 0.0
        elif _dt.datetime.fromtimestamp(last).date() != _dt.datetime.fromtimestamp(now).date():
            last = 0.0
    if unit == "minute":
        return (now - last) >= every * 60
    if unit == "hour":
        return (now - last) >= every * 3600

    n = _dt.datetime.fromtimestamp(now)
    hh, mm = parse_at(sc.get("at"))
    # fold=0: 서머타임 해제로 같은 시각이 두 번 오는 날엔 첫 번째 것으로 고정(결정적).
    slot = n.replace(hour=hh, minute=mm, second=0, microsecond=0, fold=0)
    if n < slot:
        return False                       # 오늘 지정 시각 전
    # 오늘 슬롯을 이미 돌았는지는 '현지 시각'끼리 비교한다. 서머타임 시작일에 없는 시각(예: 02:30)은
    # slot.timestamp() 가 03:30 으로 계산돼, 03:00 에 돈 뒤에도 03:30 까지 매 틱 다시 '차례'가 됐다(30분간 반복 실행).
    if last > 0 and _dt.datetime.fromtimestamp(last) >= slot:
        return False                       # 오늘 슬롯은 이미 실행됨
    anchor = _dt.datetime.fromtimestamp(float(sc.get("anchor", 0) or now))

    if unit == "day":
        delta = (n.date() - anchor.date()).days
        return delta >= 0 and delta % every == 0
    if unit == "week":
        wd = (n.weekday() + 1) % 7         # 0=일 .. 6=토
        # 요일이 비면 만든 날의 요일로 — 예전엔 영영 돌지 않았다(25회차 점검)
        wds = sc.get("weekdays") or [(anchor.weekday() + 1) % 7]
        if wd not in wds:
            return False
        # 주 묶음은 일요일 시작 달력 주로, 0주차 = 만든 뒤 첫 '선택한 요일'이 든 주 — 만든 요일 기준으로 끊으면 월·금이
        # 서로 다른 주에 들어갔고(25회차 점검), 만든 주 기준이면 만든 다음 첫 해당 요일을 건너뛰었다.
        def _wk0(d):
            return d - _dt.timedelta(days=(d.weekday() + 1) % 7)
        first = anchor.date()
        for _i in range(7):
            if (first.weekday() + 1) % 7 in wds:
                break
            first += _dt.timedelta(days=1)
        weeks = (_wk0(n.date()) - _wk0(first)).days // 7
        return weeks >= 0 and weeks % every == 0
    if unit == "month":
        sd = int(sc.get("start_day", 1) or 1)
        # 29~31일 지정은 그 날이 없는 달엔 말일에 돈다(예전엔 2·4·6·9·11월을 조용히 건너뜀).
        import calendar as _cal
        last_dom = _cal.monthrange(n.year, n.month)[1]
        if n.day != min(sd, last_dom):
            return False
        sm = int(sc.get("start_month", 1) or 1)
        # 생성 연도의 시작 월을 기준으로 every 개월 간격의 격자에서, 만든 날 이후 첫 발생부터 — 예전엔 시작일이 이미
        # 지났으면 첫 발생을 다음 해로 미뤄 '매 1개월'인데 1년을 기다렸다(25회차 점검: 11/5 시작·11/19 생성 → 이듬해 11/5)
        months = (n.year - anchor.year) * 12 + (n.month - sm)
        return months >= 0 and months % every == 0 and n.date() >= anchor.date()
    return False


def stagger_schedules(schedules: list) -> list:
    """활성 '간격형'(minute/hour) 예약들을 서로 다른 시각에 돌도록 위상을 분산한다.

    한 서버에서 여러 NAS 를 짧은 주기로 예약 스캔할 때 동시에 시작하면 GIL/단일 DB
    직렬화로 경합만 커진다. 그래서 같은 unit 의 간격형 예약들의 '다음 실행 시각'을
    주기 안에서 고르게 분산한다(last_run 조정).

    달력형(day/week/month)은 사용자가 지정한 시각(at)을 그대로 둔다 — at 을 임의로
    밀면 '오늘 이미 실행한 슬롯'을 다시 깨워 같은 날 중복 실행될 수 있기 때문이다
    (달력형끼리 겹치면 예약 화면에서 시각을 다르게 지정하면 된다).
    원본을 변형하지 않고 보정된 새 리스트를 돌려준다(호출 측이 저장).
    """
    out = [dict(s) for s in (schedules or [])]
    now = _time.time()
    active = [s for s in out if s.get("enabled", True)]
    for unit, mult in (("minute", 60), ("hour", 3600)):
        grp = [s for s in active if s.get("unit") == unit]
        n = len(grp)
        for i, s in enumerate(grp):
            every = max(1, int(s.get("every", 1) or 1))
            period = every * mult
            # 다음 실행 = now + period*(i+1)/(n+1) (즉시 실행 방지 + 균등 분산)
            s["last_run"] = now - period + period * (i + 1) / (n + 1)
    return out


def load(data_dir: str) -> dict:
    """설정을 읽는다(파일이 없거나 깨졌으면 기본값)."""
    s = dict(DEFAULTS)
    disk = read_json_with_backup(settings_path(data_dir))
    if disk:
        s.update(disk)
    return sanitize(s)


def save(data_dir: str, raw: dict) -> dict:
    """설정을 보정해 원자적으로 저장하고, 저장된 값을 반환한다."""
    s = sanitize(raw)
    # 서비스 자격증명(SMTP·Isilon·PowerStore·api_token·LLM 키)이 평문으로 들어가므로
    # 소유자만 읽도록 권한을 제한한다(POSIX — 임시 파일 단계부터 0600). Windows 등에서는 무시.
    write_json_atomic(settings_path(data_dir), s)
    return s


def seed_if_absent(data_dir: str, initial: dict) -> dict:
    """settings.json 이 없을 때만 CLI 등에서 받은 초기값으로 생성한다.

    이미 있으면 기존 파일(웹에서 편집한 값)을 그대로 둔다.
    """
    os.makedirs(data_dir, exist_ok=True)
    if os.path.exists(settings_path(data_dir)):
        # 비밀번호 칸만 든 파일(reset-password 를 먼저 돌린 새 폴더)은 아직 '설정 없음'으로 본다 — 그 키에 없는 값만
        # CLI 초기값으로 채운다(예전엔 --mount-base·--lock-settings 가 조용히 무시돼 '허용 경로 제한 없음', 26회차 점검)
        raw = read_json_with_backup(settings_path(data_dir)) or {}
        if raw and all(str(k).startswith("op_") for k in raw):
            base = dict(DEFAULTS)
            base.update({k: v for k, v in (initial or {}).items() if k in DEFAULTS})
            base.update(raw)
            return save(data_dir, base)
        return load(data_dir)
    base = dict(DEFAULTS)
    base.update({k: v for k, v in (initial or {}).items() if k in DEFAULTS})
    return save(data_dir, base)


# ----- 데이터 영속화 가드(재부팅 시 마운트 누락으로 빈 폴더에 새 식별자 만드는 사고 방지) -----
# 증상: 재부팅하면 노드·토큰·설정이 사라진다. 원인은 보통 data-dir 가 별도 영속 디스크 마운트인데,
# 그 마운트가 붙기 전에 서비스가 떠서 '빈 폴더'를 보고 새로 초기화(새 토큰 생성·노드 0) 해버리는 것.
# 코드 폴더(영속 root fs, 재부팅에 보존)에 '초기화됨' 표식을 남겨, 다음 시작 때 표식은 있는데 data-dir
# 가 비어 있으면 '마운트가 아직 안 붙음'으로 보고 새로 초기화하지 않고 대기(서비스 재시도)한다.
_PERSIST_SENTINEL = ".iu_data_persist.json"


def _persist_sentinel_path(code_dir: str) -> str:
    return os.path.join(code_dir, _PERSIST_SENTINEL)


def _data_present(data_dir: str, proxies) -> bool:
    """data-dir 에 '실제 데이터(식별자)'가 있다고 볼 만한 파일이 하나라도 있나."""
    return any(os.path.exists(os.path.join(data_dir, p)) for p in proxies)


def check_persistence(data_dir: str, code_dir: str, proxies):
    """반환 (ok, reason). ok=False 면 호출측이 시작을 중단(systemd 재시도)해 마운트를 기다려야 한다.

    표식(코드 폴더)이 '같은 data-dir 로 초기화됨'을 말하는데 지금 data-dir 가 비어 있으면 차단한다.
    표식이 없거나(첫 설치) data-dir 에 데이터가 있으면 통과한다.
    """
    data_dir = os.path.abspath(data_dir or "")
    try:
        with open(_persist_sentinel_path(code_dir), encoding="utf-8") as fh:
            mark = json.load(fh)
    except (OSError, ValueError):
        mark = None
    known = set()
    if isinstance(mark, dict) and mark.get("initialized"):
        if mark.get("data_dir"):
            known.add(os.path.abspath(mark.get("data_dir")))
        for k in (mark.get("data_dirs") or {}):     # 같은 코드 폴더로 여러 데이터 폴더(엣지·포탈 등)를 띄우는 경우
            known.add(os.path.abspath(k))
    if data_dir in known and not _data_present(data_dir, proxies):
        return False, (
            "이전에 초기화된 데이터 폴더(%s)가 비어 있습니다 — 영속 디스크가 아직 마운트되지 "
            "않은 것으로 보입니다. 새로 초기화하지 않고 마운트를 기다립니다(서비스가 곧 재시도). "
            "의도적으로 초기화하려면 표식 파일 %s 에서 이 폴더 항목을 지우거나 파일을 지우세요."
            % (data_dir, _persist_sentinel_path(code_dir)))
    return True, None


def mark_initialized(data_dir: str, code_dir: str, proxies, detail=None) -> None:
    """data-dir 에 실제 데이터가 있을 때만 '초기화됨' 표식을 코드 폴더(영속 root fs)에 남긴다."""
    data_dir = os.path.abspath(data_dir or "")
    if not _data_present(data_dir, proxies):
        return
    # 데이터 폴더마다 기록 — 예전엔 마지막에 띄운 폴더 하나만 남아, 같은 코드 폴더로 띄운 다른 폴더(엣지·포탈 등)는
    # 마운트가 빠져도 지켜지지 않았다(26회차 점검). 동시에 뜨는 여러 서비스가 서로의 기록을 덮지 않게 잠금(27회차 점검).
    lockf = None
    try:
        import fcntl
        lockf = open(_persist_sentinel_path(code_dir) + ".lock", "a")
        fcntl.flock(lockf, fcntl.LOCK_EX)
    except (ImportError, OSError):
        lockf = None
    try:
        _mark_initialized_locked(data_dir, code_dir, detail)
    finally:
        if lockf is not None:
            try:
                lockf.close()
            except OSError:
                pass


def _mark_initialized_locked(data_dir: str, code_dir: str, detail=None) -> None:
    try:
        with open(_persist_sentinel_path(code_dir), encoding="utf-8") as fh:
            old_mark = json.load(fh)
    except (OSError, ValueError):
        old_mark = None
    dirs = {}
    if isinstance(old_mark, dict):
        if isinstance(old_mark.get("data_dirs"), dict):
            dirs.update({str(k): v for k, v in old_mark["data_dirs"].items()})
        elif old_mark.get("data_dir"):
            dirs[os.path.abspath(old_mark["data_dir"])] = {k: v for k, v in old_mark.items()
                                                          if k not in ("initialized", "data_dir", "data_dirs")}
    dirs.pop(data_dir, None)               # 다시 띄운 폴더는 맨 뒤로(가장 최근) — 쓰는 폴더가 밀려나지 않게(27회차 점검)
    dirs[data_dir] = dict(detail or {})
    while len(dirs) > 50:
        dirs.pop(next(iter(dirs)))
    rec = {"initialized": True, "data_dir": data_dir, "data_dirs": dirs}
    if detail:
        rec.update(detail)
    try:
        p = _persist_sentinel_path(code_dir)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass

"""웹에서 보고 수정하는 런타임 설정.

설정은 `<data-dir>/settings.json` 에 저장되어 서버를 재시작해도 유지된다.
대시보드의 "설정" 카드(GET/POST /api/settings)에서 편집한다.

여기 담는 것은 "런타임에 바꿔도 되는" 설정들이다. host/port/data-dir 처럼
서버 구동에 고정되는 값은 읽기 전용 정보로만 보여준다.
"""


import datetime as _dt
import json
import os
import time as _time


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
    "fold_depth": 0,                    # 깊이 접기(0=off): N까지만 행 저장, 그 아래는 용량만 N에 합산(합계 정확)
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
    "op_ttl_minutes": 30,               # 로그인 세션 유지 시간(분) — 사용자가 지정
    "show_update_popup": False,         # 업그레이드 후 첫 접속 시 변경내용 팝업(기본 끔)
    "default_engine": "threads",        # 새 스캔 기본 엔진: threads(상세) | pscan(빠른 용량)
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
    "ask_llm_enabled": False,           # 자연어 질의응답에 로컬 LLM 사용(끄면 규칙 기반만)
    "ask_llm_endpoint": "",             # 로컬 LLM(OpenAI 호환) 주소(예: http://127.0.0.1:11434/v1)
    "ask_llm_model": "",                # 모델 이름(예: qwen2.5:7b)
    "ask_llm_key": "",                  # 인증 토큰(로컬은 보통 불필요)
    "ask_llm_timeout": 20,              # LLM 응답 대기(초)
}

EDITABLE_KEYS = set(DEFAULTS.keys())


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
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return dflt

    def _float(v, lo, hi, dflt):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return dflt

    s["batch_size"] = _int(s["batch_size"], 1, 1_000_000, 500)
    s["sample_interval"] = _float(s["sample_interval"], 0.2, 60.0, 2.0)
    s["top_n"] = _int(s["top_n"], 1, 500, 20)
    s["refresh_ms"] = _int(s["refresh_ms"], 500, 600_000, 1500)
    s["scan_workers"] = _int(s["scan_workers"], 1, 64, 4)
    s["scan_max_depth"] = _int(s.get("scan_max_depth"), 0, 100000, 0)
    s["fold_depth"] = _int(s.get("fold_depth"), 0, 100000, 0)
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
    s["op_ttl_minutes"] = _int(s.get("op_ttl_minutes", 30), 1, 10080, 30)   # 1분~7일
    s["show_update_popup"] = bool(s.get("show_update_popup", False))
    s["default_engine"] = (s.get("default_engine")
                           if s.get("default_engine") in ("threads", "pscan") else "threads")
    s["upgrade_watch_dir"] = str(s.get("upgrade_watch_dir") or "").strip()
    try:
        s["upgrade_check_secs"] = max(10, int(s.get("upgrade_check_secs", 60) or 60))
    except (TypeError, ValueError):
        s["upgrade_check_secs"] = 60
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
    s["mount_bases"] = [os.path.abspath(x.strip()) for x in mb if str(x).strip()]

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
            except (TypeError, ValueError):
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
        weekdays = sorted({d for d in (it.get("weekdays") or [])
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
        try:
            anchor = float(it.get("anchor", 0) or 0)
        except (TypeError, ValueError):
            anchor = 0.0
        if anchor <= 0:
            anchor = now   # 생성 시각(주/월 반복주기 정렬 기준)
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
        out.append({
            "path": path,            # 호환: 첫 경로(paths[0])
            "paths": plist,          # 순차 스캔 대상(순서대로)
            "chain_i": chain_i,
            "chain_scan_id": chain_scan_id,
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
    if unit == "minute":
        return (now - last) >= every * 60
    if unit == "hour":
        return (now - last) >= every * 3600

    n = _dt.datetime.fromtimestamp(now)
    hh, mm = parse_at(sc.get("at"))
    slot = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if n < slot:
        return False                       # 오늘 지정 시각 전
    if last >= slot.timestamp():
        return False                       # 오늘 슬롯은 이미 실행됨
    anchor = _dt.datetime.fromtimestamp(float(sc.get("anchor", 0) or now))

    if unit == "day":
        delta = (n.date() - anchor.date()).days
        return delta >= 0 and delta % every == 0
    if unit == "week":
        wd = (n.weekday() + 1) % 7         # 0=일 .. 6=토
        if wd not in (sc.get("weekdays") or []):
            return False
        weeks = (n.date() - anchor.date()).days // 7
        return weeks >= 0 and weeks % every == 0
    if unit == "month":
        sd = int(sc.get("start_day", 1) or 1)
        if n.day != sd:
            return False
        sm = int(sc.get("start_month", 1) or 1)
        # 시작(start_month/day)이 생성 연도에 이미 지났으면 첫 발생은 다음 해
        fo_year = anchor.year if (sm, sd) >= (anchor.month, anchor.day) else anchor.year + 1
        months = (n.year - fo_year) * 12 + (n.month - sm)
        return months >= 0 and months % every == 0
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
    try:
        with open(settings_path(data_dir), "r", encoding="utf-8") as fh:
            s.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return sanitize(s)


def save(data_dir: str, raw: dict) -> dict:
    """설정을 보정해 원자적으로 저장하고, 저장된 값을 반환한다."""
    s = sanitize(raw)
    os.makedirs(data_dir, exist_ok=True)
    path = settings_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return s


def seed_if_absent(data_dir: str, initial: dict) -> dict:
    """settings.json 이 없을 때만 CLI 등에서 받은 초기값으로 생성한다.

    이미 있으면 기존 파일(웹에서 편집한 값)을 그대로 둔다.
    """
    os.makedirs(data_dir, exist_ok=True)
    if os.path.exists(settings_path(data_dir)):
        return load(data_dir)
    base = dict(DEFAULTS)
    base.update({k: v for k, v in (initial or {}).items() if k in DEFAULTS})
    return save(data_dir, base)

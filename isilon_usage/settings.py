"""웹에서 보고 수정하는 런타임 설정.

설정은 `<data-dir>/settings.json` 에 저장되어 서버를 재시작해도 유지된다.
대시보드의 "설정" 카드(GET/POST /api/settings)에서 편집한다.

여기 담는 것은 "런타임에 바꿔도 되는" 설정들이다. host/port/data-dir 처럼
서버 구동에 고정되는 값은 읽기 전용 정보로만 보여준다.
"""


import json
import os


# 키: (기본값) — 새 설정 항목을 추가하면 여기에 등록한다.
DEFAULTS: dict = {
    "default_backend": "native",        # 새 스캔 기본 백엔드(native/du)
    "default_size_mode": "disk",        # 기본 용량 기준(disk/apparent)
    "default_one_file_system": False,   # 기본 -x(한 파일시스템)
    "batch_size": 500,                  # DB 커밋 배치 크기
    "sample_interval": 2.0,             # 자원 샘플링 주기(초)
    "mount_bases": [],                  # 웹에서 스캔 허용할 경로 목록(빈 목록=전체 허용)
    "top_n": 20,                        # 상위 디렉터리 표시 개수
    "refresh_ms": 1500,                 # 대시보드 자동 새로고침 주기(ms)
    "scan_workers": 8,                  # 동시 스캔 스레드 수(디렉터리 단위 병렬, NFS 가속, 더 올릴수록 빠름)
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
    s["scan_workers"] = _int(s["scan_workers"], 1, 64, 8)
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

    mb = s.get("mount_bases") or []
    if isinstance(mb, str):
        mb = mb.replace(",", "\n").splitlines()
    s["mount_bases"] = [os.path.abspath(x.strip()) for x in mb if str(x).strip()]

    s["schedules"] = _sanitize_schedules(s.get("schedules"))
    return s


def _sanitize_schedules(raw) -> list:
    """예약 스캔 목록 보정. 각 항목:
    {path, every_minutes, backend, size_mode, one_file_system, enabled, last_run}
    """
    out = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        path = str(it.get("path") or "").strip()
        if not path:
            continue
        try:
            every = max(1, int(it.get("every_minutes", 60)))
        except (TypeError, ValueError):
            every = 60
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
        out.append({
            "path": os.path.abspath(path),
            "every_minutes": every,
            "backend": backend,
            "size_mode": size_mode,
            "one_file_system": bool(it.get("one_file_system", False)),
            "enabled": bool(it.get("enabled", True)),
            "last_run": last_run,
        })
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

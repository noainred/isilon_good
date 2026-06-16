"""글로벌 통합 포탈(HQ) — 여러 데이터센터(엣지)를 한 화면에서 조망한다.

각 엣지는 `serve` 로 로컬 스토리지를 스캔하고 `/api/dbexport`(토큰 보호)로
완료된 per-run DB + 노드 요약(meta.json)을 tar.gz 로 제공한다. 포탈은:
  - 노드(엣지) 목록을 portal_nodes.json 에 보관(웹에서 추가/수정/삭제)
  - 주기적으로 각 노드를 폴링(연결·요약)하고 완료 DB 를 replicas/<id>/ 로 복제
  - 전체/지역/노드별 사용량 롤업을 글로벌 대시보드로 제공

표준 라이브러리만 사용한다(폐쇄망 호환). 엣지 한 곳만 호출하므로 운영이 단순하다.
"""

from typing import Optional

import io
import json
import os
import re
import secrets
import shlex
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import db as dbmod
from . import manager as mgrmod
from . import auth as authmod
from . import audit as auditmod
from . import settings as setmod
from . import upgrade as upgrademod
from .server import ThreadingHTTPServer, browse_dir  # 3.6 폴백 포함 재사용


HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_HTML = os.path.join(HERE, "portal.html")

# 폴링(연결+요약)은 자주, 복제(완료 DB 내려받기)는 노드별 주기마다.
POLL_EVERY = 15.0       # 초
SCHED_TICK = 5.0        # 초
FAR_FUTURE = 9999999999  # since 가 미래면 meta.json 만 받아 가볍게 연결/요약 확인


# --------------------------------------------------------------- 노드 레지스트리
def nodes_path(data_dir: str) -> str:
    return os.path.join(data_dir, "portal_nodes.json")


def portal_settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, "portal_settings.json")


def ping_history_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ping_history.db")


def sanitize_node(raw: dict) -> Optional[dict]:
    """노드 1건을 보정한다(필수: id, url). 잘못되면 None."""
    if not isinstance(raw, dict):
        return None
    nid = str(raw.get("id") or "").strip()
    url = str(raw.get("url") or "").strip().rstrip("/")
    if not nid or not url:
        return None
    if not re.match(r"^https?://", url):
        url = "http://" + url
    try:
        interval = max(1, int(raw.get("interval_minutes", 30)))
    except (TypeError, ValueError):
        interval = 30
    mode = raw.get("mode", "both")
    if mode not in ("both", "poll", "replicate"):
        mode = "both"

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # 복제 주기: 예약 스캔과 같은 '시작 + 반복주기' 모델(분/시간/일/주/개월).
    # 구버전(interval_minutes 만 있던) 노드는 unit=minute, every=interval_minutes 로 호환.
    unit = raw.get("unit")
    if unit not in ("minute", "hour", "day", "week", "month"):
        unit = "minute"
    if raw.get("every") is not None:
        try:
            every = max(1, int(raw.get("every")))
        except (TypeError, ValueError):
            every = interval if unit == "minute" else 1
    else:
        every = interval if unit == "minute" else 1
    weekdays = sorted({d for d in (raw.get("weekdays") or [])
                       if isinstance(d, int) and 0 <= d <= 6})
    hh, mm = setmod.parse_at(raw.get("at"))
    anchor = _f(raw.get("anchor")) or time.time()

    def _i(key, lo, hi, dflt):
        try:
            return max(lo, min(hi, int(raw.get(key, dflt))))
        except (TypeError, ValueError):
            return dflt

    return {
        "id": nid,
        "region": str(raw.get("region") or "").strip(),
        "url": url,
        "token": str(raw.get("token") or ""),
        # 경로 별칭: 이 노드의 로컬 마운트 접두어 ↔ 공통 논리 접두어
        # 예) alias_local=/mnt/isilon/data, alias_logical=/data
        "alias_local": str(raw.get("alias_local") or "").strip().rstrip("/"),
        "alias_logical": str(raw.get("alias_logical") or "").strip().rstrip("/"),
        "interval_minutes": interval,
        "unit": unit,
        "every": every,
        "at": "%02d:%02d" % (hh, mm),
        "weekdays": weekdays,
        "start_month": _i("start_month", 1, 12, 1),
        "start_day": _i("start_day", 1, 31, 1),
        "anchor": anchor,
        "mode": mode,
        "enabled": bool(raw.get("enabled", True)),
        "last_poll": _f(raw.get("last_poll")),
        "last_sync": _f(raw.get("last_sync")),
        "last_status": str(raw.get("last_status") or "unknown"),
        "last_error": str(raw.get("last_error") or ""),
    }


def load_nodes(data_dir: str) -> list:
    try:
        with open(nodes_path(data_dir), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for it in (raw.get("nodes") if isinstance(raw, dict) else raw) or []:
        n = sanitize_node(it)
        if n:
            out.append(n)
    return out


def save_nodes(data_dir: str, nodes: list) -> None:
    """노드 레지스트리를 원자적으로 저장한다.

    병렬 복제 스레드가 동시에 저장해도 충돌하지 않도록 스레드마다 '고유 임시파일'을 쓰고
    (os.replace 로 원자 교체), 노드 스냅샷을 떠 직렬화 중 변경 영향을 피한다.
    """
    os.makedirs(data_dir, exist_ok=True)
    path = nodes_path(data_dir)
    payload = json.dumps({"nodes": [dict(n) for n in nodes]},
                         ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".portal_nodes-", suffix=".tmp", dir=data_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _public_node(n: dict, cache: dict) -> dict:
    """화면용 노드(토큰 마스킹 + 실시간 캐시 상태 병합)."""
    c = cache.get(n["id"], {})
    ov = c.get("overall") or {}
    out = dict(n)
    out["token"] = ""
    out["token_set"] = bool(n.get("token"))
    out["online"] = bool(c.get("online"))
    out["version"] = c.get("version")
    out["hostname"] = c.get("hostname")
    out["error"] = n.get("last_error") or c.get("error") or ""
    out["active_scans"] = int(ov.get("active_scans") or 0)   # 지금 스캔 중인 개수
    out["scanning"] = out["active_scans"] > 0
    out["used_bytes"] = int(ov.get("total_scanned_bytes") or 0)
    return out


# ------------------------------------------------------------------ 네트워크
def _http_get(url: str, token: Optional[str] = None, timeout: float = 8.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Auth-Token", token)
    return urllib.request.urlopen(req, timeout=timeout)


def _probe_meta(url: str, token: Optional[str], timeout: float = 8.0) -> dict:
    """엣지의 /api/dbexport?since=먼미래 로 meta.json 만 받아(가볍게) 요약을 돌려준다.

    연결 확인 + 토큰 검증 + 노드 요약(overall)을 한 번에 처리한다.
    """
    u = url.rstrip("/") + "/api/dbexport?since=%d" % FAR_FUTURE
    resp = _http_get(u, token, timeout=timeout)
    data = resp.read()
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    try:
        f = tf.extractfile("meta.json")
        return json.loads(f.read().decode("utf-8"))
    finally:
        tf.close()


# ------------------------------------------------------------ 원격 자동 구성
def agent_bundle_bytes() -> bytes:
    """엣지에 배포할 isilon_usage 패키지(.py/.html)를 tar.gz 로 묶는다.

    엣지가 HQ 포탈에서 코드를 직접 받아 설치할 수 있게 한다(폐쇄망에서도 인터넷
    없이). 포탈이 실행 중인 바로 그 패키지를 그대로 내려준다.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py") or name.endswith(".html"):
                tf.add(os.path.join(HERE, name), arcname="isilon_usage/" + name)
    return buf.getvalue()


def agent_bundle_version() -> str:
    """포탈이 엣지에 푸시할(=디스크 HERE 의) isilon_usage 코드 버전(__init__.py 의 __version__).

    실행 중 메모리 버전(__version__)과 다르면 '코드는 갱신됐는데 재시작이 안 됨'을 뜻한다.
    """
    try:
        with open(os.path.join(HERE, "__init__.py"), encoding="utf-8") as fh:
            m = re.search(r"""__version__\s*=\s*["'](\d+\.\d+\.\d+)["']""", fh.read())
        return m.group(1) if m else ""
    except OSError:
        return ""


def newest_release_archive(release_dir: str):
    """release_dir 안의 가장 최신 isilon_usage-*.tar.gz 경로를 반환(없으면 None).

    엣지가 포탈에서 코드를 받아 오프라인(인터넷 없이) 업그레이드하도록, 운영자가 그 폴더에
    떨군 릴리스 tarball 을 서빙하기 위함이다. 파일명에서 버전을 읽어 최고 버전을 고르고,
    버전을 못 읽으면(예: isilon_usage-latest.tar.gz) mtime 으로 고른다.
    """
    d = (release_dir or "").strip()
    if not d or not os.path.isdir(d):
        return None
    cands = [os.path.join(d, n) for n in os.listdir(d)
             if n.startswith("isilon_usage-") and (n.endswith(".tar.gz") or n.endswith(".tgz"))]
    if not cands:
        return None

    def _key(p):
        m = re.search(r"isilon_usage-(\d+)\.(\d+)\.(\d+)\.(?:tar\.gz|tgz)$", os.path.basename(p))
        ver = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0.0
        return (ver, mt)

    return max(cands, key=_key)


def _ver_tuple(v):
    """'1.67.0' → (1,67,0). 못 읽으면 None(버전 비교 불가 표시)."""
    m = re.match(r"\s*v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def _poll_error_msg(exc) -> str:
    """노드 폴링 예외를 운영자가 바로 고칠 수 있는 안내 문구로 바꾼다(원시 'HTTP Error 401' 대신)."""
    code = getattr(exc, "code", None)
    if code == 401:
        return ("토큰 불일치(401): 엣지의 api_token 과 포탈에 저장된 이 노드의 토큰이 다릅니다. "
                "엣지에서 설치 한 줄에 --hq 를 붙여 재등록하거나, '노드 관리'에서 토큰을 엣지 값과 맞추세요.")
    if code == 403:
        return "거부됨(403): 엣지에 api_token 이 없거나 웹 접근이 막혀 있습니다(엣지 설정 확인)."
    if code == 404:
        return "응답 없음(404): 구버전 엣지이거나 주소/경로가 다릅니다."
    return str(exc)


def build_provision_script(*, host, port, token, path, hq_base, install="systemd",
                           data_dir="/data/isilon_edge_data",
                           edge_dir="/opt/isilon_edge") -> str:
    """엣지에서 복붙 실행할 자동 구성 스크립트(bash)를 만든다.

    HQ 포탈에서 코드를 받아(agent-bundle) 배치하고, api_token·mount-base 와 함께
    serve 를 기동한다(systemd 또는 nohup). 값은 shell 주입을 막기 위해 따옴표 처리.
    """
    q = shlex.quote
    hq = (hq_base or "http://<HQ-IP>:8800").rstrip("/")
    head = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# === isilon_usage 엣지 자동 구성 (HQ 포탈 생성) ===\n"
        "EDGE_DIR=%s\nDATA=%s\nMOUNT=%s\nPORT=%d\nTOKEN=%s\nHQ=%s\n\n"
        'echo "[1/4] 기존 엣지 프로세스·서비스 중단 (포트 충돌 방지)"\n'
        "# 현재(isilon-edge)·구버전(isilon_usage) 서비스를 멈춘다(없으면 무시).\n"
        "for _svc in isilon-edge isilon_usage; do\n"
        '  if systemctl list-unit-files 2>/dev/null | grep -q "^${_svc}\\.service"; then\n'
        '    sudo systemctl disable --now "$_svc" >/dev/null 2>&1 || true\n'
        '    echo "  -> ${_svc} 중단"\n'
        "  fi\n"
        "done\n"
        "# 구버전 유닛 파일이 남아 포트를 잡지 않도록 제거(있으면).\n"
        'if [ -f /etc/systemd/system/isilon_usage.service ]; then\n'
        "  sudo rm -f /etc/systemd/system/isilon_usage.service\n"
        "  sudo systemctl daemon-reload || true\n"
        '  echo "  -> 구버전 isilon_usage.service 유닛 제거"\n'
        "fi\n"
        "# nohup 등으로 떠 있는 잔여 serve 프로세스 정리(매칭 없어도 계속).\n"
        'if pgrep -f "isilon_usage serve" >/dev/null 2>&1; then\n'
        '  sudo pkill -f "isilon_usage serve" >/dev/null 2>&1 || true\n'
        "  sleep 1\n"
        '  echo "  -> 잔여 serve 프로세스 종료"\n'
        "fi\n\n"
        'echo "[2/4] HQ 포탈에서 코드 받기 (인터넷 불필요)"\n'
        'sudo mkdir -p "$EDGE_DIR" "$DATA"\n'
        'curl -fsSL "$HQ/api/portal/agent-bundle" | sudo tar -xz -C "$EDGE_DIR"\n'
        'sudo find "$EDGE_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true\n'
        '(cd "$EDGE_DIR" && python3 -m isilon_usage --version)\n'
    ) % (q(edge_dir), q(data_dir), q(path), int(port), q(token), q(hq))
    if install == "nohup":
        tail = (
            '\necho "[3/4] nohup 으로 기동 (포트 $PORT)"\n'
            'cd "$EDGE_DIR"\n'
            'nohup python3 -m isilon_usage serve --data-dir "$DATA" --mount-base "$MOUNT" \\\n'
            '    --host 0.0.0.0 --port "$PORT" --api-token "$TOKEN" \\\n'
            '    > /var/log/isilon_usage.log 2>&1 &\n'
            'echo "[4/4] 완료 — http://%s:%d/  (HQ 포탈이 자동 폴링)"\n'
        ) % (host, int(port))
    else:
        tail = (
            '\necho "[3/4] systemd 서비스 설치/기동 (포트 $PORT)"\n'
            "sudo tee /etc/systemd/system/isilon-edge.service >/dev/null <<UNIT\n"
            "[Unit]\n"
            "Description=Isilon Edge - 디렉터리 사용량 스캐너\n"
            "After=network-online.target remote-fs.target\n"
            "Wants=network-online.target\n"
            "[Service]\n"
            "Type=simple\n"
            "User=root\n"
            "WorkingDirectory=$EDGE_DIR\n"
            "ExecStart=/usr/bin/python3 -m isilon_usage serve --data-dir $DATA "
            "--mount-base $MOUNT --host 0.0.0.0 --port $PORT --api-token $TOKEN\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "UNIT\n"
            "sudo systemctl daemon-reload\n"
            "sudo systemctl enable --now isilon-edge\n"
            'echo "[4/4] 완료 — http://%s:%d/  ·  journalctl -u isilon-edge -f"\n'
        ) % (host, int(port))
    return head + tail


def build_upgrade_script(*, hq_base, edge_dir="/opt/isilon_edge",
                         service="isilon-edge") -> str:
    """등록된 엣지를 HQ 의 최신 코드로 올리는 bash 업그레이드 스크립트를 만든다.

    HQ 포탈에서 agent-bundle(현재 실행 중 코드)을 받아 엣지 코드 디렉터리에 덮어쓰고
    서비스를 재시작한다. 값은 shell 주입을 막기 위해 따옴표 처리한다.
    """
    q = shlex.quote
    hq = (hq_base or "http://<HQ-IP>:8800").rstrip("/")
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# === isilon_usage 엣지 원격 업그레이드 (HQ 포탈 생성) ===\n"
        "EDGE_DIR=%s\nHQ=%s\nSERVICE=%s\n\n"
        "# 지정한 서비스 유닛이 없으면 실제 설치된 isilon 엣지 서비스를 자동 탐색(이름 불일치 방지)\n"
        'if ! systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}\\.service"; then\n'
        '  for s in isilon-edge isilon_usage; do\n'
        '    if systemctl list-unit-files 2>/dev/null | grep -q "^${s}\\.service"; then SERVICE="$s"; break; fi\n'
        "  done\n"
        "fi\n"
        "# 서비스의 실제 코드 디렉터리를 systemd 에서 자동 감지(없으면 EDGE_DIR)\n"
        'WD=$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)\n'
        'TARGET="${WD:-$EDGE_DIR}"; [ -z "$TARGET" ] && TARGET="$EDGE_DIR"\n'
        'OLDV=$(cd "$TARGET" 2>/dev/null && python3 -m isilon_usage --version 2>/dev/null || echo "?")\n'
        'echo "[1/3] HQ 에서 최신 코드 받기 -> $TARGET (현재 $OLDV)"\n'
        'sudo mkdir -p "$TARGET"\n'
        'curl -fsSL "$HQ/api/portal/agent-bundle" | sudo tar -xz -C "$TARGET"\n'
        "# 낡은 바이트코드 캐시 제거 — 안 그러면 새 코드를 깔아도 옛 버전이 보고/실행될 수 있음\n"
        'sudo find "$TARGET" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true\n'
        'NEWV=$(cd "$TARGET" && python3 -m isilon_usage --version 2>/dev/null || echo "?")\n'
        'echo "  -> 새 코드: $NEWV"\n'
        'echo "[2/3] 서비스 재시작: $SERVICE"\n'
        'if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}\\.service"; then\n'
        '  sudo systemctl restart "$SERVICE"\n'
        '  echo "  -> systemctl restart $SERVICE 완료"\n'
        "else\n"
        '  echo "  WARN: systemd 유닛 ${SERVICE} 없음 - 코드는 $TARGET 에 갱신됨, 서비스를 수동 재시작하세요"\n'
        "fi\n"
        'echo "[3/3] 업그레이드 $OLDV -> $NEWV (대상 $TARGET, 서비스 $SERVICE)"\n'
    ) % (q(edge_dir), q(hq), q(service))


# ------------------------------------------------------------------ 컨트롤러
class PortalController:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.replicas_dir = os.path.join(self.data_dir, "replicas")
        os.makedirs(self.replicas_dir, exist_ok=True)
        self.nodes = load_nodes(self.data_dir)
        self.settings = self._load_settings()
        self._auth = authmod.AuthGuard(lambda: self.settings.get("op_password"))
        self._last_upgrade_check = 0.0
        self._init_ping_db()
        self._cache = {}          # id -> {online, ts, version, hostname, overall, error}
        self._inflight = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.poll_every = POLL_EVERY
        self.tick = SCHED_TICK
        self._upg_state = {"log": [], "installing": False, "available": False,
                           "latest": None, "last_check": None}   # 인터넷 자동 업그레이드 상태

    # --- 인증(작업 보호: 보기는 자유, 변경은 로그인) ---
    def _load_settings(self) -> dict:
        out = {"op_password": "", "op_password_encrypted": False,
               "upgrade_watch_dir": "", "upgrade_check_secs": 60,
               "upgrade_source": "off", "upgrade_url": "", "upgrade_token": "",
               "upgrade_auto": False,
               "enroll_token": "", "release_dir": "/opt/isilon_release"}
        try:
            with open(portal_settings_path(self.data_dir), encoding="utf-8") as fh:
                s = json.load(fh)
            if isinstance(s, dict):
                out["op_password"] = str(s.get("op_password") or "")
                out["op_password_encrypted"] = bool(s.get("op_password_encrypted", False))
                out["upgrade_watch_dir"] = str(s.get("upgrade_watch_dir") or "").strip()
                try:
                    out["upgrade_check_secs"] = max(10, int(s.get("upgrade_check_secs", 60) or 60))
                except (TypeError, ValueError):
                    pass
                _src = str(s.get("upgrade_source") or "off").strip().lower()
                out["upgrade_source"] = _src if _src in ("off", "github") else "off"
                out["upgrade_url"] = str(s.get("upgrade_url") or "").strip()
                out["upgrade_token"] = str(s.get("upgrade_token") or "").strip()
                out["upgrade_auto"] = bool(s.get("upgrade_auto"))
                out["enroll_token"] = str(s.get("enroll_token") or "").strip()
                out["release_dir"] = (str(s.get("release_dir") or "").strip()
                                      or "/opt/isilon_release")
        except (OSError, ValueError):
            pass
        return out

    def _save_settings(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        path = portal_settings_path(self.data_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.settings, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def auth_required(self) -> bool:
        return self._auth.required()

    def auth_status(self) -> dict:
        return {"ok": True, "op_required": self._auth.required(),
                "encrypted": bool(self.settings.get("op_password_encrypted"))}

    def login(self, password: str) -> dict:
        return self._auth.login(password)

    def token_valid(self, token) -> bool:
        return self._auth.token_valid(token)

    def set_password(self, new_password: str, encrypt: bool) -> dict:
        """비밀번호 설정/변경/해제. 호출 측(핸들러)에서 인증을 확인한다."""
        pw = str(new_password or "")
        enc = bool(encrypt)
        self.settings["op_password"] = authmod.store_password(pw, encrypt=enc) if pw else ""
        self.settings["op_password_encrypted"] = enc if pw else False
        self._save_settings()
        return {"ok": True, "op_required": bool(self.settings["op_password"]),
                "encrypted": self.settings["op_password_encrypted"]}

    def set_enroll_token(self, token: str) -> dict:
        """엣지 자기등록(enroll) 공유 토큰을 설정/해제. 호출 측에서 인증을 확인한다."""
        self.settings["enroll_token"] = str(token or "").strip()
        self._save_settings()
        return {"ok": True, "enroll_token_set": bool(self.settings["enroll_token"])}

    def set_release_dir(self, release_dir: str) -> dict:
        """엣지가 받아갈 릴리스 패키지 폴더를 설정(비우면 기본 /opt/isilon_release)."""
        self.settings["release_dir"] = (str(release_dir or "").strip()
                                        or "/opt/isilon_release")
        self._save_settings()
        return dict({"ok": True}, **self.release_info())

    def release_info(self) -> dict:
        """현재 release_dir 와 거기서 엣지에 내려줄 최신 패키지 정보(+진단)를 반환.

        '없음'으로 보일 때 원인을 바로 알 수 있도록, 포탈이 **실제로 읽는** 폴더 경로·존재 여부·
        그 폴더에서 보이는 isilon_usage-* 파일 목록·이 포탈의 호스트명·사유를 함께 돌려준다
        (파일을 다른 호스트/다른 경로에 두었거나 권한 문제인 경우를 바로 드러낸다).
        """
        import socket
        rel = (self.settings.get("release_dir") or "/opt/isilon_release")
        host = socket.gethostname()
        exists = os.path.isdir(rel)
        seen, n_all = [], 0
        if exists:
            try:
                names = os.listdir(rel)
                n_all = len(names)
                seen = sorted(n for n in names if n.startswith("isilon_usage-") and (
                    n.endswith(".tar.gz") or n.endswith(".tgz") or n.endswith(".zip")))
            except OSError:
                exists = False
        arc = newest_release_archive(rel)
        ver = ""
        if arc:
            m = re.search(r"isilon_usage-(\d+\.\d+\.\d+)", os.path.basename(arc))
            ver = m.group(1) if m else ""
        reason = ""
        # 파일명이 아니라 '실제 내용(__init__.py 의 __version__)'을 확인한다 — 엣지가 진짜 받게 될
        # 버전. 파일명만 1.70.1 이고 내용은 구버전인 '잘못 라벨된 tar.gz' 를 적발하기 위함이다.
        content_ver = ""
        if arc:
            try:
                from . import upgrade as _upg
                cv = _upg.members_version(_upg.read_package_members(arc))
                content_ver = _upg.vstr(cv) if cv else ""
            except Exception:   # noqa: BLE001 — 손상/이상 아카이브여도 정보 표시는 계속
                content_ver = ""
        mismatch = bool(content_ver and ver and content_ver != ver)
        if not arc:
            if not exists:
                reason = ("폴더가 없습니다: %s — 파일을 '이 포탈 서버(%s)'의 이 경로에 두세요."
                          % (rel, host))
            elif seen and not any(n.endswith((".tar.gz", ".tgz")) for n in seen):
                reason = ("tar.gz/tgz 가 없습니다(zip 은 엣지 풀로 못 풉니다). 보이는 파일: %s"
                          % ", ".join(seen))
            else:
                reason = ("isilon_usage-*.tar.gz 가 안 보입니다(폴더 파일 %d개). 파일을 '이 포탈 "
                          "서버(%s)'의 %s 에 두었는지 확인하세요." % (n_all, host, rel))
        elif mismatch:
            reason = ("⚠ 파일명은 v%s 인데 내용(실제 코드)은 v%s 입니다 — 이 tar.gz 가 잘못 "
                      "만들어졌습니다(엣지는 파일명이 아니라 내용 v%s 를 받습니다). 올바른 v%s "
                      "패키지로 교체하세요." % (ver, content_ver, content_ver, ver))
        elif not content_ver:
            reason = "내용 버전 확인 실패(손상되었거나 isilon_usage 패키지가 아닐 수 있음)."
        return {"release_dir": rel, "release_dir_exists": exists, "release_host": host,
                "release_seen": seen[:30], "release_count": n_all,
                "release_file": os.path.basename(arc) if arc else "",
                "release_version": ver, "release_content_version": content_ver,
                "release_version_mismatch": mismatch,
                "release_available": bool(arc), "release_reason": reason}

    # --- 자동 업그레이드 ---
    def set_upgrade_watch(self, watch_dir: str, check_secs=None) -> dict:
        self.settings["upgrade_watch_dir"] = str(watch_dir or "").strip()
        if check_secs is not None:
            try:
                self.settings["upgrade_check_secs"] = max(10, int(check_secs))
            except (TypeError, ValueError):
                pass
        self._save_settings()
        return {"ok": True, "upgrade_watch_dir": self.settings["upgrade_watch_dir"],
                "upgrade_check_secs": self.settings["upgrade_check_secs"]}

    def upgrade_config(self) -> dict:
        bver = agent_bundle_version()
        return dict({"ok": True, "upgrade_watch_dir": self.settings.get("upgrade_watch_dir", ""),
                     "upgrade_check_secs": self.settings.get("upgrade_check_secs", 60),
                     "enroll_token_set": bool(self.settings.get("enroll_token")),
                     "hq_version": __version__, "bundle_version": bver,
                     "bundle_stale": bool(bver and bver != __version__)},
                    **self.release_info())

    def push_upgrade_all(self) -> dict:
        """등록된 모든 엣지에 현재(=디스크) 코드 번들을 동기로 푸시한다(엣지 api_token 인증)."""
        data = agent_bundle_bytes()
        bver = agent_bundle_version() or "?"
        nodes = self._push_init(bver)
        results = self._push_loop(data, bver, nodes)
        return {"ok": True, "hq_version": __version__, "bundle_version": bver, "results": results}

    def start_push_all(self) -> dict:
        """전 노드 푸시를 백그라운드로 시작하고 즉시 반환(모달이 진행 상태를 폴링해 라이브 표시)."""
        with self._lock:
            if (self._upg_state.get("push") or {}).get("active"):
                return {"ok": False, "error": "이미 전파 중입니다."}
        data = agent_bundle_bytes()
        bver = agent_bundle_version() or "?"
        nodes = self._push_init(bver)     # 모달이 즉시 보도록 동기 초기화
        threading.Thread(target=lambda: self._push_loop(data, bver, nodes),
                         name="push-all", daemon=True).start()
        return {"ok": True, "started": True, "bundle_version": bver, "total": len(nodes)}

    def _push_init(self, bver: str) -> list:
        """전 노드 푸시용 진행 상태(_upg_state['push'])를 초기화하고 노드 목록을 반환한다."""
        nodes = list(self.nodes)
        with self._lock:
            cache = dict(self._cache)
            self._upg_state["push"] = {
                "active": True, "bundle_version": bver, "total": len(nodes),
                "done": 0, "ok": 0, "fail": 0, "started": time.time(), "finished": None,
                "nodes": [{"id": n["id"], "region": n.get("region", ""),
                           "current": (cache.get(n["id"]) or {}).get("version"),
                           "state": "pending", "reason": "", "version": None}
                          for n in nodes],
            }
        return nodes

    def _push_set(self, idx: int, **kw) -> None:
        """푸시 진행 상태에서 idx 노드를 갱신하고 done/ok/fail 카운트를 다시 센다."""
        with self._lock:
            p = self._upg_state.get("push")
            if not p or idx >= len(p["nodes"]):
                return
            p["nodes"][idx].update(kw)
            states = [x["state"] for x in p["nodes"]]
            p["done"] = sum(1 for s in states if s in ("ok", "fail"))
            p["ok"] = states.count("ok")
            p["fail"] = states.count("fail")

    def _push_loop(self, data: bytes, bver: str, nodes: list) -> list:
        """각 엣지에 번들을 POST 하고 노드별 진행 상태를 갱신한다(동기). 결과 리스트 반환."""
        results = []
        self._upg_log("③ 엣지 전파 시작 — 보내는 코드 v%s, %d대 (이 버전이 엣지보다 낮거나 같으면 거부)"
                      % (bver, len(nodes)))
        if bver and bver != "?" and bver != __version__:
            self._upg_log("   ⚠ 포탈 실행 v%s ≠ 디스크 코드 v%s — 포탈을 재시작해야 최신을 보냅니다."
                          % (__version__, bver))
        for idx, n in enumerate(nodes):
            self._push_set(idx, state="sending")
            url = n["url"].rstrip("/") + "/api/upgrade"
            try:
                req = urllib.request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/gzip")
                if n.get("token"):
                    req.add_header("X-Auth-Token", n["token"])
                resp = urllib.request.urlopen(req, timeout=120)
                j = json.loads(resp.read().decode("utf-8"))
                ok = bool(j.get("ok"))
                results.append({"id": n["id"], "ok": ok,
                                "version": j.get("version"), "reason": j.get("reason")})
                self._push_set(idx, state=("ok" if ok else "fail"),
                               version=j.get("version"), reason=j.get("reason") or "")
            except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
                detail = ""
                try:
                    detail = (json.loads(e.read().decode("utf-8")) or {}).get("reason") or ""
                except Exception:   # noqa: BLE001 — 에러 본문이 JSON 이 아니어도 무시
                    detail = ""
                if e.code == 404:
                    reason = "구버전 엣지(<1.41.0): 푸시 미지원 — SSH 업그레이드로 부트스트랩 필요"
                elif e.code == 403:
                    reason = "거부됨 — 엣지에 api_token 설정 필요"
                else:
                    reason = detail or ("HTTP %d" % e.code)
                results.append({"id": n["id"], "ok": False, "reason": reason})
                self._push_set(idx, state="fail", reason=reason)
            except Exception as e:  # noqa: BLE001
                results.append({"id": n["id"], "ok": False, "reason": str(e)})
                self._push_set(idx, state="fail", reason=str(e))
            r = results[-1]
            self._upg_log(("   • %s → ✓ v%s" % (n["id"], r.get("version") or "?")) if r.get("ok")
                          else ("   • %s → ✗ %s" % (n["id"], r.get("reason") or "실패")))
        okn = sum(1 for r in results if r.get("ok"))
        if nodes and okn == 0 and any("더 새 버전" in (r.get("reason") or "") for r in results):
            self._upg_log("⚠ 전부 '더 새 버전 아님'으로 거부 — 포탈 배포 코드 v%s 가 엣지보다 낮거나 "
                          "같습니다. 포탈을 먼저 최신으로 올린 뒤 다시 푸시하세요." % bver)
        with self._lock:
            p = self._upg_state.get("push")
            if p:
                p["active"] = False
                p["finished"] = time.time()
        self._upg_log("③ 엣지 전파 완료 — 성공 %d/%d (보낸 버전 v%s)" % (okn, len(nodes), bver))
        return results

    def push_upgrade_all_ssh(self, raw: dict) -> dict:
        """[부트스트랩] 등록된 모든 엣지에 SSH 로 접속해 업그레이드 스크립트를 실행한다.

        엣지가 구버전(/api/upgrade 없음)이어도 동작한다 — 스크립트가 HQ 에서 새 코드를 받아
        교체·재시작한다. ssh/sshpass 필요(보통 공용 계정/비번). 비번은 1회성으로만 쓴다.
        """
        from concurrent.futures import ThreadPoolExecutor
        hq_base = (raw.get("hq_base") or "").strip()
        edge_dir = (raw.get("edge_dir") or "/opt/isilon_edge").strip()
        service = (raw.get("service") or "isilon-edge").strip()
        script = build_upgrade_script(hq_base=hq_base, edge_dir=edge_dir, service=service)
        user = (raw.get("ssh_user") or "root").strip()
        password = raw.get("ssh_password") or ""
        port = raw.get("ssh_port") or 22

        def _one(n):
            host = urlparse(n["url"]).hostname or ""
            if not host:
                return {"id": n["id"], "ok": False,
                        "reason": "노드 URL 에서 host 를 못 읽음(노드 설정의 url 확인)"}
            res = self._ssh_run({"host": host, "ssh_user": user,
                                 "ssh_password": password, "ssh_port": port}, script)
            if res.get("ok"):
                return {"id": n["id"], "ok": True}
            rc = res.get("returncode")
            reason = res.get("reason") or (
                "SSH 연결/인증 실패" if rc == 255 else
                "업그레이드 스크립트 실패" if rc is not None else "실패")
            return {"id": n["id"], "ok": False, "reason": reason,
                    "detail": (res.get("output") or "").strip(), "returncode": rc}

        nodes = list(self.nodes)
        if nodes:
            with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as ex:
                results = list(ex.map(_one, nodes))
        else:
            results = []
        return {"ok": True, "hq_version": __version__, "results": results}

    def _try_begin_install(self) -> bool:
        """설치를 시작할 수 있으면(다른 설치가 진행 중이 아니면) installing 을 잡고 True.

        자동(감시폴더/인터넷)과 수동 '지금 업그레이드'가 **동시에 엣지에 푸시·재시작하는
        중복 실행**을 막는 단일 뮤텍스(원자적 test-and-set). 못 잡으면 False(이미 설치 중)."""
        with self._lock:
            if self._upg_state.get("installing"):
                return False
            self._upg_state.update(installing=True, install_started=time.time(),
                                   install_done=False, install_error=None)
            return True

    def _check_self_upgrade(self) -> None:
        """새 버전을 자가 적용 → 엣지 푸시 → 재시작 — ① 감시 폴더 ② 인터넷(GitHub).

        이미 설치 중(installing)이거나 동기화 중(_inflight)이면 건너뛴다(수동 '지금 업그레이드'
        와의 중복 실행 방지).
        """
        with self._lock:
            if self._inflight or self._upg_state.get("installing"):
                return
        code_dir = upgrademod.code_dir_of(__file__)
        # ① 로컬 감시 폴더
        wd = (self.settings.get("upgrade_watch_dir") or "").strip()
        if wd:
            found = upgrademod.find_newer_archive(wd, __version__)
            if found and self._try_begin_install():
                try:
                    res = upgrademod.upgrade_from_archive(found[0], code_dir, __version__)
                    if res.get("ok"):
                        self._apply_self_upgrade(res, "감시 폴더")   # 재시작(돌아오지 않음)
                        return
                    self._upg_log("감시 폴더 업그레이드 실패: %s" % res.get("reason"))
                finally:
                    with self._lock:
                        self._upg_state["installing"] = False
        # ② 인터넷(GitHub) 소스
        if (self.settings.get("upgrade_source") or "off").strip() == "github":
            info = upgrademod.check_remote(self.settings.get("upgrade_url") or "", __version__,
                                           token=self.settings.get("upgrade_token") or None)
            self._upg_set_check(info)
            if (info.get("available") and bool(self.settings.get("upgrade_auto"))
                    and self._try_begin_install()):
                self._upg_log("새 버전 %s 발견 — 자동 설치" % info.get("latest"))
                dest = os.path.join(self.data_dir, "upgrades")
                try:
                    res = upgrademod.upgrade_from_remote(
                        self.settings.get("upgrade_url") or "", code_dir, __version__, dest,
                        token=self.settings.get("upgrade_token") or None)
                    if res.get("ok"):
                        self._apply_self_upgrade(res, "인터넷")   # 재시작
                        return
                    self._upg_log("자동 설치 실패: %s" % res.get("reason"))
                finally:
                    with self._lock:
                        self._upg_state["installing"] = False

    def _apply_self_upgrade(self, res: dict, how: str) -> None:
        """자가 업그레이드 성공 후: 감사로그 → 엣지 전파 → 재시작(돌아오지 않음)."""
        self._upg_log("%s 자가 업그레이드 %s → %s, 엣지 전파 후 재시작" % (
            how, res.get("from"), res.get("version")))
        auditmod.record(self.data_dir, action="self_upgrade", ok=True,
                        detail="%s -> %s (%s)" % (res.get("from"), res.get("version"), how))
        try:                                  # 새 코드가 디스크에 반영됨 → 엣지에도 푸시
            self.push_upgrade_all()
        except Exception:  # noqa: BLE001
            pass
        upgrademod.restart_process()

    # --- 인터넷 자동 업그레이드 상태/조작(엣지와 동일 + 엣지 전파) ---
    def _upg_log(self, msg: str) -> None:
        with self._lock:
            self._upg_state.setdefault("log", []).append({"t": time.time(), "msg": msg})
            self._upg_state["log"] = self._upg_state["log"][-60:]

    def _upg_set_check(self, info: dict) -> None:
        with self._lock:
            self._upg_state.update({
                "last_check": info.get("checked_at"), "latest": info.get("latest"),
                "available": bool(info.get("available")), "source": info.get("source"),
                "size_bytes": info.get("size_bytes"), "check_error": info.get("error")})

    def upgrade_status(self) -> dict:
        with self._lock:
            st = dict(self._upg_state)
            nodes = list(self.nodes)
            cache = dict(self._cache)
        st["current"] = __version__
        st["bundle_version"] = agent_bundle_version()      # 엣지에 보낼 디스크 코드 버전
        st["bundle_stale"] = bool(st["bundle_version"] and st["bundle_version"] != __version__)
        st["source_mode"] = self.settings.get("upgrade_source", "off")
        st["auto"] = bool(self.settings.get("upgrade_auto"))
        st["url"] = self.settings.get("upgrade_url") or upgrademod.DEFAULT_UPGRADE_BASE
        st["url_custom"] = self.settings.get("upgrade_url") or ""
        st["token_set"] = bool(self.settings.get("upgrade_token"))   # 값은 노출 안 함(설정 여부만)
        st["check_secs"] = self.settings.get("upgrade_check_secs", 60)
        st["node_count"] = len(nodes)
        # 등록된 엣지가 HQ(현재) 버전보다 낮은지 집계 — HQ 자신만 보고 '모두 최신'이라 하던 착시 방지.
        hq = _ver_tuple(__version__)
        outdated, unknown = [], 0
        for n in nodes:
            nv = (cache.get(n["id"]) or {}).get("version")
            tv = _ver_tuple(nv)
            if tv is None:
                unknown += 1
            elif hq is not None and tv < hq:
                outdated.append({"id": n["id"], "version": nv})
        st["edges_outdated"] = len(outdated)
        st["edges_outdated_list"] = outdated[:20]
        st["edges_unknown"] = unknown
        return {"ok": True, **st}

    def upgrade_check(self) -> dict:
        info = upgrademod.check_remote(self.settings.get("upgrade_url") or "", __version__,
                                       token=self.settings.get("upgrade_token") or None)
        self._upg_set_check(info)
        if info.get("error"):
            self._upg_log("확인 오류: %s" % info["error"])
        else:
            self._upg_log("확인 — 현재 %s · 최신 %s%s" % (
                __version__, info.get("latest"),
                " · 업데이트 가능" if info.get("available") else " · 최신"))
        return {"ok": bool(info.get("ok")), **self.upgrade_status()}

    def upgrade_install(self, *, propagate: bool = True) -> dict:
        """[비동기] 최신 코드 적용 → 엣지 전파 → 재시작. 즉시 반환하고 백그라운드로 진행하며,
        진행 단계를 _upg_log 에 남긴다(업그레이드 모달이 /upgrade/status 를 폴링해 라이브 표시)."""
        if not self._try_begin_install():
            return {"ok": False, "error": "이미 설치 중입니다.", **self.upgrade_status()}
        with self._lock:
            self._upg_state["target"] = self._upg_state.get("latest")
        threading.Thread(target=self._do_upgrade_install, args=(bool(propagate),),
                         daemon=True).start()
        return {"ok": True, "started": True, **self.upgrade_status()}

    def _do_upgrade_install(self, propagate: bool) -> None:
        """업그레이드 백그라운드 작업: 내려받기 → HQ 적용 → 엣지 전파 → 재시작(돌아오지 않음)."""
        try:
            code_dir = upgrademod.code_dir_of(__file__)
            dest = os.path.join(self.data_dir, "upgrades")
            src = self.settings.get("upgrade_url") or upgrademod.DEFAULT_UPGRADE_BASE
            self._upg_log("① 최신 코드 내려받는 중… (%s)" % src)
            res = upgrademod.upgrade_from_remote(
                self.settings.get("upgrade_url") or "", code_dir, __version__, dest,
                token=self.settings.get("upgrade_token") or None)
            if not res.get("ok"):
                self._upg_log("✗ 설치 실패: %s" % res.get("reason"))
                with self._lock:
                    self._upg_state.update(installing=False, install_done=True,
                                           install_error=res.get("reason"))
                return
            self._upg_log("② HQ 코드 교체 완료: v%s → v%s" % (res.get("from"), res.get("version")))
            if propagate:
                try:                          # 새 코드를 등록된 엣지에도 전파(엣지별 로그)
                    self.push_upgrade_all()
                except Exception as e:  # noqa: BLE001
                    self._upg_log("③ 엣지 전파 중 오류: %s" % e)
            auditmod.record(self.data_dir, action="self_upgrade_manual", ok=True,
                            detail="-> %s" % res.get("version"))
            self._upg_log("④ 곧 포탈을 재시작합니다 (약 1.5초) — 새 버전 v%s 로 돌아옵니다"
                          % res.get("version"))
            with self._lock:        # 재시작 직전까지 installing 유지(1.5초 틈새 중복 설치 차단)
                self._upg_state.update(install_done=True, target=res.get("version"))
            time.sleep(1.5)
            upgrademod.restart_process()
            with self._lock:        # 재시작이 실패해 돌아온 경우에만 잠금 해제
                self._upg_state["installing"] = False
        except Exception as e:  # noqa: BLE001
            self._upg_log("✗ 설치 중 오류: %s" % e)
            with self._lock:
                self._upg_state.update(installing=False, install_done=True,
                                       install_error=str(e))

    def set_upgrade_net(self, source, url, auto, check_secs=None, token=None) -> dict:
        src = str(source or "off").strip().lower()
        self.settings["upgrade_source"] = src if src in ("off", "github") else "off"
        self.settings["upgrade_url"] = str(url or "").strip()
        if token is not None:               # None=미전송(기존 유지), ""=명시적 비움
            self.settings["upgrade_token"] = str(token or "").strip()
        self.settings["upgrade_auto"] = bool(auto)
        if check_secs is not None:
            try:
                self.settings["upgrade_check_secs"] = max(10, int(check_secs))
            except (TypeError, ValueError):
                pass
        self._save_settings()
        return self.upgrade_status()

    # --- 레지스트리 CRUD ---
    def _get(self, nid: str):
        for n in self.nodes:
            if n["id"] == nid:
                return n
        return None

    def _save(self) -> None:
        with self._lock:        # 동시 저장 직렬화(RLock — 호출 측이 이미 잡고 있어도 안전)
            save_nodes(self.data_dir, self.nodes)

    def list_nodes(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes = [_public_node(n, cache) for n in self.nodes]
        return {"ok": True, "nodes": nodes}

    def upsert_node(self, raw: dict) -> dict:
        n = sanitize_node(raw)
        if not n:
            return {"ok": False, "reason": "id 와 url 은 필수입니다."}
        with self._lock:
            cur = self._get(n["id"])
            if cur is not None:
                # 토큰이 비어 오면 기존 토큰 유지(마스킹)
                if not n["token"]:
                    n["token"] = cur.get("token", "")
                n["last_sync"] = cur.get("last_sync", 0)
                n["last_poll"] = cur.get("last_poll", 0)
                self.nodes = [n if x["id"] == n["id"] else x for x in self.nodes]
            else:
                self.nodes.append(n)
            self._save()
        return {"ok": True, "node": _public_node(n, self._cache)}

    def enroll_node(self, raw: dict) -> dict:
        """엣지가 스스로 포탈에 등록한다(자기 enroll). 공유 enroll_token 으로 인증.

        포탈에 enroll_token 이 설정돼 있으면 그 값이 일치해야 한다(여러 엣지가 공유하는
        가입 비밀). 미설정이면 포탈에 로그인 비밀번호가 걸려 있을 때는 거부(무인증 자기
        등록 금지), 비밀번호도 없으면 LAN 신뢰로 허용한다. 엣지가 보낸 api_token 을 그대로
        저장하므로 포탈 폴링이 곧바로 인증된다(토큰 불일치 401 예방). 인증 실패는 _status
        로 핸들러가 HTTP 401 을 내도록 표시한다.
        """
        want = str(self.settings.get("enroll_token") or "").strip()
        given = str(raw.get("enroll_token") or "").strip()
        if want:
            if not secrets.compare_digest(given, want):
                return {"ok": False, "reason": "enroll_token 불일치", "_status": 401}
        elif self._auth.required():
            return {"ok": False, "_status": 401,
                    "reason": "포탈에 enroll_token 을 설정하거나 로그인 비밀번호를 해제하세요."}
        url = (raw.get("url") or "").strip()
        if not url:
            return {"ok": False, "reason": "url 필요"}
        nid = ((raw.get("id") or "").strip()
               or re.sub(r"[^A-Za-z0-9_.-]+", "-", urlparse(url).hostname or url).strip("-")
               or "edge")
        path = (raw.get("path") or "").strip()
        res = self.upsert_node({
            "id": nid, "region": (raw.get("region") or "").strip(),
            "url": url, "token": (raw.get("token") or "").strip(),
            "alias_local": path, "alias_logical": path,
            "unit": "minute", "every": 30, "mode": "both", "enabled": True,
        })
        if res.get("ok"):
            res["enrolled"] = nid
        return res

    def import_nodes_csv(self, csv_text: str) -> dict:
        """CSV 로 여러 노드를 한 번에 등록(import). 행별 추가/수정/오류를 집계해 반환한다.

        헤더 행이 있으면(컬럼에 url+id 포함) 그 이름으로 매핑하고, 없으면
        id,url,region,token,alias_local,alias_logical 순서로 간주한다. 한글/별칭 헤더도 허용
        (이름→id, 주소/host→url, 지역→region, 토큰→token …). 토큰이 비면 기존 토큰을 유지한다.
        """
        import csv as _csv
        import io as _io
        if not csv_text or not csv_text.strip():
            return {"ok": False, "reason": "CSV 내용이 비어 있습니다."}
        alias = {
            "id": "id", "name": "id", "node": "id", "이름": "id", "노드": "id",
            "url": "url", "address": "url", "host": "url", "주소": "url",
            "region": "region", "지역": "region",
            "token": "token", "토큰": "token", "api_token": "token",
            "alias_local": "alias_local", "local": "alias_local", "로컬": "alias_local",
            "alias_logical": "alias_logical", "logical": "alias_logical", "논리": "alias_logical",
            "mode": "mode",
        }
        try:
            rows = [r for r in _csv.reader(_io.StringIO(csv_text))
                    if any((c or "").strip() for c in r)]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": "CSV 파싱 오류: %s" % exc}
        if not rows:
            return {"ok": False, "reason": "데이터 행이 없습니다."}
        first = [alias.get((h or "").strip().lower(), (h or "").strip().lower())
                 for h in rows[0]]
        has_header = "url" in first and "id" in first
        if has_header:
            cols, data = first, rows[1:]
        else:
            cols = ["id", "url", "region", "token", "alias_local", "alias_logical"]
            data = rows
        added = updated = 0
        errors = []
        for idx, row in enumerate(data):
            line = idx + (2 if has_header else 1)
            raw = {}
            for j, val in enumerate(row):
                if j < len(cols) and cols[j]:
                    raw[cols[j]] = (val or "").strip()
            if not raw.get("id") or not raw.get("url"):
                errors.append({"line": line, "error": "id/url 누락"})
                continue
            existed = self._get(raw["id"]) is not None
            res = self.upsert_node(raw)
            if res.get("ok"):
                if existed:
                    updated += 1
                else:
                    added += 1
            else:
                errors.append({"line": line, "error": res.get("reason", "등록 실패")})
        return {"ok": True, "added": added, "updated": updated,
                "errors": errors, "total": len(data)}

    def delete_node(self, nid: str) -> dict:
        with self._lock:
            before = len(self.nodes)
            self.nodes = [x for x in self.nodes if x["id"] != nid]
            self._cache.pop(nid, None)
            self._save()
        # 복제본도 정리(베스트 에포트)
        shutil.rmtree(os.path.join(self.replicas_dir, nid), ignore_errors=True)
        return {"ok": before != len(self.nodes)}

    def test_node(self, raw: dict) -> dict:
        """연결 테스트: url(+token) 또는 등록된 id 로 meta 를 받아 본다."""
        nid = str(raw.get("id") or "").strip()
        if nid:
            n = self._get(nid)
            if n is None:
                return {"ok": False, "reason": "등록되지 않은 노드"}
            url, token = n["url"], n.get("token")
        else:
            url = str(raw.get("url") or "").strip()
            token = raw.get("token") or None
            if not url:
                return {"ok": False, "reason": "url 필요"}
            if not re.match(r"^https?://", url):
                url = "http://" + url
        try:
            meta = _probe_meta(url, token, timeout=8.0)
            ov = meta.get("overall") or {}
            return {"ok": True, "version": meta.get("version"),
                    "hostname": meta.get("hostname"),
                    "storages": ov.get("root_count") or 0,
                    "used_bytes": ov.get("total_scanned_bytes") or 0}
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            reason = "인증 실패(토큰 확인)" if e.code == 401 else ("HTTP %d" % e.code)
            return {"ok": False, "reason": reason}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)}

    # --- 동기화(폴링 + 복제) ---
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="portal-sync",
                                            daemon=True)
            self._thread.start()
            threading.Thread(target=self._ping_history_loop, name="portal-ping-hist",
                             daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                secs = int(self.settings.get("upgrade_check_secs", 60) or 60)
                if now - self._last_upgrade_check >= max(10, secs):
                    self._last_upgrade_check = now
                    self._check_self_upgrade()
                for n in list(self.nodes):
                    if not n.get("enabled"):
                        continue
                    nid = n["id"]
                    with self._lock:
                        if nid in self._inflight:
                            continue
                    due_poll = (now - n.get("last_poll", 0)) >= self.poll_every
                    # 복제 시점은 예약 스캔과 동일한 반복주기 모델(schedule_due)로 판정.
                    # last_run 자리에 last_sync 를 넣어 마지막 복제 이후를 기준으로 한다.
                    due_rep = (n.get("mode") in ("both", "replicate") and
                               setmod.schedule_due(dict(n, last_run=n.get("last_sync", 0)), now))
                    if due_poll or due_rep:
                        with self._lock:
                            self._inflight.add(nid)
                        threading.Thread(target=self._sync_one, args=(nid, due_rep),
                                         daemon=True).start()
            except Exception:  # noqa: BLE001 — 스케줄러는 죽지 않는다
                pass
            self._stop.wait(self.tick)

    def sync_now(self, nid: Optional[str] = None) -> dict:
        targets = [nid] if nid else [n["id"] for n in self.nodes if n.get("enabled")]
        for t in targets:
            with self._lock:
                if t in self._inflight:
                    continue
                self._inflight.add(t)
            threading.Thread(target=self._sync_one, args=(t, True), daemon=True).start()
        return {"ok": True, "syncing": targets}

    def _sync_one(self, nid: str, do_rep: bool) -> None:
        try:
            n = self._get(nid)
            if n is None:
                return
            try:
                meta = _probe_meta(n["url"], n.get("token"))
                with self._lock:
                    self._cache[nid] = {
                        "online": True, "ts": time.time(),
                        "version": meta.get("version"), "hostname": meta.get("hostname"),
                        "overall": meta.get("overall") or {},
                        "isilon": meta.get("isilon") or {"configured": False},
                        "storage": meta.get("storage") or [],
                        "error": "",
                    }
                    n["last_poll"] = time.time()
                    n["last_status"] = "online"
                    n["last_error"] = ""
            except Exception as e:  # noqa: BLE001
                emsg = _poll_error_msg(e)
                with self._lock:
                    c = self._cache.get(nid, {})
                    c.update({"online": False, "error": emsg})
                    self._cache[nid] = c
                    n["last_poll"] = time.time()
                    n["last_status"] = "offline"
                    n["last_error"] = emsg
                return  # 오프라인이면 복제 생략
            if do_rep and n.get("mode") in ("both", "replicate"):
                try:
                    newest = self._replicate(n)          # 느린 네트워크 — 락 밖에서
                    with self._lock:
                        n["last_sync"] = max(float(n.get("last_sync", 0)), newest)
                        self._save()
                except Exception as e:  # noqa: BLE001
                    with self._lock:
                        n["last_error"] = "replicate: " + str(e)
        finally:
            with self._lock:
                self._inflight.discard(nid)

    def _replicate(self, n: dict) -> float:
        """완료 DB(증분) + meta.json 을 replicas/<id>/ 로 안전하게 내려받는다."""
        since = int(float(n.get("last_sync", 0) or 0))
        url = n["url"].rstrip("/") + "/api/dbexport?since=%d" % since
        dest = os.path.join(self.replicas_dir, n["id"])
        os.makedirs(os.path.join(dest, "scans"), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=self.replicas_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                resp = _http_get(url, n.get("token"), timeout=300.0)
                shutil.copyfileobj(resp, f)
            newest = float(n.get("last_sync", 0) or 0)
            tf = tarfile.open(tmp, "r:gz")
            try:
                for m in tf:
                    if not m.isfile():
                        continue
                    name = m.name.lstrip("/")
                    if name == "meta.json":
                        outp = os.path.join(dest, "meta.json")
                    elif (name.startswith("scans/") and name.endswith(".db")
                          and "/" not in name[6:] and ".." not in name):
                        outp = os.path.join(dest, "scans", os.path.basename(name))
                    else:
                        continue  # 경로 탈출 방지: 그 외 멤버는 무시
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    with open(outp, "wb") as w:
                        shutil.copyfileobj(src, w)
            finally:
                tf.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # meta 의 완료 시각으로 last_sync 전진
        try:
            with open(os.path.join(dest, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            for s in meta.get("scans", []):
                newest = max(newest, float(s.get("finished_at") or 0))
        except (OSError, ValueError):
            pass
        return newest

    # --- 글로벌 롤업 ---
    @staticmethod
    def _fs_capacity(roots_l):
        """노드의 파일시스템 사용/전체 용량. 같은 FS(같은 total)는 한 번만 센다."""
        seen = {}   # fs_total -> 최대 fs_used
        for rt in roots_l:
            ftot = int(rt.get("fs_total_bytes") or 0)
            if ftot <= 0:
                continue
            fused = int(rt.get("fs_used_bytes") or 0)
            if ftot not in seen or fused > seen[ftot]:
                seen[ftot] = fused
        ftot = sum(seen.keys())
        fused = sum(seen.values())
        pct = round(fused / ftot * 100, 1) if ftot else None
        return ftot, fused, max(0, ftot - fused), pct

    @staticmethod
    def _running_scan(roots_l):
        """진행 중(discovering/sizing) 스캔의 시작 시각·경과·단계 요약(없으면 None)."""
        act = [rt for rt in roots_l if rt.get("status") in ("discovering", "sizing")]
        if not act:
            return None
        rep = min(act, key=lambda rt: float(rt.get("started_at") or 9e18))
        started = min((float(rt.get("started_at") or 0) for rt in act
                       if rt.get("started_at")), default=0) or None
        updated = max((float(rt.get("updated_at") or 0) for rt in act), default=0) or None
        return {
            "started_at": started, "updated_at": updated,
            "phase": rep.get("phase"), "root_path": rep.get("root_path"),
            "processed_dirs": int(rep.get("processed_dirs") or 0),
            "total_dirs": int(rep.get("total_dirs") or 0),
            "count": len(act),
        }

    def overview(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes_snapshot = list(self.nodes)
        regions = {}
        total_used = 0
        total_fs_total = 0
        total_fs_used = 0
        storages = 0
        online = 0
        active = 0
        out_nodes = []
        for n in nodes_snapshot:
            c = cache.get(n["id"], {})
            ov = c.get("overall") or {}
            used = int(ov.get("total_scanned_bytes") or 0)
            st = int(ov.get("root_count") or 0)
            is_on = bool(c.get("online"))
            if is_on:
                online += 1
            active += int(ov.get("active_scans") or 0)
            total_used += used
            storages += st
            reg = n.get("region") or "(미지정)"
            r = regions.setdefault(reg, {"region": reg, "used_bytes": 0, "storages": 0})
            r["used_bytes"] += used
            r["storages"] += st
            roots_l = ov.get("roots") or []
            last_scan = max([0] + [max(rt.get("finished_at") or 0, rt.get("updated_at") or 0)
                                   for rt in roots_l])     # 그 DC 가 마지막으로 스캔한 시각
            fs_total, fs_used, fs_free, fs_pct = self._fs_capacity(roots_l)
            total_fs_total += fs_total
            total_fs_used += fs_used
            out_nodes.append({
                "id": n["id"], "region": n.get("region"), "url": n["url"],
                "enabled": n.get("enabled"), "online": is_on,
                "version": c.get("version"), "hostname": c.get("hostname"),
                "last_poll": n.get("last_poll"), "last_sync": n.get("last_sync"),
                "last_scan_at": last_scan or None,
                "last_error": n.get("last_error") or c.get("error") or "",
                "used_bytes": used, "storages": st,
                "active_scans": int(ov.get("active_scans") or 0),
                "fs_total_bytes": fs_total, "fs_used_bytes": fs_used,
                "fs_free_bytes": fs_free, "fs_used_pct": fs_pct,
                "running": self._running_scan(roots_l),
                "roots": roots_l,
                "isilon": c.get("isilon") or {"configured": False},
                "storage": c.get("storage") or [],
            })
        return {
            "ok": True,
            "version": __version__,
            "totals": {"used_bytes": total_used, "storages": storages,
                       "fs_total_bytes": total_fs_total, "fs_used_bytes": total_fs_used,
                       "nodes_online": online, "nodes_total": len(nodes_snapshot),
                       "active_scans": active},
            "regions": sorted(regions.values(), key=lambda x: -x["used_bytes"]),
            "nodes": out_nodes,
        }

    # --- 노드 응답시간(핑) ---
    def ping_nodes(self) -> dict:
        """각 노드로 HQ→노드 왕복 응답시간(ms)을 측정한다(가벼운 meta 요청 기준).

        포탈이 능동적으로 잴 수 있는 건 'HQ→각 노드' 지연이다. 엣지끼리의
        DC↔DC 메시 핑은 엣지 협조가 필요해 지원하지 않는다(여기선 미측정).
        여러 노드를 동시에(스레드풀) 측정해 전체 소요를 줄인다.
        """
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            nodes_snapshot = list(self.nodes)

        def _one(n):
            entry = {"id": n["id"], "region": n.get("region"), "url": n["url"],
                     "online": False, "latency_ms": None, "error": ""}
            if not n.get("enabled"):
                entry["error"] = "비활성"
                return entry
            t0 = time.monotonic()
            try:
                _probe_meta(n["url"], n.get("token"), timeout=8.0)
                entry["online"] = True
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            except urllib.error.HTTPError as e:   # 응답이 왔으니 도달은 됨
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                entry["online"] = True
                entry["error"] = "인증 실패" if e.code == 401 else ("HTTP %d" % e.code)
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            return entry

        if nodes_snapshot:
            with ThreadPoolExecutor(max_workers=min(16, len(nodes_snapshot))) as ex:
                results = list(ex.map(_one, nodes_snapshot))
        else:
            results = []
        return {"ok": True, "results": results, "checked_at": time.time()}

    # --- 핑 히스토리(서버측 자동 측정 + 최대 1년 저장) ---
    def _init_ping_db(self) -> None:
        try:
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS ping_samples ("
                             "ts INTEGER NOT NULL, node_id TEXT NOT NULL, "
                             "latency_ms REAL, up INTEGER NOT NULL DEFAULT 0)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping_samples(ts)")
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _record_ping(self, results: list) -> None:
        rows = [(int(time.time()), r["id"], r.get("latency_ms"), 1 if r.get("online") else 0)
                for r in (results or []) if r.get("id")]
        if not rows:
            return
        try:
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                conn.executemany(
                    "INSERT INTO ping_samples (ts,node_id,latency_ms,up) VALUES (?,?,?,?)", rows)
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _prune_ping_history(self) -> None:
        try:
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                conn.execute("DELETE FROM ping_samples WHERE ts < ?",
                             (int(time.time()) - 366 * 86400,))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def ping_history(self, range_sec: int, max_points: int = 240) -> dict:
        """기간(초) 동안의 핑 이력을 '노드별' 시계열로 돌려준다(인프라 체크 스타일).

        노드별: region, 평소(중앙값) median, ~max_points 로 다운샘플한 series[{t,ms,up}].
        점 색칠(정상/+20%/+50%)은 화면에서 median 대비로 계산한다.
        """
        import statistics
        range_sec = max(3600, int(range_sec or 86400))
        max_points = max(60, min(1000, int(max_points or 240)))
        bucket = max(60, range_sec // max_points)
        since = int(time.time()) - range_sec
        reg = {x["id"]: x.get("region") for x in list(self.nodes)}
        by_node = {}
        try:
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                cur = conn.execute(
                    "SELECT node_id, (ts/?)*? AS b, AVG(latency_ms), SUM(up), COUNT(*) "
                    "FROM ping_samples WHERE ts>=? GROUP BY node_id, b ORDER BY node_id, b",
                    (bucket, bucket, since))
                for nid, b, avg, up, n in cur.fetchall():
                    by_node.setdefault(nid, []).append(
                        {"t": int(b), "ms": round(avg, 2) if avg is not None else None,
                         "up": int(up) == int(n)})
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e), "nodes": []}
        out = []
        for nid, series in by_node.items():
            vals = [p["ms"] for p in series if p["ms"] is not None]
            med = round(statistics.median(vals), 2) if vals else None
            out.append({"id": nid, "region": reg.get(nid) or "", "median": med,
                        "series": series})
        out.sort(key=lambda x: (x["region"] or "~", x["id"]))
        return {"ok": True, "range": range_sec, "bucket": bucket, "nodes": out}

    def _ping_history_loop(self) -> None:
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                self._record_ping(self.ping_nodes().get("results", []))
                now = time.time()
                if now - last_prune > 3600:        # 1시간마다 1년치 밖 정리
                    self._prune_ping_history()
                    last_prune = now
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(60)                    # 1분 주기(가장 가는 버킷)

    # --- 원격 스캔 시작(마지막 스캔과 동일 경로) ---
    def node_scan(self, node_id: str) -> dict:
        """온라인·미스캔 노드를 '마지막 스캔과 동일한 경로'로 다시 스캔 시작한다.

        마지막 루트는 캐시(overall.roots)에서 scan_id 가 가장 큰(=최근) 루트의 root_path.
        엣지가 로그인 비밀번호로 보호돼 있으면(op_required) 포탈엔 그 토큰이 없어 시작할 수 없다
        → 사유를 돌려준다(HTTP 200, ok=False). 엔진/백엔드는 엣지 기본 설정을 따른다.
        """
        with self._lock:
            node = next((dict(n) for n in self.nodes if n["id"] == node_id), None)
            cache = dict(self._cache.get(node_id, {})) if node else {}
        if not node:
            return {"ok": False, "reason": "노드를 찾을 수 없습니다."}
        if not node.get("enabled", True):
            return {"ok": False, "reason": "비활성 노드입니다."}
        roots = (cache.get("overall") or {}).get("roots") or []
        if not roots:
            return {"ok": False, "reason": "이 노드에 이전 스캔 기록이 없습니다(먼저 한 번 스캔)."}
        if any(r.get("status") in ("discovering", "sizing") for r in roots):
            return {"ok": False, "reason": "이미 스캔이 진행 중입니다."}
        last = max(roots, key=lambda r: int(r.get("scan_id") or 0))
        path = (last.get("root_path") or "").strip()
        if not path:
            return {"ok": False, "reason": "마지막 스캔 경로를 알 수 없습니다."}
        url = node["url"].rstrip("/") + "/api/scan/start"
        req = urllib.request.Request(
            url, data=json.dumps({"path": path}).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        if node.get("token"):
            req.add_header("X-Auth-Token", node["token"])
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            j = json.loads(resp.read().decode("utf-8"))
            if j.get("ok"):
                return {"ok": True, "node": node_id, "path": path,
                        "scan_id": j.get("scan_id")}
            return {"ok": False, "path": path,
                    "reason": j.get("reason") or "엣지가 스캔 시작을 거부했습니다."}
        except urllib.error.HTTPError as e:
            reason = (
                "엣지에 로그인 비밀번호가 설정돼 있어 포탈에서 직접 시작할 수 없습니다 — 엣지 화면에서 시작하세요."
                if e.code == 401 else
                "엣지가 웹 스캔을 비활성화했거나 거부했습니다." if e.code == 403 else
                "엣지에 스캔 시작 API가 없습니다(구버전 엣지)." if e.code == 404 else
                "HTTP %d" % e.code)
            return {"ok": False, "reason": reason, "path": path}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e), "path": path}

    # --- 원격 자동 구성 ---
    def provision_plan(self, raw: dict) -> dict:
        """IP/포트/경로 등으로 엣지 설치 스크립트를 생성하고 노드를 자동 등록한다(SSH 없음).

        같은 노드(같은 이름 id, 또는 같은 url=같은 서버)가 이미 있으면 overwrite(기본 True)일 때
        **전체 덮어쓰기**한다(같은 서버가 다른 이름으로 있던 중복 항목도 제거). API 토큰을 비우면
        — 기존 노드가 있으면 그 토큰을 **재사용**(엣지 페어링 유지), 없으면 새로 생성한다.
        overwrite=False 인데 이미 있으면 등록하지 않고 사유를 돌려준다.
        """
        host = (raw.get("host") or "").strip()
        if not host:
            return {"ok": False, "reason": "host(IP/주소) 필요"}
        try:
            port = int(raw.get("port") or 8765)
        except (TypeError, ValueError):
            port = 8765
        path = (raw.get("path") or "/mnt/hadoop").strip()
        name = ((raw.get("name") or "").strip()
                or re.sub(r"[^A-Za-z0-9_.-]+", "-", host).strip("-") or "edge")
        region = (raw.get("region") or "").strip()
        install = "nohup" if (raw.get("install") == "nohup") else "systemd"
        hq_base = (raw.get("hq_base") or "").strip()
        edge_url = "http://%s:%d" % (host, port)
        overwrite = raw.get("overwrite", True)
        if isinstance(overwrite, str):
            overwrite = overwrite.strip().lower() not in ("0", "false", "no", "off", "")
        # 기존 노드: 같은 이름(id) 또는 같은 url(같은 서버)
        with self._lock:
            found = next((n for n in self.nodes
                          if n["id"] == name or (n.get("url") or "").rstrip("/") == edge_url), None)
            existing = dict(found) if found else None
        if existing and not overwrite:
            return {"ok": False,
                    "reason": "이미 등록된 노드입니다('%s'). '기존 노드 덮어쓰기'를 켜세요." % existing["id"]}
        # 토큰: 입력값 우선 → 비면 기존 토큰 재사용(페어링 유지) → 그것도 없으면 새로 생성
        token = (raw.get("token") or "").strip()
        token_reused = False
        if not token:
            if existing and existing.get("token"):
                token, token_reused = existing["token"], True
            else:
                token = secrets.token_hex(16)
        script = build_provision_script(host=host, port=port, token=token, path=path,
                                        hq_base=hq_base, install=install)
        replaced_prev = None
        if existing and existing["id"] != name:   # 같은 서버가 다른 이름으로 있었으면 옛 항목 제거
            replaced_prev = existing["id"]
            self.delete_node(existing["id"])
        node_res = self.upsert_node({
            "id": name, "region": region, "url": edge_url, "token": token,
            "alias_local": path, "alias_logical": path,
            "unit": "minute", "every": 30, "mode": "both", "enabled": True,
        })
        return {"ok": True, "script": script, "token": token, "edge_url": edge_url,
                "install": install, "registered": bool(node_res.get("ok")),
                "overwritten": bool(existing), "replaced_prev": replaced_prev,
                "token_reused": token_reused, "node": node_res.get("node")}

    def _ssh_run(self, raw: dict, script: str) -> dict:
        """SSH(키 인증 또는 sshpass 비밀번호)로 원격에 script 를 bash -s 로 실행한다.

        비밀번호는 보관하지 않고 1회성으로만 쓴다. provision/upgrade 의 SSH 실행에 공용.
        """
        import subprocess
        host = (raw.get("host") or "").strip()
        if not host:
            return {"ok": False, "reason": "host(IP/주소) 필요"}
        user = (raw.get("ssh_user") or "root").strip()
        password = raw.get("ssh_password") or ""
        try:
            ssh_port = int(raw.get("ssh_port") or 22)
        except (TypeError, ValueError):
            ssh_port = 22
        ssh = shutil.which("ssh")
        if not ssh:
            return {"ok": False,
                    "reason": "ssh 바이너리가 없습니다 — 생성된 스크립트를 엣지에서 수동 실행하세요."}
        cmd = []
        if password:
            sshpass = shutil.which("sshpass")
            if not sshpass:
                return {"ok": False,
                        "reason": "비밀번호 인증엔 sshpass 가 필요합니다(미설치). 키 인증을 쓰거나 스크립트를 수동 실행하세요."}
            cmd = [sshpass, "-p", password]
        cmd += [ssh, "-p", str(ssh_port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "%s@%s" % (user, host), "bash -s"]
        try:
            p = subprocess.run(cmd, input=script.encode(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=600)
            out = p.stdout.decode("utf-8", "replace")[-4000:]
            return {"ok": (p.returncode == 0), "returncode": p.returncode, "output": out}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": "SSH 실행 실패: %s" % e}

    def provision_ssh(self, raw: dict) -> dict:
        """[옵션] 원격에 SSH 로 접속해 구성 스크립트를 실행한다(키 또는 sshpass).

        무의존 원칙상 기본은 'A(스크립트 생성)'이며 이건 옵션이다.
        """
        plan = self.provision_plan(raw)
        if not plan.get("ok"):
            return plan
        return dict(plan, **self._ssh_run(raw, plan["script"]))

    # --- 원격 버전 업그레이드 ---
    def upgrade_plan(self, raw: dict) -> dict:
        """등록된 엣지(또는 host)를 HQ 최신 코드로 올리는 업그레이드 스크립트를 만든다.

        엣지에서 복붙 실행하면 HQ 포탈에서 최신 코드(agent-bundle)를 받아 코드 디렉터리에
        덮어쓰고 서비스를 재시작한다. 데이터(스캔 DB·설정)는 data-dir 라 영향받지 않는다.
        """
        nid = str(raw.get("id") or "").strip()
        edge_dir = (raw.get("edge_dir") or "/opt/isilon_edge").strip()
        service = (raw.get("service") or "isilon-edge").strip()
        hq_base = (raw.get("hq_base") or "").strip()
        script = build_upgrade_script(hq_base=hq_base, edge_dir=edge_dir, service=service)
        node_version = None
        if nid:
            with self._lock:
                node_version = (self._cache.get(nid) or {}).get("version")
        return {"ok": True, "script": script, "hq_version": __version__,
                "node_version": node_version, "edge_dir": edge_dir, "service": service}

    def upgrade_ssh(self, raw: dict) -> dict:
        """[옵션] SSH 로 엣지에 접속해 업그레이드 스크립트를 실행한다."""
        plan = self.upgrade_plan(raw)
        if not plan.get("ok"):
            return plan
        return dict(plan, **self._ssh_run(raw, plan["script"]))

    # --- 경로 비교(Cross-DC) — 복제본 DB 를 경로로 조인 ---
    @staticmethod
    def _to_local(node: dict, logical: str) -> str:
        """공통 논리 경로를 이 노드의 로컬 경로로 변환한다(별칭이 있으면).

        alias_logical 접두어를 alias_local 로 바꾼다. 별칭이 없으면 그대로(절대경로 일치).
        """
        al = (node.get("alias_local") or "").rstrip("/")
        lo = (node.get("alias_logical") or "").rstrip("/")
        if al and lo and (logical == lo or logical.startswith(lo + "/")):
            return al + logical[len(lo):]
        return logical

    def _replica_db_for(self, node_id: str, path: str):
        """노드 복제본에서 path 를 포함하는(root 가 prefix) 최신 완료 스캔 DB 경로."""
        rep = os.path.join(self.replicas_dir, node_id)
        try:
            with open(os.path.join(rep, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return None
        best = None
        for s in meta.get("scans", []):
            root = (s.get("root_path") or "").rstrip("/")
            if path == root or path.startswith(root + "/"):
                if best is None or float(s.get("finished_at") or 0) > float(best.get("finished_at") or 0):
                    best = s
        if not best:
            return None
        dbf = best.get("db_filename") or os.path.basename(best.get("db_path") or "")
        dbp = os.path.join(rep, "scans", dbf)
        return dbp if (dbf and os.path.exists(dbp)) else None

    def compare_path(self, path: str) -> dict:
        """같은 경로의 노드별 용량/파일 수를 모아 비교한다(복제본 기준)."""
        path = os.path.abspath(path)
        rows = []
        for n in self.nodes:
            entry = {"id": n["id"], "region": n.get("region"), "found": False,
                     "total_bytes": 0, "own_bytes": 0, "total_files": 0, "subdir_count": 0}
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            dbp = self._replica_db_for(n["id"], local)
            if dbp:
                try:
                    c = dbmod.connect(dbp)
                    rid = dbmod.latest_run_id(c)
                    r = c.execute(
                        "SELECT total_bytes, own_bytes, total_files, subdir_count "
                        "FROM directories WHERE run_id=? AND path=?", (rid, local)).fetchone()
                    c.close()
                    if r is not None:
                        sz = r["total_bytes"] if r["total_bytes"] else r["own_bytes"]
                        entry.update({"found": True, "total_bytes": sz,
                                      "own_bytes": r["own_bytes"], "total_files": r["total_files"],
                                      "subdir_count": r["subdir_count"]})
                except Exception:  # noqa: BLE001
                    pass
            rows.append(entry)
        return {"ok": True, "path": path, "rows": rows}

    def compare_matrix(self, path: str) -> dict:
        """path 직속 자식들의 노드별 용량 매트릭스(누락·드리프트 비교)."""
        path = os.path.abspath(path)
        node_ids = [n["id"] for n in self.nodes]
        children = {}
        for n in self.nodes:
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            dbp = self._replica_db_for(n["id"], local)
            if not dbp:
                continue
            try:
                c = dbmod.connect(dbp)
                rid = dbmod.latest_run_id(c)
                prow = c.execute("SELECT depth FROM directories WHERE run_id=? AND path=?",
                                 (rid, local)).fetchone()
                if prow is None:
                    c.close()
                    continue
                like = (local.replace("\\", "\\\\").replace("%", "\\%")
                        .replace("_", "\\_")).rstrip("/") + "/%"
                kids = c.execute(
                    "SELECT name, total_bytes, own_bytes, subdir_count FROM directories "
                    "WHERE run_id=? AND depth=? AND path LIKE ? ESCAPE '\\'",
                    (rid, int(prow["depth"]) + 1, like)).fetchall()
                c.close()
                for k in kids:
                    e = children.setdefault(k["name"], {"name": k["name"], "subdir_count": 0, "vals": {}})
                    e["vals"][n["id"]] = int(k["total_bytes"] or k["own_bytes"] or 0)
                    e["subdir_count"] = max(e["subdir_count"], int(k["subdir_count"] or 0))
            except Exception:  # noqa: BLE001
                pass
        out = []
        for e in children.values():
            e["sum"] = sum(e["vals"].values())
            out.append(e)
        out.sort(key=lambda x: -x["sum"])
        return {"ok": True, "path": path, "nodes": node_ids, "children": out}


# ------------------------------------------------------------------ HTTP 핸들러
class PortalHandler(BaseHTTPRequestHandler):
    data_dir = ""
    controller = None  # PortalController

    def log_message(self, fmt, *args):  # noqa: D401 — 조용히
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _send_html(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "portal.html not found")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(PORTAL_HTML)
            return
        c = self.controller
        try:
            if path == "/api/portal/auth":
                self._send_json(c.auth_status())
                return
            if path == "/api/portal/settings":
                self._send_json(c.upgrade_config())
                return
            if path == "/api/portal/upgrade/status":
                self._send_json(c.upgrade_status())
                return
            if path == "/api/portal/ping-history":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    rng = int(qs.get("range", ["86400"])[0])
                except (TypeError, ValueError):
                    rng = 86400
                self._send_json(c.ping_history(rng))
                return
            if path == "/api/portal/browse":
                # HQ(포탈) 서버의 로컬 디렉터리 탐색 — 감시 폴더 등 경로 선택용.
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                self._send_json(browse_dir(qp or "/"))
                return
            if path == "/api/portal/nodes":
                self._send_json(c.list_nodes())
                return
            if path == "/api/portal/overview":
                self._send_json(c.overview())
                return
            if path == "/api/portal/ping":
                self._send_json(c.ping_nodes())
                return
            if path == "/api/portal/agent-bundle":
                data = agent_bundle_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="isilon_usage_agent.tar.gz"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/portal/release/info":
                self._send_json(dict({"ok": True}, **c.release_info()))
                return
            if path == "/api/portal/release":   # release_dir 의 최신 패키지를 엣지에 내려줌
                rel = c.settings.get("release_dir") or "/opt/isilon_release"
                arc = newest_release_archive(rel)
                if not arc:
                    self._send_json({"ok": False, "reason":
                                     "release 패키지 없음 — %s 에 isilon_usage-*.tar.gz 를 두세요" % rel},
                                    status=404)
                    return
                try:
                    with open(arc, "rb") as fh:
                        data = fh.read()
                except OSError as exc:
                    self._send_json({"ok": False, "reason": "읽기 실패: %s" % exc}, status=500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % os.path.basename(arc))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/portal/compare":
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                if not qp:
                    self._send_json({"ok": False, "reason": "path 필요"}, status=400)
                else:
                    self._send_json(c.compare_path(qp))
                return
            if path == "/api/portal/matrix":
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                if not qp:
                    self._send_json({"ok": False, "reason": "path 필요"}, status=400)
                else:
                    self._send_json(c.compare_matrix(qp))
                return
            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def _audit(self, action: str, ok: bool, detail: str = "") -> None:
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:  # noqa: BLE001
            ip = ""
        auditmod.record(self.data_dir, action=action, ok=ok, ip=ip, detail=detail)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json_body()
        c = self.controller
        try:
            if path == "/api/portal/login":
                res = c.login(body.get("password") or "")
                self._audit("login", res.get("ok"),
                            "" if res.get("ok") else (res.get("reason") or ""))
                self._send_json(res)
                return
            # 비밀번호 설정/변경/해제 — 이미 설정돼 있으면 로그인 필요(첫 설정은 허용)
            if path == "/api/portal/password":
                if c.auth_required() and not c.token_valid(self.headers.get("X-Op-Token")):
                    self._audit("password", False, "locked")
                    self._send_json({"ok": False, "reason": "locked", "op_required": True},
                                    status=401)
                    return
                res = c.set_password(body.get("password") or "", bool(body.get("encrypt")))
                self._audit("password", res.get("ok"))
                self._send_json(res)
                return
            # 엣지 자기등록(enroll) — op 로그인 토큰이 아니라 공유 enroll_token 으로 인증하므로
            # 일반 인증 게이트 '앞'에 둔다(엣지는 포탈 로그인 토큰을 갖지 않는다).
            if path == "/api/portal/enroll":
                res = c.enroll_node(body)
                st = res.pop("_status", None) or (200 if res.get("ok") else 400)
                self._audit("enroll", res.get("ok"),
                            "" if res.get("ok") else (res.get("reason") or ""))
                self._send_json(res, status=st)
                return
            # 그 외 변경 작업(노드 등록·삭제·동기화·원격 구성)은 로그인 필요
            if c.auth_required() and not c.token_valid(self.headers.get("X-Op-Token")):
                self._audit(path, False, "locked")
                self._send_json({"ok": False, "reason": "locked", "op_required": True,
                                 "error": "로그인 후 작업하세요."}, status=401)
                return
            if path == "/api/portal/audit":      # 감사 로그 조회(로그인 필요)
                self._send_json({"ok": True, "events": auditmod.tail(self.data_dir, 300)})
                return
            self._audit(path, True)              # 인증 통과한 변경 작업 기록
            if path == "/api/portal/settings":
                res = c.set_upgrade_watch(body.get("upgrade_watch_dir") or "",
                                          body.get("upgrade_check_secs"))
                if "enroll_token" in body:
                    res.update(c.set_enroll_token(body.get("enroll_token") or ""))
                if "release_dir" in body:
                    res.update(c.set_release_dir(body.get("release_dir") or ""))
                self._send_json(res)
                return
            if path == "/api/portal/upgrade/net":     # 인터넷 자동 업그레이드 설정
                self._send_json(c.set_upgrade_net(body.get("upgrade_source"),
                                                  body.get("upgrade_url"),
                                                  body.get("upgrade_auto"),
                                                  body.get("upgrade_check_secs"),
                                                  token=body.get("upgrade_token")))
                return
            if path == "/api/portal/upgrade/check":   # 지금 인터넷에서 최신 확인
                self._send_json(c.upgrade_check())
                return
            if path == "/api/portal/upgrade/install":  # 지금 설치 + 엣지 전파 + 재시작
                self._send_json(c.upgrade_install(propagate=bool(body.get("propagate", True))))
                return
            if path == "/api/portal/upgrade-all":
                self._send_json(c.start_push_all())     # 백그라운드 푸시 — 모달이 진행 폴링
                return
            if path == "/api/portal/upgrade-all/ssh":
                self._send_json(c.push_upgrade_all_ssh(body))
                return
            if path == "/api/portal/nodes":
                self._send_json(c.upsert_node(body))
                return
            if path == "/api/portal/nodes/import":
                self._send_json(c.import_nodes_csv(body.get("csv") or ""))
                return
            if path == "/api/portal/nodes/delete":
                self._send_json(c.delete_node(str(body.get("id") or "")))
                return
            if path == "/api/portal/test":
                self._send_json(c.test_node(body))
                return
            if path == "/api/portal/sync":
                self._send_json(c.sync_now(body.get("id") or None))
                return
            if path == "/api/portal/node-scan":
                self._send_json(c.node_scan(str(body.get("id") or "")))
                return
            if path == "/api/portal/provision":
                self._send_json(c.provision_plan(body))
                return
            if path == "/api/portal/provision/ssh":
                self._send_json(c.provision_ssh(body))
                return
            if path == "/api/portal/upgrade":
                self._send_json(c.upgrade_plan(body))
                return
            if path == "/api/portal/upgrade/ssh":
                self._send_json(c.upgrade_ssh(body))
                return
            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def serve_portal(data_dir: str, host: str = "0.0.0.0", port: int = 8800):
    """글로벌 포탈 HTTP 서버를 만들어 반환한다(호출 측에서 serve_forever)."""
    data_dir = os.path.abspath(data_dir)
    mgrmod.migrate_legacy_config(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    controller = PortalController(data_dir)
    controller.start()
    handler = type("BoundPortalHandler", (PortalHandler,),
                   {"data_dir": data_dir, "controller": controller})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller
    return httpd

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
import hmac
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
from . import ask as askmod
from . import netutil
from .server import ThreadingHTTPServer, browse_dir  # 3.6 폴백 포함 재사용
from .server import read_json_body, _has_nul, _json_bytes, _truthy   # 요청 본문 검증·응답 인코딩(엣지와 같은 규칙)
from .server import reset_op_password_file      # 임시 비밀번호(엣지와 같은 규칙·같은 함수)

# SSH 실행 입력 검증(옵션 주입 차단): 사용자는 POSIX 계정 이름, 호스트는 IP/호스트명/[IPv6] 만.
_SSH_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,63}$")
_SSH_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*)$")


HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_HTML = os.path.join(HERE, "portal.html")

# 폴링(연결+요약)은 자주, 복제(완료 DB 내려받기)는 노드별 주기마다.
# 둘은 서로 막지 않는다 — 큰 DB 복제(수 분~수 시간)가 도는 동안에도 폴링은 계속 돈다.
POLL_EVERY = 15.0       # 초
SCHED_TICK = 5.0        # 초
FAR_FUTURE = 9999999999  # since 가 미래면 meta.json 만 받아 가볍게 연결/요약 확인
# 폴링에서 '새로 완료된 스캔'을 보면 복제 주기를 기다리지 않고 바로 복제한다.
# 단 직전 복제 시도 후 REP_RETRY_SECS 는 쉬고, 실패가 이어지면 2배씩(최대 REP_RETRY_MAX) 늘린다.
REP_RETRY_SECS = 60.0
REP_RETRY_MAX = 1800.0
_TERMINAL = ("done", "paused", "error")   # 엣지 _export_dbs 가 복제로 보내는 상태(완료로 간주)


# --------------------------------------------------------------- 노드 레지스트리
def nodes_path(data_dir: str) -> str:
    return os.path.join(data_dir, "portal_nodes.json")


def portal_settings_path(data_dir: str) -> str:
    return os.path.join(data_dir, "portal_settings.json")


def ping_history_path(data_dir: str) -> str:
    return os.path.join(data_dir, "ping_history.db")


def throughput_history_path(data_dir: str) -> str:
    return os.path.join(data_dir, "throughput_history.db")


# 인증이 필요한 포탈 POST — 엣지(OP_PROTECTED_POSTS)와 같은 정책: '설정'(노드 등록·수정·삭제·가져오기·토큰,
# 연결 테스트, 포탈 설정·백업·감사 로그)과 되돌릴 수 없는/원격 구성 작업(업그레이드 설치·전 노드 푸시·SSH 설치·
# 엣지 비밀번호 푸시). 그 외(동기화·원격 스캔 시작/중지·업그레이드 확인)는 인증 없이 허용.
PORTAL_PROTECTED_POSTS = frozenset({
    "/api/portal/audit", "/api/portal/settings",
    "/api/portal/backup/save", "/api/portal/backup/config", "/api/portal/backup/download",
    "/api/portal/nodes", "/api/portal/nodes/import", "/api/portal/nodes/delete", "/api/portal/nodes/set-token",
    "/api/portal/test", "/api/portal/node-password",
    "/api/portal/provision", "/api/portal/provision/ssh",
    "/api/portal/upgrade", "/api/portal/upgrade/ssh", "/api/portal/upgrade/net", "/api/portal/upgrade/install",
    "/api/portal/upgrade-all", "/api/portal/upgrade-all/ssh",
})
OP_RUNTIME_KEYS = ("op_password", "op_password_temp", "op_info_path")


def _non_str_field(raw, keys):
    """raw 에서 값이 있는데 문자열이 아닌 첫 키(없으면 None) — .strip() AttributeError 로 500 이 나던 것 방지."""
    if not isinstance(raw, dict):
        return "(body)"
    return next((k for k in keys if raw.get(k) is not None and not isinstance(raw.get(k), str)), None)


def _audit_target(body) -> str:
    """감사 로그용 대상 요약 — 노드 id·이름·경로 같은 '무엇에 대해' 만(토큰·비밀번호 등 비밀값은 제외)."""
    if not isinstance(body, dict):
        return ""
    out = []
    for k in ("id", "ids", "node", "nodes", "name", "path", "dir", "region", "version"):
        v = body.get(k)
        if v in (None, "", []):
            continue
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in list(v)[:20]) + ("…" if len(v) > 20 else "")
        out.append("%s=%s" % (k, str(v)[:200]))
    return " ".join(out)


def safe_node_id(nid) -> bool:
    """노드 id 가 복제본 폴더 이름으로 안전한지(경로 구분자·'.'/'..'·NUL 금지).

    id 는 replicas/<id> 폴더 이름으로 쓰인다 — "..", "a/b" 같은 값은 rmtree·복제 쓰기가
    replicas 밖(포탈 data_dir 전체)으로 탈출한다. 한글 등 일반 문자는 허용(기존 노드 호환).
    """
    nid = str(nid or "")
    if not nid or nid in (".", "..") or any(c in nid for c in ("/", "\\", "\x00")):
        return False
    # 제어 문자·서로게이트(파일 이름·JSON 저장에서 깨짐)도 거부 — 예전엔 노드 목록 저장이 영구히 실패했다(2026-11 퍼징)
    return not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in nid)


def sanitize_node(raw: dict) -> Optional[dict]:
    """노드 1건을 보정한다(필수: id, url). 잘못되면 None."""
    if not isinstance(raw, dict):
        return None
    nid = str(raw.get("id") or "").strip()
    url = str(raw.get("url") or "").strip().rstrip("/")
    if not nid or not url or not safe_node_id(nid):
        return None
    if not re.match(r"^https?://", url):
        url = "http://" + url
    try:
        interval = max(1, int(raw.get("interval_minutes", 30)))
    except (TypeError, ValueError, OverflowError):
        interval = 30
    mode = raw.get("mode", "both")
    if mode not in ("both", "poll", "replicate"):
        mode = "both"

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError, OverflowError):
            return 0.0

    # 복제 주기: 예약 스캔과 같은 '시작 + 반복주기' 모델(분/시간/일/주/개월).
    # 구버전(interval_minutes 만 있던) 노드는 unit=minute, every=interval_minutes 로 호환.
    unit = raw.get("unit")
    if unit not in ("minute", "hour", "day", "week", "month"):
        unit = "minute"
    if raw.get("every") is not None:
        try:
            every = max(1, int(raw.get("every")))
        except (TypeError, ValueError, OverflowError):
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
        except (TypeError, ValueError, OverflowError):
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
        "enabled": _truthy(raw.get("enabled", True)),   # CSV 의 "false"/"0" 도 꺼짐(bool("false") 는 참)
        "last_poll": _f(raw.get("last_poll")),
        "last_ok": _f(raw.get("last_ok")),     # 마지막 '성공한' 응답 시각(last_poll 은 실패한 시도도 포함)
        "last_sync": _f(raw.get("last_sync")),
        "last_status": str(raw.get("last_status") or "unknown"),
        "last_error": str(raw.get("last_error") or ""),
    }


def load_nodes(data_dir: str) -> list:
    """노드 목록을 읽는다. 본 파일이 깨졌으면(잘림·디스크 가득) 직전 정상본(.bak)으로 — 예전엔 빈 목록이 되고
    다음 저장이 그 빈 목록으로 덮어써 모든 노드·토큰이 사라졌다(2026-11 리뷰). 깨진 본은 .broken 으로 보관."""
    path = nodes_path(data_dir)
    raw = None
    for cand in (path, path + ".bak"):
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            break
        except (OSError, ValueError):
            if cand == path and os.path.exists(path):
                try:
                    shutil.copyfile(path, path + ".broken")
                    os.chmod(path + ".broken", 0o600)
                except OSError:
                    pass
            continue
    if raw is None:
        return []
    out = []
    for it in (raw.get("nodes") if isinstance(raw, dict) else raw) or []:
        n = sanitize_node(it)
        if n:
            out.append(n)
    return out


def save_nodes(data_dir: str, nodes: list) -> None:
    """노드 레지스트리를 원자적으로 저장한다 — 설정과 같은 공용 저장(고유 임시 파일·fsync·직전 정상본 .bak·0600).
    노드 스냅샷을 떠 직렬화 중 변경 영향을 피한다(api_token 이 평문이라 소유자만 읽게)."""
    setmod.write_json_atomic(nodes_path(data_dir), {"nodes": [dict(n) for n in nodes]}, mode=0o600)


_OV_NUM_KEYS = ("total_capacity_bytes", "total_scanned_bytes", "root_count", "active_scans")
_ROOT_NUM_KEYS = ("capacity_bytes", "scanned_bytes", "fs_total_bytes", "fs_used_bytes", "fs_free_bytes",
                  "total_files", "finished_at", "updated_at", "started_at", "processed_dirs",
                  "total_dirs", "discovered_dirs", "files_per_sec", "bytes_per_sec", "scan_id",
                  "capacity_scan_id", "eta_seconds", "eta_at")


def _num(v):
    """숫자로 쓸 수 있으면 int/float, 아니면 None(NaN·무한대·문자열·bool 거부)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return int(f) if f.is_integer() and abs(f) < 2 ** 63 else f


def _sanitize_overall(ov) -> dict:
    """엣지가 보낸 overall 을 포탈 집계가 믿고 쓸 수 있는 모양으로 보정한다.

    엣지 한 대의 이상한 값(문자열 숫자·null 루트·dict 가 아닌 항목)이 overview/usage/처리량
    계산 전체를 예외로 멈춰 포탈 대시보드가 통째로 비던 문제(2026-11 리뷰)."""
    if not isinstance(ov, dict):
        return {}
    out = dict(ov)
    for k in _OV_NUM_KEYS:
        if k in out:
            out[k] = _num(out[k])
    roots = []
    for r in (ov.get("roots") if isinstance(ov.get("roots"), list) else []):
        if not isinstance(r, dict):
            continue
        r = dict(r)
        for k in _ROOT_NUM_KEYS:
            if k in r:
                r[k] = _num(r[k])
        for k in ("root_path", "status", "phase", "fs_source", "eta_basis", "nested_in"):
            if k in r and r[k] is not None and not isinstance(r[k], str):
                r[k] = str(r[k])
        roots.append(r)
    out["roots"] = roots
    return out


def _ov_capacity(ov: dict) -> int:
    """엣지 overall 의 조사 용량 — 루트별 마지막 완료 스캔 합(total_capacity_bytes). 구버전 엣지는 예전 값."""
    v = ov.get("total_capacity_bytes")
    if v is None:
        v = ov.get("total_scanned_bytes")
    try:
        return int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _root_capacity(r: dict) -> int:
    v = r.get("capacity_bytes")
    if v is None:
        v = r.get("scanned_bytes")
    try:
        return int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _public_node(n: dict, cache: dict, rep_err: Optional[dict] = None) -> dict:
    """화면용 노드(토큰 마스킹 + 실시간 캐시 상태 병합). rep_err: id -> 마지막 복제 오류."""
    c = cache.get(n["id"], {})
    ov = c.get("overall") or {}
    out = dict(n)
    out["token"] = ""
    out["token_set"] = bool(n.get("token"))
    out["online"] = bool(c.get("online")) and bool(n.get("enabled", True))   # 비활성 노드의 캐시는 옛 값
    out["version"] = c.get("version")
    out["hostname"] = c.get("hostname")
    out["error"] = n.get("last_error") or c.get("error") or (rep_err or {}).get(n["id"], "")
    out["active_scans"] = int(ov.get("active_scans") or 0) if out["online"] else 0   # 지금 스캔 중인 개수(끊긴 노드는 옛 값이라 0)
    out["scanning"] = out["active_scans"] > 0
    out["used_bytes"] = _ov_capacity(ov)
    return out


# ------------------------------------------------------------------ 네트워크
def _http_get(url: str, token: Optional[str] = None, timeout: float = 8.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Auth-Token", token)
    return netutil.urlopen(req, timeout=timeout)


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
            if name.endswith((".py", ".html", ".js", ".css")):   # .js: 영어 토글, .css: 공통 테마(theme.css)
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


def _staged_install_sh(target_var: str) -> str:
    """HQ 의 agent-bundle 을 ${target_var} 에 '안전하게' 까는 bash 조각(업그레이드·프로비저닝 공용 — 단일 소스).

    받은 묶음을 파일로 받아 gzip 검사 → 임시 폴더에 풀어 버전 확인(다운그레이드 방지, ALLOW_DOWNGRADE=1 로 허용)
    → 기존 코드 백업 후 교체 → __pycache__ 제거 → 핵심 모듈 불러오기 확인(실패 시 백업으로 되돌림).
    앞쪽에서 HQ·OLDV 가 정해져 있어야 한다. 백업은 최근 3개만 남긴다."""
    return (
        "# 받은 묶음을 먼저 파일로 받아 검사 → 임시 폴더에 풀어 버전 확인 → 백업 후 교체 → 불러오기 확인(실패 시 되돌림).\n"
        "# 예전엔 받는 즉시 운영 코드 폴더에 풀어, 잘린 묶음이면 반쯤 바뀐 코드가 남았고 HQ 가 옛 버전이면\n"
        "# 엣지를 다운그레이드했다(포탈 푸시 경로는 막는데 SSH 경로는 안 막았다 — 2026-11 리뷰).\n"
        'TMPF=$(mktemp); STAGE=$(mktemp -d)\n'
        'trap \'rm -f "$TMPF"; rm -rf "$STAGE"\' EXIT\n'
        'curl -fsSL "$HQ/api/portal/agent-bundle" -o "$TMPF"\n'
        'gzip -t "$TMPF" || { echo "  ERROR: 받은 묶음이 손상/잘림 — 중단(기존 코드 그대로)"; exit 4; }\n'
        'tar -xzf "$TMPF" -C "$STAGE"\n'
        '[ -f "$STAGE/isilon_usage/__init__.py" ] || { echo "  ERROR: 묶음에 isilon_usage 가 없음 — 중단"; exit 4; }\n'
        'NEWV=$(cd "$STAGE" && python3 -m isilon_usage --version 2>/dev/null || echo "?")\n'
        'if [ "${ALLOW_DOWNGRADE:-0}" != "1" ] && ! python3 - "$OLDV" "$NEWV" <<\'PY\'\n'
        'import re, sys\n'
        'def v(s):\n'
        '    m = re.search(r"(\\d+)\\.(\\d+)\\.(\\d+)", s or "")\n'
        '    return tuple(int(x) for x in m.groups()) if m else None\n'
        'o, n = v(sys.argv[1]), v(sys.argv[2])\n'
        'sys.exit(0 if (n is not None and (o is None or n >= o)) else 3)\n'
        'PY\n'
        'then echo "  중단: 받은 코드($NEWV)가 현재($OLDV)보다 낮거나 버전을 읽을 수 없음 — 다운그레이드 방지'
        '(정말 내리려면 ALLOW_DOWNGRADE=1)"; exit 3; fi\n'
        'BK=""\n'
        'if [ -d "${TG}/isilon_usage" ]; then BK="${TG}/isilon_usage.bak.$(date +%s)"; '
        'sudo mv "${TG}/isilon_usage" "$BK"; fi\n'
        'sudo cp -a "$STAGE/." "${TG}/"\n'
        "# 낡은 바이트코드 캐시 제거 — 안 그러면 새 코드를 깔아도 옛 버전이 보고/실행될 수 있음\n"
        'sudo find "${TG}" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true\n'
        'if ! (cd "${TG}" && python3 -c "import isilon_usage.cli, isilon_usage.server, isilon_usage.scanner"); then\n'
        '  echo "  ERROR: 새 코드를 불러오지 못함 — 이전 코드로 되돌림"\n'
        '  sudo rm -rf "${TG}/isilon_usage"; [ -d "${BK:-}" ] && sudo mv "$BK" "${TG}/isilon_usage"; exit 5\n'
        'fi\n'
        '{ ls -dt "${TG}"/isilon_usage.bak.* 2>/dev/null | tail -n +4 | xargs -r sudo rm -rf; } || true\n'
    ).replace("${TG}", "${%s}" % target_var)


def build_provision_script(*, host, port, token, path, hq_base, install="systemd",
                           data_dir="/data/isilon_edge_data",
                           edge_dir="/opt/isilon_edge") -> str:
    """엣지에서 복붙 실행할 자동 구성 스크립트(bash)를 만든다.

    HQ 포탈에서 코드를 받아(agent-bundle) 배치하고, api_token·mount-base 와 함께
    serve 를 기동한다(systemd 또는 nohup). 값은 shell 주입을 막기 위해 따옴표 처리.
    """
    q = shlex.quote
    for _v in (path, data_dir, edge_dir, token):
        if re.search(r'[\s"\\]', str(_v or "")):
            raise ValueError("경로/토큰에 공백·따옴표·역슬래시는 쓸 수 없습니다(systemd 유닛이 깨짐): %r" % _v)

    def sd(v):   # systemd ExecStart 인자: 큰따옴표 + 지정자(%)·변수($) 이스케이프(유닛 파일에 문자 그대로 기록)
        return '"' + str(v).replace("%", "%%").replace("$", "$$") + '"'
    host = re.sub(r"[^A-Za-z0-9._:\[\]-]", "", str(host or ""))   # 표시용 host: 셸 위험문자 제거(주입 방지)
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
        'OLDV=$(cd "$EDGE_DIR" 2>/dev/null && python3 -m isilon_usage --version 2>/dev/null || echo "?")\n'
        "@@STAGED@@"
        '(cd "$EDGE_DIR" && python3 -m isilon_usage --version)\n'
        "# 이미 설정 파일이 있는 엣지(재구성)는 기동 플래그(--api-token·--mount-base)가 '처음 설정'에만 쓰여 무시된다 —\n"
        "# 포탈이 기록한 토큰·경로와 엣지가 달라 401 이 나던 문제(2026-11 리뷰). 있으면 그 파일에 직접 맞춘다(권한 600).\n"
        'sudo python3 - "$DATA" "$TOKEN" "$MOUNT" <<\'PY\'\n'
        'import json, os, sys\n'
        'd, tok, mnt = sys.argv[1:4]\n'
        'p = os.path.join(d, "settings.json")\n'
        'try:\n'
        '    with open(p, encoding="utf-8") as fh:\n'
        '        s = json.load(fh)\n'
        'except (OSError, ValueError):\n'
        '    s = None\n'
        'if isinstance(s, dict):\n'
        '    if tok:\n'
        '        s["api_token"] = tok\n'
        '    mb = [x for x in (s.get("mount_bases") or []) if isinstance(x, str)]\n'
        '    if mnt and os.path.abspath(mnt) not in [os.path.abspath(x) for x in mb]:\n'
        '        mb.append(mnt)\n'
        '    s["mount_bases"] = mb\n'
        '    tmp = p + ".tmp"\n'
        '    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n'
        '    with os.fdopen(fd, "w", encoding="utf-8") as fh:\n'
        '        json.dump(s, fh, ensure_ascii=False, indent=2)\n'
        '    os.replace(tmp, p)\n'
        '    print("  -> 기존 설정에 포탈의 토큰·경로 반영: " + p)\n'
        'PY\n'
    ) % (q(edge_dir), q(data_dir), q(path), int(port), q(token), q(hq))
    head = head.replace("@@STAGED@@", _staged_install_sh("EDGE_DIR"))
    if install == "nohup":
        tail = (
            '\necho "[3/4] nohup 으로 기동 (포트 $PORT)"\n'
            'cd "$EDGE_DIR"\n'
            'nohup python3 -m isilon_usage serve --data-dir "$DATA" ${MOUNT:+--mount-base "$MOUNT"} \\\n'
            '    --host 0.0.0.0 --port "$PORT" --api-token "$TOKEN" \\\n'
            '    > /var/log/isilon_usage.log 2>&1 &\n'
            'echo "[4/4] 완료 — http://%s:%d/  (HQ 포탈이 자동 폴링)"\n'
        ) % (host, int(port))
    else:
        tail = (
            '\necho "[3/4] systemd 서비스 설치/기동 (포트 $PORT)"\n'
            # 유닛 내용은 문자 그대로('UNIT' — 셸 확장 없음) 쓰고, 값은 systemd 규칙으로 이스케이프한다.
            # 예전엔 셸 변수를 따옴표 없이 펼쳐 %·$·공백이 든 경로/토큰에서 유닛이 깨졌다(2026-11 리뷰).
            "sudo tee /etc/systemd/system/isilon-edge.service >/dev/null <<'UNIT'\n"
            "[Unit]\n"
            "Description=Isilon Edge - 디렉터리 사용량 스캐너\n"
            "After=network-online.target remote-fs.target\n"
            "Wants=network-online.target\n"
            "RequiresMountsFor=%s\n"
            "[Service]\n"
            "Type=simple\n"
            "User=root\n"
            "WorkingDirectory=%s\n"
            "ExecStart=/usr/bin/python3 -m isilon_usage serve --data-dir %s%s "
            "--host 0.0.0.0 --port %d --api-token %s\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "UNIT\n"
            "sudo systemctl daemon-reload\n"
            "sudo systemctl enable --now isilon-edge\n"
            'echo "[4/4] 완료 — http://%s:%d/  ·  journalctl -u isilon-edge -f"\n'
        ) % (str(data_dir).replace("%", "%%"), str(edge_dir).replace("%", "%%"), sd(data_dir),
             (" --mount-base " + sd(path)) if path else "", int(port), sd(token), host, int(port))
    return head + tail


def build_upgrade_script(*, hq_base, edge_dir="/opt/isilon_edge",
                         service="isilon-edge") -> str:
    """등록된 엣지를 HQ 의 최신 코드로 올리는 bash 업그레이드 스크립트를 만든다.

    HQ 포탈에서 agent-bundle(현재 실행 중 코드)을 받아 엣지 코드 디렉터리에 덮어쓰고
    서비스를 재시작한다. 값은 shell 주입을 막기 위해 따옴표 처리한다.
    """
    q = shlex.quote
    hq = (hq_base or "http://<HQ-IP>:8800").rstrip("/")
    return ((
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
        "@@STAGED@@"
        'NEWV=$(cd "$TARGET" && python3 -m isilon_usage --version 2>/dev/null || echo "?")\n'
        'echo "  -> 새 코드: $NEWV (이전 코드 백업: ${BK:-없음})"\n'
        'echo "[2/3] 서비스 재시작: $SERVICE"\n'
        'if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE}\\.service"; then\n'
        '  sudo systemctl restart "$SERVICE"\n'
        '  echo "  -> systemctl restart $SERVICE 완료"\n'
        "else\n"
        '  echo "  WARN: systemd 유닛 ${SERVICE} 없음 - 코드는 $TARGET 에 갱신됨, 서비스를 수동 재시작하세요"\n'
        "fi\n"
        'echo "[3/3] 업그레이드 $OLDV -> $NEWV (대상 $TARGET, 서비스 $SERVICE)"\n'
    ) % (q(edge_dir), q(hq), q(service))).replace("@@STAGED@@", _staged_install_sh("TARGET"))


# ------------------------------------------------------------------ 컨트롤러
class PortalController:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.nodes = load_nodes(self.data_dir)
        self.settings = self._load_settings()
        # 중앙 복제 DB 저장 경로 — 설정(replica_dir)이 있으면 그곳, 없으면 data_dir/replicas
        self.replicas_dir = (str(self.settings.get("replica_dir") or "").strip()
                             or os.path.join(self.data_dir, "replicas"))
        os.makedirs(self.replicas_dir, exist_ok=True)
        self._clean_replica_temps()
        self._launch_cwd = os.getcwd()      # 임시 비밀번호 파일(portal_info.MD)을 둘 '실행한 디렉터리'
        self._op_auto = False               # 기동(ensure_op_password) 후엔 '비밀번호 없음'을 두지 않는다
        self._op_sync_lock = threading.Lock()
        self._op_disk_seen = self.settings.get("op_password")   # 마지막으로 디스크에서 본/쓴 비밀번호
        try:
            self._settings_mtime = os.stat(portal_settings_path(self.data_dir)).st_mtime_ns
        except OSError:
            self._settings_mtime = None
        self._auth = authmod.AuthGuard(
            self._op_password_now,          # 서버 명령(reset-password --portal)이 바꾼 비밀번호도 바로 반영
            ttl=lambda: float(self.settings.get("op_ttl_minutes", 30) or 30) * 60)
        self._last_upgrade_check = 0.0
        self._init_ping_db()
        self._init_throughput_db()
        self._thru_last = {}      # id -> 마지막 기록한 scanned_bytes(값 바뀔 때만 적재)
        self._cache = {}          # id -> {online, ts, version, hostname, overall, error}
        self._inflight = set()      # 상태 폴링 중인 노드(가벼움 — 수 초)
        self._rep_inflight = set()  # DB 복제 중인 노드(큰 DB 는 수 분~수 시간) — 폴링과 별개
        self._rep_at = {}           # id -> 마지막 복제 '시도' 시각(포탈 시계) — 복제 주기 판정 기준
        self._rep_fail = {}         # id -> 연속 복제 실패 횟수(재시도 백오프)
        self._rep_err = {}          # id -> 마지막 복제 오류(다음 복제 성공 전까지 표시)
        self._rep_missing = {}      # id -> 마지막 복제에서 meta 에는 있었으나 DB 가 오지 않은 스캔 수
        self._rep_missing_prev = {}  # id -> 그 누락 스캔들의 완료 시각(같은 누락이 반복되면 건너뜀)
        self._clock_rewound = {}    # id -> 이번 복제에서 엣지 시계 역행으로 되돌린 워터마크
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
               "op_password_temp": False, "op_info_path": "",
               "upgrade_watch_dir": "", "upgrade_check_secs": 60,
               "upgrade_source": "off", "upgrade_url": "", "upgrade_token": "",
               "upgrade_auto": False,
               "enroll_token": "", "release_dir": "/opt/isilon_release",
               "backup_dir": "", "backup_every_hours": 1.0, "backup_keep": 100,
               "portal_title": "", "portal_subtitle": "", "nav_hidden": [],
               "replica_dir": "",   # 중앙 복제 DB 저장 경로(빈값=기본 data_dir/replicas)
               "replica_keep_per_root": 5,   # 노드·루트별 보관할 복제 스캔 수(0=무제한)
               "export_token": "", "op_ttl_minutes": 30, "show_update_popup": False,
               "log_popup_pos": "center",   # 로그 팝업 위치: center/tl/tr/bl/br
               "ask_llm_enabled": False, "ask_llm_endpoint": "", "ask_llm_model": "",
               "ask_llm_key": "", "ask_llm_timeout": 20}
        try:
            s = setmod.read_json_with_backup(portal_settings_path(self.data_dir))
            if isinstance(s, dict):
                out["op_password"] = str(s.get("op_password") or "")
                out["op_password_encrypted"] = bool(s.get("op_password_encrypted", False))
                out["op_password_temp"] = bool(s.get("op_password_temp")) and bool(out["op_password"])
                out["op_info_path"] = str(s.get("op_info_path") or "")
                out["upgrade_watch_dir"] = str(s.get("upgrade_watch_dir") or "").strip()
                try:
                    out["upgrade_check_secs"] = max(10, int(s.get("upgrade_check_secs", 60) or 60))
                except (TypeError, ValueError, OverflowError):
                    pass
                _src = str(s.get("upgrade_source") or "off").strip().lower()
                out["upgrade_source"] = _src if _src in ("off", "github") else "off"
                out["upgrade_url"] = str(s.get("upgrade_url") or "").strip()
                out["upgrade_token"] = str(s.get("upgrade_token") or "").strip()
                out["upgrade_auto"] = bool(s.get("upgrade_auto"))
                out["enroll_token"] = str(s.get("enroll_token") or "").strip()
                out["release_dir"] = (str(s.get("release_dir") or "").strip()
                                      or "/opt/isilon_release")
                out["backup_dir"] = str(s.get("backup_dir") or "").strip()
                try:
                    out["backup_every_hours"] = min(8760.0, max(0.1, float(s.get("backup_every_hours", 1) or 1)))
                except (TypeError, ValueError, OverflowError):
                    pass
                try:
                    out["backup_keep"] = min(1000, max(1, int(s.get("backup_keep", 100) or 100)))
                except (TypeError, ValueError, OverflowError):
                    pass
                out["portal_title"] = str(s.get("portal_title") or "").strip()
                out["portal_subtitle"] = str(s.get("portal_subtitle") or "").strip()
                # 상단 메뉴 숨김 목록('nodes'=설정은 잠금 방지를 위해 숨길 수 없음)
                out["nav_hidden"] = [v for v in (s.get("nav_hidden") or [])
                                     if v in ("dash", "netmon", "compare")]
                out["export_token"] = str(s.get("export_token") or "").strip()
                try:
                    out["op_ttl_minutes"] = max(1, min(10080, int(s.get("op_ttl_minutes", 30))))
                except (TypeError, ValueError, OverflowError):
                    pass
                out["show_update_popup"] = bool(s.get("show_update_popup", False))
                _lp = str(s.get("log_popup_pos") or "center").strip().lower()
                out["log_popup_pos"] = _lp if _lp in ("center", "tl", "tr", "bl", "br") else "center"
                try:
                    out["replica_keep_per_root"] = max(0, min(1000, int(s.get("replica_keep_per_root", 5))))
                except (TypeError, ValueError, OverflowError):
                    pass
                _rd = str(s.get("replica_dir") or "").strip()
                out["replica_dir"] = _rd if os.path.isabs(_rd) else ""
                out["ask_llm_enabled"] = bool(s.get("ask_llm_enabled"))
                out["ask_llm_endpoint"] = str(s.get("ask_llm_endpoint") or "").strip()
                out["ask_llm_model"] = str(s.get("ask_llm_model") or "").strip()
                out["ask_llm_key"] = str(s.get("ask_llm_key") or "")
                try:
                    out["ask_llm_timeout"] = max(1, min(600, int(s.get("ask_llm_timeout", 20) or 20)))
                except (TypeError, ValueError, OverflowError):
                    pass
        except (OSError, ValueError):
            pass
        return out

    def _save_settings(self) -> None:
        self._sync_op_from_disk()          # 서버 명령이 바꾼 비밀번호를 옛 메모리 값으로 되돌리지 않게
        os.makedirs(self.data_dir, exist_ok=True)
        path = portal_settings_path(self.data_dir)
        # 로그인 비밀번호·enroll 토큰·LLM 키 등이 들어가므로 소유자만 읽도록 제한(고유 임시 파일·.bak).
        setmod.write_json_atomic(path, self.settings)
        self._op_disk_seen = self.settings.get("op_password")
        try:
            self._settings_mtime = os.stat(path).st_mtime_ns
        except OSError:
            pass

    # --- 관리 비밀번호: 디스크 동기화·임시 비밀번호(엣지와 같은 규칙) ---
    def _op_password_now(self):
        self._sync_op_from_disk()
        return self.settings.get("op_password")

    def _sync_op_from_disk(self) -> None:
        """portal_settings.json 의 비밀번호가 마지막으로 본 값과 다르면(서버 명령 초기화) 받아들이고 세션을 폐기."""
        path = portal_settings_path(self.data_dir)
        try:
            mt = os.stat(path).st_mtime_ns
        except OSError:
            return
        with self._op_sync_lock:
            if mt == self._settings_mtime:
                return
            self._settings_mtime = mt
            try:
                with open(path, encoding="utf-8") as fh:
                    disk = json.load(fh)
            except (OSError, ValueError):
                return
            if not isinstance(disk, dict) or disk.get("op_password", "") == self._op_disk_seen:
                return
            for k in OP_RUNTIME_KEYS:
                self.settings[k] = disk.get(k) if k != "op_password_temp" else bool(disk.get(k))
            self._op_disk_seen = disk.get("op_password", "")
        self._auth.revoke_all()
        auditmod.record(self.data_dir, action="op_password_reset_cli", ok=True,
                        detail=str(disk.get("op_info_path") or ""))

    def ensure_op_password(self) -> Optional[dict]:
        """비밀번호가 없으면 임시 비밀번호를 만들어 실행 디렉터리 portal_info.MD 에 기록(포탈 기동 시 호출).
        이후 비밀번호를 비우는 저장(보호 해제)도 다시 임시로 초기화한다. 반환: {info_path, password} 또는 None."""
        self._op_auto = True
        self._sync_op_from_disk()
        if self.settings.get("op_password"):
            return None
        res = reset_op_password_file(portal_settings_path(self.data_dir), self.data_dir,
                                     self._launch_cwd, portal=True)
        if res is None:
            return None
        fresh = self._load_settings()
        for k in OP_RUNTIME_KEYS:
            self.settings[k] = fresh.get(k)
        self._op_disk_seen = self.settings.get("op_password")
        try:
            self._settings_mtime = os.stat(portal_settings_path(self.data_dir)).st_mtime_ns
        except OSError:
            pass
        self._auth.revoke_all()
        auditmod.record(self.data_dir, action="op_password_temp", ok=True, detail=res["info_path"])
        return {"info_path": res["info_path"], "password": res["password"]}

    def op_temp_info(self) -> dict:
        """화면 안내용 — 임시 비밀번호 사용 중이면 portal_info.MD 경로(비밀번호 자체는 내보내지 않는다)."""
        self._sync_op_from_disk()
        temp = bool(self.settings.get("op_password_temp")) and bool(self.settings.get("op_password"))
        return {"op_temp": temp, "op_info_path": (self.settings.get("op_info_path") or "") if temp else ""}

    def auth_required(self) -> bool:
        return self._auth.required()

    def auth_status(self) -> dict:
        return {"ok": True, "op_required": self._auth.required(),
                **self.op_temp_info(),
                "encrypted": bool(self.settings.get("op_password_encrypted")),
                "version": __version__,
                "show_update_popup": bool(self.settings.get("show_update_popup")),
                "log_popup_pos": self.settings.get("log_popup_pos", "center"),
                "op_ttl_minutes": int(self.settings.get("op_ttl_minutes", 30) or 30),
                "portal_title": self.settings.get("portal_title") or "",
                "portal_subtitle": self.settings.get("portal_subtitle") or "",
                "nav_hidden": self.settings.get("nav_hidden") or []}

    def set_nav_hidden(self, items) -> dict:
        """상단에 숨길 메뉴 목록을 저장한다('nodes'=설정은 잠금 방지로 숨길 수 없음)."""
        hidden = [v for v in (items or []) if v in ("dash", "netmon", "compare")]
        self.settings["nav_hidden"] = hidden
        self._save_settings()
        return {"ok": True, "nav_hidden": hidden}

    def set_title(self, title: str, subtitle: str) -> dict:
        """상단/탭 제목(브랜딩)을 설정한다. 호출 측(핸들러)에서 인증을 확인한다."""
        self.settings["portal_title"] = str(title or "").strip()
        self.settings["portal_subtitle"] = str(subtitle or "").strip()
        self._save_settings()
        return {"ok": True, "portal_title": self.settings["portal_title"],
                "portal_subtitle": self.settings["portal_subtitle"]}

    def login(self, password: str, client=None) -> dict:
        return self._auth.login(password, client)

    def token_valid(self, token) -> bool:
        self._sync_op_from_disk()          # 서버 명령으로 초기화됐으면 옛 세션을 먼저 폐기
        return self._auth.token_valid(token)

    def set_password(self, new_password: str, encrypt: bool) -> dict:
        """비밀번호 설정/변경/해제. 호출 측(핸들러)에서 인증을 확인한다.
        해제(빈 비밀번호)는 기동 후엔 '비밀번호 없음'으로 두지 않고 새 임시 비밀번호로 초기화한다."""
        pw = str(new_password or "")
        if pw and len(pw.strip()) < 4:
            # 엣지와 같은 규칙 — 예전엔 공백 한 칸·1자도 로그인 비밀번호가 됐다(2026-11 리뷰). 빈 값=해제는 그대로.
            return {"ok": False, "_status": 400, "reason": "비밀번호는 4자 이상이어야 합니다."}
        enc = _truthy(encrypt)
        self._sync_op_from_disk()
        old_info = self.settings.get("op_info_path") or ""
        was_temp = bool(self.settings.get("op_password_temp"))
        self.settings["op_password"] = authmod.store_password(pw, encrypt=enc) if pw else ""
        self.settings["op_password_encrypted"] = enc if pw else False
        self.settings["op_password_temp"] = False
        self.settings["op_info_path"] = ""
        self._save_settings()
        if was_temp and old_info:          # 임시 비밀번호 파일은 더 이상 맞지 않으니 지운다
            try:
                os.remove(old_info)
            except OSError:
                pass
        self._auth.revoke_all()            # 옛 비밀번호로 받은 로그인 세션은 모두 만료
        if not pw and self._op_auto:
            self.ensure_op_password()
        out = {"ok": True, "op_required": self._auth.required(),
               "reset_to_temp": bool(not pw and self.settings.get("op_password_temp")),
               "encrypted": bool(self.settings.get("op_password_encrypted"))}
        if pw:                             # 바꾼 본인에게만 새 토큰(검증은 핸들러에서 끝남)
            out["token"] = self._auth.issue_token()
            out["ttl"] = self._auth.ttl
        return out

    def set_enroll_token(self, token: str) -> dict:
        """엣지 자기등록(enroll) 공유 토큰을 설정/해제. 호출 측에서 인증을 확인한다."""
        self.settings["enroll_token"] = str(token or "").strip()
        self._save_settings()
        return {"ok": True, "enroll_token_set": bool(self.settings["enroll_token"])}

    def set_replica_dir(self, path: str) -> dict:
        """중앙 복제 DB 저장 경로를 설정한다(빈값=기본 data_dir/replicas). 즉시 적용.

        같은 파일시스템이고 새 경로가 비어 있으면 기존 복제본을 통째로 이동(rename,
        즉시·데이터 무복사)한다. 이동하지 못하면(다른 디스크·대상에 파일 있음 등)
        경로만 바꾸고 다음 동기화가 새 경로를 다시 채운다 — 증분 복제라 자동 복구되며,
        그때까지 통합 조회가 비어 보일 수 있음을 note 로 알린다.
        """
        p = str(path or "").strip()
        if p and not os.path.isabs(p):
            return {"ok": False, "reason": "절대 경로로 입력하세요(예: /bigdisk/isilon_replicas)."}
        old = self.replicas_dir
        new = p or os.path.join(self.data_dir, "replicas")
        moved = False
        note = ""
        if os.path.abspath(new) != os.path.abspath(old):
            try:   # 새 경로 생성 + 쓰기 확인(권한/읽기전용 마운트를 저장 시점에 걸러냄)
                os.makedirs(new, exist_ok=True)
                probe = os.path.join(new, ".iu_write_test")
                with open(probe, "w") as fh:
                    fh.write("x")
                os.remove(probe)
            except OSError as exc:
                return {"ok": False, "reason": "새 경로에 쓸 수 없습니다: %s" % exc}
            try:   # 기존 복제본 이동(대상이 비어 있을 때만 — 있는 데이터는 덮지 않음)
                if os.path.isdir(old) and os.listdir(old) and not os.listdir(new):
                    os.rmdir(new)
                    os.rename(old, new)
                    moved = True
            except OSError:
                note = ("기존 복제본을 이동하지 못했습니다(다른 디스크/사용 중/대상에 파일 있음) — "
                        "다음 동기화가 새 경로를 다시 채웁니다.")
        self.settings["replica_dir"] = p
        self._save_settings()
        self.replicas_dir = new
        try:
            os.makedirs(new, exist_ok=True)
        except OSError:
            pass
        return {"ok": True, "replica_dir": p, "replica_dir_effective": new,
                "replica_moved": moved, "replica_note": note}

    def set_replica_keep(self, keep) -> dict:
        """노드·루트별 복제 스캔 보관 수(0=무제한)를 설정하고, 줄였으면 지금 있는 복제본에도 바로 적용한다.

        복제 중인 노드는 건너뛴다(그 노드는 이번 복제가 끝날 때 같은 규칙으로 정리된다)."""
        try:
            n = int(keep)
        except (TypeError, ValueError, OverflowError):
            return {"ok": False, "reason": "보관 수는 0 이상의 정수여야 합니다(0=무제한)."}
        if not 0 <= n <= 1000:
            return {"ok": False, "reason": "보관 수는 0~1000 사이여야 합니다(0=무제한)."}
        self.settings["replica_keep_per_root"] = n
        self._save_settings()
        removed = skipped = 0
        if n > 0:
            with self._lock:
                busy = set(self._rep_inflight)
            try:    # 복제본 폴더 기준(노드 목록에서 지운 노드의 옛 복제본도 같은 규칙으로)
                ids = sorted(x for x in os.listdir(self.replicas_dir)
                             if os.path.isdir(os.path.join(self.replicas_dir, x)))
            except OSError:
                ids = []
            for nid in ids:
                if nid in busy:
                    skipped += 1
                    continue
                removed += self._prune_replica_dir(os.path.join(self.replicas_dir, nid), n)
        return {"ok": True, "replica_keep_per_root": n, "replica_pruned": removed,
                "replica_prune_skipped": skipped}

    def _prune_replica_dir(self, dest: str, keep: int) -> int:
        """한 노드 복제본 폴더(meta.json + scans/)에 보관 규칙을 적용 — 지운 스캔 수를 돌려준다."""
        mp = os.path.join(dest, "meta.json")
        try:
            with open(mp, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return 0
        scans = meta.get("scans") if isinstance(meta, dict) else None
        if not isinstance(scans, list):
            return 0
        kept = self._prune_replica_scans(dest, [x for x in scans if isinstance(x, dict)], keep)
        if len(kept) == len(scans):
            return 0
        meta["scans"] = kept
        tmp = mp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as w:
            json.dump(meta, w, ensure_ascii=False)
        os.replace(tmp, mp)
        return len(scans) - len(kept)

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

    # --- 백업: 포탈 설정 + 노드 목록(+감사/핑) 을 tar.gz 하나로 ---
    _BACKUP_FILES = ("portal_settings.json", "portal_nodes.json", "audit.log",
                     "ping_history.db", "throughput_history.db")

    def make_backup_bytes(self):
        """포탈 설정·노드 목록(+감사/핑)을 tar.gz 로 묶어 (bytes, filename) 반환.

        대용량/재생성 가능한 replicas/·upgrades/ 는 제외한다. 노드 api_token·op 비밀번호가
        포함되므로 백업 파일은 민감 정보로 다룬다.
        """
        import socket
        buf = io.BytesIO()
        included = []
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in self._BACKUP_FILES:
                p = os.path.join(self.data_dir, name)
                if os.path.isfile(p):
                    tf.add(p, arcname="portal_backup/" + name)
                    included.append(name)
            manifest = {"kind": "isilon_portal_backup", "version": __version__,
                        "created_at": time.time(), "hostname": socket.gethostname(),
                        "node_count": len(self.nodes), "files": included}
            md = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            ti = tarfile.TarInfo("portal_backup/backup_manifest.json")
            ti.size = len(md)
            tf.addfile(ti, io.BytesIO(md))
        fname = "isilon_portal_backup-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
        return buf.getvalue(), fname

    def save_backup(self, dest_dir: str) -> dict:
        """백업 tar.gz 를 서버의 dest_dir 에 저장한다(폴더 없으면 생성)."""
        d = (dest_dir or "").strip()
        if not d:
            return {"ok": False, "reason": "저장할 디렉터리를 입력하세요."}
        data, fname = self.make_backup_bytes()
        try:
            if not os.path.isdir(d):
                os.makedirs(d, mode=0o700, exist_ok=True)   # 새로 만드는 백업 폴더는 소유자만
            path = os.path.join(d, fname)
            tmp = path + ".tmp"
            # 백업에는 노드 토큰(평문)·비밀번호 해시가 들어 있다 — 원본 설정 파일처럼 소유자만 읽게(0600).
            # 예전엔 umask 대로 0644 로 만들어 같은 서버의 다른 사용자가 읽을 수 있었다.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as exc:
            return {"ok": False, "reason": "저장 실패: %s" % exc}
        return {"ok": True, "path": path, "size": len(data)}

    # --- 자동 백업 일정/보관 ---
    _BACKUP_RE = re.compile(r"^isilon_portal_backup-\d{8}-\d{6}\.tar\.gz$")

    def _list_backups(self, bdir) -> list:
        """bdir 의 백업 파일을 오래된→최신 순으로 [{name,size,mtime}] 반환."""
        d = (bdir or "").strip()
        out = []
        if d and os.path.isdir(d):
            for n in os.listdir(d):
                if self._BACKUP_RE.match(n):
                    p = os.path.join(d, n)
                    try:
                        out.append({"name": n, "size": os.path.getsize(p),
                                    "mtime": os.path.getmtime(p)})
                    except OSError:
                        pass
        out.sort(key=lambda x: x["mtime"])
        return out

    def _prune_backups(self, bdir, keep) -> int:
        """최신 keep 개만 남기고 오래된 백업을 지운다(지운 개수 반환)."""
        files = self._list_backups(bdir)
        keep = max(1, int(keep or 1))
        removed = 0
        for f in (files[:-keep] if len(files) > keep else []):
            try:
                os.remove(os.path.join(bdir, f["name"]))
                removed += 1
            except OSError:
                pass
        return removed

    def backup_info(self) -> dict:
        """자동 백업 설정 + 현재 보관 현황(개수·용량·최근·다음 예정)."""
        bdir = (self.settings.get("backup_dir") or "").strip()
        every = float(self.settings.get("backup_every_hours") or 1)
        keep = int(self.settings.get("backup_keep") or 100)
        files = self._list_backups(bdir)
        last = files[-1]["mtime"] if files else 0
        return {"backup_dir": bdir, "backup_every_hours": every, "backup_keep": keep,
                "backup_count": len(files),
                "backup_total_bytes": sum(f["size"] for f in files),
                "backup_last": last, "backup_newest": (files[-1]["name"] if files else ""),
                "backup_next": (last + every * 3600) if (bdir and last) else 0}

    def set_backup_config(self, dest_dir, every_hours, keep) -> dict:
        """자동 백업 폴더·주기(시간)·최대 보관 수를 저장(폴더 비우면 자동 백업 끔).
        보내지 않은 값(None)은 그대로 둔다 — 예전엔 주기만 바꿔도 폴더가 비워져 자동 백업이 꺼졌다(2026-11 리뷰).
        폴더는 절대 경로만(상대 경로는 서버 실행 위치에 따라 달라진다 — 복제 폴더와 같은 규칙)."""
        if dest_dir is not None:
            d = str(dest_dir).strip()
            if d and not os.path.isabs(d):
                return {"ok": False, "reason": "백업 폴더는 절대 경로(/ 로 시작)여야 합니다."}
            self.settings["backup_dir"] = d
        try:
            # 상한(1년) — 1e999(=Infinity)가 저장되면 응답 JSON 이 깨지고 자동 백업이 영영 안 돌았다(2026-11 리뷰)
            self.settings["backup_every_hours"] = min(8760.0, max(0.1, float(every_hours)))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            self.settings["backup_keep"] = min(1000, max(1, int(keep)))
        except (TypeError, ValueError, OverflowError):
            pass
        self._save_settings()
        return dict({"ok": True}, **self.backup_info())

    def _maybe_scheduled_backup(self, now) -> None:
        """주기 루프에서 호출 — 자동 백업 시간이 됐으면 저장 + 오래된 것 정리."""
        bdir = (self.settings.get("backup_dir") or "").strip()
        if not bdir:
            return
        every = max(300.0, float(self.settings.get("backup_every_hours") or 1) * 3600)
        files = self._list_backups(bdir)
        last = files[-1]["mtime"] if files else 0
        if now - last < every:
            return
        # 실패 백오프: 저장이 실패(권한·디스크 부족)하면 예전엔 매 주기(수십 초) 전체 백업을 다시 만들며
        # 실패했다(2026-11 리뷰). 5분→10분→…최대 1시간 간격으로 재시도하고, 실패 기록은 연속 첫 번만 남긴다.
        if now < float(getattr(self, "_bk_retry_at", 0) or 0):
            return
        res = self.save_backup(bdir)
        if res.get("ok"):
            self._bk_fails = 0
            self._bk_retry_at = 0
            self._prune_backups(bdir, int(self.settings.get("backup_keep") or 100))
            auditmod.record(self.data_dir, action="backup_auto", ok=True,
                            detail=res.get("path", ""))
        else:
            n = int(getattr(self, "_bk_fails", 0) or 0) + 1
            self._bk_fails = n
            self._bk_retry_at = now + min(3600.0, 300.0 * (2 ** (n - 1)))
            if n == 1:
                auditmod.record(self.data_dir, action="backup_auto", ok=False,
                                detail=str(res.get("reason") or ""))

    # --- 자동 업그레이드 ---
    def set_upgrade_watch(self, watch_dir, check_secs=None) -> dict:
        if watch_dir is not None:          # None = 바꾸지 않음
            self.settings["upgrade_watch_dir"] = str(watch_dir or "").strip()
        if check_secs is not None:
            try:
                self.settings["upgrade_check_secs"] = max(10, int(check_secs))
            except (TypeError, ValueError, OverflowError):
                pass
        self._save_settings()
        return {"ok": True, "upgrade_watch_dir": self.settings.get("upgrade_watch_dir", ""),
                "upgrade_check_secs": self.settings["upgrade_check_secs"]}

    def upgrade_config(self) -> dict:
        bver = agent_bundle_version()
        try:   # 복제본 현황(노드 폴더 수) — 가벼운 목록 조회만(용량 합산은 느려서 안 함)
            replica_count = len([d for d in os.listdir(self.replicas_dir)
                                 if os.path.isdir(os.path.join(self.replicas_dir, d))])
        except OSError:
            replica_count = 0
        return dict({"ok": True, "upgrade_watch_dir": self.settings.get("upgrade_watch_dir", ""),
                     "replica_dir": self.settings.get("replica_dir") or "",
                     "replica_keep_per_root": int(self.settings.get("replica_keep_per_root", 5) or 0),
                     "replica_dir_effective": self.replicas_dir,
                     "replica_dir_default": os.path.join(self.data_dir, "replicas"),
                     "replica_count": replica_count,
                     "upgrade_check_secs": self.settings.get("upgrade_check_secs", 60),
                     "enroll_token_set": bool(self.settings.get("enroll_token")),
                     "hq_version": __version__, "bundle_version": bver,
                     "bundle_stale": bool(bver and bver != __version__),
                     "portal_title": self.settings.get("portal_title") or "",
                     "portal_subtitle": self.settings.get("portal_subtitle") or "",
                     "export_token_set": bool(self.settings.get("export_token")),
                     "ask_llm_enabled": bool(self.settings.get("ask_llm_enabled")),
                     "ask_llm_endpoint": self.settings.get("ask_llm_endpoint") or "",
                     "ask_llm_model": self.settings.get("ask_llm_model") or "",
                     "ask_llm_timeout": int(self.settings.get("ask_llm_timeout", 20) or 20),
                     "ask_llm_key_set": bool(self.settings.get("ask_llm_key"))},
                    **self.release_info(), **self.backup_info())

    def set_ask_llm(self, body) -> dict:
        """자연어 질의응답용 로컬 LLM 설정을 저장한다(보낸 키만 반영)."""
        if "ask_llm_enabled" in body:
            self.settings["ask_llm_enabled"] = _truthy(body.get("ask_llm_enabled"))
        if "ask_llm_endpoint" in body:
            self.settings["ask_llm_endpoint"] = str(body.get("ask_llm_endpoint") or "").strip()
        if "ask_llm_model" in body:
            self.settings["ask_llm_model"] = str(body.get("ask_llm_model") or "").strip()
        if body.get("ask_llm_key") is not None:   # null 은 '보내지 않음'(예전엔 저장된 키가 지워졌다)
            self.settings["ask_llm_key"] = str(body.get("ask_llm_key") or "")
        if "ask_llm_timeout" in body:
            try:
                self.settings["ask_llm_timeout"] = max(1, min(600, int(body.get("ask_llm_timeout") or 20)))
            except (TypeError, ValueError, OverflowError):
                pass
        self._save_settings()
        return {"ok": True, "ask_llm_enabled": bool(self.settings.get("ask_llm_enabled"))}

    def ask(self, question: str, *, allow_llm: bool = True) -> dict:
        """자연어 질의응답(규칙 기반 + 선택적 로컬 LLM). 노드 롤업을 공통 형태로 정규화.
        allow_llm=False 면 외부 LLM 을 부르지 않는다(로그인 없는 요청 — 저장된 키로 남이 호출량을 쓰지 못하게)."""
        ov = self.overview()
        roots = []
        for n in ov.get("nodes") or []:
            who = n.get("region") or n.get("hostname") or "node"
            for r in (n.get("roots") or []):
                roots.append({
                    "label": who + ":" + str(r.get("root_path")),
                    "root_path": r.get("root_path"),
                    "scanned_bytes": _root_capacity(r),
                    "fs_used_bytes": int(r.get("fs_used_bytes") or 0),
                    "fs_total_bytes": int(r.get("fs_total_bytes") or 0),
                    "total_files": int(r.get("capacity_files") if r.get("capacity_files") is not None
                                       else (r.get("total_files") or 0)),
                    "status": r.get("status"),
                    "finished_at": r.get("finished_at"),
                })
        totals = ov.get("totals") or {}
        overall = {
            "total_scanned_bytes": int(totals.get("used_bytes")
                                       or sum(x["scanned_bytes"] for x in roots)),
            "root_count": len(roots), "scan_count": len(roots),
            "active_scans": int(totals.get("active_scans") or 0),
            "roots": roots,
        }
        cfg = {"enabled": bool(self.settings.get("ask_llm_enabled")) and allow_llm,
               "endpoint": self.settings.get("ask_llm_endpoint") or "",
               "model": self.settings.get("ask_llm_model") or "",
               "key": self.settings.get("ask_llm_key") or "",
               "timeout": self.settings.get("ask_llm_timeout") or 20}
        return askmod.respond(question, {"scope": "portal", "overall": overall,
                                         "detail": None}, cfg)

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
        threading.Thread(target=lambda: self._push_loop(data, bver, nodes, record=True),
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

    def _push_loop(self, data: bytes, bver: str, nodes: list, *, record: bool = False) -> list:
        """각 엣지에 번들을 POST 하고 노드별 진행 상태를 갱신한다(동기). 결과 리스트 반환.

        record=True(버튼으로 시작한 단독 푸시)면 끝에 업그레이드 기록(History)도 남긴다.
        """
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
                resp = netutil.urlopen(req, timeout=120)
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
            same = all(("더 새 버전" in (r.get("reason") or "")) and (str(bver) in (r.get("reason") or ""))
                       for r in results)
            if same:   # 모두 이미 같은 버전 — 할 일이 없을 뿐 실패가 아니다
                self._upg_log("✓ 모든 노드가 이미 v%s 입니다 — 올릴 것이 없습니다." % bver)
            else:
                self._upg_log("⚠ 전부 '더 새 버전 아님'으로 거부 — 포탈 배포 코드 v%s 가 엣지보다 낮거나 "
                              "같습니다. 포탈을 먼저 최신으로 올린 뒤 다시 푸시하세요." % bver)
        with self._lock:
            p = self._upg_state.get("push")
            if p:
                p["active"] = False
                p["finished"] = time.time()
        # 이미 같거나 더 새 버전인 노드는 '실패'가 아니다 — 예전엔 일부가 최신이면 이력 전체가 실패로 남았다(2026-11 리뷰)
        curn = sum(1 for r in results if not r.get("ok") and "더 새 버전" in (r.get("reason") or ""))
        self._upg_log("③ 엣지 전파 완료 — 성공 %d/%d%s (보낸 버전 v%s)"
                      % (okn, len(nodes), (" · 이미 최신 %d" % curn) if curn else "", bver))
        if record:
            # 성공 = 모든 노드가 올라갔거나 이미 같은(또는 더 새) 버전 — 전부 '이미 최신'도 실패가 아니다(7회차 조건 수정)
            self._record_upgrade(kind="전 노드 푸시", ok=(len(nodes) > 0 and okn + curn == len(nodes)),
                                 to=bver, push=results)
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
                        "reason": "노드 URL 에서 host 를 못 읽음(설정의 url 확인)"}
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

        이미 설치 중(installing)이거나 DB 복제 중(_rep_inflight)이면 건너뛴다(수동 '지금 업그레이드'
        와의 중복 실행·복제 파일 쓰는 도중 재시작 방지). 가벼운 상태 폴링(_inflight)은 재시작해도
        잃을 것이 없으므로 막지 않는다 — 폴링은 15초마다 도니, 막으면 업그레이드가 계속 밀린다.
        """
        with self._lock:
            if self._rep_inflight or self._upg_state.get("installing"):
                return
        code_dir = upgrademod.code_dir_of(__file__)
        # ① 로컬 감시 폴더
        wd = (self.settings.get("upgrade_watch_dir") or "").strip()
        if wd:
            bad = getattr(self, "_upg_bad", None)
            if bad is None:
                bad = self._upg_bad = set()
            found = upgrademod.find_newer_archive(wd, __version__, skip=bad)
            if found and self._try_begin_install():
                try:
                    res = upgrademod.upgrade_from_archive(found[0], code_dir, __version__)
                    if res.get("ok"):
                        self._apply_self_upgrade(res, "감시 폴더")   # 재시작(돌아오지 않음)
                        return
                    bad.add(upgrademod.archive_sig(found[0]))   # 바뀌기 전까지 재시도 안 함(매 주기 실패 기록 방지)
                    self._upg_log("감시 폴더 업그레이드 실패: %s" % res.get("reason"))
                    self._record_upgrade(kind="감시 폴더", ok=False, error=res.get("reason"))
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
                    self._record_upgrade(kind="인터넷", ok=False, error=res.get("reason"))
                finally:
                    with self._lock:
                        self._upg_state["installing"] = False

    def _apply_self_upgrade(self, res: dict, how: str) -> None:
        """자가 업그레이드 성공 후: 감사로그 → 엣지 전파 → 기록(History) → 재시작(돌아오지 않음)."""
        self._upg_log("%s 자가 업그레이드 %s → %s, 엣지 전파 후 재시작" % (
            how, res.get("from"), res.get("version")))
        auditmod.record(self.data_dir, action="self_upgrade", ok=True,
                        detail="%s -> %s (%s)" % (res.get("from"), res.get("version"), how))
        push = []
        try:                                  # 새 코드가 디스크에 반영됨 → 엣지에도 푸시
            push = (self.push_upgrade_all() or {}).get("results") or []
        except Exception:  # noqa: BLE001
            pass
        self._record_upgrade(kind=how, ok=True, frm=res.get("from"),
                             to=res.get("version"), push=push, bump_marker=True)
        upgrademod.restart_process()

    # --- 업그레이드 기록(History): 자동/수동/푸시 결과를 디스크에 남겨 세부 조회 가능하게 ---
    _HISTORY_FILE = "upgrade_history.jsonl"
    _HISTORY_MAX = 200

    def _record_upgrade(self, *, kind, ok, frm=None, to=None, push=None, error=None,
                        bump_marker=False) -> None:
        """업그레이드 1건(단계 로그·엣지별 결과 포함)을 jsonl 에 append(재시작에도 보존, 상한 유지)."""
        with self._lock:
            log = list(self._upg_state.get("log") or [])[-80:]
        entry = {"ts": time.time(), "kind": kind, "ok": bool(ok), "from": frm, "to": to,
                 "bundle_version": agent_bundle_version(), "log": log}
        if error:
            entry["error"] = str(error)
        if push is not None:
            entry["nodes"] = push
            entry["push_ok"] = sum(1 for r in push if r.get("ok"))
            entry["push_total"] = len(push)
        path = os.path.join(self.data_dir, self._HISTORY_FILE)
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
            if len(lines) > self._HISTORY_MAX:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.writelines(lines[-self._HISTORY_MAX:])
            if bump_marker and to:           # 자가 업그레이드 성공 → 마커 갱신(기동 시 중복기록 방지)
                self._set_version_marker(to)
        except OSError:
            pass

    def _set_version_marker(self, v) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(os.path.join(self.data_dir, "portal_last_version"), "w",
                      encoding="utf-8") as fh:
                fh.write(str(v or ""))
        except OSError:
            pass

    def _record_startup_version(self) -> None:
        """기동 시 포탈 버전이 직전 기록과 다르면(재설치/외부 업그레이드/첫 기동) History 에 남긴다.

        포탈 자가 업그레이드는 재시작 직전 bump_marker 로 마커를 갱신하므로 여기서 중복 기록되지 않는다
        — 셸 재설치(install_portal.sh) 등 '코드 밖' 업그레이드만 이 경로로 잡힌다.
        """
        path = os.path.join(self.data_dir, "portal_last_version")
        try:
            with open(path, encoding="utf-8") as fh:
                last = fh.read().strip()
        except OSError:
            last = ""
        if last != __version__:
            self._record_upgrade(kind="포탈 기동", ok=True, frm=(last or None), to=__version__)
            self._set_version_marker(__version__)

    def upgrade_history(self, limit: int = 100) -> list:
        """업그레이드 기록을 최신순으로 반환(세부 log/nodes 포함)."""
        path = os.path.join(self.data_dir, self._HISTORY_FILE)
        out = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        out.reverse()
        return out[:max(1, int(limit or 100))]

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
        # 자동설치가 왜 안 되는지(소스 off/자동 off) 한 줄 설명 — 엣지와 같은 단일 소스 판단.
        st["auto_blocked"], st["auto_reason"] = upgrademod.auto_status_reason(
            source_mode=st["source_mode"], auto=st["auto"],
            available=bool(st.get("available")), busy=False,
            installing=bool(st.get("installing")),
            watch_dir=self.settings.get("upgrade_watch_dir", ""))
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
                self._record_upgrade(kind="수동", ok=False, error=res.get("reason"))
                with self._lock:
                    self._upg_state.update(installing=False, install_done=True,
                                           install_error=res.get("reason"))
                return
            self._upg_log("② HQ 코드 교체 완료: v%s → v%s" % (res.get("from"), res.get("version")))
            push = []
            if propagate:
                try:                          # 새 코드를 등록된 엣지에도 전파(엣지별 로그)
                    push = (self.push_upgrade_all() or {}).get("results") or []
                except Exception as e:  # noqa: BLE001
                    self._upg_log("③ 엣지 전파 중 오류: %s" % e)
            auditmod.record(self.data_dir, action="self_upgrade_manual", ok=True,
                            detail="-> %s" % res.get("version"))
            self._record_upgrade(kind="수동", ok=True, frm=res.get("from"),
                                 to=res.get("version"), push=push, bump_marker=True)
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
            except (TypeError, ValueError, OverflowError):
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
            nodes = [_public_node(n, cache, self._rep_err) for n in self.nodes]
        return {"ok": True, "nodes": nodes}

    def upsert_node(self, raw: dict, *, keep_token_on_url_change: bool = True,
                    partial: bool = False) -> dict:
        """노드 추가/수정. partial=True(CSV 가져오기·엣지 자기 등록)면 기존 노드의 값 위에 '값이 있는'
        필드만 덮는다 — 예전엔 전체 교체라 재등록·CSV 갱신 때 별칭·복제 주기·모드·사용 여부·지역이
        기본값으로 초기화됐다(2026-11 리뷰)."""
        if partial and isinstance(raw, dict):
            cur0 = self._get(str(raw.get("id") or "").strip())
            if cur0 is not None:
                base = {k: v for k, v in cur0.items() if k != "token"}   # 토큰 승계 규칙은 아래에서
                base.update({k: v for k, v in raw.items() if v not in (None, "")})
                raw = base
        n = sanitize_node(raw)
        if not n:
            return {"ok": False, "reason": "id 와 url 은 필수입니다(id 에 '/'·'..' 불가)."}
        with self._lock:
            cur = self._get(n["id"])
            if cur is not None:
                # 토큰이 비어 오면 기존 토큰 유지(마스킹). 단 자기 등록(enroll)으로 URL 이 바뀌면
                # 기존 토큰을 승계하지 않는다 — 승계하면 다음 폴링이 그 토큰을 새 URL 로 보낸다(토큰 유출).
                url_changed = (cur.get("url") or "").rstrip("/") != n["url"]
                if not n["token"] and (keep_token_on_url_change or not url_changed):
                    n["token"] = cur.get("token", "")
                n["last_sync"] = cur.get("last_sync", 0)
                # 복제 주기의 기준일(anchor)은 수정 때 승계 — 예전엔 지역·별칭만 고쳐도 일/주/월 주기의 기준이
                # 저장한 날로 옮겨졌다. 화면이 anchor 를 보내지 않으므로 값이 명시돼 오지 않으면 기존 값 유지.
                if not raw.get("anchor") and cur.get("anchor"):
                    n["anchor"] = cur.get("anchor")
                n["last_poll"] = cur.get("last_poll", 0)
                n["last_ok"] = cur.get("last_ok", 0)
                # 마지막 상태·오류도 승계 — 예전엔 지역만 고쳐도 저장 파일의 상태가 unknown·빈 오류로 초기화됐다
                for _k in ("last_status", "last_error"):
                    if _k in cur and not raw.get(_k):
                        n[_k] = cur.get(_k)
                self.nodes = [n if x["id"] == n["id"] else x for x in self.nodes]
            else:
                self.nodes.append(n)
            self._save()
        return {"ok": True, "node": _public_node(n, self._cache)}

    def set_node_token(self, ids, token: str) -> dict:
        """선택한(또는 전체) 노드의 api_token 을 포탈 저장값에 강제로 맞춘다.

        재부팅 등으로 엣지 api_token 이 새로 생성돼 '토큰 불일치(401)' 로 오프라인이 된
        노드를, 운영자가 아는 엣지의 '현재 토큰' 으로 교정한다. 엣지에는 접근하지 않고
        포탈 로컬(portal_nodes.json)의 token 만 덮어쓴다 — 다음 폴링부터 인증이 통과한다.

        ids 가 비면 전체 노드가 대상(일괄)이고, id 하나만 주면 그 노드만(1개)이다.
        빈 토큰은 엣지 인증을 꺼버리므로(공개) 거부한다.
        """
        token = str(token or "").strip()
        if not token:
            return {"ok": False, "reason": "토큰이 비어 있습니다(빈 토큰은 인증을 끄므로 거부)."}
        if ids and not isinstance(ids, (list, tuple, str)):   # 숫자·참거짓 → 500 이던 것(2026-11 퍼징)
            return {"ok": False, "reason": "ids 는 노드 id 목록이어야 합니다."}
        want = {str(i) for i in ([ids] if isinstance(ids, str) else (ids or []))}
        with self._lock:
            targets = [n for n in self.nodes if (not want or n["id"] in want)]
            if not targets:
                return {"ok": False, "reason": "대상 노드가 없습니다(등록된 노드를 확인하세요)."}
            for n in targets:
                n["token"] = token
            self._save()
            applied = [n["id"] for n in targets]
        auditmod.record(self.data_dir, action="node_token_set", ok=True,
                        detail="%d node(s): %s" % (len(applied), ",".join(applied)))
        return {"ok": True, "count": len(applied), "applied": applied,
                "results": [{"id": i, "ok": True} for i in applied]}

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
            if not authmod.secret_equal(given, want):   # 한글 토큰도 예외 없이(바이트 비교)
                return {"ok": False, "reason": "enroll_token 불일치", "_status": 401}
        elif self._auth.required():
            return {"ok": False, "_status": 401,
                    "reason": "포탈 설정(설치·구성)에서 enroll_token 을 정하고, 엣지 설치 때 --enroll <토큰> 으로 넘기세요."}
        bad = _non_str_field(raw, ("url", "id", "region", "path", "token"))
        if bad:
            return {"ok": False, "reason": "%s 는 문자열이어야 합니다" % bad}
        url = (raw.get("url") or "").strip()
        if not url:
            return {"ok": False, "reason": "url 필요"}
        nid = ((raw.get("id") or "").strip()
               or re.sub(r"[^A-Za-z0-9_.-]+", "-", urlparse(url).hostname or url).strip("-")
               or "edge")
        path = (raw.get("path") or "").strip()
        rec = {"id": nid, "region": (raw.get("region") or "").strip(),
               "url": url, "token": (raw.get("token") or "").strip(),
               "alias_local": path, "alias_logical": path}
        if self._get(nid) is None:         # 새 노드만 기본 주기·모드(재등록은 운영자가 바꾼 값 유지)
            rec.update({"unit": "minute", "every": 30, "mode": "both", "enabled": True})
        res = self.upsert_node(rec, keep_token_on_url_change=False, partial=True)
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
        if not isinstance(csv_text, str) or not csv_text.strip():
            return {"ok": False, "reason": "CSV 내용이 비어 있습니다."}
        csv_text = csv_text.lstrip("\ufeff")   # 엑셀·내보내기 파일의 BOM — 남으면 첫 헤더 'id' 를 못 알아본다
        alias = {
            "id": "id", "name": "id", "node": "id", "이름": "id", "노드": "id",
            "url": "url", "address": "url", "host": "url", "주소": "url",
            "region": "region", "지역": "region",
            "token": "token", "토큰": "token", "api_token": "token",
            "alias_local": "alias_local", "local": "alias_local", "로컬": "alias_local",
            "alias_logical": "alias_logical", "logical": "alias_logical", "논리": "alias_logical",
            "mode": "mode", "enabled": "enabled", "사용": "enabled",
            "unit": "unit", "every": "every", "at": "at", "weekdays": "weekdays",
            "start_month": "start_month", "start_day": "start_day",
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
                    v = (val or "").strip()
                    if len(v) > 1 and v[0] == "'" and v[1] in "=+-@":
                        v = v[1:]          # 내보내기가 수식 주입 방지로 붙인 ' 를 되돌린다(왕복 보존)
                    raw[cols[j]] = v
            # 내보내기 왕복: 사용 여부·주기·요일도 되살린다(예전엔 id/url/지역/별칭만 오가, 꺼 둔 노드가
            # 가져오기 후 기본값=켜짐·30분 주기로 되살아났다 — 2026-11 리뷰)
            if raw.get("weekdays") not in (None, ""):
                raw["weekdays"] = [int(x) for x in re.split(r"[;|\s/]+", raw["weekdays"]) if x.strip().isdigit()]
            else:
                raw.pop("weekdays", None)
            if not raw.get("id") or not raw.get("url"):
                errors.append({"line": line, "error": "id/url 누락"})
                continue
            existed = self._get(raw["id"]) is not None
            res = self.upsert_node(raw, partial=True)
            if res.get("ok"):
                if existed:
                    updated += 1
                else:
                    added += 1
            else:
                errors.append({"line": line, "error": res.get("reason", "등록 실패")})
        return {"ok": True, "added": added, "updated": updated,
                "errors": errors, "total": len(data)}

    def _clean_replica_temps(self) -> None:
        """기동 시 지난 프로세스가 남긴 복제 임시 파일 정리(재시작·execv 업그레이드는 finally 를 안 거친다).
        replicas/tmp*.tar.gz(받던 묶음) · replicas/<노드>/scans/*.part(풀던 DB)."""
        import glob
        for f in (glob.glob(os.path.join(self.replicas_dir, "tmp*.tar.gz"))
                  + glob.glob(os.path.join(self.replicas_dir, "*", "scans", "*.db.part"))):
            try:
                os.remove(f)
            except OSError:
                pass

    def delete_node(self, nid: str) -> dict:
        with self._lock:
            before = len(self.nodes)
            self.nodes = [x for x in self.nodes if x["id"] != nid]
            self._cache.pop(nid, None)
            self._save()
        # 복제본·핑 이력도 정리(베스트 에포트). 실제 있던 노드 + 안전한 id 일 때만 지운다
        # (id=".." 같은 요청이 replicas 밖 data_dir 전체를 rmtree 하던 문제).
        if len(self.nodes) < before and safe_node_id(nid):
            shutil.rmtree(os.path.join(self.replicas_dir, nid), ignore_errors=True)
        try:
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                conn.execute("DELETE FROM ping_samples WHERE node_id=?", (nid,))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": before != len(self.nodes)}

    def test_node(self, raw: dict) -> dict:
        """연결 테스트: url(+token) 또는 등록된 id 로 meta 를 받아 본다."""
        nid = str(raw.get("id") or "").strip()
        n = self._get(nid) if nid else None
        url = str(raw.get("url") or "").strip()
        token = raw.get("token") or None
        if url and not re.match(r"^https?://", url):
            url = "http://" + url
        url = url.rstrip("/")
        if n is not None:
            # 편집 중인 URL 을 테스트한다(예전엔 id 가 있으면 저장된 URL 만 봐서, 고친 주소가 맞는지 확인할 수
            # 없었다 — 2026-11 리뷰). 저장된 토큰은 같은 호스트일 때만 쓴다(다른 주소로 토큰이 새지 않게).
            if not url:
                url = n["url"]
            if not token and urlparse(url).netloc == urlparse(n["url"]).netloc:
                token = n.get("token")
        elif nid and not url:
            return {"ok": False, "reason": "등록되지 않은 노드"}
        if not url:
            return {"ok": False, "reason": "url 필요"}
        try:
            meta = _probe_meta(url, token, timeout=8.0)
            ov = _sanitize_overall(meta.get("overall"))
            return {"ok": True, "version": meta.get("version"),
                    "hostname": meta.get("hostname"),
                    "storages": ov.get("root_count") or 0,
                    "used_bytes": _ov_capacity(ov)}
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            reason = "인증 실패(토큰 확인)" if e.code == 401 else ("HTTP %d" % e.code)
            return {"ok": False, "reason": reason}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)}

    # --- 동기화(폴링 + 복제) ---
    def start(self) -> None:
        if self._thread is None:
            self._record_startup_version()      # 기동/재설치 시 버전 변경을 기록(History)
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
                self._maybe_scheduled_backup(now)
                secs = int(self.settings.get("upgrade_check_secs", 60) or 60)
                if now - self._last_upgrade_check >= max(10, secs):
                    self._last_upgrade_check = now
                    self._check_self_upgrade()
                for n in list(self.nodes):
                    if not n.get("enabled"):
                        continue
                    nid = n["id"]
                    # 폴링과 복제는 각자 따로 돈다(서로의 진행 여부를 보지 않는다).
                    # 예전엔 한 스레드가 폴링→복제를 이어서 하고 _inflight 하나로 묶여 있어,
                    # 큰 완료 DB 복제가 도는 동안 그 노드의 폴링이 멈춰 포탈 상태가 수 시간 묵었다
                    # (엣지는 '완료'인데 포탈은 '스캔 중'·옛 용량).
                    due_poll = (now - n.get("last_poll", 0)) >= self.poll_every
                    with self._lock:
                        start_poll = due_poll and nid not in self._inflight
                        if start_poll:
                            self._inflight.add(nid)
                        try:   # 노드 하나의 비정상 상태가 뒤 노드들의 폴링·복제를 막지 않게(노드별 격리)
                            start_rep = nid not in self._rep_inflight and self._rep_due(n, now)
                        except Exception:  # noqa: BLE001
                            start_rep = False
                        if start_rep:
                            self._rep_inflight.add(nid)
                            self._rep_at[nid] = now
                    if start_poll:
                        threading.Thread(target=self._poll_task, args=(nid,),
                                         daemon=True).start()
                    if start_rep:
                        threading.Thread(target=self._rep_task, args=(nid,),
                                         daemon=True).start()
            except Exception:  # noqa: BLE001 — 스케줄러는 죽지 않는다
                pass
            self._stop.wait(self.tick)

    def _rep_due(self, n: dict, now: float) -> bool:
        """이 노드의 DB 복제를 지금 시작할 차례인가(호출자가 self._lock 보유).

        ① 설정한 복제 주기(schedule_due) — 기준은 '마지막 복제 시도 시각'(_rep_at).
           예전엔 last_sync(=받은 완료 스캔의 finished_at 워터마크)를 기준으로 써서, 마지막
           스캔 완료 후 주기가 한 번 지나면 매 틱(5초)마다 복제가 다시 돌았다.
        ② 폴링에서 새로 완료된 스캔(finished_at > last_sync)을 보면 주기를 기다리지 않고 바로
           — 단 직전 시도 후 REP_RETRY_SECS 는 쉬고, 연속 실패면 2배씩(최대 REP_RETRY_MAX).
        오프라인(마지막 폴링 실패) 노드는 복제하지 않는다(폴링이 먼저 살아나야 함).
        """
        if n.get("mode") not in ("both", "replicate"):
            return False
        nid = n["id"]
        c = self._cache.get(nid) or {}
        if not c.get("online"):
            return False
        last = float(self._rep_at.get(nid, 0.0))
        if setmod.schedule_due(dict(n, last_run=last), now):
            return True
        if not self._has_unreplicated(nid, self._effective_since(nid, float(n.get("last_sync", 0) or 0))[0]):
            return False
        fails = int(self._rep_fail.get(nid, 0))
        gap = min(REP_RETRY_MAX, REP_RETRY_SECS * (2 ** min(fails, 10)))
        return (now - last) >= gap

    def _effective_since(self, nid: str, last_sync: float):
        """복제 워터마크(엣지 시계의 완료 시각)가 엣지의 '지금'보다 미래면 엣지 시계가 되돌아간 것이다(NTP 보정 등).
        그대로 쓰면 그 뒤 스캔이 영영 'since 이전'이라 복제가 조용히 멈췄다(2026-11 연동 점검) — 이때는
        이미 받은 복제본 중 엣지의 지금보다 이른 마지막 완료 시각으로 되돌린다. (워터마크, 되돌렸는지)"""
        c = self._cache.get(nid) or {}
        try:
            edge_now = float(c.get("edge_now") or 0)
        except (TypeError, ValueError, OverflowError):
            edge_now = 0.0
        if not edge_now or last_sync <= edge_now + 300:
            return last_sync, False
        best = 0.0
        try:
            with open(os.path.join(self.replicas_dir, nid, "meta.json"), encoding="utf-8") as fh:
                for sc in (json.load(fh).get("scans") or []):
                    t = float((sc or {}).get("finished_at") or 0)
                    if t <= edge_now and t > best:
                        best = t
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return best, True

    def _has_unreplicated(self, nid: str, wm: float) -> bool:
        """폴링 캐시에 워터마크(wm) 이후 완료된 스캔이 보이는가(호출자가 self._lock 보유)."""
        c = self._cache.get(nid) or {}
        ov = c.get("overall")
        roots = ov.get("roots") if isinstance(ov, dict) else None
        for rt in (roots if isinstance(roots, list) else []):
            # 엣지가 보낸 값이 비정상이어도 예외 없이 건너뛴다 — 여기서 예외가 나면 스케줄러 틱의
            # 노드 루프 전체가 멈춰 그 뒤 노드들의 폴링·복제가 영구히 정지했다.
            if not isinstance(rt, dict) or rt.get("status") not in _TERMINAL:
                continue
            try:
                if float(rt.get("finished_at") or 0) > wm:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False

    def sync_now(self, nid: Optional[str] = None) -> dict:
        targets = [nid] if nid else [n["id"] for n in self.nodes if n.get("enabled")]
        for t in targets:
            threading.Thread(target=self._sync_one, args=(t, True), daemon=True).start()
        return {"ok": True, "syncing": targets}

    def _poll_task(self, nid: str) -> None:
        try:
            self._poll_one(nid)
        finally:
            with self._lock:
                self._inflight.discard(nid)

    def _rep_task(self, nid: str) -> None:
        try:
            self._replicate_one(nid)
        finally:
            with self._lock:
                self._rep_inflight.discard(nid)

    def _sync_one(self, nid: str, do_rep: bool = True) -> None:
        """수동 '동기화'(동기 실행): 폴링 → (온라인이고 do_rep 면) 복제.

        같은 노드의 폴링/복제가 이미 돌고 있으면 그쪽은 건너뛴다(중복 요청 방지).
        """
        with self._lock:
            do_poll = nid not in self._inflight
            if do_poll:
                self._inflight.add(nid)
        if do_poll:
            try:
                online = self._poll_one(nid)
            finally:
                with self._lock:
                    self._inflight.discard(nid)
        else:
            with self._lock:
                online = bool((self._cache.get(nid) or {}).get("online"))
        if not (do_rep and online):
            return  # 오프라인이면 복제 생략
        with self._lock:
            if nid in self._rep_inflight:
                return
            self._rep_inflight.add(nid)
            self._rep_at[nid] = time.time()
        try:
            self._replicate_one(nid)
        finally:
            with self._lock:
                self._rep_inflight.discard(nid)

    def _poll_one(self, nid: str) -> bool:
        """상태 폴링(가벼움): meta.json 만 받아 캐시(온라인·버전·overall)를 갱신. 온라인 여부 반환."""
        n = self._get(nid)
        if n is None:
            return False
        try:
            meta = _probe_meta(n["url"], n.get("token"))
        except Exception as e:  # noqa: BLE001
            emsg = _poll_error_msg(e)
            with self._lock:
                c = self._cache.get(nid, {})
                c.update({"online": False, "error": emsg})
                self._cache[nid] = c
                cur = self._get(nid)   # 폴링 도중 노드가 수정(upsert)됐으면 새 객체에 기록
                if cur is not None:
                    cur["last_poll"] = time.time()
                    cur["last_status"] = "offline"
                    cur["last_error"] = emsg
            return False
        with self._lock:
            self._cache[nid] = {
                "online": True, "ts": time.time(),      # 포탈이 받은 시각(포탈 시계) — 예상 종료 계산의 기준
                "edge_now": meta.get("exported_at"),    # 엣지가 응답을 만든 시각(엣지 시계) — 엣지 시각 차이 계산에만 사용
                "version": meta.get("version"), "hostname": meta.get("hostname"),
                "overall": _sanitize_overall(meta.get("overall")),
                "isilon": meta.get("isilon") if isinstance(meta.get("isilon"), dict) else {"configured": False},
                "storage": [x for x in (meta.get("storage") if isinstance(meta.get("storage"), list) else [])
                            if isinstance(x, dict)],
                "error": "",
            }
            cur = self._get(nid)
            if cur is not None:
                cur["last_poll"] = cur["last_ok"] = time.time()
                cur["last_status"] = "online"
                cur["last_error"] = ""
        self._record_throughput(nid, _sanitize_overall(meta.get("overall")))   # 처리량 시계열(영구)
        return True

    def _replicate_one(self, nid: str) -> None:
        """DB 복제 1회(느린 네트워크 — 락 밖에서). 성공 시 워터마크(last_sync) 전진·저장."""
        n = self._get(nid)
        if n is None or n.get("mode") not in ("both", "replicate"):
            return
        try:
            newest = self._replicate(n)
        except Exception as e:  # noqa: BLE001
            self._clock_rewound.pop(nid, None)   # 실패한 시도의 표시가 다음 성공에 잘못 남지 않게
            with self._lock:
                self._rep_fail[nid] = int(self._rep_fail.get(nid, 0)) + 1
                self._rep_err[nid] = "replicate: " + str(e)
            return
        with self._lock:
            cur = self._get(nid) or n
            if self._clock_rewound.pop(nid, None) is not None:
                cur["last_sync"] = newest       # 미래로 남은 워터마크를 버리고 되돌린 값으로
                self._rep_err[nid] = ("replicate: 엣지 시계가 포탈 기록보다 뒤로 갔습니다(시계 보정?) — "
                                      "복제 기준을 다시 맞췄습니다. 엣지 시계(NTP)를 확인하세요.")
            else:
                cur["last_sync"] = max(float(cur.get("last_sync", 0) or 0), newest)
                self._rep_err.pop(nid, None)
            if self._rep_missing.get(nid):
                self._rep_err[nid] = ("replicate: 스캔 DB %d건을 받지 못함 — 다음 복제에서 다시 시도"
                                      % self._rep_missing[nid])
            if self._rep_missing.get(nid) or self._has_unreplicated(nid, cur["last_sync"]):
                # 받았는데도 워터마크가 그 완료 스캔을 못 넘음 = 엣지가 그 스캔을 보내지 않음(드묾 —
                # 예: 엣지 스캔 목록 최근 200개 밖). 실패처럼 백오프해 60초마다 헛복제하지 않게 한다.
                self._rep_fail[nid] = int(self._rep_fail.get(nid, 0)) + 1
            else:
                self._rep_fail.pop(nid, None)
            self._save()

    @staticmethod
    def _merge_scan_lists(old_scans, new_scans):
        """증분 복제 meta 병합: 과거+신규 스캔 목록을 db_filename(없으면 id) 기준 합침.

        같은 키는 신규가 갱신한다. 증분 응답의 부분 목록으로 과거 스캔이 사라지는 것을 막는다.
        """
        merged = {}
        now = time.time()
        for s in list(old_scans or []):
            k = s.get("db_filename") or s.get("id")
            if k is not None:
                merged[k] = s
        for s in list(new_scans or []):
            k = s.get("db_filename") or s.get("id")
            if k is None:
                continue
            s = dict(s)
            # 포탈이 처음 받은 시각(포탈 시계) — '최신' 판단에 엣지 완료 시각보다 우선한다. 엣지 시계가 되돌아가면
            # 옛 스캔의 완료 시각이 새 스캔보다 미래라, 보관 정리·비교가 새 스캔을 버리고 옛 것을 남겼다(2026-11 연동 점검).
            # 이미 있던 스캔은 받은 시각을 새로 찍지 않는다 — 이 기능 전 복제본(received_at 없음)이 다시 오면
            # '지금'으로 찍혀 더 새 스캔을 앞지르던 회귀(12회차 점검). 처음 보는 스캔만 지금 시각.
            prev = merged.get(k)
            s["received_at"] = ((prev or {}).get("received_at") or 0) if prev is not None else now
            merged[k] = s
        return list(merged.values())

    @staticmethod
    def _scan_recency(s) -> tuple:
        """복제 스캔의 '최신' 순서: 포탈이 받은 시각(없으면 0 — 이 기능 전 복제본) → 엣지 완료 시각."""
        def _f(v):
            try:
                return float(v or 0)
            except (TypeError, ValueError, OverflowError):
                return 0.0
        return (_f(s.get("received_at")), _f(s.get("finished_at")))

    @staticmethod
    def _prune_replica_scans(dest: str, scans: list, keep: int) -> list:
        """복제본 보관: 루트별 최신 keep 개만 남기고 나머지 DB 파일을 지운다(keep<=0 이면 무제한).

        예전엔 복제본을 지우지 않아 반복 스캔마다 HQ 디스크가 끝없이 늘었다(2026-11 리뷰).
        최신 완료(done) 스캔은 keep 밖이어도 항상 남긴다 — 비교·용량의 기준이다."""
        if keep <= 0:
            return scans
        by_root: dict = {}
        for sc in scans:
            by_root.setdefault(sc.get("root_path") or "", []).append(sc)
        kept = []
        for items in by_root.values():
            items.sort(key=PortalController._scan_recency, reverse=True)
            keep_set = items[:keep]
            done = [x for x in items if x.get("status") == "done"]
            if done and done[0] not in keep_set:
                keep_set = keep_set[:-1] + [done[0]] if keep > 1 else [done[0]]
            ids = {id(x) for x in keep_set}
            for x in items:
                if id(x) in ids:
                    kept.append(x)
                    continue
                dbf = os.path.basename(x.get("db_filename") or x.get("db_path") or "")
                if not dbf or dbf in {os.path.basename(k.get("db_filename") or "") for k in keep_set}:
                    continue
                for sfx in ("", "-wal", "-shm"):
                    try:
                        os.remove(os.path.join(dest, "scans", dbf + sfx))
                    except OSError:
                        pass
        return kept

    def _replicate(self, n: dict) -> float:
        """완료 DB(증분) + meta.json 을 replicas/<id>/ 로 안전하게 내려받는다."""
        since, rewound = self._effective_since(n["id"], float(n.get("last_sync", 0) or 0))
        if rewound:
            self._clock_rewound[n["id"]] = since
        # since = '이미 받은 완료 스캔의 finished_at' 워터마크(소수 초). 예전처럼 정수로 자르면
        # (int) 엣지의 'finished_at > since' 가 그 스캔에도 참이 되어, 마지막 완료 DB 를 복제할
        # 때마다 통째로 다시 받았다(대용량이면 수 GB 를 끝없이). repr 은 실수를 정확히 왕복한다.
        url = n["url"].rstrip("/") + "/api/dbexport?since=%s" % repr(since)
        dest = os.path.join(self.replicas_dir, n["id"])
        os.makedirs(os.path.join(dest, "scans"), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=self.replicas_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                resp = _http_get(url, n.get("token"), timeout=300.0)
                shutil.copyfileobj(resp, f)
            newest = since              # (엣지 시계가 되돌아갔으면 되돌린 기준에서 다시 센다)
            new_meta = None
            tf = tarfile.open(tmp, "r:gz")
            try:
                for m in tf:
                    if not m.isfile():
                        continue
                    name = m.name.lstrip("/")
                    if name == "meta.json":
                        # 바로 덮어쓰지 않고 메모리에 읽어둔다(아래에서 기존 meta 와 병합).
                        src = tf.extractfile(m)
                        if src is not None:
                            try:
                                new_meta = json.loads(src.read().decode("utf-8"))
                            except (ValueError, OSError):
                                new_meta = None
                        continue
                    elif (name.startswith("scans/") and name.endswith(".db")
                          and "/" not in name[6:] and "\\" not in name[6:]
                          and name[6:] not in (".", "..")):   # 이름 속 '..'(예: a..b.db)는 정상 — 경로 성분만 거부
                        outp = os.path.join(dest, "scans", os.path.basename(name))
                    else:
                        continue  # 경로 탈출 방지: 그 외 멤버는 무시
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    # 임시 파일에 다 받은 뒤 교체(원자적) — 스트림이 끊겨 tar 가 EOF 로 깨져도 기존 정상
                    # 복제본(같은 이름 재전송: paused→done)이 잘린 파일로 남지 않는다.
                    part = outp + ".part"
                    try:
                        with open(part, "wb") as w:
                            shutil.copyfileobj(src, w)
                        for sfx in ("-wal", "-shm"):   # 옛 DB 의 WAL 이 새 파일에 재생되지 않게
                            try:
                                os.remove(outp + sfx)
                            except OSError:
                                pass
                        os.replace(part, outp)
                    finally:
                        try:
                            os.remove(part)
                        except OSError:
                            pass
            finally:
                tf.close()
            # 증분 복제: 엣지가 보내는 meta.json 의 scans 는 'since 이후 완료분'만 담겨온다.
            # 통째로 덮어쓰면 과거 스캔이 목록에서 사라져 비교 화면이 조용히 깨진다(디스크엔 .db 남음).
            # → 기존 meta 의 scans 와 db_filename 기준으로 '병합'해 과거 목록을 보존한다.
            missing_at = []        # meta 에는 있는데 DB 파일이 안 온 스캔의 완료 시각(워터마크가 넘지 않게)
            if new_meta is not None:
                got = []
                for sc in (new_meta.get("scans") or []):
                    dbf = os.path.basename(str((sc or {}).get("db_filename") or ""))
                    if dbf and os.path.exists(os.path.join(dest, "scans", dbf)):
                        got.append(sc)
                    else:
                        try:
                            missing_at.append(float((sc or {}).get("finished_at") or 0))
                        except (TypeError, ValueError, OverflowError):
                            pass
                new_meta["scans"] = got
                old_scans = []
                try:
                    with open(os.path.join(dest, "meta.json"), encoding="utf-8") as fh:
                        old_scans = (json.load(fh).get("scans") or [])
                except (OSError, ValueError):
                    old_scans = []
                new_meta["scans"] = self._prune_replica_scans(
                    dest, self._merge_scan_lists(old_scans, new_meta.get("scans")),
                    int(self.settings.get("replica_keep_per_root", 5) or 0))
                tmp_meta = os.path.join(dest, "meta.json.tmp")
                with open(tmp_meta, "w", encoding="utf-8") as w:
                    json.dump(new_meta, w, ensure_ascii=False)
                os.replace(tmp_meta, os.path.join(dest, "meta.json"))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # meta 의 완료 시각으로 last_sync 전진 — 단 DB 가 안 온 스캔은 넘지 않는다(다음 복제에서 다시 받게)
        base = since
        try:
            with open(os.path.join(dest, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            for s in meta.get("scans", []):
                newest = max(newest, float(s.get("finished_at") or 0))
        except (OSError, ValueError):
            pass
        if rewound:
            # 되돌아간 엣지 시계보다 미래로 찍힌 옛 스캔이 워터마크를 다시 미래로 끌어올리지 않게 엣지의 '지금'에서 자른다
            try:
                newest = min(newest, float((self._cache.get(n["id"]) or {}).get("edge_now") or newest))
            except (TypeError, ValueError, OverflowError):
                pass
        miss = sorted(t for t in missing_at if t > base)
        if miss and self._rep_missing_prev.get(n["id"]) != miss:
            # 처음 보는 누락 — 워터마크를 그 앞에 멈추고 다음 복제에서 다시 받는다(일시적 오류 대비)
            newest = max(base, min(newest, miss[0] - 1e-6))
            self._rep_missing[n["id"]] = len(miss)     # _replicate_one 이 오류로 표시하고 백오프 후 다시 받는다
            self._rep_missing_prev[n["id"]] = miss
        else:
            # 같은 누락이 다시 오면(구버전 엣지가 DB 없는 스캔을 계속 싣는 경우) 그 스캔은 건너뛰고 진행한다 —
            # 안 그러면 매번 그 뒤의 모든 DB 를 다시 받는다(2026-11 회귀 점검).
            if miss:
                newest = max(newest, miss[-1])
            self._rep_missing.pop(n["id"], None)
            self._rep_missing_prev.pop(n["id"], None)
        return newest

    # --- 글로벌 롤업 ---
    @staticmethod
    def _fs_capacity(roots_l):
        """노드의 파일시스템 사용/전체 용량 — 같은 파일시스템은 한 번만 센다.

        같은 FS 판별: ① 엣지가 보낸 마운트 출처(fs_source, 예: isilon01:/ifs/data)가 같으면 같은 FS
        ② 출처를 모르거나 다르면 전체 용량이 거의 같고(0.1% 또는 4MiB 이내) 사용량도 비슷할 때(전체의 2% 이내)만
        같은 FS 로 본다. 예전엔 '전체 용량이 같으면 같은 FS' 하나로, 크기만 같은 서로 다른 볼륨(예: 같은 크기
        쿼터 export 둘)을 하나로 합치고(100TB+100TB → 100TB), 같은 FS 가 몇 KB 다르게 보고되면 두 번 셌다.
        (아이실론 한 클러스터를 여러 export·노드 IP 로 마운트하면 출처는 달라도 전체·사용량이 같아 ②로 합쳐진다.)
        """
        clusters = []   # [{"total", "used", "srcs"}]
        for rt in roots_l:
            if not isinstance(rt, dict):
                continue
            try:
                ftot = int(rt.get("fs_total_bytes") or 0)
                fused = int(rt.get("fs_used_bytes") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if ftot <= 0:
                continue
            src = str(rt.get("fs_source") or "").strip()
            hit = None
            if src:
                hit = next((c for c in clusters if src in c["srcs"]), None)
            if hit is None:
                tol_t = max(4 * 1024 * 1024, ftot * 0.001)
                hit = next((c for c in clusters
                            if abs(c["total"] - ftot) <= tol_t and abs(c["used"] - fused) <= ftot * 0.02), None)
            if hit is None:
                clusters.append({"total": ftot, "used": fused, "srcs": {src} if src else set()})
            else:
                hit["total"] = max(hit["total"], ftot)
                hit["used"] = max(hit["used"], fused)
                if src:
                    hit["srcs"].add(src)
        ftot = sum(c["total"] for c in clusters)
        fused = sum(c["used"] for c in clusters)
        pct = round(fused / ftot * 100, 1) if ftot else None
        return ftot, fused, max(0, ftot - fused), pct

    @staticmethod
    def _edge_to_portal_clock(rt: dict, recv_ts=None, edge_now=None) -> dict:
        """엣지 시계로 찍힌 절대 시각(시작·갱신·완료)을 포탈 시계로 옮긴다 — 엣지 시계가 3시간 늦으면 방금 끝난
        스캔이 '3시간 전'으로 보였다(2026-11 리뷰). 차이 = 포탈이 받은 시각 − 엣지가 응답을 만든 시각(전송 지연 포함,
        보통 1초 미만). edge_now 가 없는 구버전 엣지는 그대로. 원본(캐시)은 바꾸지 않는다."""
        try:
            skew = float(recv_ts) - float(edge_now) if (recv_ts and edge_now) else 0.0
        except (TypeError, ValueError, OverflowError):
            skew = 0.0
        # 응답 지연(바쁜 엣지는 수 초~수십 초)이 시계 차이로 오인되지 않게 1분 미만은 손대지 않는다
        if abs(skew) < 60 or not isinstance(rt, dict):
            return rt
        out = dict(rt)
        for k in ("started_at", "updated_at", "finished_at", "partial_agg_at"):
            v = out.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[k] = v + skew
        return out

    @staticmethod
    def _with_eta(rt: dict, recv_ts=None, edge_now=None) -> dict:
        """진행 중 루트의 남은 시간·끝날 예상 시각을 **포탈(메인) 시계 기준**으로 만든다. 원본(캐시)은 바꾸지 않는다.

        엣지마다 시계(시간대·오차)가 달라, 엣지가 보낸 절대 시각(eta_at 등)은 쓰지 않는다. 엣지에서는 상대 시간만 쓴다:
          - 1.99.45+ 엣지: eta_seconds = 엣지가 응답을 만든 순간 기준 남은 초
          - 구버전 엣지: 같은 공용 계산(manager.estimate_eta_seconds)을 경과(updated_at - started_at, 둘 다 엣지 시계라
            차이는 오차와 무관)로 하고, 갱신 뒤 응답까지 흐른 시간(exported_at - updated_at, 역시 엣지 시계 차이)을 뺀다.
        그 남은 초를 포탈이 응답을 받은 시각(recv_ts, 포탈 시계)에 더해 eta_at(포탈 시계)을 만든다."""
        if rt.get("status") not in ("discovering", "sizing"):
            return rt
        out = dict(rt)
        out["eta_at"] = None
        out["eta_seconds"] = None
        try:
            recv = float(recv_ts) if recv_ts else None
        except (TypeError, ValueError, OverflowError):
            recv = None
        left = None
        if rt.get("eta_seconds") is not None:              # 엣지가 계산한 남은 초(응답 순간 기준)
            try:
                left = max(0.0, float(rt["eta_seconds"]))
            except (TypeError, ValueError, OverflowError):
                left = None
        else:                                              # 구버전 엣지 — 포탈이 같은 계산으로 추정
            try:
                st = float(rt.get("started_at") or 0)
                up = float(rt.get("updated_at") or 0)
            except (TypeError, ValueError, OverflowError):
                st = up = 0.0
            if st > 0 and up > st:
                eta = mgrmod.estimate_eta_seconds(rt.get("phase"), rt.get("processed_dirs"), rt.get("total_dirs"),
                                                  rt.get("discovered_dirs"), up - st)
                if eta is not None:
                    try:
                        lag = max(0.0, float(edge_now) - up) if edge_now else 0.0
                    except (TypeError, ValueError, OverflowError):
                        lag = 0.0
                    left = max(0.0, eta - lag)
                    out["eta_by_portal"] = True
        if left is not None and recv is not None:
            out["eta_seconds"] = left                      # 포탈이 받은 순간 기준 남은 초
            out["eta_at"] = recv + left                    # 포탈 시계 기준 끝날 시각
        return out

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
            # 끝날 예상 시각(epoch) — 여러 개가 돌면 가장 늦게 끝날 것. 엣지가 같은 함수로 계산해 보낸 값
            # (구버전 엣지는 없음 → None, 화면에서 표시 안 함)
            "eta_at": max((rt["eta_at"] for rt in act if rt.get("eta_at") is not None), default=None),
        }

    def overview(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes_snapshot = list(self.nodes)
            rep_err = dict(self._rep_err)
        regions = {}
        total_used = 0
        total_fs_total = 0
        total_fs_used = 0
        storages = 0
        online = 0
        active = 0
        out_nodes = []
        running_all = []   # 전체 엣지의 진행 중 스캔(노드·경로·진행·끝날 예상 시각) — 포탈 현황 '진행 중인 스캔' 표
        for n in nodes_snapshot:
            try:
                c = cache.get(n["id"], {})
                ov = c.get("overall") or {}
                used = _ov_capacity(ov)
                st = int(ov.get("root_count") or 0)
                # 비활성(운영자가 끈) 노드는 폴링하지 않아 캐시가 옛 값으로 남는다 — 온라인·진행 중으로 세지 않는다
                # (예전엔 끄기 전에 받은 '스캔 중'이 다시 켤 때까지 합계·진행 목록에 남았다, 2026-11 연동 점검)
                is_on = bool(c.get("online")) and bool(n.get("enabled", True))
                if is_on:
                    online += 1
                    # 끊긴 노드의 '진행 중'은 끊기기 전 옛 값 — 합계·진행 표·완료 예상에 넣지 않는다(표와 KPI 일치)
                    active += int(ov.get("active_scans") or 0)
                total_used += used
                storages += st
                reg = n.get("region") or "(미지정)"
                r = regions.setdefault(reg, {"region": reg, "used_bytes": 0, "storages": 0,
                                             "fs_used_bytes": 0, "fs_total_bytes": 0})
                r["used_bytes"] += used
                r["storages"] += st
                roots_l = [self._edge_to_portal_clock(self._with_eta(rt, c.get("ts"), c.get("edge_now")),
                                                      c.get("ts"), c.get("edge_now"))
                           for rt in (ov.get("roots") or [])]
                if is_on:
                    for rt in roots_l:
                        if rt.get("status") in ("discovering", "sizing"):
                            running_all.append({
                                "node": n["id"], "region": n.get("region") or "", "root_path": rt.get("root_path"),
                                "status": rt.get("status"), "phase": rt.get("phase"),
                                "processed_dirs": rt.get("processed_dirs") or 0, "total_dirs": rt.get("total_dirs") or 0,
                                "discovered_dirs": rt.get("discovered_dirs") or 0,
                                "scanned_bytes": rt.get("scanned_bytes") or 0,
                                "started_at": rt.get("started_at"), "updated_at": rt.get("updated_at"),
                                "eta_at": rt.get("eta_at"), "eta_by_portal": bool(rt.get("eta_by_portal")),
                                "eta_basis": rt.get("eta_basis"),
                            })
                last_scan = max([0] + [max(rt.get("finished_at") or 0, rt.get("updated_at") or 0)
                                       for rt in roots_l])     # 그 DC 가 마지막으로 스캔한 시각
                fs_total, fs_used, fs_free, fs_pct = self._fs_capacity(roots_l)
                total_fs_total += fs_total
                total_fs_used += fs_used
                r["fs_used_bytes"] += fs_used      # 지역별 디스크 사용량/전체 용량 집계
                r["fs_total_bytes"] += fs_total
                out_nodes.append({
                    "id": n["id"], "region": n.get("region"), "url": n["url"],
                    "enabled": n.get("enabled"), "online": is_on,
                    "version": c.get("version"), "hostname": c.get("hostname"),
                    "last_poll": n.get("last_poll"), "last_ok": n.get("last_ok"), "last_sync": n.get("last_sync"),
                    "last_scan_at": last_scan or None,
                    # 폴링 오류 우선, 없으면 마지막 복제 오류(다음 복제 성공 전까지 유지)
                    "last_error": n.get("last_error") or c.get("error") or rep_err.get(n["id"], ""),
                    "used_bytes": used, "storages": st,
                    "active_scans": int(ov.get("active_scans") or 0) if is_on else 0,
                    "fs_total_bytes": fs_total, "fs_used_bytes": fs_used,
                    "fs_free_bytes": fs_free, "fs_used_pct": fs_pct,
                    "running": self._running_scan(roots_l) if is_on else None,
                    "auto_restart": any(rt.get("auto_restart") for rt in roots_l),  # 반복 스캔 중
                    "roots": roots_l,
                    "isilon": c.get("isilon") or {"configured": False},
                    "storage": c.get("storage") or [],
                })
            except Exception as exc:  # noqa: BLE001 — 노드 하나의 이상 값이 전체 화면을 멈추지 않게
                out_nodes.append({"id": n.get("id"), "region": n.get("region"), "url": n.get("url"),
                                  "enabled": n.get("enabled"), "online": False, "roots": [],
                                  "storage": [], "isilon": {"configured": False},
                                  "last_error": "집계 오류: %s" % exc})

        return {
            "ok": True,
            "version": __version__,
            "totals": {"used_bytes": total_used, "storages": storages,
                       "fs_total_bytes": total_fs_total, "fs_used_bytes": total_fs_used,
                       "nodes_online": online, "nodes_total": len(nodes_snapshot),
                       "active_scans": active,
                       # 전체 엣지 중 가장 늦게 끝날 예상 시각(진행 중이고 계산 가능한 것만)
                       "eta_at": max((x["eta_at"] for x in running_all if x.get("eta_at")), default=None),
                       "running_no_eta": sum(1 for x in running_all if not x.get("eta_at"))},
            # 끝나는 순서(예상 시각이 없는 것 — 폴더 찾기 중 — 은 뒤로)
            "running": sorted(running_all, key=lambda x: (x.get("eta_at") is None, x.get("eta_at") or 0)),
            "regions": sorted(regions.values(), key=lambda x: -x["used_bytes"]),
            "nodes": out_nodes,
            # 화면이 포탈 시계·시간대로 남은 시간·완료 시각을 그리도록(브라우저 PC 시계가 틀려도 보정)
            "server_time": time.time(),
            "tz_offset_min": int(time.localtime().tm_gmtoff // 60),
        }

    def usage_export(self) -> dict:
        """집계 사용량을 외부 서버용 '안정 스키마' JSON 으로 제공한다(읽기 전용 API).

        내부 UI 응답(overview)과 달리 스키마를 고정한 외부 연동용이다. url·last_error 등
        내부 필드는 빼고, 용량·노드·루트의 핵심만 추린다.
        """
        ov = self.overview()
        t = ov.get("totals") or {}
        nodes = []
        for n in ov.get("nodes") or []:
            roots = [{
                "root_path": r.get("root_path"),
                # 용량 = 마지막 완료 스캔(멈춤·오류·진행 중 부분 값 아님). capacity_partial=완료 스캔이 아직 없음
                "scanned_bytes": _root_capacity(r),
                "capacity_partial": bool(r.get("capacity_partial")),
                "fs_total_bytes": int(r.get("fs_total_bytes") or 0),
                "fs_used_bytes": int(r.get("fs_used_bytes") or 0),
                "total_files": int(r.get("total_files") or 0),
                "status": r.get("status"),
                "scan_id": r.get("scan_id"),
                "finished_at": r.get("finished_at"),
            } for r in (n.get("roots") or [])]
            nodes.append({
                "id": n.get("id"), "region": n.get("region"),
                "hostname": n.get("hostname"), "online": bool(n.get("online")),
                "scanned_bytes": int(n.get("used_bytes") or 0),
                "fs_total_bytes": int(n.get("fs_total_bytes") or 0),
                "fs_used_bytes": int(n.get("fs_used_bytes") or 0),
                "last_scan_at": n.get("last_scan_at"),
                "roots": roots,
            })
        return {
            "ok": True,
            "schema": "isilon_usage.usage/v1",
            "generated_at": time.time(),
            "portal_version": __version__,
            "totals": {
                "nodes_total": int(t.get("nodes_total") or 0),
                "nodes_online": int(t.get("nodes_online") or 0),
                "storages": int(t.get("storages") or 0),
                "scanned_bytes": int(t.get("used_bytes") or 0),
                "fs_total_bytes": int(t.get("fs_total_bytes") or 0),
                "fs_used_bytes": int(t.get("fs_used_bytes") or 0),
                "active_scans": int(t.get("active_scans") or 0),
            },
            "regions": [{"region": r.get("region"),
                         "scanned_bytes": int(r.get("used_bytes") or 0),
                         "storages": int(r.get("storages") or 0)}
                        for r in (ov.get("regions") or [])],
            "nodes": nodes,
        }

    def set_export_token(self, token: str) -> dict:
        """외부 사용량 API(/api/portal/usage) 보호 토큰을 설정/해제한다(빈 값=공개)."""
        self.settings["export_token"] = str(token or "").strip()
        self._save_settings()
        return {"ok": True, "export_token_set": bool(self.settings["export_token"])}

    def set_session_prefs(self, body) -> dict:
        """로그인 세션 유지 시간(분)·업데이트 팝업 표시·로그 팝업 위치를 저장한다."""
        if "op_ttl_minutes" in body:
            try:
                self.settings["op_ttl_minutes"] = max(1, min(10080, int(body.get("op_ttl_minutes"))))
            except (TypeError, ValueError, OverflowError):
                # 잘못된 값이면 저장하지 않는다 — 예전엔 120 이던 값이 조용히 30 으로 바뀌고 ok 를 돌려줬다
                return {"ok": False, "reason": "세션 유지 시간(분)은 1~10080 사이 숫자여야 합니다."}
        if "show_update_popup" in body:
            self.settings["show_update_popup"] = _truthy(body.get("show_update_popup"))
        if "log_popup_pos" in body:
            # 로그 팝업(업그레이드 진행/전 노드 푸시/기록 세부) 앵커: 중앙 또는 네 코너
            v = str(body.get("log_popup_pos") or "center").strip().lower()
            self.settings["log_popup_pos"] = v if v in ("center", "tl", "tr", "bl", "br") else "center"
        self._save_settings()
        return {"ok": True, "op_ttl_minutes": self.settings.get("op_ttl_minutes"),
                "show_update_popup": self.settings.get("show_update_popup"),
                "log_popup_pos": self.settings.get("log_popup_pos", "center")}

    def export_token_ok(self, given) -> bool:
        """export_token 이 설정돼 있으면 일치해야 True(미설정이면 공개=True)."""
        tok = (self.settings.get("export_token") or "").strip()
        if not tok:
            return True
        return authmod.secret_equal(given or "", tok)

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
                entry["disabled"] = True
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

    # --- 처리량 히스토리(노드별 scanned_bytes 시계열 · DB 영구 저장 · 프루닝 없음) ---
    def _init_throughput_db(self) -> None:
        try:
            conn = dbmod.connect(throughput_history_path(self.data_dir))
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS throughput_samples ("
                             "ts INTEGER NOT NULL, node_id TEXT NOT NULL, "
                             "scanned_bytes INTEGER NOT NULL DEFAULT 0, "
                             "files INTEGER NOT NULL DEFAULT 0)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_thru_node_ts "
                             "ON throughput_samples(node_id, ts)")
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _record_throughput(self, nid: str, overall: dict) -> None:
        """노드의 현재 누적 조사량(scanned_bytes)·파일 수를 DB에 영구 적재.

        값이 바뀔 때만 기록한다(유휴 노드는 행이 안 쌓임 → 용량 절약). 프루닝 없음.
        """
        try:
            scanned = int((overall or {}).get("total_scanned_bytes") or 0)
            files = sum(int(r.get("total_files") or 0) for r in ((overall or {}).get("roots") or []))
        except (TypeError, ValueError, AttributeError):
            return
        if self._thru_last.get(nid) == scanned:   # 변화 없으면 적재 안 함
            return
        self._thru_last[nid] = scanned
        try:
            conn = dbmod.connect(throughput_history_path(self.data_dir))
            try:
                conn.execute("INSERT INTO throughput_samples(ts,node_id,scanned_bytes,files) "
                             "VALUES (?,?,?,?)", (int(time.time()), nid, scanned, files))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    # 윈도우(초): 1분·10분·30분·1시간·10시간·1일
    THRU_WINDOWS = (60, 600, 1800, 3600, 36000, 86400)

    def throughput_windows(self) -> dict:
        """노드(법인)별 시간당 처리량 + 윈도우별(최근 1분~1일) 처리량.

        헤드라인(since_start)은 라이브 overview(시작~지금 평균), 윈도우는 영구 DB의
        scanned_bytes 시계열에서 '구간 내 증가분'을 합산(스캔 재시작/리셋은 음수라 무시).
        """
        now = time.time()
        # 1) 라이브: 노드별 누적 조사량 + 진행 중 스캔 시작시각
        live = {}
        for n in (self.overview().get("nodes") or []):
            roots = n.get("roots") or []
            # 시작 이후 시간당 = '진행 중' 루트마다 (그 스캔이 지금까지 센 양 ÷ 그 스캔의 경과)의 합.
            # 예전엔 모든 루트(완료 포함)의 누적량을 가장 이른 진행 시작 이후 시간으로 나눠, 순차 스캔에서
            # 51만 배까지 부풀었다. 경과는 엣지 시계(updated_at - started_at)로 — 포탈·엣지 시계 차이 영향 없음.
            bph = 0.0
            starts = []
            for r in roots:
                # 끊긴(offline) 노드의 roots 는 마지막으로 받은 옛 값 — '진행 중'으로 세면 죽은 노드가 스캔 중·
                # 거대한 처리량으로 표 맨 위에 떴다(2026-11 리뷰; overview 의 active/running 과 같은 규칙).
                if not n.get("online") or r.get("status") not in ("discovering", "sizing"):
                    continue
                try:
                    st = float(r.get("started_at") or 0)
                    up = float(r.get("updated_at") or 0) or now
                    sb = float(r.get("scanned_bytes") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if st <= 0:
                    continue
                starts.append(st)
                bph += sb / max(1.0, up - st) * 3600.0
            live[n["id"]] = {"region": n.get("region"), "hostname": n.get("hostname"),
                             "bph": bph, "start": (min(starts) if starts else None),
                             "scanning": bool(starts)}
        # 2) 히스토리: 최근 1일치 샘플(노드별)
        by_node = {}
        try:
            conn = dbmod.connect(throughput_history_path(self.data_dir))
            try:
                cur = conn.execute(
                    "SELECT node_id, ts, scanned_bytes, files FROM throughput_samples "
                    "WHERE ts >= ? ORDER BY node_id, ts", (int(now) - max(self.THRU_WINDOWS) - 120,))
                for nid, ts, sb, fl in cur.fetchall():
                    by_node.setdefault(nid, []).append((ts, sb, fl))
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            by_node = {}
        out = []
        for nid in {x["id"] for x in list(self.nodes)}:
            lv = live.get(nid, {})
            samples = by_node.get(nid, [])
            wins = {}
            for w in self.THRU_WINDOWS:
                cutoff = now - w
                db = df = 0
                for i in range(1, len(samples)):
                    (t1, b1, f1), (t2, b2, f2) = samples[i - 1], samples[i]
                    if t2 >= cutoff and t2 > t1:     # 구간 내 증가분만(리셋=음수는 제외)
                        # 윈도우와 겹치는 비율만큼만 — 공백(포탈 재시작·엣지 오프라인) 뒤 첫 샘플이
                        # 공백 동안의 증가분 전체를 짧은 윈도우에 몰아 넣어 처리량이 폭증하던 문제.
                        frac = (t2 - max(t1, cutoff)) / float(t2 - t1)
                        if b2 > b1:
                            db += (b2 - b1) * frac
                        if f2 > f1:
                            df += (f2 - f1) * frac
                db, df = int(db), int(df)
                wins[str(w)] = {"bytes": db, "files": df,
                                "bph": db / w * 3600.0, "fph": df / w * 3600.0}
            ss = None
            if lv.get("start") and lv.get("bph"):
                ss = lv["bph"]
            out.append({"id": nid, "region": lv.get("region") or "",
                        "hostname": lv.get("hostname") or "", "scanning": lv.get("scanning", False),
                        "since_start_bph": ss, "win": wins})
        out.sort(key=lambda x: -(x["since_start_bph"] or 0))
        return {"ok": True, "now": now, "windows": list(self.THRU_WINDOWS), "nodes": out}

    def _record_ping(self, results: list) -> None:
        # 비활성(점검 등으로 끈) 노드는 재지 않았으므로 기록하지 않는다 — 예전엔 '다운'으로 쌓여
        # 가용률·장애 이력이 거짓으로 나빠졌다(2026-11 리뷰).
        rows = [(int(time.time()), r["id"], r.get("latency_ms"), 1 if r.get("online") else 0)
                for r in (results or []) if r.get("id") and not r.get("disabled")]
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
            ids = [x["id"] for x in list(self.nodes)]
            conn = dbmod.connect(ping_history_path(self.data_dir))
            try:
                conn.execute("DELETE FROM ping_samples WHERE ts < ?",
                             (int(time.time()) - 366 * 86400,))
                if ids:           # 등록 노드가 아닌(삭제/개명된) 옛 id 의 샘플 정리(쓰레기 수거)
                    ph = ",".join("?" * len(ids))
                    conn.execute("DELETE FROM ping_samples WHERE node_id NOT IN (%s)" % ph, ids)
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
            if nid not in reg:        # 등록 노드가 아닌(삭제/개명된) 옛 id 의 이력은 표시하지 않음
                continue
            vals = [p["ms"] for p in series if p["ms"] is not None]
            med = round(statistics.median(vals), 2) if vals else None
            out.append({"id": nid, "region": reg.get(nid) or "", "median": med,
                        "series": series})
        out.sort(key=lambda x: (x["region"] or "~", x["id"]))
        return {"ok": True, "range": range_sec, "bucket": bucket, "nodes": out,
                "server_time": time.time()}   # 화면이 차트 시간축을 포탈 시계로 맞춘다(이 PC 시계가 틀려도)

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

    @staticmethod
    def _http_err_reason(e) -> str:
        """엣지가 보낸 거절 사유(JSON reason/error)를 읽는다 — 예전엔 버리고 'HTTP 400' 만 보였다(2026-11 원격 조작 점검)."""
        try:
            j = json.loads(e.read(65536).decode("utf-8", "replace") or "{}")
            if isinstance(j, dict):
                return str(j.get("reason") or j.get("error") or "")[:500]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _edge_post(self, node, path, payload, *, timeout=20) -> dict:
        """엣지로 POST(api_token 인증). HTTP 코드별 사유를 정리해 반환한다."""
        url = node["url"].rstrip("/") + path
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        if node.get("token"):
            req.add_header("X-Auth-Token", node["token"])
        try:
            resp = netutil.urlopen(req, timeout=timeout)
            return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_reason = self._http_err_reason(e)
            if body_reason and e.code not in (401, 404):   # 401 은 아래 안내가 더 쓸모 있음
                return {"ok": False, "reason": body_reason, "http": e.code}
            reason = (
                "엣지에 로그인 비밀번호가 설정돼 포탈에서 직접 시작할 수 없습니다 — 엣지 화면에서 시작하세요."
                if e.code == 401 else
                "엣지가 웹 스캔을 비활성화했거나 거부했습니다." if e.code == 403 else
                "엣지에 해당 API 가 없습니다(구버전 엣지 — 업그레이드 필요)." if e.code == 404 else
                "HTTP %d" % e.code)
            return {"ok": False, "reason": reason}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)}

    # --- 원격 스캔 시작(마지막 스캔과 동일 경로) ---
    def node_scan(self, node_id: str, *, one_file_system=None,
                  autotune=False, restart_busy=False, engine=None,
                  fold_depth=None) -> dict:
        """온라인·미스캔 노드를 '마지막 스캔과 동일한 경로'로 다시 스캔 시작한다.

        마지막 루트는 캐시(overall.roots)에서 scan_id 가 가장 큰(=최근) 루트의 root_path.
        옵션:
          - one_file_system: -x(한 파일시스템)
          - autotune: 엣지가 최적 엔진을 자동 측정(autotune/start) 후 본 스캔
          - restart_busy: 이미 스캔 중이면 멈추고 다시 시작(끄면 진행 중 노드는 건너뜀).
            pscan(멀티프로세스)은 중간 중지가 불가하므로 그런 노드는 start 가 거부돼 사유로 남는다.
        엣지가 로그인 비밀번호로 보호돼 있으면 포탈엔 그 토큰이 없어 시작할 수 없다(사유 반환).
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
            return {"ok": False, "reason": "이전 스캔 기록이 없습니다(먼저 한 번 스캔)."}
        busy = [r for r in roots if r.get("status") in ("discovering", "sizing")]
        if busy:
            if not restart_busy:
                return {"ok": False, "reason": "이미 스캔 진행 중(건너뜀)."}
            # 진행 중 스캔을 멈추고 재시작 — 각 진행 스캔에 stop 푸시(베스트에포트).
            stopped = 0
            for r in busy:
                sid = int(r.get("scan_id") or 0)
                if sid and self._edge_post(
                        node, "/api/scan/stop", {"scan_id": sid}).get("ok"):
                    stopped += 1
            if stopped:
                time.sleep(1.0)   # 워커가 stop 신호를 받고 멈출 시간(그 뒤 재시작)
        last = max(roots, key=lambda r: int(r.get("scan_id") or 0))
        path = (last.get("root_path") or "").strip()
        if not path:
            return {"ok": False, "reason": "마지막 스캔 경로를 알 수 없습니다."}
        endpoint = "/api/autotune/start" if autotune else "/api/scan/start"
        payload = {"path": path}
        if autotune:
            payload["then_scan"] = True
        elif engine in ("threads", "pscan"):   # 명시 엔진 선택(자동 아닐 때)
            payload["engine"] = engine
        if one_file_system is not None:
            payload["one_file_system"] = bool(one_file_system)
        if fold_depth is not None and not autotune:   # 깊이 접기 오버라이드(직접 스캔 경로에만; 오토튜닝 스캔은 노드 설정값)
            try:
                payload["fold_depth"] = max(0, min(100000, int(fold_depth)))
            except (TypeError, ValueError, OverflowError):
                pass
        res = self._edge_post(node, endpoint, payload, timeout=30)
        if res.get("ok"):
            self._refresh_node_soon(node_id)   # 다음 폴링(최대 수십 초)까지 '스캔 중'이 안 보이고 중지도 못 하던 문제
            return {"ok": True, "node": node_id, "path": path,
                    "scan_id": res.get("scan_id"), "autotune": bool(autotune)}
        return {"ok": False, "path": path,
                "reason": res.get("reason") or "엣지가 스캔 시작을 거부했습니다."}

    @staticmethod
    def _edge_key(url: str) -> str:
        """노드 URL 을 '호스트:포트' 로 정규화(스킴·경로·끝 슬래시·대소문자 무시) — 같은 엣지 중복 등록 판별용."""
        u = (url or "").strip().lower()
        if not u:
            return ""
        if "://" not in u:
            u = "http://" + u
        try:
            p = urlparse(u)
            port = p.port or (443 if p.scheme == "https" else 80)
            return "%s:%s" % (p.hostname or "", port)
        except ValueError:
            return u

    def node_scan_all(self, *, one_file_system=None, autotune=False,
                      restart_busy=False, engine=None, fold_depth=None) -> dict:
        """등록된(활성) 노드 전체에 '마지막 경로' 스캔을 시작하고 노드별 결과를 모은다.

        같은 엣지(호스트:포트)가 다른 id 로 중복 등록돼 있으면 한 번만 보낸다 — 두 번 보내면 그
        엣지에서 같은 루트 스캔이 동시에 두 개 생겨(현장 #12/#13 쌍) 처리량이 반토막 나고 끝나지 않는다.
        (엣지도 같은 루트 중복 시작을 거부하지만, 포탈에서 먼저 걸러 사유를 분명히 보여준다.)
        """
        with self._lock:
            nodes = [(n["id"], n.get("url") or "") for n in self.nodes if n.get("enabled", True)]
        results = []
        seen: dict = {}
        for nid, url in nodes:
            key = self._edge_key(url)
            if key and key in seen:
                results.append({"id": nid, "ok": False,
                                "reason": "같은 엣지(%s)가 '%s'로도 등록돼 있어 건너뜀(중복 등록)"
                                          % (key, seen[key])})
                continue
            if key:
                seen[key] = nid
            r = self.node_scan(nid, one_file_system=one_file_system,
                               autotune=autotune, restart_busy=restart_busy, engine=engine,
                               fold_depth=fold_depth)
            results.append({"id": nid, "ok": bool(r.get("ok")),
                            "reason": r.get("reason") or ""})
        ok = sum(1 for r in results if r["ok"])
        auditmod.record(self.data_dir, action="node_scan_all", ok=True,
                        detail="%d/%d (ofs=%s autotune=%s restart=%s)"
                        % (ok, len(results), one_file_system, autotune, restart_busy))
        return {"ok": True, "count": ok, "total": len(results), "results": results}

    def _refresh_node_soon(self, node_id: str) -> None:
        """원격 작업 직후 그 노드 상태를 곧바로 다시 받는다(백그라운드 — 응답을 늦추지 않게)."""
        def _go():
            try:
                time.sleep(0.5)        # 엣지가 스캔 행을 만들 틈
                self._poll_one(node_id)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_go, daemon=True).start()

    def node_stop(self, node_id: str) -> dict:
        """노드의 진행 중 스캔(탐색/집계)을 모두 중지한다(베스트에포트).

        pscan(멀티프로세스)은 중간 중지가 불가 — 엣지가 거부한 사유를 그대로 돌려준다.
        중지된 스캔은 엣지에서 'paused' 로 남아 이어하기(재개)가 가능하다(threads 엔진).
        """
        with self._lock:
            node = next((dict(n) for n in self.nodes if n["id"] == node_id), None)
            cache = dict(self._cache.get(node_id, {})) if node else {}
        if not node:
            return {"ok": False, "reason": "노드를 찾을 수 없습니다."}
        roots = (cache.get("overall") or {}).get("roots") or []
        busy = [r for r in roots if r.get("status") in ("discovering", "sizing")]
        if not busy:
            # 캐시가 오래됐을 수 있다(방금 포탈에서 시작한 스캔) — 한 번 새로 받아 다시 본다(2026-11 원격 조작 점검)
            try:
                self._poll_one(node_id)
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                cache = dict(self._cache.get(node_id, {}))
            roots = (cache.get("overall") or {}).get("roots") or []
            busy = [r for r in roots if r.get("status") in ("discovering", "sizing")]
        if not busy:
            return {"ok": True, "stopped": 0, "reason": "진행 중 스캔 없음"}
        stopped, reasons = 0, []
        for r in busy:
            sid = int(r.get("scan_id") or 0)
            if not sid:
                continue
            res = self._edge_post(node, "/api/scan/stop", {"scan_id": sid})
            if res.get("ok"):
                stopped += 1
            else:
                reasons.append(res.get("reason") or "중지 거부(pscan 은 중지 불가)")
        return {"ok": stopped > 0 or not reasons, "stopped": stopped,
                "reason": "; ".join(reasons)}

    def node_stop_all(self) -> dict:
        """등록된(활성) 노드 전체의 진행 중 스캔을 일괄 중지하고 노드별 결과를 모은다."""
        with self._lock:
            ids = [n["id"] for n in self.nodes if n.get("enabled", True)]
        results = []
        stopped_total = 0
        for nid in ids:
            r = self.node_stop(nid)
            stopped_total += int(r.get("stopped") or 0)
            results.append({"id": nid, "ok": bool(r.get("ok")),
                            "stopped": int(r.get("stopped") or 0),
                            "reason": r.get("reason") or ""})
        auditmod.record(self.data_dir, action="node_stop_all", ok=True,
                        detail="스캔 %d개 중지 (노드 %d개 대상) — %s" % (stopped_total, len(results), ", ".join(
                            "%s:%d" % (r["id"], r["stopped"]) for r in results if r["stopped"]) or "중지한 노드 없음"))
        return {"ok": True, "stopped": stopped_total, "total": len(results),
                "results": results}

    def set_node_password(self, ids, password: str, *, clear: bool = False) -> dict:
        """선택한(또는 전체) 노드의 작업 보호 비밀번호를 설정/변경/해제한다.

        엣지의 /api/op-password 로 api_token(X-Auth-Token) 인증으로 푸시한다. 토큰이 없는
        노드는 푸시할 수 없어 사유를 돌려준다. ids 가 비면 전체 노드가 대상이다.
        """
        with self._lock:
            allnodes = [dict(n) for n in self.nodes]
        if ids and not isinstance(ids, (list, tuple, str)):   # 숫자·참거짓 → 500 이던 것(2026-11 퍼징)
            return {"ok": False, "reason": "ids 는 노드 id 목록이어야 합니다."}
        idset = {str(i) for i in ([ids] if isinstance(ids, str) else (ids or []))}
        targets = allnodes if not idset else [n for n in allnodes if n["id"] in idset]
        if not targets:
            return {"ok": False, "reason": "대상 노드가 없습니다."}
        payload = json.dumps({"password": "" if clear else str(password or ""),
                              "clear": bool(clear)}).encode("utf-8")
        results, ok_n = [], 0
        for n in targets:
            if not n.get("token"):
                results.append({"id": n["id"], "ok": False,
                                "reason": "api_token(토큰) 미등록 — 푸시할 수 없음"})
                continue
            url = n["url"].rstrip("/") + "/api/op-password"
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Auth-Token", n["token"])
            try:
                resp = netutil.urlopen(req, timeout=20)
                j = json.loads(resp.read().decode("utf-8"))
                ok = bool(j.get("ok"))
                results.append({"id": n["id"], "ok": ok,
                                "reason": "" if ok else (j.get("reason") or "거부됨")})
                ok_n += 1 if ok else 0
            except urllib.error.HTTPError as e:
                body_reason = self._http_err_reason(e)
                # 401(토큰 불일치)과 403(엣지에 api_token 미설정 등)은 고치는 방법이 다르다 — 엣지 사유를 우선
                reason = ("인증 실패(api_token 불일치)" if e.code == 401
                          else (body_reason or "엣지가 거부했습니다(엣지에 api_token 설정 필요 등)") if e.code == 403
                          else "엣지에 비밀번호 관리 API 없음(구버전)" if e.code == 404
                          else (body_reason or "HTTP %d" % e.code))
                results.append({"id": n["id"], "ok": False, "reason": reason})
            except Exception as e:  # noqa: BLE001
                results.append({"id": n["id"], "ok": False, "reason": str(e)})
        failed = [r for r in results if not r.get("ok")]
        out = {"ok": ok_n > 0, "total": len(targets), "ok_count": ok_n,
               "cleared": bool(clear), "results": results}
        if failed:   # 실패한 노드와 사유를 맨 위에도 — 화면·감사 로그가 '완료'로 보이지 않게(2026-11 원격 조작 점검)
            out["failed"] = len(failed)
            out["reason"] = "실패 %d대: %s" % (len(failed), "; ".join(
                "%s: %s" % (r["id"], r.get("reason") or "실패") for r in failed[:10]))
        return out

    # --- 원격 자동 구성 ---
    def provision_plan(self, raw: dict) -> dict:
        """IP/포트/경로 등으로 엣지 설치 스크립트를 생성하고 노드를 자동 등록한다(SSH 없음).

        같은 노드(같은 이름 id, 또는 같은 url=같은 서버)가 이미 있으면 overwrite(기본 True)일 때
        **전체 덮어쓰기**한다(같은 서버가 다른 이름으로 있던 중복 항목도 제거). API 토큰을 비우면
        — 기존 노드가 있으면 그 토큰을 **재사용**(엣지 페어링 유지), 없으면 새로 생성한다.
        overwrite=False 인데 이미 있으면 등록하지 않고 사유를 돌려준다.
        """
        bad = _non_str_field(raw, ("host", "path", "name", "region", "token", "hq_base", "install"))
        if bad:
            return {"ok": False, "reason": "%s 는 문자열이어야 합니다" % bad}
        host = (raw.get("host") or "").strip()
        if not host:
            return {"ok": False, "reason": "host(IP/주소) 필요"}
        try:
            port = int(raw.get("port") or 8765)
        except (TypeError, ValueError, OverflowError):
            port = 8765
        path = (raw.get("path") or "/mnt/hadoop").strip()
        name = ((raw.get("name") or "").strip()
                or re.sub(r"[^A-Za-z0-9_.-]+", "-", host).strip("-") or "edge")
        if not safe_node_id(name):
            # 먼저 거른다 — 예전엔 같은 서버의 옛 노드(와 복제본)를 지운 뒤에야 새 이름이 거부돼 둘 다 사라졌다
            return {"ok": False, "reason": "노드 이름에 '/'·'\\'·'.'·'..' 는 쓸 수 없습니다."}
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
        try:
            script = build_provision_script(host=host, port=port, token=token, path=path,
                                            hq_base=hq_base, install=install)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}
        replaced_prev = None
        rec = {"id": name, "region": region, "url": edge_url, "token": token,
               "alias_local": path, "alias_logical": path}
        if self._get(name) is None:        # 새 노드만 기본 주기·모드(덮어쓰기는 운영자가 바꾼 주기 유지)
            rec.update({"unit": "minute", "every": 30, "mode": "both", "enabled": True})
        node_res = self.upsert_node(rec, partial=True)
        # 같은 서버가 다른 이름으로 있었으면 옛 항목 제거 — 새 항목이 '등록된 뒤에만'(실패 시 옛 노드·복제본 보존)
        if node_res.get("ok") and existing and existing["id"] != name:
            replaced_prev = existing["id"]
            self.delete_node(existing["id"])
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
        except (TypeError, ValueError, OverflowError):
            ssh_port = 22
        # 사용자/호스트가 '-o…' 처럼 옵션으로 해석되면 ProxyCommand 등으로 포탈에서 임의 명령이 돈다 —
        # 허용 문자만 받고, 목적지 앞에 '--' 를 둔다(2026-11 리뷰).
        if not _SSH_USER_RE.match(user):
            return {"ok": False, "reason": "ssh 사용자 이름이 올바르지 않습니다: %r" % user[:64]}
        if not _SSH_HOST_RE.match(host):
            return {"ok": False, "reason": "host 가 올바르지 않습니다(IP/호스트명만): %r" % host[:128]}
        if not 1 <= ssh_port <= 65535:
            return {"ok": False, "reason": "ssh 포트가 범위를 벗어났습니다: %d" % ssh_port}
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
            cmd = [sshpass, "-e"]           # 비밀번호는 환경변수(SSHPASS)로 — 명령줄(ps)에 노출 안 함
        cmd += [ssh, "-p", str(ssh_port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-l", user, "--", host, "bash -s"]
        env = dict(os.environ)
        if password:
            env["SSHPASS"] = password
        try:
            p = subprocess.run(cmd, input=script.encode(), env=env,
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
        """노드 복제본에서 path 를 포함하는(root 가 prefix) 최신 완료 스캔 DB 경로(첫 후보)."""
        dbs = self._replica_dbs_for(node_id, path)
        return dbs[0] if dbs else None

    def _replica_dbs_for(self, node_id: str, path: str) -> list:
        """path 를 덮는 복제 스캔 DB 후보들(좋은 순서). 비교는 첫 후보에 그 폴더 행이 없으면(접힘 등) 다음 후보를 본다."""
        if not safe_node_id(node_id):
            return []
        rep = os.path.join(self.replicas_dir, node_id)
        try:
            with open(os.path.join(rep, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return []
        # 고르는 순서: ① 완료(done) 스캔 — 일시정지·오류는 일부만 센 값이라 비교에 쓰면 '줄어든' 것처럼
        # 보인다 ② 최신(완료 시각) ③ 경로를 더 구체적으로 덮는 루트. 예전엔 ②③ 순서가 반대라 오래된 하위
        # 루트 스캔이 최신 상위 루트 스캔을 이겨, 같은 화면에서 부모·자식 값이 서로 다른 시점이었다.
        cands = []
        for s in meta.get("scans", []):
            root = (s.get("root_path") or "").rstrip("/")
            if path == root or path.startswith(root + "/") or (not root and path.startswith("/")):
                dbf = s.get("db_filename") or os.path.basename(s.get("db_path") or "")
                dbp = os.path.join(rep, "scans", dbf)
                if dbf and os.path.exists(dbp):
                    cands.append(((s.get("status") == "done",) + self._scan_recency(s) + (len(root),), dbp))
        cands.sort(key=lambda t: t[0], reverse=True)
        return [dbp for _k, dbp in cands]

    def _replica_cands(self, node_id: str, path: str) -> list:
        """비교용 후보 (DB 경로, 루트) — _replica_dbs_for 와 같은 순서에서 '같은 루트의 옛 스캔'은 뺀다.

        다음 후보로 넘어가는 건 최신 스캔이 그 경로를 접어(fold) 행이 없을 때뿐이고, 그때도 같은 루트의 옛 스캔은
        보지 않는다 — 옛 스캔엔 지금은 지워진 폴더가 남아 있어, 삭제된 폴더가 '있음·차이 없음'으로 보였다(2026-11 연동 점검)."""
        if not safe_node_id(node_id):
            return []
        rep = os.path.join(self.replicas_dir, node_id)
        try:
            with open(os.path.join(rep, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return []
        cands = []
        for s in meta.get("scans", []):
            root = (s.get("root_path") or "").rstrip("/")
            if path == root or path.startswith(root + "/") or (not root and path.startswith("/")):
                dbf = s.get("db_filename") or os.path.basename(s.get("db_path") or "")
                dbp = os.path.join(rep, "scans", dbf)
                if dbf and os.path.exists(dbp):
                    cands.append(((s.get("status") == "done",) + self._scan_recency(s) + (len(root),), dbp, root))
        cands.sort(key=lambda t: t[0], reverse=True)
        out, seen = [], set()
        for _k, dbp, root in cands:
            if root in seen:
                continue           # 같은 루트는 가장 좋은(완료·최신) 스캔 하나만
            seen.add(root)
            out.append((dbp, root))
        return out

    @staticmethod
    def _folded_at(c, rid, local: str):
        """local 에 행이 없을 때, 접기(fold) 때문인지 판별한다 — 가장 가까운 상위 폴더가 '하위 폴더가 있는데
        하위 행이 하나도 없으면' 접힌 것(그 상위의 합계에 들어 있음). 접힌 상위 경로를, 아니면 None.
        예전엔 접힌 폴더를 '없음/미스캔·누락'으로 보여, 노드마다 fold 깊이가 다르면 가짜 누락처럼 보였다."""
        p = local.rstrip("/")
        while True:
            parent = os.path.dirname(p)
            if not parent or parent == p:
                return None
            r = c.execute("SELECT subdir_count FROM directories WHERE run_id=? AND path=?", (rid, parent)).fetchone()
            if r is not None:
                if int(r["subdir_count"] or 0) <= 0:
                    return None
                base = parent.rstrip("/")
                kid = c.execute("SELECT 1 FROM directories WHERE run_id=? AND path>=? AND path<? LIMIT 1",
                                (rid, base + "/", base + "0")).fetchone()
                return parent if kid is None else None
            p = parent

    def compare_path(self, path: str) -> dict:
        """같은 경로의 노드별 용량/파일 수를 모아 비교한다(복제본 기준)."""
        path = os.path.abspath(path)
        rows = []
        for n in self.nodes:
            entry = {"id": n["id"], "region": n.get("region"), "found": False,
                     "total_bytes": 0, "own_bytes": 0, "total_files": 0, "subdir_count": 0}
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            for dbp, _root in self._replica_cands(n["id"], local):
                try:
                    c = dbmod.connect(dbp)
                    try:
                        rid = dbmod.latest_run_id(c)
                        r = c.execute(
                            "SELECT total_bytes, own_bytes, total_files, subdir_count "
                            "FROM directories WHERE run_id=? AND path=?", (rid, local)).fetchone()
                        fa = None if r is not None else self._folded_at(c, rid, local)
                    finally:
                        c.close()
                except Exception:  # noqa: BLE001
                    continue
                if r is not None:
                    sz = r["total_bytes"] if r["total_bytes"] else r["own_bytes"]
                    entry.update({"found": True, "total_bytes": sz,
                                  "own_bytes": r["own_bytes"], "total_files": r["total_files"],
                                  "subdir_count": r["subdir_count"]})
                    entry.pop("folded_at", None)
                    break
                if not fa:
                    break              # 이 루트의 최신 스캔에 없고 접힌 것도 아님 = 정말 없다(옛 스캔을 보지 않는다)
                entry.setdefault("folded_at", fa)   # 접힘 — 더 구체적인 다른 루트 스캔이 있으면 그걸 본다
            rows.append(entry)
        return {"ok": True, "path": path, "rows": rows}

    def _matrix_kids(self, node_id: str, local: str):
        """노드의 복제 후보들에서 local 의 직속 자식 행을 찾는다 — (자식 행, 접힘 여부) 또는 None(경로 없음).
        첫 후보에 행이 없거나 그 아래가 접혀 있으면 다음 후보(더 깊이 저장한 다른 스캔)를 본다."""
        folded = False
        for dbp, _root in self._replica_cands(node_id, local):
            try:
                c = dbmod.connect(dbp)
                try:
                    rid = dbmod.latest_run_id(c)
                    prow = c.execute("SELECT depth, subdir_count FROM directories WHERE run_id=? AND path=?",
                                     (rid, local)).fetchone()
                    if prow is None:
                        if not self._folded_at(c, rid, local):
                            # 이 루트의 최신 스캔에 없고 접힌 것도 아님 — 정말 없다(옛 스캔으로 넘어가지 않는다)
                            return ([], True) if folded else None
                        folded = True
                        continue
                    # 범위 비교(대소문자 구분·인덱스 사용): LIKE 는 ASCII 대소문자를 무시해 /data/FOO 의 자식이
                    # /data/foo 매트릭스에 섞였다(2026-11 리뷰). prefix ≤ path < prefix 의 '/' 다음 문자('0').
                    base = local.rstrip("/")
                    kids = c.execute(
                        "SELECT name, total_bytes, own_bytes, subdir_count FROM directories "
                        "WHERE run_id=? AND depth=? AND path >= ? AND path < ?",
                        (rid, int(prow["depth"]) + 1, base + "/", base + "0")).fetchall()
                finally:
                    c.close()
            except Exception:  # noqa: BLE001
                continue
            if kids:
                return [dict(k) for k in kids], False
            if int(prow["subdir_count"] or 0) > 0:
                folded = True           # 이 스캔에선 이 폴더에서 접힘 — 다른 후보가 더 깊이 있으면 그걸 쓴다
                continue
            return [], False            # 정말 하위 폴더가 없다
        return ([], True) if folded else None

    def compare_matrix(self, path: str) -> dict:
        """path 직속 자식들의 노드별 용량 매트릭스(누락·드리프트 비교)."""
        path = os.path.abspath(path)
        node_ids = [n["id"] for n in self.nodes]
        children = {}
        folded = []   # 이 경로 아래가 접혀(fold) 하위 행이 없는 노드
        for n in self.nodes:
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            res = self._matrix_kids(n["id"], local)
            if res is None:
                continue
            kids, is_folded = res
            for k in kids:
                e = children.setdefault(k["name"], {"name": k["name"], "subdir_count": 0, "vals": {}})
                e["vals"][n["id"]] = int(k["total_bytes"] or k["own_bytes"] or 0)
                e["subdir_count"] = max(e["subdir_count"], int(k["subdir_count"] or 0))
            if is_folded:
                folded.append(n["id"])
        out = []
        for e in children.values():
            e["sum"] = sum(e["vals"].values())
            out.append(e)
        out.sort(key=lambda x: -x["sum"])
        return {"ok": True, "path": path, "nodes": node_ids, "children": out, "folded_nodes": folded}


# ------------------------------------------------------------------ HTTP 핸들러
class PortalHandler(BaseHTTPRequestHandler):
    data_dir = ""
    controller = None  # PortalController

    def log_message(self, fmt, *args):  # noqa: D401 — 조용히
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        pend = getattr(self, "_audit_pending", None)
        if pend:
            self._audit_pending = None
            if not (status == 404 and isinstance(payload, dict)
                    and payload.get("reason") == "unknown_endpoint"):
                ok = status < 400 and not (isinstance(payload, dict) and payload.get("ok") is False)
                why = "" if ok else str((payload or {}).get("reason") or (payload or {}).get("error") or status)
                # 대상·결과 요약도 남긴다 — 예전엔 성공 행의 상세가 비어 '무엇을' 지웠는지 알 수 없었다(2026-11 리뷰)
                tgt = getattr(self, "_audit_target", "") or ""
                if isinstance(payload, dict):
                    cnt = ["%s=%s" % (k, payload[k]) for k in ("added", "updated", "removed", "deleted", "total",
                                                               "stopped", "scan_id")
                           if isinstance(payload.get(k), int) and not isinstance(payload.get(k), bool)]
                    if isinstance(payload.get("path"), str) and "path=" not in tgt:
                        cnt.append("path=%s" % payload["path"][:200])
                    if isinstance(payload.get("errors"), list) and payload["errors"]:
                        cnt.append("errors=%d" % len(payload["errors"]))
                    if cnt:
                        tgt = (tgt + " " if tgt else "") + " ".join(cnt)
                self._audit(pend, ok, " · ".join(x for x in (tgt, why) if x))
        body = _json_bytes(payload)   # 비 UTF-8 이름이 섞여도 500 없이(엣지와 같은 규칙)
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
        body, self._body_err = read_json_body(self)   # 엣지와 같은 규칙(단일 소스)
        return body

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if _has_nul(parse_qs(urlparse(self.path).query)):
            self._send_json({"ok": False, "reason": "요청에 NUL 문자가 있습니다"}, status=400)
            return
        if path in ("/", "/index.html"):
            self._send_html(PORTAL_HTML)
            return
        if path == "/theme.css":   # 공통 화면 테마(단일 소스, 대시보드·포탈 공유)
            try:
                with open(os.path.join(HERE, "theme.css"), "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.send_error(404, "theme.css not found")
            return

        if path == "/i18n.js":     # 영어 토글 런타임+사전(단일 소스, 대시보드·포탈 공유)
            try:
                with open(os.path.join(HERE, "i18n_en.js"), "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.send_error(404, "i18n.js not found")
            return
        c = self.controller
        try:
            if path == "/api/portal/auth":
                self._send_json(c.auth_status())
                return
            if path == "/api/portal/usage":      # 외부 서버용 집계 사용량 API(읽기 전용 · export_token)
                given = self.headers.get("X-Auth-Token") or (parse_qs(urlparse(self.path).query).get("token", [""])[0])
                if not c.export_token_ok(given):
                    self._send_json({"ok": False, "reason": "unauthorized"}, status=401)
                    return
                self._send_json(c.usage_export())
                return
            if path == "/api/portal/changelog":
                text = None
                for cand in (os.path.join(os.path.dirname(HERE), "CHANGELOG.md"),
                             os.path.join(os.getcwd(), "CHANGELOG.md")):
                    try:
                        with open(cand, "r", encoding="utf-8") as fh:
                            text = fh.read()
                        break
                    except OSError:
                        continue
                self._send_json({"ok": True, "version": __version__, "changelog": text})
                return
            if path == "/api/portal/settings":
                cfg = c.upgrade_config()
                if c.auth_required() and not c.token_valid(self.headers.get("X-Op-Token")):
                    # 로그인 전에는 서버 경로(백업·복제·릴리스 폴더)·LLM 주소 같은 운영 정보를 감춘다(2026-11 리뷰)
                    keep = ("ok", "portal_title", "portal_subtitle", "hq_version", "bundle_version", "bundle_stale",
                            "ask_llm_enabled", "export_token_set")
                    cfg = dict({k: cfg[k] for k in keep if k in cfg}, redacted=True)
                self._send_json(cfg)
                return
            if path == "/api/portal/upgrade/status":
                self._send_json(c.upgrade_status())
                return
            if path == "/api/portal/upgrade/history":   # 업그레이드 기록(자동/수동/푸시 + 세부)
                self._send_json({"ok": True, "events": c.upgrade_history(100),
                                 "version": __version__})
                return
            if path == "/api/portal/ping-history":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    rng = int(qs.get("range", ["86400"])[0])
                except (TypeError, ValueError, OverflowError):
                    rng = 86400
                self._send_json(c.ping_history(rng))
                return
            if path == "/api/portal/browse":
                # HQ(포탈) 서버의 로컬 디렉터리 탐색 — 감시 폴더 등 경로 선택용. HQ 파일시스템을
                # 마운트 제한 없이 열거하므로, 작업 비밀번호가 설정돼 있으면 인증을 요구한다
                # (무인증 GET 으로 HQ 서버 임의 디렉터리 열람 방지).
                if c.auth_required() and not c.token_valid(self.headers.get("X-Op-Token")):
                    self._send_json({"ok": False, "reason": "locked", "op_required": True},
                                    status=401)
                    return
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                self._send_json(browse_dir(qp or "/"))
                return
            if path == "/api/portal/nodes":
                self._send_json(c.list_nodes())
                return
            if path == "/api/portal/overview":
                self._send_json(c.overview())
                return
            if path == "/api/portal/ask":      # 자연어 질의응답(읽기 전용)
                q = (parse_qs(urlparse(self.path).query).get("q", [""])[0] or "").strip()
                # 규칙 기반 답은 누구나, 외부 LLM 호출(저장된 키 사용)은 로그인한 사람만(비밀번호를 쓰는 포탈일 때)
                llm_ok = not c.auth_required() or c.token_valid(self.headers.get("X-Op-Token"))
                self._send_json(c.ask(q, allow_llm=llm_ok))
                return
            if path == "/api/portal/throughput":   # 법인별 시간당 처리량 + 윈도우별(영구 DB)
                self._send_json(c.throughput_windows())
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
                if not qp.startswith("/"):   # 상대 경로는 포탈 작업 폴더 기준으로 조용히 바뀌었다 → 거부
                    self._send_json({"ok": False, "reason": "절대 경로(/ 로 시작) 필요"}, status=400)
                else:
                    self._send_json(c.compare_path(qp))
                return
            if path == "/api/portal/matrix":
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                if not qp.startswith("/"):   # 상대 경로는 포탈 작업 폴더 기준으로 조용히 바뀌었다 → 거부
                    self._send_json({"ok": False, "reason": "절대 경로(/ 로 시작) 필요"}, status=400)
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
        self._audit_pending = None
        self._audit_target = ""            # keep-alive 로 같은 핸들러가 다음 요청을 받아도 이전 대상이 섞이지 않게
        try:
            self._do_POST()
        finally:
            pend, self._audit_pending = self._audit_pending, None
            if pend:                       # JSON 이 아닌 응답(파일 등)으로 끝난 작업
                self._audit(pend, True)

    def _do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json_body()
        if self._body_err:
            self._send_json({"ok": False, "reason": self._body_err[1]}, status=self._body_err[0])
            return
        c = self.controller
        try:
            if path == "/api/portal/login":
                res = c.login(body.get("password") or "",
                              self.client_address[0] if self.client_address else None)
                self._audit("login", res.get("ok"),
                            "" if res.get("ok") else (res.get("reason") or ""))
                self._send_json(res)
                return
            # 비밀번호 설정/변경/해제 — 이미 설정돼 있으면 로그인 필요. 비밀번호가 없을 때(임시 비밀번호를
            # 만들지 못한 경우)의 첫 설정은 JSON 요청만 허용(다른 사이트 폼 CSRF 차단).
            if path == "/api/portal/password":
                if c.auth_required() and not c.token_valid(self.headers.get("X-Op-Token")):
                    self._audit("password", False, "locked")
                    self._send_json({"ok": False, "reason": "locked", "op_required": True},
                                    status=401)
                    return
                if not c.auth_required() and "application/json" not in (self.headers.get("Content-Type") or ""):
                    self._send_json({"ok": False, "reason": "JSON 요청만 허용"}, status=415)
                    return
                res = c.set_password(body.get("password") or "", body.get("encrypt"))
                self._audit("password", res.get("ok"))
                self._send_json(res, status=res.pop("_status", None) or 200)
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
            # 인증은 설정·되돌릴 수 없는 작업(PORTAL_PROTECTED_POSTS)에만 — 동기화·원격 스캔 등 일반 작업은 자유.
            # 비밀번호가 없으면 need_setup(화면이 '처음 비밀번호 만들기'). 토큰은 커스텀 헤더로만(CSRF 위조 불가).
            if path in PORTAL_PROTECTED_POSTS:
                if not c.auth_required():
                    self._audit(path, False, "need_setup")
                    self._send_json({"ok": False, "reason": "need_setup", "need_setup": True,
                                     "error": "먼저 관리 비밀번호를 만드세요."}, status=401)
                    return
                if not c.token_valid(self.headers.get("X-Op-Token")):
                    self._audit(path, False, "locked")
                    self._send_json({"ok": False, "reason": "locked", "op_required": True,
                                     "error": "로그인 후 작업하세요."}, status=401)
                    return
            if path == "/api/portal/audit":      # 감사 로그 조회(로그인 필요)
                self._send_json({"ok": True, "events": auditmod.tail(self.data_dir, 300)})
                return
            self._audit_pending = path           # 변경 작업 기록 — 처리 결과로(_send_json 이 마무리, 엣지와 같은 규칙)
            self._audit_target = _audit_target(body)   # 무엇에 대해(노드 id·경로 등) — 비밀값은 넣지 않는다
            if path == "/api/portal/backup/save":      # 포탈+노드 백업을 서버 디렉터리에 저장
                self._send_json(c.save_backup(body.get("dir") or body.get("path") or ""))
                return
            if path == "/api/portal/backup/config":    # 자동 백업 일정(시간)·최대 보관 수 설정
                self._send_json(c.set_backup_config(body.get("dir"), body.get("every_hours"),
                                                    body.get("keep")))
                return
            if path == "/api/portal/backup/download":  # 포탈+노드 백업 tar.gz 다운로드(민감)
                data, fname = c.make_backup_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")   # 토큰·비밀번호 해시가 든 파일 — 캐시에 남기지 않음
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/api/portal/settings":
                # 카드별 부분 저장 — 보낸 키만 반영한다(없는 키로 다른 설정을 덮어쓰지 않음).
                res = {"ok": True}
                if "upgrade_watch_dir" in body or "upgrade_check_secs" in body:
                    # 보내지 않은 감시 폴더는 그대로(None) — 예전엔 주기만 보내도 폴더가 비워졌다
                    res.update(c.set_upgrade_watch(body.get("upgrade_watch_dir") if "upgrade_watch_dir" in body
                                                   else None, body.get("upgrade_check_secs")))
                if "enroll_token" in body:
                    res.update(c.set_enroll_token(body.get("enroll_token") or ""))
                if "release_dir" in body:
                    res.update(c.set_release_dir(body.get("release_dir") or ""))
                if "replica_dir" in body:   # 중앙 복제 DB 저장 경로
                    res.update(c.set_replica_dir(body.get("replica_dir") or ""))
                if "replica_keep_per_root" in body:   # 노드·루트별 복제 스캔 보관 수
                    res.update(c.set_replica_keep(body.get("replica_keep_per_root")))
                if "portal_title" in body or "portal_subtitle" in body:
                    res.update(c.set_title(body.get("portal_title") or "",
                                           body.get("portal_subtitle") or ""))
                if "nav_hidden" in body:
                    res.update(c.set_nav_hidden(body.get("nav_hidden") or []))
                if "export_token" in body:
                    res.update(c.set_export_token(body.get("export_token") or ""))
                if any(k in body for k in ("op_ttl_minutes", "show_update_popup",
                                           "log_popup_pos")):
                    res.update(c.set_session_prefs(body))
                if any(k in body for k in ("ask_llm_enabled", "ask_llm_endpoint",
                                           "ask_llm_model", "ask_llm_key", "ask_llm_timeout")):
                    res.update(c.set_ask_llm(body))
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
            if path == "/api/portal/nodes/set-token":  # 토큰 강제 맞추기(1개/일괄) — 포탈 로컬만 교정
                self._send_json(c.set_node_token(body.get("ids") or [],
                                                 str(body.get("token") or "")))
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
            if path == "/api/portal/node-stop-all":   # 전체 노드 진행 중 스캔 일괄 중단
                self._send_json(c.node_stop_all())
                return
            if path == "/api/portal/node-scan-all":   # 전체 노드 일괄 스캔(옵션: -x·엔진·진행중 재시작)
                self._send_json(c.node_scan_all(
                    one_file_system=body.get("one_file_system"),
                    autotune=bool(body.get("autotune")),
                    engine=body.get("engine"),
                    restart_busy=bool(body.get("restart_busy")),
                    fold_depth=body.get("fold_depth")))
                return
            if path == "/api/portal/node-password":   # 노드 작업 비밀번호 설정/변경/해제(포탈→엣지)
                self._send_json(c.set_node_password(
                    body.get("ids") or [], str(body.get("password") or ""),
                    clear=bool(body.get("clear"))))
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


def serve_portal(data_dir: str, host: str = "0.0.0.0", port: int = 8800, *,
                 auto_op_password: bool = False):
    """글로벌 포탈 HTTP 서버를 만들어 반환한다(호출 측에서 serve_forever).
    auto_op_password=True(실서버 기동 — CLI portal)면 관리 비밀번호가 없을 때 임시 비밀번호를 실행 디렉터리
    portal_info.MD 에 만든다(결과는 httpd.op_temp_made)."""
    data_dir = os.path.abspath(data_dir)
    mgrmod.migrate_legacy_config(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    controller = PortalController(data_dir)
    op_temp_made = controller.ensure_op_password() if auto_op_password else None
    controller.start()
    handler = type("BoundPortalHandler", (PortalHandler,),
                   {"data_dir": data_dir, "controller": controller})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller
    httpd.op_temp_made = op_temp_made   # 이번 기동에 임시 비밀번호를 만들었으면 {info_path, password}
    return httpd

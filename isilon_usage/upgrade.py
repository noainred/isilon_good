"""자동 업그레이드 — 감시 폴더의 새 버전 압축본(또는 푸시된 번들)을 적용한다.

스캐너·포탈 공용. 특정 폴더를 주기적으로 보다가 ``isilon_usage-<버전>.tar.gz/.zip`` 이
현재 실행 버전보다 새것이면, 패키지를 교체하고(기존은 백업) 프로세스를 재시작(re-exec)한다.
포탈은 자가 업그레이드 후 등록된 엣지에도 새 번들을 푸시한다. 표준 라이브러리만 사용.

안전장치: 옵트인(설정 비우면 꺼짐), 아카이브 검증(패키지·버전 확인), 더 새 버전만 적용,
경로 탈출 방지, 기존 코드 백업(롤백 가능).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tarfile
import time
import zipfile
from typing import Dict, Optional, Tuple

_ARCHIVE_RE = re.compile(r"isilon_usage-(\d+)\.(\d+)\.(\d+)\.(?:tar\.gz|tgz|zip)$")
_INIT_VER_RE = re.compile(r"""__version__\s*=\s*["'](\d+)\.(\d+)\.(\d+)["']""")


def parse_version(s) -> Optional[Tuple[int, int, int]]:
    """'1.2.3' 또는 'v1.2.3' → (1,2,3). 실패 시 None."""
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", str(s or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def vstr(t) -> str:
    return ".".join(str(x) for x in t)


def auto_status_reason(*, source_mode, auto, available, busy=False,
                       installing=False, watch_dir=""):
    """자동 업그레이드가 지금 '자동 설치까지' 가는지, 안 가면 왜 안 가는지 한 줄로 설명한다.

    엣지·포탈이 같은 판단을 쓰도록 단일 소스로 둔다(화면마다 복제 금지). 반환:
      (blocked: bool, reason: str)
        blocked=True  → 새 버전이 나와도 자동 설치되지 않는다(막는 이유 설명).
        blocked=False → 자동 설치 경로가 살아 있다(상태/예정 설명).
    """
    if installing:
        return False, "설치 진행 중…"
    net_on = (str(source_mode or "off").strip() == "github")
    watch_on = bool((watch_dir or "").strip())
    if not net_on and not watch_on:
        return True, ("자동 업그레이드 소스가 꺼져 있습니다(인터넷=off · 감시폴더 없음). "
                      "수동 확인/설치만 됩니다 — 소스를 'github'로 켜세요.")
    if busy:
        return True, "스캔 중이라 자동 업그레이드를 보류합니다 — 스캔이 끝나면 진행합니다."
    if net_on and not auto:
        if watch_on:
            return False, "감시폴더 자동 적용은 켜져 있습니다(인터넷 자동설치는 꺼짐 — 새 버전 알림만)."
        return True, "자동설치가 꺼져 있습니다 — 새 버전이 있어도 알림만 합니다('자동설치'를 켜세요)."
    if available:
        return False, "새 버전이 있어 곧 자동 설치합니다."
    return False, "자동 업그레이드 켜짐 — 새 버전이 나오면 자동 설치합니다(현재 최신)."


def _archive_version(filename: str) -> Optional[Tuple[int, int, int]]:
    m = _ARCHIVE_RE.search(os.path.basename(filename))
    return tuple(int(x) for x in m.groups()) if m else None


def find_newer_archive(watch_dir: str, current_version: str):
    """watch_dir 에서 current 보다 새 isilon_usage-*.tar.gz/.zip 중 '최신'을 찾는다.

    반환 (path, version_tuple) 또는 None.
    """
    cur = parse_version(current_version) or (0, 0, 0)
    best = None
    try:
        names = os.listdir(watch_dir)
    except OSError:
        return None
    for name in names:
        v = _archive_version(name)
        if v and v > cur and (best is None or v > best[1]):
            best = (os.path.join(watch_dir, name), v)
    return best


def _accept_member(name: str) -> Optional[str]:
    """아카이브 멤버명에서 'isilon_usage/' 아래 상대경로를 안전하게 뽑는다(아니면 None)."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if "isilon_usage" not in parts:
        return None
    rel = parts[parts.index("isilon_usage") + 1:]
    if not rel or any(p == ".." for p in rel):   # 경로 탈출 방지
        return None
    return "/".join(rel)


# 압축 해제 총량/멤버 수 상한 — 손상·거대 아카이브(zip/tar 폭탄)의 메모리 폭주 방지.
MAX_BUNDLE_BYTES = 200 * 1024 * 1024
MAX_MEMBERS = 20000


def _members_from_tar(tf) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    total = 0
    for m in tf:
        if not m.isfile():
            continue
        rel = _accept_member(m.name)
        if not rel:
            continue
        if len(out) >= MAX_MEMBERS or total + int(m.size or 0) > MAX_BUNDLE_BYTES:
            raise ValueError("아카이브가 너무 큼(또는 멤버 과다)")
        f = tf.extractfile(m)
        if f is not None:
            data = f.read()
            out[rel] = data
            total += len(data)
    return out


def read_package_members(archive_path: str) -> Dict[str, bytes]:
    """아카이브에서 'isilon_usage/<...>' 파일을 {상대경로: bytes} 로 읽는다(안전·상한)."""
    if archive_path.endswith(".zip"):
        out: Dict[str, bytes] = {}
        total = 0
        with zipfile.ZipFile(archive_path) as zf:
            for zi in zf.infolist():
                if zi.is_dir():
                    continue
                rel = _accept_member(zi.filename)
                if not rel:
                    continue
                if len(out) >= MAX_MEMBERS or total + int(zi.file_size or 0) > MAX_BUNDLE_BYTES:
                    raise ValueError("아카이브가 너무 큼(또는 멤버 과다)")
                data = zf.read(zi)
                out[rel] = data
                total += len(data)
        return out
    with tarfile.open(archive_path, "r:*") as tf:
        return _members_from_tar(tf)


def members_version(members: Dict[str, bytes]) -> Optional[Tuple[int, int, int]]:
    """패키지 멤버의 __init__.py 에서 __version__ 을 읽는다(검증용)."""
    init = members.get("__init__.py")
    if not init:
        return None
    m = _INIT_VER_RE.search(init.decode("utf-8", "replace"))
    return tuple(int(x) for x in m.groups()) if m else None


def apply_package(members: Dict[str, bytes], code_dir: str) -> str:
    """members 를 code_dir/isilon_usage/ 로 교체 설치한다(기존은 백업, 원자적 스왑).

    반환: 백업 경로(없으면 ""). 실패 시 예외(가능하면 롤백).
    """
    code_dir = os.path.abspath(code_dir)
    pkg = os.path.join(code_dir, "isilon_usage")
    ts = int(time.time())
    staging = "%s.new.%d" % (pkg, ts)
    backup = "%s.bak.%d" % (pkg, ts)
    shutil.rmtree(staging, ignore_errors=True)
    for rel, data in members.items():
        dst = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)
    had_old = os.path.isdir(pkg)
    if had_old:
        os.replace(pkg, backup)            # 기존 → 백업(같은 fs, 원자적)
    try:
        os.replace(staging, pkg)           # 새 → 자리
    except OSError:
        if had_old:
            os.replace(backup, pkg)        # 롤백
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return backup if had_old else ""


def upgrade_from_archive(archive_path: str, code_dir: str, current_version: str) -> dict:
    """아카이브가 현재보다 새 버전이면 패키지를 교체한다(재시작은 호출 측에서)."""
    try:
        members = read_package_members(archive_path)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ValueError) as exc:
        return {"ok": False, "reason": "아카이브 읽기 실패: %s" % exc}
    new_v = members_version(members)
    if not new_v:
        return {"ok": False, "reason": "아카이브에 유효한 isilon_usage 패키지/버전 없음"}
    cur = parse_version(current_version) or (0, 0, 0)
    if new_v <= cur:
        return {"ok": False, "reason": "더 새 버전 아님(%s <= %s)" % (vstr(new_v), vstr(cur)),
                "version": vstr(new_v)}
    try:
        backup = apply_package(members, code_dir)
    except OSError as exc:
        return {"ok": False, "reason": "교체 실패: %s" % exc}
    return {"ok": True, "version": vstr(new_v), "from": vstr(cur), "backup": backup}


def upgrade_from_bundle_bytes(data: bytes, code_dir: str, current_version: str,
                              *, allow_same: bool = False) -> dict:
    """포탈이 푸시한 agent-bundle(tar.gz bytes)을 적용한다(엣지 측)."""
    import io
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            out = _members_from_tar(tf)
    except (tarfile.TarError, OSError, ValueError) as exc:
        return {"ok": False, "reason": "번들 읽기 실패: %s" % exc}
    new_v = members_version(out)
    if not new_v:
        return {"ok": False, "reason": "번들에 유효한 패키지/버전 없음"}
    cur = parse_version(current_version) or (0, 0, 0)
    if new_v < cur or (new_v == cur and not allow_same):
        return {"ok": False, "reason": "더 새 버전 아님(%s)" % vstr(new_v), "version": vstr(new_v)}
    try:
        backup = apply_package(out, code_dir)
    except OSError as exc:
        return {"ok": False, "reason": "교체 실패: %s" % exc}
    return {"ok": True, "version": vstr(new_v), "from": vstr(cur), "backup": backup}


def code_dir_of(package_file: str) -> str:
    """isilon_usage 패키지 파일(__file__) → 코드 디렉터리(패키지의 부모)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(package_file)))


def restart_process() -> None:
    """현재 프로세스를 같은 인자로 재시작(re-exec)한다 — 새 코드를 로드한다.

    `python -m isilon_usage <args...>` 형태로 다시 실행한다(systemd/nohup 모두 동작).
    호출하면 돌아오지 않는다.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "isilon_usage"] + sys.argv[1:])


# --- 인터넷(GitHub raw·사내 미러·사설 레포) 소스에서 새 버전 확인·다운로드 ---
# make_release 가 만드는 download/versions.json 을 받아 최신 버전을 확인한다(HTTPS·읽기 전용).
# 공개 URL 은 토큰 없이, 비공개(사설) 소스는 토큰(PAT)을 Authorization 헤더로 보낸다.
#  - 비공개 GitHub: raw URL 을 자동으로 contents API(?ref=)로 바꿔 토큰 인증(브랜치에 '/' 있어도 안전).
#  - 사내 미러/사설 호스트: 준 URL 그대로 Bearer 토큰만 덧붙인다.
DEFAULT_UPGRADE_BASE = ("https://raw.githubusercontent.com/noainred/isilon_good/"
                        "claude/upbeat-bell-cXX8f/download")

_RAW_GH_RE = re.compile(r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/(.+)$")
_WWW_GH_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/raw/(.+)$")


def _to_github_api(base: str) -> str:
    """공개 raw GitHub 디렉터리 URL 을 (비공개에서도 받는) contents API 로 바꾼다.

    ``raw.githubusercontent.com/{o}/{r}/{ref...}/{dir}`` (또는 ``github.com/{o}/{r}/raw/...``)
    → ``api.github.com/repos/{o}/{r}/contents/{dir}?ref={ref...}``.
    마지막 한 segment 를 디렉터리로 보고 나머지를 ref(쿼리)로 분리하므로, ref 에 '/' 가 있어도
    (예: ``claude/upbeat-bell-cXX8f``) 안전하다. 인식 못 하면 원본을 그대로 반환한다.
    """
    m = _RAW_GH_RE.match(base) or _WWW_GH_RE.match(base)
    if not m:
        return base
    owner, repo, rest = m.groups()
    ref, _, dirpath = rest.rpartition("/")
    if not ref or not dirpath:
        return base
    return "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (owner, repo, dirpath, ref)


def _resolve_base(base_url: str, token: Optional[str]) -> str:
    """소스 base URL 을 정규화한다(토큰이 있고 GitHub raw 면 contents API 로 변환)."""
    base = (base_url or DEFAULT_UPGRADE_BASE).rstrip("/")
    return _to_github_api(base) if token else base


def _join_url(base: str, name: str) -> str:
    """base 에 파일명을 붙인다. 쿼리(?ref=…)가 있으면 경로 끝(쿼리 앞)에 끼운다."""
    if "?" in base:
        head, _, query = base.partition("?")
        return head.rstrip("/") + "/" + name + "?" + query
    return base.rstrip("/") + "/" + name


def _auth_request(url: str, token: Optional[str]):
    """urllib Request 생성(토큰 있으면 Authorization; GitHub API 면 raw Accept)."""
    import urllib.request
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
        if "api.github.com" in url:
            headers["Accept"] = "application/vnd.github.raw"
    return urllib.request.Request(url, headers=headers)


def fetch_remote_versions(base_url: str, *, token: Optional[str] = None, timeout: float = 10.0):
    """base_url/versions.json 을 받아 (data, error). data={latest, versions:[...]}.

    base_url 은 이미 정규화된 base(check_remote 가 _resolve_base 로 변환). 토큰이 있으면 인증.
    """
    import json
    import urllib.request
    url = _join_url(base_url, "versions.json")
    try:
        with urllib.request.urlopen(_auth_request(url, token), timeout=timeout) as r:
            raw = r.read(4 * 1024 * 1024)
        return json.loads(raw.decode("utf-8")), None
    except Exception as exc:  # noqa: BLE001 — 네트워크/JSON 등 어떤 실패도 '미확인'으로
        return None, "버전 정보 조회 실패: %s" % exc


def check_remote(base_url: str, current_version: str, *, token: Optional[str] = None,
                 timeout: float = 10.0) -> dict:
    """원격 versions.json 으로 최신 버전을 확인한다(다운로드/설치는 하지 않음).

    반환: {ok, available, latest, current, tar_gz, size_bytes, download_url, checked_at, source}.
    token(PAT)을 주면 비공개(사설) 소스도 인증해서 확인한다.
    """
    base = _resolve_base(base_url, token)
    data, err = fetch_remote_versions(base, token=token, timeout=timeout)
    cur = parse_version(current_version) or (0, 0, 0)
    out = {"ok": err is None, "current": vstr(cur), "available": False,
           "checked_at": time.time(), "source": _join_url(base, "versions.json")}
    if err:
        out["error"] = err
        return out
    latest = str(data.get("latest") or "")
    lt = parse_version(latest)
    out["latest"] = latest
    out["available"] = bool(lt and lt > cur)
    for v in (data.get("versions") or []):
        if str(v.get("version")) == latest:
            out["tar_gz"] = v.get("tar_gz")
            out["size_bytes"] = v.get("size_bytes")
            out["sha256"] = v.get("sha256")   # 무결성 검증용(있으면 download 후 대조)
            if v.get("tar_gz"):
                out["download_url"] = _join_url(base, v["tar_gz"])
            break
    return out


def download_archive(url: str, dest_dir: str, *, token: Optional[str] = None,
                     timeout: float = 120.0, max_bytes: int = MAX_BUNDLE_BYTES,
                     expected_sha256: Optional[str] = None) -> dict:
    """원격 tar.gz/zip 을 dest_dir 에 내려받는다(파일명 검증·크기 상한·토큰 인증).

    반환 {ok, path, size} 또는 {ok:False, reason}.
    """
    import urllib.request
    name = os.path.basename((url or "").split("?")[0])
    if not _ARCHIVE_RE.search(name):
        return {"ok": False, "reason": "허용되지 않는 아카이브 파일명: %s" % (name or "(없음)")}
    try:
        os.makedirs(dest_dir, exist_ok=True)
        with urllib.request.urlopen(_auth_request(url, token), timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "다운로드 실패: %s" % exc}
    if len(data) > max_bytes:
        return {"ok": False, "reason": "다운로드가 너무 큼(>%d bytes)" % max_bytes}
    if expected_sha256:   # 무결성 검증(versions.json 의 sha256 과 대조) — 변조/탈취된 미러 차단
        import hashlib
        got = hashlib.sha256(data).hexdigest()
        if got.lower() != str(expected_sha256).strip().lower():
            return {"ok": False, "reason": "무결성 검증 실패: sha256 불일치(받은 %s…)" % got[:12]}
    dest = os.path.join(dest_dir, name)
    with open(dest, "wb") as fh:
        fh.write(data)
    return {"ok": True, "path": dest, "size": len(data)}


def upgrade_from_remote(base_url: str, code_dir: str, current_version: str,
                        dest_dir: str, *, token: Optional[str] = None,
                        timeout: float = 120.0) -> dict:
    """원격에서 최신 아카이브를 받아 설치까지 한다(재시작은 호출 측에서).

    반환: check_remote 결과 + 설치 결과(installed/version/from/backup) 또는 사유.
    """
    info = check_remote(base_url, current_version, token=token, timeout=min(timeout, 15))
    if not info.get("ok"):
        return {"ok": False, "reason": info.get("error", "버전 확인 실패"), "check": info}
    if not info.get("available"):
        return {"ok": False, "reason": "이미 최신입니다(%s)" % info.get("latest"),
                "check": info, "up_to_date": True}
    if not info.get("download_url"):
        return {"ok": False, "reason": "다운로드 URL 을 찾을 수 없음", "check": info}
    dl = download_archive(info["download_url"], dest_dir, token=token, timeout=timeout,
                          expected_sha256=info.get("sha256"))
    if not dl.get("ok"):
        return {"ok": False, "reason": dl.get("reason"), "check": info}
    res = upgrade_from_archive(dl["path"], code_dir, current_version)
    res["check"] = info
    res["downloaded"] = dl.get("size")
    return res


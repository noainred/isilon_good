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

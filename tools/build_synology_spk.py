#!/usr/bin/env python3
"""build_synology_spk.py — 번외 버전: The Davinci NAS Management 를 Synology DSM 7.x .spk 로 빌드.

이 프로그램은 순수 표준 라이브러리라 컴파일이 없어 **noarch** SPK 로 만든다(모든 시놀로지 CPU 공용).
실제 DSM 장비 없이 만들 수 있으나, 아래 두 가지는 실기기에서 확인 필요(스크립트가 견고하게 처리하지만):
  1) python3 위치 — DSM 7.2 는 시스템 python3 가 없을 수 있어, Package Center 의 'Python 3.9'
     패키지가 필요할 수 있다(start-stop-status 가 여러 경로를 탐색하고 없으면 안내 로그를 남긴다).
  2) 공유폴더(/volumeX) 읽기 — DSM7 이 써드파티 root 실행을 막으므로 샌드박스 사용자(run-as package)로
     돈다. 스캔할 공유폴더는 그 사용자(sc-isilon_usage)에게 읽기 권한을 줘야 한다(docs/SYNOLOGY.md).

출력: download/synology/isilon_usage-<version>.spk

.spk 구조(무압축 tar): INFO, package.tgz(gzip tar → 대상에 풀림), scripts/, conf/privilege,
                        PACKAGE_ICON.PNG(72), PACKAGE_ICON_256.PNG(256), LICENSE.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG_DIR = os.path.join(ROOT, "isilon_usage")
OUT_DIR = os.path.join(ROOT, "download", "synology")

PKG_NAME = "isilon_usage"
DISPLAY = "The Davinci NAS Management"
PORT = 8765


def _version() -> str:
    src = open(os.path.join(PKG_DIR, "__init__.py"), encoding="utf-8").read()
    return re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)


# ---- 아이콘(파비콘과 동일 컨셉: 파란 라운드 사각형 + 흰 N). PIL 있으면 예쁘게, 없으면 단색 폴백. ----
def _icon_png(size: int) -> bytes:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:  # noqa: BLE001
        # 최소 폴백: 단색 파란 정사각(투명 없음). PIL 없을 때만.
        from struct import pack
        import zlib
        w = h = size
        row = b"\x00" + bytes((74, 163, 255)) * w
        raw = row * h

        def chunk(t, d):
            c = t + d
            return pack(">I", len(d)) + c + pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(raw, 9))
        png += chunk(b"IEND", b"")
        return png
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rad = int(size * 0.22)
    # 그라데이션 라운드 사각형
    grad = Image.new("RGB", (size, size))
    gd = grad.load()
    for y in range(size):
        for x in range(size):
            f = (x + y) / (2 * size)
            gd[x, y] = (int(74 + (122 - 74) * f), int(163 + (208 - 163) * f), 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=rad, fill=255)
    img.paste(grad, (0, 0), mask)
    # 흰 N
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(size * 0.6))
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    tw = d.textbbox((0, 0), "N", font=font)
    cx = (size - (tw[2] - tw[0])) / 2 - tw[0]
    cy = (size - (tw[3] - tw[1])) / 2 - tw[1]
    d.text((cx, cy), "N", font=font, fill=(8, 32, 58, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---- payload: app/isilon_usage/*  (.py/.html/.js) ----
def _package_tgz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(os.listdir(PKG_DIR)):
            if name.endswith((".py", ".html", ".js")):
                tf.add(os.path.join(PKG_DIR, name), arcname="app/isilon_usage/" + name)
    return buf.getvalue()


START_STOP_STATUS = r"""#!/bin/sh
# The Davinci NAS Management — Synology service control
PKG="isilon_usage"
APPDIR="${SYNOPKG_PKGDEST}/app"
DATADIR="${SYNOPKG_PKGVAR}/data"      # DSM7: /var/packages/<pkg>/var — 업그레이드에도 보존
PORT=__PORT__
PIDFILE="${SYNOPKG_PKGVAR}/${PKG}.pid"
LOG="${SYNOPKG_PKGVAR}/${PKG}.log"

find_python() {
  for p in /usr/local/bin/python3 /usr/bin/python3 \
           /var/packages/Python3.9/target/usr/local/bin/python3 \
           /var/packages/Python3.9/target/bin/python3 \
           /var/packages/python3/target/usr/local/bin/python3 ; do
    [ -x "$p" ] && { echo "$p"; return 0; }
  done
  command -v python3 2>/dev/null && return 0
  return 1
}

start_daemon() {
  mkdir -p "$DATADIR"
  PY=$(find_python) || {
    echo "python3 not found. Install 'Python 3.9' from Package Center, then start again." \
      > "${SYNOPKG_TEMP_LOGFILE:-$LOG}"
    return 1
  }
  PYTHONPATH="$APPDIR" "$PY" -m isilon_usage serve \
      --data-dir "$DATADIR" --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  return 0
}

stop_daemon() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
  fi
  pkill -f "isilon_usage serve --data-dir ${DATADIR}" 2>/dev/null
  return 0
}

daemon_status() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "$1" in
  start)   daemon_status && exit 0; start_daemon; exit $? ;;
  stop)    stop_daemon; exit 0 ;;
  status)  daemon_status && exit 0 || exit 1 ;;
  log)     echo "$LOG"; exit 0 ;;
  *)       exit 0 ;;
esac
""".replace("__PORT__", str(PORT))

# DSM 7 은 써드파티(수동설치) 패키지의 run-as root 를 차단한다("루트 권한으로 실행 중이므로 설치할 수
# 없습니다"). 그래서 샌드박스 사용자(sc-isilon_usage)로 실행한다. 공유폴더 스캔은 그 사용자에게 읽기
# 권한을 줘야 한다(docs/SYNOLOGY.md 참고). 포트 8765(>1024)·데이터 폴더 쓰기는 이 권한으로 충분하다.
PRIVILEGE = '{\n  "defaults": { "run-as": "package" }\n}\n'

LICENSE = ("Copyright (c) 2026 Park Junho. All rights reserved. "
           "Proprietary — unauthorized reproduction or distribution is prohibited.\n")


def _info(checksum: str, version: str) -> bytes:
    lines = {
        "package": PKG_NAME,
        "version": version + "-1",
        "os_min_ver": "7.0-40000",
        "arch": "noarch",
        "firmware": "7.0-40000",
        "maintainer": "Park Junho",
        "displayname": DISPLAY,
        "description": "Very-large NAS/Isilon directory usage scanner & dashboard. "
                       "Pure Python stdlib (no dependencies). Web UI on port %d." % PORT,
        "adminport": str(PORT),
        "adminprotocol": "http",
        "adminurl": "/",
        "dsmappname": "SYNO.SDS.iusage.Instance",
        "ctl_stop": "yes",
        "startable": "yes",
        "silent_install": "yes",
        "silent_upgrade": "yes",
        "thirdparty": "yes",
        "checksum": checksum,
        "support_conf_folder": "yes",
    }
    return "".join('%s="%s"\n' % (k, v) for k, v in lines.items()).encode("utf-8")


def _add(tf: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = 0            # 결정적 빌드(Date.now 안 씀)
    ti.uid = ti.gid = 0
    tf.addfile(ti, io.BytesIO(data))


def build() -> str:
    version = _version()
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = _package_tgz()
    checksum = hashlib.md5(payload).hexdigest()   # noqa: S324 — DSM INFO 규약이 md5

    out = os.path.join(OUT_DIR, "%s-%s.spk" % (PKG_NAME, version))
    # .spk = 무압축 tar
    with tarfile.open(out, mode="w") as tf:
        _add(tf, "INFO", _info(checksum, version))
        _add(tf, "package.tgz", payload)
        _add(tf, "scripts/start-stop-status", START_STOP_STATUS.encode("utf-8"), mode=0o755)
        _add(tf, "conf/privilege", PRIVILEGE.encode("utf-8"))
        _add(tf, "PACKAGE_ICON.PNG", _icon_png(72))
        _add(tf, "PACKAGE_ICON_256.PNG", _icon_png(256))
        _add(tf, "LICENSE", LICENSE.encode("utf-8"))
    return out


if __name__ == "__main__":
    path = build()
    print("SPK 생성:", path, "(%.1f KB)" % (os.path.getsize(path) / 1024))

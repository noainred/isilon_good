#!/usr/bin/env python3
"""오프라인(폐쇄망) 배포용 압축본을 만든다.

표준 라이브러리만으로 동작하므로 git 이 없는 폐쇄망 서버에서도 압축을 풀어
바로 `python3 -m isilon_usage ...` 로 실행할 수 있다.

download/ 아래에 다음을 생성한다(버전은 isilon_usage.__version__ 사용):
  isilon_usage-<버전>.tar.gz   (리눅스 서버용)
  isilon_usage-<버전>.zip      (윈도우 PC 등에서 받아 scp 할 때)

사용: python3 tools/make_release.py
"""

from __future__ import annotations

import gzip
import os
import sys
import tarfile
import tempfile
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from isilon_usage import __version__  # noqa: E402

# 압축본에 담을 "최종 파일"(실행에 필요한 것 + 문서)
INCLUDE_FILES = ["README.md", "CHANGELOG.md", "requirements.txt",
                 "pyproject.toml", "Dockerfile"]
INCLUDE_DIRS = ["docs", "tools"]   # isilon_usage 패키지는 아래에서 별도 처리

# 재현 가능한(결정적) 압축을 위한 고정 타임스탬프 — 내용이 같으면 바이트도 동일.
_FIXED_DT = (2020, 1, 1, 0, 0, 0)
_FIXED_EPOCH = 1577836800


def _collect(stage: str, name: str):
    files = []
    base = os.path.join(stage, name)
    for root, dirs, fs in os.walk(base):
        dirs.sort()
        for f in sorted(fs):
            full = os.path.join(root, f)
            files.append((full, os.path.relpath(full, stage)))
    return files


def _make_targz(path: str, files) -> str:
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for full, arc in files:
                    ti = tar.gettarinfo(full, arcname=arc)
                    ti.mtime = _FIXED_EPOCH
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with open(full, "rb") as fh:
                        tar.addfile(ti, fh)
    return path


def _make_zip(path: str, files) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for full, arc in files:
            zi = zipfile.ZipInfo(arc, date_time=_FIXED_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with open(full, "rb") as fh:
                z.writestr(zi, fh.read())
    return path


def build() -> list:
    name = f"isilon_usage-{__version__}"
    stage = tempfile.mkdtemp(prefix="isilon_rel_")
    dest = os.path.join(stage, name)
    os.makedirs(dest)

    # 패키지(대시보드 HTML 포함, __pycache__ 제외)
    shutil.copytree(
        os.path.join(ROOT, "isilon_usage"),
        os.path.join(dest, "isilon_usage"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for d in INCLUDE_DIRS:
        src = os.path.join(ROOT, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, d),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in INCLUDE_FILES:
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            shutil.copy2(src, dest)

    out_dir = os.path.join(ROOT, "download")
    os.makedirs(out_dir, exist_ok=True)
    files = _collect(stage, name)
    targz = _make_targz(os.path.join(out_dir, f"{name}.tar.gz"), files)
    zipf = _make_zip(os.path.join(out_dir, f"{name}.zip"), files)
    # 버전이 바뀌어도 wget 링크가 그대로이도록 고정 이름(latest) 사본도 둔다.
    latest_targz = os.path.join(out_dir, "isilon_usage-latest.tar.gz")
    latest_zip = os.path.join(out_dir, "isilon_usage-latest.zip")
    shutil.copy2(targz, latest_targz)
    shutil.copy2(zipf, latest_zip)
    shutil.rmtree(stage, ignore_errors=True)
    return [targz, zipf, latest_targz, latest_zip]


if __name__ == "__main__":
    for path in build():
        size = os.path.getsize(path)
        print(f"생성: {os.path.relpath(path, ROOT)}  ({size/1024:.0f} KB)")

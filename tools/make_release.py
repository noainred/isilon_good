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

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from isilon_usage import __version__  # noqa: E402

# 압축본에 담을 "최종 파일"(실행에 필요한 것 + 문서)
INCLUDE_FILES = ["README.md", "CHANGELOG.md", "requirements.txt",
                 "pyproject.toml", "Dockerfile"]
INCLUDE_DIRS = ["docs", "tools"]   # isilon_usage 패키지는 아래에서 별도 처리


def build() -> list[str]:
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
    base = os.path.join(out_dir, name)
    made = [
        shutil.make_archive(base, "gztar", root_dir=stage, base_dir=name),
        shutil.make_archive(base, "zip", root_dir=stage, base_dir=name),
    ]
    shutil.rmtree(stage, ignore_errors=True)
    return made


if __name__ == "__main__":
    for path in build():
        size = os.path.getsize(path)
        print(f"생성: {os.path.relpath(path, ROOT)}  ({size/1024:.0f} KB)")

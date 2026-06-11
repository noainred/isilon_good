#!/usr/bin/env python3
"""오프라인(폐쇄망) 배포용 압축본을 만든다 — 버전별로 보관.

표준 라이브러리만으로 동작하므로 git 이 없는 폐쇄망 서버에서도 압축을 풀어
바로 `python3 -m isilon_usage ...` 로 실행할 수 있다.

download/ 아래에 다음을 생성/유지한다(버전을 지우지 않고 누적):
  isilon_usage-<버전>.tar.gz / .zip   버전 고정 본(여러 버전 공존)
  isilon_usage-latest.tar.gz  / .zip   최신본(URL 이 안 바뀜)
  versions.json                        버전 목록(자동)
그리고 download/README.md 의 버전 표를 자동 갱신한다.

과거 버전은 git 커밋에서 빌드한다(아래 HISTORY).
사용: python3 tools/make_release.py
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from isilon_usage import __version__  # noqa: E402

# 압축본에 담을 "최종 파일"(실행에 필요한 것 + 문서)
INCLUDE_FILES = ["README.md", "CHANGELOG.md", "LICENSE", "SECURITY.md",
                 "requirements.txt",
                 "pyproject.toml", "setup.cfg", "setup.py", "MANIFEST.in", "Dockerfile"]
INCLUDE_DIRS = ["docs", "tools"]   # isilon_usage 패키지는 별도 처리

# 과거 버전 → git 커밋(현재 버전은 작업트리에서 빌드하므로 제외)
HISTORY = {
    "1.0.0": "34d88f6",
    "1.1.0": "0b1ef3a",
    "1.1.1": "cc19374",
    "1.1.2": "51374e5",
    "1.1.3": "ad19214",
    "1.1.4": "2c56047",
    "1.1.5": "ac333d8",
    "1.1.6": "88a5475",
    "1.1.7": "6aff1bb",
    "1.1.8": "625fd1e",
    "1.1.9": "4fe4f3e",
    "1.2.0": "40bde7c",
    "1.3.0": "bbe958e",
    "1.3.1": "3254f43",
    "1.4.0": "b21bba2",
    "1.4.1": "321e37c",
    "1.4.2": "73548b2",
    "1.5.0": "8c92cca",
    "1.6.0": "9bc47af",
    "1.7.0": "15a5716",
    "1.7.1": "41c3b67",
    "1.8.0": "42989d3",
    "1.8.1": "7f766a9",
    "1.9.0": "3a35f7e",
    "1.10.0": "02592e2",
    "1.11.0": "cc5867c",
    "1.12.0": "2cb64bc",
    "1.13.0": "d0c0657",
    "1.14.0": "263bd5b",
    "1.15.0": "ca17dec",
    "1.16.0": "3f8d9cb",
    "1.16.1": "b0bea62",
    "1.17.0": "ae8d2ab",
    "1.18.0": "7e8c98a",
    "1.18.1": "26ea1a0",
    "1.19.0": "5993f47",
    "1.19.1": "8ab08e3",
    "1.19.2": "d13dabb",
    "1.20.0": "4ecef8a",
    "1.21.0": "5c3aa01",
    "1.21.1": "2b1d9e2",
    "1.22.0": "a0174e9",
    "1.23.0": "ed8c40a",
    "1.23.1": "26650d1",
    "1.23.2": "cfefc95",
    "1.24.0": "a1de818",
    "1.25.0": "e51151a",
    "1.26.0": "4e186d1",
    "1.27.0": "79be7af",
    "1.27.1": "8daef43",
}
# 버전별 파이썬 호환(없으면 기본값). 1.1.1 부터 3.6 호환.
PY_COMPAT = {"1.0.0": "Python 3.7+", "1.1.0": "Python 3.7+"}
DEFAULT_COMPAT = "Python 3.6+"

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


def build_from(src_dir: str, version: str, out_dir: str):
    """src_dir(프로그램 소스 루트)에서 curated 압축본을 버전명으로 만든다."""
    name = f"isilon_usage-{version}"
    stage = tempfile.mkdtemp(prefix="isilon_rel_")
    dest = os.path.join(stage, name)
    os.makedirs(dest)
    shutil.copytree(
        os.path.join(src_dir, "isilon_usage"),
        os.path.join(dest, "isilon_usage"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for d in INCLUDE_DIRS:
        s = os.path.join(src_dir, d)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(dest, d),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in INCLUDE_FILES:
        s = os.path.join(src_dir, f)
        if os.path.isfile(s):
            shutil.copy2(s, dest)

    files = _collect(stage, name)
    targz = _make_targz(os.path.join(out_dir, f"{name}.tar.gz"), files)
    zipf = _make_zip(os.path.join(out_dir, f"{name}.zip"), files)
    shutil.rmtree(stage, ignore_errors=True)
    return targz, zipf


def _extract_commit(commit: str) -> str:
    """git 커밋의 트리를 임시 디렉터리에 풀어 그 경로를 반환."""
    d = tempfile.mkdtemp(prefix="isilon_hist_")
    p = subprocess.Popen(["git", "archive", commit], stdout=subprocess.PIPE, cwd=ROOT)
    subprocess.check_call(["tar", "-x", "-C", d], stdin=p.stdout)
    p.stdout.close()
    p.wait()
    return d


def _ver_key(v: str):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def write_index(out_dir: str) -> None:
    """download/ 의 버전들을 모아 versions.json + README 표를 갱신."""
    versions = sorted(
        {m.group(1) for f in os.listdir(out_dir)
         for m in [re.match(r"isilon_usage-(\d+\.\d+\.\d+)\.tar\.gz$", f)] if m},
        key=_ver_key, reverse=True,
    )
    latest = max(versions, key=_ver_key) if versions else None
    entries = []
    for v in versions:
        tar = f"isilon_usage-{v}.tar.gz"
        zf = f"isilon_usage-{v}.zip"
        size = os.path.getsize(os.path.join(out_dir, tar))
        entries.append({
            "version": v, "python": PY_COMPAT.get(v, DEFAULT_COMPAT),
            "tar_gz": tar, "zip": zf, "size_bytes": size,
        })
    with open(os.path.join(out_dir, "versions.json"), "w", encoding="utf-8") as fh:
        json.dump({"latest": latest, "versions": entries}, fh,
                  ensure_ascii=False, indent=2)

    # README 표(마커 사이) 갱신
    base = "https://github.com/noainred/isilon_good/raw/claude/upbeat-bell-cXX8f/download"
    rows = ["| 버전 | 호환 | tar.gz | zip |", "|------|------|--------|-----|"]
    for e in entries:
        tag = " (latest)" if e["version"] == latest else ""
        rows.append(
            f"| **{e['version']}**{tag} | {e['python']} | "
            f"[tar.gz]({base}/{e['tar_gz']}) ({e['size_bytes']//1024} KB) | "
            f"[zip]({base}/{e['zip']}) |"
        )
    table = "\n".join(rows)
    readme = os.path.join(out_dir, "README.md")
    if os.path.exists(readme):
        txt = open(readme, encoding="utf-8").read()
        new = re.sub(r"<!-- VERSIONS:START -->.*<!-- VERSIONS:END -->",
                     "<!-- VERSIONS:START -->\n" + table + "\n<!-- VERSIONS:END -->",
                     txt, flags=re.S)
        if new != txt:
            open(readme, "w", encoding="utf-8").write(new)


def main() -> int:
    out_dir = os.path.join(ROOT, "download")
    os.makedirs(out_dir, exist_ok=True)

    # 1) 현재 버전(작업트리)
    targz, zipf = build_from(ROOT, __version__, out_dir)
    shutil.copy2(targz, os.path.join(out_dir, "isilon_usage-latest.tar.gz"))
    shutil.copy2(zipf, os.path.join(out_dir, "isilon_usage-latest.zip"))
    print(f"현재: {os.path.basename(targz)} (+ latest)")

    # 2) 과거 버전(없으면 git 에서 빌드)
    for ver, commit in HISTORY.items():
        if os.path.exists(os.path.join(out_dir, f"isilon_usage-{ver}.tar.gz")):
            continue
        try:
            src = _extract_commit(commit)
            build_from(src, ver, out_dir)
            shutil.rmtree(src, ignore_errors=True)
            print(f"과거: isilon_usage-{ver}.tar.gz (commit {commit})")
        except Exception as exc:  # noqa: BLE001
            print(f"  경고: {ver} 빌드 실패: {exc}", file=sys.stderr)

    # 3) 인덱스 갱신
    write_index(out_dir)
    print("versions.json + README 버전 표 갱신 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

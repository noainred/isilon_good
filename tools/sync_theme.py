#!/usr/bin/env python3
"""isilon_usage/theme.css(원본) → dashboard.html·portal.html 안의 테마 블록으로 복사한다.

왜: 화면이 별도 요청(/theme.css)에 기대면, 업그레이드 도중 '새 HTML + 옛 서버 프로세스'이거나
css 가 빠진 번들(구버전 포탈의 에이전트 번들은 .css 를 담지 않는다)일 때 스타일 없이 깨진 화면이 뜬다
(1.99.43 실제 사고). HTML 자체에 테마를 넣어 어떤 경로로 배포돼도 같은 모양이 나오게 한다.

원본은 theme.css 하나다(단일 소스). 테마를 고친 뒤 이 스크립트를 실행하고, 테스트
(tests/test_ui_theme.py)가 HTML 안의 블록이 원본과 같은지 확인한다.

사용: python3 tools/sync_theme.py          # 동기화
      python3 tools/sync_theme.py --check  # 어긋났으면 종료코드 1
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "isilon_usage")
PAGES = ("dashboard.html", "portal.html")
BEGIN = "<!-- THEME:BEGIN (tools/sync_theme.py 가 isilon_usage/theme.css 에서 생성 — 직접 고치지 말 것) -->"
END = "<!-- THEME:END -->"
_BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
_LINK = re.compile(r'<link rel="stylesheet" href="/theme\.css" />')


def block() -> str:
    with open(os.path.join(PKG, "theme.css"), encoding="utf-8") as fh:
        css = fh.read()
    return BEGIN + "\n<style>\n" + css.rstrip("\n") + "\n</style>\n" + END


def synced(text: str) -> str:
    b = block()
    if _BLOCK.search(text):
        return _BLOCK.sub(lambda _m: b, text, count=1)
    if _LINK.search(text):
        return _LINK.sub(lambda _m: b, text, count=1)
    raise SystemExit("테마 자리(THEME 블록 또는 /theme.css 링크)를 찾지 못함")


def main(argv) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    check = "--check" in argv
    bad = []
    for name in PAGES:
        p = os.path.join(PKG, name)
        with open(p, encoding="utf-8") as fh:
            old = fh.read()
        new = synced(old)
        if new != old:
            bad.append(name)
            if not check:
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(new)
    if check:
        if bad:
            print("테마가 원본(theme.css)과 다름: %s — python3 tools/sync_theme.py 실행" % ", ".join(bad))
            return 1
        print("테마 동기화 OK")
        return 0
    print("동기화함: %s" % (", ".join(bad) or "변경 없음"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

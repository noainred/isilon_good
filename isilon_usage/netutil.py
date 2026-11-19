"""HTTP 공용 헬퍼 — 모든 외부/노드 호출이 같은 urlopen 을 쓴다(단일 소스).

표준 urllib 은 리다이렉트를 따라갈 때 Authorization 같은 인증 헤더를 **다른 호스트로도 그대로**
넘긴다(CPython HTTPRedirectHandler.redirect_request 는 Content-* 만 뺀다). 그러면 엣지 토큰·
스토리지 관리자 Basic 인증·GitHub PAT·LLM 키가 리다이렉트 대상에 노출된다. 여기서는 출발지와
다른 origin(스킴·호스트·포트)으로 넘어갈 때 인증 헤더를 떼어 낸다. 같은 origin 이면 그대로 둔다.
"""

import urllib.parse
import json
import urllib.request

# 리다이렉트로 다른 origin 에 넘기지 않을 헤더(소문자). urllib 은 키를 capitalize() 해서 보관한다.
# VPLEX 는 계정을 Username/Password 헤더로 보낸다 — 빠져 있어 다른 서버로의 리다이렉트에 비밀번호가 넘어갔다(21회차 점검).
_AUTH_HEADERS = {"authorization", "x-auth-token", "x-op-token", "dell-emc-token", "cookie",
                 "proxy-authorization", "username", "password", "x-api-key", "api-key"}


def _origin(url: str):
    p = urllib.parse.urlsplit(url)
    port = p.port or {"http": 80, "https": 443}.get(p.scheme)
    return (p.scheme.lower(), (p.hostname or "").lower(), port)


class _StripAuthOnCrossOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for k in list(new.headers):
                if k.lower() in _AUTH_HEADERS:
                    del new.headers[k]
            for k in list(new.unredirected_hdrs):
                if k.lower() in _AUTH_HEADERS:
                    del new.unredirected_hdrs[k]
        return new


def urlopen(req, timeout=None, context=None):
    """urllib.request.urlopen 대체 — 다른 origin 리다이렉트 시 인증 헤더 제거. context=SSL 컨텍스트."""
    handlers = [_StripAuthOnCrossOrigin()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    if timeout is None:
        return opener.open(req)
    return opener.open(req, timeout=timeout)


MAX_JSON_RESPONSE = 8 * 1024 * 1024


def read_json(resp, limit: int = MAX_JSON_RESPONSE):
    """응답을 상한(8MB)까지 읽어 JSON 으로 — 엉뚱한 주소가 큰 파일을 주면 통째로 읽어 메모리가 800MB 를 넘었고,
    로그인 HTML 이면 'Expecting value: line 1 column 1' 같은 알아보기 힘든 오류였다(21회차 점검)."""
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("응답이 너무 큽니다(%dMB 초과) — 주소(API 경로)를 확인하세요" % (limit // (1024 * 1024)))
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        head = raw[:60].decode("utf-8", "replace").strip().replace("\n", " ")
        raise ValueError("JSON 이 아닌 응답입니다(주소·포트·API 경로 확인): %s" % head)

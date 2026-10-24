"""HTTP 공용 헬퍼 — 모든 외부/노드 호출이 같은 urlopen 을 쓴다(단일 소스).

표준 urllib 은 리다이렉트를 따라갈 때 Authorization 같은 인증 헤더를 **다른 호스트로도 그대로**
넘긴다(CPython HTTPRedirectHandler.redirect_request 는 Content-* 만 뺀다). 그러면 엣지 토큰·
스토리지 관리자 Basic 인증·GitHub PAT·LLM 키가 리다이렉트 대상에 노출된다. 여기서는 출발지와
다른 origin(스킴·호스트·포트)으로 넘어갈 때 인증 헤더를 떼어 낸다. 같은 origin 이면 그대로 둔다.
"""

import urllib.parse
import urllib.request

# 리다이렉트로 다른 origin 에 넘기지 않을 헤더(소문자). urllib 은 키를 capitalize() 해서 보관한다.
_AUTH_HEADERS = {"authorization", "x-auth-token", "x-op-token", "dell-emc-token", "cookie",
                 "proxy-authorization"}


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

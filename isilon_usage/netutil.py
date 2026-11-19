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


# --------------------------------------------------------------- URL 비밀값 가리기
# 설정 응답(GET — 인증 없이 읽힘)에 URL 속 비밀값(userinfo·쿼리 토큰, 웹훅 경로)이 그대로 나가던 문제(27회차 보안 점검).
# 화면엔 가린 값을 보여 주고, 그 가린 값이 그대로 저장 요청에 돌아오면 서버가 저장된 원래 값을 유지한다.
_MASK = "***"
MASK = _MASK


def mask_url_secret(u, hide_path: bool = False) -> str:
    """URL 의 사용자 정보·쿼리(·hide_path 면 경로)를 '***' 로 가린다. 가릴 게 없으면 그대로."""
    s = str(u or "").strip()
    if not s:
        return s
    try:
        from urllib.parse import urlsplit
        p = urlsplit(s)
    except ValueError:
        return _MASK
    if not p.scheme or not p.netloc:
        # 주소 모양이 아니면(오타 등) 통째로 가린다 — 무엇이 비밀인지 모른다
        return _MASK if (hide_path or "@" in s or "?" in s) else s
    host = p.netloc.rsplit("@", 1)[-1]
    out = p.scheme + "://" + (_MASK + "@" if "@" in p.netloc else "") + host
    path = p.path or ""
    if hide_path and path.strip("/"):
        out += "/" + _MASK
    else:
        out += path
    if p.query or p.fragment:
        out += "?" + _MASK
    return out


def is_masked_leftover(value, stored) -> bool:
    """가린 표시('***')가 남은 주소인가(저장된 원래 값과 다를 때만) — 일부만 고친 가린 주소를 걸러낸다."""
    v = str(value or "")
    if _MASK not in v or v.strip() == str(stored or "").strip():
        return False
    return ("://" + _MASK + "@") in v or ("?" + _MASK) in v or v.rstrip("/").endswith("/" + _MASK) or v.strip() == _MASK


def keep_masked(posted, stored, hide_path: bool = False):
    """저장 요청 값이 '저장된 값을 가린 모양' 그대로면 저장된 값을, 아니면 보낸 값을 돌려준다."""
    if not isinstance(posted, str):
        return posted
    st = str(stored or "")
    if st and posted.strip() == mask_url_secret(st, hide_path) and posted.strip() != st.strip():
        return st
    return posted


_URL_RE = None


def redact_urls(obj):
    """응답 안의 모든 문자열에서 URL 의 비밀값(사용자 정보·쿼리)을 가린다(재귀, 새 객체 반환).

    저장소 상태의 base·업그레이드 상태의 source/download_url·오류/로그 문구처럼 설정 URL 에서 파생된 값이
    인증 없는 조회로 그대로 나가던 문제(28회차 점검) — 응답을 내보내기 직전 한 곳에서 처리한다."""
    global _URL_RE
    if _URL_RE is None:
        import re
        _URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>]+")
    if isinstance(obj, str):
        if "://" not in obj:
            return obj
        return _URL_RE.sub(lambda m: mask_url_secret(m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: redact_urls(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_urls(v) for v in obj]
    return obj

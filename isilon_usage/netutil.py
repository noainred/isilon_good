"""HTTP 공용 헬퍼 — 모든 외부/노드 호출이 같은 urlopen 을 쓴다(단일 소스).

표준 urllib 은 리다이렉트를 따라갈 때 Authorization 같은 인증 헤더를 **다른 호스트로도 그대로**
넘긴다(CPython HTTPRedirectHandler.redirect_request 는 Content-* 만 뺀다). 그러면 엣지 토큰·
스토리지 관리자 Basic 인증·GitHub PAT·LLM 키가 리다이렉트 대상에 노출된다. 여기서는 출발지와
다른 origin(스킴·호스트·포트)으로 넘어갈 때 인증 헤더를 떼어 낸다. 같은 origin 이면 그대로 둔다.
"""

import re
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


def same_origin(a, b) -> bool:
    """두 URL 이 같은 곳(스킴·호스트·포트)인가 — 저장된 비밀(토큰·키)을 새 주소로 넘겨도 되는지 판단(29회차 점검)."""
    try:
        return _origin(str(a or "").strip()) == _origin(str(b or "").strip())
    except ValueError:
        return False


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


# 호스트[:포트] — 한글 등 국제화 도메인(IDN)도 받는다(35회차: '서버.kr/x?u=a@b.com' 의 호스트를 계정으로 오인)
_HOST_RE = re.compile(r"^(\[[0-9A-Za-z:.%]+\]|[^\s@/?#:\[\]&=;,'\"<>\\|`]+)(:\d{0,5})?$")   # 쿼리 글자(&=;)는 호스트가 아님(36회차)


def _split_userinfo(s: str):
    """'scheme://[userinfo@]rest' 를 (scheme, userinfo 또는 None, rest) 로 — 계정은 '://' 뒤 '마지막 @' 까지로 본다.

    urlsplit 은 넷로크를 첫 '/'·'?'·'#' 에서 끊어, 비밀번호에 그 글자가 든 주소(https://admin:PW/xx@host)의 계정을 못 알아봤다
    (33회차 보안 점검: 로그인 없이 비밀번호 노출). 경로 안의 '@' 까지 계정으로 볼 수 있지만, 가리는 쪽으로 틀리는 것이 안전하다."""
    k = s.find("://")
    if k <= 0:
        return None, None, s
    scheme, rest = s[:k], s[k + 3:]
    m = re.match(r"[^/?#]*", rest)
    auth = m.group(0) if m else ""
    if "@" in auth:                         # 보통: 계정은 호스트 구간(첫 '/'·'?'·'#' 앞) 안에만
        ui, _, host = auth.rpartition("@")
        return scheme, ui, host + rest[len(auth):]
    # 호스트 구간이 올바른 호스트[:포트]면 뒤의 '@'(경로·쿼리의 a@b.com)는 계정이 아니다(34회차 점검: 정상 주소의 호스트가 사라짐).
    # 호스트로 읽히지 않을 때만(예: 비밀번호에 '/' 가 든 admin:PW/xx@host) 마지막 '@' 까지를 계정으로 본다.
    if "@" in rest:
        ui, _, rest2 = rest.rpartition("@")
        if not _HOST_RE.match(auth):
            return scheme, ui, rest2
        # 'admin:12345/x@host.example' — 숫자로만 된 비밀번호면 호스트 구간이 'admin:12345'(호스트:포트)처럼 보여 계정을 놓쳤다
        # (35회차 보안 점검). 호스트 구간에 점·대괄호가 없고(점 없는 이름:숫자) 마지막 '@' 뒤가 점 있는 호스트면 계정으로 본다 —
        # 'localhost:8080/?u=a@b.com' 같은 주소는 더 가려질 수 있지만(표시만), 비밀이 새는 쪽보다 낫다.
        # 점 없는 호스트(nas01)·대괄호 IPv6 뒤에서도 잡는다(36회차 점검) — 표시만 더 가려질 수 있다(저장은 원래 값으로 돌아옴)
        _hn = auth.split(":", 1)[0]
        _after = re.match(r"[^/?#]*", rest2).group(0)
        if ":" in auth and "." not in _hn and "[" not in auth and _after and _HOST_RE.match(_after):
            return scheme, ui, rest2
    return scheme, None, rest


def mask_url_secret(u, hide_path: bool = False) -> str:
    """URL 의 사용자 정보·쿼리(·hide_path 면 경로)를 '***' 로 가린다. 가릴 게 없으면 그대로."""
    s = str(u or "").strip()
    if not s:
        return s
    scheme, ui, rest = _split_userinfo(s)
    if scheme is None or not rest:
        # 주소 모양이 아니면(오타 등) 통째로 가린다 — 무엇이 비밀인지 모른다
        return _MASK if (hide_path or "@" in s or "?" in s) else s
    m = re.match(r"([^/?#]*)(/[^?#]*)?", rest)
    host = m.group(1) if m else ""
    if not host or not _HOST_RE.match(host):
        return _MASK                        # 호스트를 못 읽으면(공백·쿼리 글자 등) 통째로 — 토큰이 호스트 자리로 새지 않게(36회차)
    path = (m.group(2) or "") if m else ""
    out = scheme + "://" + (_MASK + "@" if ui is not None else "") + host
    if hide_path and path.strip("/"):
        out += "/" + _MASK
    else:
        out += path
    if len(rest) > len(host) + len(path):   # 쿼리·조각(#…)
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


def secret_values(*urls_or_tokens) -> list:
    """설정의 주소·토큰에서 응답에 나가면 안 되는 비밀 조각(계정 비밀번호·user:pw·쿼리 값·토큰)을 모은다."""
    out = []
    from urllib.parse import urlsplit, parse_qsl
    for v in urls_or_tokens:
        s = str(v or "").strip()
        if not s:
            continue
        if "://" not in s:
            if len(s) >= 8:
                out.append(s)          # 토큰 그대로(짧은 값은 흔한 글자와 겹쳐 가리지 않음)
            continue
        try:
            p = urlsplit(s)
        except ValueError:
            continue
        # 비밀번호가 있는 계정만(사용자 이름만 있는 'x@host' 는 비밀이 아니다), 쿼리는 비밀 이름의 값만 — 너무 넓게 가려
        # 'main'·'true'·'admin' 같은 낱말이 로그에서 지워졌다(33회차 점검)
        ui = _split_userinfo(s)[1]          # 비밀번호에 '/'·'#' 가 있어도(33회차)
        if ui:
            pw = ui.partition(":")[2] if ":" in ui else ""
            if len(pw) >= 4:
                out.append(ui)
                out.append(pw)
                # 라이브러리 오류 문구는 비밀번호를 '/'·'?'·'#' 앞에서 잘라 'nonnumeric port: PW앞부분' 으로 싣는다(36회차 보안 점검)
                _pre = re.split(r"[/?#]", pw, maxsplit=1)[0]
                if len(_pre) >= 4 and _pre != pw:
                    out.append(_pre)
        for _k, val in parse_qsl(p.query, keep_blank_values=False):
            if len(val) >= 4 and re.search(r"(token|key|pass|secret|sig|auth|cred|session|code)", _k, re.I):
                out.append(val)
    return sorted(set(out), key=len, reverse=True)


def credential_values(user, password) -> list:
    """장비 계정의 비밀 조각 — 비밀번호 그대로와 Basic 인증 머리글 값(base64(user:pw)).
    장비가 요청을 그대로 되돌려 보내면 오류 문구에 실렸다(36회차 보안 점검)."""
    import base64
    pw = str(password or "")
    if len(pw) < 4:
        return []
    out = [pw]
    try:
        out.append(base64.b64encode(("%s:%s" % (user or "", pw)).encode("utf-8")).decode("ascii"))
    except Exception:  # noqa: BLE001
        pass
    return out


def redact_values(obj, secrets):
    """응답 안의 모든 문자열에서 주어진 비밀 조각을 '***' 로(주소 모양이 아닌 오류 문구 속 조각까지, 32회차 점검)."""
    if not secrets:
        return obj
    if isinstance(obj, str):
        for sv in secrets:
            if sv and sv in obj:
                obj = obj.replace(sv, _MASK)
        return obj
    if isinstance(obj, dict):
        return {k: redact_values(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_values(v, secrets) for v in obj]
    return obj


def redact_urls(obj):
    """응답 안의 모든 문자열에서 URL 의 비밀값(사용자 정보·쿼리)을 가린다(재귀, 새 객체 반환).

    저장소 상태의 base·업그레이드 상태의 source/download_url·오류/로그 문구처럼 설정 URL 에서 파생된 값이
    인증 없는 조회로 그대로 나가던 문제(28회차 점검) — 응답을 내보내기 직전 한 곳에서 처리한다."""
    global _URL_RE
    if _URL_RE is None:
        import re
        # 스킴은 단어 경계에서 시작·32자 이내(긴 영숫자열에서 느려지지 않게), 끝의 문장부호 ),;] 는 URL 에 넣지 않는다(29회차 점검)
        _URL_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s\"'<>]*[^\s\"'<>),;\]]")
    if isinstance(obj, str):
        if "://" not in obj:
            return obj
        return _URL_RE.sub(lambda m: mask_url_secret(m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: redact_urls(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_urls(v) for v in obj]
    return obj

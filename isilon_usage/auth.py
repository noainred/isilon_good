"""사용자 인증(작업 보호) — 평문 또는 암호화(PBKDF2 해시) 비밀번호 + 세션 토큰.

스캐너(server)와 포탈(portal)이 공용으로 쓴다. 모델은 'read-only 기본 + 로그인':
보기(GET)는 비밀번호 없이 자유롭고, 변경 작업(POST: 스캔 시작·설정·노드 등록 등)에는
로그인으로 발급받은 토큰이 필요하다.

비밀번호 저장:
  - 평문(기본): settings 에 그대로 저장(파일에서 바로 보임).
  - 암호화(옵션): PBKDF2-SHA256 해시로 저장(평문 미저장·복구 불가, 검증만 가능).
표준 라이브러리만 사용한다(무의존 원칙).
"""


import hashlib
import hmac
import os
import secrets
import threading
import time

PBKDF2_PREFIX = "pbkdf2_sha256$"
DEFAULT_ITERATIONS = 200_000
DEFAULT_TTL = 1800.0   # 로그인 세션 유효 시간(초) — 기본 30분


def hash_password(pw: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """비밀번호를 PBKDF2-SHA256 해시 문자열로 만든다(평문을 저장하지 않기 위함).

    형식: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``. 검증은 verify_password.
    """
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), salt, iterations)
    return "%s%d$%s$%s" % (PBKDF2_PREFIX, iterations, salt.hex(), dk.hex())


def is_hashed(stored: str) -> bool:
    """저장값이 (평문이 아니라) PBKDF2 해시 형식인가."""
    return str(stored or "").startswith(PBKDF2_PREFIX)


def verify_password(pw: str, stored: str) -> bool:
    """제출 비밀번호가 저장값(평문 또는 PBKDF2 해시)과 일치하는지 상수시간 비교."""
    stored = str(stored or "")
    pw = str(pw or "")
    if stored.startswith(PBKDF2_PREFIX):
        try:
            _, iters, salt_hex, hash_hex = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iters))
            return hmac.compare_digest(dk.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    if not stored:
        return False
    # 바이트로 비교 — str 끼리면 비 ASCII(한글 비밀번호)에서 compare_digest 가 TypeError(→ 500, 로그인 영구 불가)
    return secret_equal(pw, stored)


def secret_equal(a, b) -> bool:
    """비밀값 상수시간 비교(단일 소스). 한글 등 비 ASCII 도 안전하게 UTF-8 바이트로 비교한다."""
    return hmac.compare_digest(str(a if a is not None else "").encode("utf-8", "surrogatepass"),
                               str(b if b is not None else "").encode("utf-8", "surrogatepass"))


def store_password(pw: str, *, encrypt: bool) -> str:
    """저장용 비밀번호 값을 만든다 — encrypt 면 해시, 아니면 평문 그대로."""
    pw = str(pw or "")
    if not pw:
        return ""
    return hash_password(pw) if encrypt else pw


class AuthGuard:
    """비밀번호 게이트 + 로그인 세션 토큰.

    password_getter() 는 현재 저장된 비밀번호(평문 또는 해시)를 돌려준다(지연 평가).
    비밀번호가 비어 있으면 인증이 필요 없는(전체 공개) 상태로 본다.
    """

    def __init__(self, password_getter, ttl=DEFAULT_TTL,
                 max_fails: int = 5, lockout: float = 30.0) -> None:
        self._get_pw = password_getter
        self._ttl_src = ttl                  # 값 또는 콜러블(설정에서 동적으로 읽기 → 사용자가 세션 시간 지정)
        self._tokens: dict = {}              # token -> 발급 시각
        self._lock = threading.Lock()
        self._max_fails = int(max_fails)     # 연속 로그인 실패 허용 횟수
        self._lockout = float(lockout)       # 초과 시 잠금 시간(초)
        # 잠금은 클라이언트(IP)별 — 전역 잠금이면 공격자 한 명이 모든 운영자를 잠가 버린다(DoS).
        # 진행 중(in-flight) 시도도 실패 횟수에 미리 예약해, PBKDF2 검증이 느린 동안 병렬 요청으로
        # 잠금 한도를 우회하지 못하게 한다(2026-11 리뷰).
        self._fails: dict = {}               # client -> 연속 실패 수
        self._locked: dict = {}              # client -> 잠금 해제 시각
        self._inflight: dict = {}            # client -> 검증 중인 시도 수

    def _ttl_now(self) -> float:
        """현재 세션 유효 시간(초). 콜러블이면 호출해 동적으로 읽는다(최소값 가드는 설정에서)."""
        try:
            t = self._ttl_src() if callable(self._ttl_src) else self._ttl_src
            return float(t)
        except Exception:
            return DEFAULT_TTL

    @property
    def ttl(self) -> int:
        return int(self._ttl_now())

    def required(self) -> bool:
        """변경 작업에 로그인이 필요한가(= 비밀번호가 설정됨)."""
        return bool(str(self._get_pw() or ""))

    _MAX_CLIENTS = 4096                     # 추적할 클라이언트 상한(메모리 보호)

    def _prune_clients_locked(self, now: float) -> None:
        if len(self._fails) + len(self._locked) <= self._MAX_CLIENTS:
            return
        self._locked = {k: v for k, v in self._locked.items() if v > now}
        keep = set(self._locked) | {k for k, v in self._inflight.items() if v}
        self._fails = {k: v for k, v in self._fails.items() if k in keep}

    def login(self, password: str, client=None) -> dict:
        """비밀번호를 확인하고 맞으면 세션 토큰을 발급한다.

        무차별 대입을 막기 위해 같은 클라이언트(client — 보통 접속 IP)의 연속 실패가
        max_fails 회에 이르면 lockout 초간 그 클라이언트만 잠근다. 검증 중인 시도도 한도에 포함한다.
        """
        stored = str(self._get_pw() or "")
        if not stored:
            return {"ok": True, "token": "", "op_required": False}
        key = str(client or "")
        now = time.time()
        with self._lock:
            until = self._locked.get(key, 0.0)
            if now < until:
                wait = int(until - now) + 1
                return {"ok": False, "locked_out": True,
                        "reason": "로그인 시도가 많아 잠시 잠겼습니다(%d초 후 재시도)" % wait}
            self._locked.pop(key, None)
            if self._fails.get(key, 0) + self._inflight.get(key, 0) >= self._max_fails:
                return {"ok": False, "locked_out": True,
                        "reason": "동시 로그인 시도가 너무 많습니다 — 잠시 후 다시 시도하세요"}
            self._inflight[key] = self._inflight.get(key, 0) + 1
        try:
            good = verify_password(password, stored)
        finally:
            with self._lock:
                n = self._inflight.get(key, 1) - 1
                if n > 0:
                    self._inflight[key] = n
                else:
                    self._inflight.pop(key, None)
        if not good:
            with self._lock:
                n = self._fails.get(key, 0) + 1
                if n >= self._max_fails:
                    self._locked[key] = now + self._lockout
                    self._fails.pop(key, None)
                    self._prune_clients_locked(now)
                    return {"ok": False, "locked_out": True,
                            "reason": "로그인 실패가 많아 %d초간 잠급니다" % int(self._lockout)}
                self._fails[key] = n
                self._prune_clients_locked(now)
            return {"ok": False, "reason": "비밀번호가 올바르지 않습니다."}
        token = secrets.token_hex(16)
        with self._lock:
            self._fails.pop(key, None)
            ttl = self._ttl_now()
            self._tokens = {t: ts for t, ts in self._tokens.items()
                            if now - ts < ttl}
            self._tokens[token] = now
        return {"ok": True, "token": token, "op_required": True, "ttl": int(ttl)}

    def issue_token(self) -> str:
        """검증을 이미 마친 호출자(비밀번호를 바꾼 본인 등)에게 새 세션 토큰을 발급한다."""
        if not self.required():
            return ""
        token = secrets.token_hex(16)
        now = time.time()
        with self._lock:
            self._tokens[token] = now
        return token

    def token_valid(self, token) -> bool:
        """세션 토큰이 유효한가(만료 시 폐기)."""
        token = str(token or "")
        if not token:
            return False
        with self._lock:
            ts = self._tokens.get(token)
            if ts is None:
                return False
            if time.time() - ts >= self._ttl_now():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke_all(self) -> None:
        """모든 로그인 세션 폐기(비밀번호가 서버 명령 등으로 바뀌었을 때)."""
        with self._lock:
            self._tokens = {}

    def logout(self, token) -> None:
        token = str(token or "")
        with self._lock:
            self._tokens.pop(token, None)


class FailLimiter:
    """클라이언트(보통 IP)별 연속 실패 잠금 — 비밀번호가 아닌 공유 비밀(enroll_token 등)의 무차별 대입 방지.

    로그인(OpAuth.login)과 같은 규칙: max_fails 회 연속 실패하면 lockout 초간 그 클라이언트만 잠근다(27회차 보안 점검:
    enroll_token 은 횟수 제한 없이 추측할 수 있었다)."""

    _MAX_CLIENTS = 4096

    def __init__(self, max_fails: int = 5, lockout: float = 60.0) -> None:
        self._max = int(max_fails)
        self._lockout = float(lockout)
        self._fails: dict = {}
        self._locked: dict = {}
        self._lock = threading.Lock()

    def blocked(self, client) -> int:
        """잠겨 있으면 남은 초(>0), 아니면 0."""
        key = str(client or "")
        now = time.time()
        with self._lock:
            until = self._locked.get(key, 0.0)
            if now < until:
                return int(until - now) + 1
            self._locked.pop(key, None)
            return 0

    def failed(self, client) -> None:
        key = str(client or "")
        now = time.time()
        with self._lock:
            n = self._fails.get(key, 0) + 1
            if n >= self._max:
                self._locked[key] = now + self._lockout
                self._fails.pop(key, None)
            else:
                self._fails[key] = n
            if len(self._fails) + len(self._locked) > self._MAX_CLIENTS:
                self._locked = {k: v for k, v in self._locked.items() if v > now}
                self._fails = {}

    def succeeded(self, client) -> None:
        with self._lock:
            self._fails.pop(str(client or ""), None)

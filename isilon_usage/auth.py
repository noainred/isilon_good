"""사용자 인증(작업 보호) — 평문 또는 암호화(PBKDF2 해시) 비밀번호 + 세션 토큰.

스캐너(server)와 포탈(portal)이 공용으로 쓴다. 모델은 'read-only 기본 + 로그인':
보기(GET)는 비밀번호 없이 자유롭고, 변경 작업(POST: 스캔 시작·설정·노드 등록 등)에는
로그인으로 발급받은 토큰이 필요하다.

비밀번호 저장:
  - 평문(기본): settings 에 그대로 저장(파일에서 바로 보임).
  - 암호화(옵션): PBKDF2-SHA256 해시로 저장(평문 미저장·복구 불가, 검증만 가능).
표준 라이브러리만 사용한다(무의존 원칙).
"""

from __future__ import annotations

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
    return hmac.compare_digest(pw, stored)


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
        self._fails = 0
        self._locked_until = 0.0

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

    def login(self, password: str) -> dict:
        """비밀번호를 확인하고 맞으면 세션 토큰을 발급한다.

        무차별 대입을 막기 위해 연속 실패가 max_fails 회를 넘으면 lockout 초간 잠근다.
        """
        stored = str(self._get_pw() or "")
        if not stored:
            return {"ok": True, "token": "", "op_required": False}
        now = time.time()
        with self._lock:
            if now < self._locked_until:
                wait = int(self._locked_until - now) + 1
                return {"ok": False, "locked_out": True,
                        "reason": "로그인 시도가 많아 잠시 잠겼습니다(%d초 후 재시도)" % wait}
        if not verify_password(password, stored):
            with self._lock:
                self._fails += 1
                if self._fails >= self._max_fails:
                    self._locked_until = now + self._lockout
                    self._fails = 0
                    return {"ok": False, "locked_out": True,
                            "reason": "로그인 실패가 많아 %d초간 잠급니다" % int(self._lockout)}
            return {"ok": False, "reason": "비밀번호가 올바르지 않습니다."}
        token = secrets.token_hex(16)
        with self._lock:
            self._fails = 0
            ttl = self._ttl_now()
            self._tokens = {t: ts for t, ts in self._tokens.items()
                            if now - ts < ttl}
            self._tokens[token] = now
        return {"ok": True, "token": token, "op_required": True, "ttl": int(ttl)}

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

    def logout(self, token) -> None:
        token = str(token or "")
        with self._lock:
            self._tokens.pop(token, None)

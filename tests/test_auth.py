"""인증(auth) — 비밀번호 평문/해시 검증 + AuthGuard 세션 토큰."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from isilon_usage import auth  # noqa: E402


def main() -> int:
    # 평문 검증
    assert auth.verify_password("secret", "secret")
    assert not auth.verify_password("wrong", "secret")
    assert not auth.verify_password("", "")              # 빈 저장값은 항상 실패

    # 해시: 같은 비밀번호라도 salt 로 매번 다른 문자열, 검증은 성공
    h1 = auth.hash_password("secret")
    h2 = auth.hash_password("secret")
    assert h1 != h2 and auth.is_hashed(h1)
    assert auth.verify_password("secret", h1)
    assert not auth.verify_password("nope", h1)
    assert not auth.is_hashed("plain")
    assert not auth.verify_password("x", "pbkdf2_sha256$bad$format")   # 깨진 해시 → False

    # store_password: encrypt 여부
    assert auth.store_password("pw", encrypt=False) == "pw"
    assert auth.is_hashed(auth.store_password("pw", encrypt=True))
    assert auth.store_password("", encrypt=True) == ""    # 빈 비번은 빈 문자열

    # AuthGuard: 비번 미설정이면 인증 불필요
    box = {"pw": ""}
    g = auth.AuthGuard(lambda: box["pw"], ttl=100)
    assert not g.required()
    r = g.login("anything")
    assert r["ok"] and not r["op_required"]

    # 비번 설정(암호화) 후: 틀리면 실패, 맞으면 세션 토큰
    box["pw"] = auth.store_password("admin", encrypt=True)
    assert g.required()
    assert not g.login("bad")["ok"]
    res = g.login("admin")
    assert res["ok"] and res["token"] and res["op_required"]
    tok = res["token"]
    assert g.token_valid(tok)
    assert not g.token_valid("deadbeef")
    g.logout(tok)
    assert not g.token_valid(tok)                         # 로그아웃 후 무효

    # 만료(ttl=0 → 즉시 만료)
    g2 = auth.AuthGuard(lambda: "x", ttl=0)
    t = g2.login("x")["token"]
    assert not g2.token_valid(t)

    # 콜러블 ttl — 설정에서 동적으로 읽기(사용자가 세션 유지 시간 지정)
    ttlbox = {"sec": 100}
    g2b = auth.AuthGuard(lambda: "x", ttl=lambda: ttlbox["sec"])
    assert g2b.ttl == 100
    tb = g2b.login("x")["token"]
    assert g2b.token_valid(tb)            # 100초 유지면 유효
    ttlbox["sec"] = 0                     # 설정을 0으로 바꾸면 즉시 만료(다음 검증부터)
    assert not g2b.token_valid(tb)
    ttlbox["sec"] = 600
    assert g2b.ttl == 600                 # 콜러블이 매번 최신값을 반영

    # 무차별 대입 잠금(max_fails 초과 시 lockout)
    box["pw"] = auth.store_password("admin", encrypt=False)
    g3 = auth.AuthGuard(lambda: box["pw"], ttl=100, max_fails=3, lockout=100)
    assert not g3.login("x")["ok"]                   # 1
    assert not g3.login("x")["ok"]                   # 2
    assert g3.login("x").get("locked_out")           # 3 → 잠금
    r4 = g3.login("admin")
    assert not r4["ok"] and r4.get("locked_out")     # 잠긴 동안엔 올바른 비번도 막힘

    # 감사 로그 기록/조회(최신순)
    import tempfile
    import shutil as _sh
    from isilon_usage import audit as _audit
    d = tempfile.mkdtemp(prefix="isilon_audit_")
    try:
        _audit.record(d, action="login", ok=True, ip="1.2.3.4")
        _audit.record(d, action="/api/scan/start", ok=False, ip="1.2.3.4", detail="locked")
        ev = _audit.tail(d, 10)
        assert len(ev) == 2 and ev[0]["action"] == "/api/scan/start"   # 최신순
        assert ev[0]["ok"] is False and ev[1]["ok"] is True
    finally:
        _sh.rmtree(d, ignore_errors=True)
    print("[auth] OK  무차별 대입 잠금 + 감사 로그 기록/조회")

    print("[auth] OK  평문/해시 검증 + 세션 토큰 발급/검증/로그아웃/만료")
    print("모든 테스트 통과 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""스캔 완료/오류 시 웹훅 알림(베스트 에포트).

설정의 notify_webhook 에 URL 을 넣으면 스캔이 끝날 때 그 URL 로 JSON 을 POST 한다.
Slack 수신 웹훅 등 일반 웹훅과 호환되도록 'text' 필드도 함께 보낸다.
표준 라이브러리(urllib)만 사용하며, 실패해도 스캔에 영향을 주지 않는다.
"""


import json
import re
import smtplib
import urllib.request
from email.message import EmailMessage

from . import netutil


def send(webhook_url: str, payload: dict, *, timeout: float = 5.0) -> bool:
    if not webhook_url:
        return False
    body = dict(payload)
    # Slack 호환: 사람이 읽을 요약을 text 로
    body.setdefault(
        "text",
        f"[isilon_usage] 스캔 {payload.get('status','?')}: "
        f"{payload.get('root_path','')} "
        f"({payload.get('scanned_human','')}, dirs={payload.get('total_dirs','?')})",
    )
    try:
        # 비 UTF-8 경로(서로게이트)면 UTF-8 인코딩이 실패한다 — 베스트 에포트 약속대로 예외를 밖으로 내지 않는다
        try:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            data = json.dumps(body, ensure_ascii=True).encode("ascii")
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with netutil.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def parse_recipients(raw: str) -> list:
    """받는 사람 목록 — 쉼표·세미콜론·줄바꿈·공백으로 여럿, '이름 <주소>' 형식도 주소만 뽑는다."""
    out = []
    for part in re.split(r"[,;\n]+", str(raw or "")):
        part = part.strip()
        if not part:
            continue
        # '<주소>' 와 같은 조각에 맨 주소가 섞여 있어도(예: 'Kim <a@x> b@y') 둘 다(30회차 점검)
        cands = re.findall(r"<([^<>\s]+@[^<>\s]+)>", part)
        if not cands:
            cands = part.split()
        else:   # '<주소>' 밖은 표시 이름 — 따옴표 이름("a@b.com")·괄호 메모는 버리고 맨 주소만(31회차 점검)
            rest = re.sub(r'"[^"]*"|\([^)]*\)|<[^<>]*>', " ", part)
            cands += rest.split()
        cands = [c.strip("\"'(),;") for c in cands]
        for a in cands:     # 같은 주소는 한 번만
            if "@" in a and not re.search(r"[\s<>\"'()]", a) and a not in out:
                out.append(a)
    return out


def send_email(settings: dict, subject: str, body: str, *, timeout: float = 20.0) -> bool:
    """설정의 SMTP 로 완료/오류 메일을 보낸다(베스트 에포트, 실패해도 무시).

    notify_email(받는 사람)과 smtp_host 가 모두 있어야 보낸다.
    받는 사람은 쉼표/세미콜론/공백으로 여러 명을 적을 수 있다(다중 수신자).
    포트 465 는 SSL, 그 외(587/25)는 STARTTLS(smtp_tls).
    """
    raw = (settings.get("notify_email") or "").strip()
    # '이름 <주소>' 형식도 받는다 — 예전엔 공백으로 쪼개 '김철수'·'<b@x.com>' 가 주소로 들어가, 서버가 거부하면
    # 다른 받는 사람에게도 한 통도 가지 않았다(29회차 점검). 주소 모양(@)이 아닌 조각은 버린다.
    recipients = parse_recipients(raw)
    host = (settings.get("smtp_host") or "").strip()
    if not recipients or not host:
        return False
    sender = (settings.get("smtp_from") or settings.get("smtp_user")
              or "isilon-usage@localhost")
    # 메시지 만들기도 실패할 수 있다(제목에 줄바꿈이 든 경로, 비 UTF-8 이름의 대리 문자) — 예전엔 여기서 난 예외가
    # 호출부의 뒤 작업(멈춤 감시 등록·보관 정리)까지 건너뛰게 했다. 제목의 줄바꿈은 공백으로, 대리 문자는 치환.
    def _clean(t):
        return str(t or "").encode("utf-8", "replace").decode("utf-8")
    try:
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = re.sub(r"[\r\n]+", " ", _clean(subject))
        msg.set_content(_clean(body))
    except Exception:  # noqa: BLE001 — 알림은 best-effort
        return False
    try:
        port = int(settings.get("smtp_port") or 587)
    except (TypeError, ValueError):
        port = 587
    user = settings.get("smtp_user") or ""
    pw = settings.get("smtp_password") or ""
    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            srv = smtplib.SMTP(host, port, timeout=timeout)
        try:
            if port != 465 and settings.get("smtp_tls", True):
                srv.starttls()
            if user:
                try:
                    srv.login(user, pw)
                except UnicodeEncodeError:
                    # smtplib 은 계정·비밀번호를 ASCII 로만 보내 한글 등이 들어가면 늘 실패했다(21회차 점검) — UTF-8 로 AUTH PLAIN
                    import base64
                    tok = base64.b64encode(("\0%s\0%s" % (user, pw)).encode("utf-8")).decode("ascii")
                    code, resp = srv.docmd("AUTH", "PLAIN " + tok)
                    if code not in (235, 503):
                        raise smtplib.SMTPAuthenticationError(code, resp)
            srv.send_message(msg)
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        return True
    except Exception as exc:  # noqa: BLE001
        # 실패 이유를 남긴다 — 예전엔 STARTTLS 없는 중계 서버·인증 실패가 아무 기록 없이 사라졌다(21회차 점검)
        try:
            import sys
            sys.stderr.write("[notify] 메일 보내기 실패(%s:%s): %s: %s\n" % (host, port, type(exc).__name__, exc))
        except Exception:  # noqa: BLE001
            pass
        return False

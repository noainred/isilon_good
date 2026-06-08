"""스캔 완료/오류 시 웹훅 알림(베스트 에포트).

설정의 notify_webhook 에 URL 을 넣으면 스캔이 끝날 때 그 URL 로 JSON 을 POST 한다.
Slack 수신 웹훅 등 일반 웹훅과 호환되도록 'text' 필드도 함께 보낸다.
표준 라이브러리(urllib)만 사용하며, 실패해도 스캔에 영향을 주지 않는다.
"""


import json
import smtplib
import urllib.request
from email.message import EmailMessage


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
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def send_email(settings: dict, subject: str, body: str, *, timeout: float = 20.0) -> bool:
    """설정의 SMTP 로 완료/오류 메일을 보낸다(베스트 에포트, 실패해도 무시).

    notify_email(받는 사람)과 smtp_host 가 모두 있어야 보낸다.
    포트 465 는 SSL, 그 외(587/25)는 STARTTLS(smtp_tls).
    """
    to = (settings.get("notify_email") or "").strip()
    host = (settings.get("smtp_host") or "").strip()
    if not to or not host:
        return False
    sender = (settings.get("smtp_from") or settings.get("smtp_user")
              or "isilon-usage@localhost")
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
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
                srv.login(user, pw)
            srv.send_message(msg)
        finally:
            try:
                srv.quit()
            except Exception:
                pass
        return True
    except Exception:
        return False

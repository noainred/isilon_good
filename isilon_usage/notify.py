"""스캔 완료/오류 시 웹훅 알림(베스트 에포트).

설정의 notify_webhook 에 URL 을 넣으면 스캔이 끝날 때 그 URL 로 JSON 을 POST 한다.
Slack 수신 웹훅 등 일반 웹훅과 호환되도록 'text' 필드도 함께 보낸다.
표준 라이브러리(urllib)만 사용하며, 실패해도 스캔에 영향을 주지 않는다.
"""

from __future__ import annotations

import json
import urllib.request


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

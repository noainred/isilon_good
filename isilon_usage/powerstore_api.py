"""Dell PowerStore 상태 조회 — PowerStore REST API.

표준 라이브러리(urllib + ssl)만으로 PowerStore Manager REST API 를 호출해
클러스터 이름/상태, 어플라이언스 모델, 노드 수, 용량(space metrics), 활성
알림(건강)을 가져온다. 읽기 전용 계정이면 충분하다.

REST API 는 `https://<powerstore>/api/rest` 에 있다. GET 은 Basic 인증으로
바로 호출되고, 응답 헤더의 DELL-EMC-TOKEN 을 받아 두면 POST(metrics/generate)
에 함께 보낸다. GET 은 `?select=` 로 필요한 필드를 지정해야 값이 채워진다.

OneFS 모듈(isilon_api)과 동일한 상태 dict 모양을 돌려줘 UI 가 한 가지로 처리한다.
"""

from typing import Optional

import base64
import json
import ssl
import urllib.error
import urllib.request


def _ssl_context(verify: bool):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class PowerStoreClient:
    """PowerStore REST API 최소 클라이언트(베이직 인증 + DELL-EMC-TOKEN)."""

    def __init__(self, base_url: str, user: str, password: str, *,
                 verify_ssl: bool = False, timeout: float = 10.0) -> None:
        b = (base_url or "").rstrip("/")
        if b.endswith("/api/rest"):
            b = b[:-len("/api/rest")]
        self.api = b + "/api/rest"
        self.user = user or ""
        self.password = password or ""
        self.verify_ssl = bool(verify_ssl)
        self.timeout = float(timeout)
        self._token = None  # type: Optional[str]

    def _auth(self) -> str:
        return "Basic " + base64.b64encode(
            ("%s:%s" % (self.user, self.password)).encode("utf-8")).decode("ascii")

    def _ctx(self):
        return _ssl_context(self.verify_ssl) if self.api.lower().startswith("https") else None

    def _get(self, path: str):
        req = urllib.request.Request(self.api + path)
        req.add_header("Authorization", self._auth())
        req.add_header("Accept", "application/json")
        resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx())
        try:
            tok = resp.headers.get("DELL-EMC-TOKEN")
            if tok:
                self._token = tok
            return json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()

    def _post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.api + path, data=data, method="POST")
        req.add_header("Authorization", self._auth())
        req.add_header("Content-Type", "application/json")
        if self._token:
            req.add_header("DELL-EMC-TOKEN", self._token)
        resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx())
        try:
            return json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()

    def status(self) -> dict:
        out = {
            "ok": False, "type": "powerstore", "label": "Dell PowerStore",
            "base": self.api, "name": None, "version": None,
            "capacity": {}, "nodes": {}, "events_unresolved": None,
            "health": "unknown", "error": "",
        }
        state = None
        cid = None
        # 1) 클러스터(이름/상태) — 여기까지 되면 ok + 토큰 확보
        try:
            cl = self._get("/cluster?select=id,name,state,system_time")
            if not cl:
                out["error"] = "cluster 없음"
                return out
            c = cl[0]
            out["name"] = c.get("name")
            state = c.get("state")
            cid = c.get("id")
            out["ok"] = True
        except urllib.error.HTTPError as e:
            out["error"] = ("인증 실패(계정 확인)" if e.code in (401, 403)
                            else "HTTP %d" % e.code)
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
            return out
        # 2) 어플라이언스 모델(버전 칸에 표기)
        try:
            ap = self._get("/appliance?select=id,name,model")
            if ap:
                out["version"] = ap[0].get("model")
        except Exception:  # noqa: BLE001
            pass
        # 3) 노드 수
        try:
            nd = self._get("/node?select=id")
            if isinstance(nd, list):
                out["nodes"] = {"total": len(nd), "online": len(nd)}
        except Exception:  # noqa: BLE001
            pass
        # 4) 용량(space metrics) — POST metrics/generate
        try:
            m = self._post("/metrics/generate",
                           {"entity": "space_metrics_by_cluster", "entity_id": cid})
            if isinstance(m, list) and m:
                last = m[-1]
                total = last.get("physical_total")
                used = last.get("physical_used")
                if total:
                    avail = (total - used) if (total is not None and used is not None) else None
                    out["capacity"] = {
                        "total": total, "used": used, "avail": avail,
                        "used_pct": round(used / total * 100.0, 1) if (used and total) else None,
                    }
        except Exception:  # noqa: BLE001
            pass
        # 5) 활성 알림(건강) — Critical/Major 개수
        try:
            al = self._get("/alert?select=id,severity,is_active&is_active=eq.true")
            if isinstance(al, list):
                out["events_unresolved"] = sum(
                    1 for a in al if str(a.get("severity", "")).lower() in ("critical", "major"))
        except Exception:  # noqa: BLE001
            pass
        bad_state = state not in (None, "Configured", "Operational", "Unconfigured")
        out["health"] = ("ok" if (out["events_unresolved"] in (0, None) and not bad_state)
                         else "attention")
        return out


def cluster_status(url: str, user: str, password: str, *,
                   verify_ssl: bool = False, timeout: float = 10.0) -> dict:
    """편의 함수: 설정값으로 한 번 조회. 미설정이면 configured=False."""
    if not (url or "").strip():
        return {"ok": False, "configured": False, "type": "powerstore", "error": "미설정"}
    res = PowerStoreClient(url, user, password, verify_ssl=verify_ssl, timeout=timeout).status()
    res["configured"] = True
    return res

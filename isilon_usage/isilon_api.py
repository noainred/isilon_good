"""아이실론(Dell PowerScale/Isilon, OneFS) 상태 조회 — Platform API(PAPI).

표준 라이브러리(urllib + ssl)만으로 OneFS Platform API 를 호출해 클러스터의
이름/버전/용량/노드/미해결 이벤트를 가져온다. 읽기 전용 계정이면 충분하다.

PAPI 는 보통 `https://<클러스터>:8080` 에 있고 자체 서명 인증서를 쓰므로,
`verify_ssl=False`(기본)면 인증서 검증을 생략한다. 각 호출은 베스트 에포트라
일부가 실패해도 가능한 항목만 채워서 돌려준다.
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


_SEV_RANK = {"emergency": 0, "critical": 1, "warning": 2, "information": 3, "info": 3}


def _summarize_events(ev: dict):
    """eventgroup-occurrences 응답 → (total, by_severity, alarms[상위]) 베스트 에포트.

    OneFS 버전마다 필드명이 조금씩 달라, 여러 후보를 방어적으로 읽는다.
    """
    occ = (ev.get("eventgroup-occurrences") or ev.get("occurrences")
           or ev.get("entries") or [])
    by_sev: dict = {}
    alarms = []
    for o in occ:
        if not isinstance(o, dict):
            continue
        sev = str(o.get("severity") or "").lower() or "information"
        by_sev[sev] = by_sev.get(sev, 0) + 1
        msg = ""
        causes = o.get("causes")
        if isinstance(causes, list) and causes and isinstance(causes[0], dict):
            msg = str(causes[0].get("cause") or "")
        if not msg:
            msg = str(o.get("message") or o.get("value")
                      or ("eventgroup %s" % o.get("eventgroup_id", "")))
        alarms.append({
            "severity": sev,
            "message": msg[:200],
            "time": o.get("time_noticed") or o.get("last_event") or o.get("time"),
            "count": o.get("event_count") or o.get("count") or 1,
        })
    alarms.sort(key=lambda a: (_SEV_RANK.get(a["severity"], 9), -(a["time"] or 0)))
    total = ev.get("total")
    if total is None:
        total = len(occ)
    return total, by_sev, alarms[:20]


def _health_from(by_sev: dict, total) -> str:
    """심각도/미해결 개수로 health 판정 — critical(심각/긴급) > attention(경고/미해결) > ok."""
    crit = (by_sev or {}).get("emergency", 0) + (by_sev or {}).get("critical", 0)
    if crit:
        return "critical"
    if (by_sev or {}).get("warning", 0) or (total not in (0, None)):
        return "attention"
    return "ok"


class IsilonClient:
    """OneFS Platform API 최소 클라이언트(베이직 인증)."""

    def __init__(self, base_url: str, user: str, password: str, *,
                 verify_ssl: bool = False, timeout: float = 10.0) -> None:
        self.base = (base_url or "").rstrip("/")
        self.user = user or ""
        self.password = password or ""
        self.verify_ssl = bool(verify_ssl)
        self.timeout = float(timeout)

    def _get(self, path: str):
        url = self.base + path
        req = urllib.request.Request(url)
        if self.user:
            tok = base64.b64encode(
                ("%s:%s" % (self.user, self.password)).encode("utf-8")).decode("ascii")
            req.add_header("Authorization", "Basic " + tok)
        ctx = _ssl_context(self.verify_ssl) if url.lower().startswith("https") else None
        resp = urllib.request.urlopen(req, timeout=self.timeout, context=ctx)
        try:
            return json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()

    def status(self) -> dict:
        """클러스터 상태 요약(베스트 에포트)."""
        out = {
            "ok": False, "type": "isilon", "label": "Isilon (OneFS)",
            "base": self.base, "name": None, "version": None,
            "capacity": {}, "nodes": {}, "events_unresolved": None,
            "events_by_severity": {}, "alarms": [],
            "health": "unknown", "error": "",
        }
        # 1) 클러스터 설정(이름/버전) — 여기까지 되면 ok
        try:
            cfg = self._get("/platform/1/cluster/config")
            out["name"] = cfg.get("name")
            ver = cfg.get("onefs_version") or {}
            out["version"] = ver.get("release") or ver.get("version")
            out["ok"] = True
        except urllib.error.HTTPError as e:
            out["error"] = ("인증 실패(계정 확인)" if e.code in (401, 403)
                            else "HTTP %d" % e.code)
            return out
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
            return out
        # 2) 용량 — statistics current (ifs.bytes.*)
        try:
            st = self._get("/platform/1/statistics/current"
                           "?key=ifs.bytes.total&key=ifs.bytes.used&key=ifs.bytes.avail")
            vals = {}
            for s in st.get("stats", []):
                vals[s.get("key")] = s.get("value")
            total = vals.get("ifs.bytes.total")
            used = vals.get("ifs.bytes.used")
            avail = vals.get("ifs.bytes.avail")
            if total:
                out["capacity"] = {
                    "total": total, "used": used, "avail": avail,
                    "used_pct": round(used / total * 100.0, 1) if (used and total) else None,
                }
        except Exception:  # noqa: BLE001
            pass
        # 3) 노드 수/상태
        try:
            nd = self._get("/platform/1/cluster/nodes")
            nodes = nd.get("nodes", []) or []
            total = nd.get("total") or len(nodes)

            def _up(n):
                s = str(n.get("status", "") or "").lower()
                return s in ("", "up", "ok", "online", "available", "attention")

            online = sum(1 for n in nodes if _up(n)) if nodes else total
            out["nodes"] = {"total": int(total or 0), "online": int(online or 0)}
        except Exception:  # noqa: BLE001
            pass
        # 4) 미해결 이벤트(알람) — 개수 + 심각도별 + 상위 목록
        try:
            ev = self._get("/platform/3/event/eventgroup-occurrences"
                           "?resolved=false&limit=100")
            total, by_sev, alarms = _summarize_events(ev)
            out["events_unresolved"] = total
            out["events_by_severity"] = by_sev
            out["alarms"] = alarms
        except Exception:  # noqa: BLE001
            pass
        out["health"] = _health_from(out["events_by_severity"], out["events_unresolved"])
        return out


def cluster_status(url: str, user: str, password: str, *,
                   verify_ssl: bool = False, timeout: float = 10.0) -> dict:
    """편의 함수: 설정값으로 한 번 조회. 미설정이면 configured=False."""
    if not (url or "").strip():
        return {"ok": False, "configured": False, "type": "isilon", "error": "미설정"}
    res = IsilonClient(url, user, password, verify_ssl=verify_ssl, timeout=timeout).status()
    res["configured"] = True
    return res

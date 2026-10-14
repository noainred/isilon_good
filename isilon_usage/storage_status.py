"""여러 스토리지 어레이(Dell/EMC 계열) 상태 조회 디스패처.

유형(type)별 REST API 클라이언트를 모아, 설정의 스토리지 어레이 목록을 한
가지 상태 dict 모양으로 조회한다. OneFS/PowerStore 는 전용 모듈을 재사용하고,
Unity/PowerMax/VMAX/XtremIO/VPLEX 는 여기에 최소 클라이언트를 둔다.

각 클라이언트는 표준 라이브러리(urllib+ssl)만 쓰고, 일부 호출이 실패해도
가능한 항목만 채워 돌려준다(베스트 에포트). 실제 응답 필드는 장비/펌웨어
버전에 따라 다를 수 있어, 안 맞으면 'error'/부분값으로 degrade 한다.

상태 dict 모양(공통):
  {ok, configured, type, label, name, version, capacity:{total,used,avail,used_pct},
   nodes:{total,online}, events_unresolved, health(ok/attention/unknown), error}
"""

import base64
import json
import ssl
import urllib.error
import urllib.request

from . import isilon_api
from . import powerstore_api


def _ctx(verify):
    c = ssl.create_default_context()
    if not verify:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def _req(url, user, password, *, verify_ssl=False, headers=None, data=None,
         method=None, timeout=10.0):
    req = urllib.request.Request(url, data=data, method=method)
    if user:
        req.add_header("Authorization", "Basic " + base64.b64encode(
            ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii"))
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    ctx = _ctx(verify_ssl) if url.lower().startswith("https") else None
    resp = urllib.request.urlopen(req, timeout=timeout, context=ctx)
    try:
        return json.loads(resp.read().decode("utf-8"))
    finally:
        resp.close()


def _blank(t, label):
    return {"ok": False, "type": t, "label": label, "name": None, "version": None,
            "capacity": {}, "nodes": {}, "events_unresolved": None,
            "health": "unknown", "error": ""}


def _cap(total, used, avail=None):
    if not total:
        return {}
    if avail is None and used is not None:
        avail = total - used
    return {"total": total, "used": used, "avail": avail,
            "used_pct": round(used / total * 100.0, 1) if (used and total) else None}


def _httperr(e):
    return "인증 실패(계정 확인)" if e.code in (401, 403) else "HTTP %d" % e.code


# ------------------------------------------------------------------- Dell Unity
def unity_status(url, user, password, *, verify_ssl=False, timeout=10.0):
    out = _blank("unity", "Dell Unity")
    base = (url or "").rstrip("/")
    H = {"X-EMC-REST-CLIENT": "true"}

    def g(p):
        return _req(base + p, user, password, verify_ssl=verify_ssl, headers=H, timeout=timeout)

    try:
        sysr = g("/api/types/system/instances?fields=name,model,health")
        ent = sysr.get("entries") or []
        if not ent:
            out["error"] = "system 없음"
            return out
        c = ent[0].get("content", {})
        out["name"] = c.get("name")
        out["version"] = c.get("model")
        hv = (c.get("health") or {}).get("value")
        out["health"] = "ok" if hv in (5, 7) else ("attention" if hv is not None else "unknown")
        out["ok"] = True
    except urllib.error.HTTPError as e:
        out["error"] = _httperr(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    try:
        capr = g("/api/types/systemCapacity/instances?fields=sizeTotal,sizeUsed,sizeFree")
        ent = capr.get("entries") or []
        if ent:
            c = ent[0].get("content", {})
            out["capacity"] = _cap(c.get("sizeTotal"), c.get("sizeUsed"), c.get("sizeFree"))
    except Exception:  # noqa: BLE001
        pass
    try:
        sp = g("/api/types/storageProcessor/instances")
        ent = sp.get("entries") or []
        out["nodes"] = {"total": len(ent), "online": len(ent)}
    except Exception:  # noqa: BLE001
        pass
    return out


# ----------------------------------------------------------- Dell PowerMax/VMAX
def powermax_status(url, user, password, *, verify_ssl=False, timeout=10.0,
                    _type="powermax", _label="Dell PowerMax"):
    out = _blank(_type, _label)
    base = (url or "").rstrip("/")
    if not base.endswith("/univmax/restapi"):
        base = base + "/univmax/restapi"

    def g(p):
        return _req(base + p, user, password, verify_ssl=verify_ssl, timeout=timeout)

    try:
        sym = g("/sloprovisioning/symmetrix")
        ids = sym.get("symmetrixId") or []
        if not ids:
            out["error"] = "symmetrix 없음"
            return out
        sid = ids[0]
        out["name"] = sid
        out["ok"] = True
    except urllib.error.HTTPError as e:
        out["error"] = _httperr(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    try:
        det = g("/sloprovisioning/symmetrix/" + str(sid))
        out["version"] = det.get("model") or det.get("ucode") or det.get("microcode_date")
        cap = det.get("system_capacity") or {}
        tb = cap.get("usable_total_tb")
        utb = cap.get("usable_used_tb")
        if tb:
            total = int(float(tb) * (1024 ** 4))
            used = int(float(utb) * (1024 ** 4)) if utb is not None else None
            out["capacity"] = _cap(total, used)
    except Exception:  # noqa: BLE001
        pass
    try:
        h = g("/system/symmetrix/" + str(sid) + "/health")
        score = (h.get("health_score_metric") or {}).get("health_score")
        if score is not None:
            out["health"] = "ok" if float(score) >= 95 else "attention"
    except Exception:  # noqa: BLE001
        pass
    return out


def vmax_status(url, user, password, *, verify_ssl=False, timeout=10.0):
    return powermax_status(url, user, password, verify_ssl=verify_ssl, timeout=timeout,
                           _type="vmax", _label="Dell VMAX")


# --------------------------------------------------------------- Dell XtremIO
def xtremio_status(url, user, password, *, verify_ssl=False, timeout=10.0):
    out = _blank("xtremio", "Dell XtremIO")
    base = (url or "").rstrip("/")
    if "/api/json" not in base:
        base = base + "/api/json/v2"

    def g(p):
        return _req(base + p, user, password, verify_ssl=verify_ssl, timeout=timeout)

    try:
        cl = g("/types/clusters?full=1")
        arr = cl.get("clusters") or []
        if not arr:
            out["error"] = "cluster 없음"
            return out
        c = arr[0]
        out["name"] = c.get("name")
        out["version"] = c.get("sys-sw-version")
        out["health"] = "ok" if str(c.get("sys-health-state", "")).lower() in ("healthy", "") else "attention"
        # XtremIO 용량 단위는 KB
        total = c.get("ud-ssd-space")
        used = c.get("ud-ssd-space-in-use")
        if total:
            out["capacity"] = _cap(int(total) * 1024, int(used) * 1024 if used is not None else None)
        out["ok"] = True
    except urllib.error.HTTPError as e:
        out["error"] = _httperr(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    try:
        sc = g("/types/storage-controllers")
        n = sc.get("storage-controllers") or []
        out["nodes"] = {"total": len(n), "online": len(n)}
    except Exception:  # noqa: BLE001
        pass
    return out


# ----------------------------------------------------------------- Dell VPLEX
def vplex_status(url, user, password, *, verify_ssl=False, timeout=10.0):
    out = _blank("vplex", "Dell VPLEX")
    base = (url or "").rstrip("/")
    H = {"Username": user, "Password": password}  # VPLEX v1 커스텀 헤더도 허용

    def g(p):
        return _req(base + p, user, password, verify_ssl=verify_ssl, headers=H, timeout=timeout)

    try:
        # VPLEX v1 컨텍스트 API: /vplex/clusters
        r = g("/vplex/clusters")
        ctxs = (((r.get("response") or {}).get("context")) or [])
        if not ctxs:
            out["error"] = "clusters 없음"
            return out
        names = []
        for ctx in ctxs:
            nm = ctx.get("name")
            if not nm:
                for a in ctx.get("attributes", []):
                    if a.get("name") == "name":
                        nm = a.get("value")
            if nm:
                names.append(nm)
        out["name"] = ", ".join(names) if names else "VPLEX"
        out["nodes"] = {"total": len(ctxs), "online": len(ctxs)}
        out["health"] = "ok"
        out["ok"] = True
    except urllib.error.HTTPError as e:
        out["error"] = _httperr(e)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
        return out
    return out


# ------------------------------------------------------------------ 디스패처
# type -> (label, status 함수)
PROVIDERS = {
    "isilon": ("Isilon (OneFS)", isilon_api.cluster_status),
    "powerstore": ("Dell PowerStore", powerstore_api.cluster_status),
    "unity": ("Dell Unity", unity_status),
    "powermax": ("Dell PowerMax", powermax_status),
    "vmax": ("Dell VMAX", vmax_status),
    "xtremio": ("Dell XtremIO", xtremio_status),
    "vplex": ("Dell VPLEX", vplex_status),
}

ARRAY_TYPES = list(PROVIDERS.keys())


def query(array: dict) -> dict:
    """어레이 한 건({type,url,user,password,verify_ssl,name})의 상태를 조회한다."""
    t = (array or {}).get("type")
    prov = PROVIDERS.get(t)
    if prov is None:
        return {"ok": False, "configured": True, "type": t, "label": t or "?",
                "error": "지원하지 않는 유형"}
    label, fn = prov
    try:
        res = fn((array.get("url") or ""), (array.get("user") or ""),
                 (array.get("password") or ""),
                 verify_ssl=bool(array.get("verify_ssl", False)))
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "type": t, "label": label, "error": str(e)}
    res.setdefault("type", t)
    res.setdefault("label", label)
    res["configured"] = True
    if array.get("name"):
        res["label"] = label + " · " + str(array["name"])
    return res

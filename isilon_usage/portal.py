"""글로벌 통합 포탈(HQ) — 여러 데이터센터(엣지)를 한 화면에서 조망한다.

각 엣지는 `serve` 로 로컬 스토리지를 스캔하고 `/api/dbexport`(토큰 보호)로
완료된 per-run DB + 노드 요약(meta.json)을 tar.gz 로 제공한다. 포탈은:
  - 노드(엣지) 목록을 portal_nodes.json 에 보관(웹에서 추가/수정/삭제)
  - 주기적으로 각 노드를 폴링(연결·요약)하고 완료 DB 를 replicas/<id>/ 로 복제
  - 전체/지역/노드별 사용량 롤업을 글로벌 대시보드로 제공

표준 라이브러리만 사용한다(폐쇄망 호환). 엣지 한 곳만 호출하므로 운영이 단순하다.
"""

from typing import Optional

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import __version__
from . import db as dbmod
from . import settings as setmod
from .server import ThreadingHTTPServer  # 3.6 폴백 포함 재사용


HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL_HTML = os.path.join(HERE, "portal.html")

# 폴링(연결+요약)은 자주, 복제(완료 DB 내려받기)는 노드별 주기마다.
POLL_EVERY = 15.0       # 초
SCHED_TICK = 5.0        # 초
FAR_FUTURE = 9999999999  # since 가 미래면 meta.json 만 받아 가볍게 연결/요약 확인


# --------------------------------------------------------------- 노드 레지스트리
def nodes_path(data_dir: str) -> str:
    return os.path.join(data_dir, "portal_nodes.json")


def sanitize_node(raw: dict) -> Optional[dict]:
    """노드 1건을 보정한다(필수: id, url). 잘못되면 None."""
    if not isinstance(raw, dict):
        return None
    nid = str(raw.get("id") or "").strip()
    url = str(raw.get("url") or "").strip().rstrip("/")
    if not nid or not url:
        return None
    if not re.match(r"^https?://", url):
        url = "http://" + url
    try:
        interval = max(1, int(raw.get("interval_minutes", 30)))
    except (TypeError, ValueError):
        interval = 30
    mode = raw.get("mode", "both")
    if mode not in ("both", "poll", "replicate"):
        mode = "both"

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # 복제 주기: 예약 스캔과 같은 '시작 + 반복주기' 모델(분/시간/일/주/개월).
    # 구버전(interval_minutes 만 있던) 노드는 unit=minute, every=interval_minutes 로 호환.
    unit = raw.get("unit")
    if unit not in ("minute", "hour", "day", "week", "month"):
        unit = "minute"
    if raw.get("every") is not None:
        try:
            every = max(1, int(raw.get("every")))
        except (TypeError, ValueError):
            every = interval if unit == "minute" else 1
    else:
        every = interval if unit == "minute" else 1
    weekdays = sorted({d for d in (raw.get("weekdays") or [])
                       if isinstance(d, int) and 0 <= d <= 6})
    hh, mm = setmod.parse_at(raw.get("at"))
    anchor = _f(raw.get("anchor")) or time.time()

    def _i(key, lo, hi, dflt):
        try:
            return max(lo, min(hi, int(raw.get(key, dflt))))
        except (TypeError, ValueError):
            return dflt

    return {
        "id": nid,
        "region": str(raw.get("region") or "").strip(),
        "url": url,
        "token": str(raw.get("token") or ""),
        # 경로 별칭: 이 노드의 로컬 마운트 접두어 ↔ 공통 논리 접두어
        # 예) alias_local=/mnt/isilon/data, alias_logical=/data
        "alias_local": str(raw.get("alias_local") or "").strip().rstrip("/"),
        "alias_logical": str(raw.get("alias_logical") or "").strip().rstrip("/"),
        "interval_minutes": interval,
        "unit": unit,
        "every": every,
        "at": "%02d:%02d" % (hh, mm),
        "weekdays": weekdays,
        "start_month": _i("start_month", 1, 12, 1),
        "start_day": _i("start_day", 1, 31, 1),
        "anchor": anchor,
        "mode": mode,
        "enabled": bool(raw.get("enabled", True)),
        "last_poll": _f(raw.get("last_poll")),
        "last_sync": _f(raw.get("last_sync")),
        "last_status": str(raw.get("last_status") or "unknown"),
        "last_error": str(raw.get("last_error") or ""),
    }


def load_nodes(data_dir: str) -> list:
    try:
        with open(nodes_path(data_dir), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for it in (raw.get("nodes") if isinstance(raw, dict) else raw) or []:
        n = sanitize_node(it)
        if n:
            out.append(n)
    return out


def save_nodes(data_dir: str, nodes: list) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = nodes_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"nodes": nodes}, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _public_node(n: dict, cache: dict) -> dict:
    """화면용 노드(토큰 마스킹 + 실시간 캐시 상태 병합)."""
    c = cache.get(n["id"], {})
    out = dict(n)
    out["token"] = ""
    out["token_set"] = bool(n.get("token"))
    out["online"] = bool(c.get("online"))
    out["version"] = c.get("version")
    out["hostname"] = c.get("hostname")
    out["error"] = n.get("last_error") or c.get("error") or ""
    return out


# ------------------------------------------------------------------ 네트워크
def _http_get(url: str, token: Optional[str] = None, timeout: float = 8.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Auth-Token", token)
    return urllib.request.urlopen(req, timeout=timeout)


def _probe_meta(url: str, token: Optional[str], timeout: float = 8.0) -> dict:
    """엣지의 /api/dbexport?since=먼미래 로 meta.json 만 받아(가볍게) 요약을 돌려준다.

    연결 확인 + 토큰 검증 + 노드 요약(overall)을 한 번에 처리한다.
    """
    u = url.rstrip("/") + "/api/dbexport?since=%d" % FAR_FUTURE
    resp = _http_get(u, token, timeout=timeout)
    data = resp.read()
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    try:
        f = tf.extractfile("meta.json")
        return json.loads(f.read().decode("utf-8"))
    finally:
        tf.close()


# ------------------------------------------------------------------ 컨트롤러
class PortalController:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.replicas_dir = os.path.join(self.data_dir, "replicas")
        os.makedirs(self.replicas_dir, exist_ok=True)
        self.nodes = load_nodes(self.data_dir)
        self._cache = {}          # id -> {online, ts, version, hostname, overall, error}
        self._inflight = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.poll_every = POLL_EVERY
        self.tick = SCHED_TICK

    # --- 레지스트리 CRUD ---
    def _get(self, nid: str):
        for n in self.nodes:
            if n["id"] == nid:
                return n
        return None

    def _save(self) -> None:
        save_nodes(self.data_dir, self.nodes)

    def list_nodes(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes = [_public_node(n, cache) for n in self.nodes]
        return {"ok": True, "nodes": nodes}

    def upsert_node(self, raw: dict) -> dict:
        n = sanitize_node(raw)
        if not n:
            return {"ok": False, "reason": "id 와 url 은 필수입니다."}
        with self._lock:
            cur = self._get(n["id"])
            if cur is not None:
                # 토큰이 비어 오면 기존 토큰 유지(마스킹)
                if not n["token"]:
                    n["token"] = cur.get("token", "")
                n["last_sync"] = cur.get("last_sync", 0)
                n["last_poll"] = cur.get("last_poll", 0)
                self.nodes = [n if x["id"] == n["id"] else x for x in self.nodes]
            else:
                self.nodes.append(n)
            self._save()
        return {"ok": True, "node": _public_node(n, self._cache)}

    def delete_node(self, nid: str) -> dict:
        with self._lock:
            before = len(self.nodes)
            self.nodes = [x for x in self.nodes if x["id"] != nid]
            self._cache.pop(nid, None)
            self._save()
        # 복제본도 정리(베스트 에포트)
        shutil.rmtree(os.path.join(self.replicas_dir, nid), ignore_errors=True)
        return {"ok": before != len(self.nodes)}

    def test_node(self, raw: dict) -> dict:
        """연결 테스트: url(+token) 또는 등록된 id 로 meta 를 받아 본다."""
        nid = str(raw.get("id") or "").strip()
        if nid:
            n = self._get(nid)
            if n is None:
                return {"ok": False, "reason": "등록되지 않은 노드"}
            url, token = n["url"], n.get("token")
        else:
            url = str(raw.get("url") or "").strip()
            token = raw.get("token") or None
            if not url:
                return {"ok": False, "reason": "url 필요"}
            if not re.match(r"^https?://", url):
                url = "http://" + url
        try:
            meta = _probe_meta(url, token, timeout=8.0)
            ov = meta.get("overall") or {}
            return {"ok": True, "version": meta.get("version"),
                    "hostname": meta.get("hostname"),
                    "storages": ov.get("root_count") or 0,
                    "used_bytes": ov.get("total_scanned_bytes") or 0}
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            reason = "인증 실패(토큰 확인)" if e.code == 401 else ("HTTP %d" % e.code)
            return {"ok": False, "reason": reason}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": str(e)}

    # --- 동기화(폴링 + 복제) ---
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="portal-sync",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                for n in list(self.nodes):
                    if not n.get("enabled"):
                        continue
                    nid = n["id"]
                    with self._lock:
                        if nid in self._inflight:
                            continue
                    due_poll = (now - n.get("last_poll", 0)) >= self.poll_every
                    # 복제 시점은 예약 스캔과 동일한 반복주기 모델(schedule_due)로 판정.
                    # last_run 자리에 last_sync 를 넣어 마지막 복제 이후를 기준으로 한다.
                    due_rep = (n.get("mode") in ("both", "replicate") and
                               setmod.schedule_due(dict(n, last_run=n.get("last_sync", 0)), now))
                    if due_poll or due_rep:
                        with self._lock:
                            self._inflight.add(nid)
                        threading.Thread(target=self._sync_one, args=(nid, due_rep),
                                         daemon=True).start()
            except Exception:  # noqa: BLE001 — 스케줄러는 죽지 않는다
                pass
            self._stop.wait(self.tick)

    def sync_now(self, nid: Optional[str] = None) -> dict:
        targets = [nid] if nid else [n["id"] for n in self.nodes if n.get("enabled")]
        for t in targets:
            with self._lock:
                if t in self._inflight:
                    continue
                self._inflight.add(t)
            threading.Thread(target=self._sync_one, args=(t, True), daemon=True).start()
        return {"ok": True, "syncing": targets}

    def _sync_one(self, nid: str, do_rep: bool) -> None:
        try:
            n = self._get(nid)
            if n is None:
                return
            try:
                meta = _probe_meta(n["url"], n.get("token"))
                with self._lock:
                    self._cache[nid] = {
                        "online": True, "ts": time.time(),
                        "version": meta.get("version"), "hostname": meta.get("hostname"),
                        "overall": meta.get("overall") or {},
                        "isilon": meta.get("isilon") or {"configured": False},
                        "error": "",
                    }
                n["last_poll"] = time.time()
                n["last_status"] = "online"
                n["last_error"] = ""
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    c = self._cache.get(nid, {})
                    c.update({"online": False, "error": str(e)})
                    self._cache[nid] = c
                n["last_poll"] = time.time()
                n["last_status"] = "offline"
                n["last_error"] = str(e)
                return  # 오프라인이면 복제 생략
            if do_rep and n.get("mode") in ("both", "replicate"):
                try:
                    newest = self._replicate(n)
                    n["last_sync"] = max(float(n.get("last_sync", 0)), newest)
                    self._save()
                except Exception as e:  # noqa: BLE001
                    n["last_error"] = "replicate: " + str(e)
        finally:
            with self._lock:
                self._inflight.discard(nid)

    def _replicate(self, n: dict) -> float:
        """완료 DB(증분) + meta.json 을 replicas/<id>/ 로 안전하게 내려받는다."""
        since = int(float(n.get("last_sync", 0) or 0))
        url = n["url"].rstrip("/") + "/api/dbexport?since=%d" % since
        dest = os.path.join(self.replicas_dir, n["id"])
        os.makedirs(os.path.join(dest, "scans"), exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".tar.gz", dir=self.replicas_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                resp = _http_get(url, n.get("token"), timeout=300.0)
                shutil.copyfileobj(resp, f)
            newest = float(n.get("last_sync", 0) or 0)
            tf = tarfile.open(tmp, "r:gz")
            try:
                for m in tf:
                    if not m.isfile():
                        continue
                    name = m.name.lstrip("/")
                    if name == "meta.json":
                        outp = os.path.join(dest, "meta.json")
                    elif (name.startswith("scans/") and name.endswith(".db")
                          and "/" not in name[6:] and ".." not in name):
                        outp = os.path.join(dest, "scans", os.path.basename(name))
                    else:
                        continue  # 경로 탈출 방지: 그 외 멤버는 무시
                    src = tf.extractfile(m)
                    if src is None:
                        continue
                    with open(outp, "wb") as w:
                        shutil.copyfileobj(src, w)
            finally:
                tf.close()
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # meta 의 완료 시각으로 last_sync 전진
        try:
            with open(os.path.join(dest, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            for s in meta.get("scans", []):
                newest = max(newest, float(s.get("finished_at") or 0))
        except (OSError, ValueError):
            pass
        return newest

    # --- 글로벌 롤업 ---
    def overview(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes_snapshot = list(self.nodes)
        regions = {}
        total_used = 0
        storages = 0
        online = 0
        active = 0
        out_nodes = []
        for n in nodes_snapshot:
            c = cache.get(n["id"], {})
            ov = c.get("overall") or {}
            used = int(ov.get("total_scanned_bytes") or 0)
            st = int(ov.get("root_count") or 0)
            is_on = bool(c.get("online"))
            if is_on:
                online += 1
            active += int(ov.get("active_scans") or 0)
            total_used += used
            storages += st
            reg = n.get("region") or "(미지정)"
            r = regions.setdefault(reg, {"region": reg, "used_bytes": 0, "storages": 0})
            r["used_bytes"] += used
            r["storages"] += st
            out_nodes.append({
                "id": n["id"], "region": n.get("region"), "url": n["url"],
                "enabled": n.get("enabled"), "online": is_on,
                "version": c.get("version"), "hostname": c.get("hostname"),
                "last_poll": n.get("last_poll"), "last_sync": n.get("last_sync"),
                "last_error": n.get("last_error") or c.get("error") or "",
                "used_bytes": used, "storages": st,
                "active_scans": int(ov.get("active_scans") or 0),
                "roots": ov.get("roots") or [],
                "isilon": c.get("isilon") or {"configured": False},
            })
        return {
            "ok": True,
            "version": __version__,
            "totals": {"used_bytes": total_used, "storages": storages,
                       "nodes_online": online, "nodes_total": len(nodes_snapshot),
                       "active_scans": active},
            "regions": sorted(regions.values(), key=lambda x: -x["used_bytes"]),
            "nodes": out_nodes,
        }

    # --- 경로 비교(Cross-DC) — 복제본 DB 를 경로로 조인 ---
    @staticmethod
    def _to_local(node: dict, logical: str) -> str:
        """공통 논리 경로를 이 노드의 로컬 경로로 변환한다(별칭이 있으면).

        alias_logical 접두어를 alias_local 로 바꾼다. 별칭이 없으면 그대로(절대경로 일치).
        """
        al = (node.get("alias_local") or "").rstrip("/")
        lo = (node.get("alias_logical") or "").rstrip("/")
        if al and lo and (logical == lo or logical.startswith(lo + "/")):
            return al + logical[len(lo):]
        return logical

    def _replica_db_for(self, node_id: str, path: str):
        """노드 복제본에서 path 를 포함하는(root 가 prefix) 최신 완료 스캔 DB 경로."""
        rep = os.path.join(self.replicas_dir, node_id)
        try:
            with open(os.path.join(rep, "meta.json"), "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return None
        best = None
        for s in meta.get("scans", []):
            root = (s.get("root_path") or "").rstrip("/")
            if path == root or path.startswith(root + "/"):
                if best is None or float(s.get("finished_at") or 0) > float(best.get("finished_at") or 0):
                    best = s
        if not best:
            return None
        dbf = best.get("db_filename") or os.path.basename(best.get("db_path") or "")
        dbp = os.path.join(rep, "scans", dbf)
        return dbp if (dbf and os.path.exists(dbp)) else None

    def compare_path(self, path: str) -> dict:
        """같은 경로의 노드별 용량/파일 수를 모아 비교한다(복제본 기준)."""
        path = os.path.abspath(path)
        rows = []
        for n in self.nodes:
            entry = {"id": n["id"], "region": n.get("region"), "found": False,
                     "total_bytes": 0, "own_bytes": 0, "total_files": 0, "subdir_count": 0}
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            dbp = self._replica_db_for(n["id"], local)
            if dbp:
                try:
                    c = dbmod.connect(dbp)
                    rid = dbmod.latest_run_id(c)
                    r = c.execute(
                        "SELECT total_bytes, own_bytes, total_files, subdir_count "
                        "FROM directories WHERE run_id=? AND path=?", (rid, local)).fetchone()
                    c.close()
                    if r is not None:
                        sz = r["total_bytes"] if r["total_bytes"] else r["own_bytes"]
                        entry.update({"found": True, "total_bytes": sz,
                                      "own_bytes": r["own_bytes"], "total_files": r["total_files"],
                                      "subdir_count": r["subdir_count"]})
                except Exception:  # noqa: BLE001
                    pass
            rows.append(entry)
        return {"ok": True, "path": path, "rows": rows}

    def compare_matrix(self, path: str) -> dict:
        """path 직속 자식들의 노드별 용량 매트릭스(누락·드리프트 비교)."""
        path = os.path.abspath(path)
        node_ids = [n["id"] for n in self.nodes]
        children = {}
        for n in self.nodes:
            local = self._to_local(n, path)   # 논리 경로 → 이 노드의 로컬 경로(별칭)
            dbp = self._replica_db_for(n["id"], local)
            if not dbp:
                continue
            try:
                c = dbmod.connect(dbp)
                rid = dbmod.latest_run_id(c)
                prow = c.execute("SELECT depth FROM directories WHERE run_id=? AND path=?",
                                 (rid, local)).fetchone()
                if prow is None:
                    c.close()
                    continue
                like = (local.replace("\\", "\\\\").replace("%", "\\%")
                        .replace("_", "\\_")).rstrip("/") + "/%"
                kids = c.execute(
                    "SELECT name, total_bytes, own_bytes, subdir_count FROM directories "
                    "WHERE run_id=? AND depth=? AND path LIKE ? ESCAPE '\\'",
                    (rid, int(prow["depth"]) + 1, like)).fetchall()
                c.close()
                for k in kids:
                    e = children.setdefault(k["name"], {"name": k["name"], "subdir_count": 0, "vals": {}})
                    e["vals"][n["id"]] = int(k["total_bytes"] or k["own_bytes"] or 0)
                    e["subdir_count"] = max(e["subdir_count"], int(k["subdir_count"] or 0))
            except Exception:  # noqa: BLE001
                pass
        out = []
        for e in children.values():
            e["sum"] = sum(e["vals"].values())
            out.append(e)
        out.sort(key=lambda x: -x["sum"])
        return {"ok": True, "path": path, "nodes": node_ids, "children": out}


# ------------------------------------------------------------------ HTTP 핸들러
class PortalHandler(BaseHTTPRequestHandler):
    data_dir = ""
    controller = None  # PortalController

    def log_message(self, fmt, *args):  # noqa: D401 — 조용히
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _send_html(self, path: str) -> None:
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404, "portal.html not found")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, OSError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(PORTAL_HTML)
            return
        c = self.controller
        try:
            if path == "/api/portal/nodes":
                self._send_json(c.list_nodes())
                return
            if path == "/api/portal/overview":
                self._send_json(c.overview())
                return
            if path == "/api/portal/compare":
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                if not qp:
                    self._send_json({"ok": False, "reason": "path 필요"}, status=400)
                else:
                    self._send_json(c.compare_path(qp))
                return
            if path == "/api/portal/matrix":
                qp = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                if not qp:
                    self._send_json({"ok": False, "reason": "path 필요"}, status=400)
                else:
                    self._send_json(c.compare_matrix(qp))
                return
            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json_body()
        c = self.controller
        try:
            if path == "/api/portal/nodes":
                self._send_json(c.upsert_node(body))
                return
            if path == "/api/portal/nodes/delete":
                self._send_json(c.delete_node(str(body.get("id") or "")))
                return
            if path == "/api/portal/test":
                self._send_json(c.test_node(body))
                return
            if path == "/api/portal/sync":
                self._send_json(c.sync_now(body.get("id") or None))
                return
            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def serve_portal(data_dir: str, host: str = "0.0.0.0", port: int = 8800):
    """글로벌 포탈 HTTP 서버를 만들어 반환한다(호출 측에서 serve_forever)."""
    data_dir = os.path.abspath(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    controller = PortalController(data_dir)
    controller.start()
    handler = type("BoundPortalHandler", (PortalHandler,),
                   {"data_dir": data_dir, "controller": controller})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller
    return httpd

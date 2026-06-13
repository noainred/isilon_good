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
import secrets
import shlex
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
from . import manager as mgrmod
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
    ov = c.get("overall") or {}
    out = dict(n)
    out["token"] = ""
    out["token_set"] = bool(n.get("token"))
    out["online"] = bool(c.get("online"))
    out["version"] = c.get("version")
    out["hostname"] = c.get("hostname")
    out["error"] = n.get("last_error") or c.get("error") or ""
    out["active_scans"] = int(ov.get("active_scans") or 0)   # 지금 스캔 중인 개수
    out["scanning"] = out["active_scans"] > 0
    out["used_bytes"] = int(ov.get("total_scanned_bytes") or 0)
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


# ------------------------------------------------------------ 원격 자동 구성
def agent_bundle_bytes() -> bytes:
    """엣지에 배포할 isilon_usage 패키지(.py/.html)를 tar.gz 로 묶는다.

    엣지가 HQ 포탈에서 코드를 직접 받아 설치할 수 있게 한다(폐쇄망에서도 인터넷
    없이). 포탈이 실행 중인 바로 그 패키지를 그대로 내려준다.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(os.listdir(HERE)):
            if name.endswith(".py") or name.endswith(".html"):
                tf.add(os.path.join(HERE, name), arcname="isilon_usage/" + name)
    return buf.getvalue()


def build_provision_script(*, host, port, token, path, hq_base, install="systemd",
                           data_dir="/data/isilon_usage",
                           edge_dir="/opt/isilon_edge") -> str:
    """엣지에서 복붙 실행할 자동 구성 스크립트(bash)를 만든다.

    HQ 포탈에서 코드를 받아(agent-bundle) 배치하고, api_token·mount-base 와 함께
    serve 를 기동한다(systemd 또는 nohup). 값은 shell 주입을 막기 위해 따옴표 처리.
    """
    q = shlex.quote
    hq = (hq_base or "http://<HQ-IP>:8800").rstrip("/")
    head = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "# === isilon_usage 엣지 자동 구성 (HQ 포탈 생성) ===\n"
        "EDGE_DIR=%s\nDATA=%s\nMOUNT=%s\nPORT=%d\nTOKEN=%s\nHQ=%s\n\n"
        'echo "[1/3] HQ 포탈에서 코드 받기 (인터넷 불필요)"\n'
        'sudo mkdir -p "$EDGE_DIR" "$DATA"\n'
        'curl -fsSL "$HQ/api/portal/agent-bundle" | sudo tar -xz -C "$EDGE_DIR"\n'
        '(cd "$EDGE_DIR" && python3 -m isilon_usage --version)\n'
    ) % (q(edge_dir), q(data_dir), q(path), int(port), q(token), q(hq))
    if install == "nohup":
        tail = (
            '\necho "[2/3] nohup 으로 기동 (포트 $PORT)"\n'
            'cd "$EDGE_DIR"\n'
            'nohup python3 -m isilon_usage serve --data-dir "$DATA" --mount-base "$MOUNT" \\\n'
            '    --host 0.0.0.0 --port "$PORT" --api-token "$TOKEN" \\\n'
            '    > /var/log/isilon_usage.log 2>&1 &\n'
            'echo "[3/3] 완료 — http://%s:%d/  (HQ 포탈이 자동 폴링)"\n'
        ) % (host, int(port))
    else:
        tail = (
            '\necho "[2/3] systemd 서비스 설치/기동 (포트 $PORT)"\n'
            "sudo tee /etc/systemd/system/isilon_usage.service >/dev/null <<UNIT\n"
            "[Unit]\n"
            "Description=isilon_usage edge scanner\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n"
            "[Service]\n"
            "Type=simple\n"
            "WorkingDirectory=$EDGE_DIR\n"
            "ExecStart=/usr/bin/python3 -m isilon_usage serve --data-dir $DATA "
            "--mount-base $MOUNT --host 0.0.0.0 --port $PORT --api-token $TOKEN\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
            "UNIT\n"
            "sudo systemctl daemon-reload\n"
            "sudo systemctl enable --now isilon_usage\n"
            'echo "[3/3] 완료 — http://%s:%d/  ·  journalctl -u isilon_usage -f"\n'
        ) % (host, int(port))
    return head + tail


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
                        "storage": meta.get("storage") or [],
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
    @staticmethod
    def _fs_capacity(roots_l):
        """노드의 파일시스템 사용/전체 용량. 같은 FS(같은 total)는 한 번만 센다."""
        seen = {}   # fs_total -> 최대 fs_used
        for rt in roots_l:
            ftot = int(rt.get("fs_total_bytes") or 0)
            if ftot <= 0:
                continue
            fused = int(rt.get("fs_used_bytes") or 0)
            if ftot not in seen or fused > seen[ftot]:
                seen[ftot] = fused
        ftot = sum(seen.keys())
        fused = sum(seen.values())
        pct = round(fused / ftot * 100, 1) if ftot else None
        return ftot, fused, max(0, ftot - fused), pct

    @staticmethod
    def _running_scan(roots_l):
        """진행 중(discovering/sizing) 스캔의 시작 시각·경과·단계 요약(없으면 None)."""
        act = [rt for rt in roots_l if rt.get("status") in ("discovering", "sizing")]
        if not act:
            return None
        rep = min(act, key=lambda rt: float(rt.get("started_at") or 9e18))
        started = min((float(rt.get("started_at") or 0) for rt in act
                       if rt.get("started_at")), default=0) or None
        updated = max((float(rt.get("updated_at") or 0) for rt in act), default=0) or None
        return {
            "started_at": started, "updated_at": updated,
            "phase": rep.get("phase"), "root_path": rep.get("root_path"),
            "processed_dirs": int(rep.get("processed_dirs") or 0),
            "total_dirs": int(rep.get("total_dirs") or 0),
            "count": len(act),
        }

    def overview(self) -> dict:
        with self._lock:
            cache = dict(self._cache)
            nodes_snapshot = list(self.nodes)
        regions = {}
        total_used = 0
        total_fs_total = 0
        total_fs_used = 0
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
            roots_l = ov.get("roots") or []
            last_scan = max([0] + [max(rt.get("finished_at") or 0, rt.get("updated_at") or 0)
                                   for rt in roots_l])     # 그 DC 가 마지막으로 스캔한 시각
            fs_total, fs_used, fs_free, fs_pct = self._fs_capacity(roots_l)
            total_fs_total += fs_total
            total_fs_used += fs_used
            out_nodes.append({
                "id": n["id"], "region": n.get("region"), "url": n["url"],
                "enabled": n.get("enabled"), "online": is_on,
                "version": c.get("version"), "hostname": c.get("hostname"),
                "last_poll": n.get("last_poll"), "last_sync": n.get("last_sync"),
                "last_scan_at": last_scan or None,
                "last_error": n.get("last_error") or c.get("error") or "",
                "used_bytes": used, "storages": st,
                "active_scans": int(ov.get("active_scans") or 0),
                "fs_total_bytes": fs_total, "fs_used_bytes": fs_used,
                "fs_free_bytes": fs_free, "fs_used_pct": fs_pct,
                "running": self._running_scan(roots_l),
                "roots": roots_l,
                "isilon": c.get("isilon") or {"configured": False},
                "storage": c.get("storage") or [],
            })
        return {
            "ok": True,
            "version": __version__,
            "totals": {"used_bytes": total_used, "storages": storages,
                       "fs_total_bytes": total_fs_total, "fs_used_bytes": total_fs_used,
                       "nodes_online": online, "nodes_total": len(nodes_snapshot),
                       "active_scans": active},
            "regions": sorted(regions.values(), key=lambda x: -x["used_bytes"]),
            "nodes": out_nodes,
        }

    # --- 노드 응답시간(핑) ---
    def ping_nodes(self) -> dict:
        """각 노드로 HQ→노드 왕복 응답시간(ms)을 측정한다(가벼운 meta 요청 기준).

        포탈이 능동적으로 잴 수 있는 건 'HQ→각 노드' 지연이다. 엣지끼리의
        DC↔DC 메시 핑은 엣지 협조가 필요해 지원하지 않는다(여기선 미측정).
        여러 노드를 동시에(스레드풀) 측정해 전체 소요를 줄인다.
        """
        from concurrent.futures import ThreadPoolExecutor

        with self._lock:
            nodes_snapshot = list(self.nodes)

        def _one(n):
            entry = {"id": n["id"], "region": n.get("region"), "url": n["url"],
                     "online": False, "latency_ms": None, "error": ""}
            if not n.get("enabled"):
                entry["error"] = "비활성"
                return entry
            t0 = time.monotonic()
            try:
                _probe_meta(n["url"], n.get("token"), timeout=8.0)
                entry["online"] = True
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            except urllib.error.HTTPError as e:   # 응답이 왔으니 도달은 됨
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
                entry["online"] = True
                entry["error"] = "인증 실패" if e.code == 401 else ("HTTP %d" % e.code)
            except Exception as e:  # noqa: BLE001
                entry["error"] = str(e)
            return entry

        if nodes_snapshot:
            with ThreadPoolExecutor(max_workers=min(16, len(nodes_snapshot))) as ex:
                results = list(ex.map(_one, nodes_snapshot))
        else:
            results = []
        return {"ok": True, "results": results, "checked_at": time.time()}

    # --- 원격 자동 구성 ---
    def provision_plan(self, raw: dict) -> dict:
        """IP/포트/경로 등으로 엣지 설치 스크립트를 생성하고 노드를 자동 등록한다(SSH 없음).

        '엣지에서 복붙 실행할 스크립트'를 돌려주고 그 노드를 포탈에 등록해 둔다
        (엣지가 뜨면 폴링으로 자동 연결). 비밀번호는 다루지 않아 안전하다.
        """
        host = (raw.get("host") or "").strip()
        if not host:
            return {"ok": False, "reason": "host(IP/주소) 필요"}
        try:
            port = int(raw.get("port") or 8765)
        except (TypeError, ValueError):
            port = 8765
        token = (raw.get("token") or "").strip() or secrets.token_hex(16)
        path = (raw.get("path") or "/mnt/hadoop").strip()
        name = ((raw.get("name") or "").strip()
                or re.sub(r"[^A-Za-z0-9_.-]+", "-", host).strip("-") or "edge")
        region = (raw.get("region") or "").strip()
        install = "nohup" if (raw.get("install") == "nohup") else "systemd"
        hq_base = (raw.get("hq_base") or "").strip()
        edge_url = "http://%s:%d" % (host, port)
        script = build_provision_script(host=host, port=port, token=token, path=path,
                                        hq_base=hq_base, install=install)
        node_res = self.upsert_node({
            "id": name, "region": region, "url": edge_url, "token": token,
            "alias_local": path, "alias_logical": path,
            "unit": "minute", "every": 30, "mode": "both", "enabled": True,
        })
        return {"ok": True, "script": script, "token": token, "edge_url": edge_url,
                "install": install, "registered": bool(node_res.get("ok")),
                "node": node_res.get("node")}

    def provision_ssh(self, raw: dict) -> dict:
        """[옵션] 원격에 SSH 로 접속해 구성 스크립트를 실행한다.

        ssh(키 인증) 또는 sshpass(비밀번호)가 있어야 한다. 비밀번호는 보관하지 않고
        1회성으로만 쓴다. 무의존 원칙상 기본은 'A(스크립트 생성)'이며 이건 옵션이다.
        """
        import subprocess
        plan = self.provision_plan(raw)
        if not plan.get("ok"):
            return plan
        host = (raw.get("host") or "").strip()
        user = (raw.get("ssh_user") or "root").strip()
        password = raw.get("ssh_password") or ""
        try:
            ssh_port = int(raw.get("ssh_port") or 22)
        except (TypeError, ValueError):
            ssh_port = 22
        ssh = shutil.which("ssh")
        if not ssh:
            return dict(plan, ok=False,
                        reason="ssh 바이너리가 없습니다 — 생성된 스크립트를 엣지에서 수동 실행하세요.")
        cmd = []
        if password:
            sshpass = shutil.which("sshpass")
            if not sshpass:
                return dict(plan, ok=False,
                            reason="비밀번호 인증엔 sshpass 가 필요합니다(미설치). 키 인증을 쓰거나 스크립트를 수동 실행하세요.")
            cmd = [sshpass, "-p", password]
        cmd += [ssh, "-p", str(ssh_port),
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "%s@%s" % (user, host), "bash -s"]
        try:
            p = subprocess.run(cmd, input=plan["script"].encode(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=300)
            out = p.stdout.decode("utf-8", "replace")[-4000:]
            return dict(plan, ok=(p.returncode == 0), returncode=p.returncode, output=out)
        except Exception as e:  # noqa: BLE001
            return dict(plan, ok=False, reason="SSH 실행 실패: %s" % e)

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
            if path == "/api/portal/ping":
                self._send_json(c.ping_nodes())
                return
            if path == "/api/portal/agent-bundle":
                data = agent_bundle_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="isilon_usage_agent.tar.gz"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
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
            if path == "/api/portal/provision":
                self._send_json(c.provision_plan(body))
                return
            if path == "/api/portal/provision/ssh":
                self._send_json(c.provision_ssh(body))
                return
            self._send_json({"ok": False, "reason": "unknown_endpoint"}, status=404)
        except ConnectionError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=500)


def serve_portal(data_dir: str, host: str = "0.0.0.0", port: int = 8800):
    """글로벌 포탈 HTTP 서버를 만들어 반환한다(호출 측에서 serve_forever)."""
    data_dir = os.path.abspath(data_dir)
    mgrmod.migrate_legacy_config(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    controller = PortalController(data_dir)
    controller.start()
    handler = type("BoundPortalHandler", (PortalHandler,),
                   {"data_dir": data_dir, "controller": controller})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.controller = controller
    return httpd

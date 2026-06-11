"""튜닝 점검 — OS 커널/마운트 설정을 분석해 '거대 NAS 스캔을 빠르게' 만드는
튜닝 포인트를 체크한다.

대상 환경은 NFS(아이실론) 위의 메타데이터 다수(수억 stat/readdir) 스캔이라,
핵심은 **NFS 동시성(nconnect·RPC 슬롯)·속성 캐시(actimeo)·페이지/덴트리 캐시
(swappiness·vfs_cache_pressure)·CPU 거버너·open files 한도** 다.

모든 값은 /proc·/sys 에서 읽고(없으면 'na'), 항목별로 현재값·권장값·상태
(ok/warn/tip)·적용법(fix)을 돌려준다. 표준 라이브러리만 사용.
"""

import os
import resource


def _read(path):
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path):
    v = _read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _item(key, current, recommended, level, note, fix=None):
    return {"key": key, "current": current, "recommended": recommended,
            "level": level, "note": note, "fix": fix}


def parse_nfs_mounts():
    """/proc/mounts 에서 nfs/nfs4 마운트를 파싱한다."""
    out = []
    data = _read("/proc/mounts")
    if not data:
        return out
    for line in data.splitlines():
        p = line.split()
        if len(p) < 4:
            continue
        dev, mnt, fstype, opts = p[0], p[1], p[2], p[3]
        if fstype not in ("nfs", "nfs4"):
            continue
        od = {}
        for tok in opts.split(","):
            if "=" in tok:
                k, v = tok.split("=", 1)
                od[k] = v
            else:
                od[tok] = True
        out.append({"device": dev, "mount": mnt, "fstype": fstype, "options": od})
    return out


def _pick_target(mounts, scan_root):
    if not scan_root:
        return mounts[0] if mounts else None
    root = os.path.abspath(scan_root)
    best, best_len = None, -1
    for m in mounts:
        mp = m["mount"]
        try:
            if (root == mp or root.startswith(mp.rstrip("/") + "/")) and len(mp) > best_len:
                best, best_len = m, len(mp)
        except Exception:
            continue
    return best or (mounts[0] if mounts else None)


def _nfs_section(target):
    items = []
    if not target:
        return {"name": "NFS 마운트(대상)", "items": [
            _item("NFS 마운트", "대상 경로가 NFS가 아님/미탐지", "-", "na",
                  "대상이 로컬 FS이거나 /proc/mounts에서 못 찾음.")]}
    o = target["options"]
    mp = target["mount"]

    # nconnect — 서버당 TCP 연결 다중화(메타데이터 병렬의 핵심, Linux 5.3+)
    nconn = o.get("nconnect")
    items.append(_item(
        "nconnect", nconn or "미설정(기본 1)", "8~16",
        "ok" if (nconn and int(nconn) >= 4) else "warn",
        "서버당 TCP 연결 수. 1이면 모든 RPC가 한 연결에 직렬화 → 메타데이터 병렬의 "
        "가장 큰 제약. 4~16으로 올리면 stat/readdir 처리량이 크게 늘 수 있음.",
        "remount: mount -o remount,nconnect=16 %s  (또는 fstab 후 재마운트)" % mp))

    # NFS 버전
    vers = o.get("vers") or o.get("nfsvers")
    items.append(_item(
        "vers(NFS 버전)", vers or "미상", "3 또는 4.1+(pNFS)",
        "tip",
        "메타데이터 다수엔 NFSv3가 단순·빠른 경우가 많고, 4.1+는 pNFS로 다중 노드 "
        "병렬이 가능(아이실론 SmartConnect와 함께)."))

    # rsize/wsize
    for k in ("rsize", "wsize"):
        v = _read_int_opt(o.get(k))
        items.append(_item(
            k, o.get(k) or "미상", "1048576(1MB)",
            "ok" if (v and v >= 1048576) else "tip",
            "전송 블록 크기. 1MB 권장(처리량)."))

    # 속성 캐시 — noac 면 치명적(매 접근 GETATTR)
    if o.get("noac"):
        items.append(_item("actimeo/noac", "noac(속성 캐시 끔)", "actimeo=600",
                           "warn", "noac는 매 stat마다 서버 왕복 → 메타데이터 스캔에 최악. 끄세요.",
                           "remount without noac, with actimeo=600"))
    else:
        items.append(_item("actimeo(속성 캐시)", o.get("actimeo") or "기본", "600(읽기 위주)",
                           "tip", "속성 캐시 시간↑ → 반복 stat의 GETATTR 왕복 감소."))

    # hard/soft
    if o.get("soft"):
        items.append(_item("hard/soft", "soft", "hard", "warn",
                           "soft는 타임아웃 시 I/O 오류로 결과 누락 위험. hard 권장."))
    else:
        items.append(_item("hard/soft", "hard", "hard", "ok", "안전한 기본값."))

    # atime
    if not (o.get("noatime") or o.get("relatime")):
        items.append(_item("atime", "atime(접근시각 기록)", "noatime", "tip",
                           "스캔은 읽기지만 noatime이면 불필요한 메타 쓰기를 줄임.",
                           "remount,noatime"))
    else:
        items.append(_item("atime", "noatime/relatime", "noatime", "ok", "양호."))

    return {"name": "NFS 마운트(대상): %s" % mp, "items": items}


def _read_int_opt(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _kernel_section():
    items = []
    # sunrpc RPC 슬롯 — 동시 in-flight RPC 수(메타데이터 병렬)
    cur = _read("/proc/sys/sunrpc/tcp_max_slot_table_entries")
    cur2 = _read("/proc/sys/sunrpc/tcp_slot_table_entries")
    val = _read_int("/proc/sys/sunrpc/tcp_slot_table_entries")
    items.append(_item(
        "sunrpc.tcp_slot_table_entries", cur2 or "na", "128 이상",
        "na" if val is None else ("ok" if val >= 128 else "warn"),
        "한 TCP 연결에서 동시에 처리하는 RPC 수. 기본이 낮으면(16 등) NFS 메타데이터 "
        "병렬이 막힘. nconnect와 함께 올리면 효과 큼. (max=%s)" % (cur or "na"),
        "sysctl -w sunrpc.tcp_slot_table_entries=128"))

    sw = _read_int("/proc/sys/vm/swappiness")
    items.append(_item(
        "vm.swappiness", sw if sw is not None else "na", "10 이하",
        "na" if sw is None else ("ok" if sw <= 10 else "tip"),
        "낮을수록 페이지 캐시(=per-run DB 캐시)를 RAM에 유지 → 스왑 지연 회피.",
        "sysctl -w vm.swappiness=10"))

    vc = _read_int("/proc/sys/vm/vfs_cache_pressure")
    items.append(_item(
        "vm.vfs_cache_pressure", vc if vc is not None else "na", "50~100",
        "na" if vc is None else ("ok" if vc <= 100 else "tip"),
        "낮출수록 dentry/inode 캐시를 더 오래 유지 → 반복 readdir/stat 가속.",
        "sysctl -w vm.vfs_cache_pressure=50"))

    fm = _read_int("/proc/sys/fs/file-max")
    items.append(_item(
        "fs.file-max", fm if fm is not None else "na", "≥ 1,000,000",
        "na" if fm is None else ("ok" if fm >= 1_000_000 else "tip"),
        "시스템 전체 열 수 있는 파일 수 상한."))

    rmem = _read_int("/proc/sys/net/core/rmem_max")
    items.append(_item(
        "net.core.rmem_max", rmem if rmem is not None else "na", "≥ 16 MB",
        "na" if rmem is None else ("ok" if rmem >= 16 * 1024 * 1024 else "tip"),
        "TCP 수신 버퍼 상한 — NFS 처리량(특히 고지연 링크).",
        "sysctl -w net.core.rmem_max=16777216"))
    return {"name": "커널 / sysctl", "items": items}


def _limits_section():
    items = []
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        items.append(_item(
            "open files(nofile, soft)", soft, "≥ 65536",
            "ok" if soft >= 65536 else ("warn" if soft < 4096 else "tip"),
            "워커 fd + DB + 소켓. 너무 낮으면 다중 워커/연결에서 부족.",
            "ulimit -n 65536  (또는 limits.conf/systemd LimitNOFILE)"))
    except (ValueError, OSError):
        pass
    return {"name": "프로세스 한도(ulimit)", "items": items}


def _cpu_section():
    items = []
    gov = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov is None:
        items.append(_item("CPU 거버너", "na", "performance", "na",
                           "거버너 정보를 읽을 수 없음(가상화/컨테이너일 수 있음)."))
    else:
        items.append(_item(
            "CPU 거버너", gov, "performance",
            "ok" if gov == "performance" else "tip",
            "powersave면 클럭이 낮아져 단일코어(GIL) 작업이 느려질 수 있음.",
            "cpupower frequency-set -g performance"))
    items.append(_item("논리 CPU", os.cpu_count() or "na", "-", "ok",
                       "동시 워커 수 상한 참고."))
    return {"name": "CPU / 전원", "items": items}


def _localdisk_section(data_dir):
    items = []
    if not data_dir:
        return {"name": "로컬 디스크(DB)", "items": items}
    try:
        st = os.stat(data_dir)
        dev = st.st_dev
        major, minor = os.major(dev), os.minor(dev)
        rot = _read("/sys/dev/block/%d:%d/queue/rotational" % (major, minor))
        if rot is None:
            # 파티션이면 부모 디스크에서
            rot = "na"
        items.append(_item(
            "DB 디스크 유형", ("HDD(회전)" if rot == "1" else ("SSD/NVMe" if rot == "0" else "na")),
            "SSD/NVMe", "ok" if rot == "0" else ("tip" if rot == "1" else "na"),
            "per-run DB 커밋(fsync) 지연은 디스크 속도에 직결. SSD 권장."))
    except OSError:
        pass
    # data-dir 가 NFS 위에 있으면 경고(DB는 로컬에)
    for m in parse_nfs_mounts():
        mp = m["mount"].rstrip("/")
        if os.path.abspath(data_dir) == mp or os.path.abspath(data_dir).startswith(mp + "/"):
            items.append(_item("DB 위치", "NFS 위(%s)" % m["mount"], "로컬 디스크", "warn",
                               "per-run DB를 NFS에 두면 커밋이 매우 느리고 락 문제. 로컬로 옮기세요."))
            break
    return {"name": "로컬 디스크(DB)", "items": items}


def check(scan_root=None, data_dir=None):
    """튜닝 점검 보고서를 만든다."""
    mounts = parse_nfs_mounts()
    target = _pick_target(mounts, scan_root)
    sections = [
        _nfs_section(target),
        _kernel_section(),
        _limits_section(),
        _cpu_section(),
        _localdisk_section(data_dir),
    ]
    counts = {"ok": 0, "warn": 0, "tip": 0, "na": 0}
    for s in sections:
        for it in s["items"]:
            counts[it["level"]] = counts.get(it["level"], 0) + 1
    return {
        "ok": True,
        "scan_root": os.path.abspath(scan_root) if scan_root else None,
        "nfs_mounts": [{"mount": m["mount"], "device": m["device"], "fstype": m["fstype"]}
                       for m in mounts],
        "target_mount": target["mount"] if target else None,
        "sections": sections,
        "summary": counts,
    }

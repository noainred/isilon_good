"""서버/프로세스 자원 사용량 모니터링.

psutil 이 설치돼 있으면 사용하고, 없으면 리눅스 /proc 파일시스템을 직접
읽어 동작한다(아이실론을 마운트한 게이트웨이 서버처럼 추가 패키지 설치가
어려운 환경을 고려).

핵심 지표:
  - 시스템 전체 메모리 사용률 / 스왑
  - CPU 사용률, 1분 load average
  - 스캐너 프로세스 RSS (디렉터리 사용량을 직접 측정하는 프로세스의 메모리)
  - du 자식 프로세스 RSS (backend=du 일 때 실제 du 프로세스의 메모리)
"""

from typing import Dict, Optional, Tuple

import os
import threading
import time

from . import db as dbmod

try:  # psutil 은 선택적 의존성
    import psutil  # type: ignore

    _HAVE_PSUTIL = True
except Exception:  # pragma: no cover - 환경에 따라 다름
    psutil = None  # type: ignore
    _HAVE_PSUTIL = False


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


class Sample:
    """자원 샘플 1개 (Python 3.6 호환을 위해 dataclass 대신 일반 클래스)."""

    __slots__ = ("ts", "mem_total", "mem_used", "mem_percent", "swap_used",
                 "cpu_percent", "load1", "scanner_rss", "du_rss", "du_pid",
                 "scanner_cpu", "du_cpu")

    def __init__(self, ts=0.0, mem_total=0, mem_used=0, mem_percent=0.0,
                 swap_used=0, cpu_percent=0.0, load1=0.0, scanner_rss=0,
                 du_rss=0, du_pid=None, scanner_cpu=0.0, du_cpu=0.0):
        self.ts = ts
        self.mem_total = mem_total
        self.mem_used = mem_used
        self.mem_percent = mem_percent
        self.swap_used = swap_used
        self.cpu_percent = cpu_percent
        self.load1 = load1
        self.scanner_rss = scanner_rss
        self.du_rss = du_rss
        self.du_pid = du_pid
        self.scanner_cpu = scanner_cpu   # 스캐너 프로세스 CPU%(코어 합산, >100% 가능)
        self.du_cpu = du_cpu             # du 자식 프로세스 CPU%

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def _proc_cputime_seconds(pid: Optional[int]) -> Optional[float]:
    """프로세스의 누적 CPU 시간(user+system, 초). 없으면 None."""
    if not pid:
        return None
    if _HAVE_PSUTIL:
        try:
            t = psutil.Process(int(pid)).cpu_times()
            return float(t.user + t.system)
        except Exception:
            pass
    try:
        with open("/proc/%d/stat" % int(pid), "r") as fh:
            data = fh.read()
        # comm(2번째 필드)에 공백/괄호가 있을 수 있어 마지막 ')' 뒤부터 파싱
        rest = data[data.rfind(")") + 1:].split()
        utime = int(rest[11])   # 14번째 필드(utime)
        stime = int(rest[12])   # 15번째 필드(stime)
        return (utime + stime) / CLK_TCK
    except Exception:
        return None


# ----------------------------------------------------------------------------
# /proc 기반 폴백 구현
# ----------------------------------------------------------------------------

def _read_meminfo() -> Tuple[int, int, int]:
    """(mem_total, mem_used, swap_used) 바이트 단위로 반환."""
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    # 값은 보통 kB 단위
                    info[key] = int(parts[0]) * 1024
    except OSError:
        return (0, 0, 0)
    total = info.get("MemTotal", 0)
    # MemAvailable 이 있으면 가장 정확하게 "사용 중"을 계산
    if "MemAvailable" in info:
        used = total - info["MemAvailable"]
    else:
        free = info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0)
        used = total - free
    swap_used = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    return (total, max(used, 0), max(swap_used, 0))


def _read_proc_rss(pid: Optional[int]) -> int:
    """주어진 PID 의 RSS(바이트). /proc/<pid>/statm 의 두 번째 값 × 페이지 크기."""
    if not pid:
        return 0
    try:
        with open(f"/proc/{pid}/statm", "r") as fh:
            fields = fh.readline().split()
        if len(fields) >= 2:
            return int(fields[1]) * PAGE_SIZE
    except OSError:
        return 0
    return 0


_prev_cpu: Dict[str, float] = {}


def _read_cpu_percent() -> float:
    """/proc/stat 의 누적값 차분으로 전체 CPU 사용률(%)을 계산."""
    try:
        with open("/proc/stat", "r") as fh:
            line = fh.readline()
    except OSError:
        return 0.0
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return 0.0
    vals = [float(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
    total = sum(vals)
    prev_total = _prev_cpu.get("total", 0.0)
    prev_idle = _prev_cpu.get("idle", 0.0)
    _prev_cpu["total"] = total
    _prev_cpu["idle"] = idle
    dt = total - prev_total
    di = idle - prev_idle
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))


def _proc_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    if _HAVE_PSUTIL:
        try:
            return psutil.pid_exists(int(pid))
        except Exception:
            return False
    return os.path.exists(f"/proc/{pid}")


def collect(scanner_pid: int, du_pid: Optional[int] = None) -> Sample:
    """현재 시점의 자원 샘플 1개를 수집한다."""
    s = Sample(ts=time.time(), du_pid=du_pid)

    if _HAVE_PSUTIL:
        vm = psutil.virtual_memory()
        s.mem_total = int(vm.total)
        s.mem_used = int(vm.total - vm.available)
        s.mem_percent = float(vm.percent)
        try:
            sm = psutil.swap_memory()
            s.swap_used = int(sm.used)
        except Exception:
            s.swap_used = 0
        s.cpu_percent = float(psutil.cpu_percent(interval=None))
        try:
            s.scanner_rss = int(psutil.Process(scanner_pid).memory_info().rss)
        except Exception:
            s.scanner_rss = _read_proc_rss(scanner_pid)
        if du_pid and _proc_alive(du_pid):
            try:
                s.du_rss = int(psutil.Process(du_pid).memory_info().rss)
            except Exception:
                s.du_rss = _read_proc_rss(du_pid)
    else:
        total, used, swap_used = _read_meminfo()
        s.mem_total = total
        s.mem_used = used
        s.mem_percent = round((used / total * 100.0), 1) if total else 0.0
        s.swap_used = swap_used
        s.cpu_percent = round(_read_cpu_percent(), 1)
        s.scanner_rss = _read_proc_rss(scanner_pid)
        if du_pid and _proc_alive(du_pid):
            s.du_rss = _read_proc_rss(du_pid)

    try:
        s.load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        s.load1 = 0.0
    return s


class ResourceMonitor(threading.Thread):
    """주기적으로 자원 샘플을 수집해 DB(resource_samples)에 적재하는 스레드.

    backend=du 인 경우 스캐너가 현재 실행 중인 du 자식 프로세스의 PID를
    `set_du_pid()` 로 알려주면, 그 프로세스의 RSS 를 함께 기록한다.
    """

    def __init__(
        self,
        db_path: str,
        run_id: int,
        scanner_pid: int,
        *,
        interval: float = 2.0,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        super().__init__(name="resource-monitor", daemon=True)
        self.db_path = db_path
        self.run_id = run_id
        self.scanner_pid = scanner_pid
        self.interval = interval
        self.stop_event = stop_event or threading.Event()
        self._du_pid: Optional[int] = None
        self._lock = threading.Lock()
        self._written = 0
        self.peak_scanner_rss = 0
        self.peak_du_rss = 0
        # 프로세스별 CPU% 계산용 직전 측정값: role -> (pid, cpu_seconds, wall_ts)
        self._cpu_prev: Dict[str, tuple] = {}

    def _cpu_pct(self, role: str, pid: Optional[int]) -> float:
        """프로세스의 CPU%(직전 샘플 대비 델타). 코어 합산이라 100%를 넘을 수 있다."""
        if not pid:
            self._cpu_prev.pop(role, None)
            return 0.0
        now_t = time.time()
        now_c = _proc_cputime_seconds(pid)
        prev = self._cpu_prev.get(role)
        self._cpu_prev[role] = (pid, now_c, now_t)
        if now_c is None or not prev or prev[0] != pid or prev[1] is None:
            return 0.0
        dt = now_t - prev[2]
        dc = now_c - prev[1]
        if dt <= 0:
            return 0.0
        return round(max(0.0, dc / dt * 100.0), 1)

    def set_du_pid(self, pid: Optional[int]) -> None:
        with self._lock:
            self._du_pid = pid

    def get_du_pid(self) -> Optional[int]:
        with self._lock:
            return self._du_pid

    def _write_sample(self, conn) -> None:
        du_pid = self.get_du_pid()
        sample = collect(self.scanner_pid, du_pid)
        sample.scanner_cpu = self._cpu_pct("scanner", self.scanner_pid)
        sample.du_cpu = self._cpu_pct("du", du_pid)
        self.peak_scanner_rss = max(self.peak_scanner_rss, sample.scanner_rss)
        self.peak_du_rss = max(self.peak_du_rss, sample.du_rss)
        try:
            conn.execute(
                """
                INSERT INTO resource_samples
                    (run_id, ts, mem_total, mem_used, mem_percent,
                     swap_used, cpu_percent, load1, scanner_rss, du_rss, du_pid,
                     scanner_cpu, du_cpu)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.run_id, sample.ts, sample.mem_total, sample.mem_used,
                    sample.mem_percent, sample.swap_used, sample.cpu_percent,
                    sample.load1, sample.scanner_rss, sample.du_rss, sample.du_pid,
                    sample.scanner_cpu, sample.du_cpu,
                ),
            )
            dbmod.prune_samples(conn, self.run_id)
            conn.commit()
            self._written += 1
        except Exception as _exc:
            # 모니터링 실패가 스캔을 멈추면 안 된다.
            if os.environ.get("ISILON_DEBUG"):
                import sys
                print(f"[monitor] write failed: {type(_exc).__name__}: {_exc}",
                      file=sys.stderr)

    def run(self) -> None:  # noqa: D401
        conn = dbmod.connect(self.db_path)
        # psutil 의 cpu_percent 는 첫 호출이 0 을 반환하므로 워밍업
        if _HAVE_PSUTIL:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        try:
            while not self.stop_event.is_set():
                if not self.run_id:
                    # run 이 만들어지기 전이면 촘촘히 재확인해, run_id 가 정해지는
                    # 즉시(다음 0.05s 안에) 첫 샘플을 기록한다.
                    self.stop_event.wait(min(0.05, self.interval))
                    continue
                self._write_sample(conn)
                self.stop_event.wait(self.interval)
        finally:
            # 매우 짧은 스캔이라도 최소 1개의 샘플은 남긴다.
            if self.run_id and self._written == 0:
                self._write_sample(conn)
            conn.close()


def have_psutil() -> bool:
    return _HAVE_PSUTIL


def system_specs() -> dict:
    """서버 사양(논리 CPU 수, 총/가용 메모리 바이트)을 반환."""
    cpu = os.cpu_count() or 1
    total = 0
    avail = 0
    if _HAVE_PSUTIL:
        try:
            vm = psutil.virtual_memory()
            total = int(vm.total)
            avail = int(vm.available)
        except Exception:  # noqa: BLE001
            total = 0
    if not total:
        t, used, _swap = _read_meminfo()
        total = t
        avail = max(0, t - used)
    return {
        "cpu_count": int(cpu),
        "mem_total_bytes": int(total),
        "mem_avail_bytes": int(avail),
        "have_psutil": _HAVE_PSUTIL,
    }


def recommend_workers(specs: dict, backend: str = "native") -> dict:
    """서버 사양 기반 '권장 동시 스캔 스레드 수'를 계산해 근거와 함께 반환.

    이 스캔의 병목은 CPU 가 아니라 NFS 메타데이터 I/O(scandir/stat) 대기다.
    스레드가 syscall 로 대기하는 동안 GIL 이 풀리므로, native 백엔드는 코어
    수보다 많은 스레드가 '동시 진행 중 RPC 수'를 늘려 처리량을 키운다. 반대로
    du 백엔드는 디렉터리마다 du 프로세스를 띄워 CPU·프로세스 부하가 커서 코어
    수 근처가 적당하다. 실제 최적값은 NAS/네트워크/지연에 따라 달라지므로 이
    값은 '권장 출발점'이며, 대시보드 처리량을 보며 ±조정하는 것을 권한다.
    """
    cores = max(1, int(specs.get("cpu_count") or 1))
    avail_gb = (specs.get("mem_avail_bytes") or 0) / (1024 ** 3)
    hard_cap = 64  # settings 의 scan_workers 상한과 일치
    if backend == "du":
        base = cores * 2
        lo = cores
        hi = cores * 3
        why = ("du 백엔드는 디렉터리마다 du 프로세스를 띄워 CPU·프로세스 부하가 "
               "커서 코어 수 근처가 적당합니다.")
    else:
        base = cores * 4
        lo = max(8, cores * 2)
        hi = cores * 6
        why = ("native 스캔은 NFS 메타데이터 I/O 대기가 대부분이라(대기 중 GIL 이 "
               "풀림) 코어 수보다 많은 스레드가 동시 RPC 를 늘려 더 빨라집니다.")
    # 메모리 가드: 스레드당 약 16MB(스택+버퍼) 여유로 잡아 가용 메모리로 상한을 둔다.
    mem_cap = int(avail_gb * 1024 / 16) if avail_gb > 0 else hard_cap
    mem_cap = max(1, mem_cap)
    eff_cap = min(hard_cap, mem_cap)           # 설정 상한 + 메모리 상한
    lo = max(1, min(lo, eff_cap))
    hi = max(lo, min(hi, eff_cap))
    rec = max(1, min(max(base, lo), hi, eff_cap))   # base 를 [lo, hi]·상한 안으로
    notes = []
    if mem_cap < hard_cap:
        notes.append("가용 메모리가 적어 메모리 기준으로 상한을 %d 로 낮췄습니다." % mem_cap)
    if rec >= hard_cap:
        notes.append("설정 상한(64)에 도달했습니다. 더 높이려면 NAS·네트워크 여력을 "
                     "먼저 확인하세요.")
    notes.append("실제 최적값은 NAS/네트워크에 따라 다릅니다. 이 값으로 시작해 "
                 "처리량(초당 디렉터리)을 보며 조정하세요.")
    return {
        "recommended": int(rec),
        "min": int(max(1, lo)),
        "max": int(min(hi, hard_cap)),
        "backend": backend,
        "cpu_count": cores,
        "mem_total_bytes": int(specs.get("mem_total_bytes") or 0),
        "mem_avail_bytes": int(specs.get("mem_avail_bytes") or 0),
        "rationale": why,
        "notes": notes,
    }

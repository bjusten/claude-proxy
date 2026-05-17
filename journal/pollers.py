"""Pollers for the journal subsystem.

`NvidiaSmiPoller` is a daemon thread that periodically samples GPU state
(by default via `nvidia-smi`) and fires `on_sample` only when the sample
changes since the previous tick. A sampler callable is injected so tests
can drive the poller without shelling out.

`SystemInfoPoller` is the same pattern for host state — uptime, load,
memory, and per-mount disk usage — built on top of `/proc` reads and
`shutil.disk_usage`.
"""

import datetime
import logging
import shutil
import subprocess
import threading


logger = logging.getLogger(__name__)


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat()


NVIDIA_SMI_ARGV = [
    "nvidia-smi",
    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
    "--format=csv,noheader,nounits",
]


def default_nvidia_smi_sampler():
    """Shell out to `nvidia-smi`, parse the CSV, return the gpu list.

    Raises:
        FileNotFoundError: if the `nvidia-smi` binary isn't on PATH.
        subprocess.CalledProcessError: if nvidia-smi exits non-zero.
        ValueError: if the CSV cannot be parsed.
    """
    completed = subprocess.run(
        NVIDIA_SMI_ARGV, capture_output=True, text=True, check=True,
    )
    return parse_nvidia_smi_csv(completed.stdout)


def parse_nvidia_smi_csv(text):
    """Parse `nvidia-smi --format=csv,noheader,nounits` output into dicts.

    Expected field order:
      index, name, utilization.gpu, memory.used, memory.total,
      temperature.gpu, power.draw, power.limit
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 8:
            raise ValueError(f"unexpected field count ({len(parts)}): {raw!r}")
        out.append({
            "index": int(parts[0]),
            "name": parts[1],
            "utilization_gpu": int(parts[2]),
            "memory_used_mib": int(parts[3]),
            "memory_total_mib": int(parts[4]),
            "temperature_c": int(parts[5]),
            "power_draw_w": float(parts[6]),
            "power_limit_w": float(parts[7]),
        })
    return out


class NvidiaSmiPoller:
    def __init__(self, *, sampler, on_sample, interval=5.0, stop_event=None):
        self._sampler = sampler
        self._on_sample = on_sample
        self._interval = interval
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._prev_gpus = None
        self._latest = {}
        self._thread = None

    @property
    def latest(self):
        return self._latest

    def start(self):
        t = threading.Thread(target=self._loop,
                             name="nvidia-smi-poller", daemon=True)
        t.start()
        self._thread = t
        return t

    def _loop(self):
        while not self._stop.is_set():
            try:
                gpus = self._sampler()
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                logger.warning("nvidia-smi unavailable, GPU polling disabled: %s", e)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("nvidia-smi sampler failed: %s", e)
                if self._stop.wait(self._interval):
                    return
                continue
            if gpus != self._prev_gpus:
                self._prev_gpus = gpus
                sample = {"ts": _now_iso(), "gpus": gpus}
                self._latest = sample
                self._on_sample(sample)
            if self._stop.wait(self._interval):
                return


def parse_proc_uptime(text):
    """`/proc/uptime` is `<uptime_seconds> <idle_seconds>`. Return uptime."""
    return float(text.split()[0])


def parse_proc_loadavg(text):
    """`/proc/loadavg` is `1m 5m 15m running/total last_pid`. Return [1m,5m,15m]."""
    fields = text.split()
    return [float(fields[0]), float(fields[1]), float(fields[2])]


def parse_proc_meminfo(text):
    """Parse `/proc/meminfo`. Convert kB → bytes. `used = total - available`."""
    values_kb = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            values_kb[key] = int(parts[0])
        except ValueError:
            continue
    total = values_kb.get("MemTotal", 0) * 1024
    free = values_kb.get("MemFree", 0) * 1024
    available = values_kb.get("MemAvailable", 0) * 1024
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": total - available,
        "free_bytes": free,
    }


def _default_read_text(path):
    with open(path, "r") as f:
        return f.read()


def make_default_system_sampler(*, disk_mounts, disk_usage=None, read_text=None):
    """Build the default sampler used by `SystemInfoPoller`.

    Tracks mounts that have failed once and skips them on subsequent calls
    (logging exactly one WARNING per mount on first failure). `disk_usage`
    and `read_text` are injectable so tests can avoid touching the real
    filesystem.
    """
    disk_usage_fn = disk_usage if disk_usage is not None else shutil.disk_usage
    read_text_fn = read_text if read_text is not None else _default_read_text
    bad_mounts = set()

    def sample():
        uptime = parse_proc_uptime(read_text_fn("/proc/uptime"))
        loadavg = parse_proc_loadavg(read_text_fn("/proc/loadavg"))
        memory = parse_proc_meminfo(read_text_fn("/proc/meminfo"))
        disks = []
        for mount in disk_mounts:
            if mount in bad_mounts:
                continue
            try:
                u = disk_usage_fn(mount)
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.warning("disk mount %s unreadable, omitting: %s", mount, e)
                bad_mounts.add(mount)
                continue
            disks.append({
                "mount": mount,
                "total_bytes": u.total,
                "used_bytes": u.used,
                "free_bytes": u.free,
            })
        return {
            "uptime_seconds": uptime,
            "loadavg": loadavg,
            "memory": memory,
            "disks": disks,
        }

    return sample


class SystemInfoPoller:
    """Daemon-threaded host-state poller. See module docstring."""

    def __init__(self, *, sampler, on_sample, interval=10.0, stop_event=None):
        self._sampler = sampler
        self._on_sample = on_sample
        self._interval = interval
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._prev = None
        self._latest = {}
        self._thread = None

    @property
    def latest(self):
        return self._latest

    def start(self):
        t = threading.Thread(target=self._loop,
                             name="system-info-poller", daemon=True)
        t.start()
        self._thread = t
        return t

    def _loop(self):
        while not self._stop.is_set():
            try:
                sample = self._sampler()
            except Exception as e:  # noqa: BLE001
                logger.warning("system-info sampler failed: %s", e)
                if self._stop.wait(self._interval):
                    return
                continue
            if sample != self._prev:
                self._prev = sample
                stamped = {"ts": _now_iso(), **sample}
                self._latest = stamped
                self._on_sample(stamped)
            if self._stop.wait(self._interval):
                return

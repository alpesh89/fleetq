"""Host resource readings used to keep a worker from starving the machine it shares.

Everything here is best-effort and stdlib-only. Values are fractions in 0..1.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import time

_last_cpu: tuple[float, float] | None = None  # (idle, total) for Linux /proc/stat deltas


def cpu_count() -> int:
    return os.cpu_count() or 1


def load_ratio() -> float:
    """1-minute load average divided by cores. >1 means more runnable work than cores."""
    try:
        return os.getloadavg()[0] / cpu_count()
    except OSError:
        return 0.0


def cpu_busy() -> float | None:
    """Fraction of CPU time busy since the last call (Linux) or right now (macOS). None if unknown."""
    global _last_cpu
    if platform.system() == "Linux":
        try:
            with open("/proc/stat") as f:
                parts = f.readline().split()
            vals = list(map(float, parts[1:]))
            idle, total = vals[3] + vals[4], sum(vals)
            if _last_cpu is None:
                _last_cpu = (idle, total)
                return None
            di, dt = idle - _last_cpu[0], total - _last_cpu[1]
            _last_cpu = (idle, total)
            return max(0.0, min(1.0, 1 - di / dt)) if dt > 0 else None
        except (OSError, IndexError, ValueError):
            return None
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["top", "-l", "1", "-n", "0", "-s", "0"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"CPU usage:\s+([\d.]+)% user,\s+([\d.]+)% sys,\s+([\d.]+)% idle", out)
            if m:
                return max(0.0, min(1.0, 1 - float(m.group(3)) / 100))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def mem_total_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def mem_available_bytes() -> int | None:
    """Memory the OS could hand to us without swapping. None if unknown."""
    sysname = platform.system()
    if sysname == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
    if sysname == "Darwin":
        try:
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+) bytes", out).group(1))

            def pages(name):
                m = re.search(rf"{name}:\s+(\d+)\.", out)
                return int(m.group(1)) if m else 0

            # free + inactive + purgeable + speculative is what macOS can reclaim cheaply
            return (pages("Pages free") + pages("Pages inactive") + pages("Pages purgeable") + pages("Pages speculative")) * page
        except (OSError, AttributeError, ValueError, subprocess.SubprocessError):
            return None
    return None


def mem_available_fraction() -> float | None:
    total, avail = mem_total_bytes(), mem_available_bytes()
    if not total or avail is None:
        return None
    return max(0.0, min(1.0, avail / total))


def snapshot() -> dict:
    avail = mem_available_bytes()
    return {
        "load": round(load_ratio(), 2),
        "cpu_busy": cpu_busy(),
        "mem_total_gb": round(mem_total_bytes() / 1e9, 1),
        "mem_avail_gb": round(avail / 1e9, 1) if avail is not None else None,
        "at": time.time(),
    }

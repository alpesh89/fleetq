"""Per-kind adaptive concurrency: additive increase, multiplicative decrease.

Signals that change the count: host pressure, machine load and queue demand. Error rate,
throttle rate and median latency are tracked for the dashboard only (see tick()).
Decisions are logged with a reason so the dashboard can show *why* a node is running
the number of slots it is.
"""

from __future__ import annotations

import statistics
import time


class SlotController:
    def __init__(self, kind: str, start: int = 4, floor: int = 1, ceiling: int = 32):
        self.kind = kind
        self.floor = max(1, floor)
        self.ceiling = max(self.floor, ceiling)
        self.start = min(max(start, self.floor), self.ceiling)
        self.cur = self.start
        self.inflight = 0
        self.hold_after_cut = 20.0  # seconds after a cut before growing again
        self.last_cut = 0.0
        self.baseline: float | None = None  # EMA of healthy median latency
        self.last_reason = "start"
        self.last_change = time.time()
        self._window: list[tuple[bool, float, bool]] = []
        self.total_ok = 0
        self.total_err = 0
        self.total_throttled = 0

    @property
    def free(self) -> int:
        return max(0, self.cur - self.inflight)

    def record(self, ok: bool, latency: float, throttled: bool = False):
        self._window.append((ok, latency, throttled))
        if throttled:
            self.total_throttled += 1
        elif ok:
            self.total_ok += 1
        else:
            self.total_err += 1

    def tick(self, load_ratio: float, demand: int, pressure: str | None = None) -> str | None:
        """Called every few seconds. `pressure` is the host guard's reason when the machine is
        short of CPU or memory; it forces a cut regardless of task health.
        Returns a reason string when the slot count changes.

        Only signals about *this machine* move the count: host pressure and load. Remote errors,
        push-back (429s, bot walls) and latency are recorded for the dashboard but never cut
        slots: a worker serves dozens of unrelated sites at once, so one slow, broken or angry site
        says nothing about how much the machine can run. Each site is held to its own rps, and a
        failing site is slowed down fleet-wide through its own bucket (Context.site_failed).
        (Cutting on remote errors once starved a 50-site run down to one fetch per machine while two
        thousand pages were due; cutting on push-back halved every worker for one walled site.)
        """
        if pressure:
            self._window.clear()
            if self.cur > self.floor:
                return self._set(max(self.floor, self.cur // 2), pressure)
            return None
        n = len(self._window)
        errs = thr = 0.0
        if n:
            errs = sum(1 for ok, _, th in self._window if not ok and not th) / n
            thr = sum(1 for _, _, th in self._window if th) / n
            p50 = statistics.median(lat for _, lat, _ in self._window)
            self._window.clear()
            if errs < 0.05 and thr < 0.1 and (self.baseline is None or p50 < 1.5 * self.baseline):
                self.baseline = p50 if self.baseline is None else 0.8 * self.baseline + 0.2 * p50
        if load_ratio > 0.9 and self.cur > self.floor:
            return self._set(max(self.floor, self.cur // 2), f"load {load_ratio:.2f}")
        # Grow while the box is cool and there is work waiting. Quick back to the start value after
        # a cut, then by a quarter at a time up to the ceiling. A short hold after a cut stops the
        # count from bouncing every tick.
        if (
            load_ratio < 0.8
            and demand > self.cur
            and self.cur < self.ceiling
            and time.time() - self.last_cut > self.hold_after_cut
        ):
            if self.cur < self.start:
                return self._set(self.start, "recovered, demand")
            return self._set(min(self.ceiling, self.cur + max(1, self.cur // 4)), "healthy, demand")
        return None

    def _set(self, value: int, reason: str) -> str | None:
        if value == self.cur:
            return None
        direction = "up" if value > self.cur else "down"
        if direction == "down":
            self.last_cut = time.time()
        self.cur = value
        self.last_reason = f"{direction}: {reason}"
        self.last_change = time.time()
        return self.last_reason

    def snapshot(self) -> dict:
        return {
            "cur": self.cur,
            "inflight": self.inflight,
            "floor": self.floor,
            "ceiling": self.ceiling,
            "baseline_s": round(self.baseline, 2) if self.baseline else None,
            "reason": self.last_reason,
            "ok": self.total_ok,
            "err": self.total_err,
            "throttled": self.total_throttled,
        }

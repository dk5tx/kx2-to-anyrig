"""Link to the KX2: priority poll scheduler, Auto-Info parsing, guarded writes.

Measured behaviour (CLAUDE.md 3.1): AI comes only once per second, so the VFO and AF gain must be
polled. One query is in flight at a time; replies are matched by command key, everything else is
treated as Auto-Info.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..common import elecraft
from ..common.log import TrafficLog
from ..common.serialport import FrameSplitter, SerialPort
from ..common.state import State

log = logging.getLogger("kx2")

# (command, priority, condition on state or None)
POLL_TABLE: list[tuple[str, str, Callable[[State], bool] | None]] = [
    ("FA", "fast", None),
    ("AG", "fast", None),
    ("IF", "normal", None),          # RIT/XIT, offset, mode, split
    ("BW", "normal", None),          # not reported by AI
    ("MD", "normal", None),
    ("IS", "normal", None),
    ("DS", "normal", None),          # NR / notch / CWT (K31 only, removed otherwise)
    ("FB", "slow", None),
    ("MD$", "slow", None),
    ("FT", "slow", None),
    ("FR", "slow", None),
    ("KS", "slow", None),
    ("RA", "slow", None),
    ("PA", "slow", None),
    ("NB", "slow", None),
    ("GT", "slow", None),
    ("RG", "slow", None),
    ("PC", "slow", None),
    ("IC", "slow", None),            # APF (byte c bit 5), TX TEST
    ("TQ", "slow", None),
    ("AP", "slow", lambda s: s.get("mode") in ("CW", "CW-R")),   # answered only in CW
]

# after writing a value, ignore differing reports for that key for this long [s]
GUARD_POLL = 0.4
GUARD_AI = 1.5


@dataclass
class _PollItem:
    cmd: str
    prio: str
    cond: Callable[[State], bool] | None
    last: float = -1e9
    fails: int = 0
    skip_until: float = 0.0


@dataclass
class _Pending:
    key: str
    deadline: float
    future: asyncio.Future | None = None
    item: _PollItem | None = None


@dataclass
class _Urgent:
    cmd: str
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class KX2Link:
    def __init__(self, cfg: dict, state: State, traffic: TrafficLog | None = None):
        self.cfg = cfg
        self.state = state
        self.traffic = traffic
        self.k31 = bool(cfg.get("k31", True))
        self.intervals = cfg["intervals"]
        self.timeout = float(cfg.get("query_timeout", 0.3))
        self.gap = float(cfg.get("gap", 0.002))
        self.port: SerialPort | None = None
        self.raw_cache: dict[str, str] = {}       # key -> last full frame (without ';')
        self.if_template: str | None = None
        self.frames_rx = 0
        self.polls_sent = 0
        self._splitter = FrameSplitter()
        self._items = [_PollItem(c, p, cond) for c, p, cond in POLL_TABLE if c != "DS" or self.k31]
        self._pending: _Pending | None = None
        self._urgent: collections.deque[_Urgent] = collections.deque()
        self._wake: asyncio.Event | None = None
        self._next_send = 0.0
        self._task: asyncio.Task | None = None
        self._guard: dict[str, tuple[Any, float, float]] = {}
        self._orig_ai: str | None = None
        self._orig_k3: str | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.port = SerialPort(self.cfg["port"], int(self.cfg["baud"]))
        self.port.open()
        self._wake = asyncio.Event()
        self.port.start_reading(self._on_data)
        self._task = asyncio.create_task(self._scheduler(), name="kx2-scheduler")
        self._orig_ai = await self.query("AI;")
        self._orig_k3 = await self.query("K3;")
        log.info("KX2 on %s: AI was %s, K3 mode was %s", self.cfg["port"], self._orig_ai, self._orig_k3)
        self.write("AI2;")
        if self.k31:
            self.write("K31;")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.port and self.port.fd is not None:
            try:
                if self._orig_k3 and self._orig_k3[2:3] in ("0", "1"):
                    self.write("K3%s;" % self._orig_k3[2])
                if self._orig_ai and self._orig_ai[2:3] in ("0", "1", "2"):
                    self.write("AI%s;" % self._orig_ai[2])
            except OSError:
                pass
            self.port.close()

    # ------------------------------------------------------------------ public API
    def write(self, cmd: str, guard: dict[str, Any] | None = None) -> None:
        """Send a set command (must end with ';'). ``guard``: state values just written."""
        assert self.port is not None
        if not cmd.endswith(";"):
            cmd += ";"
        self.port.write(cmd)
        if self.traffic:
            self.traffic.write("KX2<", cmd[:-1])
        now = time.monotonic()
        for k, v in (guard or {}).items():
            self._guard[k] = (v, now + GUARD_POLL, now + GUARD_AI)

    def write_state(self, key: str, value: Any) -> bool:
        """Set a normalized state value on the KX2 (reverse sync). Returns False if not settable."""
        cmd = elecraft.encode_set(key, value)
        if cmd is None:
            return False
        self.write(cmd, {key: value})
        return True

    async def query(self, cmd: str, timeout: float | None = None) -> str | None:
        """Send a query (e.g. 'OM;') with priority over polling. Returns the full reply frame or None."""
        if not cmd.endswith(";"):
            cmd += ";"
        u = _Urgent(cmd)
        self._urgent.append(u)
        assert self._wake is not None
        self._wake.set()
        try:
            return await asyncio.wait_for(asyncio.shield(u.future), (timeout or self.timeout) + 0.5)
        except asyncio.TimeoutError:
            return None

    def stats(self) -> dict[str, Any]:
        return {"frames_rx": self.frames_rx, "polls_sent": self.polls_sent}

    # ------------------------------------------------------------------ receive
    def _on_data(self, data: bytes) -> None:
        for frame in self._splitter.feed(data):
            self._handle(frame)

    def _handle(self, frame: str) -> None:
        frame = frame.lstrip("\r\n")
        if not frame:
            return
        self.frames_rx += 1
        now = time.monotonic()
        if frame == "?":
            if self.traffic:
                self.traffic.write("KX2>", frame)
            if self._pending:
                self._finish(None, failed=True)
            return
        key = elecraft.frame_key(frame)
        payload = frame[len(key):]
        source = "ai"
        if self._pending and self._pending.key == key:
            source = "poll"
            self._finish(frame)
        if self.traffic and source == "ai":
            self.traffic.write("KX2>", frame, "AI")
        self.raw_cache[key] = frame
        if key == "IF" and len(payload) >= 35:
            self.if_template = payload
        values = elecraft.decode(key, payload, k31=self.k31)
        self._apply(values, source, now)

    def _apply(self, values: dict[str, Any], source: str, now: float) -> None:
        for k, v in values.items():
            g = self._guard.get(k)
            if g:
                wanted, until_poll, until_ai = g
                if v == wanted:
                    del self._guard[k]
                elif now < (until_poll if source == "poll" else until_ai):
                    continue           # stale value from before our write
                else:
                    del self._guard[k]
            self.state.set(k, v, "kx2")

    def _finish(self, frame: str | None, failed: bool = False) -> None:
        p = self._pending
        self._pending = None
        self._next_send = time.monotonic() + self.gap
        if p is None:
            return
        if p.item is not None:
            if failed:
                p.item.fails += 1
                if p.item.fails >= 3:
                    p.item.skip_until = time.monotonic() + 30.0
                    p.item.fails = 0
                    log.debug("KX2: %s not answered, retry in 30 s", p.item.cmd)
            else:
                p.item.fails = 0
        if p.future is not None and not p.future.done():
            p.future.set_result(frame)
        if self._wake:
            self._wake.set()

    # ------------------------------------------------------------------ scheduler
    def _next_due(self, now: float) -> tuple[_PollItem | None, float]:
        """Most overdue item relative to its interval, and the time when the next one is due."""
        best, best_score, next_due = None, 0.0, now + 1.0
        for it in self._items:
            if now < it.skip_until or (it.cond and not it.cond(self.state)):
                continue
            iv = float(self.intervals.get(it.prio, 2.0))
            due = it.last + iv
            if due > now:
                next_due = min(next_due, due)
                continue
            score = (now - it.last) / iv
            if best is None or score > best_score:
                best, best_score = it, score
        return best, next_due

    async def _scheduler(self) -> None:
        assert self.port is not None and self._wake is not None
        while True:
            self._wake.clear()          # clear first, so a wake-up during this pass is not lost
            now = time.monotonic()
            wait = 0.05
            if self._pending and now >= self._pending.deadline:
                if self.traffic:
                    self.traffic.write("KX2?", self._pending.key, "timeout")
                self._finish(None, failed=True)
            if self._pending is None:
                if now < self._next_send:
                    wait = self._next_send - now
                elif self._urgent:
                    u = self._urgent.popleft()
                    key = elecraft.frame_key(u.cmd[:-1])
                    self._pending = _Pending(key, now + self.timeout, future=u.future)
                    self.port.write(u.cmd)
                    if self.traffic:
                        self.traffic.write("KX2<", u.cmd[:-1], "query")
                    wait = self.timeout
                else:
                    item, next_due = self._next_due(now)
                    if item is not None:
                        item.last = now
                        self._pending = _Pending(elecraft.frame_key(item.cmd), now + self.timeout, item=item)
                        self.port.write(item.cmd + ";")
                        self.polls_sent += 1
                        wait = self.timeout
                    else:
                        wait = max(0.001, next_due - now)
            else:
                wait = max(0.001, self._pending.deadline - now)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

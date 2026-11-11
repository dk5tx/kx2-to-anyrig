"""CW keyer on a serial key line (DTR), CLAUDE.md 4.7 "method: dtr".

Used instead of the rig's text command (Kenwood KY) when the rig's KEY input is wired to DTR
(author's shack: TS-590SG KEY input set to straight key, RTS = PTT for the amplifier).

Why: KY can only key whole words (measured: the padding of a packet is keyed as a word gap), so
the WinKeyer emulation had to let Win-Test run a word ahead. Keying here character by character
behaves like a real WinKeyer: every character is reported (``on_echo``) when it starts, Win-Test
sends the next one meanwhile. Win-Test's features that rely on this work again: completing a
call that is still being typed, CQ repeat timing, keyboard mode, ++/-- speed changes.

The timing loop runs in its own thread (sleep + short spin for ~1 ms accuracy). Characters are
queued as they come; a space makes the next gap a word gap. ``abort()`` (ESC, safety) releases
the key line at once; ``clear()`` drops what is queued but lets the current character finish.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import threading
import time
from typing import Any, Callable

from ..common import morse

log = logging.getLogger("dtrkeyer")

DEFAULTS = {
    "ptt_lead_ms": 10,          # PTT (RTS) before the first element (Win-Test sends its own value)
    "ptt_tail_ms": 20,          # PTT held after the last element
    "word_gap_units": 7,
    # Win-Test sends the next character only after the echo (~55 ms) plus the network: the queue is
    # often empty during a character or word gap. Only after the gap plus this grace time the
    # message is over: then "done" is reported and PTT dropped (10 Nov 2026: PTT dropped in a
    # word gap after 20 ms tail -> the rig went to RX in the middle of a CQ).
    "idle_grace_ms": 150,
    # When a character is echoed: as late as possible, so Win-Test takes the next character from
    # the entry field late (completing a call typed while sending: DL [Ins] 1XYZ), but early enough
    # that the next character is here before the character gap ends. Budget = Win-Test's ~55 ms +
    # network round trip incl. jitter (WLAN: ~130 ms jitter measured 10 Nov 2026).
    "echo_budget_ms": 220,
    "max_key_down_s": 5.0,      # safety: no element is longer than this
}


class DTRKeyer:
    def __init__(self, line: Any, wpm: Callable[[], float], cfg: dict | None = None,
                 on_done: Callable[[list[Any]], None] | None = None,
                 on_echo: Callable[[str], None] | None = None, traffic: Any = None):
        self.line = line
        self.wpm_fn = wpm
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.on_done = on_done
        self.on_echo = on_echo
        self.traffic = traffic                  # TrafficLog: "KEY>" line per character (timing)
        self.on_echo_ts: Callable[[str, float], None] | None = None
        self.blocked = False
        self.retries = 0                        # interface of CWSender (unused here)
        self.chars_keyed = 0
        self._q: collections.deque = collections.deque()
        self._ids: list[Any] = []
        self._cv = threading.Condition()
        self._abort = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffered_wpm: float | None = None    # WinKeyer buffered speed (++/--)
        self._echo_pending: tuple[float, str] | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if getattr(self.line, "fd", 0) is None:
            self.line.open()
        self._thread = threading.Thread(target=self._run, name="dtr-keyer", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._abort.set()
            self._cv.notify()
        if self._thread:
            await asyncio.get_running_loop().run_in_executor(None, self._thread.join, 2.0)
        self.line.release()
        close = getattr(self.line, "close", None)
        if close:
            close()

    # ------------------------------------------------------------------ API (as CWSender)
    def send(self, text: str, msg_id: Any = None, end: bool = False) -> None:
        if self.blocked:
            log.warning("CW blocked (safety) – dropped %r", text)
            return
        with self._cv:
            self._abort.clear()
            for ch in text.upper():
                if " " <= ch <= "~":                    # printable ASCII only
                    self._q.append(ch)
            if msg_id is not None:
                self._ids.append(msg_id)
            self._cv.notify()

    def set_buffered_wpm(self, wpm: float) -> None:
        """WinKeyer buffered speed change (0 = back to the normal speed), in order with the text."""
        with self._cv:
            self._q.append(("wpm", wpm))
            self._cv.notify()

    def set_ptt_timing(self, lead_ms: float, tail_ms: float) -> None:
        self.cfg["ptt_lead_ms"] = lead_ms
        self.cfg["ptt_tail_ms"] = tail_ms

    def clear(self) -> None:
        """Drop queued text; the character being keyed is finished."""
        with self._cv:
            self._q.clear()
            self._cv.notify()

    def abort(self) -> None:
        """Stop keying at once (ESC, safety)."""
        with self._cv:
            self._q.clear()
            self._abort.set()
            self._cv.notify()
        self.line.key(False)
        self.line.ptt(False)
        self._report_done()

    def on_raw(self, frame: str) -> None:
        pass

    # ------------------------------------------------------------------ helpers
    def _wpm(self) -> float:
        w = self._buffered_wpm or self.wpm_fn() or 25
        return max(5.0, min(60.0, float(w)))

    def _call(self, func, *args) -> None:
        if func and self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(func, *args)

    def _report_done(self) -> None:
        with self._cv:
            ids, self._ids = self._ids, []
        if ids:
            self._call(self.on_done, ids)

    def _echo_out(self, item: str) -> None:
        """Echo from the keying thread; with the thread's time stamp if wanted (timing check)."""
        if self.on_echo_ts:
            self._call(self.on_echo_ts, item, time.monotonic())
        else:
            self._call(self.on_echo, item)

    def _fire_echo(self) -> None:
        if self._echo_pending:
            _, item = self._echo_pending
            self._echo_pending = None
            self._echo_out(item)

    def _sleep_until(self, t: float) -> bool:
        """Precise wait (also sends a due echo); False if aborted."""
        while True:
            if self._abort.is_set():
                return False
            now = time.monotonic()
            if self._echo_pending and now >= self._echo_pending[0]:
                self._fire_echo()
            d = t - now
            if d <= 0:
                return True
            if self._echo_pending:
                d = min(d, max(0.0, self._echo_pending[0] - now) + 0.003)
            if d > 0.003:
                self._abort.wait(d - 0.002)
            else:
                pass                                    # spin for the last ~2 ms

    # ------------------------------------------------------------------ keying thread
    def _run(self) -> None:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(10))
            log.info("DTR keyer runs with real-time priority")
        except (AttributeError, PermissionError, OSError):
            log.debug("no real-time priority for the DTR keyer (normal scheduling)")
        last_end = 0.0               # end of the last element keyed
        next_start = 0.0             # earliest start of the next character
        try:
            while True:
                with self._cv:
                    if self._stop:
                        return
                    item = self._q.popleft() if self._q else None
                    if item is None:
                        now = time.monotonic()
                        unit = 1.2 / self._wpm()
                        done_at = max(last_end + 3 * unit, next_start) \
                            + self.cfg["idle_grace_ms"] / 1000.0
                        tail_at = max(done_at, last_end + self.cfg["ptt_tail_ms"] / 1000.0)
                        if self._ids and now >= done_at:
                            ids, self._ids = self._ids, []
                            self._call(self.on_done, ids)
                        if self.line.ptt_on and now >= tail_at:
                            self.line.ptt(False)
                        wait = 0.5
                        for t in (done_at if self._ids else None, tail_at if self.line.ptt_on else None):
                            if t is not None:
                                wait = min(wait, max(0.001, t - now))
                        self._cv.wait(wait)
                        continue
                if self._abort.is_set():
                    # aborted while this item was taken: drop it, start fresh with the next text
                    self.line.key(False)
                    last_end = next_start = 0.0
                    continue
                if isinstance(item, tuple):                 # ("wpm", n)
                    self._buffered_wpm = float(item[1]) or None
                    continue
                unit = 1.2 / self._wpm()
                if item == " ":
                    # word gap: the next character starts 7 units after the last element
                    next_start = max(next_start, last_end + self.cfg["word_gap_units"] * unit)
                    if self.traffic:
                        self._call(self.traffic.write, "KEY>", "(space)")
                    self._echo_out(item)
                    continue
                code = morse.CODE.get(item)
                if not code:
                    log.debug("no Morse code for %r – skipped", item)
                    self._echo_out(item)
                    continue
                now = time.monotonic()
                start = max(now, next_start)
                if self.line.has_ptt and not self.line.ptt_on:
                    self.line.ptt(True)
                    start = max(start, time.monotonic() + self.cfg["ptt_lead_ms"] / 1000.0)
                if not self._sleep_until(start):
                    continue
                units = morse.char_units(item)
                self._echo_pending = (max(start, start + (units + 3) * unit
                                          - self.cfg["echo_budget_ms"] / 1000.0), item)
                if self.traffic:
                    # "late": the character came after its gap had already ended (waiting for text)
                    late = (start - next_start) * 1000 if next_start and start - next_start < 2 else 0.0
                    self._call(self.traffic.write, "KEY>", item,
                               "%.0f WPM%s" % (self._wpm(), ", late %.0f ms" % late if late > 2 else ""))
                t = time.monotonic()
                ok = True
                for i, el in enumerate(code):
                    if i:
                        t += unit                           # element gap
                        if not self._sleep_until(t):
                            ok = False
                            break
                    length = unit * (1 if el == "." else 3)
                    self.line.key(True)
                    t = max(t, time.monotonic())
                    ok = self._sleep_until(t + min(length, self.cfg["max_key_down_s"]))
                    self.line.key(False)
                    t += length
                    if not ok:
                        break
                if not ok:
                    self._echo_pending = None
                    continue
                self._fire_echo()                           # not yet due only for short chars
                self.chars_keyed += 1
                last_end = t
                next_start = last_end + 3 * unit            # character gap
        except Exception:
            log.exception("DTR keyer failed")
        finally:
            self.line.release()

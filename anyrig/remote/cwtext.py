"""CW text output on the remote rig (CLAUDE.md 4.7), e.g. Kenwood KY.

Measured TS-590SG behaviour (docs/measurements.md, hardware tests 8 and 10 Nov 2026):
- KY packets carry exactly 24 characters (space padded).
- **The padding of a short packet is keyed as a word gap** (10 Nov: "C" + "Q D" + "L1ABC..." gave
  C_Q_D_L with word gaps at both packet edges).
- A space at the **start** of a packet is dropped.
- Full 24-character packets are joined seamlessly.
- KY; answers KY0 (buffer has room) or KY1 (full); KY0; aborts after the current element.
- Rarely a packet is lost (10 Nov 2026: once answered with "E;", once ignored without an answer,
  right after the error). A packet answered with E/O is sent again after a ";" that resets the
  rig's command parser; a packet that should start keying but brings no TX within ``verify_ms``
  (break-in on) is sent again once.

So every packet ends at a word end and the padding *is* the word gap: packets are built from
complete words only (a word is complete when the space after it has arrived). Only a word longer
than a packet is cut, into full 24-character packets. The last word of a message has no space
after it; it is sent when the local side marks the end of the message (``end``), or as a safety
net when no more text came for ``hold_ms``.

"Done" is reported when the text should have been keyed completely (Morse timing at the rig's
keyer speed). That is what Win-Test needs for its busy indicator and CQ repeat; the break-in hang
time of the rig is not waited for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..common import morse
from ..drivers.base import Rig

log = logging.getLogger("cwtext")

DEFAULTS = {
    "method": "ky",
    "packet_len": 24,
    "packet": "KY {text};",            # {text} = packet text padded to packet_len
    "query": "KY;",
    "full_reply": "KY1",
    "free_reply": "KY0",
    "abort": "KY0;",
    "poll_ms": 50,
    "start_delay_ms": 15,               # measured: command -> TX0 11-16 ms
    "lead_ms": 300,                     # ask KY; only if the rig still has this much to key
    "final_lead_ms": 60,
    "word_gap_units": 7,
    "error_replies": ["E", "O"],        # Kenwood: E = communication error, O = overflow
    "verify_ms": 250,                   # no TX this long after the first packet: send it again                # gap the rig keys for the padding (TS-590SG: 6, measured)                # send the marked last word this long before the rig is idle
    # safety net only: no end mark and no text for this long -> send the last word. Must be longer
    # than the local side holds an echo back (up to one word at slow speed; 10 Nov 2026 a 3 s value
    # split "TEST" into "T" + "EST" because the echo of "T" was held for 3.1 s)
    "hold_ms": 15000,
    "allowed": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /?.,=+-'():\"@[_<#>]\\%",
}


def next_packet(text: str, maxlen: int, final: bool) -> tuple[str, str] | None:
    """Next packet from ``text`` (no leading space) and the rest, or None to wait for more text.

    A packet shorter than ``maxlen`` is padded and the rig keys the padding as a word gap, so it
    must end exactly at a word end. ``final``: no more text is expected, the last word is complete.
    """
    t = text.rstrip(" ") + " " if final else text
    best = 0                     # longest run of complete words that leaves room for padding
    for i, ch in enumerate(t):
        if ch == " " and i > 0 and t[i - 1] != " ":
            if i > maxlen - 1:
                break
            best = i
    if best:
        return t[:best], t[best:].lstrip(" ")
    first = t.split(" ", 1)[0]
    if len(first) > maxlen or (len(first) >= maxlen and " " in t):
        return t[:maxlen], t[maxlen:]         # word longer than a packet: full, seamless packet
    return None


class CWSender:
    def __init__(self, rig: Rig, cfg: dict | None = None,
                 on_done: Callable[[list[Any]], None] | None = None):
        self.rig = rig
        self.cfg = dict(DEFAULTS, **(cfg or {}))
        self.on_done = on_done
        self.allowed = set(self.cfg["allowed"])
        self._pending = ""                  # text not yet handed to the rig
        self._ids: list[Any] = []           # message ids waiting for "done"
        self._est_end = 0.0                 # when the rig should have keyed everything sent so far
        self._wake = asyncio.Event()
        self._reply: asyncio.Future | None = None
        self._task: asyncio.Task | None = None
        self._last_padded = True
        self._last_cmd: tuple[str, float] | None = None   # last packet command and send time
        self._retry: asyncio.TimerHandle | None = None
        self.retries = 0
        self._tx_seen = False
        self._end = False
        self._last_input = 0.0
        self.packets: list[str] = []
        self.blocked = False                # set by safety (link lost) until a new connection

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="cw-sender")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------ API
    def wpm(self) -> float:
        return float(self.rig.state.get("keyer_wpm") or 25)

    def send(self, text: str, msg_id: Any = None, end: bool = False) -> None:
        if self.blocked:
            log.warning("CW blocked (safety) – dropped %r", text)
            return
        clean = "".join(ch for ch in text.upper() if ch in self.allowed)
        if clean != text.upper():
            log.debug("dropped unsupported characters from %r", text)
        self._pending += clean
        self._last_input = time.monotonic()
        self._end = end
        if msg_id is not None:
            self._ids.append(msg_id)
        self._wake.set()

    def _schedule_retry(self, delay: float, cmd: str) -> None:
        if self._retry:
            self._retry.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._retry = loop.call_later(delay, self._resend, cmd)

    def _resend(self, cmd: str) -> None:
        self._retry = None
        if self.blocked:
            return
        self.retries += 1
        self.rig.send_raw(";")                          # end whatever the rig's parser holds
        self.rig.send_raw(cmd)
        self._last_cmd = (cmd, time.monotonic())
        self._est_end = max(self._est_end, time.monotonic()) + 0.03

    def _verify_started(self, cmd: str) -> None:
        self._retry = None
        # TX seen since the packet (not the current state: with full break-in a short packet
        # may already be over)
        if self._tx_seen or self.rig.state.get("tx") or self.blocked or not self._ids:
            return
        log.warning("no TX after %r – sending it again", cmd)
        self._resend(cmd)

    def abort(self) -> None:
        if self._retry:
            self._retry.cancel()
            self._retry = None
        self._last_cmd = None
        self._pending = ""
        self.rig.send_raw(self.cfg["abort"])
        self._est_end = 0.0
        self._finish()

    def on_raw(self, frame: str) -> None:
        """Rig frames (driver hook): resolves a pending buffer query, repeats a rejected packet."""
        if frame.startswith("TX"):
            self._tx_seen = True
        if frame in self.cfg["error_replies"] and self._last_cmd \
                and time.monotonic() - self._last_cmd[1] < 0.5:
            cmd = self._last_cmd[0]
            self._last_cmd = None
            log.warning("rig answered %s; to %r – sending it again", frame, cmd)
            self._schedule_retry(0.02, cmd)
            return
        if self._reply and not self._reply.done() and frame.startswith(self.cfg["query"][:2]) \
                and frame in (self.cfg["full_reply"], self.cfg["free_reply"]):
            self._reply.set_result(frame == self.cfg["free_reply"])

    # ------------------------------------------------------------------ internals
    def _finish(self) -> None:
        ids, self._ids = self._ids, []
        if self.on_done:
            self.on_done(ids)

    async def _buffer_free(self) -> bool:
        loop = asyncio.get_running_loop()
        self._reply = loop.create_future()
        self.rig.send_raw(self.cfg["query"])
        try:
            return await asyncio.wait_for(self._reply, 0.3)
        except asyncio.TimeoutError:
            return True                     # no answer: do not block CW forever

    async def _run(self) -> None:
        maxlen = int(self.cfg["packet_len"])
        poll = self.cfg["poll_ms"] / 1000.0
        lead = self.cfg["lead_ms"] / 1000.0
        hold = self.cfg["hold_ms"] / 1000.0
        while True:
            now = time.monotonic()
            # the rig drops a space at the start of a packet
            self._pending = self._pending.lstrip(" ")
            if self._pending:
                # the marked last word is held until the rig needs it: in keyboard mode more
                # characters of the same word may still follow
                final_lead = self.cfg["final_lead_ms"] / 1000.0
                final = (self._end and now >= self._est_end - final_lead) \
                    or now - self._last_input >= hold
                nxt = next_packet(self._pending, maxlen, final)
                if nxt is None:
                    wake = self._last_input + hold
                    if self._end:
                        wake = min(wake, self._est_end - final_lead)
                    await self._wait(max(0.002, wake - now))
                    continue
                if self.packets and now < self._est_end - lead and not await self._buffer_free():
                    await asyncio.sleep(poll)
                    continue
                chunk, self._pending = nxt
                if chunk:
                    cmd = self.cfg["packet"].format(text=chunk.ljust(maxlen))
                    idle = not self.rig.state.get("tx") and self._est_end <= now
                    if idle:
                        self._tx_seen = False
                    self.rig.send_raw(cmd)
                    self._last_cmd = (cmd, now)
                    if idle and self.rig.state.get("vox") and self.cfg.get("verify_ms"):
                        # break-in on: the rig must go to TX within a few ms (measured 11-16 ms)
                        if self._retry:
                            self._retry.cancel()
                        self._retry = asyncio.get_running_loop().call_later(
                            self.cfg["verify_ms"] / 1000.0, self._verify_started, cmd)
                    self.packets.append(chunk)
                    unit = 1.2 / self.wpm()
                    start = max(now + self.cfg["start_delay_ms"] / 1000.0,
                                self._est_end + (self.cfg["word_gap_units"] if self._last_padded else 3) * unit
                                if self._est_end > now else 0.0)
                    self._last_padded = len(chunk) < maxlen
                    self._est_end = start + morse.duration(chunk, self.wpm())
                continue
            if self._ids and now >= self._est_end:
                self._finish()
                continue
            await self._wait(max(0.005, self._est_end - now) if self._ids else None)

    async def _wait(self, timeout: float | None) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass

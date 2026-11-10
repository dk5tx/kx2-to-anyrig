"""CW text output on the remote rig (CLAUDE.md 4.7), e.g. Kenwood KY.

Measured TS-590SG behaviour (docs/measurements.md):
- KY packets carry exactly 24 characters (space padded); padding is not keyed.
- A space at the **start** of a packet is dropped -> never let a packet start with a space;
  cut inside a word instead (packets are joined seamlessly). A trailing space is kept.
- KY; answers KY0 (buffer has room) or KY1 (full); the buffer holds more than one packet.
- KY0; aborts: the current element is finished, nothing more is keyed.

Text arrives in pieces (Win-Test sends one character at a time). Packets are therefore only
cut **inside a word**: the last character(s) are held back until the next text arrives, so a
word gap always lies inside a packet. A piece that cannot be cut that way is sent when no more
text came for ``hold_ms`` or when the rig is about to run out of text.

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
    "lead_ms": 300,                     # hand the next packet to the rig this long before it runs dry
    "hold_ms": 150,                     # no more text for this long: send the rest as it is
    "allowed": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /?.,=+-'():\"@[_<#>]\\%",
}


def inner_cut(text: str, maxlen: int) -> int:
    """Largest p <= maxlen, p < len(text), with text[:p] / text[p:] cut inside a word; 0 if none."""
    for p in range(min(maxlen, len(text) - 1), 0, -1):
        if text[p] != " " and text[p - 1] != " ":
            return p
    return 0


def split_packet(text: str, maxlen: int) -> tuple[str, str]:
    """Next packet and the rest.

    Packets are cut **inside a word**: neither the packet ends with a space nor does the rest start
    with one. Measured: a leading space is dropped by the rig; how trailing spaces next to the
    padding are treated is not known, so word gaps are never put at a packet edge.
    """
    if len(text) <= maxlen:
        return text, ""
    for p in range(maxlen, 1, -1):
        if text[p] != " " and text[p - 1] != " ":
            return text[:p], text[p:]
    # a single "word" longer than the packet: cut where the rest does not start with a space
    for p in range(maxlen, 0, -1):
        if text[p] != " ":
            return text[:p], text[p:]
    return text[:maxlen], text[maxlen:].lstrip(" ")


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
        self._last_char_sent = ""
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

    def send(self, text: str, msg_id: Any = None) -> None:
        if self.blocked:
            log.warning("CW blocked (safety) – dropped %r", text)
            return
        clean = "".join(ch for ch in text.upper() if ch in self.allowed)
        if clean != text.upper():
            log.debug("dropped unsupported characters from %r", text)
        if not self._pending and clean.startswith(" ") and self._last_char_sent not in ("", " ") \
                and time.monotonic() < self._est_end:
            # a word gap that arrives after the previous word was already sent would be dropped
            # by the rig at the start of a packet; nothing better can be done than to log it
            log.info("word gap at packet start will be lost (text typed after the previous word)")
        self._pending += clean
        self._last_input = time.monotonic()
        if msg_id is not None:
            self._ids.append(msg_id)
        self._wake.set()

    def abort(self) -> None:
        self._pending = ""
        self.rig.send_raw(self.cfg["abort"])
        self._est_end = 0.0
        self._finish()

    def on_raw(self, frame: str) -> None:
        """Rig frames (driver hook): resolves a pending buffer query."""
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
                text = self._pending
                needed = now >= self._est_end - lead          # rig runs dry soon (or is idle)
                if len(text) > maxlen:
                    chunk, rest = split_packet(text, maxlen)
                elif needed and inner_cut(text, maxlen):
                    p = inner_cut(text, maxlen)
                    chunk, rest = text[:p], text[p:]          # keep the tail for the next piece
                elif now - self._last_input >= hold:
                    chunk, rest = text.rstrip(" "), ""       # no more text coming: send it all
                else:
                    wake = self._last_input + hold
                    if not needed:
                        wake = min(wake, self._est_end - lead)
                    await self._wait(max(0.002, wake - now))
                    continue
                if self.packets and now < self._est_end - lead and not await self._buffer_free():
                    await asyncio.sleep(poll)
                    continue
                self._pending = rest
                if chunk:
                    self.rig.send_raw(self.cfg["packet"].format(text=chunk.ljust(maxlen)))
                    self.packets.append(chunk)
                    continuing = self._est_end > now
                    start = max(now + self.cfg["start_delay_ms"] / 1000.0, self._est_end)
                    # packets are joined seamlessly: a character gap unless the last one ended a word
                    gap = 3 * 1.2 / self.wpm() if continuing and self._last_char_sent not in ("", " ") else 0
                    self._last_char_sent = chunk[-1]
                    self._est_end = start + gap + morse.duration(chunk, self.wpm())
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

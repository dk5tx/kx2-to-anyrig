"""K1EL WinKeyer (WK2) emulation for Win-Test on a pseudo terminal (CLAUDE.md 4.6).

Win-Test talks to this as it would to a real WinKeyer on COM2. Text is forwarded to the remote rig
(keyed there by its internal keyer, Kenwood KY), the speed goes to the rig's keyer, "clear buffer"
(ESC in Win-Test) aborts at once. Status bytes tell Win-Test when the keyer is busy.

Measured (Win-Test, 10 Nov 2026): with serial echo on (mode register 0x94/0x95) Win-Test sends a
message **one character at a time** and sends the next one only after the echo of the previous
one (about 55 ms later). It treats the message as sent when the last echo has come; a CQ repeat
then starts (with "clear buffer" first) after the repeat interval.

Pacing of the echo: the remote side can only key complete words (KY padding = word gap), so
Win-Test must stay ahead of the keying, but not much more, or it thinks the message is over too
early. The characters of word k are echoed once word k-1 is (estimated to be) keyed at the rig;
the first two words and every space are echoed at once. The estimate uses the same Morse timing
as the remote side. "Busy" stays set until the remote reports the text as keyed.

Every byte from and to Win-Test is written to the traffic log, so the exact command set Win-Test
uses can be checked against a real hardware test.

Protocol reference: K1EL WinKeyer2 v2.3 interface description.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from ..common import morse
from ..common.log import TrafficLog
from ..common.serialport import PtyPort

log = logging.getLogger("winkeyer")

VERSION = 23                     # reported firmware: WK2 v2.3

# immediate commands: byte -> number of parameter bytes
PARAMS = {
    0x01: 1,   # sidetone control
    0x02: 1,   # set WPM speed
    0x03: 1,   # weighting
    0x04: 2,   # PTT lead-in / tail
    0x05: 3,   # speed pot setup
    0x06: 1,   # set pause state
    0x07: 0,   # get speed pot
    0x08: 0,   # backspace
    0x09: 1,   # pin configuration
    0x0A: 0,   # clear buffer
    0x0B: 1,   # key immediate
    0x0C: 1,   # HSCW
    0x0D: 1,   # Farnsworth
    0x0E: 1,   # WinKeyer mode register
    0x0F: 15,  # load defaults
    0x10: 1,   # first extension
    0x11: 1,   # key compensation
    0x12: 1,   # paddle switchpoint
    0x13: 0,   # null command
    0x14: 1,   # software paddle
    0x15: 0,   # request status
    0x16: 1,   # pointer command (+1 for sub-command 0x03)
    0x17: 1,   # dit/dah ratio
    # buffered commands (part of the text stream)
    0x18: 1,   # PTT on/off
    0x19: 1,   # key buffered
    0x1A: 1,   # wait
    0x1B: 2,   # merge letters (prosign)
    0x1C: 1,   # change speed buffered
    0x1D: 1,   # HSCW speed buffered
    0x1E: 0,   # cancel buffered speed
    0x1F: 0,   # buffered NOP
}
NAMES = {
    0x01: "sidetone", 0x02: "speed", 0x03: "weight", 0x04: "ptt_timing", 0x05: "pot_setup",
    0x06: "pause", 0x07: "get_pot", 0x08: "backspace", 0x09: "pinconfig", 0x0A: "clear",
    0x0B: "key_immediate", 0x0C: "hscw", 0x0D: "farnsworth", 0x0E: "mode", 0x0F: "load_defaults",
    0x10: "ext1", 0x11: "keycomp", 0x12: "switchpoint", 0x13: "null", 0x14: "sw_paddle",
    0x15: "status", 0x16: "pointer", 0x17: "ratio", 0x18: "ptt_buf", 0x19: "key_buf", 0x1A: "wait",
    0x1B: "merge", 0x1C: "speed_buf", 0x1D: "hscw_buf", 0x1E: "cancel_speed", 0x1F: "nop",
}
ADMIN_PARAMS = {0x04: 1}          # admin sub-commands with an extra byte (echo test)

# merged letters (WinKeyer prosigns) -> single prosign characters of the rig (TS-590SG KY)
MERGE_TO_PROSIGN = {"AR": "_", "SK": ">", "BT": "[", "KN": "]", "AS": "<", "BK": "\\", "SN": "%", "VA": ">"}

STATUS_BASE = 0xC0
ST_XOFF = 0x01
ST_BUSY = 0x04


class WinKeyerEmulator:
    def __init__(self, link: str | None, on_text: Callable[[str], None],
                 on_abort: Callable[[], None], on_speed: Callable[[int], None],
                 traffic: TrafficLog | None = None, flush_ms: float = 20.0,
                 on_end: Callable[[str], None] | None = None, end_ms: float = 150.0):
        self.port = PtyPort(link)
        self.on_text = on_text
        self.on_abort = on_abort
        self.on_speed = on_speed
        self.traffic = traffic
        self.flush_delay = flush_ms / 1000.0
        self.on_end = on_end                # on_end(text): last text of a message (may be "")
        self.end_delay = end_ms / 1000.0
        self._end_timer: asyncio.TimerHandle | None = None
        self.open = False
        self.wpm = 0
        self.mode = 0
        self.busy = False
        self.echo_enabled = False
        self._buf = bytearray()
        self._text = ""
        self._flush: asyncio.TimerHandle | None = None
        # echo pacing: complete words [(text, complete_time)], word being received, held echo
        self.latency = 0.05             # estimated time from complete word to keying start [s]
        self.margin = 0.3               # echo the next word this long before the previous starts
        self._words: list[tuple[str, float]] = []
        self._cur = ""
        self._cur_since = 0.0
        self._held: asyncio.TimerHandle | None = None
        self.commands_seen: dict[str, int] = {}

    @property
    def device(self) -> str | None:
        return self.port.slave_name

    def start(self) -> None:
        self.port.open()
        self.port.start_reading(self._on_data)
        log.info("Win-Test WinKeyer port: %s -> %s", self.port.link, self.port.slave_name)

    async def stop(self) -> None:
        self.port.close()

    # ------------------------------------------------------------------ output to Win-Test
    def _send(self, data: bytes, note: str = "") -> None:
        if self.traffic:
            self.traffic.write("WK<", " ".join("%02X" % b for b in data), note)
        try:
            self.port.write(data)
        except OSError:
            pass

    def _status(self) -> None:
        self._send(bytes([STATUS_BASE | (ST_BUSY if self.busy else 0)]), "status")

    def set_busy(self, busy: bool) -> None:
        if busy != self.busy:
            self.busy = busy
            if self.open:
                self._status()

    # ------------------------------------------------------------------ input from Win-Test
    def _on_data(self, data: bytes) -> None:
        if self.traffic:
            self.traffic.write("WK>", " ".join("%02X" % b for b in data),
                               "".join(chr(b) if 32 <= b < 127 else "." for b in data))
        self._buf += data
        self._parse()

    def _seen(self, name: str) -> None:
        self.commands_seen[name] = self.commands_seen.get(name, 0) + 1

    def _parse(self) -> None:
        buf = self._buf
        while buf:
            b = buf[0]
            if b == 0x00:                               # admin command
                if len(buf) < 2:
                    return
                sub = buf[1]
                extra = ADMIN_PARAMS.get(sub, 0)
                if len(buf) < 2 + extra:
                    return
                args = bytes(buf[2:2 + extra])
                del buf[:2 + extra]
                self._admin(sub, args)
                continue
            if b < 0x20:
                n = PARAMS.get(b, 0)
                if b == 0x16 and len(buf) >= 2 and buf[1] == 0x03:
                    n = 2
                if len(buf) < 1 + n:
                    return
                args = bytes(buf[1:1 + n])
                del buf[:1 + n]
                self._command(b, args)
                continue
            # text
            del buf[:1]
            self._add_text(chr(b))

    def _admin(self, sub: int, args: bytes) -> None:
        self._seen("admin_%02X" % sub)
        if sub == 0x02:                                 # host open
            self.open = True
            log.info("Win-Test opened the WinKeyer (host open)")
            self._send(bytes([VERSION]), "version")
        elif sub == 0x03:                               # host close
            self.open = False
            log.info("Win-Test closed the WinKeyer (host close)")
        elif sub == 0x04:                               # echo test
            self._send(args, "echo test")
        elif sub == 0x07:                               # get values: 15 bytes of defaults
            self._send(bytes([0x00, self.wpm or 25, 0x05, 0x32, 0x00, 0x00, 0x05, 0x14, 0x00, 0x00,
                              0x00, 0x00, 0x00, 0x00, 0x00]), "values")
        elif sub == 0x09:                               # firmware major revision (WK3 style)
            self._send(bytes([VERSION]), "fw rev")
        else:
            log.debug("admin 0x%02X ignored", sub)

    def _command(self, cmd: int, args: bytes) -> None:
        name = NAMES.get(cmd, "cmd_%02X" % cmd)
        self._seen(name)
        if cmd == 0x02:                                 # speed
            wpm = args[0]
            if wpm and wpm != self.wpm:
                self.wpm = wpm
                self.on_speed(wpm)
        elif cmd == 0x0A:                               # clear buffer = abort (ESC)
            self._text = ""
            self._reset_words()
            if self._end_timer:
                self._end_timer.cancel()
                self._end_timer = None
            if self._flush:
                self._flush.cancel()
            self.on_abort()
            self.set_busy(False)
        elif cmd == 0x0E:                               # mode register
            self.mode = args[0]
            self.echo_enabled = bool(self.mode & 0x04)  # bit 2: serial echo back
            log.info("WinKeyer mode register 0x%02X (echo %s)", self.mode,
                     "on" if self.echo_enabled else "off")
        elif cmd == 0x0F:                               # load defaults (byte 2 = speed)
            if len(args) > 1 and args[1]:
                self.wpm = args[1]
                self.on_speed(self.wpm)
            if args:
                self.mode = args[0]
                self.echo_enabled = bool(self.mode & 0x04)
        elif cmd == 0x15:                               # request status
            self._status()
        elif cmd == 0x07:                               # speed pot: report "no pot" (0)
            self._send(bytes([0x80]), "speed pot")
        elif cmd == 0x1B:                               # merge letters -> prosign character
            pair = (chr(args[0]) + chr(args[1])).upper()
            self._add_text(MERGE_TO_PROSIGN.get(pair, pair), echo=pair)
        elif cmd == 0x08:                               # backspace: remove last unsent char
            self._text = self._text[:-1]
        elif cmd == 0x1C:
            log.info("buffered speed change to %d WPM ignored (not possible with KY)", args[0])
        else:
            log.debug("WinKeyer command %s %s accepted", name, args.hex())

    def _add_text(self, ch: str, echo: str | None = None) -> None:
        if self._end_timer:
            self._end_timer.cancel()
            self._end_timer = None
        self._text += ch.upper()
        self.set_busy(True)
        if self.echo_enabled:                           # Win-Test waits for it before the next char
            self._echo_paced(ch, (echo if echo is not None else ch).encode("latin-1"))
        if self._flush:
            self._flush.cancel()
        loop = asyncio.get_running_loop()
        self._flush = loop.call_later(self.flush_delay, self._flush_text)

    def _flush_text(self, end: bool = False) -> None:
        text, self._text = self._text, ""
        if (end or not self.echo_enabled) and self.on_end:   # without echo: text comes in one go
            self.on_end(text)
        elif text:
            self.on_text(text)

    # ------------------------------------------------------------------ echo pacing
    def _reset_words(self) -> None:
        self._words.clear()
        self._cur = ""
        if self._held:
            self._held.cancel()
            self._held = None

    def word_starts(self) -> list[float]:
        """Estimated keying start of every complete word (same model as remote/cwtext.py)."""
        unit = 1.2 / (self.wpm or 25)
        starts, end = [], 0.0
        for text, complete in self._words:
            start = max(complete + self.latency, end + 7 * unit if end else 0.0)
            starts.append(start)
            end = start + morse.duration(text, self.wpm or 25)
        return starts

    def _echo_paced(self, ch: str, data: bytes) -> None:
        now = time.monotonic()
        if self._words and self._words[-1][1] < now - 5.0 and not self.busy:
            self._words.clear()                         # a new message long after the last one
        if ch == " ":
            if self._cur:
                self._words.append((self._cur, now))
                self._cur = ""
            self._echo_now(data)
            return
        if not self._cur:
            self._cur_since = now
        self._cur += ch
        k = len(self._words)                            # index of the word being received
        due = 0.0
        if k >= 2:
            due = self.word_starts()[k - 1] - self.margin
        if due <= now:
            self._echo_now(data)
        else:
            loop = asyncio.get_running_loop()
            self._held = loop.call_later(due - now, self._release, data)

    def _release(self, data: bytes) -> None:
        self._held = None
        self._echo_now(data)

    def _echo_now(self, data: bytes) -> None:
        """Send an echo; if Win-Test sends no further character, the message is complete."""
        self._send(data, "echo")
        if self.on_end:
            if self._end_timer:
                self._end_timer.cancel()
            self._end_timer = asyncio.get_running_loop().call_later(self.end_delay, self._message_end)

    def _message_end(self) -> None:
        self._end_timer = None
        if self._flush:
            self._flush.cancel()
        self._flush_text(end=True)

    def done(self) -> None:
        """Remote finished keying everything we sent."""
        if not self._text:
            self.set_busy(False)

"""K1EL WinKeyer (WK2) emulation for Win-Test on a pseudo terminal (CLAUDE.md 4.6).

Win-Test talks to this as it would to a real WinKeyer on COM2. Text is forwarded to the remote rig
(keyed there by its internal keyer, Kenwood KY), the speed goes to the rig's keyer, "clear buffer"
(ESC in Win-Test) aborts at once. Status bytes tell Win-Test when the keyer is busy.

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
                 traffic: TrafficLog | None = None, flush_ms: float = 20.0):
        self.port = PtyPort(link)
        self.on_text = on_text
        self.on_abort = on_abort
        self.on_speed = on_speed
        self.traffic = traffic
        self.flush_delay = flush_ms / 1000.0
        self.open = False
        self.wpm = 0
        self.mode = 0
        self.busy = False
        self.echo_enabled = False
        self._buf = bytearray()
        self._text = ""
        self._flush: asyncio.TimerHandle | None = None
        self._echo_task: asyncio.Task | None = None
        self._echo_queue: list[str] = []
        self.commands_seen: dict[str, int] = {}

    @property
    def device(self) -> str | None:
        return self.port.slave_name

    def start(self) -> None:
        self.port.open()
        self.port.start_reading(self._on_data)
        log.info("Win-Test WinKeyer port: %s -> %s", self.port.link, self.port.slave_name)

    async def stop(self) -> None:
        if self._echo_task:
            self._echo_task.cancel()
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
            self._echo_queue.clear()
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
            self._add_text(MERGE_TO_PROSIGN.get(pair, pair))
        elif cmd == 0x08:                               # backspace: remove last unsent char
            self._text = self._text[:-1]
        elif cmd == 0x1C:
            log.info("buffered speed change to %d WPM ignored (not possible with KY)", args[0])
        else:
            log.debug("WinKeyer command %s %s accepted", name, args.hex())

    def _add_text(self, ch: str) -> None:
        self._text += ch.upper()
        self.set_busy(True)
        if self._flush:
            self._flush.cancel()
        loop = asyncio.get_running_loop()
        self._flush = loop.call_later(self.flush_delay, self._flush_text)

    def _flush_text(self) -> None:
        text, self._text = self._text, ""
        if text:
            self.on_text(text)
            if self.echo_enabled:
                self._echo(text)

    # ------------------------------------------------------------------ echo (estimated timing)
    def _echo(self, text: str) -> None:
        self._echo_queue.append(text)
        if not self._echo_task or self._echo_task.done():
            self._echo_task = asyncio.create_task(self._echo_run())

    async def _echo_run(self) -> None:
        start = time.monotonic()
        t = 0.0
        while self._echo_queue:
            text = self._echo_queue.pop(0)
            for ch in text:
                t += morse.duration(ch, self.wpm or 25) + 3 * 1.2 / (self.wpm or 25)
                await asyncio.sleep(max(0.0, start + t - time.monotonic()))
                if not self.busy:
                    return
                self._send(ch.encode("latin-1"), "echo")

    def done(self) -> None:
        """Remote finished keying everything we sent."""
        if not self._text:
            self.set_busy(False)

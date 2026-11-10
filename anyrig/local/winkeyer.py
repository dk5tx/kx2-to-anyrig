"""K1EL WinKeyer (WK2) emulation for Win-Test on a pseudo terminal (CLAUDE.md 4.6).

Win-Test talks to this as it would to a real WinKeyer on COM2. Text is forwarded to the remote rig
(keyed there by its internal keyer, Kenwood KY), the speed goes to the rig's keyer, "clear buffer"
(ESC in Win-Test) aborts at once. Status bytes tell Win-Test when the keyer is busy.

Measured (Win-Test, 10 Nov 2026): with serial echo on (mode register 0x94/0x95) Win-Test sends a
message **one character at a time** and sends the next one only after the echo of the previous
one (about 55 ms later). It treats the message as sent when the last echo has come; a CQ repeat
then starts (with "clear buffer" first) after the repeat interval.

Pacing of the echo: the remote side can only key complete words (KY padding = word gap), so
Win-Test must stay ahead of the keying, but not much more. The characters of word k are echoed
from ``margin`` before the end of word k-1 at the rig (estimate with the same Morse timing as the
remote side); the first word and every space are echoed at once. "Busy" stays set until the
remote reports the text as keyed.

Win-Test therefore sees a message as sent about one word before the rig has keyed it. Measured
(10 Nov 2026), Win-Test sends "clear buffer" plus new text in one write whenever it believes the
keyer is idle: at a CQ repeat (after the repeat interval) and in keyboard mode (Alt+K) before
each typed character. A real ESC is a "clear buffer" without text. So "clear + text" while the
rig is still keying is not an abort:
- coming shortly after the last echo (typing in keyboard mode): the text is appended;
- coming later (CQ repeat): the new message waits until the rig has finished, plus the same pause
  Win-Test waited after the last echo, so the repeat interval is kept.
"Clear + text" while Win-Test is still sending (an echo outstanding) is an interruption, e.g.
another F key: abort and start the new message, as a real WinKeyer does.

Remote keyer mode (``remote_echo``, remote ``cw.method: dtr``): the remote keys character by
character and reports each character when it starts (``echo()``), exactly what a real WinKeyer
does. Text goes out at once, "clear + text" drops what is queued but finishes the character being
keyed, buffered speed (++/--) and PTT timing are passed on. None of the pacing above is needed.

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
                 on_end: Callable[[str], None] | None = None, end_ms: float = 150.0,
                 cq_repeat_s: float | None = None,
                 on_clear: Callable[[], None] | None = None,
                 on_wpm: Callable[[int], None] | None = None,
                 on_ptt: Callable[[int, int], None] | None = None):
        self.port = PtyPort(link)
        self.on_text = on_text
        self.on_abort = on_abort
        self.on_speed = on_speed
        self.traffic = traffic
        self.flush_delay = flush_ms / 1000.0
        self.on_end = on_end                # on_end(text): last text of a message (may be "")
        self.end_delay = end_ms / 1000.0         # after an echo at macro pace
        self.end_delay_first = 0.35             # after the first character of a message
        self.end_delay_typing = 0.8             # keyboard mode: keys come slower than 80 ms
        self.cq_repeat_s = cq_repeat_s           # configured Win-Test repeat interval (optional)
        self.remote_echo = False                # remote keyer reports characters (DTR keying)
        self.on_clear = on_clear
        self.on_wpm = on_wpm
        self.on_ptt = on_ptt
        self.word_gap_units = 6                 # TS-590SG keys 6 units between words (measured)
        self.repeat_comp = 0.2                  # first word must arrive before keying starts
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
        self.margin = 0.6               # echo the next word this long before the previous one ends
        self.defer_s = 1.5              # clear+text this long after the last echo while busy: repeat
        self._typing = False            # this message comes slower than macro pace
        self._chars = 0                 # characters of the current message
        self._done_at = 0.0             # when the remote reported the last message as keyed
        self._prev_pause: float | None = None
        self._aborted = False           # last message ended by ESC
        self._msg_first: str | None = None      # first character of the last message
        self._last_echo = 0.0
        self._complete = False          # Win-Test has nothing more to send (end mark sent)
        self._defer: float | None = None        # pause of a deferred CQ repeat [s]
        self._defer_text: list[tuple[str, str | None]] = []
        self._defer_timer: asyncio.TimerHandle | None = None
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
        for h in (self._held, self._end_timer, self._defer_timer, self._flush):
            if h:
                h.cancel()
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
                if b == 0x0A:                           # clear: does text follow in this write?
                    first = next((chr(x) for x in buf[1:] if x >= 0x20), None)
                    self._clear(first is not None, first)
                    del buf[:1]
                    continue
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
        elif cmd == 0x0A:
            self._clear(False)
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
        elif cmd in (0x1C, 0x1E):                       # buffered speed (++/--), cancel
            wpm = args[0] if cmd == 0x1C else 0
            if self.remote_echo and self.on_wpm:
                self._flush_now()
                self.on_wpm(wpm)
            else:
                log.info("buffered speed change ignored (not possible with KY)")
        elif cmd == 0x04:                               # PTT lead-in / tail, 10 ms units
            if self.on_ptt:
                self.on_ptt(args[0] * 10, args[1] * 10)
        else:
            log.debug("WinKeyer command %s %s accepted", name, args.hex())

    def _clear(self, text_follows: bool, first: str | None = None) -> None:
        """WinKeyer "clear buffer": ESC, or Win-Test starting a new message (see module doc)."""
        self._seen("clear")
        if self.remote_echo:
            if text_follows and self.on_clear:
                self._text = ""
                self.on_clear()                         # new message: queue dropped, char finished
            else:
                self._abort()
            return
        now = time.monotonic()
        pause = now - self._last_echo
        if not text_follows or self._held is not None:
            # ESC, or Win-Test gave up a message it was still sending (another F key)
            self._abort()
            return
        # a repeat sends the same message again; never right after an ESC
        repeat = self._is_repeat(pause) and not self._aborted and first == self._msg_first
        self._aborted = False
        self._msg_first = first
        if self._defer is not None:
            return                                      # already waiting for the rig
        if self.busy:
            if repeat or pause >= self.defer_s:
                log.info("CQ repeat while the rig is still keying: starts %.1f s after the end", pause)
                self._defer = max(0.0, pause - self.repeat_comp)
                self._defer_text = []
            else:
                log.debug("clear+text %.2f s after the last echo: appended (keyboard mode)", pause)
                self._complete = False
            return
        # rig (estimated to be) idle: a new message. No KY0, it could cut the last element of
        # the previous message if the estimate was a little early.
        self._abort(send=False)
        if repeat and self._done_at > self._last_echo:
            # CQ repeat: Win-Test counted its interval from the last echo, which came
            # (done - last echo) before the rig finished -> wait that much longer
            wait = self._done_at + pause - self.repeat_comp - now
            if wait > 0.02:
                log.info("CQ repeat: waiting %.1f s more to keep the interval", wait)
                self._defer = 0.0
                self._defer_text = []
                self._defer_timer = asyncio.get_running_loop().call_later(wait, self._start_deferred)
                self.set_busy(True)

    def _is_repeat(self, pause: float) -> bool:
        """A CQ repeat comes the same time after the last echo every time."""
        prev = self._prev_pause
        self._prev_pause = pause
        if self.cq_repeat_s and abs(pause - self.cq_repeat_s) < 0.3:
            return True
        return prev is not None and pause >= 0.8 and abs(pause - prev) < 0.25

    def _abort(self, send: bool = True) -> None:
        if send:                                        # ESC: what follows is no CQ repeat
            self._aborted = True
            self._prev_pause = None
        self._text = ""
        self._reset_words()
        self._complete = False
        self._chars = 0
        self._typing = False
        self._defer = None
        self._defer_text = []
        if self._defer_timer:
            self._defer_timer.cancel()
            self._defer_timer = None
        if self._end_timer:
            self._end_timer.cancel()
            self._end_timer = None
        if self._flush:
            self._flush.cancel()
        if send:
            self.on_abort()
        self.set_busy(False)

    def _start_deferred(self) -> None:
        self._defer_timer = None
        chars, self._defer_text = self._defer_text, []
        self._defer = None
        self._reset_words()
        for ch, echo in chars:
            self._add_text(ch, echo)

    def _flush_now(self) -> None:
        if self._flush:
            self._flush.cancel()
            self._flush = None
        if self._text:
            text, self._text = self._text, ""
            self.on_text(text)

    def set_remote_echo(self, on: bool) -> None:
        if on != self.remote_echo:
            log.info("WinKeyer echo from the remote keyer: %s", "on" if on else "off")
        self.remote_echo = on

    def echo(self, ch: str, note: str = "") -> None:
        """Remote keyer reports a character (remote_echo mode)."""
        if not self.echo_enabled:
            return
        rev = {v: k for k, v in MERGE_TO_PROSIGN.items() if k != "VA"}
        self._send(rev.get(ch, ch).encode("latin-1"), ("echo " + note).strip())
        self._last_echo = time.monotonic()

    def _add_text(self, ch: str, echo: str | None = None) -> None:
        if self.remote_echo:                            # remote keys char by char and echoes
            self._text += ch.upper()
            self.set_busy(True)
            self._flush_now()
            return
        if self._defer is not None:                     # CQ repeat waits for the rig
            self._defer_text.append((ch, echo))
            return
        self._complete = False
        now = time.monotonic()
        if self._chars and now - self._last_echo > 0.08:
            self._typing = True                         # slower than Win-Test's macro pace
        self._chars += 1
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
            start = max(complete + self.latency, end + self.word_gap_units * unit if end else 0.0)
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
        if k >= 1:                                      # word k must be complete before k-1 ends
            text, _ = self._words[k - 1]
            due = self.word_starts()[k - 1] + morse.duration(text, self.wpm or 25) - self.margin
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
        self._last_echo = time.monotonic()
        if self.on_end:
            if self._end_timer:
                self._end_timer.cancel()
            delay = self.end_delay_typing if self._typing else \
                self.end_delay_first if self._chars <= 1 else self.end_delay
            self._end_timer = asyncio.get_running_loop().call_later(delay, self._message_end)

    def _message_end(self) -> None:
        self._end_timer = None
        self._complete = True
        self._chars = 0
        self._typing = False
        if self._flush:
            self._flush.cancel()
        self._flush_text(end=True)

    def done(self) -> None:
        """Remote finished keying everything we sent."""
        if self._defer is not None:
            if not self._defer_timer:                   # CQ repeat: keep Win-Test's pause
                self._defer_timer = asyncio.get_running_loop().call_later(
                    self._defer, self._start_deferred)
            return
        if not self._text:
            self._done_at = time.monotonic()
            self.set_busy(False)

"""Key and PTT lines of a serial port (DTR/RTS), Linux ioctl, standard library only.

Used by the remote DTR keyer (CLAUDE.md 4.7, author's shack: /dev/ttyUSB0, DTR = KEY input of the
TS-590SG set to straight key, RTS = PTT for the amplifier).

Note: Linux raises DTR and RTS when a serial port is opened. Both lines are cleared right after
``open()``; the remaining pulse is a few milliseconds at most.
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
import termios

from .serialport import configure_raw

log = logging.getLogger("keyline")

LINES = {"dtr": termios.TIOCM_DTR, "rts": termios.TIOCM_RTS}


class KeyLine:
    def __init__(self, port: str, key: str = "dtr", ptt: str | None = None):
        if key not in LINES or (ptt and ptt not in LINES) or key == ptt:
            raise ValueError(f"key line {key!r} / ptt line {ptt!r} not possible")
        self.port = port
        self.key_bit = LINES[key]
        self.ptt_bit = LINES[ptt] if ptt else 0
        self.fd: int | None = None
        self.key_down = False
        self.ptt_on = False

    @property
    def has_ptt(self) -> bool:
        return bool(self.ptt_bit)

    def open(self) -> None:
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._set(LINES["dtr"] | LINES["rts"], False)     # Linux raised both on open
        configure_raw(self.fd)                              # no HUPCL
        log.info("key line %s open (key %s, ptt %s)", self.port,
                 "DTR" if self.key_bit == LINES["dtr"] else "RTS",
                 {0: "none", LINES["dtr"]: "DTR", LINES["rts"]: "RTS"}[self.ptt_bit])

    def _set(self, bits: int, on: bool) -> None:
        if self.fd is None or not bits:
            return
        fcntl.ioctl(self.fd, termios.TIOCMBIS if on else termios.TIOCMBIC, struct.pack("I", bits))

    def key(self, down: bool) -> None:
        self._set(self.key_bit, down)
        self.key_down = down

    def ptt(self, on: bool) -> None:
        if self.ptt_bit:
            self._set(self.ptt_bit, on)
        self.ptt_on = on

    def release(self) -> None:
        """Key up and PTT off – safe state."""
        try:
            self._set(self.key_bit | self.ptt_bit, False)
        except OSError as exc:
            log.error("cannot release key line: %s", exc)
        self.key_down = self.ptt_on = False

    def close(self) -> None:
        self.release()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

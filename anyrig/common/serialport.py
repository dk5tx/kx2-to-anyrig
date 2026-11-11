"""Linux serial ports and pseudo terminals for asyncio, standard library only (termios).

``SerialPort`` opens a real port (raw 8N1). ``PtyPort`` creates a pseudo terminal and a symlink
to its slave side, so another program (Win-Test under Wine) can open it like a COM port.
Both deliver ';'-terminated frames (CAT) or raw bytes to a callback and offer ``write``.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import termios
import tty
from typing import Callable

log = logging.getLogger(__name__)

BAUDRATES = {
    4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400,
    57600: termios.B57600, 115200: termios.B115200,
}


def configure_raw(fd: int, baud: int | None = None) -> None:
    attrs = termios.tcgetattr(fd)
    attrs[0] = termios.IGNPAR                                   # iflag
    attrs[1] = 0                                                # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL     # cflag, 8N1, no HUPCL
    attrs[3] = 0                                                # lflag
    if baud is not None:
        if baud not in BAUDRATES:
            raise ValueError(f"unsupported baud rate {baud}")
        attrs[4] = attrs[5] = BAUDRATES[baud]
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)


class FrameSplitter:
    """Splits a byte stream into ';'-terminated text frames (latin-1, terminator removed)."""

    def __init__(self, max_buffer: int = 4096):
        self._buf = b""
        self._max = max_buffer

    def feed(self, data: bytes) -> list[str]:
        self._buf += data
        frames = []
        while b";" in self._buf:
            frame, self._buf = self._buf.split(b";", 1)
            frames.append(frame.decode("latin-1"))
        if len(self._buf) > self._max:
            log.warning("discarding %d bytes without ';'", len(self._buf))
            self._buf = b""
        return frames


class _FdPort:
    """Common part: non-blocking fd registered with the asyncio loop."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self._on_data: Callable[[bytes], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start_reading(self, on_data: Callable[[bytes], None]) -> None:
        assert self.fd is not None
        self._on_data = on_data
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.fd, self._readable)

    def _readable(self) -> None:
        assert self.fd is not None
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            # EIO on a pty master while nobody has the slave open – ignore
            return
        if data and self._on_data:
            self._on_data(data)

    def write(self, data: bytes | str) -> None:
        if self.fd is None:
            raise OSError("port not open")
        if isinstance(data, str):
            data = data.encode("latin-1")
        view = memoryview(data)
        while view:
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                # tiny CAT frames: wait briefly instead of buffering
                import select
                select.select([], [self.fd], [], 0.05)
                continue
            view = view[n:]

    def close(self) -> None:
        if self.fd is not None:
            if self._loop is not None:
                try:
                    self._loop.remove_reader(self.fd)
                except Exception:
                    pass
            os.close(self.fd)
            self.fd = None


class SerialPort(_FdPort):
    def __init__(self, path: str, baud: int, exclusive: bool = True):
        super().__init__()
        self.path = path
        self.baud = baud
        self.exclusive = exclusive

    def open(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        if self.exclusive:
            try:
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
            except OSError:
                pass
        try:
            configure_raw(self.fd, self.baud)
        except termios.error:
            # simulators on a pty may not accept every setting
            configure_raw(self.fd, None)
        termios.tcflush(self.fd, termios.TCIOFLUSH)


class PtyPort(_FdPort):
    """A pseudo terminal. ``link`` is a symlink to the slave device for other programs.

    If ``holder_socket`` is set and anyrig-ports (``anyrig/local/ports.py``) runs and holds
    ``link``, its pty is used instead of a new one: it survives restarts of this program."""

    holder_socket: str | None = None            # set by kx2-local (config wintest.port_holder)

    def __init__(self, link: str | None, use_holder: bool = True):
        super().__init__()
        self.link = link
        self.use_holder = use_holder
        self.slave_name: str | None = None
        self._slave_fd: int | None = None
        self.held = False                       # pty borrowed from anyrig-ports
        self._holder: object | None = None

    def open(self) -> None:
        if self.link and self.use_holder and PtyPort.holder_socket:
            from ..local.ports import request_port   # noqa: WPS433 (avoid import cycle)
            got = request_port(self.link, PtyPort.holder_socket)
            if got:
                self.fd, self.slave_name, self._holder = got
                self.held = True
                return
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        self.fd = master
        # keep our own slave fd open so the master never sees EIO/hangup when the client closes
        self._slave_fd = slave
        self.slave_name = os.ttyname(slave)
        if self.link:
            try:
                if os.path.islink(self.link) or os.path.exists(self.link):
                    os.remove(self.link)
                os.symlink(self.slave_name, self.link)
            except OSError as exc:
                log.error("cannot create link %s -> %s: %s", self.link, self.slave_name, exc)
                raise

    def close(self) -> None:
        super().close()
        if self._holder is not None:            # give the port back, it stays open for Win-Test
            self._holder.close()
            self._holder = None
            return
        if self._slave_fd is not None:
            os.close(self._slave_fd)
            self._slave_fd = None
        if self.link and os.path.islink(self.link):
            try:
                os.remove(self.link)
            except OSError:
                pass

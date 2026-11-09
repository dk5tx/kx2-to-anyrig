"""Common base for pty-based rig simulators (run in a background thread)."""

from __future__ import annotations

import os
import pty
import select
import threading
import time
import tty


class PtySim:
    """A simulated rig on a pseudo terminal. ``port`` is the slave device path to open."""

    def __init__(self, reply_delay: float = 0.0):
        self.reply_delay = reply_delay
        self.master, self._slave = pty.openpty()
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)
        self.received: list[str] = []          # every frame received (without ';')
        self.lock = threading.RLock()
        self._buf = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._busy_until = 0.0

    # --- lifecycle
    def start(self) -> "PtySim":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- helpers for subclasses
    def send(self, text: str) -> None:
        try:
            os.write(self.master, text.encode("latin-1"))
        except OSError:
            pass

    def busy(self, seconds: float) -> None:
        """Do not answer anything for a while (e.g. KX2 band change)."""
        self._busy_until = time.monotonic() + seconds

    def frames_with(self, prefix: str) -> list[str]:
        with self.lock:
            return [f for f in self.received if f.startswith(prefix)]

    def handle(self, frame: str) -> None:          # pragma: no cover - abstract
        raise NotImplementedError

    def tick(self, now: float) -> None:            # periodic work (AI, keying)
        pass

    def _run(self) -> None:
        pending: list[str] = []
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self.master], [], [], 0.005)
            except (OSError, ValueError):
                return
            now = time.monotonic()
            if r:
                try:
                    self._buf += os.read(self.master, 4096)
                except OSError:
                    self._buf = b""
                while b";" in self._buf:
                    f, self._buf = self._buf.split(b";", 1)
                    pending.append(f.decode("latin-1"))
            if pending and now >= self._busy_until:
                for f in pending:
                    with self.lock:
                        self.received.append(f)
                    if self.reply_delay:
                        time.sleep(self.reply_delay)
                    with self.lock:
                        self.handle(f)
                pending = []
            with self.lock:
                self.tick(now)

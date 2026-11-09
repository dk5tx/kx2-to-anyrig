"""Logging setup and a traffic logger for serial conversations."""

from __future__ import annotations

import logging
import time
from typing import TextIO


def setup(level: str = "INFO", file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if file:
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def stamp() -> str:
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)


def escape(text: str) -> str:
    """Printable form of a frame: non-printable characters as \\xNN."""
    return "".join(c if 32 <= ord(c) < 127 and c != "\\" else "\\x%02X" % ord(c) for c in text)


class TrafficLog:
    """Append-only log of frames, one per line: time, direction, frame (same format as tools/catsniff.py)."""

    def __init__(self, path: str | None):
        self._f: TextIO | None = open(path, "a", buffering=1, encoding="utf-8") if path else None
        if self._f:
            self._f.write("\n# --- Start %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S"))

    def write(self, direction: str, frame: str, note: str = "") -> None:
        if self._f:
            self._f.write("%s %-5s %s;%s\n" % (stamp(), direction, escape(frame), ("  # " + note) if note else ""))

    def close(self) -> None:
        if self._f:
            self._f.write("# --- End %s ---\n" % time.strftime("%H:%M:%S"))
            self._f.close()
            self._f = None

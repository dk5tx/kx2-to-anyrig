"""Control protocol between kx2-local and anyrig-remote (CLAUDE.md 4.2).

Newline-delimited JSON over one TCP connection. Message types:
  hello, set, state, raw, action, cw, cw_abort, cw_done, meters, ping, pong, error
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

log = logging.getLogger("protocol")

VERSION = 1


def encode(msg: dict[str, Any]) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict[str, Any] | None:
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return msg if isinstance(msg, dict) and "t" in msg else None


class Connection:
    """One control connection (used on both sides)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.last_rx = time.monotonic()
        self.closed = False

    def send(self, msg: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.writer.write(encode(msg))
        except (ConnectionError, RuntimeError) as exc:
            log.debug("send failed: %s", exc)
            self.close()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while not self.closed:
            try:
                line = await self.reader.readline()
            except (ConnectionError, asyncio.IncompleteReadError):
                break
            if not line:
                break
            self.last_rx = time.monotonic()
            msg = decode(line)
            if msg is None:
                log.warning("invalid message from %s: %r", self.peer, line[:80])
                continue
            yield msg
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass

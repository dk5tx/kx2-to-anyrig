"""Control link from kx2-local to anyrig-remote: connect, reconnect, ping (CLAUDE.md 4.2)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ..common import protocol
from ..common.protocol import Connection

log = logging.getLogger("link")

PING_INTERVAL = 0.2


class RemoteLink:
    def __init__(self, host: str, port: int,
                 on_message: Callable[[dict], None] | None = None,
                 on_connect: Callable[[], None] | None = None,
                 on_disconnect: Callable[[], None] | None = None):
        self.host = host
        self.port = int(port)
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.conn: Connection | None = None
        self.rtt_ms: float | None = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.conn is not None and not self.conn.closed

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="remote-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self.conn:
            self.conn.close()

    async def wait_connected(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def send(self, msg: dict[str, Any]) -> bool:
        if not self.connected:
            return False
        assert self.conn is not None
        self.conn.send(msg)
        return True

    async def _run(self) -> None:
        # Nothing may end this loop except cancellation: on 11 Nov 2026 one exception in a
        # message handler killed the task and kx2-local never reconnected.
        delay = 0.5
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
            except OSError as exc:
                log.debug("connect %s:%s failed: %s", self.host, self.port, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            delay = 0.5
            try:
                await self._session(Connection(reader, writer))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("control link failed – reconnecting")
                await asyncio.sleep(0.5)

    async def _session(self, conn: Connection) -> None:
        self.conn = conn
        log.info("connected to remote %s:%s", self.host, self.port)
        conn.send({"t": "hello", "role": "local", "version": protocol.VERSION})
        pinger = asyncio.create_task(self._ping(conn))
        self._connected.set()
        try:
            if self.on_connect:
                self.on_connect()
            async for msg in conn.messages():
                if msg.get("t") == "pong":
                    ts = msg.get("ts")
                    if isinstance(ts, (int, float)):
                        self.rtt_ms = round((time.monotonic() - ts) * 1000.0, 1)
                    continue
                if self.on_message:
                    try:
                        self.on_message(msg)
                    except Exception:
                        log.exception("error handling %r", msg)
        finally:
            pinger.cancel()
            self._connected.clear()
            self.conn = None
            try:
                conn.close()
            except Exception:
                pass
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    log.exception("error in disconnect handler")
            log.warning("remote connection lost – reconnecting")

    async def _ping(self, conn: Connection) -> None:
        while not conn.closed:
            conn.send({"t": "ping", "ts": time.monotonic()})
            await asyncio.sleep(PING_INTERVAL)

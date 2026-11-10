"""anyrig-remote core: control server, rig state forwarding, safety (CLAUDE.md 4.2, 4.12).

One local client at a time (a new connection replaces the old one). Rig changes are forwarded
as ``state`` messages, meters rate-limited as ``meters``. If the control link goes silent longer
than ``safety.link_timeout_ms``, the rig is unkeyed immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..common import protocol
from ..common.protocol import Connection
from ..drivers.base import Rig
from .cwtext import CWSender

log = logging.getLogger("remote")

METER_INTERVAL = 0.05           # max. 20 meter messages per second


class RemoteServer:
    def __init__(self, cfg: dict, rig: Rig):
        self.cfg = cfg
        self.rig = rig
        self.conn: Connection | None = None
        self.server: asyncio.base_events.Server | None = None
        self.link_timeout = cfg["safety"]["link_timeout_ms"] / 1000.0
        self.max_power = int(cfg["safety"].get("max_power_w", 100))
        self._tasks: list[asyncio.Task] = []
        self._meters: dict[str, Any] | None = None
        self._last_meter_send = 0.0
        self.port: int | None = None
        rig.state.subscribe(self._on_rig_change)
        rig.on_meters = self._on_meters
        self.cw = CWSender(rig, rig.profile.get("cw"), on_done=self._cw_done)
        rig.on_raw = self.cw.on_raw

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        listen = self.cfg["listen"]
        self.server = await asyncio.start_server(self._client, listen["host"], int(listen["port"]))
        self.port = self.server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.create_task(self._watchdog(), name="watchdog"))
        self._tasks.append(asyncio.create_task(self._meter_flush(), name="meters"))
        self.cw.start()
        log.info("listening on %s:%s", listen["host"], self.port)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.cw.stop()
        if self.conn:
            self.conn.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    # ------------------------------------------------------------------ client
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Connection(reader, writer)
        if self.conn and not self.conn.closed:
            log.warning("new client %s replaces %s", conn.peer, self.conn.peer)
            self.conn.close()
        self.conn = conn
        self.cw.blocked = False                  # safety block ends with a new connection
        log.info("client connected: %s", conn.peer)
        conn.send({"t": "hello", "role": "remote", "version": protocol.VERSION,
                   "rig": self.rig.profile.get("name"), "caps": sorted(self.rig.capabilities())})
        for k, v in list(self.rig.state.items()):
            conn.send({"t": "state", "k": k, "v": v, "src": "rig"})
        conn.send({"t": "snapshot_done"})
        try:
            async for msg in conn.messages():
                self.handle(msg, conn)
        finally:
            if self.conn is conn:
                log.warning("client %s disconnected", conn.peer)
                self._safety("link closed")
                self.conn = None

    def handle(self, msg: dict, conn: Connection) -> None:
        t = msg.get("t")
        if t == "ping":
            conn.send({"t": "pong", "ts": msg.get("ts")})
        elif t == "set":
            k, v = msg.get("k"), msg.get("v")
            if k == "power" and isinstance(v, (int, float)) and v > self.max_power:
                v = self.max_power
            if not self.rig.set(k, v):
                log.debug("set %s=%r not supported by rig", k, v)
        elif t == "raw":
            d = str(msg.get("d", ""))
            if d:
                self.rig.send_raw(d)
        elif t == "action":
            self._action(str(msg.get("a", "")))
        elif t == "hello":
            log.info("client hello: %s", msg)
        elif t == "cw":
            self.cw.send(str(msg.get("text", "")), msg.get("id"))
        elif t == "cw_abort":
            self.cw.abort()
        else:
            log.debug("unknown message %s", msg)

    def _cw_done(self, ids: list) -> None:
        if self.conn:
            for i in ids:
                self.conn.send({"t": "cw_done", "id": i})

    def _action(self, name: str) -> None:
        spec = (self.rig.profile.get("actions") or {}).get(name)
        if not spec:
            log.warning("unknown action %r", name)
            return
        key, values = spec["key"], spec["values"]
        cur = self.rig.state.get(key, values[0])
        nxt = values[(values.index(cur) + 1) % len(values)] if cur in values else values[-1]
        self.rig.set(key, nxt)

    # ------------------------------------------------------------------ rig -> client
    def _on_rig_change(self, key: str, value: Any, source: str) -> None:
        if self.conn:
            self.conn.send({"t": "state", "k": key, "v": value, "src": "rig"})

    def _on_meters(self, meters: dict[str, Any]) -> None:
        self._meters = meters

    async def _meter_flush(self) -> None:
        while True:
            await asyncio.sleep(METER_INTERVAL)
            if self._meters and self.conn:
                m = {k: v for k, v in self._meters.items() if k != "t"}
                self.conn.send({"t": "meters", **m})
                self._meters = None

    # ------------------------------------------------------------------ safety
    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            c = self.conn
            if c and not c.closed and time.monotonic() - c.last_rx > self.link_timeout:
                log.error("control link silent for %.1f s – unkeying", time.monotonic() - c.last_rx)
                self._safety("link timeout")
                c.close()

    def _safety(self, reason: str) -> None:
        try:
            self.cw.blocked = True
            self.cw.abort()
            self.rig.unkey()
            log.info("safety (%s): rig unkeyed", reason)
        except Exception as exc:          # never let safety crash the server
            log.error("safety failed: %s", exc)

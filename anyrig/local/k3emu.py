"""Elecraft K3 emulation for Win-Test on a pseudo terminal (CLAUDE.md 4.4).

Win-Test polls only IF; and FB; (measured). Those and the other common queries are answered
immediately from the normalized state. Set commands Win-Test issues itself (spot click: FA, MD, FT)
update the state and go to the KX2 at once (and to the remote rig via ``on_base_set``).
Everything else (Lua scripts) goes to the router. Queries that cannot be answered from the state
are relayed to the KX2, which speaks the same protocol.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from ..common import elecraft
from ..common.log import TrafficLog
from ..common.serialport import FrameSplitter, PtyPort
from ..common.state import State
from .kx2 import KX2Link
from .router import Router

log = logging.getLogger("k3emu")

BASE_SETS = {"FA", "FB", "MD", "MD$", "FT", "FR", "RT", "XT"}
LOCAL_SETTINGS = {"AI", "K3", "K2"}       # Win-Test's view only; the KX2 link manages its own AI/K31


class K3Emulator:
    def __init__(self, link: str | None, state: State, kx2: KX2Link, router: Router,
                 traffic: TrafficLog | None = None,
                 on_base_set: Callable[[str, dict], None] | None = None):
        self.state = state
        self.kx2 = kx2
        self.router = router
        self.traffic = traffic
        self.on_base_set = on_base_set
        self.port = PtyPort(link)
        self._splitter = FrameSplitter()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.settings = {"AI": "0", "K3": "0", "K2": "0"}
        self.frames_in = 0

    @property
    def device(self) -> str | None:
        return self.port.slave_name

    def start(self) -> None:
        self.port.open()
        self.port.start_reading(self._on_data)
        self._task = asyncio.create_task(self._worker(), name="k3emu")
        log.info("Win-Test CAT port: %s -> %s", self.port.link, self.port.slave_name)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.port.close()

    # ------------------------------------------------------------------
    def _on_data(self, data: bytes) -> None:
        for frame in self._splitter.feed(data):
            frame = frame.strip("\r\n")
            if frame:
                self._queue.put_nowait(frame)

    async def _worker(self) -> None:
        # frames are processed strictly in order (spot click = FA, MD, FT back-to-back)
        while True:
            frame = await self._queue.get()
            try:
                await self.handle(frame)
            except Exception:
                log.exception("error handling %r", frame)

    def _reply(self, frame: str) -> None:
        if self.traffic:
            self.traffic.write("WT<", frame)
        self.port.write(frame + ";")

    async def handle(self, frame: str) -> None:
        self.frames_in += 1
        if self.traffic:
            self.traffic.write("WT>", frame)
        key = elecraft.frame_key(frame)
        arg = frame[len(key):]
        if not arg:
            await self._query(key, frame)
        else:
            self._set(key, arg, frame)

    async def _query(self, key: str, frame: str) -> None:
        answer: str | None = None
        if key == "IF":
            if "freq_a" in self.state:
                answer = elecraft.build_if(self.state, self.kx2.if_template, tx=bool(self.state.get("tx")))
        elif key == "ID":
            answer = "ID017"
        elif key in LOCAL_SETTINGS:
            answer = key + self.settings[key]
        else:
            answer = elecraft.encode_answer(key, self.state, self.kx2.raw_cache)
        if answer is None:
            # not in the state yet / unknown: ask the KX2 (same protocol family)
            answer = await self.kx2.query(frame + ";")
            if answer is None:
                if self.traffic:
                    self.traffic.write("WT<", "", f"no answer for {frame}")
                return
        self._reply(answer)

    def _set(self, key: str, arg: str, frame: str) -> None:
        if key in LOCAL_SETTINGS:
            self.settings[key] = arg[:1]
            return
        if key in BASE_SETS:
            values = elecraft.decode(key, arg)
            if not values:
                log.warning("cannot decode %r", frame)
                return
            self.state.update(values, "wintest")
            self.kx2.write(frame + ";", values)
            if self.on_base_set:
                self.on_base_set(frame, values)
            return
        self.router.route(frame)

"""Run a GStreamer pipeline as a ``gst-launch-1.0`` child process and restart it when it ends.

Used by the remote audio sender (no live control needed) and as the fallback of the local
receiver when the GStreamer Python bindings (python3-gi) are missing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from typing import Callable

log = logging.getLogger("gst")

GST_LAUNCH = "gst-launch-1.0"


class GstProcess:
    def __init__(self, name: str, pipeline: str, env: dict[str, str] | None = None,
                 restart_s: float = 2.0, argv: list[str] | None = None,
                 on_state: Callable[[str], None] | None = None):
        self.name = name
        self.pipeline = pipeline
        self.env = env or {}
        self.restart_s = restart_s
        self.argv = argv            # test hook: command instead of gst-launch
        self.on_state = on_state
        self.state = "stopped"      # stopped | starting | running | restarting | missing
        self.starts = 0
        self.last_error = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    def _set(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if self.on_state:
                self.on_state(state)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="gst-" + self.name)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await self._kill()
        self._set("stopped")

    async def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 2.0)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()

    def command(self) -> list[str]:
        if self.argv:
            return list(self.argv)
        return [GST_LAUNCH, "-q"] + shlex.split(self.pipeline)

    async def _run(self) -> None:
        cmd = self.command()
        if not self.argv and shutil.which(GST_LAUNCH) is None:
            log.error("%s: %s not found – install gstreamer1.0-tools", self.name, GST_LAUNCH)
            self._set("missing")
            return
        while True:
            self._set("starting" if self.starts == 0 else "restarting")
            self.starts += 1
            log.info("%s: starting %s", self.name, self.pipeline)
            self._proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                env=dict(os.environ, **self.env))
            self._set("running")
            _, err = await self._proc.communicate()
            code = self._proc.returncode
            self.last_error = (err or b"").decode(errors="replace").strip()[-400:]
            log.warning("%s: pipeline ended (exit %s)%s – restart in %.0f s", self.name, code,
                        (": " + self.last_error) if self.last_error else "", self.restart_s)
            self._proc = None
            self._set("restarting")
            await asyncio.sleep(self.restart_s)

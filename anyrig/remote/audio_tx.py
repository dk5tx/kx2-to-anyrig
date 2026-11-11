"""Remote RX audio sender (CLAUDE.md 4.10): rig sound card -> Opus -> RTP/UDP -> laptop.

Measured 11 Nov 2026 (TS-590SG USB codec -> WLAN -> laptop, recorded acoustically):
default buffers 230 ms; small capture buffers here plus small playback buffers on the
laptop 96 ms. The capture device is opened directly (``alsasrc hw:``), past PipeWire and
EasyEffects.

The stream goes to the address of the connected kx2-local, to the port it asks for
(``{"t":"audio","port":N}``). It runs only while a client is connected.
"""

from __future__ import annotations

import logging
from typing import Any

from ..common.gstproc import GstProcess

log = logging.getLogger("audio-tx")


def source_element(cfg: dict) -> str:
    src = str(cfg.get("source", "hw:CARD=CODEC"))
    buf = int(cfg.get("buffer_ms", 20)) * 1000
    lat = int(cfg.get("latency_ms", 5)) * 1000
    if src == "test":                              # test tone, no sound card needed
        return "audiotestsrc is-live=true wave=sine freq=600 volume=0.3"
    if src.startswith("pulse:"):                   # through PipeWire/Pulse (adds latency)
        return "pulsesrc device=%s buffer-time=%d latency-time=%d" % (src[6:], buf, lat)
    return "alsasrc device=%s buffer-time=%d latency-time=%d" % (src, buf, lat)


def pipeline(cfg: dict, host: str, port: int) -> str:
    return ("{src} ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
            "opusenc frame-size={frame} bitrate={rate} audio-type=voice ! rtpopuspay pt=96 ! "
            "udpsink host={host} port={port} sync=false").format(
        src=source_element(cfg), frame=int(cfg.get("frame_ms", 10)),
        rate=int(cfg.get("bitrate", 48000)), host=host, port=int(port))


class AudioSender:
    def __init__(self, cfg: dict, process_factory: Any = GstProcess):
        self.cfg = cfg
        self.factory = process_factory
        self.proc: GstProcess | None = None
        self.target: tuple[str, int] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    async def start(self, host: str, port: int) -> None:
        if not self.enabled:
            log.info("audio disabled in the config – no stream to %s:%s", host, port)
            return
        if self.target == (host, port) and self.proc:
            return
        await self.stop()
        self.target = (host, port)
        self.proc = self.factory("audio-tx", pipeline(self.cfg, host, port))
        self.proc.start()
        log.info("RX audio -> %s:%s", host, port)

    async def stop(self) -> None:
        proc, self.proc = self.proc, None
        self.target = None
        if proc:
            await proc.stop()

    @property
    def state(self) -> str:
        return self.proc.state if self.proc else "stopped"

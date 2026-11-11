"""Local RX audio receiver (CLAUDE.md 4.10): RTP/Opus from the shack -> laptop speakers.

Volume follows the KX2 AF gain knob (0-60), set live on the GStreamer ``volume`` element when
the GStreamer Python bindings are available (python3-gi, gir1.2-gstreamer-1.0); otherwise
``gst-launch-1.0`` runs with a fixed volume.

Measured 11 Nov 2026 (acoustic recording, rig speaker vs. laptop):
- default sink buffers: 230 ms; pipewiresink: 237 ms;
- ``pulsesink buffer-time=20000 latency-time=5000`` with PULSE_LATENCY_MSEC=10: 96 ms.
- over WLAN about 4 short dropouts in 2 minutes with a 25 ms jitter buffer (irregular -> WLAN).
  Lost packets are concealed by Opus (do-lost + plc) instead of cut hard.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from ..common.gstproc import GstProcess

log = logging.getLogger("audio-rx")

CAPS = "application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,payload=96"
AF_MAX = 60


def af_to_volume(af: float, curve: str = "log", range_db: float = 40.0, gain: float = 1.0) -> float:
    """KX2 AF gain 0..60 -> GStreamer volume (1.0 = unchanged)."""
    af = max(0.0, min(float(af), AF_MAX))
    if af <= 0:
        return 0.0
    if curve == "linear":
        v = af / AF_MAX
    else:
        v = 10 ** ((af / AF_MAX - 1.0) * range_db / 20.0)
    return round(v * gain, 4)


def sink_element(cfg: dict) -> str:
    sink = str(cfg.get("sink", "pulse"))
    if sink in ("pulse", "default"):
        return "pulsesink buffer-time=20000 latency-time=5000"
    if sink == "auto":
        return "autoaudiosink"
    if sink.startswith("alsa:"):
        return "alsasink device=%s buffer-time=20000 latency-time=5000" % sink[5:]
    if sink == "fake":
        return "fakesink sync=true"
    return sink                                     # any element description


def pipeline(cfg: dict, volume: float) -> str:
    return ('udpsrc port={port} caps="{caps}" ! rtpjitterbuffer latency={jit} do-lost=true ! '
            "rtpopusdepay ! opusdec plc=true ! audioconvert ! audioresample ! "
            "volume name=vol volume={vol} ! {sink}").format(
        port=int(cfg.get("port", 7356)), caps=CAPS, jit=int(cfg.get("jitter_ms", 40)),
        vol=volume, sink=sink_element(cfg))


def _gst_module() -> Any:
    try:
        import gi                                   # noqa: WPS433 (optional dependency)
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst               # type: ignore
        Gst.init(None)
        return Gst
    except Exception:                               # ImportError, ValueError, GLib errors
        return None


class AudioReceiver:
    def __init__(self, cfg: dict, use_bindings: bool | None = None, process_factory: Any = GstProcess):
        self.cfg = cfg
        self.factory = process_factory
        self.volume = af_to_volume(cfg.get("af_default", 30), cfg.get("af_curve", "log"),
                                   float(cfg.get("af_range_db", 40)), float(cfg.get("gain", 1.0)))
        self.state = "stopped"
        self.restarts = 0
        self._gst = _gst_module() if use_bindings is not False else None
        self._pipe: Any = None
        self._vol: Any = None
        self._proc: GstProcess | None = None
        self._task: asyncio.Task | None = None
        os.environ.setdefault("PULSE_LATENCY_MSEC", str(int(cfg.get("pulse_latency_ms", 10))))

    @property
    def port(self) -> int:
        return int(self.cfg.get("port", 7356))

    @property
    def live_volume(self) -> bool:
        return self._gst is not None

    # ------------------------------------------------------------------ volume
    def set_af(self, af: float) -> None:
        self.set_volume(af_to_volume(af, self.cfg.get("af_curve", "log"),
                                     float(self.cfg.get("af_range_db", 40)),
                                     float(self.cfg.get("gain", 1.0))))

    def set_volume(self, v: float) -> None:
        self.volume = v
        if self._vol is not None:
            self._vol.set_property("volume", v)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.cfg.get("enabled", True):
            log.info("audio disabled in the config")
            return
        if self._gst is not None:
            self._task = asyncio.create_task(self._run_bindings(), name="audio-rx")
        else:
            log.warning("GStreamer Python bindings missing (python3-gi, gir1.2-gstreamer-1.0): "
                        "audio runs with a fixed volume, the KX2 AF knob has no effect")
            self._proc = self.factory("audio-rx", pipeline(self.cfg, self.volume),
                                      on_state=self._set_state)
            self._proc.start()

    def _set_state(self, s: str) -> None:
        self.state = s

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._stop_pipe()
        if self._proc:
            await self._proc.stop()
            self._proc = None
        self.state = "stopped"

    def _stop_pipe(self) -> None:
        if self._pipe is not None:
            self._pipe.set_state(self._gst.State.NULL)
            self._pipe = self._vol = None

    async def _run_bindings(self) -> None:
        Gst = self._gst
        while True:
            desc = pipeline(self.cfg, self.volume)
            try:
                self._pipe = Gst.parse_launch(desc)
                self._vol = self._pipe.get_by_name("vol")
                self._pipe.set_state(Gst.State.PLAYING)
                self.state = "running"
                log.info("RX audio on UDP port %s (jitter buffer %s ms, volume %.3f)",
                         self.port, self.cfg.get("jitter_ms", 40), self.volume)
                bus = self._pipe.get_bus()
                while True:
                    msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                    if msg is not None:
                        if msg.type == Gst.MessageType.ERROR:
                            err, dbg = msg.parse_error()
                            log.warning("audio pipeline error: %s (%s)", err.message, dbg)
                        else:
                            log.warning("audio pipeline ended")
                        break
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("audio pipeline failed: %s", exc)
            self._stop_pipe()
            self.state = "restarting"
            self.restarts += 1
            await asyncio.sleep(2.0)

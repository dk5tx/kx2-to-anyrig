"""Milestone M5: RX audio pipelines (no sound hardware, no GStreamer needed)."""

import asyncio
import sys

from anyrig.common.gstproc import GstProcess
from anyrig.local.audio_rx import AudioReceiver, af_to_volume, pipeline as rx_pipeline
from anyrig.remote.audio_tx import pipeline as tx_pipeline
from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import wait_for
from tests.test_m2_end_to_end import Stack


def run(coro):
    return asyncio.run(coro)


def test_af_knob_to_volume():
    assert af_to_volume(60) == 1.0
    assert af_to_volume(0) == 0.0
    assert abs(af_to_volume(30) - 0.1) < 1e-3              # log, 40 dB range: half = -20 dB
    assert af_to_volume(30, "linear") == 0.5
    assert af_to_volume(80) == 1.0                         # clamped
    assert af_to_volume(60, gain=2.0) == 2.0


def test_pipelines_use_the_measured_low_latency_settings():
    tx = tx_pipeline({"source": "hw:CARD=CODEC", "bitrate": 48000, "frame_ms": 10,
                      "buffer_ms": 20, "latency_ms": 5}, "192.168.86.75", 7356)
    assert "alsasrc device=hw:CARD=CODEC buffer-time=20000 latency-time=5000" in tx
    assert "opusenc frame-size=10 bitrate=48000 audio-type=voice" in tx
    assert tx.endswith("udpsink host=192.168.86.75 port=7356 sync=false")
    assert "audiotestsrc" in tx_pipeline({"source": "test"}, "h", 1)
    assert tx_pipeline({"source": "pulse:abc"}, "h", 1).startswith("pulsesrc device=abc")
    rx = rx_pipeline({"port": 7356, "jitter_ms": 40, "sink": "pulse"}, 0.5)
    assert "rtpjitterbuffer latency=40 do-lost=true" in rx and "opusdec plc=true" in rx
    assert "level name=lvl interval=100000000 ! volume name=vol volume=0.5" in rx
    assert rx.endswith("pulsesink buffer-time=20000 latency-time=5000")


def test_gst_process_restarts_after_exit():
    async def body():
        p = GstProcess("t", "x", restart_s=0.05, argv=[sys.executable, "-c", "import sys; sys.exit(3)"])
        p.start()
        assert await wait_for(lambda: p.starts >= 3, 3.0)
        await p.stop()
        assert p.state == "stopped"
    run(body())


class FakeProc:
    def __init__(self, name, pipeline, on_state=None):
        self.pipeline = pipeline
        self.state = "stopped"
        self.on_state = on_state

    def start(self):
        self.state = "running"

    async def stop(self):
        self.state = "stopped"


def test_receiver_without_bindings_runs_gst_launch_with_fixed_volume():
    async def body():
        r = AudioReceiver({"port": 7400, "jitter_ms": 25}, use_bindings=False, process_factory=FakeProc)
        r.set_af(60)
        r.start()
        assert "udpsrc port=7400" in r._proc.pipeline and "volume=1.0" in r._proc.pipeline
        assert not r.live_volume
        await r.stop()
    run(body())


def test_remote_streams_to_the_connected_client_only(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            s.server.audio.factory = FakeProc
            s.link.send({"t": "audio", "port": 7999})
            assert await wait_for(lambda: s.server.audio.target == ("127.0.0.1", 7999), 2.0)
            assert "udpsink host=127.0.0.1 port=7999" in s.server.audio.proc.pipeline
            await s.link.stop()                            # client gone: stream stops
            assert await wait_for(lambda: s.server.audio.proc is None, 3.0)
            await s.stop()
    run(body())

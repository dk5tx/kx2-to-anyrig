"""Milestone M2 end to end:
KX2 simulator -> kx2-local (KX2Link, K3Emulator, Router, Sync, RemoteLink) -> TCP ->
anyrig-remote (RemoteServer, KenwoodRig) -> TS-590SG simulator.
"""

import asyncio
import os
import time

from anyrig.common.config import LOCAL_DEFAULTS, REMOTE_DEFAULTS, deep_merge
from anyrig.common.state import State
from anyrig.drivers.kenwood import KenwoodRig
from anyrig.local.k3emu import K3Emulator
from anyrig.local.kx2 import KX2Link
from anyrig.local.link import RemoteLink
from anyrig.local.router import Router
from anyrig.local.sync import Sync
from anyrig.remote.core import RemoteServer
from anyrig.remote.main import load_profile
from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import WinTest, wait_for

PROFILE = load_profile("profiles/ts590sg.yaml")


class Stack:
    async def start(self, tmp_path, kx2sim, rigsim, link_timeout_ms=1000, rcfg_extra=None,
                    keyline=None):
        rcfg = deep_merge(REMOTE_DEFAULTS, {"listen": {"host": "127.0.0.1", "port": 0},
                                            "safety": {"link_timeout_ms": link_timeout_ms}})
        rcfg = deep_merge(rcfg, rcfg_extra or {})
        self.rig = KenwoodRig(PROFILE, rigsim.port)
        await self.rig.open()
        self.server = RemoteServer(rcfg, self.rig, keyline=keyline)
        await self.server.start()

        lcfg = deep_merge(LOCAL_DEFAULTS, {"kx2": {"port": kx2sim.port}})
        self.state = State()
        self.kx2 = KX2Link(lcfg["kx2"], self.state)
        self.link = RemoteLink("127.0.0.1", self.server.port)
        self.sync = Sync(self.state, self.link, lcfg["kx2_keys"], kx2=self.kx2)
        self.link.on_message = self.sync.on_message
        self.link.on_disconnect = self.sync.on_disconnect
        self.router = Router(lcfg["router"], to_kx2=self.kx2.write, to_remote=self.sync.send_raw)
        self.emu = K3Emulator(str(tmp_path / "com1"), self.state, self.kx2, self.router)
        await self.kx2.start()
        self.emu.start()
        self.link.start()
        assert await self.link.wait_connected(3.0)
        assert await wait_for(lambda: "freq_a" in self.state and self.sync.ready)
        return self

    async def settle(self, timeout=6.0):
        """Wait until the start-up sync (rig is master) is complete: every mirrored KX2 value has
        been read once and no quiet period is running."""
        keys = {"mode", "filter_bw", "preamp", "agc", "nb", "keyer_wpm", "rit_offset"}
        assert await wait_for(lambda: keys <= self.sync._known
                              and time.monotonic() > self.sync._quiet_until + 0.1, timeout)

    async def stop(self):
        await self.link.stop()
        await self.emu.stop()
        await self.kx2.stop()
        await self.server.stop()
        await self.rig.close()


def run(coro):
    return asyncio.run(coro)


def test_kx2_vfo_moves_ts590_fast(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.02) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            for f in (14025100, 14025250, 14025400):
                t0 = time.monotonic()
                ksim.knob("FA", "%011d" % f)
                assert await wait_for(lambda: rsim.raw["FA"] == "%011d" % f, 1.0)
                assert time.monotonic() - t0 < 0.3        # target <= 150 ms on hardware
            ksim.knob("BW", "0040")
            assert await wait_for(lambda: rsim.raw["FW"] == "0400")
            ksim.knob("PA", "1")                          # preamp via 1 Hz AI or slow poll
            ksim.knob("PA", "0")
            ksim.knob("RA", "01")
            assert await wait_for(lambda: rsim.raw["RA"].startswith("01"), 3.0)
            await s.stop()
    run(body())


def test_spot_click_qsys_both_rigs(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.02) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            ksim.knob("MD", "2")
            assert await wait_for(lambda: rsim.raw["MD"] == "2", 1.5)
            wt = WinTest(str(tmp_path / "com1"))
            t0 = time.monotonic()
            wt.send("FA00007039800;MD3;FT0;")
            assert await wait_for(lambda: rsim.raw["FA"] == "00007039800" and rsim.raw["MD"] == "3", 1.0)
            assert time.monotonic() - t0 < 0.2            # directly, not via KX2 polling
            assert await wait_for(lambda: ksim.raw["FA"] == "00007039800")
            wt.close()
            await s.stop()
    run(body())


def test_lua_scripts_reach_ts590(tmp_path):
    async def body():
        with KX2Sim() as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            wt = WinTest(str(tmp_path / "com1"))
            wt.send("FW0200;RA01;AN919;KS030;CA1;")
            assert await wait_for(lambda: rsim.raw["FW"] == "0200" and rsim.raw["RA"] == "01"
                                  and rsim.raw["AN"] == "919")
            assert await wait_for(lambda: "CA1" in rsim.received)
            await asyncio.sleep(0.1)
            assert "KS030" not in rsim.received           # ignored (WinKeyer owns the speed)
            assert "FW0200" not in ksim.received          # Lua commands do not go to the KX2
            wt.send("TX0;")
            await asyncio.sleep(0.1)
            assert "TX0" not in rsim.received             # blocked
            wt.close()
            await s.stop()
    run(body())


def test_apf_key_sets_manual_notch_in_cw(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            ksim.apf = True                                  # APF already on when we start
            s = await Stack().start(tmp_path, ksim, rsim)
            await asyncio.sleep(2.5)                         # IC is polled slowly (2 s)
            assert rsim.raw["NT"] == "00"                    # first reading is not a key press
            ksim.set_apf(False)
            assert await wait_for(lambda: s.state.get("apf") is False, 3.0)
            ksim.set_apf(True)
            assert await wait_for(lambda: rsim.raw["NT"] == "20", 3.0)
            ksim.set_apf(False)
            assert await wait_for(lambda: rsim.raw["NT"] == "00", 3.0)
            await s.stop()
    run(body())


def test_kx2_notch_in_ssb_sets_auto_notch(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            ksim.raw["MD"] = "2"
            rsim.raw["MD"] = "2"
            s = await Stack().start(tmp_path, ksim, rsim)
            await asyncio.sleep(0.8)                         # DS (normal priority) read once
            ksim.notch = True
            assert await wait_for(lambda: rsim.raw["NT"] == "10", 2.0)
            await s.stop()
    run(body())


def test_watchdog_unkeys_when_link_dies(tmp_path):
    async def body():
        with KX2Sim() as ksim, TS590Sim(time_scale=1.0) as rsim:
            s = await Stack().start(tmp_path, ksim, rsim, link_timeout_ms=300)
            s.rig.send_raw("KY " + "TEST TEST TEST TEST TEST" + ";")   # long transmission
            assert await wait_for(lambda: s.rig.state.get("tx") is True)
            await s.link.stop()                                          # laptop gone
            t0 = time.monotonic()
            assert await wait_for(lambda: rsim.received.count("KY0") >= 1, 1.5)
            assert time.monotonic() - t0 < 1.0
            await s.emu.stop(); await s.kx2.stop(); await s.server.stop(); await s.rig.close()
    run(body())


def test_remote_state_and_meters_reach_local(tmp_path):
    async def body():
        with KX2Sim() as ksim, TS590Sim(sm_period=0.05) as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            assert await wait_for(lambda: s.sync.remote_state.get("freq_a") == 14025000)
            assert s.sync.remote_info.get("rig") == "Kenwood TS-590SG"
            assert await wait_for(lambda: "s" in s.sync.meters)
            assert s.link.rtt_ms is not None and s.link.rtt_ms < 50
            await s.stop()
    run(body())

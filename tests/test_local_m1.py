"""Milestone M1 integration: KX2 simulator <-> KX2Link <-> K3Emulator <-> fake Win-Test."""

import asyncio
import os
import time

import pytest

from anyrig.common.config import LOCAL_DEFAULTS, deep_merge
from anyrig.common.state import State
from anyrig.local.k3emu import K3Emulator
from anyrig.local.kx2 import KX2Link
from anyrig.local.router import Router
from tests.sim.kx2sim import KX2Sim


class WinTest:
    """Minimal asyncio client on the emulator's pty, behaving like Win-Test."""

    def __init__(self, device):
        self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.buf = b""

    async def ask(self, cmd, timeout=0.5):
        os.write(self.fd, cmd.encode())
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                self.buf += os.read(self.fd, 4096)
            except BlockingIOError:
                pass
            if b";" in self.buf:
                f, self.buf = self.buf.split(b";", 1)
                return f.decode("latin-1")
            await asyncio.sleep(0.005)
        return None

    def send(self, cmd):
        os.write(self.fd, cmd.encode())

    def close(self):
        os.close(self.fd)


async def setup(tmp_path, sim, default="kx2"):
    cfg = deep_merge(LOCAL_DEFAULTS, {"kx2": {"port": sim.port}, "router": {"default": default}})
    state = State()
    kx2 = KX2Link(cfg["kx2"], state)
    router = Router(cfg["router"], to_kx2=kx2.write)
    emu = K3Emulator(str(tmp_path / "com1"), state, kx2, router)
    await kx2.start()
    emu.start()
    return state, kx2, emu


async def wait_for(pred, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def run(coro):
    return asyncio.run(coro)


def test_startup_sets_ai2_and_k31_and_restores(tmp_path):
    async def body():
        with KX2Sim() as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            assert await wait_for(lambda: sim.ai == 2 and sim.k3 == 1)
            await emu.stop()
            await kx2.stop()
            await asyncio.sleep(0.05)
            assert sim.ai == 0 and sim.k3 == 0          # restored
            assert os.path.islink(tmp_path / "com1") is False
    run(body())


def test_wintest_if_fb_answered_from_state(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.02) as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            assert await wait_for(lambda: "freq_a" in state and kx2.if_template)
            wt = WinTest(str(tmp_path / "com1"))
            t0 = time.monotonic()
            ans = await wt.ask("IF;")
            dt = time.monotonic() - t0
            assert ans.startswith("IF00014025000     +0000")
            assert len(ans) == 37 and ans[2 + 27] == "3"
            assert dt < 0.05                                    # from state, not via the KX2
            assert await wt.ask("FB;") == "FB00014030000"
            assert await wt.ask("ID;") == "ID017"
            wt.close()
            await emu.stop(); await kx2.stop()
    run(body())


def test_vfo_turn_reaches_wintest_fast(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.02, ai_period=1.0) as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            assert await wait_for(lambda: "freq_a" in state)
            t0 = time.monotonic()
            sim.knob("FA", "00014031230")
            assert await wait_for(lambda: state.get("freq_a") == 14031230, 1.0)
            assert time.monotonic() - t0 < 0.3                  # polled, not waiting for 1 s AI
            wt = WinTest(str(tmp_path / "com1"))
            assert (await wt.ask("IF;"))[2:13] == "00014031230"
            wt.close()
            await emu.stop(); await kx2.stop()
    run(body())


def test_spot_click_sets_kx2_and_no_flipback(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.02) as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            assert await wait_for(lambda: "freq_a" in state)
            sim.knob("MD", "2")
            assert await wait_for(lambda: state.get("mode") == "USB")
            wt = WinTest(str(tmp_path / "com1"))
            wt.send("FA00007039800;MD3;FT0;")
            seen = set()
            end = time.monotonic() + 0.6
            while time.monotonic() < end:                       # Win-Test polls during the switch
                ans = await wt.ask("IF;")
                seen.add((ans[2:13], ans[2 + 27]))
                await asyncio.sleep(0.02)
            assert seen == {("00007039800", "3")}               # never the old value
            assert sim.raw["FA"] == "00007039800" and sim.raw["MD"] == "3"
            wt.close()
            await emu.stop(); await kx2.stop()
    run(body())


def test_unknown_query_relayed_and_lua_routed_local_only(tmp_path):
    async def body():
        with KX2Sim() as sim:
            state, kx2, emu = await setup(tmp_path, sim, default="kx2")
            wt = WinTest(str(tmp_path / "com1"))
            assert await wt.ask("OM;") == "OM ------------06"     # relayed to the KX2
            wt.send("SWH19;")                                    # Lua script, local-only -> KX2
            assert await wait_for(lambda: "SWH19" in sim.received)
            wt.send("K30;AI0;")                                  # Win-Test settings stay local
            await asyncio.sleep(0.1)
            assert sim.k3 == 1 and sim.ai == 2
            wt.close()
            await emu.stop(); await kx2.stop()
    run(body())


def test_nr_via_ds_k31_and_scheduler_priorities(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            await asyncio.sleep(1.0)
            sim.toggle_nr()
            assert await wait_for(lambda: state.get("nr") == 1, 1.5)
            fa = len(sim.frames_with("FA"))
            ks = len(sim.frames_with("KS"))
            assert fa >= 8 * max(ks, 1)                         # fast vs slow priority
            assert not [f for f in sim.received if f == "AP"] or state.get("mode") == "CW"
            await emu.stop(); await kx2.stop()
    run(body())


def test_kx2_silence_does_not_block_wintest(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as sim:
            state, kx2, emu = await setup(tmp_path, sim)
            assert await wait_for(lambda: "freq_a" in state)
            sim.busy(1.5)                                       # band change: KX2 silent
            wt = WinTest(str(tmp_path / "com1"))
            for _ in range(5):
                assert (await wt.ask("IF;", timeout=0.2)) is not None
            wt.close()
            await emu.stop(); await kx2.stop()
    run(body())

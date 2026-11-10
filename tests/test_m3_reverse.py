"""Milestone M3: rig -> KX2 reverse sync, loop protection, busy suppression, rig master at start."""

import asyncio
import time

from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import WinTest, wait_for
from tests.test_m2_end_to_end import Stack


def run(coro):
    return asyncio.run(coro)


def kx2_sets(ksim, prefix):
    """Set commands the KX2 received (prefix + digits, i.e. not queries)."""
    return [f for f in ksim.received if f.startswith(prefix) and len(f) > len(prefix)]


def test_rig_is_master_at_start(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            ksim.raw.update({"FA": "00007030000", "PA": "0", "GT": "004"})     # KX2 differs
            rsim.raw.update({"FA": "00014025000", "PA": "1", "GC": "2"})
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            assert ksim.raw["FA"] == "00014025000"        # KX2 took the rig's frequency
            assert ksim.raw["PA"] == "1" and ksim.raw["GT"] == "002"
            assert "FA00007030000" not in rsim.received   # the rig was not changed
            assert "PA0" not in rsim.received
            await s.stop()
    run(body())


def test_operator_at_rig_moves_kx2_and_wintest(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            rsim.operator("FA", "00014031500")
            assert await wait_for(lambda: ksim.raw["FA"] == "00014031500", 1.5)
            wt = WinTest(str(tmp_path / "com1"))
            assert (await wt.ask("IF;"))[2:13] == "00014031500"
            wt.close()
            await s.stop()
    run(body())


def test_no_oscillation_while_turning_kx2(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            before = len(kx2_sets(ksim, "FA"))
            f = 14025000
            for _ in range(15):                            # turning the VFO for ~1.2 s
                f += 20
                ksim.knob("FA", "%011d" % f)
                await asyncio.sleep(0.08)
            await asyncio.sleep(1.0)
            assert rsim.raw["FA"] == "%011d" % f
            assert ksim.raw["FA"] == "%011d" % f
            assert len(kx2_sets(ksim, "FA")) == before      # rig echoes never written back
            await s.stop()
    run(body())


def test_lua_filter_script_shows_on_kx2(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            wt = WinTest(str(tmp_path / "com1"))
            wt.send("FW0200;")                              # author's 200HZ.wts
            rsim_fw = await wait_for(lambda: rsim.raw["FW"] == "0200")
            assert rsim_fw
            rsim._ai("FW0200;")                             # the real rig reports it by AI
            assert await wait_for(lambda: ksim.raw["BW"] == "0020", 2.5)
            wt.close()
            await s.stop()
    run(body())


def test_rig_mode_change_keeps_rig_master(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            rig_fw = rsim.raw["FW"]
            rsim.operator("MD", "2")                        # operator switches the rig to USB
            assert await wait_for(lambda: ksim.raw["MD"] == "2", 1.5)
            ksim.knob("BW", "0270")                         # KX2 recalls its USB filter memory
            await asyncio.sleep(2.5)                        # quiet period + resync
            assert "SH" not in "".join(f[:2] for f in rsim.received[-20:] if f.startswith("SH") and len(f) > 2)
            assert ksim.raw["BW"] == "%04d" % (s.rig.state.get("filter_bw") // 10)
            assert rsim.raw["MD"] == "2"
            await s.stop()
    run(body())


def test_atu_tune_does_not_switch_kx2_to_cw(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim(time_scale=0.05) as rsim:
            ksim.raw["MD"] = "2"
            rsim.raw["MD"] = "2"
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            wt = WinTest(str(tmp_path / "com1"))
            wt.send("AC111;")                               # ATTUNE.wts (key 'ä')
            assert await wait_for(lambda: s.sync.remote_state.get("tx") is True, 1.0)
            assert await wait_for(lambda: s.sync.remote_state.get("atu_tuning") is False, 2.0)
            await asyncio.sleep(0.5)
            assert "MD3" not in kx2_sets(ksim, "MD")       # the CW jump was not mirrored
            assert ksim.raw["MD"] == "2"
            wt.close()
            await s.stop()
    run(body())


def test_cw_tune_follow_up(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            before = len(kx2_sets(ksim, "FA"))
            wt = WinTest(str(tmp_path / "com1"))
            wt.send("CA1;")                                 # CWTUNE.wts
            assert await wait_for(lambda: ksim.raw["FA"] == "00014025030", 2.0)
            new = kx2_sets(ksim, "FA")[before:]
            assert new == ["FA00014025030"]                # only the final frequency
            wt.close()
            await s.stop()
    run(body())


def test_rig_nr_toggles_kx2_nr(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            assert await wait_for(lambda: "nr" in s.sync._known, 2.0)
            rsim.operator("NR", "1")
            assert await wait_for(lambda: ksim.nr is True, 2.0)
            assert ksim.received.count("SWH19") == 1
            await asyncio.sleep(1.0)
            assert ksim.received.count("SWH19") == 1        # no toggling back and forth
            await s.stop()
    run(body())


def test_reconnect_resyncs_from_rig(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            await s.link.stop()                             # laptop loses the connection
            assert not s.sync.ready
            rsim.operator("FA", "00014044000")              # rig changed meanwhile
            ksim.knob("FA", "00014020000")                  # and someone turned the KX2
            await asyncio.sleep(0.5)
            assert rsim.raw["FA"] == "00014044000"          # not forwarded while disconnected
            s.link.start()                                  # reconnect
            assert await wait_for(lambda: s.sync.ready, 3.0)
            assert await wait_for(lambda: ksim.raw["FA"] == "00014044000", 2.0)
            await s.stop()
    run(body())


def test_kx2_10hz_rounding_is_not_forwarded(tmp_path):
    """Measured 9 Nov: rig at 14 014 527 Hz (after CW tune); the KX2 stores 14 014 520 and the
    proxy sent that back to the rig. Rounding below 10 Hz must not count as an operator action."""
    async def body():
        with KX2Sim(reply_delay=0.01) as ksim, TS590Sim() as rsim:
            rsim.raw["FA"] = "00014014527"
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            assert ksim.raw["FA"] == "00014014520"          # KX2 rounded
            await asyncio.sleep(1.0)
            assert not [f for f in rsim.received if f.startswith("FA0")]   # rig untouched
            assert s.state.get("freq_a") == 14014527        # Win-Test sees the exact value
            ksim.knob("FA", "00014014530")                  # a real 10 Hz step is forwarded
            assert await wait_for(lambda: rsim.raw["FA"] == "00014014530", 1.0)
            await s.stop()
    run(body())


# ---------------------------------------------------------------------- KX2 A/B swap (10 Nov 2026)
class _FakeLink:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return True


def test_kx2_ab_swap_sends_mode_before_filter_and_no_vfo_b_mode():
    """A/B on the KX2: FA/FB swap and the filter come by Auto-Info, the mode later by poll.
    Measured: the SSB filter was set in the rig's CW mode (FW2000) and FR1;MD;FR0 made the
    receiver twitch."""
    from anyrig.common.state import State
    from anyrig.local.sync import Sync

    async def body():
        state, link = State(), _FakeLink()
        s = Sync(state, link)
        for k, v in (("freq_a", 14015000), ("freq_b", 14000000), ("mode", "CW"), ("mode_b", "USB"),
                     ("filter_bw", 250), ("split", False)):
            state.set(k, v, "kx2")                   # first readings
        s.ready = True
        link.sent.clear()
        state.set("freq_a", 14000000, "kx2")        # A/B tapped
        state.set("freq_b", 14015000, "kx2")
        state.set("filter_bw", 2000, "kx2")         # AI bundle, before the mode
        state.set("mode_b", "CW", "kx2")
        await asyncio.sleep(0.2)
        state.set("mode", "USB", "kx2")             # poll answer
        await asyncio.sleep(0.05)
        keys = [m["k"] for m in link.sent]
        assert keys == ["freq_a", "freq_b", "mode", "filter_bw"]
        assert link.sent[-1]["v"] == 2000
        link.sent.clear()
        state.set("split", True, "wintest")         # split on: now VFO B mode is needed
        assert [m["k"] for m in link.sent] == ["split", "mode_b"]
    asyncio.run(body())

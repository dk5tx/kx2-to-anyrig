"""Kenwood driver with the TS-590SG profile against the TS-590SG simulator."""

import asyncio
import time

from anyrig.drivers.kenwood import KenwoodRig, nearest
from anyrig.remote.main import load_profile
from tests.sim.ts590sim import TS590Sim

PROFILE = load_profile("profiles/ts590sg.yaml")


async def wait_for(pred, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def run(coro):
    return asyncio.run(coro)


def test_decode_real_frames():
    rig = KenwoodRig(PROFILE, "/dev/null")
    assert rig.decode("FA", "00007025000") == {"freq_a": 7025000}
    assert rig.decode("MD", "3") == {"mode": "CW"}
    assert rig.decode("NT", "20") == {"notch": 2}            # measured: NT20 = manual normal
    assert rig.decode("GC", "1") == {"agc": "slow"}
    assert rig.decode("RA", "0100") == {"att": 1}
    assert rig.decode("AC", "111") == {"atu_tuning": True, "atu_on": True}
    assert rig.decode("SD", "0500") == {"break_in_delay": 500}
    assert rig.decode("RG", "255") == {"rf_gain": 1.0}
    assert rig.decode("TX", "0") == {"tx": True} and rig.decode("RX", "") == {"tx": False}
    d = rig.decode("IF", "00014025000     +0120" + "10" + "0" + "00" + "0" + "3" + "0" + "0" + "0" + "0" + "00" + " ")
    assert d["freq_a"] == 14025000 and d["rit_offset"] == 120 and d["rit"] is True and d["mode"] == "CW"


def test_encode_rules():
    rig = KenwoodRig(PROFILE, "/dev/null")
    rig.state.update({"mode": "CW", "rit_offset": 0, "split": False}, "rig")
    assert rig._encode("freq_a", 7039800) == ["FA00007039800;"]
    assert rig._encode("filter_bw", 430) == ["FW0400;"]        # nearest FW step
    assert rig._encode("filter_bw", 2700) == ["FW2500;"]
    assert rig._encode("filter_center", 620) == ["IS 0600;"]
    assert rig._encode("rit_offset", 120) == ["RU00120;"]
    rig.state.set("rit_offset", 120, "rig")
    assert rig._encode("rit_offset", 50) == ["RD00070;"]
    assert rig._encode("rit_offset", 0) == ["RC;"]
    assert rig._encode("agc", "fast") == ["GC2;"]
    assert rig._encode("nr", 1) == ["NR1;"] and rig._encode("nb", 0) == ["NB0;"]
    assert rig._encode("att", 1) == ["RA01;"]
    assert rig._encode("rf_gain", 0.5) == ["RG128;"]
    assert rig._encode("mode", "DATA") == ["MD2;", "DA1;"]
    assert rig._encode("split", True) == ["FR0;", "FT1;"]
    assert rig._encode("mode_b", "CW") == ["FR1;", "MD3;", "FR0;"]
    rig.state.update({"mode": "USB", "sl_index": 2}, "rig")
    assert rig._encode("filter_bw", 2300) == ["SH07;"]          # low cut 100 Hz + 2300 = 2400 Hz high cut
    assert rig._encode("filter_center", 600) is None           # IS only in CW
    assert rig._encode("cw_tune", True) is None                # read-only key
    assert nearest([50, 80, 100], 90) in (1, 2)


def test_driver_reads_state_and_sets(tmp_path):
    async def body():
        with TS590Sim() as sim:
            rig = KenwoodRig(PROFILE, sim.port)
            await rig.open()
            assert rig.state.get("freq_a") == 14025000 and rig.state.get("mode") == "CW"
            assert rig.state.get("filter_bw") == 500
            assert sim.ai == 2
            rig.set("freq_a", 7039800)
            rig.set("filter_bw", 300)
            rig.set("rit_offset", 150)
            assert await wait_for(lambda: sim.raw["FA"] == "00007039800" and sim.raw["FW"] == "0300")
            assert await wait_for(lambda: rig.state.get("rit_offset") == 150)   # via AI IF
            sim.operator("FA", "00007040000")                                    # operator at the rig
            assert await wait_for(lambda: rig.state.get("freq_a") == 7040000)
            await rig.close()
            assert "KY0" in sim.received and sim.ai == 0
    run(body())


def test_meters_unsolicited_and_tx(tmp_path):
    async def body():
        with TS590Sim(time_scale=0.02, sm_period=0.05) as sim:
            rig = KenwoodRig(PROFILE, sim.port)
            meters = []
            rig.on_meters = meters.append
            await rig.open()
            assert await wait_for(lambda: any("s" in m for m in meters))
            rig.send_raw("KY TEST                    ;")
            assert await wait_for(lambda: rig.state.get("tx") is True)
            assert await wait_for(lambda: any(m.get("alc_raw") == 15 for m in meters))
            assert await wait_for(lambda: rig.state.get("tx") is False, 3.0)
            await rig.close()
    run(body())


def test_open_selects_vfo_a_simplex():
    async def body():
        with TS590Sim() as sim:
            sim.raw["FR"] = "1"                 # measured start state: rig on VFO B
            sim.raw["FT"] = "1"
            rig = KenwoodRig(PROFILE, sim.port)
            await rig.open()
            assert "FR0" in sim.received
            assert sim.received.index("FR0") < sim.received.index("FA")
            await rig.close()
    run(body())

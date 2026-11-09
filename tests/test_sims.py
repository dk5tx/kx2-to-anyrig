"""Sanity tests for the simulators themselves (they must model the measured behaviour)."""

import os
import select
import time

from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim


class Client:
    def __init__(self, port):
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.buf = b""

    def send(self, text):
        os.write(self.fd, text.encode("latin-1"))

    def read(self, timeout=0.3, until=None):
        frames = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], 0.01)
            if r:
                self.buf += os.read(self.fd, 4096)
            while b";" in self.buf:
                f, self.buf = self.buf.split(b";", 1)
                frames.append(f.decode("latin-1"))
                if until and frames[-1].startswith(until):
                    return frames
        return frames

    def close(self):
        os.close(self.fd)


def test_kx2_ai_is_bundled_and_slow():
    with KX2Sim(ai_period=0.3) as sim:
        c = Client(sim.port)
        c.send("AI2;")
        time.sleep(0.05)
        t0 = time.monotonic()
        sim.knob("FA", "00014025100")
        sim.knob("FA", "00014025200")
        sim.knob("BW", "0040")
        frames = c.read(1.0, until="FW")
        c.close()
    fa = [f for f in frames if f.startswith("FA")]
    assert fa == ["FA00014025200"]            # only the last value, once
    assert "FW0000" in frames                 # BW is not reported, FW0000 instead
    assert time.monotonic() - t0 < 1.5


def test_kx2_ap_only_in_cw_and_ds_k31():
    with KX2Sim() as sim:
        c = Client(sim.port)
        c.send("AP;")
        assert c.read(0.2) == ["AP0"]
        sim.knob("MD", "2")
        c.send("AP;")
        assert c.read(0.2) == []              # no answer in SSB (measured)
        sim.toggle_nr()
        c.send("DS;")
        ds = c.read(0.2)[0]
        assert ord(ds[-1]) == 0x80            # K30: extra byte always 0x80
        c.send("K31;DS;")
        ds = c.read(0.2)[0]
        assert ord(ds[-1]) & 0x04             # K31: NR bit
        c.close()


def test_ts590_immediate_ai_and_mode_dependent():
    with TS590Sim() as sim:
        c = Client(sim.port)
        c.send("AI2;")
        time.sleep(0.02)
        sim.operator("FA", "00007025000")
        assert "FA00007025000" in c.read(0.2, until="FA")
        c.send("MD2;FW;")
        frames = c.read(0.2, until="?")
        assert "?" in frames
        c.close()


def test_ts590_ky_drops_leading_space_and_keys():
    with TS590Sim(time_scale=0.01) as sim:
        c = Client(sim.port)
        c.send("AI2;")
        c.send("KY " + "CQ CQ TEST DL1ABC DL1ABC" + ";")
        c.send("KY " + " TEST".ljust(24) + ";")
        frames = c.read(2.0, until="RX")
        c.close()
        assert frames[0] == "TX0" or "TX0" in frames
        assert "RX" in frames
        assert "DL1ABCTEST" in sim.sent_text


def test_ts590_atu_sequence_jumps_mode():
    with TS590Sim(time_scale=0.02) as sim:
        c = Client(sim.port)
        c.send("AI2;MD2;")
        c.send("AC111;")
        frames = c.read(1.0, until="AC110")
        c.close()
    keys = [f for f in frames if f[:2] in ("MD", "TX", "RX", "AC")]
    keys = keys[keys.index("AC111"):]
    assert keys == ["AC111", "MD3", "TX0", "RX", "MD2", "AC110"]

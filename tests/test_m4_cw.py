"""Milestone M4: Morse timing, KY packet splitting, WinKeyer protocol, end to end CW."""

import asyncio
import os
import time

from anyrig.common import morse
from anyrig.local.winkeyer import VERSION, WinKeyerEmulator
from anyrig.remote.cwtext import next_packet
from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import wait_for
from tests.test_m2_end_to_end import Stack

# the text from the author's ky_test.py run (8 Nov 2026) that lost a word gap
LONG = "CQ CQ TEST DL1ABC DL1ABC TEST CQ CQ TEST DL1ABC DL1ABC TEST"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------- pure functions
def test_morse_paris():
    assert morse.units("PARIS ") == 50
    assert abs(morse.duration("PARIS ", 20) - 3.0) < 1e-9
    assert morse.units("E") == 1 and morse.units("T") == 3 and morse.units("EE") == 5
    assert morse.elements("ET") == [(True, 1), (False, 3), (True, 3)]


def test_packets_are_complete_words():
    # padding is keyed as a word gap (measured 10 Nov 2026): packets end at word ends
    assert next_packet("CQ", 24, False) is None                       # word may go on
    assert next_packet("CQ ", 24, False) == ("CQ", "")
    assert next_packet("CQ D", 24, False) == ("CQ", "D")
    assert next_packet("TEST", 24, True) == ("TEST", "")              # last word of a message
    text, packets = LONG, []
    while text:
        p, text = next_packet(text, 24, True)
        assert len(p) <= 23 and not p.startswith(" ") and not p.endswith(" ")
        packets.append(p)
    assert packets == ["CQ CQ TEST DL1ABC", "DL1ABC TEST CQ CQ TEST", "DL1ABC DL1ABC TEST"]
    # a word longer than a packet is cut into full (seamless) packets
    assert next_packet("A" * 30, 24, False) == ("A" * 24, "A" * 6)


# ---------------------------------------------------------------------- WinKeyer protocol
class FakeWT:
    """Win-Test side of the WinKeyer pty (bytes)."""

    def __init__(self, device):
        self.fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.rx = bytearray()

    def send(self, data: bytes):
        os.write(self.fd, data)

    async def read(self, n=1, timeout=0.5):
        end = time.monotonic() + timeout
        while time.monotonic() < end and len(self.rx) < n:
            try:
                self.rx += os.read(self.fd, 4096)
            except BlockingIOError:
                pass
            await asyncio.sleep(0.005)
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    async def drain(self, timeout=0.2):
        await asyncio.sleep(timeout)
        try:
            self.rx += os.read(self.fd, 4096)
        except BlockingIOError:
            pass
        out = bytes(self.rx)
        self.rx.clear()
        return out

    def close(self):
        os.close(self.fd)


def test_winkeyer_protocol(tmp_path):
    async def body():
        texts, aborts, speeds = [], [], []
        wk = WinKeyerEmulator(str(tmp_path / "com2"), on_text=texts.append,
                              on_abort=lambda: aborts.append(1), on_speed=speeds.append)
        wk.start()
        wt = FakeWT(str(tmp_path / "com2"))
        wt.send(b"\x00\x02")                                   # host open
        assert await wt.read(1) == bytes([VERSION])
        wt.send(b"\x00\x04\x55")                               # echo test
        assert await wt.read(1) == b"\x55"
        wt.send(bytes([0x0F]) + bytes(range(1, 16)) + b"\x02\x1e")   # load defaults + speed 30
        await asyncio.sleep(0.05)
        assert speeds[-1] == 30
        wt.send(b"\x16\x03\x05\x16\x00\x13")                    # pointer cmds incl. 2-byte variant, null
        wt.send(b"CQ TEST DK5TX\x1bAR")                        # text + merged AR
        assert await wt.read(1) == b"\xc4"                     # busy
        await asyncio.sleep(0.1)
        assert texts == ["CQ TEST DK5TX_"]
        wk.done()
        assert await wt.read(1) == b"\xc0"                     # idle
        wt.send(b"TU\x0a")                                     # text, then clear buffer (ESC)
        await asyncio.sleep(0.1)
        assert aborts and texts == ["CQ TEST DK5TX_"]          # aborted text never sent
        wt.send(b"\x15")                                       # request status
        st = await wt.drain()
        assert st.endswith(b"\xc0")
        wt.close()
        await wk.stop()
    run(body())


# ---------------------------------------------------------------------- end to end
async def cw_stack(tmp_path, ksim, rsim):
    s = await Stack().start(tmp_path, ksim, rsim)
    await s.settle()
    wk = WinKeyerEmulator(str(tmp_path / "com2"), on_text=s.sync.cw_text, on_abort=s.sync.cw_abort,
                          on_speed=s.sync.cw_speed, on_end=s.sync.cw_end)
    s.sync.winkeyer = wk
    wk.start()
    s.wk = wk
    wt = FakeWT(str(tmp_path / "com2"))
    wt.send(b"\x00\x02")
    assert await wt.read(1) == bytes([VERSION])
    return s, wt


def test_macro_keyed_with_all_word_gaps(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=0.05) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"\x02\x3c")                               # 60 WPM keeps the test short
            assert await wait_for(lambda: rsim.raw["KS"] == "060", 1.0)
            assert await wait_for(lambda: ksim.raw["KS"] == "050", 1.0)    # KX2 maximum is 50
            await asyncio.sleep(2.5)                           # KS is polled slowly on the KX2
            assert rsim.raw["KS"] == "060"                     # the KX2 clamp is not sent back
            t0 = time.monotonic()
            wt.send(LONG.encode())                             # F1 macro
            assert await wt.read(1) == b"\xc4"
            assert await wait_for(lambda: "TX0" in rsim.received or rsim.tx, 1.0)
            assert time.monotonic() - t0 < 0.15                # keying starts at once
            done = await wt.read(1, timeout=12.0)
            assert done == b"\xc0"                             # idle after the estimated keying time
            assert rsim.sent_text.strip() == LONG              # no word gap lost
            assert all(not p.startswith(" ") for p in rsim.packets)
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_esc_aborts_immediately(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"CQ CQ CQ TEST DK5TX DK5TX TEST")
            assert await wt.read(1) == b"\xc4"
            assert await wait_for(lambda: rsim.tx, 1.0)
            t0 = time.monotonic()
            wt.send(b"\x0a")                                   # ESC in Win-Test
            assert await wt.read(1) == b"\xc0"
            assert await wait_for(lambda: "KY0" in rsim.received, 0.5)
            assert time.monotonic() - t0 < 0.2
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_cw_blocked_after_link_loss_until_reconnect(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            s.server._safety("test")
            assert s.server.cw.blocked
            s.server.cw.send("TEST", 99)
            await asyncio.sleep(0.1)
            assert not [f for f in rsim.received if f.startswith("KY ") and "TEST" in f]
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_wintest_char_by_char_with_echo(tmp_path):
    """Measured Win-Test behaviour: mode 0x94 (echo on), then one character per echo."""
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=0.05) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"\x0e\x94\x0a\x02\x3c")                   # mode (echo), clear, 60 WPM
            assert await wait_for(lambda: rsim.raw["KS"] == "060", 1.0)
            text = "599 001 CQ DK5TX DK5TX TEST"
            t0 = time.monotonic()
            got = b""
            for i, ch in enumerate(text.encode()):
                wt.send(bytes([ch]))
                while True:                                    # wait for this character's echo
                    b = await wt.read(1, timeout=3.0)
                    assert b, "no echo for %r" % chr(ch)
                    if b < b"\xc0":
                        break
                got += b
                last_echo = time.monotonic()
                if i == 0:
                    assert time.monotonic() - t0 < 0.05        # echo comes at once
                await asyncio.sleep(0.03)                      # Win-Test's own pacing
            assert got == text.encode()
            assert await wait_for(lambda: rsim.tx, 0.5)
            for _ in range(200):
                if (await wt.read(1, timeout=0.1)) == b"\xc0":
                    break
            assert rsim.sent_text.strip() == text              # no gap lost, none inserted
            packets = [p.rstrip(" ") for p in rsim.packets]    # strip the padding
            assert all(not p.startswith(" ") for p in packets)
            assert " ".join(packets) == text                   # every packet ends at a word end
            # Win-Test must not see the message as sent long before it is keyed
            assert last_echo - t0 > 0.4 * morse.duration(text, 60)
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


async def wt_send(wt, text: bytes, first: bytes = b"\x0a", timeout=5.0):
    """Like Win-Test: clear + first character, then one character per echo. Returns echo times."""
    times = []
    for i, ch in enumerate(text):
        wt.send((first if i == 0 else b"") + bytes([ch]))
        while True:
            b = await wt.read(1, timeout=timeout)
            assert b, "no echo for %r" % chr(ch)
            if b < b"\xc0":
                break
        times.append(time.monotonic())
        await asyncio.sleep(0.05)
    return times


def test_cq_repeat_keeps_the_interval(tmp_path):
    """Win-Test's CQ repeat ('clear' + text, a fixed time after the last echo) must neither cut
    the running CQ nor shorten the pause: Win-Test sees the message as sent about a word early."""
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"\x0e\xb4\x02\x30")                       # echo on, 48 WPM
            assert await wait_for(lambda: rsim.raw["KS"] == "048", 1.0)
            msg, interval, gaps = b"CQ DL1ABC TEST", 1.2, []
            done = None
            for cycle in range(3):
                echoes = await wt_send(wt, msg, timeout=5.0)
                if done is not None:
                    gaps.append(echoes[0] - done)
                # Win-Test: repeat 'interval' after the last echo; the rig is still keying then
                next_at = echoes[-1] + interval
                st = b""
                while time.monotonic() < next_at:
                    b = await wt.read(1, timeout=max(0.01, next_at - time.monotonic()))
                    if b == b"\xc0":
                        done = time.monotonic()
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))
            assert "KY0" not in rsim.received                  # no CQ was cut
            assert await wait_for(lambda: not rsim.tx, 8.0)
            assert rsim.sent_text.split() == (msg.decode() + " ") .split() * 3
            # from the second repeat on, the pause after the rig's end is Win-Test's interval
            assert abs(gaps[-1] - interval) < 0.2, gaps
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_keyboard_mode_typing_is_appended(tmp_path):
    """Keyboard mode: 'clear' + character for every key while the rig still keys."""
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"\x0e\xb4\x02\x1e")                       # echo on, 30 WPM
            assert await wait_for(lambda: rsim.raw["KS"] == "030", 1.0)
            for ch in b"TEST 73":                              # typed faster than keyed
                await wt_send(wt, bytes([ch]))
                await asyncio.sleep(0.05)                      # ~10 keys per second
            await asyncio.sleep(3.0)
            # nothing lost, nothing aborted; only the first letter may stand alone (the rig was
            # idle, nobody knows yet that a word follows)
            assert rsim.sent_text.replace(" ", "") == "TEST73"
            assert rsim.sent_text.split()[-1] == "73"
            assert len(rsim.sent_text.split()) <= 3
            assert "KY0" not in rsim.received
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_keyboard_two_quick_keys_after_a_pause(tmp_path):
    """Measured: 'TEST', wait until keyed, then '73' typed 29 ms apart -> only '3' came."""
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            wt.send(b"\x0e\xb4\x02\x30")
            assert await wait_for(lambda: rsim.raw["KS"] == "048", 1.0)
            for ch in b"TEST":
                await wt_send(wt, bytes([ch]))
                await asyncio.sleep(0.1)                       # typed, ~6 keys per second
            assert await wait_for(lambda: rsim.sent_text.strip() == "TEST", 3.0)
            assert await wait_for(lambda: not s.wk.busy, 3.0)
            await wt_send(wt, b"7")
            wt.send(b"\x0a3")                                  # 2nd key right after the echo
            assert await wait_for(lambda: rsim.sent_text.split() == ["TEST", "73"], 4.0), \
                rsim.sent_text
            assert "KY0" not in rsim.received
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_configured_cq_repeat_keeps_first_pause(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s, wt = await cw_stack(tmp_path, ksim, rsim)
            s.wk.cq_repeat_s = 1.5
            wt.send(b"\x0e\xb4\x02\x30")
            assert await wait_for(lambda: rsim.raw["KS"] == "048", 1.0)
            msg = b"CQ DL1ABC TEST"
            echoes = await wt_send(wt, msg)
            done = None
            while time.monotonic() < echoes[-1] + 1.5:
                b = await wt.read(1, timeout=max(0.01, echoes[-1] + 1.5 - time.monotonic()))
                if b == b"\xc0":
                    done = time.monotonic()
            assert done is not None                            # rig finished before the repeat
            echoes2 = await wt_send(wt, msg)
            assert abs(echoes2[0] - done - (1.5 - s.wk.repeat_comp)) < 0.15
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())

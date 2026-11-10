"""DTR keyer (remote cw.method: dtr) and the WinKeyer emulation with echo from the remote."""

import asyncio
import time

from anyrig.common import morse
from anyrig.local.winkeyer import WinKeyerEmulator
from anyrig.remote.dtrkeyer import DTRKeyer
from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import wait_for
from tests.test_m2_end_to_end import Stack
from tests.test_m4_cw import FakeWT


def run(coro):
    return asyncio.run(coro)


class FakeLine:
    """Records key/PTT transitions with time stamps (no hardware)."""

    def __init__(self, ptt=False):
        self.events = []                 # (t, "key"/"ptt", on)
        self.key_down = False
        self.ptt_on = False
        self.has_ptt = ptt

    def key(self, down):
        if down != self.key_down:
            self.events.append((time.monotonic(), "key", down))
        self.key_down = down

    def ptt(self, on):
        if self.has_ptt and on != self.ptt_on:
            self.events.append((time.monotonic(), "ptt", on))
        self.ptt_on = on

    def release(self):
        self.key(False)
        self.ptt(False)

    def marks(self):
        """[(start, length)] of key-down periods."""
        out, t0 = [], None
        for t, what, on in self.events:
            if what != "key":
                continue
            if on:
                t0 = t
            elif t0 is not None:
                out.append((t0, t - t0))
                t0 = None
        return out

    def text(self, wpm):
        """Decode the keyed marks back to text."""
        unit = 1.2 / wpm
        rev = {v: k for k, v in morse.CODE.items()}
        out, code, prev_end = "", "", None
        for start, length in self.marks():
            if prev_end is not None:
                gap = start - prev_end
                if gap > 2 * unit:
                    out += rev.get(code, "?")
                    code = ""
                    if gap > 5 * unit:
                        out += " "
            code += "." if length < 2 * unit else "-"
            prev_end = start + length
        return out + rev.get(code, "?") if code else out


async def keyer(wpm=20, ptt=False, **cfg):
    line = FakeLine(ptt)
    done, echo = [], []
    k = DTRKeyer(line, lambda: wpm, cfg, on_done=done.extend, on_echo=echo.append)
    k.start()
    return k, line, done, echo


def test_timing_paris():
    async def body():
        k, line, done, echo = await keyer(20)
        k.send("PARIS PARIS", 1)
        assert await wait_for(lambda: done == [1], 8.0)
        assert line.text(20) == "PARIS PARIS"
        unit = 1.2 / 20
        for start, length in line.marks():
            assert min(abs(length - unit), abs(length - 3 * unit)) < 0.006
        m = line.marks()
        # P-A-R-I-S P: word gap 7 units between the S and the second P
        s_end = m[13][0] + m[13][1]
        assert abs(m[14][0] - s_end - 7 * unit) < 0.008
        assert echo == list("PARIS PARIS")
        await k.stop()
        assert not line.key_down
    run(body())


def test_abort_releases_at_once_and_clear_finishes_char():
    async def body():
        k, line, done, echo = await keyer(20)
        k.send("TEST TEST", 1)
        await asyncio.sleep(0.5)
        t0 = time.monotonic()
        k.abort()
        assert not line.key_down
        await asyncio.sleep(0.3)
        assert all(t <= t0 + 0.002 for t, w, on in line.events if on)   # nothing new keyed
        assert done == [1]
        k.send("O", 2)                                   # O = 3 dahs, then clear during it
        await asyncio.sleep(0.25)
        k.clear()
        k.send("", None)
        assert await wait_for(lambda: done == [1, 2], 3.0)
        assert line.text(20).endswith("O")                # the O was finished
        await k.stop()
    run(body())


def test_ptt_lead_tail_and_buffered_speed():
    async def body():
        k, line, done, echo = await keyer(20, ptt=True, ptt_lead_ms=40, ptt_tail_ms=100)
        k.send("E", 1)
        k.set_buffered_wpm(40)
        k.send("EE", 2)
        assert await wait_for(lambda: done == [1, 2], 3.0)
        await asyncio.sleep(0.3)
        ev = line.events
        assert ev[0][1:] == ("ptt", True) and ev[1][1:] == ("key", True)
        assert ev[1][0] - ev[0][0] >= 0.039
        assert ev[-1][1:] == ("ptt", False)
        last_up = [t for t, w, on in ev if w == "key" and not on][-1]
        # PTT is held through a character gap plus the grace time (more text may still come)
        assert 0.2 < ev[-1][0] - last_up < 0.3
        marks = line.marks()
        assert abs(marks[0][1] - 0.06) < 0.006 and abs(marks[-1][1] - 0.03) < 0.006   # 20 -> 40 WPM
        await k.stop()
    run(body())


# ---------------------------------------------------------------------- end to end with Win-Test
async def dtr_stack(tmp_path, ksim, rsim, line):
    s = await Stack().start(tmp_path, ksim, rsim, rcfg_extra={"cw": {"method": "dtr"}}, keyline=line)
    await s.settle()
    sync = s.sync
    wk = WinKeyerEmulator(str(tmp_path / "com2"), on_text=sync.cw_text, on_abort=sync.cw_abort,
                          on_speed=sync.cw_speed, on_end=sync.cw_end, on_clear=sync.cw_clear,
                          on_wpm=sync.cw_wpm, on_ptt=sync.cw_ptt)
    sync.winkeyer = wk
    wk.set_remote_echo(sync.remote_info.get("cw") == "dtr")
    wk.start()
    s.wk = wk
    wt = FakeWT(str(tmp_path / "com2"))
    wt.send(b"\x00\x02")
    assert await wt.read(1)
    wt.send(b"\x0e\xb4\x02\x1e")                        # echo on, 30 WPM
    assert await wait_for(lambda: rsim.raw["KS"] == "030", 1.0)
    return s, wt


def test_wintest_completes_a_call_typed_while_sending(tmp_path):
    """Measured with DTR from Win-Test: type DL, [Ins], 1XYZ -> Win-Test sends DL1XYZ 5NN.
    Win-Test takes each next character from the entry field after the echo of the previous one."""
    async def body():
        line = FakeLine()
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s, wt = await dtr_stack(tmp_path, ksim, rsim, line)
            assert s.wk.remote_echo
            field = "DL"                                 # what is typed so far
            t0 = time.monotonic()
            sent = ""
            first = True
            while True:
                if time.monotonic() - t0 > 0.3:
                    field = "DL1XYZ"                     # the rest typed 0.3 s after [Ins]
                nxt = field[len(sent):len(sent) + 1] if len(sent) < len(field) else None
                if nxt is None:
                    break
                wt.send((b"\x0a" if first else b"") + nxt.encode())
                first = False
                while True:                              # wait for the echo of this character
                    b = await wt.read(1, timeout=2.0)
                    assert b, "no echo"
                    if b < b"\xc0":
                        break
                sent += nxt
                await asyncio.sleep(0.055)               # Win-Test's pace after an echo
            for ch in b" 5NN":                           # then the report
                wt.send(bytes([ch]))
                while (await wt.read(1, timeout=2.0)) >= b"\xc0":
                    pass
                await asyncio.sleep(0.055)
            assert sent == "DL1XYZ"
            assert await wait_for(lambda: not s.wk.busy, 5.0)
            assert line.text(30) == "DL1XYZ 5NN"
            assert "KY" not in "".join(f for f in rsim.received if f.startswith("KY "))
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_dtr_esc_and_safety(tmp_path):
    async def body():
        line = FakeLine()
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s, wt = await dtr_stack(tmp_path, ksim, rsim, line)
            s.server.cw.send("TEST TEST TEST", 1)
            await asyncio.sleep(0.3)
            wt.send(b"\x0a")                             # ESC
            await asyncio.sleep(0.1)
            n = len(line.events)
            assert not line.key_down
            await asyncio.sleep(0.3)
            assert len(line.events) == n                 # nothing keyed after ESC
            s.server.cw.send("TEST", 2)
            await asyncio.sleep(0.15)
            s.server._safety("test")
            assert not line.key_down and s.server.cw.blocked
            n = len(line.events)
            s.server.cw.send("TEST", 3)
            await asyncio.sleep(0.3)
            assert len(line.events) == n
            wt.close()
            await s.wk.stop()
            await s.stop()
    run(body())


def test_ptt_and_busy_held_through_word_gap_while_text_trickles_in():
    """Measured: Win-Test sends the next character ~55 ms after the echo; with a 20 ms PTT tail
    the PTT dropped in every word gap and the rig went to RX in the middle of a CQ."""
    async def body():
        k, line, done, echo = await keyer(26, ptt=True, ptt_lead_ms=10, ptt_tail_ms=20)
        for ch in "CQ DL1AB":                          # one character per echo, like Win-Test
            n = len(echo)
            k.send(ch, None)
            assert await wait_for(lambda: len(echo) > n, 2.0)
            await asyncio.sleep(0.07)                  # 55 ms + network
        k.send("", 1)
        assert await wait_for(lambda: done == [1], 4.0)
        ptt = [(t, on) for t, w, on in line.events if w == "ptt"]
        assert ptt[0][1] and len(ptt) == 2             # on once, off once at the end
        assert line.text(26) == "CQ DL1AB"
        await k.stop()
    run(body())


def test_echo_comes_late_in_the_character():
    """Win-Test takes the next character from the entry field after the echo: echo as late as the
    budget allows (char + gap - budget), so a call typed while sending is completed."""
    async def body():
        line = FakeLine()
        echo_t = []
        k = DTRKeyer(line, lambda: 20, {"echo_budget_ms": 220},
                     on_echo=lambda ch: echo_t.append(time.monotonic()))
        k.start()
        k.send("O", 1)                                  # 11 units + 3 gap at 20 WPM = 0.84 s
        assert await wait_for(lambda: echo_t, 2.0)
        first_down = line.marks()[0][0]
        assert abs(echo_t[0] - first_down - (0.84 - 0.22)) < 0.02
        await k.stop()
    run(body())

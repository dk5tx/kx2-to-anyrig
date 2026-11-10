"""Simulated Kenwood TS-590SG, modelling the measured behaviour (CLAUDE.md 3.3):

- AI2: every change is reported immediately.
- Meters: RX -> SM periodically; TX -> RM1/RM2/RM3 + SM every 60 ms.
- Mode-dependent commands answer '?' (FW/IS only CW/FSK, SH/SL only SSB/AM/FM, NL/RL only if NB/NR on).
- KY: 24-char packets; a space at the start of a packet is dropped; buffer of two packets;
  TX0 ~12 ms after the first packet; RX after the text is keyed + break-in delay; KY0 aborts.
- AC111 (ATU tune): MD3, TX0, ..., RX, mode restored, AC110.
- CA1 (CW tune): a few FA steps, then CA0.
Keying time is scaled by ``time_scale`` to keep tests fast.
"""

from __future__ import annotations

import time

from .base import PtySim

CW_MODES = ("3", "7", "6", "9")
VOICE_MODES = ("1", "2", "4", "5")


class TS590Sim(PtySim):
    def __init__(self, reply_delay: float = 0.0, time_scale: float = 0.05, sm_period: float = 0.5):
        super().__init__(reply_delay)
        self.time_scale = time_scale
        self.sm_period = sm_period
        self.ai = 0
        self.raw: dict[str, str] = {
            "FA": "00014025000", "FB": "00014030000", "FR": "0", "FT": "0", "MD": "3", "DA": "0",
            "FW": "0500", "SH": "07", "SL": "03", "IS": " 0600", "RT": "0", "XT": "0", "RA": "0000",
            "PA": "1", "NB": "0", "NL": "005", "NR": "0", "RL": "05", "NT": "00", "BP": "064", "BC": "0",
            "GC": "2", "GT": "05", "AG": "0100", "RG": "255", "KS": "026", "SD": "0500", "VX": "1",
            "PC": "050", "TP": "010", "AC": "110", "CA": "0", "AN": "199", "SM": "00005", "FV": "1.04",
            "ID": "023",
        }
        self.tx = False
        self.sent_text = ""               # characters actually keyed
        self.packets: list[str] = []
        self._ky_queue: list[str] = []
        self._key_until = 0.0
        self._rx_at = 0.0
        self._next_sm = 0.0
        self._next_rm = 0.0
        self._atu_steps: list[tuple[float, str]] = []
        self._ca_steps: list[tuple[float, str]] = []
        self.rit_offset = 0

    # --- helpers
    def _ai(self, text: str) -> None:
        if self.ai:
            self.send(text)

    def operator(self, key: str, raw: str) -> None:
        """Simulate the operator changing something on the rig (reported by AI)."""
        with self.lock:
            self.raw[key] = raw
            self._ai(key + raw + ";")

    def _mode_ok(self, key: str) -> bool:
        md = self.raw["MD"]
        if key in ("FW", "IS"):
            return md in CW_MODES
        if key in ("SH", "SL"):
            return md in VOICE_MODES
        if key == "NL":
            return self.raw["NB"] != "0"
        if key == "RL":
            return self.raw["NR"] != "0"
        return True

    def _if(self) -> str:
        off = "%+05d" % self.rit_offset
        return ("IF" + self.raw["FA"] + "     " + off + self.raw["RT"] + self.raw["XT"] + "0" + "00"
                + ("1" if self.tx else "0") + self.raw["MD"] + "0" + "0" + "0" + "0" + "0" + "0 ")

    def _start_tx(self, now: float) -> None:
        if not self.tx:
            self.tx = True
            self._ai("TX0;")
            self._next_rm = now

    def _stop_tx(self) -> None:
        if self.tx:
            self.tx = False
            self._ai("RX;")

    # --- command handling
    def handle(self, frame: str) -> None:
        now = time.monotonic()
        key, arg = frame[:2], frame[2:]
        if key == "AI":
            if arg:
                self.ai = int(arg[:1])
            else:
                self.send("AI%d;" % self.ai)
            return
        if key == "KY":
            if arg == "":
                self.send("KY%d;" % (1 if len(self._ky_queue) >= 2 else 0))
            elif arg == "0":
                self._ky_queue = []
                self._key_until = now
                self._rx_at = now + int(self.raw["SD"]) / 1000.0 * self.time_scale
            else:
                text = arg[1:] if arg.startswith(" ") else arg      # "KY " + 24 chars
                self.packets.append(text)
                # Kenwood: blank (padding) positions are not keyed; measured: a leading space is
                # dropped. Packets are joined seamlessly.
                text = text.rstrip(" ")
                text = text.lstrip(" ")
                self._ky_queue.append(text)
                if not self.tx:
                    self._start_tx(now)
                    self._key_until = now
            return
        if key == "RM" and not arg:
            self.send("RM10000;RM20000;RM30000;")
            return
        if key == "SM" and arg in ("", "0"):
            self.send("SM" + self.raw["SM"] + ";")
            return
        if key == "AG" and arg == "0":
            self.send("AG" + self.raw["AG"] + ";")
            return
        if key == "IF" and not arg:
            self.send(self._if() + ";")
            return
        if key == "TX":
            self._start_tx(now)
            return
        if key == "RX":
            self._ky_queue = []
            self._stop_tx()
            return
        if key == "RC":
            self.rit_offset = 0
            self._ai(self._if() + ";")
            return
        if key in ("RU", "RD") and arg:
            self.rit_offset += int(arg) * (1 if key == "RU" else -1)
            self._ai(self._if() + ";")
            return
        if key in ("UP", "DN"):
            step = int(arg or "10") * (1 if key == "UP" else -1) * 5
            self.raw["FA"] = "%011d" % (int(self.raw["FA"]) + step)
            self._ai("FA" + self.raw["FA"] + ";")
            return
        if key == "AC" and arg == "111":
            prev_md = self.raw["MD"]
            self.raw["AC"] = "111"
            self._ai("AC111;")
            s = self.time_scale
            self._atu_steps = [
                (now + 0.01, "MD3"), (now + 0.02, "TX"), (now + 2.0 * s + 0.05, "RX"),
                (now + 2.0 * s + 0.06, "MD" + prev_md), (now + 2.0 * s + 0.07, "AC110"),
            ]
            return
        if key == "CA" and arg == "1":
            self.raw["CA"] = "1"
            self._ai("CA1;")
            fa = int(self.raw["FA"])
            self._ca_steps = [(now + 0.02 * i, "FA%011d" % (fa + 10 * i)) for i in range(1, 4)]
            self._ca_steps.append((now + 0.1, "CA0"))
            return
        if key in self.raw:
            if not arg:
                if self._mode_ok(key):
                    self.send(key + self.raw[key] + ";")
                else:
                    self.send("?;")
            else:
                if not self._mode_ok(key):
                    self.send("?;")
                    return
                self.raw[key] = arg
                # measured: the TS-590SG confirms every CAT set by Auto-Info
                self._ai(key + arg + ";")
            return
        if not arg:
            self.send("?;")

    def tick(self, now: float) -> None:
        # keying
        if self.tx and now >= self._key_until:
            if self._ky_queue:
                text = self._ky_queue.pop(0)
                self.sent_text += text
                wpm = int(self.raw["KS"])
                self._key_until = now + len(text) * 10 * (1.2 / wpm) * self.time_scale
                self._rx_at = self._key_until + int(self.raw["SD"]) / 1000.0 * self.time_scale
            elif self._rx_at and now >= self._rx_at and not self._atu_steps:
                self._rx_at = 0.0
                self._stop_tx()
        # meters
        if self.ai and self.tx and now >= self._next_rm:
            self._next_rm = now + 0.06
            self.send("RM10001;RM20000;RM30015;SM00012;")
        elif self.ai and not self.tx and now >= self._next_sm:
            self._next_sm = now + self.sm_period
            self.send("SM" + self.raw["SM"] + ";")
        # scripted sequences
        for steps in (self._atu_steps, self._ca_steps):
            while steps and now >= steps[0][0]:
                _, what = steps.pop(0)
                if what == "TX":
                    self._start_tx(now)
                    self._rx_at = 0.0
                elif what == "RX":
                    self._stop_tx()
                else:
                    k, v = what[:2], what[2:]
                    self.raw[k] = v
                    self._ai(what + ";")

"""Simulated Elecraft KX2, modelling the measured behaviour (CLAUDE.md 3.1):

- Auto-Info (AI1/AI2) is sent only once per ``ai_period`` (1 s on the real rig), bundled.
- ``BW``, ``NL``, ``LK`` are never reported by AI; AI sends ``FW0000`` instead of BW.
- ``AP`` is only answered in CW (no answer otherwise).
- ``DS`` extra byte carries NR/notch/CWT only in K31.
- Optional reply delay (real KX2 ~25 ms).

Use ``knob(key, raw)`` to simulate the operator changing something on the front panel.
"""

from __future__ import annotations

from .base import PtySim

NO_AI = {"BW", "NL", "LK"}


class KX2Sim(PtySim):
    def __init__(self, reply_delay: float = 0.0, ai_period: float = 1.0, freq_step: int = 10):
        super().__init__(reply_delay)
        self.freq_step = freq_step          # measured: the KX2 keeps frequencies in 10 Hz steps
        self.ai_period = ai_period
        self.ai = 0
        self.k3 = 0
        self.raw: dict[str, str] = {
            "FA": "00014025000", "FB": "00014030000", "MD": "3", "MD$": "3", "BW": "0050",
            "IS": " 0600", "RT": "0", "XT": "0", "RO": "+0000", "RA": "00", "PA": "0", "AG": "030",
            "RG": "250", "NB": "0", "KS": "026", "PC": "000", "FT": "0", "FR": "0", "TQ": "0",
            "GT": "004", "AN": "1", "NL": "0000", "LK": "0", "OM": " ------------06",
        }
        self.nr = False
        self.notch = False
        self.cwt = False
        self.apf = False
        self.tx_test = True
        self._ai_dirty: list[str] = []
        self._next_ai = 0.0

    # --- operator actions
    def knob(self, key: str, raw: str) -> None:
        with self.lock:
            self.raw[key] = raw
            if key not in self._ai_dirty:
                self._ai_dirty.append(key)

    def toggle_nr(self) -> None:
        with self.lock:
            self.nr = not self.nr

    def set_apf(self, on: bool) -> None:
        with self.lock:
            self.apf = on
            self._ai_dirty.append("IC")

    # --- derived responses
    def _if(self) -> str:
        off = self.raw["RO"]
        return ("IF" + self.raw["FA"] + "     " + off + self.raw["RT"] + self.raw["XT"] + " 00" + "0"
                + self.raw["MD"] + self.raw["FR"] + "0" + self.raw["FT"] + "001 ")

    def _ic(self) -> str:
        a = 0x80 | (0x20 if self.tx_test else 0)
        c = 0x80 | 0x14 | (0x20 if self.apf else 0)
        return "IC" + "".join(chr(x) for x in (a, 0x80, c, 0xC0, 0x82))

    def _ds(self) -> str:
        text = "14025.00"
        chars = [ord(ch) for ch in text.replace(".", "")][:8]
        while len(chars) < 8:
            chars.append(ord("@"))
        icon = 0x80 | (0x40 if self.raw["NB"] == "1" else 0) | (0x10 if self.raw["PA"] == "1" else 0)
        flash = 0x80
        if self.k3 == 1:
            flash |= (0x08 if self.cwt else 0) | (0x04 if self.nr else 0) | (0x02 if self.notch else 0)
        return "DS" + "".join(chr(c) for c in chars) + chr(icon) + chr(flash)

    def answer(self, key: str) -> str | None:
        if key == "IF":
            return self._if()
        if key == "IC":
            return self._ic()
        if key == "DS":
            return self._ds()
        if key == "AI":
            return "AI%d" % self.ai
        if key == "K3":
            return "K3%d" % self.k3
        if key == "AP":
            return ("AP%d" % self.apf) if self.raw["MD"] in ("3", "7") else None   # no answer in SSB
        if key in self.raw:
            return key + self.raw[key]
        return "?"

    def handle(self, frame: str) -> None:
        key = frame[:3] if len(frame) >= 3 and frame[2] == "$" else frame[:2]
        arg = frame[len(key):]
        if key == "AI":
            if arg:
                self.ai = int(arg[:1])
                return
        elif key == "K3":
            if arg:
                self.k3 = int(arg[:1])
                return
        elif key in ("SWT", "SWH") or frame.startswith("SW"):
            if frame == "SWH19":
                self.nr = not self.nr
            return
        elif key == "RC" and not arg:
            self.raw["RO"] = "+0000"
            return
        elif arg and key in self.raw:
            if key in ("FA", "FB") and arg.isdigit() and self.freq_step > 1:
                arg = "%011d" % (int(arg) // self.freq_step * self.freq_step)
            self.raw[key] = arg
            return
        elif arg:
            return
        ans = self.answer(key)
        if ans is not None:
            self.send(ans + ";")

    def tick(self, now: float) -> None:
        if not self.ai or not self._ai_dirty or now < self._next_ai:
            return
        self._next_ai = now + self.ai_period
        out = []
        for key in self._ai_dirty:
            if key in NO_AI:
                if key == "BW":
                    out.append("FW0000;")
                continue
            ans = self.answer(key)
            if ans and ans != "?":
                out.append(ans + ";")
        self._ai_dirty = []
        if out:
            self.send("".join(out))

"""Kenwood ASCII CAT driver (TS-590S/SG and similar), driven by a YAML model profile.

Measured TS-590SG behaviour (CLAUDE.md 3.3): AI2 reports every change immediately, meters arrive
unsolicited (SM in RX; RM1/RM2/RM3 + SM in TX), TX0;/RX; mark transmit. So the driver does not
poll: it reads the state once after opening and then follows the Auto-Info stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..common.log import TrafficLog
from ..common.serialport import FrameSplitter, SerialPort
from .base import Rig

log = logging.getLogger("kenwood")


def nearest(values: list[int], target: float) -> int:
    """Index of the value closest to target."""
    return min(range(len(values)), key=lambda i: abs(values[i] - target))


class KenwoodRig(Rig):
    def __init__(self, profile: dict, port: str, baud: int | None = None, traffic: TrafficLog | None = None):
        super().__init__(profile)
        self.port_path = port
        self.baud = int(baud or profile.get("baud", 115200))
        self.traffic = traffic
        self.port: SerialPort | None = None
        self._splitter = FrameSplitter()
        self.modes: dict[str, str] = {str(k): v for k, v in profile["modes"].items()}
        self.mode_codes = {v: k for k, v in self.modes.items()}
        self.commands: dict[str, dict] = profile.get("commands", {})
        self.by_cmd: dict[str, tuple[str, dict]] = {c["cmd"]: (k, c) for k, c in self.commands.items()}
        self.ignore = set(profile.get("ignore", []))
        self.filter = profile.get("filter", {})
        self.meter_max = float(profile.get("meters", {}).get("max", 30))
        self.meters: dict[str, Any] = {}
        self.frames_rx = 0

    # ------------------------------------------------------------------ lifecycle
    async def open(self) -> None:
        self.port = SerialPort(self.port_path, self.baud)
        self.port.open()
        self.port.start_reading(self._on_data)
        for cmd in self.profile.get("init", []):
            self._write(cmd)
        for q in self.profile.get("initial_queries", []):
            self._write(q + ";")
        await asyncio.sleep(0.2)
        log.info("%s on %s: %s", self.profile.get("name", "rig"), self.port_path,
                 "state read" if "freq_a" in self.state else "NO ANSWER – check port/baud")

    async def close(self) -> None:
        if self.port and self.port.fd is not None:
            for cmd in self.profile.get("shutdown", []):
                try:
                    self._write(cmd)
                except OSError:
                    pass
            await asyncio.sleep(0.05)
            self.port.close()

    # ------------------------------------------------------------------ output
    def _write(self, cmd: str) -> None:
        assert self.port is not None
        if not cmd.endswith(";"):
            cmd += ";"
        self.port.write(cmd)
        if self.traffic:
            self.traffic.write("RIG<", cmd[:-1])

    def send_raw(self, cmd: str) -> None:
        self._write(cmd)

    def unkey(self) -> None:
        self._write("KY0;")
        if self.state.get("tx"):
            self._write("RX;")

    def abort_cw(self) -> None:
        self._write("KY0;")

    def capabilities(self) -> set[str]:
        return set(self.commands) | {"mode", "mode_b", "split", "filter_bw", "filter_center", "rit_offset"}

    # ------------------------------------------------------------------ set
    def set(self, key: str, value: Any) -> bool:
        cmds = self._encode(key, value)
        if cmds is None:
            log.debug("cannot set %s=%r", key, value)
            return False
        for c in cmds:
            self._write(c)
        return True

    def _encode(self, key: str, value: Any) -> list[str] | None:
        st = self.state
        if key == "mode":
            return self._encode_mode(value)
        if key == "mode_b":
            # Kenwood has no "mode of VFO B": select B, set mode, go back (FR switches to simplex)
            code = self._mode_cmds(value)
            if code is None:
                return None
            back = ["FR0;", "FT1;"] if st.get("split") else ["FR0;"]
            return ["FR1;"] + code + back
        if key == "split":
            return ["FR0;", "FT1;"] if value else ["FT0;"]
        if key == "filter_bw":
            mode = st.get("mode")
            if mode in self.profile.get("cw_modes", []):
                steps = self.filter["fw_steps"]
                return ["FW%04d;" % steps[nearest(steps, float(value))]]
            if mode in ("USB", "LSB", "FM"):
                sl = self.filter["sl"]
                sh = self.filter["sh"]
                lo_idx = st.get("sl_index", self.filter.get("sl_default", 2))
                hi_idx = nearest(sh, sl[lo_idx] + float(value))
                return ["SH%02d;" % hi_idx]
            return None
        if key == "filter_center":
            if st.get("mode") not in self.profile.get("cw_modes", []):
                return None
            lo, hi = self.filter.get("is_range", [300, 1000])
            step = self.filter.get("is_step", 50)
            v = int(round(float(value) / step) * step)
            return ["IS %04d;" % max(lo, min(hi, v))]
        if key == "rit_offset":
            cur = int(st.get("rit_offset", 0) or 0)
            new = max(-9999, min(9999, int(value)))
            if new == cur:
                return []
            if new == 0:
                return ["RC;"]
            delta = new - cur
            return [("RU%05d;" if delta > 0 else "RD%05d;") % abs(delta)]
        spec = self.commands.get(key)
        if not spec or "set" not in spec:
            return None
        t = spec.get("type")
        if t == "bool":
            v: Any = 1 if value else 0
        elif t == "enum":
            rev = {name: code for code, name in spec["map"].items()}
            if value not in rev:
                return None
            v = rev[value]
        elif t == "scale":
            v = int(round(max(0.0, min(1.0, float(value))) * spec.get("max", 255)))
        else:
            v = int(value)
            if key in self.profile.get("on_levels", {}) and v == 1:
                v = int(self.profile["on_levels"][key])
        return [spec["set"].format(v=v)]

    def _mode_cmds(self, mode: str) -> list[str] | None:
        data_modes = self.profile.get("data_modes", {})
        if mode in data_modes:
            base = self.mode_codes.get(data_modes[mode])
            return [f"MD{base};", "DA1;"] if base else None
        code = self.mode_codes.get(mode)
        if code is None:
            return None
        out = [f"MD{code};"]
        if self.state.get("data") and mode in ("USB", "LSB", "FM", "AM"):
            out.append("DA0;")
        return out

    def _encode_mode(self, mode: str) -> list[str] | None:
        return self._mode_cmds(mode)

    # ------------------------------------------------------------------ input
    def _on_data(self, data: bytes) -> None:
        for frame in self._splitter.feed(data):
            frame = frame.strip("\r\n")
            if frame:
                self._handle(frame)

    def _handle(self, frame: str) -> None:
        self.frames_rx += 1
        if frame == "?":
            return                       # mode-dependent command, not an error (measured)
        key = frame[:3] if frame.startswith("RM") else frame[:2]
        payload = frame[len(key):]
        if key in ("SM", "RM1", "RM2", "RM3"):
            self._meter(key, payload)
            return
        if self.traffic:
            self.traffic.write("RIG>", frame)
        if self.on_raw:
            self.on_raw(frame)
        if key in self.ignore:
            return
        values = self.decode(key, payload)
        for k, v in values.items():
            self.state.set(k, v, "rig")

    def decode(self, key: str, p: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if key == "TX":
            out["tx"] = True
        elif key == "RX":
            out["tx"] = False
        elif key == "MD":
            if p[:1] in self.modes:
                out["mode"] = self.modes[p[:1]]
        elif key == "IF" and len(p) >= 31:
            # same layout as K3: f(11) 5 spaces offset(5) r x P6 P7(2) t m func scan split ...
            try:
                out["freq_a" if p[28:29] != "1" else "freq_b"] = int(p[0:11])
                out["rit_offset"] = int(p[16:21])
            except ValueError:
                pass
            out["rit"] = p[21] == "1"
            out["xit"] = p[22] == "1"
            out["tx"] = p[26] == "1"
            if p[27] in self.modes:
                out["mode"] = self.modes[p[27]]
            out["split"] = p[30] == "1"
        elif key == "FR":
            out["rx_vfo"] = "B" if p[:1] == "1" else "A"
        elif key == "FT":
            out["tx_vfo"] = "B" if p[:1] == "1" else "A"
            out["split"] = out["tx_vfo"] != self.state.get("rx_vfo", "A")
        elif key == "FW":
            if p.strip().isdigit():
                out["filter_bw"] = int(p)
        elif key == "IS":
            try:
                out["filter_center"] = int(p.strip())
            except ValueError:
                pass
        elif key in ("SH", "SL"):
            if p[:2].isdigit():
                idx = int(p[:2])
                out["sh_index" if key == "SH" else "sl_index"] = idx
                sh, sl = self.filter.get("sh", []), self.filter.get("sl", [])
                hi = out.get("sh_index", self.state.get("sh_index"))
                lo = out.get("sl_index", self.state.get("sl_index"))
                if self.state.get("mode") in ("USB", "LSB", "FM") and hi is not None and lo is not None \
                        and hi < len(sh) and lo < len(sl):
                    out["filter_bw"] = sh[hi] - sl[lo]
        elif key == "AC":
            if len(p) >= 3:
                out["atu_tuning"] = p[2] == "1"
                out["atu_on"] = p[1] == "1"
        elif key in self.by_cmd:
            name, spec = self.by_cmd[key]
            sl = spec.get("slice")
            part = p[sl[0]:sl[1]] if sl else p
            t = spec.get("type")
            try:
                if t == "bool":
                    out[name] = part[:1] == "1"
                elif t == "int":
                    out[name] = int(part.strip())
                elif t == "enum":
                    if part[:1] in spec["map"]:
                        out[name] = spec["map"][part[:1]]
                elif t == "scale":
                    out[name] = round(int(part.strip()) / float(spec.get("max", 255)), 3)
                else:
                    out[name] = part
            except ValueError:
                pass
        return out

    def _meter(self, key: str, p: str) -> None:
        try:
            raw = int(p[-4:])
        except ValueError:
            return
        name = {"SM": "po" if self.state.get("tx") else "s", "RM1": "swr", "RM2": "comp", "RM3": "alc"}[key]
        self.meters[name] = round(raw / self.meter_max, 3)
        self.meters[name + "_raw"] = raw
        self.meters["t"] = time.monotonic()
        if self.on_meters:
            self.on_meters(dict(self.meters))

"""Elecraft (K3/KX3/KX2) CAT codec: decode frames into normalized state, encode set commands,
build K3 IF responses.

References: Elecraft K3S/K3/KX3/KX2 Programmer's Reference Rev. G5; measurements in docs/measurements.md.
"""

from __future__ import annotations

from typing import Any

MODES = {"1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM", "6": "DATA", "7": "CW-R", "9": "DATA-R"}
MODE_CODES = {v: k for k, v in MODES.items()}
AGC = {"002": "fast", "004": "slow"}
AGC_CODES = {v: k for k, v in AGC.items()}

# Template for a K3 IF payload (after "IF", before ";"), see CLAUDE.md 3.2
IF_TEMPLATE = "00000000000     +000000 0003000001 "
#              0         1         2         3
#              0123456789012345678901234567890123 4
# f 0-10 | spaces 11-15 | offset 16-20 | r 21 | x 22 | space 23 | "00" 24-25 | t 26 (TX) | m 27 (mode) |
# v 28 (RX VFO, 1 = B) | s 29 (scan) | p 30 (split) | b 31 | d 32 | "1" 33 | space 34


def frame_key(frame: str) -> str:
    """Command key of a frame: two letters, plus '$' for sub-receiver/VFO B variants (e.g. 'MD$')."""
    if len(frame) >= 3 and frame[2] == "$":
        return frame[:3]
    return frame[:2]


def _int(text: str, default: int | None = None) -> int | None:
    text = text.strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def decode(key: str, payload: str, k31: bool = True) -> dict[str, Any]:
    """Decode the payload of a KX2 response/AI frame into normalized state values."""
    p = payload
    out: dict[str, Any] = {}
    if key in ("FA", "FB"):
        v = _int(p)
        if v is not None:
            out["freq_a" if key == "FA" else "freq_b"] = v
    elif key == "MD":
        if p[:1] in MODES:
            out["mode"] = MODES[p[:1]]
    elif key == "MD$":
        if p[:1] in MODES:
            out["mode_b"] = MODES[p[:1]]
    elif key == "BW":
        v = _int(p[:4])
        if v is not None:
            out["filter_bw"] = v * 10
    elif key == "IS":
        v = _int(p)
        if v is not None:
            out["filter_center"] = v
    elif key in ("RT", "XT"):
        if p[:1] in "01" and p[:1]:
            out["rit" if key == "RT" else "xit"] = p[:1] == "1"
    elif key == "RO":
        v = _int(p)
        if v is not None:
            out["rit_offset"] = v
    elif key == "RA":
        v = _int(p[:2])
        if v is not None:
            out["att"] = v
    elif key == "PA":
        v = _int(p[:1])
        if v is not None:
            out["preamp"] = v
    elif key == "NB":
        v = _int(p[:1])
        if v is not None:
            out["nb"] = v
    elif key == "GT":
        if p[:3] in AGC:
            out["agc"] = AGC[p[:3]]
    elif key == "RG":
        v = _int(p[:3])
        if v is not None:
            out["rf_gain_raw"] = v
            out["rf_gain"] = round(min(max(v, 0), 250) / 250.0, 3)
    elif key == "KS":
        v = _int(p[:3])
        if v is not None:
            out["keyer_wpm"] = v
    elif key == "AG":
        v = _int(p[:3])
        if v is not None:
            out["af_gain"] = v
    elif key == "PC":
        v = _int(p[:3])
        if v is not None:
            out["kx2_power"] = v
    elif key == "FT":
        if p[:1] in ("0", "1"):
            out["split"] = p[:1] == "1"
    elif key == "FR":
        if p[:1] in ("0", "1"):
            out["rx_vfo"] = "B" if p[:1] == "1" else "A"
    elif key == "TQ":
        if p[:1] in ("0", "1"):
            out["kx2_tx"] = p[:1] == "1"
    elif key == "VX":
        if p[:1] in ("0", "1"):
            out["vox"] = p[:1] == "1"
    elif key == "AP":
        if p[:1] in ("0", "1"):
            out["apf"] = p[:1] == "1"
    elif key == "IC":
        out.update(decode_ic(p))
    elif key == "DS":
        out.update(decode_ds(p, k31))
    elif key == "IF":
        out.update(decode_if(p))
    return out


def decode_ic(p: str) -> dict[str, Any]:
    """IC status bytes a..e (Programmer's Reference table 4)."""
    if len(p) < 5:
        return {}
    a, _b, c, _d, _e = (ord(ch) for ch in p[:5])
    return {"kx2_tx_test": bool(a & 0x20), "apf": bool(c & 0x20), "full_qsk": bool(c & 0x40)}


def decode_ds(p: str, k31: bool = True) -> dict[str, Any]:
    """DS display: 8 characters (bit 7 = decimal point left of the character), icon byte,
    flash/extra byte (K31 only). Verified: '14\\xB071\\xB84@' is shown as 14.071.84 (14 071 840 Hz)."""
    if len(p) < 10:
        return {}
    text = "".join(("." if ord(ch) & 0x80 else "") + chr(ord(ch) & 0x7F) for ch in p[:8])
    icon = ord(p[8])
    flash = ord(p[9])
    out: dict[str, Any] = {"display": text, "nb_icon": bool(icon & 0x40)}
    if k31:
        out.update({
            "atu_inline": bool(flash & 0x10),
            "kx2_cwt": bool(flash & 0x08),
            "nr": 1 if flash & 0x04 else 0,
            "notch": bool(flash & 0x02),
            "manual_notch": bool(flash & 0x01),
        })
    return out


def decode_if(p: str) -> dict[str, Any]:
    if len(p) < 34:
        return {}
    out: dict[str, Any] = {}
    f = _int(p[0:11])
    if f is not None:
        out["freq_a"] = f
    off = _int(p[16:21])
    if off is not None:
        out["rit_offset"] = off
    out["rit"] = p[21] == "1"
    out["xit"] = p[22] == "1"
    out["kx2_tx"] = p[26] == "1"
    if p[27] in MODES:
        out["mode"] = MODES[p[27]]
    out["rx_vfo"] = "B" if p[28] == "1" else "A"
    out["split"] = p[30] == "1"          # s (index 29) is 'scan', not split
    return out


def build_if(state: dict[str, Any] | Any, template: str | None = None, tx: bool = False) -> str:
    """Build a K3 IF response frame ('IF...' without ';') from normalized state.

    Fields not represented in the state (v, p, b, d) are taken from ``template`` – normally the
    last IF payload received from the real KX2.
    """
    get = state.get
    t = list(template if template and len(template) >= 35 else IF_TEMPLATE)
    freq = int(get("freq_a", 0) or 0)
    t[0:11] = list("%011d" % max(0, min(freq, 99999999999)))
    off = int(get("rit_offset", 0) or 0)
    off = max(-9999, min(off, 9999))
    t[16:21] = list(("+" if off >= 0 else "-") + "%04d" % abs(off))
    t[21] = "1" if get("rit") else "0"
    t[22] = "1" if get("xit") else "0"
    t[26] = "1" if tx else "0"
    t[27] = MODE_CODES.get(get("mode", "CW"), "3")
    t[28] = "1" if get("rx_vfo") == "B" else "0"
    t[29] = "0"                                   # scan: never
    t[30] = "1" if get("split") else "0"
    return "IF" + "".join(t[:35])


def encode_set(key: str, value: Any) -> str | None:
    """Encode a normalized state value as a KX2 set command (with ';'), or None if not settable."""
    if key == "freq_a":
        return "FA%011d;" % int(value)
    if key == "freq_b":
        return "FB%011d;" % int(value)
    if key == "mode":
        code = MODE_CODES.get(value)
        return f"MD{code};" if code else None
    if key == "mode_b":
        code = MODE_CODES.get(value)
        return f"MD${code};" if code else None
    if key == "filter_bw":
        return "BW%04d;" % max(0, min(int(round(int(value) / 10.0)), 9999))
    if key == "filter_center":
        return "IS %04d;" % max(0, min(int(value), 9999))
    if key == "rit":
        return "RT%d;" % (1 if value else 0)
    if key == "xit":
        return "XT%d;" % (1 if value else 0)
    if key == "rit_offset":
        v = max(-9999, min(int(value), 9999))
        return "RO%s%04d;" % ("+" if v >= 0 else "-", abs(v))
    if key == "att":
        return "RA%02d;" % (1 if value else 0)
    if key == "preamp":
        return "PA%d;" % (1 if value else 0)
    if key == "nb":
        return "NB%d;" % (1 if value else 0)
    if key == "agc":
        code = AGC_CODES.get(value)
        return f"GT{code};" if code else None
    if key == "keyer_wpm":
        return "KS%03d;" % max(8, min(int(value), 50))
    if key == "split":
        return "FT%d;" % (1 if value else 0)
    if key == "vox":
        return "VX%d;" % (1 if value else 0)
    if key == "rx_vfo":
        return "FR%d;" % (1 if value == "B" else 0)
    return None


def encode_answer(key: str, state: Any, raw_cache: dict[str, str]) -> str | None:
    """K3-format answer (without ';') for a query from Win-Test, built from state.

    Returns None if the state cannot answer it (caller may use the raw cache or forward the query).
    """
    get = state.get
    if key == "FA" and get("freq_a") is not None:
        return "FA%011d" % get("freq_a")
    if key == "FB" and get("freq_b") is not None:
        return "FB%011d" % get("freq_b")
    if key == "MD" and get("mode") in MODE_CODES:
        return "MD" + MODE_CODES[get("mode")]
    if key == "MD$" and get("mode_b") in MODE_CODES:
        return "MD$" + MODE_CODES[get("mode_b")]
    if key == "FT" and get("split") is not None:
        return "FT%d" % (1 if get("split") else 0)
    if key == "FR" and get("rx_vfo") is not None:
        return "FR%d" % (1 if get("rx_vfo") == "B" else 0)
    if key == "RT" and get("rit") is not None:
        return "RT%d" % (1 if get("rit") else 0)
    if key == "XT" and get("xit") is not None:
        return "XT%d" % (1 if get("xit") else 0)
    if key == "BW" and get("filter_bw") is not None:
        return "BW%04d" % int(round(get("filter_bw") / 10.0))
    return None

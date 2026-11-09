"""Elecraft codec tests with real frames captured on the author's KX2 / Win-Test (Nov 2026)."""

from anyrig.common import elecraft as e
from anyrig.common.state import State

# real IF from catsniff log (Win-Test <-> KX2)
IF_USB = "00014074002     -000000 0002000001 "
IF_CW = "00007026900     -000000 0003000001 "
# real DS from kx2probe log with K31 and NR on: "14.071.84" + icon 0x80 + extra 0x9C
DS_NR = "14\xB071\xB84@\x80\x9C"
# real IC in USB
IC_USB = "\x80\x80\x94\xC0\x82"


def test_frame_key():
    assert e.frame_key("FA00014025000") == "FA"
    assert e.frame_key("MD$3") == "MD$"


def test_decode_if_real_frames():
    d = e.decode("IF", IF_USB)
    assert d["freq_a"] == 14074002 and d["mode"] == "USB"
    assert d["rit_offset"] == 0 and d["rit"] is False and d["split"] is False and d["kx2_tx"] is False
    assert e.decode("IF", IF_CW)["mode"] == "CW"


def test_build_if_matches_real_frame():
    s = State()
    s.update({"freq_a": 14074002, "rit_offset": 0, "rit": False, "xit": False, "mode": "USB", "split": False}, "kx2")
    assert e.build_if(s, template=IF_USB) == "IF" + IF_USB.replace("-0000", "+0000")
    # with the real frame as template and a different state
    s.update({"freq_a": 7039800, "mode": "CW", "rit": True, "rit_offset": -120}, "wintest")
    f = e.build_if(s, template=IF_USB)
    assert f.startswith("IF00007039800     -01201")
    assert f[2 + 27] == "3" and len(f) == 2 + 35


def test_if_split_is_p_not_s():
    # field order t m v s p b d (Programmer's Reference): s = scan, p = split
    d = e.decode_if("00007014900     +000000 0003001001 ")
    assert d["split"] is True and d["rx_vfo"] == "A"
    d = e.decode_if("00007014900     +000000 0003011001 ")      # scan flag must not mean split
    assert d["split"] is True
    d = e.decode_if("00007014900     +000000 0003010001 ")
    assert d["split"] is False
    s = State()
    s.update({"freq_a": 7014900, "mode": "CW", "split": True, "rx_vfo": "A"}, "kx2")
    assert e.build_if(s, template="00007014900     +000000 0003010001 ")[2:] == "00007014900     +000000 0003001001 "


def test_build_if_roundtrip_without_template():
    s = State()
    s.update({"freq_a": 21025000, "mode": "CW-R", "split": True, "rit_offset": 250, "xit": True}, "kx2")
    d = e.decode_if(e.build_if(s)[2:])
    assert d["freq_a"] == 21025000 and d["mode"] == "CW-R" and d["split"] is True
    assert d["rit_offset"] == 250 and d["xit"] is True


def test_decode_ds_k31_nr():
    d = e.decode("DS", DS_NR, k31=True)
    assert d["display"] == "14.071.84@"
    assert d["nr"] == 1 and d["kx2_cwt"] is True and d["atu_inline"] is True
    assert "nr" not in e.decode("DS", DS_NR, k31=False)


def test_decode_ic():
    d = e.decode("IC", IC_USB)
    assert d["apf"] is False
    d = e.decode("IC", "\x80\x80\xB4\xC0\x82")      # byte c bit 5 set (measured APF on)
    assert d["apf"] is True


def test_decode_misc():
    assert e.decode("BW", "0050") == {"filter_bw": 500}
    assert e.decode("RO", "-0120") == {"rit_offset": -120}
    assert e.decode("GT", "002") == {"agc": "fast"}
    assert e.decode("RA", "01") == {"att": 1}
    assert e.decode("FR", "1") == {"rx_vfo": "B"}
    assert e.decode("MD", "x") == {}


def test_encode_set():
    assert e.encode_set("freq_a", 7039800) == "FA00007039800;"
    assert e.encode_set("mode", "CW") == "MD3;"
    assert e.encode_set("filter_bw", 400) == "BW0040;"
    assert e.encode_set("rit_offset", -50) == "RO-0050;"
    assert e.encode_set("agc", "slow") == "GT004;"
    assert e.encode_set("power", 5) is None

from anyrig.common.config import LOCAL_DEFAULTS
from anyrig.local.router import Router


def make(default="remote", **over):
    cfg = dict(LOCAL_DEFAULTS["router"], default=default, **over)
    kx2, remote = [], []
    r = Router(cfg, to_kx2=kx2.append, to_remote=remote.append)
    return r, kx2, remote


def test_author_scripts_go_to_remote():
    r, kx2, remote = make()
    for cmd in ["FW0200", "UP10", "DN10", "RA01", "PA0", "AN919", "AC111", "CA1", "GT05", "NB1"]:
        assert r.route(cmd) == "remote"
    assert kx2 == []
    assert remote[0] == "FW0200;" and "AC111;" in remote


def test_rc_goes_to_both():
    r, kx2, remote = make()
    assert r.route("RC") == "both"
    assert kx2 == ["RC;"] and remote == ["RC;"]


def test_ks_ignored_and_block():
    r, kx2, remote = make()
    assert r.route("KS026") == "ignored"
    for cmd in ["TX0", "TX", "RX", "PS0", "SR1", "KY TEST"]:
        assert r.route(cmd) == "blocked"
    assert kx2 == [] and remote == []


def test_prefix_and_translate():
    r, kx2, remote = make(translate={"SWT34;": "CA1;"})
    assert r.route("@KS020") == "remote"          # prefix bypasses ignore
    assert r.route("SWT34") == "remote"
    assert remote == ["KS020;", "CA1;"]
    assert r.route("@TX0") == "blocked"           # but never bypasses the blocklist


def test_local_only_and_no_remote():
    r, kx2, remote = make(default="kx2")
    assert r.route("SWT34") == "kx2" and kx2 == ["SWT34;"]
    r2 = Router(dict(LOCAL_DEFAULTS["router"]), to_kx2=lambda s: None, to_remote=None)
    assert r2.route("CA1") == "dropped"

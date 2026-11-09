from anyrig.common import config


def test_defaults_without_file():
    cfg = config.load(None, config.LOCAL_DEFAULTS)
    assert cfg["kx2"]["baud"] == 38400
    assert cfg["router"]["ignore"] == ["KS"]


def test_merge(tmp_path):
    p = tmp_path / "local.yaml"
    p.write_text("kx2: {port: /dev/ttyUSB3}\nrouter: {default: kx2}\n")
    cfg = config.load(p, config.LOCAL_DEFAULTS)
    assert cfg["kx2"]["port"] == "/dev/ttyUSB3"
    assert cfg["kx2"]["baud"] == 38400             # default kept
    assert cfg["router"]["default"] == "kx2"
    assert config.LOCAL_DEFAULTS["kx2"]["port"] == "/dev/ttyUSB0"   # defaults untouched


def test_example_configs_load():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config.load(root / "config/local.example.yaml", config.LOCAL_DEFAULTS)
    config.load(root / "config/remote.example.yaml", config.REMOTE_DEFAULTS)

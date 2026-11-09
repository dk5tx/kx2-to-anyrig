"""anyrig-remote: the shack side (home rig)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import yaml

from .. import __version__
from ..common import config, log as logsetup
from ..common.log import TrafficLog
from ..drivers.base import Rig
from .core import RemoteServer

log = logging.getLogger("anyrig-remote")


def load_profile(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        # relative to the repository / package
        alt = Path(__file__).resolve().parents[2] / path
        if alt.exists():
            p = alt
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def make_rig(cfg: dict, traffic: TrafficLog | None = None) -> Rig:
    profile = load_profile(cfg["rig"]["profile"])
    driver = profile.get("driver")
    if driver == "kenwood":
        from ..drivers.kenwood import KenwoodRig
        return KenwoodRig(profile, cfg["rig"]["port"], cfg["rig"].get("baud"), traffic)
    raise SystemExit(f"driver {driver!r} not available yet")


async def run(cfg: dict, stop: asyncio.Event | None = None) -> None:
    traffic = TrafficLog(cfg.get("traffic_log") or None)
    rig = make_rig(cfg, traffic)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await rig.open()
    except OSError as exc:
        log.error("cannot open rig port %s: %s", cfg["rig"]["port"], exc)
        raise SystemExit(1)
    server = RemoteServer(cfg, rig)
    await server.start()
    log.info("anyrig-remote %s running. Ctrl+C to stop.", __version__)
    try:
        await stop.wait()
    finally:
        await server.stop()
        rig.unkey()
        await rig.close()
        traffic.close()
        log.info("stopped")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="anyrig-remote", description="Remote side: controls the home rig")
    ap.add_argument("-c", "--config", help="YAML config (default: built-in defaults)")
    ap.add_argument("--rig-port", help="override rig.port")
    ap.add_argument("--port", type=int, help="override listen.port")
    ap.add_argument("--traffic-log", help="log every frame to/from the rig to this file")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    cfg = config.load(args.config, config.REMOTE_DEFAULTS)
    if args.rig_port:
        cfg["rig"]["port"] = args.rig_port
    if args.port is not None:
        cfg["listen"]["port"] = args.port
    if args.traffic_log:
        cfg["traffic_log"] = args.traffic_log
    logsetup.setup("DEBUG" if args.debug else cfg["log"]["level"], cfg["log"].get("file"))
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()

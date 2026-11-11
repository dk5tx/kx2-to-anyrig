"""Milestone M6: web UI (stdlib HTTP + Server-Sent Events) and safety."""

import asyncio
import json
import time

from anyrig.local.webui.server import WebUI
from tests.sim.kx2sim import KX2Sim
from tests.sim.ts590sim import TS590Sim
from tests.test_local_m1 import wait_for
from tests.test_m2_end_to_end import Stack


def run(coro):
    return asyncio.run(coro)


async def http(port, method, path, body=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode() if body is not None else b""
    w.write(("%s %s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
             % (method, path, len(data))).encode() + data)
    await w.drain()
    resp = await asyncio.wait_for(r.read(), 3)
    w.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


async def first_event(port):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
    await w.drain()
    buf = b""
    while b"\n\n" not in buf.split(b"\r\n\r\n", 1)[-1] or b"data: " not in buf:
        buf += await asyncio.wait_for(r.read(4096), 3)
    w.close()
    line = [l for l in buf.split(b"\n") if l.startswith(b"data: ")][0]
    return json.loads(line[6:])


def test_webui_shows_rig_state_and_sends_allowed_commands(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(sm_period=0.05) as rsim:
            s = await Stack().start(tmp_path, ksim, rsim)
            await s.settle()
            ui = WebUI({"host": "127.0.0.1", "port": 0}, s.state, sync=s.sync, link=s.link)
            await ui.start()
            code, page = await http(ui.port, "GET", "/")
            assert code == 200 and b"<title>anyrig</title>" in page
            ev = await first_event(ui.port)
            assert ev["link"]["connected"] and ev["rig"] == "Kenwood TS-590SG"
            assert ev["state"]["freq_a"] == 14025000 and ev["max_power"] == 100
            # settings go to the rig, the UI then shows what the rig reports
            assert (await http(ui.port, "POST", "/api/set", {"k": "nb", "v": 1}))[0] == 200
            assert await wait_for(lambda: rsim.raw["NB"] == "1", 1.0)
            assert await wait_for(lambda: s.sync.remote_state.get("nb") == 1, 1.0)
            assert (await http(ui.port, "POST", "/api/set", {"k": "power", "v": 200}))[0] == 200
            assert await wait_for(lambda: rsim.raw["PC"] == "100", 1.0)      # remote limit
            assert (await http(ui.port, "POST", "/api/raw", {"d": "CA1;"}))[0] == 200
            assert await wait_for(lambda: "CA1" in rsim.received, 1.0)
            # never: TX commands, unknown keys
            assert (await http(ui.port, "POST", "/api/raw", {"d": "TX;"}))[0] == 400
            assert (await http(ui.port, "POST", "/api/raw", {"d": "KY TEST;"}))[0] == 400
            assert (await http(ui.port, "POST", "/api/set", {"k": "freq_a", "v": 1}))[0] == 400
            assert (await http(ui.port, "GET", "/nothing"))[0] == 404
            await ui.stop()
            await s.stop()
    run(body())


def test_tx_timeout_unkeys(tmp_path):
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim() as rsim:
            s = await Stack().start(tmp_path, ksim, rsim, rcfg_extra={"safety": {"tx_timeout_s": 0.5}})
            await s.settle()
            rsim.operator("TX", "0")                     # stuck in TX (e.g. PTT)
            assert await wait_for(lambda: s.rig.state.get("tx") is True, 1.0)
            t0 = time.monotonic()
            assert await wait_for(lambda: "RX" in rsim.received, 2.0)
            assert 0.4 < time.monotonic() - t0 < 1.0
            await s.stop()
    run(body())


def test_silent_link_unkeys_within_one_second(tmp_path):
    """Network cable pulled: no FIN, the link just goes silent."""
    async def body():
        with KX2Sim(reply_delay=0.005) as ksim, TS590Sim(time_scale=1.0) as rsim:
            s = await Stack().start(tmp_path, ksim, rsim, link_timeout_ms=1000)
            await s.settle()
            s.rig.send_raw("KY " + "TEST TEST TEST TEST TEST" + ";")
            assert await wait_for(lambda: s.rig.state.get("tx") is True)
            s.link.conn.send = lambda msg: True          # nothing reaches the shack any more
            t0 = time.monotonic()
            assert await wait_for(lambda: rsim.received.count("KY0") >= 1, 2.0)
            assert time.monotonic() - t0 < 1.3          # 1 s timeout + 0.1 s check interval
            # (the laptop reconnects right after, which lifts the block again)
            await s.stop()
    run(body())

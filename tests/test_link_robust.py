"""Regression (11 Nov 2026): Win-Test sent FF after re-opening the WinKeyer; the byte became
text 'Y-umlaut', its echo raised UnicodeEncodeError in the link's message handler, the link
task died and kx2-local never reconnected."""

import asyncio
import json

from anyrig.local.link import RemoteLink
from anyrig.local.winkeyer import VERSION, WinKeyerEmulator
from tests.test_m4_cw import FakeWT, run


def test_high_bytes_are_not_text(tmp_path):
    async def body():
        texts = []
        wk = WinKeyerEmulator(str(tmp_path / "com2"), on_text=texts.append,
                              on_abort=lambda: None, on_speed=lambda w: None)
        wk.start()
        wt = FakeWT(str(tmp_path / "com2"))
        wt.send(b"\x00\x03\x00\x00\xff\x00\x02")               # close, nulls, FF, open
        assert await wt.read(1) == bytes([VERSION])
        wt.send(b"\x0e\x04\xffTEST\xe4")                        # echo mode, FF, text, a-umlaut
        await asyncio.sleep(0.6)
        assert "".join(texts) == "TEST"
        wk.echo("Ÿ")                                      # must not raise
        wt.close()
        await wk.stop()
    run(body())


def test_handler_exception_does_not_kill_the_link():
    async def body():
        conns = []

        async def serve(reader, writer):
            conns.append(writer)
            writer.write(b'{"t":"boom"}\n{"t":"ok"}\n')
            await writer.drain()
            await reader.read()                                 # until the client closes

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        link = RemoteLink("127.0.0.1", port)
        got = []

        def on_message(msg):
            if msg["t"] == "boom":
                raise UnicodeEncodeError("latin-1", "Ÿ", 0, 1, "test")
            got.append(msg["t"])

        link.on_message = on_message
        link.start()
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.02)
        assert got == ["ok"]                                    # handler error skipped, link alive
        # the connection drops -> the link must come back
        conns[0].close()
        for _ in range(200):
            if len(conns) >= 2 and link.conn is not None:
                break
            await asyncio.sleep(0.02)
        assert len(conns) >= 2
        await link.stop()
        server.close()
    run(body())

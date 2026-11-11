"""anyrig-ports keeps Win-Test's virtual COM ports across kx2-local restarts."""

import asyncio
import os

from anyrig.common.serialport import PtyPort
from anyrig.local.ports import PortHolder, request_port
from tests.test_m4_cw import run


def _read(fd, timeout=1.0):
    import select
    out = b""
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                out += os.read(fd, 4096)
            except BlockingIOError:
                pass
        elif out:
            break
    return out


def test_ports_survive_a_kx2_local_restart(tmp_path):
    async def body():
        link = str(tmp_path / "com1")
        sock = str(tmp_path / "ports.sock")
        holder = PortHolder([link], sock)
        await holder.start()
        # Win-Test opens the port before kx2-local runs: its writes are drained, never block
        wt = os.open(link, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        for _ in range(50):
            os.write(wt, b"IF;" * 100)
            await asyncio.sleep(0.002)
        slave = os.path.realpath(link)

        PtyPort.holder_socket = sock
        try:
            for round_ in range(2):                    # start, stop, start again
                got = []
                port = PtyPort(link)
                # kx2-local is another process; here the holder shares the loop -> thread
                await asyncio.get_running_loop().run_in_executor(None, port.open)
                assert port.held and port.slave_name == slave
                port.start_reading(got.append)
                os.write(wt, b"FA;")
                for _ in range(50):
                    if got:
                        break
                    await asyncio.sleep(0.01)
                assert b"".join(got) == b"FA;"             # stale polls were dropped
                port.write(b"FA00014025000;")
                assert _read(wt) == b"FA00014025000;"
                port.close()                               # kx2-local ends
                await asyncio.sleep(0.05)
                assert os.path.realpath(link) == slave     # link and pty stay
                os.write(wt, b"IF;" * 50)                  # Win-Test keeps polling
        finally:
            PtyPort.holder_socket = None
        os.close(wt)
        await holder.stop()
        assert not os.path.exists(link)
    run(body())


def test_without_holder_an_own_pty_is_used(tmp_path):
    PtyPort.holder_socket = str(tmp_path / "none.sock")
    try:
        port = PtyPort(str(tmp_path / "com1"))
        port.open()
        assert not port.held and os.path.islink(str(tmp_path / "com1"))
        port.close()
    finally:
        PtyPort.holder_socket = None
    assert request_port("/x", str(tmp_path / "none.sock")) is None


def test_unknown_port_is_refused(tmp_path):
    async def body():
        holder = PortHolder([str(tmp_path / "com1")], str(tmp_path / "s.sock"))
        await holder.start()
        r = await asyncio.get_running_loop().run_in_executor(
            None, request_port, str(tmp_path / "com9"), str(tmp_path / "s.sock"))
        assert r is None
        await holder.stop()
    run(body())

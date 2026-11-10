#!/usr/bin/env python3
"""dtr_test.py – key a text on a serial DTR line with the anyrig DTR keyer (TRANSMITS!).

Checks the timing of DTR keying on the real port before using cw.method: dtr, e.g. while the
rig's sidetone is recorded. Use a dummy load or low power.

  python3 tools/dtr_test.py --port /dev/ttyUSB0 --wpm 26 --text "CQ TEST DL1ABC"
  python3 tools/dtr_test.py --ptt rts --lead 10 --tail 20 ...
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from anyrig.common.keyline import KeyLine          # noqa: E402
from anyrig.remote.dtrkeyer import DTRKeyer        # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--wpm", type=float, default=26)
    ap.add_argument("--text", default="CQ TEST DL1ABC DL1ABC TEST")
    ap.add_argument("--ptt", choices=["rts", "none"], default="none")
    ap.add_argument("--lead", type=float, default=10, help="PTT lead-in [ms]")
    ap.add_argument("--tail", type=float, default=20, help="PTT tail [ms]")
    a = ap.parse_args()
    line = KeyLine(a.port, "dtr", None if a.ptt == "none" else "rts")
    line.open()
    done = asyncio.Event()
    t0 = time.monotonic()
    echo = lambda ch: print("%7.3f  %r" % (time.monotonic() - t0, ch), flush=True)  # noqa: E731
    k = DTRKeyer(line, lambda: a.wpm, {"ptt_lead_ms": a.lead, "ptt_tail_ms": a.tail},
                 on_done=lambda ids: done.set(), on_echo=echo)
    k.start()
    k.send(a.text, 1)
    try:
        await asyncio.wait_for(done.wait(), 120)
        await asyncio.sleep(0.2)
    finally:
        await k.stop()
    print("done, %d characters in %.2f s" % (k.chars_keyed, time.monotonic() - t0))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

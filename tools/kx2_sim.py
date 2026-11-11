#!/usr/bin/env python3
"""Simple KX2 simulator on a virtual port - for trying kx2probe.py without a rig.
Start:  python3 kx2_sim.py   -> prints the port name, then: python3 kx2probe.py --port <name>
The replies are simplified and not exactly like a real KX2."""
import os, pty, random, select, sys, time
DELAY = float(sys.argv[1]) / 1000 if len(sys.argv) > 1 else 0.0   # reply time in ms, e.g. 25

state = {"FA": "00014025000", "FB": "00014030000", "MD": "3", "BN": "05", "BW": "0050",
         "IS": " 0600", "RT": "0", "XT": "0", "RO": "+0000", "RA": "00", "PA": "0", "AG": "030",
         "RG": "250", "SQ": "000", "NB": "0", "AP": "0", "KS": "026", "CW": "60", "MG": "030",
         "CP": "010", "PC": "005", "VX": "0", "FT": "0", "LK": "0", "TQ": "0", "SM": "0005",
         "IC": "\x40\x01\x00\x00\x00", "IF": "00014025000     +000000 0003000001 ", "OM": "--------01"}
AI_REPORTS = {"FA", "MD", "BW", "RT", "RO", "AG"}       # what the simulator reports by AI
master, slave = pty.openpty()
print("simulated KX2 on:", os.ttyname(slave), flush=True)
ai, buf, t_next = 0, b"", time.time() + 1.5
while True:
    r, _, _ = select.select([master], [], [], 0.05)
    if r:
        buf += os.read(master, 1024)
        while b";" in buf:
            f, buf = buf.split(b";", 1)
            f = f.decode("latin-1")
            cmd, arg = f[:2], f[2:]
            if DELAY and not arg:
                time.sleep(DELAY)
            if cmd == "AI":
                if arg:
                    ai = int(arg)
                else:
                    os.write(master, ("AI%d;" % ai).encode())
            elif cmd == "DS":
                pass                                   # simulated: no reply
            elif cmd in state:
                if arg:
                    state[cmd] = arg
                else:
                    os.write(master, (cmd + state[cmd] + ";").encode("latin-1"))
            else:
                os.write(master, b"?;")
    if time.time() > t_next:
        t_next = time.time() + random.uniform(0.7, 2.0)
        k = random.choice(["FA", "MD", "BW", "RT", "RO", "AG", "RA", "PA", "NB", "SM", "IC", "KS"])
        if k == "FA": state[k] = "%011d" % (int(state[k]) + random.choice([-50, 20, 100]))
        elif k == "MD": state[k] = random.choice("1237")
        elif k == "BW": state[k] = "%04d" % random.choice([5, 10, 20, 40, 270])
        elif k in ("RT", "PA", "NB"): state[k] = "1" if state[k] == "0" else "0"
        elif k == "RA": state[k] = "01" if state[k] == "00" else "00"
        elif k == "RO": state[k] = "%+05d" % random.randint(-500, 500)
        elif k == "AG": state[k] = "%03d" % random.randint(0, 60)
        elif k == "SM": state[k] = "%04d" % random.randint(0, 30)
        elif k == "KS": state[k] = "%03d" % random.randint(18, 34)
        elif k == "IC": state[k] = state[k][:1] + chr(ord(state[k][1]) ^ (1 << random.randint(0, 7))) + state[k][2:]
        if ai and k in AI_REPORTS:
            os.write(master, (k + state[k] + ";").encode("latin-1"))

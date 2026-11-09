# kx2-to-anyrig

Use an **Elecraft KX2** as the local control head for your transceiver at home –
for remote contesting with Win-Test (CW first).

> Status: **in development.** Nothing to install yet. The measurement tools in `tools/` work today.

## Idea

- Turn the KX2's VFO, change filter, RIT, band, ATT, preamp, NB, NR … – the home rig follows.
- Change something at the home rig (or via a Win-Test script) – the KX2 follows.
- Win-Test talks to a virtual "Elecraft K3" and a virtual "WinKeyer". CW macros are keyed by the
  home rig's internal keyer; ESC aborts immediately.
- RX audio including the rig's own CW sidetone is streamed to your laptop with low latency.
- Web page with S-meter, power, SWR, ALC and buttons for functions the KX2 does not have
  (power, NR level, notch, beat cancel, ATU, CW tune, …).

First supported home rig: **Kenwood TS-590SG**. The remote side is designed to be rig-independent:
a driver per protocol family (Kenwood first; Yaesu, Icom CI-V, Hamlib planned) plus a YAML profile per model.

## Tools available now (`tools/`)

| Tool | Purpose |
|---|---|
| `kx2probe.py` | Text UI showing every CAT parameter of a KX2 (or TS-590SG with `--rig ts590`) and whether changes arrive by Auto-Info or polling. |
| `catsniff.py` | Sits between Win-Test (Wine) and the rig as a virtual serial port and logs all traffic with timestamps. |
| `ky_test.py` | Measures CW text output via `KY` on a TS-590SG (**transmits – use a dummy load**). |
| `kx2_sim.py`, `ts590_sim.py` | Simple rig simulators on a pty for testing without hardware. |

All tools need only Python 3 (standard library) on Linux.

## Kurzinfo (Deutsch)

Ein KX2 am Laptop steuert den Transceiver zu Hause – für Remote-Contestbetrieb mit Win-Test unter Wine.
Das Projekt ist in Entwicklung; die Messwerkzeuge in `tools/` funktionieren bereits.
Eine deutsche Schnellanleitung folgt mit der ersten lauffähigen Version.

## License

GPL-3.0 – see `LICENSE`.

Author: DK5TX. Developed with Claude Code.

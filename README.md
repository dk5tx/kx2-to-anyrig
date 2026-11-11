# kx2-to-anyrig

Use an **Elecraft KX2** as the local control head for your transceiver at home –
for remote contesting with Win-Test (CW first).

> Status: **in development.** Milestones M1–M4 work on the author's station (KX2 + TS-590SG + Win-Test, CW macros included). Next: M5 audio.
> The measurement tools in `tools/` work today.

## Trying milestone M1 (local only, no remote rig yet)

kx2-local sits between Win-Test and the KX2. Win-Test sees an Elecraft K3 on a virtual port;
kx2-local polls the KX2 with priorities (VFO and AF fast) and answers Win-Test instantly.
In `--local-only` mode every other command (Lua scripts) goes to the KX2, so Win-Test behaves
as with a directly connected KX2. Every frame is logged to `anyrig-wintest.log`.

```bash
sudo apt install python3-yaml            # only dependency for M1
git clone https://github.com/dk5tx/kx2-to-anyrig && cd kx2-to-anyrig

# once: tell Wine that COM1 is the virtual port (Wine recreates plain symlinks itself,
# so a registry entry is needed – a symlink in dosdevices alone gets replaced)
wineserver -k
wine reg add 'HKLM\Software\Wine\Ports' /v COM1 /t REG_SZ /d /tmp/anyrig-com1 /f
wineserver -k

python3 -m anyrig.local --local-only     # first: KX2 on /dev/ttyUSB0, 38400 Bd
# then start Win-Test (Radio 1 = Elecraft K3 on COM1; switch off "CW"/"PTT" on COM1)
```
kx2-local checks at start whether Wine's COM1 leads to its virtual port and tells you if not.

Back to the KX2 directly on COM1 (without kx2-local):
```bash
wine reg delete 'HKLM\Software\Wine\Ports' /v COM1 /f && wineserver -k
```
Options: `--kx2-port /dev/ttyUSB1`, `--debug`, `-c config/local.yaml` (see `config/local.example.yaml`).
Tests: `python3 -m pytest -q` (no hardware needed).

## Trying milestone M2 (KX2 controls the home rig)

On the **shack PC** (TS-590SG on `/dev/ttyUSB1`, 115200 Bd):
```bash
sudo apt install python3-yaml
git clone https://github.com/dk5tx/kx2-to-anyrig && cd kx2-to-anyrig
python3 -m anyrig.remote --traffic-log rig.log      # listens on TCP port 7355
```
On the **laptop** (Win-Test closed or open, Wine COM1 set up as for M1):
```bash
python3 -m anyrig.local --remote <shack-ip>         # instead of --local-only
```
Then turn the KX2 VFO, change filter, ATT, preamp, NB, NR, AGC, RIT … – the TS-590SG follows.
A spot click in Win-Test QSYs both rigs at once. Win-Test Lua scripts (TS-590SG variants) go
straight to the TS-590SG; `KS` from scripts is ignored, `TX`/`RX`/`PS0`/`SR`/`KY` are blocked.
The KX2 APF key (CW) switches the TS-590SG manual notch, the KX2 notch key (SSB) its auto notch
(`kx2_keys` in the config). In SSB the KX2 bandwidth becomes a low-cut/high-cut pair on the
TS-590SG centred near 1650 Hz (e.g. 2700 Hz → 300/3000 Hz, 2400 Hz → 400/2800 Hz).

Safety: if the laptop or the network goes away, anyrig-remote unkeys the rig within 1 s.

### Milestone M3: the KX2 follows the rig (two-way sync)

- Changes made at the TS-590SG or by Win-Test scripts (filter, ATT, preamp, RX antenna, CW tune,
  RIT clear, UP/DN …) appear on the KX2 and in Win-Test.
- On connect the rig is master: the KX2 is set to the rig's values, nothing on the rig changes.
- No ping-pong: echoes of your own KX2 changes are ignored; when you stop turning, the KX2 snaps
  to the value the rig really uses (e.g. a filter step).
- During TX, ATU tuning (the TS-590SG briefly switches to CW) and CW tune nothing is mirrored;
  afterwards the KX2 gets the final values (e.g. the zero-beat frequency after CW tune).
- After a band or mode change at the rig, the KX2's own band/mode memories do not overwrite the rig.
- NR is switched on the KX2 with a held PRE key (`SWH19`), the KX2 has no NR command.

### Milestone M4: CW macros (WinKeyer emulation → `KY`)

kx2-local offers a second virtual port that behaves like a K1EL WinKeyer. Win-Test sends its
macros there; the text is keyed by the rig's internal keyer (`KY`), no keying line is involved.

```bash
# once: Wine COM2 -> WinKeyer emulation
wine reg add 'HKLM\Software\Wine\Ports' /v COM2 /t REG_SZ /d /tmp/anyrig-com2 /f && wineserver -k
```
In Win-Test: Options → Configure Interfaces → COM2 = **WinKeyer** (no CW/PTT on COM1).

- F1–F8 macros start at once. Win-Test sends one character at a time and waits for its echo.
  The emulation paces the echo so that Win-Test stays about one word ahead of the keying.
- Speed keys (PgUp/PgDn) set `KS` on the rig and on the KX2 (the KX2 shows at most 50 WPM).
- ESC sends `KY0;`: keying stops after the current element.
- CQ repeat: the next CQ starts the set interval after the rig has finished. Win-Test itself
  counts from the last echo, about one word too early. Set `wintest.cq_repeat_s` to Win-Test's
  interval (e.g. `3.0`) so the first pause is right too; otherwise it is learnt from the 2nd one.
- Keyboard mode (Alt+K): typed text is keyed word by word – a word goes out when you type the
  space or pause for 0.8 s. Typing ahead gives the smoothest result.
- Each `KY` packet holds complete words; its space padding is keyed as the word gap
  (measured). Only words longer than 24 characters are split.
- Busy status for Win-Test lasts until the estimated end of keying.
- If the control link is lost, CW is blocked until the next connect.
- Every WinKeyer byte is logged in the traffic log (`WK>` / `WK<`).

**Keying over DTR instead of `KY`** (rig KEY input on a serial DTR line, set to straight key):
in the remote config set `cw: {method: dtr}` and `keyline: {port: /dev/ttyUSB0, ptt: rts}`
(`ptt: null` without amplifier). anyrig-remote then keys character by character and reports
each character back when it starts, like a real WinKeyer. Win-Test completes a call you are
still typing (DL [Ins] 1XYZ), `++`/`--` works, CQ repeat and keyboard mode need no tricks.
Check the timing first (transmits – dummy load): `python3 tools/dtr_test.py --port /dev/ttyUSB0 --wpm 26`.

### Milestone M5: RX audio

anyrig-remote streams the rig's USB sound card (RX audio incl. CW sidetone) with Opus over RTP/UDP
to the connected kx2-local; kx2-local plays it with small buffers. The KX2 AF knob sets the
laptop volume (needs `python3-gi`, normally installed on Mint; without it the volume is fixed).

- apt: `gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad`
- measured (TS-590SG -> WLAN -> laptop): **96 ms** with these settings (230 ms with GStreamer defaults)
- WLAN: `audio.jitter_ms: 40` (default); with a cable 25 is enough. Lost packets are concealed by Opus.
- UDP port 7356 must be open on the laptop (Mint: ufw is off by default).
- `kx2-local --no-audio` switches it off; `audio: {source: test}` on the remote sends a tone.

### Milestone M6: web UI and safety (accepted on hardware 11 Nov 2026)

kx2-local serves a page at **http://127.0.0.1:8073/** (standard library only, no extra package):
frequency, mode, filter, RIT/XIT, TX, meters (S/PO, SWR, ALC, COMP), link RTT, audio level, KX2
warnings, and controls for power, AGC, NR (+level), NB, notch (+frequency), beat cancel, ATT/PRE,
break-in delay, RF gain, antenna, CW tune and ATU tune (hold 1 s – it transmits). Keys: T CW tune,
N NR, B NB, M notch, A AGC. It always shows what the rig reports, not the last command.

Safety on the remote: link silent > 1 s -> `KY0;`/`RX;`/DTR released and CW blocked until the next
connect; continuous TX longer than `safety.tx_timeout_s` (60 s) -> CW aborted, `RX;`.
When the link drops, kx2-local echoes the characters Win-Test still waits for, so its macros work
again right after the reconnect.

**Restarting kx2-local:** the virtual COM ports are new ptys after a restart, but Win-Test keeps
the old (dead) ones open and becomes slow. Press REOPEN in Win-Test (or restart it) after
restarting kx2-local.

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

# kx2-to-anyrig

Use an **Elecraft KX2** as the local control head for your transceiver at home – for remote
contesting with Win-Test (CW first).

```
 LAPTOP                                                   SHACK PC
 Win-Test (Wine)  COM1 "K3", COM2 "WinKeyer"              anyrig-remote ── TS-590SG (CAT)
 kx2-local ── KX2 (knobs, buttons, display)   TCP/UDP     │             ── KEY / PTT (serial DTR/RTS)
 web UI, RX audio                           ◄──────────►  └─ RX audio (rig USB sound card)
```

- Turn the KX2's VFO, change filter, RIT, band, ATT, preamp, NB, NR … – the home rig follows.
- Change something at the home rig (or with a Win-Test script) – the KX2 follows.
- Win-Test talks to a virtual Elecraft K3 and a virtual WinKeyer. CW macros are keyed by the home
  rig (its own keyer or its KEY input); ESC stops at once.
- RX audio including the rig's own CW sidetone comes to the laptop with about 100 ms delay.
- A web page shows S-meter, power, SWR, ALC and has buttons for what the KX2 does not have.
- If the network goes away, the home rig stops transmitting within one second.

The KX2 never transmits; it is a box of knobs and a display.

**Status:** all milestones up to M6 work on the author's station (KX2 + TS-590SG + Win-Test,
CW). First supported home rig: **Kenwood TS-590SG**. The remote side is rig-independent: a
driver per protocol family (Kenwood today; Yaesu, Icom CI-V, Hamlib planned) plus a YAML profile
per model – see [docs/profiles.md](docs/profiles.md).

Contents: [Requirements](#requirements) · [Installation](#installation) ·
[Network](#network) · [Shack PC](#shack-pc) · [Laptop](#laptop) · [Starting](#starting) ·
[Operating](#operating) · [Troubleshooting](#troubleshooting) · [Tools](#tools) ·
[Development](#development)

---

## Requirements

| | Laptop (local) | Shack PC (remote) |
|---|---|---|
| OS | Linux (developed on Linux Mint 21/22), Win-Test under Wine | Linux |
| Rig | Elecraft KX2 on USB serial (38400 Bd) | Kenwood TS-590SG on USB (115200 Bd) |
| Optional | – | serial port whose DTR keys the rig's KEY input (and RTS = PTT for an amplifier) |
| Audio | any sound output | the rig's USB sound card |

Software: Python ≥ 3.10, PyYAML, GStreamer. Everything is in the distribution's packages; no pip
needed. The ports used: TCP 7355 (control), UDP 7356 (audio, shack → laptop), web UI on
127.0.0.1:8073.

## Installation

On **both PCs**:

```bash
sudo apt install git python3-yaml python3-gi gir1.2-gstreamer-1.0 \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
git clone https://github.com/dk5tx/kx2-to-anyrig ~/kx2-to-anyrig
sudo usermod -aG dialout $USER          # access to the serial ports; log out and in again
mkdir -p ~/.config/anyrig
```

The systemd units in `systemd/` expect the checkout in `~/kx2-to-anyrig` and the configs in
`~/.config/anyrig/`. Updating later: `cd ~/kx2-to-anyrig && git pull`, then restart the programs.

**Serial port names:** with more than one USB serial adapter, `/dev/ttyUSB0` and `/dev/ttyUSB1`
can swap after a reboot – on the shack PC the CAT port and the keying port would trade places.
Use the stable names instead:

```bash
ls -l /dev/serial/by-id/
# e.g. usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0 -> ../../ttyUSB1
```

and put the full `/dev/serial/by-id/…` path into the configs.

## Network

The laptop must reach the shack PC on TCP 7355; the shack sends the audio back to the laptop
on UDP 7356.

- **Fixed address for the shack PC:** give it a fixed IP in the router (FRITZ!Box: Home Network →
  Network → edit device → "Always assign this IPv4 address") or use its name, e.g.
  `remote: {host: shack.fritz.box}`.
- **Firewall:** if `ufw` is active, allow TCP 7355 on the shack PC and UDP 7356 on the laptop
  (`sudo ufw allow 7355/tcp`, `sudo ufw allow 7356/udp`). On Linux Mint ufw is off by default.
- **WLAN power saving off – on every PC that uses WLAN.** With power saving the WLAN card
  sleeps between packets: echo and audio packets arrive in bursts, CW timing and audio suffer.
  NetworkManager setting, permanent:

  ```bash
  printf '[connection]\nwifi.powersave = 2\n' | \
      sudo tee /etc/NetworkManager/conf.d/zz-anyrig-wifi-powersave-off.conf
  sudo systemctl restart NetworkManager
  iw dev                                   # shows the interface name, e.g. wlp2s0
  iw dev wlp2s0 get power_save             # must say: Power save: off
  ```

  (`2` = disable. The file name starts with `zz-` so it is read after Mint's own
  `default-wifi-powersave-on.conf`. Check again after a reboot.)
- **Cable beats WLAN.** Measured over WLAN: echo delay median 7 ms, 99 % below 19 ms, rare
  outliers to 100 ms; with a cable the audio jitter buffer can be smaller (25 instead of 40 ms).
- **Away from home only through a VPN** (e.g. WireGuard on the router). The control connection
  has no password or encryption – never forward port 7355 or 7356 to the internet.

## Shack PC

### TS-590SG settings

- USB CAT: 115200 Bd (the profile's default).
- Menu 72 (USB audio output level): **6**. Measured: about 3 dB per step; at 9 strong CW
  signals clip, at 6 CW peaks reach about −7 dBFS. Adjust the volume on the laptop, not here.
- Menu 06 (sidetone volume): to taste – the sidetone is in the USB audio, so you hear your
  own CW on the laptop.
- For keying over DTR: KEY input set to straight key.

### Configuration

```bash
cp ~/kx2-to-anyrig/config/remote.example.yaml ~/.config/anyrig/remote.yaml
```

and edit it:

```yaml
rig: {profile: profiles/ts590sg.yaml, port: /dev/serial/by-id/usb-…, baud: 115200}
cw: {method: dtr}        # or cat (the rig keys text with KY)
keyline: {port: /dev/serial/by-id/usb-…, key: dtr, ptt: rts}   # ptt: null without amplifier
audio: {source: "hw:CARD=CODEC"}     # the rig's USB sound card (arecord -l lists the cards)
safety: {link_timeout_ms: 1000, tx_timeout_s: 60}
```

**CW method.** `cat` uses the rig's own keyer with the `KY` command: no extra cable, but the rig
keys whole words, so Win-Test cannot complete a call while it is being sent. `dtr` keys the KEY
input through a serial port character by character, like a real WinKeyer: Win-Test can
complete a call you are still typing (DL [Ins] 1XYZ), `++`/`--` speed changes work inside a
message, CQ repeat and keyboard mode behave natively. PTT on RTS (for an amplifier) uses the
lead/tail times set in Win-Test. Check the timing once with a dummy load:
`python3 tools/dtr_test.py --port /dev/serial/by-id/usb-… --wpm 26` (this transmits).

### Optional: start automatically at boot

Usually you start anyrig-remote by hand when you want to operate remotely – and leave it off
while you sit at the rig yourself. If the shack PC should always be ready:

```bash
mkdir -p ~/.config/systemd/user
cp ~/kx2-to-anyrig/systemd/anyrig-remote.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now anyrig-remote
sudo loginctl enable-linger $USER        # start at boot without anybody logging in
journalctl --user -u anyrig-remote -f    # log
```

## Laptop

### KX2 settings

- **TX TEST on / power 0 W.** XMIT and ATU still key the KX2 briefly; it must not transmit.
  kx2-local warns at start if the power is above 0.
- **CWT (auto spot) off** – it would tune to the local signal. kx2-local warns if it is on.
- RS-232/USB: 38400 Bd.

### Configuration

```bash
cp ~/kx2-to-anyrig/config/local.example.yaml ~/.config/anyrig/local.yaml
```

and set at least:

```yaml
remote: {host: shack.fritz.box, port: 7355}      # or the shack's IP
kx2: {port: /dev/serial/by-id/usb-…, baud: 38400}
audio: {jitter_ms: 40, sink_buffer_ms: 20}       # WLAN; with crackle under CPU load: 60 / 40
```

### Wine: COM1 and COM2

Win-Test reaches kx2-local through two virtual serial ports, `/tmp/anyrig-com1` (K3) and
`/tmp/anyrig-com2` (WinKeyer). Tell Wine once (a symlink in `dosdevices` alone is replaced by
Wine, the registry entry is needed):

```bash
wineserver -k
wine reg add 'HKLM\Software\Wine\Ports' /v COM1 /t REG_SZ /d /tmp/anyrig-com1 /f
wine reg add 'HKLM\Software\Wine\Ports' /v COM2 /t REG_SZ /d /tmp/anyrig-com2 /f
wineserver -k
```

kx2-local checks at start that Wine's COM1/COM2 lead to its ports and tells you if not.
Back to a directly connected KX2: `wine reg delete 'HKLM\Software\Wine\Ports' /v COM1 /f && wineserver -k`.

### Win-Test settings

Options → Configure Interfaces:

- **COM1:** Radio 1 = **Elecraft K3**, 38400 8N1. **"CW" and "PTT" off** (a virtual port has no
  DTR/RTS lines).
- **COM2:** **WinKeyer**. DTR/RTS settings do not matter. PTT lead-in/tail set here are used
  for the RTS PTT of the DTR keying.
- Lua scripts: use the **TS-590SG** variants. They go straight to the home rig; `KS` from scripts
  is ignored (the WinKeyer sets the speed), `TX`, `RX`, `PS0`, `SR`, `KY` are blocked.

### Keeping the ports open: anyrig-ports

The virtual ports normally live inside kx2-local. When kx2-local is restarted they are new
devices, but Win-Test still holds the old ones, becomes slow and needs **REOPEN**.
`anyrig-ports` is a small program that creates the two ports and keeps them; kx2-local borrows
them. Then kx2-local can be stopped and started as often as you like while Win-Test runs.
Start it at login, before Win-Test:

```bash
mkdir -p ~/.config/systemd/user
cp ~/kx2-to-anyrig/systemd/anyrig-ports.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now anyrig-ports
```

kx2-local logs at start which ports it uses ("virtual COM ports from anyrig-ports" or "own
virtual COM ports"). Without anyrig-ports everything works as before, only with REOPEN after a
restart of kx2-local. Only if anyrig-ports itself is restarted, Win-Test needs REOPEN.

## Starting

**By hand** (recommended while you watch the log). `python3 -m anyrig.local` runs the module
`anyrig/local/__main__.py` of the checkout – there is nothing to install, but the command must
be run inside the checkout directory. Config files can have any name and place (`-c`); only
the systemd units expect `~/.config/anyrig/local.yaml` and `remote.yaml`.

```bash
cd ~/kx2-to-anyrig
python3 -m anyrig.remote -c ~/.config/anyrig/remote.yaml      # shack PC (unless it runs as a service)
python3 -m anyrig.local  -c ~/.config/anyrig/local.yaml       # laptop
```

Stop with Ctrl+C (a second Ctrl+C exits at once). Then start Win-Test and open
http://127.0.0.1:8073/ for the web UI.

**As a service on the laptop** (optional):

```bash
cp ~/kx2-to-anyrig/systemd/kx2-local.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start kx2-local          # 'enable --now' to start it at every login
journalctl --user -u kx2-local -f
```

**Order does not matter** between the PCs: kx2-local reconnects by itself (every few seconds),
and the rig is always master – at every connect the KX2 is set to the rig's state.

**After shutting down and rebooting:** `/tmp` is empty after a reboot, so the ports and their
links are gone. With `anyrig-ports` enabled they are created again at login, before you start
Win-Test, and Wine's COM1/COM2 entries (in the Wine registry) point to them again. On the shack
PC, `anyrig-remote` starts at boot (linger). So: switch on, log in, start kx2-local (or let the
service do it), start Win-Test – no REOPEN, nothing to set again.

Useful options: `kx2-local --local-only` (no shack: Win-Test works with the KX2 as if it were
directly connected), `--remote host[:port]`, `--kx2-port`, `--no-audio`, `--debug`;
`anyrig-remote --traffic-log rig.log` logs every frame to and from the rig.

## Operating

### KX2 and rig in sync

- KX2 → rig: VFO A/B, mode, filter (CW bandwidth and shift; in SSB the bandwidth becomes a
  low-cut/high-cut pair, e.g. 2700 Hz → 300/3000 Hz, table in the profile), RIT/XIT, split,
  ATT, preamp, NB, NR, AGC, RF gain, keyer speed, VOX.
- Rig → KX2: everything the KX2 can display or set by CAT, also changes made at the rig, by Win-Test
  or by its scripts.
- At connect the rig is master: the KX2 takes the rig's values, nothing on the rig changes.
- Your own changes are not mirrored back (no ping-pong); when you stop turning, the KX2 snaps to
  the value the rig really uses (e.g. a filter step).
- During TX, ATU tuning (the TS-590SG briefly switches to CW) and CW tune nothing is mirrored;
  afterwards the KX2 gets the final values (e.g. the zero-beat frequency after CW tune).
- NR is switched on the KX2 with a held PRE key; KX2 NR on means NR1 on the TS-590SG.
- The KX2 APF key (CW) switches the TS-590SG manual notch, the KX2 notch key (SSB) its auto
  notch (`kx2_keys` in the local config).
- The KX2 AF knob sets the laptop volume (log curve, 40 dB range); it does not touch the rig.
- Not mirrored: power (web UI or scripts), the KX2's XMIT/ATU/TX.

### Win-Test

- Spot click, frequency entry, VFO B, split: both rigs follow at once.
- F1–F8 macros, ESC, speed keys (PgUp/PgDn), CQ repeat and keyboard mode (Alt+K) work as with a
  real WinKeyer. Speed changes go to the rig and are shown on the KX2.
- With `cw: {method: cat}` the rig keys whole words (`KY`); a word goes out when complete. Set
  `wintest.cq_repeat_s` in the local config to Win-Test's CQ repeat interval so the first pause
  is right too. With `dtr` this is not needed.
- ATU tune (Win-Test script `AC111`) and CW tune (`CA1`) go to the rig.

### Web UI

http://127.0.0.1:8073/ shows frequency, mode, filter, RIT/XIT, TX, meters (S/PO, SWR, ALC,
COMP), link round trip time, audio level and KX2 warnings, and has controls for power, AGC,
NR (+level), NB, notch (+frequency), beat cancel, ATT/PRE, break-in delay, RF gain, antenna,
CW tune and ATU tune (hold 1 s – it transmits). Keys: T CW tune, N NR, B NB, M notch, A AGC.
It always shows what the rig reports, not the last command. It is reachable only from the
laptop itself.

### RX audio

The shack streams the rig's USB sound card with Opus over RTP/UDP to the connected laptop.
Measured from the rig's speaker to the laptop's speaker: **96–102 ms** (WLAN, jitter buffer
25–40 ms). Lost packets are concealed by Opus. Settings in the local config: `jitter_ms`
(40 for WLAN, 25 with a cable), `sink_buffer_ms` (raise to 40 if it crackles under CPU load),
`gain`. `kx2-local --no-audio` switches it off; `audio: {source: test}` on the shack sends a
600 Hz tone for testing.

### Safety

- Control link silent for more than 1 s (network gone, laptop crashed): the shack stops CW at
  once, releases KEY/PTT, sends `RX;` and blocks CW until the next connect.
- Continuous TX longer than `safety.tx_timeout_s` (60 s): CW aborted, `RX;`.
- The shack never transmits on its own: only CW text from Win-Test and ATU tune on request.
- When the link comes back, CW works again at once; the rest of an interrupted message is
  dropped (Win-Test's CQ repeat starts the next one as usual – ESC stops it).
- On exit or crash of anyrig-remote: `KY0;`, KEY/PTT released, auto information off.

## Troubleshooting

| Problem | Cause / fix |
|---|---|
| Win-Test: "no radio" / no frequency | kx2-local not running, or Wine COM1 does not point to `/tmp/anyrig-com1` (kx2-local says so at start). |
| Win-Test slow, macros dead after restarting kx2-local | the old virtual ports are gone: REOPEN in Win-Test, or use anyrig-ports. |
| "Permission denied" on `/dev/ttyUSB…` | user not in group `dialout` (log out and in after `usermod`). |
| Shack keys on the wrong port after a reboot | ttyUSB names swapped: use `/dev/serial/by-id/…`. |
| No audio | UDP 7356 blocked on the laptop; wrong `audio.source` on the shack (`arecord -l`); look for "audio" lines in both logs. |
| Audio crackles | raise `sink_buffer_ms` (40) and `jitter_ms` (60); WLAN power saving off. |
| Audio distorted | rig menu 72 too high (6 recommended). |
| CW timing uneven over WLAN | WLAN power saving off; cable if possible. |
| KX2 shows something else than the rig | it follows within about 1 s (KX2 reports only once per second); a persistent difference: please report with the traffic log. |

Logs: kx2-local writes every frame to and from Win-Test (`WT>`/`WT<`, WinKeyer `WK>`/`WK<`)
and link events to `anyrig-wintest.log` in its working directory; anyrig-remote writes the rig
traffic with `--traffic-log`.

## Tools

| Tool | Purpose |
|---|---|
| `tools/kx2probe.py` | Text UI showing every CAT parameter of a KX2 (or TS-590SG with `--rig ts590`) and whether changes arrive by auto information or only by polling. |
| `tools/catsniff.py` | Sits between Win-Test (Wine) and a rig as a virtual serial port and logs all traffic with time stamps (`--hex` for WinKeyer). |
| `tools/ky_test.py` | Measures CW text output with `KY` on a TS-590SG (**transmits – dummy load**). |
| `tools/dtr_test.py` | Measures DTR keying timing (**transmits – dummy load**). |
| `tools/kx2_sim.py`, `tools/ts590_sim.py` | Simple rig simulators on a virtual port. |

## Development

- Specification and measured protocol facts: [CLAUDE.md](CLAUDE.md); measurement notes:
  [docs/measurements.md](docs/measurements.md); new rigs: [docs/profiles.md](docs/profiles.md).
- Tests (simulators, no hardware): `python3 -m pytest -q` (`sudo apt install python3-pytest`).
- Milestones: M1 local CAT proxy · M2 remote and Kenwood driver · M3 two-way sync · M4 WinKeyer
  emulation and CW (KY and DTR) · M5 RX audio · M6 web UI and safety – all accepted on the author's
  station (Nov 2026) · M7 packaging and documentation.
- Later: paddle keying, SSB TX audio and PTT, a WireGuard guide, Yaesu/Icom/Hamlib drivers,
  KX3/K3 as control head.

## License

GPL-3.0 – see `LICENSE`.

Author: DK5TX. Developed with Claude Code.

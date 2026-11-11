# CLAUDE.md – kx2-to-anyrig

Developer specification for Claude Code. Read this file completely before changing code.
All values marked **measured** come from tests on real hardware (Nov 2026) and are binding.
Detailed measurement notes: `docs/measurements.md`.

---

## 1. Goal

Remote contest operation (CW first) of a home transceiver ("remote rig") using an
**Elecraft KX2 as the local control head**, with Win-Test (under Wine) as logger.

```
 LAPTOP (local)                                         SHACK PC (remote)
 ┌──────────────────────────────────────┐   LAN/VPN    ┌──────────────────────────────┐
 │ Win-Test (Wine)                      │              │                              │
 │   COM1 (K3 CAT) ──┐                  │              │                              │
 │   COM2 (WinKeyer)─┤                  │  TCP control │  anyrig-remote               │
 │                   ▼                  │◄────────────►│   driver (Kenwood, …)        │
 │ kx2-local  ◄── /dev/ttyUSB0 ── KX2   │  UDP audio   │   ── /dev/ttyUSB1 ── TS-590SG│
 │   web UI (browser)                   │◄─────────────│   audio: USB Audio CODEC     │
 │   audio out                          │              │   (opt.) /dev/ttyUSB0 DTR=KEY│
 └──────────────────────────────────────┘              └──────────────────────────────┘
```

- The KX2 never transmits. It is a knob/button box and shows the remote rig's state.
- Every change on the KX2 is mirrored to the remote rig; every change on the remote rig is mirrored back
  to the KX2 (**two-way sync**).
- Win-Test believes it talks to an Elecraft K3 (COM1) and a WinKeyer (COM2).
- CW macros are sent as text and keyed by the remote rig's internal keyer (Kenwood `KY`).
- Remote RX audio (incl. CW sidetone) is streamed to the laptop with low latency.

The project is open source (GPL-3.0). The remote side must be **rig-independent**
(driver per protocol family + YAML model profile). The first and reference profile is the Kenwood TS-590SG.

### Scope of version 1
CAT mirroring both ways, Win-Test K3 emulation, Lua passthrough, WinKeyer emulation → `KY`,
RX audio, web UI with meters and buttons, watchdog. LAN only (no encryption, plain TCP/UDP),
but structured so WireGuard can be put in front without code changes.

### Not in version 1 (keep room in the design)
Paddle / hand keying (time-stamped key events → DTR on remote), SSB TX audio + PTT,
internet operation, drivers other than Kenwood, other local control heads (KX3/K3).

---

## 2. Reference installation (author DK5TX)

| | Laptop "stream" (local) | Shack PC "shack" (remote) |
|---|---|---|
| OS | Linux Mint, Win-Test under Wine | Linux Mint |
| Rig | KX2, `/dev/ttyUSB0`, 38400 Bd, FW MCU 3.02 / DSP 1.52 | TS-590SG, `/dev/ttyUSB1`, 115200 Bd |
| Extra serial | – | `/dev/ttyUSB0`: RS-232, DTR = KEY input, RTS = PTT |
| Audio | card 0 HDA Intel PCH (ALC236) | card 1 "USB Audio CODEC" (= TS-590SG), 48 kHz mono |

Win-Test: Radio 1 = "Elecraft K3" on COM1 38400 8N1. In remote mode COM1 must point to the
kx2-local pty and the "CW"/"PTT" options on COM1 must be **off** (a pty has no modem lines).

---

## 3. Measured facts (binding)

### 3.1 KX2 (Elecraft protocol, see `docs/` and Elecraft Programmer's Reference Rev. G5)
- Auto-Info (`AI2`) reports changes **only once per second**, bundled (FA, FB, FR, FT …). Too slow for VFO/AF.
- Poll: ~36 queries/s possible, reply typically 8–27 ms (median 25 ms), rare outliers ~230 ms.
  During band change the KX2 does not answer for up to ~1.5 s.
- Must be **polled** with priorities: FA and AG fast (~50–100 ms); BW, IF, MD, RT/XT, RO normal (~0.5 s);
  ATT/PRE/NB/band etc. come per AI (poll every few s as safety net).
- `BW` (filter) is **not** reported by AI (AI only sends `FW0000`, K2 format) → poll.
- Not reported by AI: `NL` (NB level), `LK` (lock).
- `AP` (APF) answers only in CW (timeout in SSB). APF state = IC byte c (3rd byte) bit 5.
- **K31 (extended mode) works**: DS last byte ("icon flash") then shows B4 ATU, B3 CWT, B2 **NR**,
  B1 NTCH (auto notch), B0 MAN NOTCH. DS icon byte: B6 NB, B5 ANT2, B4 PRE, B3 ATT, B1 RIT, B0 XIT.
  → kx2-local sends `K31;` at start and polls `DS` (normal priority) to see NR/notch.
- Split only possible if VFO A and B have the same mode (else display "SPL N/A"); split = `FT1/FT0`.
- XMIT and ATU buttons key the KX2 (TQ=1 ~0.5 s). Run the KX2 in TX TEST / 0 W. Never forward KX2 TX.
- Auto Spot (CWT) on the KX2 tunes to the *local* signal → must be off (CWT bit visible in K31).
- `SWT`/`SWH` emulate button taps/holds (KX2 table 8A), e.g. SWT34 = KYR-SPT tap.
- The KX2 stores frequencies in **10 Hz steps, truncated** (`FA00014014527;` → reads back `14014520`).
  Such a read-back is rounding, not an operator action (M3 hardware test, 9 Nov 2026).

### 3.2 Win-Test ↔ "K3" (captured with tools/catsniff.py)
- Win-Test polls **only** `IF;` and `FB;`, alternating, ~450 ms each (its own pacing, regardless of the
  100 ms setting). No `AI`, no `K31`, no init.
- Click on cluster spot: `FA00007039800;` `MD3;` `FT0;` back-to-back, no read-back.
- IF reply format (K3): `IF` f(11) 5×space `+yyyy`(RIT/XIT offset, sign+4) r x space `00` t m v s p b d `1` space `;`
  Example: `IF00014074002     -000000 0002000001 ;` (t = TX, m = mode digit, v = RX VFO, s = **scan**, p = **split**).
- Lua scripts send arbitrary strings through the same port (`wtRadio:Send("CA1;")`).
- WinKeyer (M4 hardware test, 10 Nov 2026): Win-Test sets mode 0x94/0x95 (serial echo) and sends a message
  **one character per echo** (~55 ms after each echo), framed by `0A 02 nn` (clear, speed) and `02 00`.
  It considers the message sent at the last echo; a CQ repeat starts with `0A`+text the repeat interval after
  the last echo (also while the keyer is still busy). Keyboard mode (Alt+K) sends `0A`+char for every key once
  all echoes are in. ESC = `0A` without text. → `0A`+text with no echo outstanding is not an abort
  (`local/winkeyer.py`: append when typing, CQ repeat waits for the rig's end + Win-Test's pause).
- Captured with kx2-local (M1, 9 Nov 2026) – Win-Test only ever sends these set commands:
  spot click `FA…;MD3;FT0;` · direct entry (e.g. "14025") `FA…;MD3;` · spot to VFO B `FB…;MD$3;` ·
  split on with QSX `FB…;FT1;` · split off `FT0;`. **M1 accepted on hardware.**

### 3.3 TS-590SG (Kenwood, PC Control Command Reference B5A-0316-00)
- `AI2` reports changes **immediately** (VFO turning ~every 30 ms). Query reply ~2 ms.
- With AI2 the rig sends **meters by itself**: RX: `SM` continuously; TX: every ~60 ms `RM1` (SWR),
  `RM2` (COMP), `RM3` (ALC) and `SM` (= PO), each 0–30. → no meter polling needed.
- `RM;` returns three frames (`RM1xxxx;RM2xxxx;RM3xxxx;`), 3-char key.
- TX state: `TX0;` at start, `RX;` at end (AI).
- Reported by AI (tested): FA/FB/IF, MD, RA, PA, NB, NR(+RL), NT(+BP), BC, GC(+GT), VX, PR, PC, KS, SD,
  AC, CA, FT, FR, RT, XT, FL, FW, IS, SH/SL, AG, RG, AN. Mode change sends a bundle
  (EX037, MD, XI, UR, FS, FW+IS or SH+SL, PR). `RS0/RS1` (undocumented) = MULTI/CH quick menu open/closed → ignore.
- Mode-dependent commands answer `?`: FW/IS only CW/FSK, SH/SL only SSB/AM/FM, NL only if NB on,
  RL only if NR on. Not an error.
- **During ATU tune (`AC111`) the rig switches briefly to CW (MD3), transmits (TX0) and switches back.**
  Do not mirror these changes to the KX2/Win-Test (suppress reverse sync during TX and AC111).
- CW Tune: `CA1;` → frequency steps arrive as FA/FB → `CA0;` after 0.24–0.6 s.
  With RIT on, the RIT is tuned instead of the VFO.
- `KY`: format `KY ` + exactly 24 chars (space padded) + `;`. `KY;` → `KY0` buffer free / `KY1` full.
  `KY0;` aborts. Prosigns: `[`=BT `_`=AR `<`=AS `#`=HH `>`=SK `]`=KN `\`=BK `%`=SN.
  - command → TX0: **11–16 ms**
  - chunks are joined seamlessly; buffer accepts more than one chunk
  - **a space at the start of a chunk is dropped** ("DL1ABC" + " TEST" → "DL1ABCTEST").
  - **the padding of a short chunk is keyed as a word gap** (10 Nov 2026: "C"+"Q D"+"L1ABC…" → C␣Q␣D␣L).
    Full 24-char chunks join seamlessly. → chunks = complete words, padding = word gap; split inside a word
    only for words > 24 chars.
  - abort: keying stops after the current element; `RX;` follows after the break-in delay (SD).
  - timing (10 Nov 2026, 26 WPM): char gap 3 units, **word gap 6 units** (275–279 ms), also at packet edges.
  - rarely a packet is lost (10 Nov 2026): answered with `E;` (communication error), and the next packet right
    after was ignored without any answer → resend after `;` on E/O, resend once if no `TX` within 250 ms.
- Sidetone is on USB audio (menu 06 = sidetone level, 8/20 preferred). Menu 72 = USB output level (RX only).
- USB output level (menu 72), measured 11 Nov 2026 with `arecord hw:CARD=CODEC` (40 m, AGC on): ~3 dB per step.
  Level 5: strong CW peaks −10 dBFS, band noise RMS −24 dBFS, no clipping. Level 9: CW clipped heavily
  (106 620 samples in 20 s), noise crackle clips. **Recommended: 6** (CW peaks ≈ −7 dBFS); gain on the laptop side.
- Filter: `FW` only steps 50, 80, 100, 150, 200, 250 … 1000, 1500, 2000, 2500 Hz. `IS` = CW centre 300–1000 Hz (50 Hz steps).
- RIT offset: no absolute set; use `RC` (clear) + `RU`/`RD` (Hz step).
- AGC: `GC` (0 off, 1 slow, 2 fast). `GT` means time constant (≠ KX2 `GT`).

---

## 4. Architecture

Two programs in one Python package `anyrig` (Python ≥ 3.10, asyncio):

```
anyrig/
  common/      protocol.py (control messages), config.py, log.py, morse.py
  local/       kx2.py (KX2 link + poll scheduler), k3emu.py (Win-Test CAT pty), ports.py (anyrig-ports pty holder),
               winkeyer.py (WinKeyer pty), router.py (command routing), sync.py,
               audio_rx.py, webui/ (stdlib HTTP + Server-Sent Events, one html file), main.py
  remote/      core.py (state, sync, safety), cwtext.py (KY chunking / DTR fallback),
               audio_tx.py, main.py
  drivers/     base.py (abstract Rig), kenwood.py (first), hamlib.py (later), icom.py (later), yaesu.py (later)
profiles/      ts590sg.yaml (+ later others)
config/        local.example.yaml, remote.example.yaml
tools/         kx2probe.py, catsniff.py, ky_test.py, kx2_sim.py, ts590_sim.py
tests/         pytest, simulators, no hardware
docs/
```

Dependencies (all available as Debian/Mint packages, prefer apt):
`python3-yaml`, GStreamer (web UI uses the standard library only – no aiohttp)
(`gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-pipewire`).
No other runtime dependencies without a good reason. Tests: `python3-pytest`.
Serial ports and ptys use our own termios wrapper (`anyrig/common/serialport.py`, Linux only) – no pyserial.

### 4.1 Rig abstraction (remote)
`drivers/base.py` defines `Rig` with a **normalized state model** and async methods:

```
state keys (normalized units):
  freq_a, freq_b [Hz], rx_vfo, tx_vfo ('A'|'B'), split (bool), mode ('CW','CW-R','USB','LSB','AM','FM','DATA',…),
  rit, xit (bool), rit_offset [Hz], filter_bw [Hz], filter_center [Hz],
  att, preamp (int level, 0 = off), nb, nr (int level), notch, beat_cancel, agc ('off','slow','fast'),
  rf_gain [0..1], keyer_wpm, break_in_delay [ms], power [W], tx (bool), atu_tuning (bool),
  cw_tune (bool), meters {s, po, swr, alc, comp} [0..1 normalized + raw]
methods:
  open(), close(), set(key, value), get(key), send_raw(str), send_cw_text(str), abort_cw(),
  start_cw_tune(), start_atu_tune(), capabilities() -> set of keys
events (callback/async queue):
  changed(key, value, source='rig'), raw_frame(str)
```

`drivers/kenwood.py` implements this for Kenwood ASCII using the YAML profile
(command templates, value tables, filter steps). Other families get their own driver later.
A profile must be enough to add a new Kenwood-style model without code changes.

### 4.2 Control protocol local ↔ remote
TCP, newline-delimited JSON (UTF-8), one connection, both directions. Messages:

```
{"t":"hello","role":"local","version":1}
{"t":"set","k":"freq_a","v":14025000,"seq":17}          local → remote (from KX2 or Win-Test)
{"t":"state","k":"freq_a","v":14025010,"src":"rig"}      remote → local (rig change, for reverse sync)
{"t":"raw","d":"CA1;"}                                   local → remote (Lua passthrough)
{"t":"cw","text":"CQ TEST DK5TX","id":5}                 local → remote
{"t":"cw_abort"}  {"t":"cw_done","id":5}
{"t":"meters","s":0.3,"po":0.0,"swr":0.0,"alc":0.0}      remote → local (rate-limited ~20/s)
{"t":"ping","ts":…} / {"t":"pong","ts":…}                both, every 200 ms
```
Audio: separate UDP stream (RTP/Opus, see 4.6). Keep transport replaceable (later WireGuard).

### 4.3 kx2-local: KX2 link
- Open `/dev/ttyUSB0` 38400 8N1 exclusive. Send `AI2;` and `K31;` at start, restore on exit.
- Priority poll scheduler (as in `tools/kx2probe.py`): FA, AG fast; BW, IF, MD, DS, RO normal; rest slow/AI.
  Pick the most overdue item relative to its interval; one query in flight at a time; 2 ms gap.
- Parse AI frames and poll replies into the same normalized state (Elecraft units → Hz etc.).
- **Writes to the KX2** (reverse sync): FA/FB, MD, BW, RT/XT, RO (via RC + RU/RD or absolute if supported), RA, PA, NB, KS, FT.
  NR has no set command → toggle via `SWH19` only if state differs (verify with DS afterwards).
- Never send TX commands to the KX2. On start: warn in UI if KX2 power > 0 W / not in TX TEST,
  and if CWT is on.

### 4.4 kx2-local: Win-Test K3 emulation (`k3emu.py`)
- Create a pty, symlink e.g. `/tmp/anyrig-com1`; user links `~/.wine/dosdevices/com1` to it
  (document; optionally a helper command `anyrig-local --link-wine com1`).
- Answer `IF;`, `FB;`, `FA;`, `MD;`, `FT;`, `FR;`, `RT;`, `XT;`, `ID;` (`ID017`), `OM;`, `K3;`/`K2;`, `AI;`
  **immediately from the normalized state** in exact K3 format (see 3.2). Build IF from state.
- Set commands from Win-Test (`FA…;` `FB…;` `MD…;` `FT…;` `FR…;` `RT`, `XT`, `RC`): update state, send
  to remote immediately (do not wait for polling) **and** to the KX2.
- Everything else from Win-Test (Lua scripts) → **router** (4.5).
- Unknown/unhandled commands are logged.

### 4.5 Command router (Lua passthrough)
Decision of the author: in remote mode Win-Test uses the **TS-590SG Lua scripts** unchanged.
Rules (configurable):
1. Win-Test base commands (4.4) → handled locally + sent to remote + KX2.
2. `RC;` (clear RIT) → remote **and** KX2.
3. `KS…;` from scripts → **ignored** by default (`router.ignore: [KS]`), because the WinKeyer emulation owns the
   CW speed. (N6TV keyer-sync script would otherwise add its paddle offset to macro speed.)
4. Optional prefix `@` → always remote, prefix stripped.
5. Optional translation table, e.g. `SWT34;` → `CA1;` (only needed if KX2-variant scripts are used).
6. Blocklist (never forwarded unless allowed in config): `TX`, `RX`, `PS0`, `SR`, `KY` (CW only via cw path).
7. **Everything else → remote rig unchanged** (`{"t":"raw"}`). Replies from the rig to raw commands are **not**
   forwarded to Win-Test (it expects K3 format); they update remote state / UI.
Known script commands (author): FW0100–FW0500, UP10/DN10, RA00/01, PA0/1, AN909/AN919, AC111 (ATU,
transmits!, key "ä" in Win-Test), CA1, RC, KS0nn.

### 4.6 WinKeyer emulation (`winkeyer.py`) → CW text
- Second pty (e.g. `/tmp/anyrig-com2`) linked to Wine COM2; Win-Test configured for WinKeyer on COM2.
- Implement the K1EL WinKeyer 2/3 host protocol subset Win-Test uses: host open (`00 02` → version byte),
  host close, speed (`02 nn`), clear buffer (`0A`) = **abort**, PTT/lead-in settings (accept, ignore),
  buffered text, status byte (`0xC0` base, bit 2 busy) and echo of sent characters if Win-Test expects it.
  Capture real traffic with `tools/catsniff.py --hex` on the emulator to confirm. Be tolerant: log and ignore
  unknown commands.
- Text is collected and sent to remote as `{"t":"cw"}` with minimal delay (send per word or after 30 ms idle,
  so CQ starts immediately). Speed changes → `set keyer_wpm` (→ `KS` on the rig).
- Busy status for Win-Test: busy from first char until `cw_done` (remote sees `RX;` / KY buffer empty).
- Abort (`0A`, ESC in Win-Test) → `cw_abort` → `KY0;` immediately.

### 4.7 Remote CW text (`remote/cwtext.py`)
- Kenwood: chunk text to 24-char `KY` packets, **never let a chunk start with a space** (split inside words),
  pad with spaces, keep the buffer filled by polling `KY;` (≤ 50 ms) while sending, abort with `KY0;`.
- Map WinKeyer prosign conventions to rig prosign characters via profile.
- Fallback (profile `cw.method: dtr`): generate Morse timing (`common/morse.py`) and key DTR on a serial port
  with a real-time loop (SCHED_FIFO if permitted). Needed for rigs without CAT text. Not required for v1 tests on hardware,
  but implement with unit tests.

### 4.8 Two-way sync and loop protection (`local/sync.py`, `remote/core.py`)
- Each state change carries a source: `kx2`, `wintest`, `rig`, `ui`.
- Forward direction: KX2 change → remote `set` → rig. Win-Test set → remote + KX2.
- Reverse: rig change (AI) → local `state` → KX2 write (+ Win-Test sees it via its next poll).
- Loop protection: after writing key K to a device, ignore reports of the same value for K from that device for
  a hold-off window (KX2: 1.5 s because of 1 Hz AI; rig: 300 ms). Last writer wins; while the user turns the KX2 VFO,
  reverse FA updates from the rig are suppressed for 500 ms after the last KX2 change.
- Suppress reverse sync of MD/FA/filter **while `tx` or `atu_tuning`** is true; after it ends, resync once.
- After `cw_tune` ends (`CA0`), push final freq_a / rit_offset to KX2.
- Only **changes** are mirrored, but on (re)connect a full state sync runs: rig is master (rig → KX2).
- Implemented (M3, `local/sync.py`): remote sends `snapshot_done` after the state dump; forwarding starts
  only then. The **first reading of every KX2 value is never forwarded** – it is compared with the rig and
  the rig value is written to the KX2. Unknown KX2 values are never written (NR toggle needs a known state).
  After a reverse mode change or a frequency jump > 100 kHz, KX2 changes are not forwarded for 1.5 s
  (KX2 band/mode memory recall) and a resync follows. Hold-offs: freq/RIT 0.5 s, filter 1.5 s, others 0.4 s.

### 4.9 Value mapping KX2 ↔ TS-590SG (via profile)
| Function | KX2 | TS-590SG | Note |
|---|---|---|---|
| VFO | FA/FB | FA/FB | identical, 11 digits Hz |
| Split | FT | FR/FT | on split-on also set VFO B mode |
| Mode | MD 1 LSB,2 USB,3 CW,4 FM,5 AM,6 DATA,7 CW-R,9 DATA-R; DT | MD 1–9, DA for DATA | DATA A/AFSK → USB/LSB + DA1 |
| Filter CW | BW (10 Hz units) | FW (steps) | round to nearest step |
| Filter SSB | BW | SH/SL (index) | profile table |
| CW centre | IS | IS | round 50 Hz |
| RIT/XIT | RT/XT/RO | RT/XT, RC+RU/RD | |
| ATT/PRE | RA/PA | RA/PA | |
| NB | NB | NB 0/1/2 | KX2 on → NB1 |
| NR | DS flash bit 2 (K31) / SWH19 | NR 0/1/2 | KX2 on → NR1 |
| AGC | GT 002 fast / 004 slow | GC 2/1 | not GT! |
| RF gain | RG (~190–250) | RG 0–255 | scale |
| Keyer | KS | KS | |
| CW pitch | CW | menu (EX) | later |
| VOX/QSK | VX | VX | |
| APF key (CW) | IC c bit 5 | configurable (default BC toggle) | |
Not mirrored: AF gain (KX2 AF knob 0–60 → **laptop playback volume**), power (UI/scripts only),
KX2 XMIT/ATU/TX (never).

### 4.10 Audio
- Remote: GStreamer `pipewiresrc`/`alsasrc device=…` → `opusenc frame-size=10 bitrate=48000 audio-type=voice` →
  `rtpopuspay` → `udpsink`. Device from config (`hw:CARD=CODEC`).
- Local: `udpsrc` → `rtpjitterbuffer latency=40` (configurable 20–100) → `rtpopusdepay` → `opusdec` → `volume` → sink.
  Volume follows KX2 AF gain (0–60 → 0.0–1.0, curve configurable) – via GStreamer volume element (not system volume).
- Python starts/monitors the pipelines (subprocess `gst-launch-1.0` or `Gst` bindings if available), restarts on failure,
  shows status in UI. Target end-to-end ≤ 80 ms in LAN.
- Mute/attenuate RX audio on local side is NOT wanted for v1 (author wants to hear the rig's sidetone).

### 4.11 Web UI (local, aiohttp + WebSocket, plain HTML/JS, no build step)
- Shows: remote freq/mode/filter/RIT, TX state, bars for S/PO/SWR/ALC/COMP, connection + latency, audio status,
  KX2 warnings (power, CWT).
- Controls: power slider (max from config), NR level, NB, notch/beat cancel, AGC, break-in delay, ATU tune,
  CW tune, antenna, keyboard shortcuts. Shows real rig state, not last command.
- Bind to 127.0.0.1 by default.

### 4.12 Safety (must have tests)
- Watchdog: control link silent > 1 s → remote sends `KY0;`, `RX;`, and blocks further TX until reconnect.
- TX timeout (config, default 60 s continuous TX) → abort CW, `RX;`.
- Remote never keys on its own except: KY text from `cw` messages, ATU tune from explicit `raw AC111`.
- On exit/crash of remote: `KY0;`, `RX;`, restore AI mode.
- Tests must never talk to real hardware unless `ANYRIG_HW_TEST=1` is set.

---

## 5. Configuration

`config/local.example.yaml`
```yaml
remote: {host: 192.168.178.50, port: 7355}
kx2: {port: /dev/ttyUSB0, baud: 38400, k31: true}
wintest:
  cat_pty_link: /tmp/anyrig-com1
  winkeyer_pty_link: /tmp/anyrig-com2
router:
  ignore: [KS]
  to_both: [RC]
  translate: {}            # e.g. {"SWT34;": "CA1;"}
  block: [TX, RX, PS0, SR, KY]
kx2_keys:
  apf: beat_cancel_toggle  # what the KX2 APF key does on the remote rig (CW)
audio: {port: 7356, jitter_ms: 40, sink: default, af_curve: log}
webui: {host: 127.0.0.1, port: 8073}
```
`config/remote.example.yaml`
```yaml
listen: {host: 0.0.0.0, port: 7355}
rig: {profile: profiles/ts590sg.yaml, port: /dev/ttyUSB1, baud: 115200}
cw: {method: cat}          # cat | dtr
keyline: {port: /dev/ttyUSB0, key: dtr, ptt: rts}   # used for method dtr / later paddle
audio: {source: "hw:CARD=CODEC", port: 7356, bitrate: 48000, frame_ms: 10}
safety: {link_timeout_ms: 1000, tx_timeout_s: 60, max_power_w: 100}
```
`profiles/ts590sg.yaml` holds: driver `kenwood`, init (`AI2;`), command templates, value tables
(modes, filter steps, SH/SL tables, GC values), meter scale (0–30), KY rules (24 chars, no leading space),
prosign map, mode-dependent commands, capabilities, ignore list (`RS`).

---

## 6. Milestones (each: implement, tests with simulators, short README section, then author tests on hardware)

- **M0 Skeleton**: package layout, config loading, logging, simulators made reusable for tests
  (`tools/kx2_sim.py`, `tools/ts590_sim.py` → `tests/sim/`; must model AI behaviour from section 3), CI (pytest).
- **M1 Local CAT proxy**: kx2-local opens KX2, provides Win-Test pty with K3 emulation answered from state,
  polls KX2 with priority scheduler, logs all Win-Test traffic. No remote yet. Acceptance: Win-Test works as with the
  real KX2 (frequency, mode, spot click), log shows all commands.
- **M2 Remote + Kenwood driver + control link**: anyrig-remote with TS-590SG profile; KX2 → rig mirroring for all
  functions in 4.9; Win-Test spot click → rig immediately. Acceptance: turning the KX2 VFO moves the TS-590SG
  (target ≤ 150 ms), spot click QSYs both rigs.
- **M3 Reverse sync + router**: rig → KX2, loop protection, TX/ATU suppression, CW tune follow-up, Lua passthrough rules.
  Acceptance: author's scripts (FW…, RA, PA, AN, CA1, RC, AC111) work; KX2 shows the rig's state; no oscillation.
- **M4 WinKeyer emulation + KY**: Acceptance: Win-Test F1–F8 macros, speed keys and ESC work; no lost word spaces.
  **Accepted on hardware 10 Nov 2026** (26 WPM: clean gaps, CQ repeat 3.0 s → 3.04–3.15 s after keying end,
  ESC, speed keys, keyboard mode).
  **DTR keying** (`cw.method: dtr`, `remote/dtrkeyer.py`) accepted the same evening at 26 and 32 WPM: exact element
  timing (C→Q 648 ms, word gap 323 ms at 26 WPM), echo late in the character (`echo_budget_ms` 220) so Win-Test
  completes a call typed while sending (DL [Ins] 1XYZ). Echo delivery over WLAN: median 7 ms, p99 19 ms, max 100 ms.
- **M5 Audio**: Acceptance: sidetone and RX audio on the laptop, measured latency documented, AF knob = volume.
  **Accepted on hardware 11 Nov 2026**: rig speaker -> laptop 96 ms (jitter buffer 25 ms) / 102 ms (40 ms, WLAN
  default), measured acoustically; GStreamer defaults gave 230 ms, pipewiresink 237 ms. No dropouts in 93 s with
  Opus PLC (before: ~4 per 2 min). AF knob = volume works (GStreamer volume element, not the mixer slider).
- **M6 Web UI + safety**: meters, buttons, watchdog/TX timeout tests (pull the network cable → TX stops ≤ 1 s).
  **Accepted on hardware 11 Nov 2026**: all web UI controls (ATT, PRE, NB, NR, notch, beat cancel, AGC incl.
  off → on via `GC3`, antenna, CW tune, ATU tune); WLAN off during CQ → TS-590SG stops at once, CW works again
  right after the reconnect. Found on the way: Win-Test sends `FF` when it re-opens the WinKeyer (never text);
  on link loss kx2-local must echo what Win-Test still waits for, else the F keys stay dead until REOPEN;
  after a kx2-local restart Win-Test needs REOPEN (new ptys).
- **M7 Packaging & docs**: install guide (apt packages, dialout group, Wine COM links, Win-Test settings), systemd user
  units, profile writing guide. All documentation in English only (author's decision, 11 Nov 2026; no German quick
  start). WLAN power saving must be off permanently (NetworkManager `wifi.powersave = 2`); use `/dev/serial/by-id`.
  `anyrig-ports` (`local/ports.py`) holds Win-Test's ptys and hands the master fd to kx2-local (SCM_RIGHTS over a
  Unix socket), so kx2-local restarts need no REOPEN; it drains Win-Test's writes while kx2-local is away.

Later: paddle (RP2040 or serial CTS/DSR, time-stamped key events, DTR on remote), SSB TX + PTT, WireGuard guide,
drivers yaesu/icom/hamlib, KX3/K3 as control head.

---

## 7. Working rules for Claude Code
- Read `docs/measurements.md` before touching timing or protocol code.
- Keep the remote core rig-agnostic; rig specifics belong in driver + profile.
- Every protocol parser/formatter gets unit tests with real frames from the logs in this file.
- Simulators must reproduce the measured behaviour (KX2 1 Hz AI, TS-590SG immediate AI, `?` for mode-dependent
  commands, KY leading-space drop, ATU mode jump).
- No hardware access in tests; no TX commands in any default code path except the ones listed in 4.12.
- Code comments and docs in English; user-facing CLI messages in English (German translation later).
- Commit per milestone step with clear messages.

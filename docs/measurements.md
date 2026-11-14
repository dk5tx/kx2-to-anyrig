# Measurement notes

Notes from the hardware tests by DK5TX, 8–9 Nov 2026. The binding summary, including the later
measurements (M2–M6, 9–11 Nov 2026), is in `CLAUDE.md`, section 3. Tools used: `tools/kx2probe.py`,
`tools/catsniff.py`, `tools/ky_test.py`.

---

## 1. Station

**Local: laptop "stream"** (Linux Mint, Win-Test under Wine)
- KX2 on /dev/ttyUSB0, 38400 Bd. Firmware MCU 3.02 / DSP 1.52 (04 Aug 2022).
- Win-Test: Radio 1 = Elecraft K3, COM1 (38400 8-N-1), polling rate set to 100 ms.
- Win-Test COM1 used to have "CW PTT" on (DTR/RTS on the KX2's CAT port). Off in remote operation,
  because a pty has no modem lines.
- Sound card: card 0 "HDA Intel PCH" (ALC236).

**Remote: PC "shack"** (Linux Mint)
- TS-590SG via USB on /dev/ttyUSB1, 115200 Bd.
- /dev/ttyUSB0: RS-232 interface, DTR = KEY input of the TS-590SG, RTS = PTT.
- card 0 "HDA Intel PCH" (ALC221), card 1 "USB Audio CODEC" = TS-590SG.

---

## 2. KX2 (kx2probe, 8 Nov 2026)

Reference: Elecraft K3S/K3/KX3/KX2 Programmer's Reference, Rev. G5.

### Timing
- Auto information (AI2) reports at a fixed **once per second**, bundled (FA, FB, FR, FT). Too slow
  for VFO/AF.
- Polling: about 36 queries/s, reply time median 25 ms (8–27 ms, outlier 234 ms).
- During a band change no replies for up to about 1.5 s, then bundled.

### Reported by auto information (1 Hz)
AG, FA/FB, FR/FT, MD / MD$ (VFO B), DT, IF (incl. RIT/XIT), IS, RG, PA, RA, NB, KS, PC, MG, CP, AN,
GT (AGC 002/004), IC. On a band change a complete bundle (FA, FB, RT, RO, MD, PA, RA, AN, GT, NB, RG, IS …).

### Seen by polling
CW (pitch, 60 = 600 Hz), VX (VOX/QSK), ML (changes with the mode).

### Only by polling
- BW (filter): AI only reports FW0000 (K2 format).
- NL (NB level), LK (lock).

### IC status bits (table 4; bytes a–e)
- a: B6 BSET, B5 TX TEST, B4 mW, B3 message bank, B2 message playing, B1 CONFIG/BAND SEL, B0 preset.
- b: B5 VFO bands independent, B0 dual watch.
- c: B6 full QSK, **B5 dual passband CW / APF (confirmed)**, B4 VOX in CW, B1 sync DATA,
  B0 text to terminal.
- d: B6 VOX voice, B5 ESSB, B4 noise gate, B3 AM sync, B2 PL tone, B1/B0 offset.
- e: B6 10 Hz shift, B5 AM sync USB, B4 squelched, B1 OFS LED, B0 fast play.
- Idle state in USB: `80 80 94 C0 82`. Switching to CW: a.B0 and e.B6 → 1.

### DS (display)
- 8 characters (bit 7 = decimal point), icon byte, extra byte. Menu texts ("ON", "N/A", "-13 dB")
  are readable.
- Icon byte: B6 NB (confirmed), B5 ANT2, B4 PREAMP, B3 ATT, B1 RIT, B0 XIT.
- Extra byte **only in K31**: B5 RX ANT, B4 ATU, B3 CWT, **B2 NR (confirmed 98 → 9C)**, B1 NTCH,
  B0 MAN NOTCH. In K30 always 0x80.

### APF / notch (same key)
- CW: APF, IC c.B5; `AP` answers only in CW.
- SSB: auto notch, no trace in K30, in K31 presumably extra byte B1 (to be confirmed).

### Split
- Only with the same mode on VFO A/B, otherwise "SPL N/A". `FT1/FT0` by AI.

### TX on the KX2
- XMIT and ATU key the KX2 (TQ=1 for about 0.5 s, ATU sends a carrier). Run the KX2 in TX TEST / 0 W.

### Auto spot (CWT)
- Only a small FA correction visible; tunes to the local signal → off in remote operation.

### SWT/SWH (table 8A, KX2)
AF GAIN/MON 32 (H: NB), PRE/ATTN 19 (H: NR), FIL 27 (H: APF/AN), ATU 20 (H: PFn), XMIT 16 (H: TUNE),
KYR-SPT/MIC 34 (H: PWR), DATA 26, MSG 11, RATE 41, MODE 08, BAND 14, A/B 44 (H: A>B), RIT 18 (H: SPLIT),
DISP 09 (H: MENU), OFS/B 35 (H: CLR).

---

## 3. TS-590SG (kx2probe --rig ts590, ky_test, 8 Nov 2026)

### Timing
- AI2 reports **immediately**, turning the VFO about every 30 ms. Query reply about 2 ms.
- At start VFO B was active (FR1/FT1) → turning came as FB.

### Meters
- With AI2 by itself: RX continuously SM; TX every ~60 ms RM1 (SWR), RM2 (COMP), RM3 (ALC),
  SM (= PO); each 0–30.
- Example CW TX: SWR 0–1, ALC 15–20, PO 8–22.
- SWR scale (14 Nov 2026, read off the rig's display with all 30 segments lit): 1.5 at about segment 6,
  2 at about 11, 3 at 15, infinity at 30. This fits a bar linear in the reflection coefficient,
  |G| = RM1/30, so SWR = (1 + G) / (1 − G): 6 → 1.5, 10 → 2.0, 15 → 3.0. The web UI shows that value.
  Resolution near 1:1 is about 0.07 per segment; the rig's own SWR bridge reads low (reports of
  1.7:1 shown as about 1.2:1).

### TX
- `TX0;` / `RX;` immediately, plus XI.

### CW tune
- `CA1;` → frequency steps as FA/FB → `CA0;` after 0.24–0.6 s.

### Reported by AI
FA/FB/IF, RA, PA, VX, PR (PROC), PC (5 W steps), KS, SD (50 ms steps), AC (110 ATU on, 111 tuning,
000 off), FT, RT, XT, NT (00 off, 10 auto, 20 manual normal, 21 manual wide), BP, BC (0/1/2),
NR (0/1/2) + RL, NB (0/1/2), GC (0/1/2) + GT, CA, FL, FW, IS, SH/SL, AG, RG, MD.
Mode change bundle: EX037, MD, XI, UR, FS, FW+IS or SH+SL, PR.
`RS1/RS0` (undocumented) = MULTI/CH quick menu open/closed → ignore.

### Peculiarities
- **ATU tuning (AC111): briefly MD3 + TX0, then back** → do not mirror.
- `?` for mode-dependent commands: FW/IS only CW/FSK, SH/SL only SSB/AM/FM, NL only with NB on,
  RL only with NR on.

### KY (26 WPM, 5 W, dummy load)
- `KY ` + 24 characters + `;`; `KY;` → 0 free / 1 full; `KY0;` aborts.
- Prosigns: `[`BT `_`AR `<`AS `#`HH `>`SK `]`KN `\`BK `%`SN.
- Command to TX0: 11–16 ms.
- 59 characters in 3 packets sent without a gap; the second packet was accepted at once.
- **A space at the start of a packet is lost** ("DL1ABC" + " TEST" → "DL1ABCTEST"); at the end it stays.
- Abort: the current element is finished, then it stops; `RX;` after the break-in delay
  (419 ms with SD 500).
- Dit about 50 ms, dah 130–145 ms.

### Audio
- Sidetone on the USB audio confirmed (600 Hz, 48 kHz mono). Menu 06 = sidetone level (8/20).
- Menu 72 (USB OUTPUT, then 5) only raises the RX audio. Sidetone about −27 dBFS, about 17 dB above
  the noise.

---

## 4. Win-Test ↔ "K3" (catsniff, 8 Nov 2026)

- Polls only `IF;` and `FB;` alternately, about 450 ms each (Win-Test's own pace). No AI/K31/init.
- Spot click: `FA…;` `MD3;` `FT0;`.
- IF format: `IF00014074002     -000000 0002000001 ;`
- `SWT34;` comes from the KX2 variant of the CW tune script (not used in remote operation).
- The display in Win-Test does not get faster through the proxy (Win-Test itself polls only every
  ~450 ms).
- Captured later (9 Nov 2026, see CLAUDE.md 3.2): spot to VFO B, split, frequency entry.

---

## 5. Author's Lua scripts (TS-590SG variants)

| Script | Command(s) | Handling |
|---|---|---|
| 100HZ … 500HZ | FW0100 … FW0500 | to the remote rig |
| 5&50Hz_UP / _DOWN | UP10 / DN10 | to the remote rig, the frequency comes back by AI |
| ATT / PreAmp | RA00/RA01, PA0/PA1 | to the remote rig |
| RxAnt (N6TV) | AN909/AN919 + RA + PA | to the remote rig |
| ATTUNE (key ä) | AC111 (transmits!) | to the remote rig, no mirroring while tuning |
| CWTUNE | CA1 | to the remote rig, mirror after CA0 |
| CLEARRIT | RC | to the remote rig **and** the KX2 |
| AltF9/AltF10 → set590KeyerSpeed (N6TV) | KS0nn (Win-Test speed + offset) | ignored in remote operation (the WinKeyer emulation sets the speed) |

Decisions: two-way sync (remote → KX2 for everything the KX2 can set by CAT); ATU only by Win-Test script.

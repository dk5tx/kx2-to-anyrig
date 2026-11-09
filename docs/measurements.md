# Measurement notes (German) / Messprotokolle

Original notes from the hardware tests by DK5TX, 8–9 Nov 2026 (German).
The binding English summary is in `CLAUDE.md`, section 3. Tools used: `tools/kx2probe.py`,
`tools/catsniff.py`, `tools/ky_test.py`.

---

## 1. Rahmendaten

**Lokal: Laptop "stream"** (Linux Mint, Win-Test unter Wine)
- KX2 an /dev/ttyUSB0, 38400 Bd. Firmware MCU 3.02 / DSP 1.52 (04.08.2022).
- Win-Test: Radio 1 = Elecraft K3, COM1 (38400 8-N-1), Polling-Rate 100 ms eingestellt.
- Win-Test COM1 bisher mit "CW PTT" (DTR/RTS auf dem CAT-Port des KX2). Im Remote-Betrieb aus, da ein pty keine Modemleitungen hat.
- Soundkarte: card 0 "HDA Intel PCH" (ALC236).

**Remote: PC "shack"** (Linux Mint)
- TS-590SG per USB an /dev/ttyUSB1, 115200 Bd.
- /dev/ttyUSB0: RS-232-Interface, DTR = KEY-Eingang des TS-590SG, RTS = PTT.
- card 0 "HDA Intel PCH" (ALC221), card 1 "USB Audio CODEC" = TS-590SG.

---

## 2. KX2 (kx2probe, 08.11.2026)

Referenz: Elecraft K3S/K3/KX3/KX2 Programmer's Reference, Rev. G5.

### Timing
- Auto-Info (AI2) sendet fest **1× pro Sekunde**, gebündelt (FA, FB, FR, FT). Für VFO/AF zu langsam.
- Polling: ca. 36 Abfragen/s, Antwortzeit median 25 ms (8–27 ms, Ausreißer 234 ms).
- Bei Bandwechsel bis ca. 1,5 s keine Antworten, danach gebündelt.

### Per Auto-Info gemeldet (1 Hz)
AG, FA/FB, FR/FT, MD / MD$ (VFO B), DT, IF (inkl. RIT/XIT), IS, RG, PA, RA, NB, KS, PC, MG, CP, AN, GT (AGC 002/004), IC.
Bei Bandwechsel komplettes Paket (FA, FB, RT, RO, MD, PA, RA, AN, GT, NB, RG, IS …).

### Per Poll gesehen
CW (Pitch, 60 = 600 Hz), VX (VOX/QSK), ML (ändert sich mit dem Mode).

### Nur per Poll
- BW (Filter): AI meldet nur FW0000 (K2-Format).
- NL (NB-Pegel), LK (Sperre).

### IC-Statusbits (Tabelle 4; Byte a–e)
- a: B6 BSET, B5 TX TEST, B4 mW, B3 MSG-Bank, B2 MSG spielt, B1 CONFIG/BAND SEL, B0 Preset.
- b: B5 VFO-Bänder unabhängig, B0 Dual Watch.
- c: B6 Full QSK, **B5 Dual-Passband CW / APF (bestätigt)**, B4 VOX in CW, B1 Sync DATA, B0 Text-to-terminal.
- d: B6 VOX Voice, B5 ESSB, B4 Noise Gate, B3 AM Sync, B2 PL-Ton, B1/B0 Ablage.
- e: B6 10-Hz-Shift, B5 AM Sync USB, B4 squelched, B1 OFS-LED, B0 Fast Play.
- Grundzustand USB: `80 80 94 C0 82`. Wechsel auf CW: a.B0 und e.B6 → 1.

### DS (Display)
- 8 Zeichen (Bit 7 = Dezimalpunkt), Icon-Byte, Zusatz-Byte. Menütexte ("ON", "N/A", "-13 dB") lesbar.
- Icon-Byte: B6 NB (bestätigt), B5 ANT2, B4 PREAMP, B3 ATT, B1 RIT, B0 XIT.
- Zusatz-Byte **nur in K31**: B5 RX ANT, B4 ATU, B3 CWT, **B2 NR (bestätigt 98 → 9C)**, B1 NTCH, B0 MAN NOTCH. In K30 immer 0x80.

### APF / Notch (gleiche Taste)
- CW: APF, IC c.B5; `AP` antwortet nur in CW.
- SSB: Auto-Notch, in K30 keine Spur, in K31 vermutlich Zusatz-Byte B1 (noch zu bestätigen).

### Split
- Nur bei gleichem Mode von VFO A/B, sonst "SPL N/A". `FT1/FT0` per AI.

### TX am KX2
- XMIT und ATU tasten (TQ=1 ca. 0,5 s, ATU sendet Träger). KX2 in TX TEST / 0 W betreiben.

### Auto Spot (CWT)
- Nur kleine FA-Korrektur sichtbar; stimmt auf lokales Signal ab → im Remote-Betrieb aus.

### SWT/SWH (Tabelle 8A, KX2)
AF GAIN/MON 32 (H: NB), PRE/ATTN 19 (H: NR), FIL 27 (H: APF/AN), ATU 20 (H: PFn), XMIT 16 (H: TUNE),
KYR-SPT/MIC 34 (H: PWR), DATA 26, MSG 11, RATE 41, MODE 08, BAND 14, A/B 44 (H: A>B), RIT 18 (H: SPLIT),
DISP 09 (H: MENU), OFS/B 35 (H: CLR).

---

## 3. TS-590SG (kx2probe --rig ts590, ky_test, 08.11.2026)

### Timing
- AI2 meldet **sofort**, VFO-Drehen ca. alle 30 ms. Abfrage-Antwort ca. 2 ms.
- Beim Start war VFO B aktiv (FR1/FT1) → Drehen kam als FB.

### Meter
- Mit AI2 von selbst: RX laufend SM; TX alle ca. 60 ms RM1 (SWR), RM2 (COMP), RM3 (ALC), SM (= PO); je 0–30.
- Beispiel CW-TX: SWR 0–1, ALC 15–20, PO 8–22.

### TX
- `TX0;` / `RX;` sofort, dazu XI.

### CW Tune
- `CA1;` → Frequenzschritte als FA/FB → `CA0;` nach 0,24–0,6 s.

### Per AI gemeldet
FA/FB/IF, RA, PA, VX, PR (PROC), PC (5-W-Schritte), KS, SD (50-ms-Schritte), AC (110 ATU ein, 111 Abgleich, 000 aus),
FT, RT, XT, NT (00 aus, 10 Auto, 20 manuell normal, 21 manuell breit), BP, BC (0/1/2), NR (0/1/2) + RL,
NB (0/1/2), GC (0/1/2) + GT, CA, FL, FW, IS, SH/SL, AG, RG, MD.
Mode-Wechsel-Paket: EX037, MD, XI, UR, FS, FW+IS bzw. SH+SL, PR.
`RS1/RS0` (undokumentiert) = Schnellmenü MULTI/CH offen/zu → ignorieren.

### Besonderheiten
- **ATU-Abgleich (AC111): kurz MD3 + TX0, danach zurück** → nicht zurückspiegeln.
- `?` bei modeabhängigen Befehlen: FW/IS nur CW/FSK, SH/SL nur SSB/AM/FM, NL nur NB an, RL nur NR an.

### KY (26 WPM, 5 W, Dummy Load)
- `KY ` + 24 Zeichen + `;`; `KY;` → 0 frei / 1 voll; `KY0;` Abbruch.
- Prosigns: `[`BT `_`AR `<`AS `#`HH `>`SK `]`KN `\`BK `%`SN.
- Befehl bis TX0: 11–16 ms.
- 59 Zeichen in 3 Stücken ohne Lücke gesendet; zweites Stück sofort angenommen.
- **Leerzeichen am Stückanfang geht verloren** ("DL1ABC" + " TEST" → "DL1ABCTEST"); am Ende bleibt es.
- Abbruch: laufendes Element wird beendet, dann Schluss; `RX;` nach Break-in-Delay (419 ms bei SD 500).
- Dit ca. 50 ms, Dah 130–145 ms.

### Audio
- Mithörton auf USB-Audio bestätigt (600 Hz, 48 kHz mono). Menü 06 = Mithörpegel (8/20).
- Menü 72 (USB OUTPUT, jetzt 5) hebt nur RX-Audio. Mithörton ca. -27 dBFS, Abstand zum Rauschen ca. 17 dB.

---

## 4. Win-Test ↔ "K3" (catsniff, 08.11.2026)

- Fragt nur `IF;` und `FB;` abwechselnd ab, je ca. 450 ms (eigenes Tempo von Win-Test). Kein AI/K31/Init.
- Spot-Klick: `FA…;` `MD3;` `FT0;`.
- IF-Format: `IF00014074002     -000000 0002000001 ;`
- `SWT34;` stammt aus der KX2-Variante des CW-Tune-Scripts (wird im Remote-Betrieb nicht genutzt).
- Die Anzeige in Win-Test wird durch den Proxy nicht schneller (Win-Test fragt selbst nur alle ~450 ms).
- Noch mitzuschneiden: Spot an VFO B, Split, Band/Mode über Win-Test, Frequenzeingabe.

---

## 5. Lua-Scripts des Autors (TS-590SG-Varianten)

| Script | Befehl(e) | Behandlung |
|---|---|---|
| 100HZ … 500HZ | FW0100 … FW0500 | an Remote |
| 5&50Hz_UP / _DOWN | UP10 / DN10 | an Remote, Frequenz kommt per AI zurück |
| ATT / PreAmp | RA00/RA01, PA0/PA1 | an Remote |
| RxAnt (N6TV) | AN909/AN919 + RA + PA | an Remote |
| ATTUNE (Taste ä) | AC111 (sendet!) | an Remote, Rückspiegelung während Abgleich aus |
| CWTUNE | CA1 | an Remote, nach CA0 zurückspiegeln |
| CLEARRIT | RC | an Remote **und** KX2 |
| AltF9/AltF10 → set590KeyerSpeed (N6TV) | KS0nn (Win-Test-Tempo + Versatz) | im Remote-Betrieb ignorieren (WinKeyer-Emulation bestimmt das Tempo) |

Entscheidungen: Zwei-Wege-Synchronisation (Remote → KX2 für alles, was der KX2 per CAT setzen kann);
ATU nur per Win-Test-Script.

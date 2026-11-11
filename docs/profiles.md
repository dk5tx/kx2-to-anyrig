# Writing a rig profile

anyrig-remote talks to the home rig through a **driver** (one per protocol family) and a
**profile** (one YAML file per model). Today there is one driver, `kenwood` (Kenwood-style ASCII
CAT: `FA00014025000;`), and one profile, `profiles/ts590sg.yaml`. A new Kenwood-style model
should only need a new profile. Other families (Yaesu, Icom CI-V, Hamlib) need a driver first.

Start by copying `profiles/ts590sg.yaml` and change what differs. Point the remote config to it:

```yaml
rig: {profile: profiles/my-rig.yaml, port: /dev/serial/by-id/usb-…, baud: 115200}
```

## What the driver does itself

The `kenwood` driver handles these commands in code. A model needs the same formats as the
TS-590SG for them (or a code change):

| Command | Used for |
|---|---|
| `IF` | frequency, RIT/XIT on and offset, TX, mode, split (TS-590SG layout) |
| `MD`, `DA` | mode (codes from `modes`), DATA modes via `data_modes` |
| `FR`, `FT` | RX / TX VFO, split |
| `FW`, `IS` | CW/FSK filter bandwidth and centre |
| `SH`, `SL` | SSB/AM/FM high cut and low cut (index tables) |
| `RC`, `RU`, `RD` | RIT offset (no absolute set: clear, then step) |
| `AC` | ATU state and tuning |
| `SM`, `RM1`–`RM3` | meters (S/PO, SWR, COMP, ALC); sent by the rig with `AI2` |
| `TX`, `RX` | TX state (read only – the remote never sends them on its own) |

## Profile keys

| Key | Meaning |
|---|---|
| `name`, `driver`, `baud` | display name, `kenwood`, default baud rate |
| `init` | sent after opening the port, e.g. `["AI2;", "FR0;"]`. The rig must report changes by itself (auto information) – the remote does not poll. |
| `shutdown` | sent on exit and by the safety watchdog, e.g. `["KY0;", "AI0;"]` |
| `initial_queries` | queried once at start to build the state. Mode-dependent commands may answer `?`; that is fine. |
| `ignore` | frame prefixes to ignore (menu indicators and the like) |
| `modes` | rig mode digit → normalized mode (`LSB USB CW CW-R FM AM FSK FSK-R`) |
| `data_modes` | normalized DATA mode → rig mode + `DA1` |
| `cw_modes` | modes in which `FW`/`IS` are valid |
| `commands` | simple settings, see below |
| `agc_on_from_off` | command that switches AGC on again from off (TS-590SG: `GC3;`) |
| `on_levels` | what "on" from the KX2 means for multi-level functions (e.g. `nr: 1` → NR1, not NR2) |
| `filter` | `fw_steps` (CW bandwidths the rig accepts), `sh`/`sl` (SSB index → Hz), `ssb_table` (bandwidth → low/high cut), `is_range`, `is_step` |
| `rit.max` | largest RIT offset in Hz |
| `meters.max` | full scale of `SM`/`RM` values |
| `cw` | text keying, see below |
| `actions` | named toggles the KX2 keys and the web UI can use (`kx2_keys` in the local config) |

### `commands`

Each entry maps a normalized key to a CAT command:

```yaml
att:    {cmd: RA, type: int,  slice: [0, 2], set: "RA{v:02d};"}
agc:    {cmd: GC, type: enum, map: {"0": "off", "1": "slow", "2": "fast"}, set: "GC{v};"}
rf_gain: {cmd: RG, type: scale, max: 255, set: "RG{v:03d};"}
```

- `cmd`: the command prefix in the rig's reports.
- `type`: `int`, `bool`, `enum` (with `map`), `scale` (0..1 ↔ 0..`max`) or `raw`.
- `slice`: which characters of the payload hold the value (default: all).
- `set`: Python format string for setting it (`{v}`). Without `set` the key is read only.

The normalized keys the local side knows are listed in `CLAUDE.md` section 4.1 (`att`, `preamp`,
`nb`, `nr`, `nr_level`, `notch`, `notch_freq`, `beat_cancel`, `agc`, `keyer_wpm`,
`break_in_delay`, `power`, `vox`, `rf_gain`, `antenna`, …). Leave out what the rig does not have.

### `cw`

```yaml
cw:
  method: ky            # text keying by the rig's own keyer
  packet_len: 24        # characters per packet
  packet: "KY {text};"
  query: "KY;"          # buffer state: full_reply / free_reply
  full_reply: KY1
  free_reply: KY0
  abort: "KY0;"
  poll_ms: 50
  word_gap_units: 6     # how long the rig makes a word gap (measure it!)
```

The TS-590SG drops a space at the start of a packet and keys the padding of a short packet as
a word gap, so packets always hold complete words. Check your rig with `tools/ky_test.py`
(transmits – dummy load). If the rig cannot key text, use `cw: {method: dtr}` in the remote
config and key its KEY input over a serial DTR line instead.

## Checking a new profile

1. `python3 tools/kx2probe.py --rig ts590 --port /dev/ttyUSB1 --baud 115200` shows which values
   the rig reports by itself and which it does not.
2. Run `python3 -m anyrig.remote -c config/remote.yaml --traffic-log rig.log` and watch every
   frame in `rig.log` while you turn knobs on the rig and on the KX2.
3. Add a simulator in `tests/sim/` and tests if you want the profile in the project – pull
   requests welcome.

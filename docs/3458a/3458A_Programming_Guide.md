# Keysight / HP / Agilent 3458A Multimeter — Programming Guide

A self-contained reference for writing host-side drivers (C/C++/Python) and
troubleshooting the 3458A 8½-digit multimeter over GPIB. Distilled from the
official 3458A User's Guide (Keysight 03458-90014, Edition 10, March 2023).

---

## 1. Instrument at a glance

| Attribute | Value |
|---|---|
| Resolution (DCV) | up to 8.5 digits (28-bit ADC) |
| DCV ranges | 100 mV, 1 V, 10 V, 100 V, 1000 V (full-scale 120%) |
| Input Z (DCV ≤10 V) | >10 GΩ (FIXEDZ OFF) or 10 MΩ (FIXEDZ ON) |
| DCI ranges | 100 nA … 1 A (8 ranges) |
| OHM/OHMF ranges | 10 Ω … 1 GΩ (9 ranges, 2- or 4-wire) |
| ACV/ACI bandwidth | 1 Hz – 10 MHz (sync), 10 Hz – 2 MHz (analog), 20 Hz – 10 MHz (random) |
| AC current bandwidth | 1 Hz – 100 kHz |
| FREQ/PER measurement | 1 Hz – 10 MHz (V) / 1 Hz – 100 kHz (I) |
| Max DCV reading rate | 100 000 readings/s (DCV digitizing, 16 bits) |
| Track/hold path | 12 MHz BW, 2 ns aperture |
| Sub-sampling effective rate | up to 100 M samples/s (10 ns intervals) |
| Reading memory | 14 kB std, 148 kB w/ Option 001 |
| Interface | IEEE-488 (GPIB), default address 22 |
| Language | HPML (HP Multimeter Language) |
| Power | 100/120/220/240 VAC, 48–66 / 360–420 Hz, ≤30 W |
| Operating temp | 0 °C – 55 °C |

The 3458A is GPIB-only. There is no LXI/USB/Ethernet on the instrument itself.
Hosts typically use a National Instruments GPIB-USB-HS or Prologix
GPIB-USB/GPIB-Ethernet adapter.

---

## 2. Front and rear panels

### Front panel
- **HI / LO**: voltage input (also used for DCI shunt, freq voltage input).
- **Ω Sense HI / LO**: 4-wire ohms / DCV ratio reference.
- **I**: current input (1 A 250 V fuse, ±1.5 A peak / ≤1.25 A RMS).
- **Guard**: separate guard binding post; Guard switch chooses Open or "to LO".
- **Front/Rear** terminals selector (depressed = rear).
- Function keys (DCV, ACV, OHM, DCI, ACI, FREQ; shifted: ACDCV, OHMF, ACDCI, PER).
- Configuration keys: Auto Cal, NPLC, Auto Zero, Offset Comp Ω, Trig, N Rdgs/Trig, Recall State, Store State.
- MENU keys (alphabetic command access C, E, L, N, R, S, T).
- Numeric / user-defined keys f0–f9 (shifted), Local, Address, Recall, Reset, Test, Error, Clear, Back Space, Enter.

### Rear panel
- 24-pin GPIB connector.
- Ext Trig BNC (TTL, neg-edge, ≥250 ns pulse, 5 MHz BW).
- Ext Out BNC (TTL pulse, NEG/POS programmable).
- Line voltage selector switches and fuse holder.
- Chassis ground lug.
- Option 001/002 indicator stickers.

### Input ratings (do not exceed)

| Path | Rated | Non-destructive max |
|---|---|---|
| HI ↔ LO | ±1000 V peak | ±1200 V peak |
| HI/LO Ω Sense ↔ LO | ±200 V peak | ±350 V peak |
| HI ↔ LO Ω Sense | ±200 V peak | ±350 V peak |
| LO ↔ Guard | ±200 V peak | ±350 V peak |
| Guard ↔ Earth | ±500 V peak | ±1000 V peak |
| Any input ↔ Earth | ±1000 V peak | ±1500 V peak |
| Front ↔ Rear terminals | ±1000 V peak | ±1500 V peak |

---

## 3. GPIB / HPML basics

### Addressing
- Factory address: 22 (decimal). Combined with a typical interface select code
  of 7 the BASIC-style numeric is 722. Programmatically, only the device
  address (0–31) matters; talk address = device + 0x40, listen = device + 0x20.
- Setting address 31 puts the instrument in *Talk Only* (sends ASCII to a
  GPIB-only printer). Cannot be used with a controller.
- ADDRESS command is front-panel only. Cannot change address from remote.

### Command structure
- Commands are ASCII, case-insensitive headers (e.g. `DCV`, `NRDGS`, `TARM`).
- Parameters separated by commas. Multiple commands separated by `;`.
- Termination: CR, LF, semicolon, or EOI on last byte ends a message.
- A trailing LF on `OUTPUT 722;"TEST"` will hold the bus until command finishes
  *unless* INBUF ON or you suppress LF (BASIC `OUTPUT 722 USING "#,K";"TEST;"`).
- Header is optional in some commands: `DCV 10` ≡ `FUNC DCV,10`.
- Numbers can be integer / float / exponential. Floats are rounded to int when
  required (e.g. `SUB 2.49` → `SUB 2`, `SUB 2.5` → `SUB 3`).
- Default a parameter by omitting it or sending `-1`. From remote you may use
  consecutive commas: `ACV 10,,.01`.

### Querying
A query ends with `?`. The response goes to the output buffer until read.
Standard queries: `ID?`, `ERR?`, `ERRSTR?`, `AUXERR?`, `STB?`, `LINE?`, `LFREQ?`,
`MCOUNT?`, `MSIZE?`, `OPT?`, `REV?`, `TEMP?`, `CAL?`, `CALNUM?`, `ISCALE?`,
`SSPARM?`. Any setting command can be queried by appending `?` (e.g. `AZERO?`,
`NRDGS?`).

`QFORMAT` controls response style:
- `QFORMAT NUM` — pure numeric (default for many examples).
- `QFORMAT NORM` (default) — bus responses numeric, display alpha.
- `QFORMAT ALPHA` — alpha header + alpha response (e.g. `AZERO ON`).

Query responses are always ASCII. After the response the output format
returns to whatever OFORMAT specified.

### Identification
```
OUTPUT 722;"ID?"   -> "HP3458A" (older firmware) / "Keysight 3458A" (recent)
OUTPUT 722;"REV?"  -> "<master_fw>,<slave_fw>"
OUTPUT 722;"OPT?"  -> "0" (no opts) or "1" (extended memory)
```

---

## 4. Power-on, RESET, PRESET states

The instrument has three preset profiles plus the power-on default.

### Power-on / front-panel `Reset` key
- Performs power-on self-test, sets PWR-ON SRQ bit, clears reading memory, and
  reloads the *power-on state* (Table 2-1 of UG):
  - `ACBAND 20,2E6`, `AZERO ON`, `DCV AUTO`, `DEFEAT OFF`, `DELAY -1`
  - `DISP ON`, `EMASK 32767`, `END OFF`, `EXTOUT ICOMP,NEG`, `FIXEDZ OFF`
  - `FSOURCE ACV`, `INBUF OFF`, `LEVEL 0,AC`, `LFILTER OFF`, `LFREQ <50|60>`
  - `LOCK OFF`, `MATH OFF`, `MEM OFF` (FIFO), `MFORMAT SREAL`, `MMATH OFF`
  - `NDIG 7`, `NPLC 10`, `NRDGS 1,AUTO`, `OCOMP OFF`, `OFORMAT ASCII`
  - `QFORMAT NORM`, `RATIO OFF`, `RQS 0` (or 8 if PWR-ON SRQ left set)
  - `SETACV ANA`, `SLOPE POS`, `SSRC LEVEL,AUTO`, `SWEEP 100E-9,1024`
  - `TARM AUTO`, `TBUFF OFF`, `TIMER 1`, `TRIG AUTO`
  - Math regs: `DEGREE=20 REF=1 SCALE=1 RES=50 PERC=1`.

### `RESET` command (alphabetic menu / remote)
- Same effect as power-on in terms of register state, but **does not** run the
  power-on sequence (so it does not destroy stored states/subprograms or set
  PWR-ON SRQ bit).
- Aborts readings, clears error & aux-error registers, clears status register
  except PWR-ON SRQ, clears reading memory.
- Always send GPIB Device Clear (`CLEAR 722`) before `RESET` from remote, in
  case the bus is held.

### `PRESET NORM` (recommended starting point for remote)
Configures the meter for remote operation. Differs from `RESET` in:
- `MEM OFF` (last operation set to FIFO).
- `BEEP ON`, `MMATH OFF`.
- `DCV AUTO`, `NDIG 6`, `NPLC 1` (faster default).
- `INBUF OFF`, `TARM AUTO`, `TRIG SYN` (suspends readings until controller asks).
- `OFORMAT ASCII`, `MFORMAT SREAL`, `QFORMAT NORM`.

### `PRESET FAST` (fast readings)
Builds on PRESET NORM but sets `DCV 10`, `AZERO OFF`, `DISP OFF`,
`MFORMAT DINT`, `OFORMAT DINT`, `TARM SYN`, `TRIG AUTO`. Used for high
throughput on a fixed range.

### `PRESET DIG` (DCV digitizing)
Adds: `TARM HOLD`, `TRIG LEVEL`, `LEVEL 0,AC`, `NRDGS 256,TIMER`,
`TIMER 20E-6`, `APER 3E-6`, `MFORMAT SINT`, `OFORMAT SINT`, `AZERO OFF`,
`DELAY 0`, `DISP OFF`. 50 kS/s, 256 samples, level-triggered on rising 0 V.

> Recommended driver-init sequence: `CLEAR <addr>` → `OUTPUT "PRESET NORM"` →
> `OUTPUT "OFORMAT ..."` → `OUTPUT "INBUF ON"` → application config.

---

## 5. Configuration

### Selecting a measurement function

`FUNC <function>[,<max_input>[,%_resolution]]` or directly the function header:

| FUNC | Description |
|---|---|
| DCV | DC voltage |
| ACV | AC voltage (method per SETACV) |
| ACDCV | AC + DC voltage |
| OHM | 2-wire resistance |
| OHMF | 4-wire resistance |
| DCI | DC current |
| ACI | AC current |
| ACDCI | AC + DC current |
| FREQ | Frequency (input via FSOURCE) |
| PER | Period |
| DSAC | Direct-sampling, AC-coupled (digitize) |
| DSDC | Direct-sampling, DC-coupled |
| SSAC | Sub-sampling, AC-coupled |
| SSDC | Sub-sampling, DC-coupled |

`max_input` selects a fixed range or `AUTO`/`-1` for autorange. Specify the
absolute value of the largest expected signal magnitude.

### Range tables (selection by max_input)

DCV: `0..0.12 → 100 mV`, `..1.2 → 1 V`, `..12 → 10 V`, `..120 → 100 V`,
`..1000 → 1000 V`.

OHM/OHMF: powers of 10 from 10 Ω to 1 GΩ; full scale = 120 % of nominal.

DCI: 100 nA, 1 µA, 10 µA, 100 µA, 1 mA, 10 mA, 100 mA, 1 A.

ACI/ACDCI: 100 µA, 1 mA, 10 mA, 100 mA, 1 A (no 100 nA / 10 µA shunts on AC).

For DSAC/DSDC under DINT format, the **full scale = 5× the nominal range**
(e.g. 10 V range → 50 V FS) because the track/hold can be over-driven within
±120 % up to 12 MHz without slew-rate errors.

### `RANGE [max_input][,%_res]` / `R` shortcut
Selects a fixed range without changing the function. Same `max_input` rules.

### Autorange
`ARANGE ON|OFF|ONCE` (default ON). Autorange samples the input before each
reading and picks a range — adds time. **Cannot be used** with DSAC/DSDC,
SSAC/SSDC, with TIMER sample event, or with the SWEEP command.

Use `ARANGE ONCE` to range once at start of a measurement series, then lock.

### Integration time and resolution

Three parameters interlock for DC/Ω/analog AC: NPLC, APER, %resolution.
- `NPLC <plc>` — power-line-cycles. `0` = minimum (500 ns aperture, no NMR).
  Range: 0–1 PLC in 0.000005/0.000006 PLC steps; 1–10 in 1 steps; 10–1000 in 10 steps.
  >10 PLC averages multiple 10-PLC readings.
- `APER <seconds>` — A/D integration time in seconds. Valid range 0–1 s in
  100 ns increments. Specifying any value <500 ns selects the minimum
  aperture, which is 500 ns. Unlike NPLC ≥ 10, APER does not average
  multiple readings — it integrates a single reading for the requested time.
- `RES <pct>` or %_res in `FUNC` — resolution as percent of `max_input`.
  If RES is given *before* NPLC/APER, the larger integration wins; if *after*,
  RES is ignored.

NMR @ ≥1 PLC ≈ 60 dB (≥1000 PLC ≈ 80 dB). For digitizing, APER directly is the
sample aperture (3 µs default in PRESET DIG → 17-bit resolution).

`AZERO ON|OFF|ONCE`: when ON, doubles the time per reading by interleaving
zero readings. Disable for high-speed sampling, but **always leave it ON for
4-wire ohms** unless lead resistance is known stable.

`OCOMP ON|OFF`: offset-compensated ohms (10 Ω – 100 kΩ ranges only). Doubles
read time but cancels series thermal EMF.

`FIXEDZ ON|OFF`: forces 10 MΩ input Z on all DCV ranges (otherwise >10 GΩ on
≤10 V ranges). Useful to keep Z constant across range changes.

`LFREQ <Hz>` or `LFREQ LINE`: reference frequency for NMR. `LINE` measures and
locks to actual mains. Cycling power resets to rounded 50/60.

### AC measurement methods (`SETACV`)
| Method | Range | Best accuracy | Repetitive needed | Rdg/s min/max |
|---|---|---|---|---|
| ANA  | 10 Hz – 2 MHz | 0.03 % | No | 0.8 / 50 |
| SYNC | 1 Hz – 10 MHz | 0.01 % | **Yes** | 0.025 / 10 |
| RNDM | 20 Hz – 10 MHz | 0.10 % | No | 0.025 / 45 |

`ACBAND <low_Hz>,<high_Hz>` describes input bandwidth so the meter can pick
optimal settling and sampling. SYNC uses the LEVEL (or EXT) sync source.
Random can be used for noise. ANA is fastest / lowest BW.

For FREQ/PER: pick `FSOURCE ACV|ACDCV|ACI|ACDCI`, then `%_res` selects gate
time:

| %_res | Gate time | Digits |
|---|---|---|
| .00001 | 1 s | 7 |
| .0001 | 100 ms | 7 |
| .001 | 10 ms | 6 |
| .01 | 1 ms | 5 |
| .1 | 100 µs | 4 |

### Ratio measurements
Apply DC reference (≤±12 V) to **Ω Sense HI/LO**, signal to Input HI/LO.
`RATIO ON` after setting the signal function. Reference auto-ranges. The
Ω Sense LO and Input LO must share a common reference (≤0.25 V difference).

---

## 6. Triggering hierarchy — *fundamental*

Three sequential events gate every reading or burst:

```
START → [TRIGGER ARM EVENT] → [TRIGGER EVENT] → [SAMPLE EVENT] → reading(s)
```

| Command | Selects |
|---|---|
| `TARM <event>[,<arming_count>]` | Trigger arm event |
| `TRIG <event>` | Trigger event |
| `NRDGS <count>,<event>` | Number of readings per trigger and the sample event |
| `SWEEP <interval>,<count>` | shortcut for `NRDGS n,TIMER` + `TIMER` |

After the sample event fires, one reading is taken, and is repeated until the
NRDGS count is reached. Then control returns to the trigger event (next
trigger). When the arming count exhausts, the trigger arm becomes HOLD.

### Event values

| Event | TARM | TRIG | NRDGS | Description |
|---|---|---|---|---|
| AUTO | ✓ | ✓ | ✓ | Fires immediately when prerequisite met |
| EXT | ✓ | ✓ | ✓ | Negative-edge on Ext Trig BNC (≥250 ns) |
| HOLD | ✓ | ✓ | – | Suspends measurements |
| LEVEL[a] | – | ✓ | ✓ | Input crosses programmable voltage on slope |
| LINE[b] | – | ✓ | ✓ | Mains zero-crossing |
| SGL | ✓ | ✓ | – | Fires once on receipt of `TARM SGL` / `TRIG SGL`, then HOLD |
| SYN | ✓ | ✓ | ✓ | When output buffer empty, reading memory off/empty, controller requests data |
| TIMER[b] | – | – | ✓ | Each reading fires at programmed `TIMER` interval |

[a] LEVEL only valid on DCV or direct-sampling (DCV path with DC coupling).
[b] TIMER and LINE incompatible with synchronous/random ACV and FREQ/PER.

### Useful patterns

#### Continuous reading
Power-on default: `TARM AUTO; TRIG AUTO; NRDGS 1,AUTO` — readings free-run.

#### Single reading on demand
```
PRESET NORM            ; TARM AUTO, TRIG SYN, NRDGS 1,AUTO
ENTER 722;A            ; SYN satisfied by data request → 1 reading returned
```
Or:
```
TARM HOLD              ; suspend
DCV 10; NPLC 1; AZERO OFF
TARM SGL               ; one reading taken, transferred, then HOLD
ENTER 722;A
```

#### N readings per trigger
```
NRDGS 10,AUTO          ; 10 rdgs per trigger event
TARM AUTO; TRIG SGL    ; one trigger → 10 readings
```

#### Multiple trigger arming (groups)
```
TARM HOLD
NRDGS 10,AUTO          ; 10 rdgs/group
TRIG AUTO
TARM SGL,5             ; 5 groups of 10 = 50 readings, then HOLD
```

#### Timed readings (free-running with sample interval)
```
PRESET NORM
NRDGS 8,TIMER ; TIMER 1   ; 8 readings, 1 s apart
TARM AUTO; TRIG SGL
```
or `SWEEP 1,8`. Watch for `TRIGGER TOO FAST` if interval < per-reading time.

#### Delayed first sample
`DELAY <seconds>` inserts time between trigger event and first sample event.
Default delay (`DELAY -1`) is computed automatically from function/range/
ACBAND for settling.

### External triggering and trigger buffering
`TRIG EXT` waits for negative edge on Ext Trig BNC.
With external scanning, an extra trigger arriving during a reading raises
`TRIGGER TOO FAST` (error bit 2). Set `TBUFF ON` to *latch* the next external
edge while a reading is in progress so it is honoured immediately afterward
without error.

### Event combinations (Table 4-2)
Most combinations of TARM/TRIG/NRDGS events are legal; the meaningful
illegal cases are:
- Any combination where TRIG=EXT and NRDGS=SYN (and similar SYN/LEVEL,
  SYN/LINE, SGL/SGL conflicts).
- LEVEL or LINE in NRDGS while TRIG is SYN.

When SYN is used in **multiple** layers, a single data request satisfies all.
When SYN is used only as the sample event, each reading needs its own request.

---

## 7. Asynchronous sampling without holding the GPIB bus busy

This is the most important section for real driver implementations.

### Why a naive driver hangs the bus
- With `INBUF OFF` (default at power-on) the multimeter accepts *one* command
  at a time and **does not release the bus until the command finishes
  executing**. A 2-minute `ACAL ALL` will lock the controller for 2 minutes.
- With `TARM SGL`, `TRIG SGL`, or any SYN event in the chain, the bus is held
  while readings are accumulated.
- A `TEST` (full self-test) command takes ~50 s; an `ACAL ALL` ~16 minutes.

### Mechanisms to release the bus

#### A. Enable the input buffer
```
OUTPUT 722;"INBUF ON"
```
- The 255-byte input buffer accepts the entire command, then GPIB is released
  immediately. The 3458A executes from the buffer in order.
- Throughput cost: minor; bus is freed for other operations.
- This is the recommended baseline for *all* drivers — set it right after
  `PRESET NORM` and leave it on.

#### B. Park readings in reading memory, poll later
```
OUTPUT 722;"PRESET NORM"
OUTPUT 722;"INBUF ON"
OUTPUT 722;"OFORMAT DREAL"      ! or SREAL/SINT/DINT
OUTPUT 722;"MFORMAT SREAL"
OUTPUT 722;"MEM FIFO"
OUTPUT 722;"NRDGS 1000,TIMER"
OUTPUT 722;"TIMER 1E-3"
OUTPUT 722;"TARM HOLD"
OUTPUT 722;"TRIG AUTO"
OUTPUT 722;"TARM SGL"           ! kicks off acquisition, returns immediately
                                ! because INBUF buffered the command
... do other work, optionally poll ...
OUTPUT 722;"MCOUNT?"            ! non-destructive count of readings stored
ENTER 722;N
... wait until N=1000 or use SRQ instead ...
OUTPUT 722;"RMEM 1,1000"        ! recall all
ENTER 722;Rdgs(*)
```

Notes:
- `MEM FIFO` (or `LIFO`) decouples acquisition from transfer — bus is *not*
  held while the burst happens; readings accumulate in 14 kB / 148 kB memory.
- Without reading memory, SYN events would block the acquisition until the
  controller calls for data; with `MEM FIFO`, acquisition proceeds at full
  rate.
- `RMEM` is non-destructive; "implied read" (`ENTER 722;A` while output buffer
  empty) consumes readings from memory.

Memory size capacity by format:

| MFORMAT | Bytes / reading | Std mem (14 kB) | Opt 001 (148 kB) |
|---|---|---|---|
| SINT | 2 | ~7 168 | ~75 776 |
| DINT | 4 | ~3 584 | ~37 888 |
| SREAL | 4 | ~3 584 | ~37 888 |
| DREAL | 8 | ~1 792 | ~18 944 |
| ASCII | 16 | ~896 | ~9 472 |

#### C. Stage the work in a stored subprogram with `PAUSE`
```
SUB MEAS
  TARM HOLD
  DCI 1, .01
  NRDGS 5, AUTO
  TRIG SGL                 ! 5 readings
  PAUSE                    ! releases bus immediately
SUBEND
```
On the host: `OUTPUT 722;"CALL MEAS"`. As soon as PAUSE executes, the bus is
released even with INBUF OFF. To resume execution: `OUTPUT 722;"CONT"` or send
the GPIB Group Execute Trigger (`TRIGGER 722`).

#### D. SRQ-driven completion notification
Status register bits (weights):

| Bit | W | Meaning |
|---|---|---|
| 0 | 1 | Subprogram complete (CALL finished) |
| 1 | 2 | High/Low limit exceeded (PFAIL / PFAIL math) |
| 2 | 4 | SRQ command executed |
| 3 | 8 | Power-on |
| 4 | 16 | Ready for instructions (only when INBUF disabled and a SGL group is done) |
| 5 | 32 | Error (any bit in error register) |
| 6 | 64 | Service requested (asserts GPIB SRQ line) |
| 7 | 128 | Data available in output buffer |

```
OUTPUT 722;"CSB"           ! clear status register
OUTPUT 722;"RQS 1"         ! enable bit 0 (subprogram complete) to assert SRQ
OUTPUT 722;"CALL MEAS"     ! returns when MEAS completes; SRQ goes true
WAIT for SRQ ON INTR ...
SPOLL(722)                 ! reads + clears the status register
```

Use `EMASK` to mask which error conditions can set bit 5 (e.g. ignore
`TRIGGER TOO FAST`).

`STB?` returns the weighted sum; **STB? cannot reveal bit 4** because the
meter is busy parsing the query. Use Serial Poll to read bit 4.

#### E. EXTOUT pulse for hardware-level handshake
Set `EXTOUT BCOMP,NEG` (or `RCOMP`, etc.) to fire a 1 µs TTL pulse on Ext Out
when a burst (or each reading) completes. Allows external hardware to advance
a scanner or wake an MCU without GPIB polling.

### Practical pattern — high-rate burst capture, fully asynchronous
```
CLEAR 722
OUTPUT 722;"PRESET FAST"           ! sets DCV 10, AZERO OFF, DISP OFF, ...
OUTPUT 722;"INBUF ON"
OUTPUT 722;"APER 1.4E-6"           ! 1.4 µs aperture = longest APER that
                                   ! still permits >100 kS/s sustained DCV
                                   ! (matches UG App-D high-speed example)
OUTPUT 722;"NPLC 0"                ! min integration (overrides APER if
                                   ! sent after — keep one or the other)
OUTPUT 722;"MFORMAT DINT"
OUTPUT 722;"OFORMAT DINT"
OUTPUT 722;"MEM FIFO"
OUTPUT 722;"NRDGS 50000,AUTO"      ! one big burst
OUTPUT 722;"CSB"
OUTPUT 722;"RQS 64,POS"            ! actually use bit-0 via SUB or bit-7
OUTPUT 722;"TARM SGL"              ! GO  -- bus released right after SGL is buffered
... host can do anything else ...
WAIT until reading memory full or until SRQ
OUTPUT 722;"ISCALE?"
ENTER 722;Scale
TRANSFER 722 TO buffer; WAIT       ! DMA-friendly, integer transfer
... apply Scale, sign-extend DINT, watch for overload (±2.147483647E9) ...
```

### Don'ts
- Do **not** issue `TARM SGL` with INBUF OFF and SYN as a sample event — the
  meter expects a separate data request for each reading and the bus is held
  in between, which deadlocks unless your driver supplies the requests.
- Do **not** mix autorange with TIMER/SWEEP — autorange is silently disabled.
- Do **not** use SINT/DINT formats with autorange enabled or with real-time
  math (except STAT/PFAIL); the scale factor depends on the configuration at
  recall time, producing bogus values.

---

## 8. Reading formats and high-speed transfer

### Output / memory formats (`OFORMAT`, `MFORMAT`)
| Format | Bytes | Notes |
|---|---|---|
| ASCII | 15 (+1 NUL between in mem) | `SD.DDDDDDDDESDD`, also `cr lf` between if RMEM |
| SINT | 2 | 16-bit two's-complement, **scaled** by ISCALE? |
| DINT | 4 | 32-bit two's-complement, **scaled** by ISCALE? |
| SREAL | 4 | IEEE-754 binary32 (S, 8-bit biased exp E, 23-bit mantissa M, MSB implied 1) |
| DREAL | 8 | IEEE-754 binary64 (S, 11-bit biased exp, 52-bit mantissa, MSB implied 1) |

Decoding:
- **SREAL**: `value = (-1)^S * (1.M) * 2^(E-127)`. Compute mantissa as
  `1 + M/2^23`. Direct cast on most modern CPUs (little-endian needs byte swap;
  3458A sends big-endian / network order).
- **DREAL**: `(-1)^S * (1.M) * 2^(E-1023)`. 8 bytes big-endian.
- **SINT/DINT**: read as signed integer, multiply by `ISCALE?` scalar (also
  big-endian on the wire).

Overload:
- ASCII / SREAL / DREAL: ±1.0E+38
- SINT: ±32767 / ±32768 (unscaled)
- DINT: ±2.147483647E9 / ±2.147483648E9 (unscaled)

### Transfer recommendations
- For speed: match `MFORMAT` to `OFORMAT` and to the A/D's native depth (DINT
  for ≥5.5 digit / >10.8 µs aperture, SINT for ≤10.8 µs aperture).
- Use the fastest binary path the controller supports (DMA / large block
  reads). The 3458A can sustain >100 k rdg/s; the GPIB needs ≥200 kB/s for
  SINT and ≥400 kB/s for DINT.
- Disable display (`DISP OFF`), math (`MATH OFF`, `MMATH OFF`), and autorange
  during high-rate work.

### `END` (EOI)
- `END OFF` (default in `PRESET NORM`): no EOI on transfers.
- `END ON`: EOI on the last byte of the last reading in a burst (1 reading or
  the end of an NRDGS group).
- `END ALWAYS`: EOI on the last byte of *every* reading. In high-speed mode
  this is forced ON during the burst and reverts after.

### `cr lf` rules
- ASCII → each reading followed by `cr lf` to bus, except RMEM groups separate
  by comma and append `cr lf` only at end.
- Binary formats → no `cr lf`.

### `ISCALE?`
Returns the SINT/DINT scale factor for the present configuration (function,
range, A/D setup, math). Always re-read after configuration changes. Reading
ISCALE? while a measurement is queued in the output buffer will *overwrite*
that reading.

### High-speed mode
Entered automatically for DCV/DCI/OHM/OHMF/direct-sampling/sub-sampling when:
- Integration time ≤10 PLC AND
- `ARANGE OFF; DISP OFF; MATH OFF; MFORMAT SINT|DINT; OFORMAT SINT|DINT`.

In this mode the meter is dedicated to acquisition; commands are deferred
until the burst completes. Send Device Clear (`CLEAR 722`) to forcibly exit.

---

## 9. Digitizing

| Method | Max rate | BW | Path | Repetitive needed |
|---|---|---|---|---|
| DCV | 100 kS/s | DC – 150 kHz | DCV signal cond. | No |
| DSAC/DSDC | 50 kS/s | DC – 12 MHz | Track/hold | No |
| SSAC/SSDC | 100 MS/s effective | DC – 12 MHz | Track/hold | **Yes** |

### DCV digitizing (`PRESET DIG` baseline)
Use APER (500 ns – 1 s) for sample aperture; integrates the signal over the
aperture, so amplitude rolls off vs frequency:

| APER | Bits | 3 dB freq |
|---|---|---|
| 2 ns (DSAC/DSDC) | 16 | 100 MHz |
| 500 ns | 15 | 400 kHz |
| 1 µs | 16 | 206 kHz |
| 3 µs | 17 | 69 kHz |
| 6 µs | 18 | 35 kHz |
| 100 µs | 21 | 2 kHz |

### Direct-sampling
`DSAC <max_input>` / `DSDC <max_input>` — no autorange. 2 ns aperture, fixed.
Min interval 20 µs, max ~50 kS/s. Use SINT format if input ≤120 % of range,
DINT if up to 500 % (because track/hold can swing to 5×).

When digitizing ≥1 MHz content, insert a 500 ns delay (`DELAY 500E-9`) before
the first sample to avoid interpolator settling errors.

### Sub-sampling (sequential)
Reconstructs a *repetitive* waveform by spreading samples across many periods.
- Use only `SWEEP` (not NRDGS) and a sync source (`SSRC LEVEL` default, or
  `SSRC EXT` for external sync via Ext Trig BNC).
- Effective interval as low as 10 ns. Real sample rate is still ≤50 kS/s.
- Reading memory must be in `MEM FIFO`, format must be `MFORMAT SINT`, must be
  empty when the sub-sampling burst arms; else `SETTINGS CONFLICT` is raised.
- After capture, `SSPARM?` returns `(N1, N2, N3)`:
  - N1 = count of bursts of size N3
  - N2 = count of bursts of size N3-1
  - N3 = base burst size
- Use these to reorder samples into the composite waveform when transferring
  directly to controller (sending to memory auto-orders).

### Level triggering for digitizing
`TRIG LEVEL` for DCV/DSAC/DSDC. `SSRC LEVEL` for SSAC/SSDC.
`LEVEL <pct>[,<DC|AC>]`:
- DCV: ±120 % of range, 1 % steps.
- Direct/sub-sampling: ±500 % in 5 % steps.
- Hysteresis ≈ 4 % of range.
`SLOPE POS|NEG`. With AC-coupled inputs (DSAC/SSAC) the level coupling is
forced AC regardless of LEVEL coupling parameter.

`LFILTER ON|OFF` — 75 kHz single-pole low-pass on the level-detection path
(prevents false trigger from RF on the input) — also helpful for SYNC ACV and
FREQ/PER measurements with noisy signals.

For DCV digitizing `AZERO OFF` is mandatory when level-triggering.

---

## 10. EXTOUT signal (rear panel BNC)

`EXTOUT <event>[,<polarity>]`. `polarity` = `NEG` (low-going) or `POS`.

| Event | Numeric | Pulse |
|---|---|---|
| OFF | 0 | disabled |
| ICOMP (default) | 1 | 1 µs after A/D integration finishes (or after track/hold acquires for direct/sub-sampling) |
| ONCE | 2 | 1 µs on `EXTOUT ONCE`, then disables |
| APER | 3 | level: high while A/D is integrating |
| BCOMP | 4 | 1 µs after a burst (NRDGS group) completes |
| SRQ | 5 | 1 µs whenever any enabled status event sets bit 6 |
| RCOMP | 6 | 1 µs after every reading |

Use cases:
- **External scanner advance** with `RCOMP` (one rdg per channel) or `BCOMP`
  (multiple rdgs per channel).
- `ICOMP` advances scanner *before* reading completes (good for slow relay
  scanners; pair with `TBUFF ON` to swallow the early channel-closed pulse).
- `APER` lets external switching wait for a quiet integration window.
- `ONCE` from inside a stored subprogram to flag completion.

---

## 11. Math operations

`MATH <op_a>[,<op_b>]` — real-time on each reading.
`MMATH <op_a>[,<op_b>]` — post-process on readings already in memory (MMATH
runs immediately for STAT/PFAIL; for others, it runs as the readings are
recalled from memory).

| Op | What it computes | Registers |
|---|---|---|
| NULL | rdg − OFFSET | OFFSET |
| SCALE | (rdg − OFFSET)/SCALE | OFFSET, SCALE |
| PERC | ((rdg − PERC)/PERC)·100 | PERC |
| DB | 20·log10(rdg/REF) | REF |
| DBM | 10·log10(rdg²/RES/1 mW) | RES |
| FILTER | RC LPF approximation | DEGREE |
| RMS | RMS via filter | DEGREE |
| STAT | min/max/mean/sdev/N | LOWER, UPPER, MEAN, SDEV, NSAMP |
| PFAIL | rdg vs MIN/MAX, sets STB bit 1 | MIN, MAX, PFAILNUM |
| CTHRM*/FTHRM*/CRTD*/FRTD* | Temperature from R | (none) |

`SMATH <reg>,<value>` writes a math register. `RMATH <reg>` reads. Default
register values: `DEGREE=20`, `REF=1`, `SCALE=1`, `RES=50`, `PERC=1`, others 0.

> Caution: enabling MATH (real-time) disables high-speed mode and prevents
> SINT/DINT scale factor from being constant — disable MATH and use MMATH if
> you need raw integer streams.

---

## 12. Status, errors, interrupts

### Status register bits — see §7 D for table.
- Read non-destructively with `STB?` (bit 4 hidden — meter is busy).
- Read & clear with serial poll (`SPOLL 722`) — this also clears bit 6 if
  asserted.
- Clear with `CSB` command (bits 4/5/6 only clear if condition no longer true).
- Enable bits to assert SRQ with `RQS <weighted_sum>`. Power-on default
  effectively `RQS 0` (or 8 if PWR-ON SRQ left set from a previous power cycle).

### Error register (`ERR?` returns + clears, weighted sum)
| Bit | Weight | Meaning |
|---|---|---|
| 0 | 1 | Hardware (read AUXERR? for detail) |
| 1 | 2 | Calibration error |
| 2 | 4 | Trigger too fast |
| 3 | 8 | Syntax |
| 4 | 16 | Command not allowed from remote (e.g. ADDRESS) |
| 5 | 32 | Undefined parameter |
| 6 | 64 | Parameter out of range |
| 7 | 128 | Memory error (memory full) |
| 8 | 256 | Destructive overload detected |
| 9 | 512 | Out of calibration |
| 10 | 1024 | Calibration required (after firmware update or interrupted ACAL) |
| 11 | 2048 | Settings conflict (e.g. sub-sampling memory mismatch) |
| 12 | 4096 | Math error (divide by 0, integer overflow, ...) |
| 13 | 8192 | Subprogram error (calling deleted, CONT/PAUSE misuse) |
| 14 | 16384 | System error |

Mask with `EMASK <weighted_sum>`. A masked bit still sets the bit in the error
register, but does NOT propagate into status-register bit 5 → no SRQ.

`AUXERR?` (auxiliary error register, weighted sum):
| Bit | Weight | Meaning |
|---|---|---|
| 0 | 1 | Slave processor not responding |
| 1 | 2 | DTACK failure |
| 2 | 4 | Slave processor self-test failure |
| 3 | 8 | Isolator test failure |
| 4 | 16 | A/D converter convergence failure |
| 5 | 32 | Calibration value out of range |
| 6 | 64 | GPIB chip failure |
| 7 | 128 | UART failure |
| 8 | 256 | Timer failure |
| 9 | 512 | Internal overload |
| 10 | 1024 | ROM checksum (low) |
| 11 | 2048 | ROM checksum (high) |
| 12 | 4096 | Nonvolatile RAM failure |
| 13 | 8192 | Option RAM failure |
| 14 | 16384 | Cal RAM write/protection failure |

Read both bits and the error string via `ERRSTR?` (returns `<num>,"<msg>"`,
clears one bit per call). If error bit 0 is set, ERRSTR? prefers the AUX
register first.

### Service request example
```
PRESET NORM
CSB                            ! clear status
ON INTR 7 GOTO Handler         ! controller-side
ENABLE INTR 7;2                ! GPIB interrupt enable
RQS 42                         ! enable bits 1, 3, 5 (high/low limit, power-on, error)
MATH PFAIL; SMATH MIN -5; SMATH MAX 5
TRIG AUTO
GOTO 80                         ! idle until SRQ
Handler:
STB? -> A
IF (A AND 2) PRINT "limit"
IF (A AND 8) PRINT "power cycled"
IF (A AND 32) PRINT "error"
```

---

## 13. Subprograms and state memory

The meter has 14 kB (or 148 kB w/ Opt 001) shared between subprograms and
stored states. Each state ≈ 300 bytes. Names: 1–10 chars, alpha or
alphanumeric (or integer 0–127 for front-panel friendliness).

### Subprograms
```
SUB <name>
  ...commands...
SUBEND
```
- Created subprograms are *compiled* on first store → faster than equivalent
  bus traffic.
- Execute with `CALL <name>`.
- `PAUSE` inside a SUB suspends execution and releases the GPIB bus (see §7).
- Resume with `CONT` or with GPIB Group Execute Trigger.
- Up to 10 nesting levels (a SUB can CALL another SUB).
- `CALL 0` runs an autostart subprogram named "0", invoked automatically on
  power-on or `Reset` key. Useful for restoring ACAL or a known config.
- `COMPRESS <name>` discards the ASCII source (saves space) but the SUB then
  vanishes when power is cycled.
- `DELSUB <name>` deletes a SUB; `SCRATCH` deletes ALL subs and states.

### State memory
- `SSTATE <name>` stores function/range/integration/etc. (NOT readings, NOT
  subprograms, NOT some math regs). About 300 B/state.
- `RSTATE <name>` recalls.
- `PURGE <name>` removes a single state.
- States are recalled much faster than re-issuing the equivalent commands
  because the meter stores compiled internal config.

### Memory management
`MSIZE?` → returns `(reading_memory_bytes, largest_unused_subprog_state_block)`.
`MCOUNT?` → number of stored readings.
After heavy SUB churn the SUB/state heap fragments — power cycle or `SCRATCH`
to compact.

---

## 14. Calibration

### `ACAL <type>[,<security_code>]`
Self-cal on internal references; not a substitute for external calibration.

| Type | Time | Effect |
|---|---|---|
| ALL | 16 min | DCV + AC + OHMS |
| DCV | 2 min 45 s | All functions (DCV affects everything) |
| AC | 2 min 45 s | ACV/ACDCV (any method), ACI/ACDCI, DSAC/DSDC, SSAC/SSDC, FREQ, PER |
| OHMS | 11 min | OHM/OHMF, DCI, ACI |

After ACAL let the relays thermally settle (15–30 min) before high-precision
work. Recommend ACAL ALL once / 24 h or after ±1 °C drift.

### Other cal commands
- `CAL` — issues the external calibration sequence (refer to Calibration
  Manual; needs reference sources). Don't use without procedure.
- `CALNUM?` — increments on every calibration; used to track number of
  calibrations performed.
- `CALSTR <"text">[, <sec_code>]` — store up to 75 chars of cal metadata in
  nonvolatile RAM. `CALSTR?` reads it.
- `SECURE <old_code>,<new_code>[,ACAL_SECURITY 0|1]` — secures cal RAM.
- `TEMP?` — returns instrument internal temperature in °C.
- `LINE?` — measures actual mains frequency.
- `TEST` — full self-test (~50 s); errors set bit 0 + AUXERR.

`DEFEAT ON` disables input protection switching and some syntax checking — do
NOT use unless you accept ≤±100 V on the 10 V range; intended for last few %
of speed during well-characterised production tests.

---

## 15. Display and front-panel control

- `DISP OFF` — blanks display, speeds reading rate. `DISP MSG,'text'` shows a
  message. `DISP CLR` clears, `DISP ON` resumes.
- `BEEP ON|OFF|ONCE`. `TONE <freq>,<duration>` for arbitrary beeps.
- `LOCK ON` — disables front panel except `Local`. With LOCK ON, even `Local`
  is disabled (must release from controller or cycle power).
- `MENU SHORT|FULL` — selects alphabetic menu list.
- `DEFKEY <0–9>,'<command;...>'` — assign a command sequence to f0–f9.
  `DEFKEY DEFAULT` clears all.

---

## 16. Connections cheat-sheet

| Measurement | Wires |
|---|---|
| DCV / ACV / ACDCV / FREQ-V / digitizing | HI, LO (Guard to LO if shielded) |
| DCI / ACI / ACDCI / FREQ-I | I, LO |
| 2-wire OHM | HI, LO |
| 4-wire OHMF | HI, LO (current); Ω Sense HI, Ω Sense LO (sense) |
| Ratio | Signal: HI, LO. Reference: Ω Sense HI, Ω Sense LO. Common LO. |

> 4-wire ohms: leave `AZERO ON` unless you have manually zeroed the lead R
> and the leads are stable.

> AC ranges 10 mV / 100 mV are sensitive to radiated EMI — use shielded leads.

---

## 17. Commonly-used recipes

### A. Robust 6½-digit DCV reading on demand
```
CLEAR 722
OUTPUT 722;"PRESET NORM"
OUTPUT 722;"INBUF ON"
OUTPUT 722;"DCV 10"            ! force 10 V range, autorange off
OUTPUT 722;"NPLC 10"           ! 200 ms @ 50 Hz / 167 ms @ 60 Hz
OUTPUT 722;"AZERO ON"
OUTPUT 722;"OFORMAT ASCII"
OUTPUT 722;"TARM AUTO; TRIG SYN; NRDGS 1,AUTO"
ENTER 722;V
```

### B. Buffered burst at maximum speed (DCV)
```
OUTPUT 722;"PRESET FAST"        ! DCV 10, AZERO OFF, DISP OFF, DINT, TARM SYN, TRIG AUTO
OUTPUT 722;"INBUF ON"
OUTPUT 722;"APER 1.4E-6"        ! 100 kS/s
OUTPUT 722;"MFORMAT SINT"       ! 2 bytes/rdg
OUTPUT 722;"MEM FIFO"
OUTPUT 722;"NRDGS 30000,AUTO"
OUTPUT 722;"TARM SGL"
... wait for completion (SRQ on bit 4 with INBUF OFF, or poll MCOUNT?) ...
OUTPUT 722;"OFORMAT SINT"
OUTPUT 722;"ISCALE?"
ENTER 722;Scale
TRANSFER 722 TO Buf;WAIT
```

### C. 4-wire OHMF temperature with PT100 (RTD)
```
OUTPUT 722;"PRESET NORM"
OUTPUT 722;"OHMF 1E3"            ! 1 kΩ range
OUTPUT 722;"OCOMP ON"
OUTPUT 722;"NPLC 100"
OUTPUT 722;"MATH CRTD92"         ! Celsius, alpha 0.003916
OUTPUT 722;"TRIG SGL"
ENTER 722;Temp_C
```

### D. AC voltage with synchronous sampling, 0.01 % resolution
```
PRESET NORM
SETACV SYNC
ACV 10, .01                       ! 10 V range, 0.01 % res
ACBAND 50, 1E5                    ! 50 Hz – 100 kHz signal
TRIG SGL
ENTER 722;Vrms
```

### E. Externally-triggered scanner sweep (RCOMP handshake)
```
PRESET NORM
DCV; NRDGS 1,AUTO
TARM AUTO; TRIG EXT
EXTOUT RCOMP, NEG
MEM FIFO
... configure scanner: each channel close pulses Ext Trig ...
... after 6 channels, RMEM 1,6 ...
```

### F. Sub-sampled 1 MHz waveform reconstruction
```
PRESET FAST
INBUF ON
SSDC 10                            ! sub-sampling, DC-coupled, 10 V
TARM HOLD
MFORMAT SINT; OFORMAT SINT
MEM FIFO
SWEEP 5E-9, 4000                   ! 5 ns effective interval, 4000 samples
LEVEL 75, DC; SLOPE POS            ! sync at +7.5 V
SSRC LEVEL
TARM SGL                           ! GO
... wait, then read back ordered samples with RMEM ...
SSPARM?                            ! confirm bursts (N1,N2,N3) for verification
```

### G. Pass/fail go-no-go with SRQ
```
PRESET NORM
INBUF ON
DCV 10; NPLC 1
MATH PFAIL; SMATH MIN 4.9; SMATH MAX 5.1
EMASK 0                            ! ignore plain errors
RQS 2                              ! bit 1 (limit exceeded) → SRQ
CSB
TRIG AUTO
... host sleeps until SRQ; STB? returns 64+2 ...
RMATH PFAILNUM                     ! how many passed before failure
```

---

## 18. Quick command reference

Lower-case `<x>` = parameter; `[…]` = optional. Numeric query equivalents are
shown where helpful.

### A/D and integration
- `APER <s>` — aperture 500 ns–1 s, 100 ns step.
- `NPLC <plc>` — 0–1000 PLC.
- `LFREQ <Hz>|LINE` — A/D reference freq.
- `LINE?` — measure mains.
- `RES <%>` — resolution percentage.

### Function and ranging
- `FUNC <fn>[,<max_in>[,<%res>]]` (or function header alone).
- `DCV/ACV/ACDCV/OHM/OHMF/DCI/ACI/ACDCI/FREQ/PER/DSAC/DSDC/SSAC/SSDC` —
  shortcut headers, same params.
- `RANGE <max_in>[,<%res>]` / `R <…>` — range only.
- `ARANGE OFF|ON|ONCE` — autorange.
- `FIXEDZ ON|OFF` — DCV fixed input Z.
- `OCOMP ON|OFF` — offset-compensated ohms.
- `AZERO ON|OFF|ONCE` — autozero.
- `SETACV ANA|SYNC|RNDM` — AC method.
- `ACBAND <flo>,<fhi>` — AC bandwidth.
- `FSOURCE ACV|ACDCV|ACI|ACDCI` — freq/period source.
- `RATIO ON|OFF` — ratio mode.

### Trigger
- `TARM <evt>[,<count>]` — `AUTO|EXT|HOLD|SGL|SYN`.
- `TRIG <evt>` — `AUTO|EXT|HOLD|LEVEL|LINE|SGL|SYN`.
- `NRDGS <n>[,<evt>]` — `AUTO|EXTSYN|SYN|TIMER|LEVEL|LINE`.
- `SWEEP <interval>,<count>` — convenience for NRDGS+TIMER.
- `TIMER <s>` — interval for TIMER event.
- `DELAY <s>` — pre-first-sample delay (`-1` = auto).
- `SLOPE POS|NEG` — for LEVEL trig.
- `LEVEL <%>[,DC|AC]` — level trig threshold.
- `LFILTER ON|OFF` — 75 kHz LPF on level path.
- `SSRC LEVEL|EXT[,AUTO]` — sync source for SSAC/SSDC and SYNC ACV.
- `TBUFF ON|OFF` — buffer one external trigger during a reading.

### Acquisition / memory / formats
- `MEM OFF|LIFO|FIFO|CONT` — reading memory mode.
- `MFORMAT ASCII|SINT|DINT|SREAL|DREAL` — memory storage format.
- `OFORMAT ASCII|SINT|DINT|SREAL|DREAL` — bus output format.
- `MSIZE?` — memory sizes.
- `MCOUNT?` — number of stored readings.
- `RMEM [first[,count[,record]]]` — recall readings.
- `ISCALE?` — SINT/DINT scale factor.
- `END OFF|ON|ALWAYS` — EOI behaviour.
- `INBUF ON|OFF` — input buffer.
- `DEFEAT ON|OFF` — disable protection (use with care!).

### Math
- `MATH <op_a>[,<op_b>]` / `MATH OFF` / `MATH CONT[,CONT]`.
- `MMATH <op_a>[,<op_b>]` / `MMATH OFF` — post-process.
- `RMATH <reg>` / `SMATH <reg>,<value>` — math registers.

### Subprograms / states
- `SUB <name>` … `SUBEND`, `CALL <name>`, `PAUSE`, `CONT`.
- `COMPRESS <name>`, `DELSUB <name>`, `SCRATCH`.
- `SSTATE <name>`, `RSTATE <name>`, `PURGE <name>`.

### Status / errors
- `STB?`, `CSB`, `RQS <bits>`, `SRQ`, `SPOLL`.
- `ERR?`, `ERRSTR?`, `EMASK <bits>`, `AUXERR?`, `TEST`.

### EXTOUT and identification
- `EXTOUT <event>[,POS|NEG]`, `EXTOUT ONCE`.
- `ID?`, `OPT?`, `REV?`, `ADDRESS <0..31>`.
- `BEEP <ON|OFF|ONCE>`, `TONE <freq>,<sec>`.
- `DISP <ON|OFF|MSG|CLR>[,'<msg>']`.
- `LOCK ON|OFF`, `DEFKEY <0-9>,'<cmds>'|DEFAULT`, `MENU SHORT|FULL`.

### Cal
- `ACAL <ALL|DCV|AC|OHMS>[,<sec>]`, `CAL`, `CAL?`, `CALNUM?`, `CALSTR <"text">`,
  `SECURE`, `SCAL`, `TEMP?`.

---

## 19. Specifications you will care about while writing a driver

- **Settling**: default DELAY auto-derives from function/range/ACBAND. If you
  override DELAY, expect 1–20 ms of pre-roll for AC; for OHM ≥1 MΩ default
  delays grow into seconds (parasitic capacitance + RC charge).
- **NMR**: ≥60 dB at NPLC ≥ 1 (LFREQ-locked); ≥80 dB at NPLC ≥ 1000.
- **Trigger jitter**:
  - DCV digitizing: ~100 ns, level-trigger jitter higher (signal-dependent).
  - Direct-/sub-sampling: ~50 ps RMS timebase jitter.
- **Min external trigger pulse width**: 250 ns; max trig BW: 5 MHz.
- **GPIB capabilities**: full SH1, AH1, T6/TE0, L4/LE0, SR1, RL2, PP1, DC1,
  DT1, C0, E2 — the 3458A is *not* a controller, but supports all standard
  talker/listener / SRQ / device-clear functions.
- **Maximum cable length**: 20 m total bus, 2 m per device.
- **Memory layout**: 14 kB (or 148 kB) shared by SUB/state/readings; reading
  memory size = total - subprog/state used.

---

## 20. Driver-implementation notes (C / C++ / Python)

### Recommended layering
1. **Transport layer** — GPIB driver (NI-VISA, linux-gpib, Prologix). Handle
   write / read / serial-poll / device-clear / SRQ-wait.
2. **Session layer** — simple `dmm_send(cmd)` + `dmm_query(cmd, buf, len)`
   wrappers. Always send `\n`-terminated; many adapters auto-EOI on last byte.
3. **Configuration helpers** — apply `PRESET NORM; INBUF ON; END ALWAYS`,
   then function-specific helpers (`dcv_setup(range, nplc)`, etc.).
4. **Acquisition orchestration** — encapsulate a measurement as
   `start()` / `wait_done()` / `read_buffer()`:
   - `start()` issues `TARM SGL` (or `TRIG SGL`) and returns immediately
     thanks to INBUF ON.
   - `wait_done()` either:
     - Polls `MCOUNT?` until equal to `NRDGS×arming_count`; or
     - Waits on SRQ. Recommended pattern: enable bit 0 via a wrapper SUB:
       ```
       SUB ACQ
         ... configure & TARM SGL ...
       SUBEND
       RQS 1
       CALL ACQ
       ```
       SRQ fires when ACQ completes. Serial-poll, check bit 6 + bit 0.
   - `read_buffer()` issues `TRANSFER` / large block read and applies ISCALE
     (for SINT/DINT) and IEEE-754 byte-swap (for SREAL/DREAL).

### Endianness
- Wire is **big-endian** (network order). Byte-swap into host order before
  treating as IEEE-754. Embedded targets without `<endian.h>`:
  ```c
  static inline uint32_t be32(const uint8_t *p) {
      return ((uint32_t)p[0]<<24)|((uint32_t)p[1]<<16)|((uint32_t)p[2]<<8)|p[3];
  }
  static inline float be_f32(const uint8_t *p){
      uint32_t u = be32(p); float f; memcpy(&f,&u,4); return f;
  }
  ```
- SINT: int16_t (sign-extended naturally), DINT: int32_t. After the cast,
  multiply by the cached `ISCALE?` value (a double).

### Overload sentinels
Always test before scaling:

```c
if (s == INT16_MAX || s == INT16_MIN) value = NAN;       // SINT
if (s == 0x7FFFFFFF || s == (int32_t)0x80000000) value = NAN; // DINT
if (fabs(v) >= 9.99e37) value = NAN;                     // ASCII / SREAL / DREAL
```

### SREAL / DREAL pitfalls
- `1E+38` is the overload code (positive saturation), `-1E+38` for negative.
- The SREAL "implied 1" mantissa decode is identical to standard IEEE-754, so
  `memcpy` after byte-swap works on every modern host.

### Robust startup
```c
gpib_clear(addr);                          // Device Clear
dmm_send("RESET");                          // sane state, no PWR-ON SRQ side-effect
                                            // (or "PRESET NORM" for remote)
dmm_send("INBUF ON");                       // do this BEFORE long ops like ACAL/TEST
dmm_send("END ALWAYS");                     // makes block reads simpler
dmm_send("BEEP OFF");                       // optional, depending on UX
dmm_send("DISP OFF");                       // for high-speed; comment out for debugging
dmm_send("OFORMAT DREAL");                  // 8 bytes/rdg, no scale, no autorange care
read_id();                                  // ID? sanity check
read_revs();                                // REV?
read_temp();                                // TEMP? for log
read_cal_string();                          // CALSTR? for record
check_errors();                             // drain ERRSTR? until 0,"NO ERROR"
```

### ACAL handling
ACAL ALL ~16 min. Always:
- Disconnect the input.
- `INBUF ON; ACAL ALL` → `ACAL` returns immediately to the bus, but the meter
  is busy ~16 min.
- Poll the *Ready for instructions* status bit (bit 4) by serial-poll.
- Or wrap inside a SUB and use `CALL` + RQS-1.
- After ACAL, wait the recommended thermal settle time (15–30 min) before
  precision work; expose this as a "ready" timer to the user.

### Error policy
- After every group of commands (or at end of acquisition) call `ERRSTR?` in a
  loop until it returns `0,"NO ERROR"`. Cache & report.
- `EMASK 4` to keep `TRIGGER TOO FAST` from spamming SRQ during high-rate
  bursts where occasional fast triggers are tolerable.

### High-speed notes
- Don't issue *any* command (other than DCL) while a high-speed burst is in
  progress; the meter ignores them.
- The output buffer holds exactly one reading in high-speed mode; the meter
  blocks each new reading until the previous one is read. So with INBUF ON +
  high-speed, the bus is shared with reading transfers — for absolute
  throughput, prefer `MEM FIFO` and DMA-style block reads after the burst.

### Sub-sampling correctness
After `SSPARM?` returns `(N1, N2, N3)`:

```text
total samples = N1 * N3 + N2 * (N3 - 1)
Inc = N1 + N2          ; total bursts
For i = 0..N1-1:
    For j = 0..N3-1:
        wave[ i + j*Inc ] = samp[ i*N3 + j ]
For i = N1..N1+N2-1:
    For j = 0..N3-2:
        wave[ i + j*Inc ] = samp[ N1*N3 + (i-N1)*(N3-1) + j ]
```
(Equivalent to the BASIC code in the User's Guide §5; integer arithmetic
only.)

When sending sub-sampled data directly to memory (`MEM FIFO; MFORMAT SINT`)
the meter performs this reordering internally — readings come out already in
correct waveform order. Sending direct-to-controller via TRANSFER skips
reordering, so you must apply it.

### Python skeleton (pyvisa)
```python
import pyvisa, struct, time
rm = pyvisa.ResourceManager()
dmm = rm.open_resource("GPIB0::22::INSTR", read_termination='\n', write_termination='\n')
dmm.timeout = 30000

def send(cmd): dmm.write(cmd)
def query(cmd): return dmm.query(cmd).strip()

def init():
    dmm.clear()
    send("RESET")
    send("INBUF ON")
    send("END ALWAYS")
    send("OFORMAT DREAL")
    assert query("ID?").endswith("3458A")

def burst_dcv(n=10000, aper=1.4e-6, vrange=10.0):
    send("PRESET FAST")
    send("INBUF ON")
    send(f"DCV {vrange}")
    send(f"APER {aper}")
    send("MFORMAT DREAL")
    send("OFORMAT DREAL")
    send("MEM FIFO")
    send(f"NRDGS {n},AUTO")
    send("CSB")
    dmm.write("TARM SGL")
    # wait until N readings accumulated
    while int(query("MCOUNT?")) < n:
        time.sleep(0.05)
    # pull all
    raw = dmm.read_bytes(n * 8)
    # 3458A DREAL is big-endian IEEE-754
    return list(struct.unpack(f">{n}d", raw))
```

### C skeleton (NI-488.2)
```c
#include <ni4882.h>     // or ibLinux equivalent
#include <stdint.h>
#include <string.h>

#define DEV 22

static int hdl;

static int snd(const char *cmd){ ibwrt(hdl, (void*)cmd, strlen(cmd)); return iberr; }
static int rcv(char *buf, size_t n){ ibrd(hdl, buf, n); return ibcnt; }

void dmm_init(void){
    hdl = ibdev(0, DEV, 0, T10s, 1, 0);     // EOI, EOT
    ibclr(hdl);                              // Device Clear
    snd("RESET\n");
    snd("INBUF ON\n");
    snd("END ALWAYS\n");
    snd("OFORMAT DREAL\n");
}

double dmm_dcv(double range, double nplc){
    char tmp[64];
    snprintf(tmp, sizeof tmp, "DCV %g\n", range); snd(tmp);
    snprintf(tmp, sizeof tmp, "NPLC %g\n", nplc); snd(tmp);
    snd("TRIG SGL\n");
    uint8_t buf[8];
    rcv((char*)buf, 8);
    uint64_t u = ((uint64_t)buf[0]<<56)|((uint64_t)buf[1]<<48)|((uint64_t)buf[2]<<40)|
                 ((uint64_t)buf[3]<<32)|((uint64_t)buf[4]<<24)|((uint64_t)buf[5]<<16)|
                 ((uint64_t)buf[6]<<8 )|((uint64_t)buf[7]    );
    double d; memcpy(&d, &u, 8);
    return d;
}
```

### Async pattern in C with SRQ
```c
// Configure a 30 000-sample DCV burst, return immediately, block on SRQ.
void dmm_burst_async(int n){
    snd("PRESET FAST\n");
    snd("INBUF ON\n");
    snd("APER 1.4E-6\n");
    snd("MFORMAT SINT\n");
    snd("OFORMAT SINT\n");
    snd("MEM FIFO\n");
    char tmp[40];
    snprintf(tmp, sizeof tmp, "NRDGS %d,AUTO\n", n);  snd(tmp);
    snd("CSB\n");                          // clear status
    // Use a wrapper SUB so bit 0 fires on completion
    snd("SUB BURST\n");
    snd(" TARM SGL\n");
    snd("SUBEND\n");
    snd("RQS 1\n");                        // bit 0 -> SRQ
    snd("CALL BURST\n");                   // returns immediately (INBUF ON)
}

void dmm_burst_wait_and_read(int n, int16_t *out){
    ibwait(hdl, RQS);                      // block until SRQ
    char stb;
    ibrsp(hdl, &stb);                      // serial poll, clears SRQ
    snd("ISCALE?\n");
    char sbuf[32]; rcv(sbuf, sizeof sbuf);
    double scale = strtod(sbuf, NULL);
    snd("OFORMAT SINT\n");                 // make sure
    // pull memory
    snprintf(...,"RMEM 1,%d\n", n); snd(...);
    rcv((char*)out, 2 * n);                // big-endian int16
    for(int i=0;i<n;i++){
        int16_t s = (int16_t)((out[i] >> 8 & 0xFF) | (out[i] << 8 & 0xFF00));
        out[i] = s; // stash unscaled
        // user multiplies by `scale` to get volts (double)
    }
}
```

---

## 21. Troubleshooting playbook

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERR` annunciator on, ERRSTR? returns `200,"HARDWARE FAILURE—..."` | Hardware fault | Run `TEST`; if persistent, AUXERR? for detail; meter needs service. |
| `1024,"CALIBRATION REQUIRED"` (bit 10 in ERR) | Power cycled or aborted during ACAL; cal constants invalid | `ACAL ALL`; do not interrupt. |
| `4,"TRIGGER TOO FAST"` | TIMER interval too short for measurement, or external triggers arriving during a reading | Slow TIMER; or `TBUFF ON`; or check NPLC. |
| `2048,"SETTINGS CONFLICT"` during sub-sampling arm | MFORMAT not SINT, or memory not empty, or MEM not FIFO | `MFORMAT SINT; MEM FIFO` then re-arm. |
| Bus hung after `OUTPUT ...;"TEST"` | INBUF OFF; controller waiting for LF | Use `INBUF ON` or send `CLEAR <addr>`; do not abort with reset. |
| 4-wire ohms reading drifts | AZERO disabled with changing lead R | `AZERO ON`. |
| Random AC reading varies > spec | Wrong ACBAND; ANA method on low-frequency signal | `ACBAND <flo>,<fhi>` matched to signal; for <10 Hz prefer SYNC. |
| Front panel locked; cannot regain control | LOCK ON from remote; or `LOCAL LOCKOUT` asserted | `LOCK OFF` from controller; or send GPIB `LOCAL`/`GTL`; or cycle power. |
| Display shows `OVLD` but burst values look fine | Range too low; SINT format saturates at ±32 767 | Increase range; check ISCALE; consider DINT if track/hold path. |
| All readings = `±1E+38` (or `±32767` SINT) | Persistent overload | Check connections, attenuation; range; FIXEDZ for low Z paths. |
| Cannot change address remotely | `ADDRESS` is front-panel only | Use front-panel `Address` (shifted Local) key. |
| ID? returns garbage characters | Bus race: a previous query response sat in output buffer | `CLEAR 722` then re-issue. |
| `Memory Error` (bit 7) when storing SUB/state | Sub/state heap exhausted or fragmented | `SCRATCH` or power cycle to compact; use `COMPRESS` to reclaim. |
| Sub-sampling gives wrong waveform period | LEVEL not synchronized to signal, or SLOPE wrong | Check `SSRC LEVEL` vs `SSRC EXT`; verify LEVEL voltage and SLOPE. |
| `NMR` worse than spec | LFREQ not locked to actual mains | `LFREQ LINE` to measure & lock. |
| ACAL refuses with `Out of Range` (AUXERR bit 5) | Input signal connected during ACAL | Disconnect inputs and re-run. |

---

## 22. Glossary

- **ACAL** — Auto-calibration: internal self-cal of gain/offset.
- **APER (Aperture)** — Time the A/D actually integrates the signal per sample.
- **AZERO** — Autozero: interleaves a zero reading to cancel internal offsets.
- **EOI** — End Or Identify: GPIB last-byte indicator.
- **GPIB** — General Purpose Interface Bus (IEEE-488).
- **HPML** — HP Multimeter Language (the 3458A's command set; SCPI is *not*
  used).
- **NMR** — Normal-Mode Rejection: rejection of mains noise via integration.
- **NPLC** — Number of Power Line Cycles (integration time).
- **OFORMAT / MFORMAT** — Bus output / reading-memory storage format.
- **PRESET NORM/FAST/DIG** — Predefined remote/fast/digitizing setups.
- **RMEM / Implied read** — Recall readings by index / by data-request.
- **SRQ** — Service Request: GPIB asynchronous attention line.
- **SYN event** — Synchronous-with-controller; "satisfied" when controller
  requests data and output buffer is empty.
- **TARM / TRIG / NRDGS** — Trigger arm / trigger / sample-event commands.
- **Track-and-hold path** — 12 MHz BW, 2 ns aperture front-end used for
  direct- and sub-sampling.

---

## 23. Document conventions used by the User's Guide examples

The original User's Guide uses HP/Keysight Series 200/300 BASIC syntax:

```
OUTPUT 722;"<HPML command>"   ! send command(s)
ENTER 722;<vars>              ! read response/readings
TRANSFER 722 TO @buf;WAIT     ! DMA-style fast transfer (typed I/O paths)
```

`722` = select code 7 + address 22. Substitute your transport's equivalent
write/read calls. Termination: BASIC `OUTPUT` appends `cr lf`; suppress with
`OUTPUT 722 USING "#,K";"…"` if you need to keep the bus released without
waiting for the meter to consume a `lf` (rare — `INBUF ON` is the better
fix).

---

## 24. Appendices: Min/max parameter cheat-sheet

| Command | Min | Max |
|---|---|---|
| APER | 0 (any value <500 ns floors at 500 ns) | 1 s (100 ns steps) |
| NPLC | 0 | 1000 |
| DELAY | 100 ns (other) / 10 ns (digitizing) | 6000 s |
| SWEEP / TIMER interval | 100 ns | (limited by per-rdg time; else TRIG TOO FAST) |
| NRDGS count | 1 | 16 777 215 |
| TARM arming count | 1 | (essentially unlimited) |
| Sub-sampling effective interval | 10 ns | 50 µs (≥20 µs becomes direct-sampling) |
| INBUF buffer | up to 255 chars | (longer commands stall until space frees) |
| Reading memory | 14 kB / 148 kB | (shared with subprograms/states) |
| Subprogram name length | 1 | 10 chars (alpha+_+? + digits) |
| Calibration string | 1 | 75 chars |
| User-defined keys (DEFKEY) | f0 | f9 (max 40 chars per key) |
| GPIB address | 0 | 30 (31 = Talk Only) |

---

End of document.

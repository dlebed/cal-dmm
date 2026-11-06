# Verification and calibration

How to use `caldmm-cal-verify`, `caldmm-cal-analyze` and
`caldmm-5700a-selfcal`.

Every transcript here is real output. The instruments are simulated (the test
suite's fake source and meter over the real plan generator, the real execution
engine and the real result writer), so the numbers are synthetic — but the
screens, the counts, the verdicts and the arithmetic are the ones you will get.

---

## Read this first: which direction?

Two instruments, one comparison, and **two different questions**:

```bash
# Question A: is the meter right?  The calibrator is the reference.
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter

# Question B: is the calibrator right?  The meter is the reference.
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut source
```

**For a 3458A and a 5700A, question B is usually the honest one.** A 3458A's
1-year dc-volts specification is *tighter* than a 5700A's. Verify the meter
against the calibrator and over most of the range you are not measuring the
meter at all — you are measuring the calibrator's ability to source.

The numbers, from the shipped pairing:

| plan | points | with a spec ratio ≤ 1 |
|---|---:|---:|
| `--dut meter` (3458A as DUT) | 204 | **117 (57 %)** |
| `--dut source` (5700A as DUT) | 175 | 95 (54 %) |

The **spec ratio** is `DUT tolerance / reference tolerance`. Below 1 the
reference's own specification is *looser* than the thing it is judging. Every
generated point carries it in its note, so you see it before the run:

```
note: spec ratio 0.375 (hp3458a 3e-07 / fluke5700a 8e-07 V)
```

and the runner prints a census before it touches an instrument. Meter-as-DUT:

```console
$ caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 --dut meter -o full.csv --dry-run

--- Plan ---------------------------------------------------------------------
  hp3458a verified against fluke5700a
  DUT       hp3458a (the meter)
  reference fluke5700a
  rule      interval 1y, 95 % confidence, TUR floor 4:1, guard band g=1, temperature policy gate
  sampling  10 samples per point, 1 discarded first

  !! THE REFERENCE IS NO BETTER THAN THE DUT AT 130 OF 257 POINTS (51 %)
     Their spec ratio (DUT tolerance / reference tolerance) is at or below 1, so at those points the comparison mostly measures
     fluke5700a rather than hp3458a.  Worst 0.004 at DCI 0 A [1e-07 range] 2w.
     By quantity: ACI 13, ACV 32, DCI 65, DCV 8, RES 12
     They are 1 h 00 min to 1 h 39 min of this run.  Skip them with --min-spec-ratio 1.

  257 points in 2 wiring groups:
    group 1/2  voltage/ohms terminals: 145 points from #0
        DCV 2w     45 points  0.000V to 1.000kV
        ACV 2w     80 points  5.000mV to 700.000V
        RES 4w     17 points  0.000OHM to 19.000MOHM
        RES 2w      3 points  0.000OHM to 100.000MOHM
    group 2/2  current terminals: 112 points from #145
        DCI 2w     72 points  0.000A to 1.000A
        ACI 2w     40 points  10.000μA to 1.000A
  estimated 1 h 59 min to 3 h 17 min of instrument time, plus 2 wiring prompts (the first is the initial connection)
```

Calibrator-as-DUT, the same two instruments:

```console
$ caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 --dut source -o full.csv --dry-run

--- Plan ---------------------------------------------------------------------
  fluke5700a verified against hp3458a
  DUT       fluke5700a (the source)
  reference hp3458a
  rule      interval 1y, 95 % confidence, TUR floor 4:1, guard band g=1, temperature policy gate
  sampling  10 samples per point, 1 discarded first

  !! THE REFERENCE IS NO BETTER THAN THE DUT AT 131 OF 211 POINTS (62 %)
     Their spec ratio (DUT tolerance / reference tolerance) is at or below 1, so at those points the comparison mostly measures
     hp3458a rather than fluke5700a.  Worst 0.0768 at ACI 0.11 A @ 1000 Hz [AC220MA] 2w.
     By quantity: ACI 36, ACV 42, DCI 8, DCV 38, RES 7
     They are 1 h 01 min to 1 h 40 min of this run.  Skip them with --min-spec-ratio 1.

  211 points in 2 wiring groups:
    group 1/2  voltage/ohms terminals: 134 points from #0
        DCV 2w     53 points  0.000V to 1.000kV
        ACV 2w     63 points  1.980mV to 700.000V
        RES 4w     17 points  0.000OHM to 19.000MOHM
        RES 2w      1 points  100.000MOHM to 100.000MOHM
    group 2/2  current terminals: 77 points from #134
        DCI 2w     41 points  0.000A to 1.000A
        ACI 2w     36 points  22.000μA to 1.000A
```

The worst meter-DUT ratio is **0.004**, on the 3458A's 100 nA dc-current range —
`spec ratio 0.004 (hp3458a 4e-11 / fluke5700a 1e-08 A)`. That
comparison is arithmetically incapable of producing a verdict about the meter,
and the TUR floor will correctly mark it `INDETERMINATE`. It is honest, and it
also wastes bench time.

### What to actually do

1. **Default to `--dut source`.** A golden 3458A verifying the calibrator is
   the direction where the reference is genuinely better over most of the
   range.
2. **Use `--dut meter` only where the calibrator's spec is genuinely tighter** —
   ac volts at some frequencies, high-voltage dc, high resistance. Add
   `--min-spec-ratio 1` and the runner drops the rest for you:

   ```bash
   caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 \
       --dut meter --min-spec-ratio 1 -o dmm.csv
   ```

   On the 257-point plan that prints `dropped 130 points, 127 remain`. Points
   with no stated ratio are **kept** — "no ratio" is not "a bad ratio".
3. **Read the per-point note in the report.** A verdict on a point with a ratio
   below 1 is a statement about the *pairing*, not about the instrument.

The census is printed whether or not you filter, so the decision is always in
front of you.

> `--min-spec-ratio` uses **at or below**, matching the census.

### What the generated plan does that a typed list would not

Six things worth knowing before you read a plan, all of them visible in the
point's own `note:` or in the `not planned` list:

* **The calibrator is put on the range the plan credits.** A 5700A autoranges
  to "the lowest possible range" ([OM 1996] p.4-24), so 110 V asked for on the
  1100 V range comes out of the 220 V range unless the range is locked. Every
  `--dut source` point that needs it says `sourced with the range lock on
  DC1100V …`, and the runner anchors the range, sends `RANGELCK ON`, sources
  the setpoint and re-reads `RANGE?` before it energizes anything — exactly the
  order Fluke's own verification uses ([SM 2007] §3-16). A point whose range
  cannot be selected at all is **not planned**, with that reason. If the
  instrument ever disagrees at run time the row carries
  `source-range-mismatch`, and the limit follows the *readback*, not the plan.
* **The top of a range is not silently lost.** Where a percentage of full scale
  is beyond what the pair can manage, the largest value both instruments do
  reach is planned beside it: 700 V at 1 kHz on a 3458A 1000 V ac range,
  250 V at 30 Hz on the 5700A's own 1100 V ac range. The ladder point that could not be
  made is still listed in `not planned` with the reason, and the substitute
  says what it stands in for.
* **Resistance points say what current they were read at.** The 5700A's
  resistance uncertainty holds only inside its Full Spec Load Range
  ([OM 1996] p.49); a 3458A reads 19 Ω on its 100 Ω range at 1 mA against a
  minimum of 8 mA, which the manual derates by `K(IL − I)/(IL × I)` = 14 ppm on
  a 31 ppm figure. The plan prefers a meter range inside the window where one
  exists and otherwise carries the adder and the arithmetic in the note. The
  adder is then **applied**, and the row says so with `load-current-derated`:
  with the 5700A as the reference it is an extra term on its side of the
  uncertainty budget, at the same confidence as the figure it is added to;
  with the 5700A under test it widens the 5700A's own tolerance, because the
  manual's accuracy statement at that load is the stated figure plus the
  derating.
* **The 100 MΩ standard is a two-wire group of one** — see *Run sizing*.
* **`--frequencies manufacturer`** replaces the default centre-of-band tones
  with the specification break points, which is where the manufacturers put
  their own verification points (3458A Cal Manual Table 4-3: 20 Hz, 1 kHz,
  20 kHz, 100 kHz, 1 MHz). The default is unchanged; this is opt-in, and it is
  the slower, more searching run.

---

## Naming and addressing the instruments

Both `--source` and `--meter` take `MODEL[:NAME]:ADDRESS`. The name is yours
and appears in the report and the CSV; it is a unit designator, never a role
or a channel. The address depends on how the instrument is connected:

| spelling | means |
|---|---|
| `4` | GPIB primary address 4 on board 0 |
| `gpib1,4` or `1,4` | primary address 4 on the **second** USB-GPIB adapter |
| `/dev/ttyUSB0` | a serial port, at the factory-default 9600 baud |
| `/dev/ttyUSB0@19200` | a serial port at 19200 baud |

The separator inside a GPIB address is a comma, not a colon, because colons
already separate model, name and address. A slash is what distinguishes a
serial address from a GPIB one, so no spelling is ambiguous:

```bash
# a 5700A on the second GPIB adapter, a 3458A on the first
caldmm-cal-verify --source fluke5700a:cal1:gpib1,4 --meter hp3458a:dmm1:22 --dut source

# the same 5700A over its rear-panel RS-232C port instead
caldmm-cal-verify --source fluke5700a:cal1:/dev/ttyUSB0 --meter hp3458a:dmm1:22 --dut source
```

Nothing about a run changes with the interface — the same plan, the same
budgets, the same file. Over RS-232C the driver sends `REMOTE` to take
control (there is no REN line) and puts the port into `COMP` response style
before it parses anything; the console how-to has the details, including what
to do if the front panel has GPIB selected as the control interface. On Windows
the serial port is spelled `COM3` (or `COM3@19200`).

---

## Verifying the calibrator against a golden meter (`--dut source`)

```console
$ caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 --dut source \
      --quantities DCV --percentages 100 --no-negative --ambient 23.1 --yes -o src.csv

--- Plan ---------------------------------------------------------------------
  fluke5700a verified against hp3458a
  DUT       fluke5700a (the source)
  reference hp3458a
  rule      interval 1y, 95 % confidence, TUR floor 4:1, guard band g=1, temperature policy gate
  sampling  10 samples per point, 1 discarded first

  !! THE REFERENCE IS NO BETTER THAN THE DUT AT 3 OF 10 POINTS (30 %)
     Their spec ratio (DUT tolerance / reference tolerance) is at or below 1, so at those points the comparison mostly measures
     hp3458a rather than fluke5700a.  Worst 0.657 at DCV 11 V [DC11V] 2w.
     By quantity: DCV 3
     They are 1 min 24 s to 2 min 18 s of this run.  Skip them with --min-spec-ratio 1.

  10 points in 1 wiring group:
    group 1/1  voltage/ohms terminals: 10 points from #0
        DCV 2w     10 points  0.000V to 220.000V
  estimated 4 min 40 s to 7 min 40 s of instrument time, plus 1 wiring prompt (the first is the initial connection)
  not planned: 2 candidate points (DCV 2); --dry-run lists them with reasons
  !! this plan sources up to 220.0000V -- lethal, and above the 22 V threshold at which a queued fault silently inhibits the output

--- Instruments --------------------------------------------------------------
  source  fluke5700a  cal1:4
    *IDN?     FLUKE,5720A,5248000,1.0+B+*
    *OPT?     5725A Attached
    ONTIME?   45 min warm (specifications assume 30 min)
    CAL_DAYS? CAL 120 d, ZERO 3 d (zeros are due at 30 d)
    CAL_INTV? 365 d   CAL_CONF? CONF95
  meter   hp3458a  dmm1:22
    ID?       HP3458A
  ambient   23.100 C, 45.0 %RH from operator at 2026-01-01 00:00:00+00:00

--- Group 1 of 1: DCV 2w -----------------------------------------------------
  quantity : DCV -- 10 points, 0.000V to 220.000V
  terminals: voltage: the calibrator's OUTPUT terminals
  wiring   : 2-wire: calibrator OUTPUT HI/LO to the DUT terminals, sense off
  --yes: not waiting for an operator
[   1/10] g1 DCV 0 V [DC220MV] 2w          0.0000000V ->     2.0000000μV  err +1000000.000 ppm  TUR   2.36  FAIL  <<< FAIL
           |error| = 2e-06 V exceeds the tolerance limit 8e-07 V by 1.2e-06 V; note: TUR = 2.36 is below the required 4:1 (TL = 8e-07 V, U = 3.395e-07 V)
[   2/10] g1 DCV 0.22 V [DC220MV] 2w    220.0006600mV ->   220.0026600mV  err     +9.096 ppm  TUR   0.47  FAIL  <<< FAIL
           |error| = 2.001e-06 V exceeds the tolerance limit 1.1e-06 V by 9.011e-07 V; note: TUR = 0.47 is below the required 4:1 (TL = 1.1e-06 V, U = 2.331e-06 V)
[   3/10] g1 DCV 0 V [DC2_2V] 2w           0.0000000V ->     2.0000000μV  err +1000000.000 ppm  TUR   3.53  FAIL  <<< FAIL
           |error| = 2e-06 V exceeds the tolerance limit 1.2e-06 V by 8e-07 V; note: TUR = 3.53 is below the required 4:1 (TL = 1.2e-06 V, U = 3.395e-07 V)
[   4/10] g1 DCV 2.2 V [DC2_2V] 2w         2.2000066V ->      2.2000086V  err     +0.909 ppm  TUR   0.54  INDETERMINATE
[   5/10] g1 DCV 0 V [DC11V] 2w            0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR  11.78  PASS
[   6/10] g1 DCV 11 V [DC11V] 2w          11.0000330V ->     11.0000350V  err     +0.182 ppm  TUR   0.35  INDETERMINATE
[   7/10] g1 DCV 0 V [DC22V] 2w            0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR  23.56  PASS
[   8/10] g1 DCV 22 V [DC22V] 2w          22.0000660V ->     22.0000680V  err     +0.091 ppm  TUR   0.39  INDETERMINATE
[   9/10] g1 DCV 0 V [DC220V] 2w           0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR 294.53  PASS
[  10/10] g1 DCV 220 V [DC220V] 2w       220.0006600V ->    220.0006620V  err     +0.009 ppm  TUR   0.42  INDETERMINATE

--- Summary ------------------------------------------------------------------
  10 points: 3 pass, 3 fail, 4 indeterminate, 0 no spec (4 below the TUR floor); worst margin -150.0 % of TL at DCV 0 V [DC220MV] 2w; 10 flagged
  FAILED points:
    #0 DCV 0 V [DC220MV] 2w: error 2.0000μV against a limit of 800.0000nV
    #1 DCV 0.22 V [DC220MV] 2w: error 2.0011μV against a limit of 1.1000μV
    #2 DCV 0 V [DC2_2V] 2w: error 2.0000μV against a limit of 1.2000μV
```

In this direction the calibrator's tolerance limit comes from **its own
`UNCERT?`** for the point it is sourcing — the claim under test is the
calibrator's own claim — falling back to the transcribed specification table
when `UNCERT?` gives no usable figure (the row is flagged when it does).

**Which claim that is, is checked at the start.** `UNCERT?` is computed by the
instrument's firmware at *its* stored `CAL_INTV` and `CAL_CONF`, not at the
interval the plan names, and those two are the one thing a certificate may not
be ambiguous about (JCGM 106 §5.1). So the runner prints the claim —

```
  claim tested: UNCERT? at 365 d / CONF95 -- the tolerance every non-zero point
                of this run is judged against
```

— and **refuses** a run whose plan interval says something else:

```
error: the calibrator's own CAL_INTV is 1 d (CONF99) but this plan states
interval 1y: UNCERT? would judge every non-zero point against the 1-day
specification while the header, the spec-table cross-check and the zero points
state 1y.  Set the instrument's interval to match (caldmm-5700a-console ...
calintv 365), or run with --interval matching the instrument
```

(`calintv` is the service console's own command: open it with
`caldmm-5700a-console --address 4` and type `calintv 365`.)

On one unit that is not hypothetical: the unit is set to 1 day at 99 %, so
a run headed `interval 1y` would test the 24-hour specification (3.5 ppm +
3 µV at 10 V) at every non-zero point and the 1-year floor at its zeros.
`--force` downgrades the refusal to a warning; the header records both numbers
and the report states the claim either way.

**And the calibrator's own temperature condition is checked.** Every 5700A
specification table is headed "± 5 °C from calibration temperature", and its
tempco "does not apply unless operating more than ±5 °C from calibration
temperature" ([OM p.35]). The runner compares the ambient with `CAL_TEMP?` and
says so before the run when the bench is further away than that; the departure
goes in the header. Whether that makes a point `INDETERMINATE` is the model
file's temperature window, not this message.

**Zero setpoints are one such case, and worth understanding.** `UNCERT?`
answers in whatever unit suits the output — the manual lists "PPM, PCT, V, A,
OHM, etc." — and at a **zero setpoint this unit answers the range's absolute
floor in volts**: `UNCERT? +7.5000E-07,V,1` at `OUT 0 V` on the 220 mV range,
which is that range's 24-hour figure. So a zero point is
normally judged against the instrument's own figure like any other. The
specification table's **absolute floor** — 800 nV on the 220 mV range at 1 y,
1.2 µV on the 2.2 V range, and so on up — is the *fallback*, for a source that
answers a relative figure there (ppm of zero is `0`, which is not a tight claim
but the absence of one) or none at all; the row is flagged
`tolerance-limit-from-spec-table` when that happens. That is why the three zero
points above at 11 V and higher PASS on a 2 µV error while the two on the
tightest ranges FAIL — the floors there really are below 2 µV. **11 of the 175
points** in the shipped source-DUT plan are at exactly zero (DCV 5, DCI 5,
RES 1).

> If you ran a `--dut source` plan before commit `b002385`, its zero points
> reported an unconditional `FAIL` against a `tolerance limit 0 V` — all eleven
> of them, whatever the error. Re-run or discard those rows; the verdicts on
> every non-zero point were and are correct.
>
> The runner's `err` column above still prints `+1000000.000 ppm` at a zero
> setpoint. That is a transcript from an older build: a point *planned* at zero
> is now a zero point whatever the meter read, so both the CSV's ppm columns
> and `caldmm-cal-analyze`'s are blank there and the absolute error is what is
> shown. (Before that fix the analyzer did **not** blank it either, whatever
> this paragraph used to say: it keyed on the reference — the meter's own mean
> in this direction — and 2 µV of meter offset is not "approximately zero".)

**Give the reference its calibration temperature: `--ref-tempco-ref-c`.** In
this direction the reference component is the 3458A's own specification, whose
per-range tempco tables are in `hp3458a.yaml` — and the number to pass is the
meter's **`Tcal`**, the temperature of its last *external* calibration, which
no model file can know. The data sheet's with-ACAL coefficients are an
"additional error from Tcal ± 5 °C" (`3458a-datasheet.txt:445-449`), so `Tcal`
is both the centre of the window and the edge the coefficient is an adder
beyond. It is on the calibration certificate; `TEMP?` right after an ACAL is
the practical stand-in when that is all there is.

```console
$ caldmm-cal-verify ... --dut source --ambient 23.1 --ref-tempco-ref-c 22.8
```

Without it every row carries `tempco-not-evaluated` and the reference's
temperature drift is silently absent from the budget. **Inside** the window
there is no term either, and that is right: the coefficient is an adder for
operation outside it, not a slope from a point, so a bench 3 °C from `Tcal`
adds nothing and only a bench beyond ±5 °C does — from the window edge, not
from `Tcal`. (On `--dut meter` runs the reference is the calibrator's
`UNCERT?`, which already includes tempco — the flag is not needed and not used
there.)

---

## A full, unattended 3458A verification

One command verifies every range of every 3458A function the 5700A can drive:
DCV, ACV (SYNC and ANA), 4- and 2-wire ohms, DCI and ACI. It includes every row
of the Calibration Manual's performance tests (Tables 4-1 to 4-6, labelled
`[CM 4-x #n]`):

```bash
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter \
    --acal --lead-resistance 0.032 --ambient 23.1 -o runs/3458a-full.csv
# add --meter-option 002 for an Option 002 (high stability) unit
```

Three options change what a verdict can say:

* `--acal` runs the ACAL the data sheet's figures depend on ("within 24 hours and
  ±1 °C of last ACAL"), and every row is checked against its time and temperature.
  `--acal-recent` states that one was already run. With neither, every row is
  INDETERMINATE (`acal-condition-not-established`).
* `--lead-resistance OHMS` is the measured round-trip resistance of the HI and LO leads.
  A 2-wire reading includes it. Without it, the 10 Ω-range 2-wire short is
  INDETERMINATE (`lead-resistance-unknown`), because the leads alone are a tenth of its
  250 mΩ limit.
* `--meter-option 002` judges an Option 002 unit at its tighter dc-volts figures. The
  standard figures are looser and would pass errors the unit's own specification does
  not allow.

Each row's `TEMP?` is judged against the meter's own windows: dc against `CAL? 59/60`
±5 °C (±1 °C at 24 h), ac against the ACAL temperature ±1 °C. A row outside is
INDETERMINATE. So are the 100 nA – 10 µA dc-current points: their figures are typical
only (`dut-typical-only`). If the 5700A is past its own calibration interval, verdicts
are still stated, with `reference-calibration-overdue` on every row.

The operator's whole job:

1. **Connect once** for the voltage/ohms group:
   * 5700A OUTPUT HI/LO to the 3458A HI/LO inputs;
   * 5700A SENSE HI/LO to the 3458A Ω SENSE HI/LO inputs;
   * GUARD to GUARD;
   * the 3458A Terminals switch on Front.

   Answer the warm-up and ≥22 V questions, then press Enter at the first wiring
   prompt.
2. `--acal` runs `ACAL ALL` with the calibrator in standby, then waits the
   manual's 30 minutes of settling. About 46 minutes, unattended.
3. DCV, ACV, 4-wire and 2-wire ohms run with no further prompt: about 1.5 h.
4. **The one re-cable.** At the current group's prompt, move the lead on the
   3458A's HI input to its **I** input and press Enter. LO, guard and the sense
   leads stay put.
5. DCI and ACI run: about 1 h.

Before anything is energised the runner compares the 3458A's `TEMP?` with
`CAL? 59` (voltage functions) and `CAL? 60` (ohms and current). If it is more
than 5 °C away, the run is refused, as the manual requires (`--force` runs it
anyway, flagged). The result's header records all three temperatures and the
ACAL.

`caldmm-cal-analyze runs/3458a-full.csv` then prints two extra sections:

* **Range coverage:** every 3458A range with its verdict counts, and any range
  not exercised.
* **Manufacturer test card:** every Table 4-x row with |error| against the
  card's 1-year limits, for both the standard unit and Option 002.

How this maps onto the manual is in
[`docs/3458a/cal-manual-verification.md`](3458a/cal-manual-verification.md):

* the method differences (no transfer standard, the calibrator's own 0 V / 0 Ω / 0 A
  for offsets, front terminals only);
* why the card is tighter than the data-sheet ANA table, so the report shows it
  beside the verdict;
* how every AC point is held to the 5700A's Volt-Hertz and 1100 V-range limits.

## Verifying a meter against the calibrator (`--dut meter`)

```console
$ caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 \
      --dut meter --quantities DCV,RES --percentages 100 --no-negative \
      --points ladder -o run.csv

--- Plan ---------------------------------------------------------------------
  hp3458a verified against fluke5700a
  DUT       hp3458a (the meter)
  reference fluke5700a
  rule      interval 1y, 95 % confidence, TUR floor 4:1, guard band g=1, temperature policy gate
  sampling  10 samples per point, 1 discarded first

  !! THE REFERENCE IS NO BETTER THAN THE DUT AT 13 OF 28 POINTS (46 %)
     Their spec ratio (DUT tolerance / reference tolerance) is at or below 1, so at those points the comparison mostly measures
     fluke5700a rather than hp3458a.  Worst 0.376 at RES 1.9 OHM [OHM1_9] 4w.
     By quantity: DCV 1, RES 12
     They are 6 min 04 s to 9 min 58 s of this run.  Skip them with --min-spec-ratio 1.

  28 points in 1 wiring group:
    group 1/1  voltage/ohms terminals: 28 points from #0
        DCV 2w     10 points  0.000V to 1.000kV
        RES 4w     17 points  0.000OHM to 19.000MOHM
        RES 2w      1 points  100.000MOHM to 100.000MOHM
  estimated 13 min 04 s to 21 min 28 s of instrument time, plus 1 wiring prompt (the first is the initial connection)
  !! this plan sources up to 1.0000kV -- lethal, and above the 22 V threshold at which a queued fault silently inhibits the output

--- Instruments --------------------------------------------------------------
  source  fluke5700a  cal1:4
    *IDN?     FLUKE,5720A,5248000,1.0+B+*
    *OPT?     5725A Attached
    ONTIME?   45 min warm (specifications assume 30 min)
    CAL_DAYS? CAL 120 d, ZERO 3 d (zeros are due at 30 d)
    CAL_INTV? 365 d   CAL_CONF? CONF95
  meter   hp3458a  dmm1:22
    ID?       HP3458A
  ambient   23.147 C, 41.2 %RH from probe at 2026-01-01 00:00:00+00:00

  !! THIS PLAN SOURCES UP TO 1.0000kV.
     Check the leads are rated for it, that nothing else is connected to the calibrator's OUTPUT terminals, and that nobody else is at the bench.
  → Override and accept a run that sources up to 1.0000kV on fluke5700a? [type "yes" to accept / Enter to repeat] yes

--- Run ----------------------------------------------------------------------
  run id 20260802T100123, writing run.csv

--- Group 1 of 1: DCV 2w, RES 4w, RES 2w -------------------------------------
  group    : voltage/ohms terminals -- 28 points
    DCV 2w     10 points, 0.000V to 1.000kV
    RES 4w     17 points, 0.000OHM to 19.000MOHM
    RES 2w      1 points, 100.000MOHM to 100.000MOHM
  terminals: the calibrator's OUTPUT (and SENSE) terminals
  wiring   : one cable set for the whole group: calibrator OUTPUT HI/LO to the meter's HI/LO inputs, calibrator SENSE HI/LO to the meter's ohms SENSE HI/LO inputs, calibrator GUARD to the meter's GUARD; nothing is touched again until the current group

  The calibrator is in STANDBY.  It goes to operate as soon as this prompt is answered, and re-configures itself between the functions above.
  → press Enter when the wiring above is in place (Ctrl-C to abort): 
[   1/28] g1 DCV 0 V [0.1 range] 2w        0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR   1.56  FAIL  <<< FAIL
           |error| = 2e-06 V exceeds the tolerance limit 2e-06 V by 1e-14 V; note: TUR = 1.56 is below the required 4:1 (TL = 2e-06 V, U = 1.285e-06 V)
[   2/28] g1 DCV 0.1 V [0.1 range] 2w   100.0003000mV ->   100.0020000mV  err    +17.005 ppm  TUR   2.34  INDETERMINATE
[   3/28] g1 DCV 0 V [1 range] 2w          0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR   1.56  FAIL  <<< FAIL
           |error| = 2e-06 V exceeds the tolerance limit 2e-06 V by 1e-14 V; note: TUR = 1.56 is below the required 4:1 (TL = 2e-06 V, U = 1.285e-06 V)
[   4/28] g1 DCV 1 V [1 range] 2w          1.0000030V ->      1.0000020V  err     -1.000 ppm  TUR   1.95  INDETERMINATE
[   5/28] g1 DCV 0 V [10 range] 2w         0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR   2.33  INDETERMINATE
[   6/28] g1 DCV 10 V [10 range] 2w       10.0000300V ->     10.0000020V  err     -2.800 ppm  TUR   1.66  INDETERMINATE
[   7/28] g1 DCV 0 V [100 range] 2w        0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR  21.37  PASS
[   8/28] g1 DCV 100 V [100 range] 2w    100.0003000V ->    100.0000020V  err     -2.980 ppm  TUR   2.06  INDETERMINATE
[   9/28] g1 DCV 0 V [1000 range] 2w       0.0000000V ->     2.0000000μV  err      +2.0000μV  TUR  17.24  PASS
[  10/28] g1 DCV 1000 V [1000 range]      1.0000030kV ->     1.0000000kV  err     -2.998 ppm  TUR   4.42  PASS
```

Reading a point line: `[n/total] group  point label  sourced -> measured  error
TUR verdict`. The reason line under a FAIL states the error, the limit and by
how much it was exceeded, and — where relevant — that the TUR floor was also
below 4:1.

### The ≥22 V confirmation, and `--yes`

Any plan that reaches 22 V asks for a typed `yes` before anything is
energized. The maximum is taken over **the groups that will actually run**, so a
resume into the resistance group is not asked about the 1000 V the first session
already sourced.

`--yes` on its own does **not** accept it. Unattended, the maximum has to be
*stated*: `--accept-max 1000` is the typed acknowledgement the interactive
prompt asks for, and a number that does not equal the plan's real maximum is
refused — a plan whose maximum you cannot name is one you have not read.

```
error: --yes will not accept a run that sources 1.0000kV, at or above the 22 V
hazard threshold, with nobody at the bench.  Answer the confirmation yourself,
state the maximum with --accept-max 1000, or cap the run with --max-voltage
(below 22 V no acknowledgement is needed at all)
```

Note the maximum is the *plan's*, not the model's, and it is taken over the
groups that will actually run — so narrow the plan or resume into a later
group and the number changes. The shipped `--dut source` ladder still reaches
1000 V (its 100 % point on the 1100 V range), so `--accept-max 1000` is what
it wants; a `--percentages 10` run of the same plan tops out at 110 V and
wants `--accept-max 110`. Read the plan block the runner prints — it states
the figure — rather than guessing from the model.

**`--accept-max` states a maximum; `--max-voltage` imposes one.** They are
different flags for different jobs and they compose. To run the same dc plan
but stop at 100 V:

```bash
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 \
    --dut meter --quantities DCV --max-voltage 100 -o dmm.csv
```

```
  --max-voltage 100: dropped 6 points above 100.0000V, 39 remain
     the highest dropped point was 1.0000kV; this run is NOT a verification of
     the ranges above the ceiling
```

The ceiling is applied **before** the plan is validated and before the
confirmation, so the census, the group listing, the duration estimate and both
the typed prompt and `--accept-max` are all about the reduced plan — capped at
100 V, `--accept-max 100` is what it now wants, and capped below 22 V nothing
is asked at all. Only voltage points are dropped; a resistance catalogue in the
same plan is untouched. What it removed goes in the result header
(`max_voltage_v`, `points_dropped_by_max_voltage`), because a file that stops
at 100 V under a plan named for 1100 V has to say which of the two it is.

**The calibrator's own entry limits are armed too**, before the first point:
`LIMIT` is the one interlock the *instrument* enforces whatever software asks
(faults 815/856). Both pairs, because the voltage limit bounds dc and ac volts
and the current limit bounds dc and ac amps and they are separate commands —
so a dc-current run arms the pair that bounds it rather than nothing at all.

They are armed to the plan's largest **entry**, which is not always its largest
setpoint: a point that needs the 5700A's range lock is programmed to the
range's own maximum first (1100 V for DC1100V, 2.2 A for DC2_2A) so that
autoranging selects the range, and only then to the setpoint. `LIMIT` refuses
an entry whatever it was for, so a limit armed at the largest setpoint would
fault the anchor of every 10 %-of-range point and fail them. The anchor is
entered in **standby** and never energised — the typed acknowledgement above is
still about what reaches the terminals. What was in force is read first and put
back at shutdown, and what was armed goes in the header (`output_limits`), which
is why that field can read higher than the plan's maximum. All best effort: a
calibrator that will not take a pair still runs the plan `validate_output`
checked offline, and says so.

`--yes` is also **refused outright when more than one wiring group remains**:

```
error: --yes would run 2 wiring groups unattended (DCV 2w, ACV 2w, RES 4w, RES 2w, DCI 2w, ACI 2w).  A
group boundary is where the leads are physically moved, so every group after
the first would be measured through the previous group's wiring and recorded
as valid.  Narrow the run to one group (--quantities, --min-spec-ratio or
--start-group), or drop --yes and answer the prompts.
```

### Interrupting and resuming

**SIGTERM and SIGHUP are handled too**, not just Ctrl-C: a `kill`, a
`timeout`, a systemd stop or a dropped ssh session raises inside the runner
rather than killing the interpreter where it stands, so the same shutdown path
runs and the calibrator ends in standby. (Their default disposition does not
unwind, which is why a script that energizes an output must never be wrapped
in a bare `timeout`.)

Ctrl-C at any point. Rows are written *before* they are printed, so nothing
measured is lost, and the source is put in standby:

```
--- Interrupted --------------------------------------------------------------
  interrupted.
  12 points measured in this session are on disk; every row was flushed as it was taken.
  file    run.csv
  resume  caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 --dut meter --quantities DCV,RES --percentages 100 --no-negative --points ladder --resume run.csv --start-group 0
$ echo $?
3
```

Paste the printed command back. It refuses a file whose header or point
identities disagree with the plan about to run, and it continues **inside** the
partially-measured group so nothing is measured twice:

```
--- Resume -------------------------------------------------------------------
  run.csv: 12 rows already measured, run id 20260802T100156, started 2026-08-02 10:01:56-07:00
  group 1 is incomplete (12 of 28 points measured); it continues at its point 12, so nothing is measured twice
  resuming at group 1 of 1 (DCV 2w, RES 4w, RES 2w), 16 points left
...
--- Group 1 of 1: DCV 2w, RES 4w, RES 2w -------------------------------------
  group    : voltage/ohms terminals -- 28 points, resuming at #12 (16 left)
    DCV 2w     10 points, 0.000V to 1.000kV
    RES 4w     17 points, 0.000OHM to 19.000MOHM
    RES 2w      1 points, 100.000MOHM to 100.000MOHM
  terminals: the calibrator's OUTPUT (and SENSE) terminals
  wiring   : one cable set for the whole group: calibrator OUTPUT HI/LO to the meter's HI/LO inputs, calibrator SENSE HI/LO to the meter's ohms SENSE HI/LO inputs, calibrator GUARD to the meter's GUARD; nothing is touched again until the current group

  The calibrator is in STANDBY.  It goes to operate as soon as this prompt is answered, and re-configures itself between the functions above.
  → press Enter when the wiring above is in place (Ctrl-C to abort): 
[  13/28] g1 RES 1.9 OHM [OHM1_9] 4w     1.9000057OHM ->    1.9000020OHM  err     -1.947 ppm  TUR   7.10  PASS
[  14/28] g1 RES 10 OHM [OHM10] 4w      10.0000300OHM ->   10.0000020OHM  err     -2.800 ppm  TUR   3.97  INDETERMINATE
...
[  27/28] g1 RES 1.9e+07 OHM [OHM19M]  19.0000570MOHM ->  19.0000000MOHM  err     -3.000 ppm  TUR 110.33  PASS
[  28/28] g1 RES 1e+08 OHM [OHM100M]  100.0003000MOHM -> 100.0000000MOHM  err     -3.000 ppm  TUR 101.99  PASS

--- Summary ------------------------------------------------------------------
  28 points: 10 pass, 2 fail, 16 indeterminate, 0 no spec (16 below the TUR floor); worst margin -0.0 % of TL at DCV 0 V [0.1 range] 2w; 28 flagged
  (16 measured in this session, 12 already in the file)
  FAILED points:
    #0 DCV 0 V [0.1 range] 2w: error 2.0000μV against a limit of 2.0000μV
    #2 DCV 0 V [1 range] 2w: error 2.0000μV against a limit of 2.0000μV
  file    run.csv
  analyse caldmm-cal-analyze run.csv
$ echo $?
1
```

Note that 28 rows came out of a 28-point plan across two sessions, with no
duplicates. The run id is the one from the file — the provenance block is
written once, when the file is created.

Exit status: `0` finished with no FAIL · `1` finished with at least one FAIL ·
`2` usage, an invalid plan, a pre-flight refusal, or a declined confirmation ·
`3` interrupted or abandoned (the printed `--resume` command continues it).

### What every run does before it trusts the wiring

Two rules run on **every verification run**, and **no flag turns either off**.
They are enforced by the engine, which is what `caldmm-cal-verify` drives; the
adjustment procedures (`--adjust`) do not go through it and carry their own
implementation of the same two rules, described in their own sections below.

**The wiring proof.** Before the first point, at the start of every wiring
group, on every resume and whenever the calibrator changes range, the source
goes to a level that cannot hurt anything — `max(10 mV, 0.1 % of the range's
full scale)` for volts, `max(100 µA, 0.1 % of full scale)` for amps, the
standard itself for ohms, at the point's own frequency — and the meter is
asked what it sees. It has to agree to within 10 %. A lead in the wrong
socket, a sense pair still shorted from the last group or a current path
through the volts terminals reads as nothing *there*, at 10 mV, instead of as
a catastrophe at 1000 V twenty minutes later. A proof that fails writes its
row with `wiring-proof-failed` and stops the run with the output in standby;
the planned level is never applied to wiring that could not prove itself.

A proof that could not be **read** is a different finding and carries a
different flag. If the meter raises instead of returning a value — a driver
timeout, a refused capability, an interface that went away — nothing was
established about the wiring either way, so the row says
`wiring-proof-unreadable` and its Reason names the underlying error. Check
the meter and the interface, not the leads. (This distinction was added on
2026-08-24: a 3458A burst timeout had been reported as `wiring-proof-failed`
and sent an operator to re-check a correctly wired 4-wire hookup, twice.)
The run still stops with the output in standby: an unproved wiring is not
sourced into either way.

**...and never below what the meter can read** (the controller's amendment of
§8, 2026-08-22). Those figures are a property of the *calibrator's* range and
say nothing about the instrument that has to judge the reading. A meter whose
lowest range for a function cannot resolve a 0.1 %-of-calibrator-range probe to
±10 % would fail a bench that is wired correctly. So the level is raised to at least **5 % of the meter's lowest
usable range for the function**, or that range's own stated `min_reading_frac`
where the manufacturer names a larger one, and it always stays strictly below
the hazard thresholds (< 22 V, < 220 mA). The same rule, from the same code,
applies to the adjustment procedures.

Where even the raised level cannot be had — no range reads it, or the smallest
readable level is not below the hazard threshold — the proof is **skipped, not
faked and not aborted on**: the first row of that group carries
`wiring-proof-not-resolvable` and a plain-language note saying why, the report
leads with it in "Read this first", and the points themselves are measured
normally. No shipped pairing hits this today; it exists so that adding a model
file with a coarse bottom range cannot silently produce runs whose proofs prove
nothing.

The proof **de-energises first**: the source goes to standby, *then* the meter
is put on the range that holds the proof level, then the proof is sourced. A
proof is one or two decades below the point it guards, so its range is below
the point's, and nothing between two points of a group takes the output off on
its own — without the standby the 0.1 A → 1 A step of a dc-current group would
put a meter on a low current range, behind a high-value shunt, with 100 mA
still flowing, and
at 1 A the same mistake demands 5 V against the calibrator's 2 V compliance
limit (fault 1819). The proof is read on **its own** range and not on the
point's, because a 2.2 mA proof read on a 3 A range sits at 0.07 % of full
scale, inside that range's own floor, and would abort a bench that was wired
correctly; the meter is put back up on the point's range — under the harmless
proof level, before the planned amplitude — by the point that follows. A
standby the calibrator will not confirm stops the run before the meter is
touched at all, with the row flagged `standby-not-confirmed`: nothing was
re-ranged and nothing further was sourced, but **the output may still be
live**, so press RESET and check the standby light before touching the
terminals. It is the one abort the report leads with, above every other.

**The gross-error abort.** After every reading, an error above **ten times**
the point's own tolerance (or ten times the reference's expanded uncertainty
where the point has no usable limit) stops the run at that point, with the row
recorded and flagged `gross-error-abort`. An instrument out of specification
is the finding a verification exists to make — that is a `FAIL` and the run
goes on. Ten tolerances is not an instrument, it is a connection, and every
later point would measure the same mistake.

Both stop the run the same way a trip does: the rows already taken are in the
file, **the source is put in standby before the abort row is even written**,
and the printed `--resume` command continues from the group it stopped in once
the cause is fixed.

**A trip is also caught between sourcing and reading, not just before the
next point.** The calibrator's health is re-read after the reading as well as
before it, so a trip or a queued fault that happens *while* a point is being
measured — including the last point of a run, which has no next point to
catch it — is caught there and then: the row is recorded `point-failed` /
`source-tripped-mid-point`, carries no verdict, and the run stops the same
way.

Both reads honour the plan's own discards, so the sample a plan throws away
after a range change is not the sample a ±10 % check is decided on.

**Which post the current comes out of is read, not assumed.** For any plan
that sources current the runner sends `CUR_POST` and reads `CUR_POST?` back
**twice** — once at open, so the header records a post that was really read
and a unit that will not take the setting stops the run before anyone is told
to cable anything, and again at the group boundary, because the front panel
can move it in between. Either read-back disagreeing stops the run. The group
prompt then names the post *the instrument reports*, and the wiring line under
it agrees with it. A source that cannot report it at all is rendered
differently — "whichever post CUR_POST selects — assumed … (requested, not
read back)" — in the prompt and in the header, because an unread setting
printed as a fact is the defect this closes. `--current-post` selects it — `normal` (the default, and
the instrument's own: the OUTPUT HI binding post) or `aux` (the AUX CURRENT
OUTPUT post). This is not a formality: the setting is **non-volatile, survives
`*RST` and survives power-off** ([OM Table 5-2, p.176]), so on any unit the
post in force is whatever the last operator left behind, and a current sourced
into a post nobody has cabled trips the calibrator on compliance (fault 1819)
after the group has already been declared good to go. The post is recorded in
the result header.

**The meter's warm-up is asked for and recorded.** Every meter specification
is stated after a warm-up — 4 h for the 3458A — and the meter cannot report
how long it has been powered. So the runner
prints the figure with the instrument's identity and asks the operator to
confirm it, defaulting to **not confirmed**: answering anything but `y` (or
running with `--yes`, which cannot know) records `meter_warm_confirmed=no` in
the header and puts `meter-warmup-unconfirmed` on **every row**. It never
refuses the run: an unconfirmed warm-up is a condition of the claim, and the
place for that is the record.

### Ranges are chosen before anything is energised

The meter is put on a **fixed** range — chosen from the plan's nominal, with
the meter's own overrange allowance — *before* the source goes to operate, and
autorange is explicitly off. Choosing a range from a live output means
choosing it after the output has already been driven into whatever range the
meter happened to be on: on a dc-current group that is the 3458A's 100 nA
shunt, 545 kΩ, and the 22 µA point after the zero would demand 12 V through it
against a 10 V compliance limit — tripping the calibrator on every range.

Above the hazard thresholds (**> 22 V** or **> 220 mA**, and any point on a
range whose full scale exceeds them) the meter's range is **read back from the
instrument** and compared with what the adapter applied; a mismatch aborts
before operate, with the output never energised (`meter-range-mismatch`).
Above 220 mA — always — and above 22 V whenever the meter's range was **not
verified**, the source first goes to **10 % of the target**, one quick reading
has to come back within 10 % of that level, and only then does it go to the
full value (`prestep-failed` otherwise). The 10 % reading is taken on the
target's own range, which holds it comfortably on every shipped pairing.

"Not verified" covers two cases, and the second is easy to miss: a meter that
cannot fix its range at all, and a meter that can fix it but cannot **report**
it. A range that is set and never checked is a range nothing has
verified, so its 100 V and 1 kV points get the pre-step, exactly as a meter with
no manual ranging would. Where a pre-step *does* run it re-configures the meter,
so the range read-back is taken **again** afterwards: what the output is
energised against has to be the configuration that was read back, not the one
before it.

A range step up on the meter always precedes an amplitude step up on the
source — and a range step **down** never happens under a live output. Ranging
up while a lower level is live is safe and is how the meter gets from a proof
to its point; ranging down is the hazard, so the source is put in standby
first, whether the step down is a probe's or a plan point below the one before
it.

---

## Ambient temperature is not optional

`caldmm-cal-verify` will not return `PASS` for a point whose model declares a
temperature window unless an ambient was recorded, so without an ambient
reading every such point is `INDETERMINATE` by design. This is not a bug and it is not
loosened by `--temperature-policy flag`.

Supply it as a measured number:

```bash
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter --ambient 23.4 -o run.csv
```

A logged room sensor can be wired in instead by implementing
`caldmm.cal.roles.EnvironmentProbe` (one `read()` method returning an
`Environment`).

The 3458A and the 5700A declare no *absolute* window — their figures are
written for a window **relative to their own stored calibration temperature**
("Absolute Uncertainty ±5 °C from calibration temperature" heads every 5700A
table). The engine reads the calibrator's `CAL_TEMP?` once per run and
resolves that window from it, so a bench 6 °C from the stored temperature is
out of window on the instrument's own terms — and the same check is applied to
whichever instrument is the *reference*, whose figures are no more valid
outside its window than the DUT's are outside theirs (flag
`reference-temperature-out-of-window`). Where no centre is in hand nothing is
invented: the runner prints a one-line note. For a model with an absolute window and no
ambient it prints the block, names the window, says the fix is `--ambient` and
*not* `--temperature-policy flag`, and makes you decide.

`--temperature-policy` is `gate` (default), `flag` or `strict`. `flag` also
disables the tight window a 24-hour specification needs; supply an `--ambient`
instead.

### Conditions of the specification are not advice

A specification holds **under its stated conditions**. Where one of them was
not shown to hold, the point is `INDETERMINATE` with the flag
`conditions-not-met` and its own reason — never a `PASS`, and never a counted
`FAIL` either, because a limit that does not apply cannot be exceeded. Such
points are counted in their own bucket, are not in the `FAILED points` list,
and do not set exit status 1. The conditions are:

| condition | flag | where it comes from |
|---|---|---|
| the calibrator had not finished warming up | `source-not-warmed-up` | OM p.30: "valid after a warm-up period of twice the time the calibrator has been turned off, up to a maximum of 30 minutes" |
| its dc zeros are overdue | `source-zeros-due` | OM p.29: "at least every 30 days" |
| the ambient is outside the window | `temperature-out-of-window` | the DUT's own window, absolute or relative |
| the ambient was never recorded | `ambient-temperature-unknown` | there is a window and nothing to check it against |
| the *reference* is outside its window | `reference-temperature-out-of-window` | its figures do not cover the point either |

The TUR floor is deliberately **not** in that list: there the limit does
apply and only the measurement is coarse, so a failure past it still stands.

---

## Run sizing

The shipped 3458A-against-5700A plan, at defaults (10/50/90/100 % of each
range, zero and both polarities, one connection per quantity, plus the
Calibration Manual's own test points for a meter under test):

| | points | groups | instrument time |
|---|---:|---|---|
| `--dut meter` | 257 (35 skipped) | voltage/ohms (DCV, ACV, RES 4w, RES 2w), then current (DCI, ACI) | 2 h 00 m – 3 h 17 m |
| `--dut meter --points manufacturer` | 42 | the same two | 20 m – 32 m |
| `--dut source` | 211 (35 skipped) | the same two | 1 h 39 m – 2 h 42 m |

Per function, meter-DUT: DCV 45, ACV 80 (SYNC ladder + ANA Table 4-3), RES 17
four-wire + 3 two-wire, DCI 72, ACI 40.

**Groups are terminal sets, not functions**, so a full run has **two
operator prompts**: the initial connection, and one re-cable before the current
functions. dc volts, ac volts and both resistance wirings run through one
4-wire cable set (calibrator OUTPUT and SENSE to the meter's HI/LO and ohms
SENSE inputs, plus guard) and the calibrator switches EXTSENSE itself. Then the
HI lead moves to the meter's current (I) input for DCI and ACI. Narrow the run:

```bash
--points manufacturer        # the Cal Manual's Tables 4-1..4-6 only: 42 points
--quantities DCV,ACV,RES     # one group, no re-cable at all
--no-negative                # halves the dc points
--percentages 10,100         # endpoints only
--min-spec-ratio 1           # drop the points where the reference is no better
--max-voltage 100            # a real ceiling: stop at 100 V, keep everything else
```

Or *widen* it. The planner picks **one wiring per quantity** by default. Ask for
both when both resistance paths matter at every catalogue value:

```bash
--connections 4w,2w          # 35 RES points, 17 4w + 18 2w, still one group
```

A wiring neither instrument offers on a quantity is reported
(`!! no point is wired 4w`) and listed in `--dry-run`, never quietly
substituted. Note that asking for `4w` *alone* also disables the fallback that
rescues the 100 MΩ standard: it has no four-wire mode, so it is dropped with
its reason rather than wired the other way behind your back.

The duration estimate is a measured envelope (`SECONDS_PER_POINT = 28–46 s`),
not a computation from settling times and NPLC — a precise-looking number built
out of assumptions about GPIB latency would be worse.

---

## Reading a report

```bash
caldmm-cal-analyze run.csv                     # markdown + PDF beside the CSV
caldmm-cal-analyze run.csv --no-pdf --print    # markdown to a file and stdout
caldmm-cal-analyze run.csv --full-budgets      # every budget in the PDF too
caldmm-cal-analyze after.csv --compare before.csv
caldmm-cal-analyze run.csv --spec-ratio-warn 4 # the 4:1 calibration convention
```

It opens the file and nothing else — no instrument, no database, no network,
and no display (matplotlib is forced to `Agg`).

You usually do not have to run it: **a completed verification run renders the
same report itself**, in-process, next to the CSV (`--no-report` opts out).
A render failure never changes the run's exit status — the CSV is the record,
the report is a rendering of it, and the manual command above is printed when
rendering fails.

```console
$ caldmm-cal-analyze run.csv
run.csv: 28 points: 10 pass, 2 fail, 16 indeterminate, 0 no spec (16 below the TUR floor); worst margin -0.0 % of TL at DCV 0 V [0.1 range] 2w; 28 flagged
  indeterminate: 16 below the TUR floor (reference not good enough), 0 outside the specification's stated conditions (0 of them temperature), 0 in the guard band, 0 with no budget — causes overlap
  (16 more carry a temperature condition the 'gate' policy does not block on; see the report)
  WARNING: the reference is no better than the DUT at 13 of 28 points (46 %); those points measure fluke5700a, not hp3458a
report: run-report.md
plots : run.pdf (15 pages)
```

### The four verdicts

| verdict | what it means | what to do |
|---|---|---|
| **PASS** | `\|error\| ≤ TL − w`, with `w = g·U` | nothing |
| **FAIL** | `\|error\| > TL` | the instrument is out of tolerance at that point |
| **INDETERMINATE** | neither statement can be made at this confidence | depends on *why* — see below |
| **NO_SPEC** | the DUT's model has no specification row for the point | not a failure; the point was measured and recorded |

`NO_SPEC` is deliberately not folded into `INDETERMINATE`: "there is no
specification" and "the specification cannot be evaluated" are different
statements and must not share a column on a certificate.

### Why a point is INDETERMINATE — the causes

```
### Why the indeterminate points are indeterminate

| cause | points | what it means |
|---|---:|---|
| TUR below the 4:1 floor | 13 | the **reference** is not good enough at that point; re-running will not change it |
| temperature conditions not shown | 0 | the **specification's stated conditions** were not demonstrated; record the ambient and repeat |
| stated conditions not shown (any) | 0 | the temperature above **plus** warm-up and overdue zeros: the specification was not in force, so neither conformity nor non-conformity is stated |
| error inside the guard band | 0 | the **DUT** is close enough to its limit that neither statement can be made at this confidence |
| no uncertainty budget | 0 | nothing was supplied to support any statement |

A point blocked by two causes is counted under both; these are independent blockers, not a partition of the indeterminate total.
```

The two that get confused are the first two, and the distinction is the most
useful thing in the report:

- **Below the TUR floor.** `TUR = TL / U` is under 4:1. **No amount of
  re-measuring helps** — more samples shrink the Type A term, and the Type A
  term is not what is limiting you. The *reference* is. The fix is a better
  reference, a different direction (see the top of this document), or accepting
  a lower floor with `--tur-floor` and saying so on the certificate. It is
  **not** a statement about the DUT.
- **Temperature-gated.** The specification states conditions that were not
  shown to hold: no ambient was recorded, or the recorded one was outside the
  model's window. That is a *procedural* fix — record the ambient and repeat
  the point.

The report also separates "carries a temperature condition" from "was blocked
by one". Both the 3458A and the 5700A declare no absolute window, so every row
of that pairing carries a `no-temperature-window` flag; the policy does not
gate on it, and the report says so rather than reporting 13 points as
temperature-gated when the TUR floor blocked all 13.

### Choosing the decision rule: `--rule`

The default is **guarded acceptance** (JCGM 106 8.3.3 / ILAC-G8 4.2.3):
PASS below `TL − U`, FAIL above `TL`, INDETERMINATE between. Note what that
asymmetry means at low TUR: a FAIL is asserted at the bare tolerance limit,
so with TUR ≈ 0.5 an error 1 % past `TL` is called non-conformity even though
the false-reject probability there approaches 50 %. When a false reject is
the expensive outcome — failing a transfer standard you would have to pull
from service, or a customer instrument that would ship back — run with
`--rule guardbanded-reject` (JCGM 106 8.3.4): FAIL only above `TL + U`, with
the band in between INDETERMINATE. Never put a guarded-rejection PASS on a
statement of conformity a consumer relies on; the rule as applied is printed
in every row's `Rule` cell either way. `--rule simple` (ILAC-G8 4.2.1, no
guard band at all) exists for comparison against labs that use it.

### The uncertainty budget

Every point gets one, and **every budget in the report is reconstructed from
the `Budget` cell of its own row** — `u_c`, `ν_eff`, `k` and `U` are recomputed
from the components, never read back from the file. Corrupting those cells in
the CSV changes nothing in the report.

```
### Point 4 — DCV 0 V [10 range] 2w [FAIL]

Dominated by: **hp3458a resolution** (100 % of the variance).

Uncertainty budget [V]
component                       distribution  ty     dof          u_i          c_i      c_i*u_i    %var
-------------------------------------------------------------------------------------------------------
hp3458a resolution              rectangular   B      inf    2.887e-08            1    2.887e-08   99.99
repeatability                   normal        A        9    3.333e-10            1    3.333e-10    0.01
input loading                   rectangular   B      inf    5.774e-16            1    5.774e-16    0.00
reference (fluke5700a UNCERT?)  normal        B      inf            0            1            0    0.00
-------------------------------------------------------------------------------------------------------
combined standard uncertainty u_c = 2.887e-08 V
effective degrees of freedom nu_eff = 5.064e+08
coverage factor k(95 %) = 1.960
expanded uncertainty U = k*u_c = 5.658e-08 V (95 % confidence)

Plan note: spec ratio 0.625 (hp3458a 5e-07 / fluke5700a 8e-07 V)

Decision: |error| = 2e-06 V exceeds the tolerance limit 5e-07 V by 1.5e-06 V
```

> **That reference line of `0` cannot happen any more, and never described the
> instrument.** It is what a source answering a *relative* `UNCERT?` at a zero
> setpoint produced — ppm of zero — and it made the budget claim a reference
> contribution of nothing, which is where the 57 nV `U` above comes from. A
> figure of zero is now treated as no figure at all: the term is rebuilt from
> the specification table's absolute floor and the row is flagged
> `reference-uncertainty-from-spec-table`, which on the 220 mV range means a
> `U` near 0.6 µV rather than 57 nV, and a TUR below 1 rather than 9. The real
> 5700A does not take that path: it answers `+7.5000E-07,V,1` at `OUT 0 V`.

`k` is Student-*t* at `ν_eff`, **not** a fixed 2. The `%var` column and the
`Dominated by:` line above the table are the actionable half — they tell you
what to change:

| dominant term | what it means | what to do |
|---|---|---|
| `<meter> resolution` | you are quantisation-limited | a longer aperture, more digits, a lower range |
| `repeatability` | noise-limited | more samples, more NPLC, better guarding, let it settle |
| `reference (… UNCERT?)` | the reference is the limit | a better reference, or the other direction |
| `thermal EMF` | at a short or a zero, usually | copper-to-copper joints, a thermal soak, reverse the leads |
| `input loading` | source impedance against meter input Z | 4-wire, or the high-Z input setting |

More samples only ever shrink `repeatability`. If it is not the dominant term,
re-running the point is wasted bench time.

**The thermal-EMF term is declared by default.** Every voltage budget carries
a **1 µV** bound — what clean copper-to-copper joints do — and the row says so
with the flag `thermal-emf-default`, because a bound that was assumed rather
than measured has to be visible as such. `--thermal-emf <volts>` replaces it
with a bound you assert about *your* cabling (more for binding posts, nickel
plating or dissimilar metals; less if you have measured yours), and
`--thermal-emf 0` states that there is none. It used to be absent unless the
flag was typed, which at a short or a zero point — where it is the *largest*
term, against a 0.8 µV floor on the dc 220 mV range — silently overstated the
quality of the measurement.

On ohms and current the bound cannot be converted without a figure no model
file carries: a loop EMF becomes an ohms error as `emf / I_test` and a current
error through the meter's shunt. Rather than inventing a test current or
dropping the term in silence, low-ohms points (≤ 100 Ω) and current zeros
carry `thermal-emf-not-converted` and no term.

**Whose uncertainty is which.** `repeatability` and `<meter> resolution` are
measured on the meter, whichever side is under test. On a `--dut meter` run
they belong to the *measurement process* (the Z540.3 convention: the DUT's own
noise legitimately depresses the TUR of a claim about the DUT). On a
`--dut source` run the same two terms sit **on top of** the meter's datasheet
limit in `reference (hp3458a spec)`, which already envelopes noise and
quantisation — the budget is deliberately conservative in that direction, and
part of why source-DUT TURs read low. Read the `%var` column with that in
mind before chasing a low TUR.

### Provenance

The report opens with everything needed to reproduce the run: both `*IDN?`
strings, options, `CAL_DAYS?`, the zero age, `CAL_INTV`/`CAL_CONF`, warm-up, the
meter's `CALNUM`, the ambient **with its own timestamp** (and an explicit
`unknown` when there was none), the decision rule as applied, the run's start
and duration, the caldmm version and git revision.

> `ACAL age` on a 3458A is **always `unknown`**. `hp3458a.yaml` declares
> `interval_basis: since_acal` and requires an ACAL within 24 h for every
> interval but 24 h — but no 3458A query returns the age (`CALNUM?` counts
> calibrations, `CAL? n` returns constants). Run an ACAL before the session and
> note it by hand; the field cannot yet be filled in honestly.

### Comparing two runs

```bash
caldmm-cal-analyze after.csv --compare before.csv
```

Points are matched on identity — quantity, setpoint, frequency, connection,
range — never on the index, which a resume renumbers. You get Δerror,
Δ(error/TL), verdict transitions, and unmatched points on both sides listed
rather than dropped.

Every comparison also writes a machine-readable **`<after>-diff.csv`** — one
row per point, `status` of `both`/`removed`/`added`, both runs' errors and
error/TL ratios side by side with `repr` precision — so a spreadsheet can
check the arithmetic.

An **as-left run that names its counterpart compares itself**: when the loaded
header carries `run_kind=as-left` and a `counterpart`, the analyzer loads the
as-found file (as recorded, or by basename next to the CSV) without being
asked. An explicit `--compare` always wins, and a counterpart that is named
but missing is a warning, never a crash. Only the as-left direction
auto-compares — swapping an as-found run's counterpart in as "before" would
invert every delta.

### As-found / as-left: recording a calibration cycle

An adjustment overwrites the only measurement of how far the instrument had
drifted, so the cycle is three artefacts: the **as-found** verification, the
**adjustment session record**, and the **as-left** verification. The runner
links them at the header level:

```bash
# 1. as-found: how far had it drifted?
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter \
    --run-kind as-found -o 3458a-asfound.csv

# 2. the adjustment (its session record lands in 3458a-adjust-<stamp>.log)
caldmm-cal-verify --adjust --meter hp3458a:22 --as-found 3458a-asfound.csv ...

# 3. as-left: the state it was released in, naming its pair
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter \
    --run-kind as-left --counterpart 3458a-asfound.csv \
    --adjust-log 3458a-adjust-20260802T143000.log -o 3458a-asleft.csv
```

`--run-kind` is recorded in the result header (`standalone` when absent);
`--counterpart` and `--adjust-log` require it — a `standalone` header naming
a counterpart would be a record that contradicts itself, so the pairing flags
without a kind are refused. (`--adjust-log` here *names* an existing record;
`--adjust-record` is the different flag that tells an `--adjust` run where to
*write* one.) A resume must keep the same `--run-kind` — the printed resume
command carries it. The as-left run's automatic end-of-run report then
includes the before/after comparison against its counterpart.

### The calibration-cycle PDF

```bash
caldmm-cal-analyze --adjust-log 3458a-adjust-20260802T143000.log \
    [--as-found 3458a-asfound.csv] [--as-left 3458a-asleft.csv]
```

Renders the adjustment session record — verbatim — plus, when both
verification runs are given, the before/after comparison table, the
comparison plot and the diff CSV, into `<log stem>-report.md` and
`<log stem>.pdf` next to the log. An as-left file that names its counterpart
lets you drop `--as-found`. **A log alone yields a text-only report that says
so** — the analyzer does not invent plots out of a file with no measurements.

### Instrument temperatures

Schema v2 records the full thermal picture:

- **per point**: ambient and RH (from `--ambient` or your own probe) plus
  `Meter_Temp_C` — the meter's own internal sensor (`TEMP?` on a 3458A),
  read once per point after the reading. Meters without a sensor record
  nothing; a failed read records nothing and never costs the point.
- **per run**: `source_cal_temp_c` in the header — the calibrator's
  `CAL_TEMP?`, the ambient its stored constants assume (it has no live
  sensor). The bench-ambient-minus-`CAL_TEMP` delta is what drives its
  tempco.
- the report gains a temperature-over-run plot (ambient, RH on a twin axis,
  meter internal temperature, the source's `CAL_TEMP?` as a reference line)
  and the corresponding provenance lines.

Schema v2 also stores each reading's surviving samples (`Samples`, a JSON
list at `repr` precision, sentinels already excluded and counted), so the
Type A term — and therefore the whole budget — is recomputable from the raw
data in the file alone. Files written before v2 load fine; their samples read
back empty, which means "not recorded", not "no samples".

---

## Artifact calibration of the 5700A

`caldmm-5700a-selfcal --artifact` adjusts the calibrator's **main output**
against three external standards. Budget an hour.

```bash
caldmm-5700a-selfcal --zeros                    # CAL_ZERO, ~2½ min, commits
caldmm-5700a-selfcal --check                    # CAL_CHK, changes no constant
caldmm-5700a-selfcal --artifact                 # artifact cal, DRY RUN
caldmm-5700a-selfcal --artifact --store         # ... and commit
caldmm-5700a-selfcal --report CAL               # capture CAL_RPT?, reads only
caldmm-5700a-selfcal --compare a.txt b.txt      # diff two captures, offline
```

### What each standard is for, and the order

Four `CAL_REF` steps, in a **mandatory order the instrument enforces**. Out of
order it answers fault 852 and the whole sequence restarts from `CAL_TEMP` — so
the app refuses an out-of-order step before it reaches the wire.

| step | standard | what it sets |
|---|---|---|
| 1 | **+10 V** — a 732A/732B, HI→HI, LO→LO | the dc voltage scale |
| 2 | **−10 V** — the *same* reference with the **leads reversed** | dc linearity/polarity symmetry; the certified value is unchanged and the procedure sends its negative |
| 3 | **10 kΩ** — a 742A-10k, 4-wire across OUTPUT and SENSE | the resistance scale |
| 4 | **1 Ω** — a 742A-1, 4-wire on its own potential terminals | the low-resistance end; lead resistance matters, so use the standard's own potential terminals |

Then `CAL_ADJ` computes the new constants from all four.

**You are asked for each standard's certified value *and* its uncertainty**,
from that artefact's own certificate. The uncertainty is not optional — it
propagates into everything the calibrator sources until its next calibration,
and a certificate that does not state it is not defensible.

### Always dry-run first — but know what a dry run does

```console
$ caldmm-5700a-selfcal --artifact --ambient 23.5 --operator "A. Operator" \
      --notes "annual artifact cal, bench 1" -o /var/cal/dry

--- Fluke 5700A/5720A artifact calibration (main output) ---------------------
  *IDN?              FLUKE,5720A,5248000,1.0+B+*
  model / serial     5720A / 5248000
  *OPT?              5725A Attached
  procedure          artifact-cal
  mode               DRY RUN -- CAL_TEMP, the CAL_REFs and CAL_ADJ really run; CAL_STORE is held back
  session log        /var/cal/dry.log

── before you start ──
  Three external standards are needed, connected one at a time, in this order:

    1. +10 V   10 V dc reference to OUTPUT HI/LO (standard HI → HI, LO → LO)
    2. −10 V   the SAME reference with the LEADS REVERSED (HI → LO, LO → HI)
    3. 10 kΩ   10 kΩ standard, 4-wire across OUTPUT and SENSE
    4.  1 Ω    1 Ω standard, 4-wire on its own potential terminals

  then CAL_ADJ computes the new constants from all four.

  The order is mandatory (guide §14.6). Out of order the calibrator answers fault 852
  and the whole sequence has to restart from CAL_TEMP -- so this program refuses
  an out-of-order step before it reaches the wire.

── external standard: 10 V dc reference ──
  10 V dc reference (nominal 10 V), e.g. Fluke 732A/732B at ±1.5 ppm
  Enter the values from this artefact's own calibration certificate.
  The uncertainty is not optional: it propagates into everything the
  calibrator sources until the next calibration.
  → certified value of the 10 V dc reference [V, e.g. 10]: 10.00013
  → expanded uncertainty (k=2), WITH a unit [e.g. '1.5 ppm' or '15 uV']: 1.5 ppm
  → standard serial / certificate ref (optional): SN-TEST
```

Then, step by step:

```
── step 4/10: ref-10v-pos ──
  Connect the 10 V dc reference to the calibrator's OUTPUT terminals:
    standard HI → OUTPUT HI, standard LO → OUTPUT LO.
  Step 1 of 4 of the mandatory order (+10 V, −10 V, 10 kΩ, 1 Ω).
  certified: 10 V dc reference: 10.00013 V (+13.0 ppm from nominal) ± 1.5e-05 V (1.50 ppm, k=2), s/n SN-TEST
  the output will be at 10.00013 V (the standard, not a source setting)
  sends: CAL_REF 1.0000130000E+01 V;*OPC
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step ref-10v-pos? [Y = run it / anything else = skip it] y

  … ref-10v-pos: state 0 Idle

── step 5/10: ref-10v-neg ──
  REVERSE THE LEADS on the same 10 V reference:
    standard HI → OUTPUT LO, standard LO → OUTPUT HI.
  The certified value is unchanged; the procedure sends its negative.
  Step 2 of 4.
  certified: 10 V dc reference: 10.00013 V (+13.0 ppm from nominal) ± 1.5e-05 V (1.50 ppm, k=2), s/n SN-TEST
  the output will be at -10.00013 V (the standard, not a source setting)
  sends: CAL_REF -1.0000130000E+01 V;*OPC
```

and it ends:

```
── step 10/10: store ──
  SKIPPED — dry run. This step would have asked:
    GO TO THE BACK OF THE INSTRUMENT and set the CALIBRATION switch to ENABLE.
  There is no remote query for that switch (guide §7.6, gap G29). If it is
  left in NORMAL the calibrator answers fault 845 and stores nothing.
  This is the only irreversible step: it overwrites the calibration constants
  in non-volatile memory. The previous ones are not recoverable.
  would send (NOT sent — dry run): CAL_STORE;*WAI

** DRY RUN COMPLETE: every step ran except the ones that write non-volatile memory.
   NOT sent: CAL_STORE;*WAI
!! THE CALIBRATOR IS NOW RUNNING ON UNCOMMITTED CONSTANTS.
   CAL_ADJ really ran and really computed new calibration constants.
   They live in VOLATILE memory only: nothing was stored, and they will be
   LOST at power-off, reverting to the constants that are still in NVRAM.
   Until then this calibrator is sourcing from numbers that are in no record.
   POWER-CYCLE it before using it for anything else, or re-run with --store.

[exit status 0]
```

> **A dry run is not a simulation.** `CAL_TEMP`, all four `CAL_REF` steps and
> `CAL_ADJ` **really execute**. Only `CAL_STORE` is held back. The calibrator
> ends the run sourcing from **new, uncommitted constants that are in no
> record**, and stays that way until it is powered off. That is deliberate —
> guide §14.6 says omitting `CAL_STORE` collects performance data without
> committing constants, which is how you find out what a calibration *would*
> do. But **power-cycle the instrument before using it for anything else**, or
> re-run with `--store`.

### Committing

```console
$ caldmm-5700a-selfcal --artifact --store --ambient 23.5 --operator "A. Operator" -o /var/cal/commit

── step 10/11: store ──
  GO TO THE BACK OF THE INSTRUMENT and set the CALIBRATION switch to ENABLE.
  There is no remote query for that switch (guide §7.6, gap G29). If it is
  left in NORMAL the calibrator answers fault 845 and stores nothing.
  This is the only irreversible step: it overwrites the calibration constants
  in non-volatile memory. The previous ones are not recoverable.
  sends: CAL_STORE;*WAI
  THIS STEP WRITES NON-VOLATILE CALIBRATION MEMORY.
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step store? [Y = run it / anything else = skip it] y
  → Override and accept committing the new calibration constants to the calibrator's NON-VOLATILE memory (CAL_STORE), overwriting the existing ones irreversibly? [type "yes" to commit / anything else = SKIP this step permanently] yes

── step 11/11: switch-normal ──
  Set the rear CALIBRATION switch back to NORMAL.
  Left in ENABLE, any later CAL_STORE, CAL_RNG STORE or CAL_WBGAIN STORE
  can overwrite the calibration constants with no further protection.
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step switch-normal? [Y = run it / anything else = skip it] y

** COMMITTED: new calibration constants are in the calibrator's non-volatile memory.
   The previous constants are gone and are not recoverable.
   Verify the result before quoting it: caldmm-cal-verify --source fluke5700a:<addr> ...

[exit status 0]
```

**The rear-panel CALIBRATION switch.** There is no remote query for its
position (guide §7.6, gap G29), so the *only* way to find out it was left in
NORMAL is the fault 845 the store attempt returns. The committing run therefore
has an explicit final step to put it back — leave it in ENABLE and any later
`CAL_STORE`, `CAL_RNG STORE` or `CAL_WBGAIN STORE` can overwrite the constants
with no further protection.

If it *was* left in NORMAL, the store is refused and you are offered a
**single-step retry** — you do not have to reconnect the artefacts or repeat
the four `CAL_REF` steps, because the constants `CAL_ADJ` computed are still
live in volatile memory:

```
── CAL_STORE can be retried on its own (1/3) ──
  The constants CAL_ADJ computed are still live in volatile memory, so CAL_STORE on its
  own commits exactly the calibration that was already performed -- the artefacts do
  not have to be re-connected and the four CAL_REF steps do not have to be re-run.
  That holds only until the calibrator is powered off.

  Set the rear-panel CALIBRATION switch to ENABLE before answering.
  → OK with retrying CAL_STORE now? [Y = retry the commit / anything else = give up] y
```

### The pre-flight is not overridable

`--force` overwrites existing output files. It does **not** override the
procedure's pre-flight, because those are the conditions the calibration's own
specifications assume:

```
--- Refused ------------------------------------------------------------------
  the calibrator is not in a state this procedure will start from, and
  NOTHING WAS SENT:
    - the last dc zeros calibration was 412 days ago, past the 30-day interval (fault 1303). Run the 'dc-zeros' procedure first — CAL_ZERO takes about 2½ minutes and removes 2.2 V-range offsets this calibration would otherwise bake into non-volatile memory
  --force does not override these: they are the conditions the calibration's
  own specifications assume. Fix the cause (warm it up, run --zeros, set the
  output to 0 V and press STBY) and start again.

[exit status 2]
```

The gates are: warm-up (guide §11.6 wants twice the off-time, up to 30 min),
the 30-day dc-zero interval, a live output at or above 22 V, and the model
check. Stale zeros **block** a committing run and only warn on a dry run — a
verification produces a re-measurable number, but an artifact cal on stale dc
zeros writes 2.2 V-range offsets into NVRAM permanently.

### The before/after diff

Every mode captures `CAL_RPT?` before and after and writes a diff:

```
--- Report diff (before -> after CAL_ADJ, before any commit) -----------------
before printed 2026-01-02 03:04:05
after  printed 2026-01-02 03:04:05
2 points compared: 2 moved closer to nominal, 0 further away, 0 unchanged

  function           range point       before %   after %    change
  DC VOLTAGE        220 mV +FS           -18.88     -2.10    +16.78
  DC VOLTAGE         2.2 V +FS            10.44      1.02     -9.42

--- Record -------------------------------------------------------------------
  /var/cal/commit.log
  /var/cal/commit-before.txt
  /var/cal/commit-after.txt
  /var/cal/commit-diff.csv
```

The column is the shift **as a percentage of that range's specification**: a
point at 90 % still passes and is about to stop passing, which is the signal a
calibration is run to find. The headline counts closer / further / unchanged
separately, so a diff that moved only one function reads as such.

### Exit status, and the state you must not misread

| status | meaning |
|---|---|
| `0` | the sequence ran (dry run complete, or committed) |
| `1` | it did not run and **nothing was committed** |
| `2` | usage error or a pre-flight refusal — nothing was sent |
| `3` | interrupted |
| `4` | **the non-volatile state is UNKNOWN** |
| `5` | the session log is not where it was asked to be |

**Exit 4 is neither success nor failure.** `CAL_STORE;*WAI` reached the
instrument and the outcome could not be read back, so the calibrator must be
treated as *possibly adjusted* until somebody checks by hand:

```
!! THE STATE OF NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN.
   CAL_STORE;*WAI reached the instrument and the outcome could NOT be read back.
   This run is NEITHER a success NOR a failure. The calibrator must be treated
   as POSSIBLY ADJUSTED until somebody checks, by hand:

       CAL_DATE? CAL     the date of the stored main-output calibration
       CAL_RPT?  CAL     the stored report
```

No `CAL_STORE` retry is offered in that state. The right next action when
nobody can account for one irreversible command is to read `CAL_DATE?`, not to
send another one on top of it.

### The record

`-o BASENAME` writes `BASENAME.log`, `-before.txt`, `-after.txt` and
`-diff.csv`. Without it the basename defaults to
`5700a-<procedure>-<timestamp>`. The log carries the command line, both
reports, the step log, **the operator transcript verbatim** (every prompt and
every answer), and the external standards as entered — because every output the
calibrator sources until its next calibration inherits their uncertainties.

The record is written the instant a result exists and before anything else that
could itself fail, including on the paths where a Ctrl-C escaped the runner.
The app refuses by default to overwrite an existing record: those files are the
only account of a calibration that cannot be repeated to produce them.

`--operator` and `--notes` go into it. Use them.

---

## Adjusting a 3458A (`--adjust`, artefacts by hand)

`caldmm-cal-verify --adjust` runs the **DUT's own external adjustment** —
`CAL` on an HP 3458A, chapter 3 of the Calibration Manual — against certified
artefacts you connect by hand. It is a different kind of run from a
verification: no plan, no points, no result CSV, no calibrator. Thirteen
steps, five of which write non-volatile memory — four `CAL`s, irreversibly,
and the closing `ACAL AC`.

```bash
# rehearse the whole sequence.  DRY RUN: no CAL is sent
caldmm-cal-verify --meter hp3458a:dmm1:22 --adjust --ambient 22.5 \
    --operator "A. Operator" --notes "annual external adjustment" \
    --adjust-record runs/3458a-2026-08-02

# ... and write calibration memory, irreversibly
caldmm-cal-verify --meter hp3458a:dmm1:22 --adjust --adjust-commit \
    --ambient 22.5 --operator "A. Operator" \
    --adjust-record runs/3458a-2026-08-02-commit
```

**`CAL` is not `ACAL`, and the difference is the whole point.** `ACAL` is
auto-calibration: the meter measuring its own internal 7 V and 40 kΩ
references and recomputing about 197 of its 253 calibration constants from
them. It needs no external equipment, is run daily, and **cannot make the
meter more accurate than those internal references already are**. `CAL`
retrains those references against a traceable standard physically on the
terminals. It is the only one of the two that moves absolute accuracy, and the
only one that needs artefacts. The procedure is named for the distinction:
`HP 3458A external adjustment (CAL — not ACAL)`.

**An adjustment and a verification are refused in one invocation.** Verifying
a meter against the reference that was just used to adjust it measures the
reference against itself. `--plan`, `--csv`, `--quantities`, `--samples`,
`--interval` and the rest of the verification flags are therefore *refused*
under `--adjust` rather than ignored, as are `--source` (nothing is sourced),
`--yes` (every prompt asks you to move a short, a lead or the Terminals
switch), `--force` (it overrides a calibrator interval, and no calibrator is
opened) and `--dry-run` (see below — `--adjust` is already one, and it is not
a simulation). The independent verification command is printed at the end of
the run.

### What each standard is for, and the order

Four adjustments and a closing `ACAL AC`, in a **mandatory order enforced
before the wire**:

| step | what is on the terminals | what it writes |
|---|---|---|
| 1 `offset-front` | four-terminal low-thermal short, FRONT, settled 5 min | `CAL 0` — DCV/DCI/OHM/OHMF offsets for the FRONT Terminals position, ~5 min |
| 2 `offset-rear` | the same short moved to REAR | the same offsets for the REAR position, ~5 min |
| 3 `dc-gain` | **10 V dc** — a 732A/732B on the FRONT Input HI/LO | `CAL <certified>` — the dc gain *and* the stored value of the internal 7 V reference, ~2 min |
| 4 `resistance` | **10 kΩ** — a 742A-10k, 4-wire across Input and Sense | `CAL <certified>` — the resistance **and dc current** gains, ~12 min |
| 5 `acal-ac` | nothing connected | `ACAL AC`, ch.3 p.45 step 7, ~2 min 45 s |

Chapter 3's introduction says "You must perform the adjustments in the order
presented in this chapter", and p.43 states the dependency outright: the
resistance adjustment "relies on the values calculated by the DC Gain
Adjustment". **Unlike the 5700A there is no out-of-order fault to catch a
mistake afterwards** — a 3458A asked to adjust resistance without a fresh dc
gain simply computes and stores constants from stale gains. So the procedure
refuses the step itself. Decline one adjustment and everything downstream of
it stops:

```
    offset-front: declined [CAL 0]
    short-rear: ok [DCV 1.00E-01; TARM SGL,1]
    offset-rear: FAILED [CAL 0] -- StepOrderError: refusing to send offset-rear: offset-front did not complete. Chapter 3 of the Calibration Manual states 'You must perform the adjustments in the order presented in this chapter' [...] Nothing was sent.
  NOT committed — would have sent:
    CAL 0
  never reached: standard-10v, dc-gain, standard-10kohm, resistance, acal-ac, settle, capture-after
```

Every irreversible step is preceded by a **non-committing setup step that
really runs**: it selects the function and range, applies `OCOMP ON` and the
`DELAY` the manual asks for, and takes a reading through the wiring you have
just made. A four-terminal short that reads over **1 mV** on the 100 mV range,
or a standard more than **1 %** from its certified value, stops the run before
the `CAL` — those are wiring or typing mistakes, not a meter out of
calibration, and `CAL` believes whatever it is told. (Both thresholds are this
project's gross-error gates, not Calibration Manual figures. The manual's own
legal `CAL` arguments are only `CAL 0`, `CAL 1..12` V and `CAL 1e3..12e3` Ω —
which is exactly why `1.0000` typed for a 10 V standard would otherwise be
accepted.)

**You are asked for each standard's certified value *and* its uncertainty**,
from that artefact's own certificate. Chapter 3's Required Equipment list is a
Fluke 732A/732B and a 742A-10k; the procedure carries their ±1.5 ppm and
±4 ppm as the reference points, and an entered uncertainty worse than that
warns — the meter's accuracy after the adjustment inherits it. The 10 kΩ
standard is entered **in ohms** (`10000.021`), because that is what `CAL`
takes; a certificate written in kΩ is accepted at the prompt as
`10.000021 kohm`.

Two things chapter 3 asks for that this procedure deliberately does **not**
do, printed in the safety brief and again in the record:

- the **AC Adjustment** (ch.3 item 5, the `SCAL` commands) — it needs a
  synthesizer and three thermal converters, it is a manual characterization
  loop, and the driver has no `SCAL` method. The manual requires it only once
  every 2 years or after a repair;
- the `Perform an ACAL OHMS` line that appears on the paper Adjustment Record
  form (p.51) but nowhere in ch.3's numbered text. That is ambiguous, so
  nothing is sent — run it by hand first if your lab's practice requires it.

### The security code, and jumper J132

`CAL` is gated by **two** things and **neither can be queried remotely**.

The first is the security code set with `SECURE`. It is asked for at the
prompt and never taken from the command line, because an argument vector is
visible to every process on the machine:

```
── calibration security code ──
  CAL is gated by the code set with SECURE (Calibration Manual p.21).
  The factory value is documented inconsistently — 3458 in the Calibration
  Manual, 3458A in the User's Guide — so nothing is assumed here. Leave it
  blank if security is disabled (code 0).

  It is asked for here and never taken from the command line: an argument
  vector is visible to every process on the machine. It is sent on the wire
  and stored nowhere — not in the record, not in the step log, not in any
  exception. NOTE: this prompt does echo what you type.
  → calibration security code (blank if disabled): 3458
```

There are two ways for it to be wrong, and they fail in different places. A
**malformed** one is caught locally: it must be alphanumeric, optionally with
a leading `-`, and anything else is rejected at the prompt and asked for again
(five attempts, then the run gives up) — the 3458A accepts several commands on
one line, so a code carrying whitespace, a comma or a semicolon could append a
second command to an irreversible write. The offending value is never echoed
back. The same check runs again before every write, because a context can be
built by hand.

An **incorrect** one can only be discovered by the meter, and there is no
query to ask it with. See below.

The code is held **on the run's context and nowhere else** — not on the
registered procedure object, which is shared and stateless, not in a declared
command, not in any `StepResult`. What goes on the wire is `CAL 0,3458`; what
the record contains is `CAL 0`. A test asserts it appears in no rendered
record.

The second gate is **internal jumper J132** on the 03458-66505 (or -66515,
option 001) assembly. **The 3458A has no rear-panel calibration switch** —
its rear panel carries GPIB, Ext Trig, Ext Out, the line-voltage selector and
the ground lug, and nothing else. Do not go looking for one. With J132 in the
**right-hand** position (instrument front facing you) *neither* `CAL` nor
`ACAL` runs even with the correct security code, and clearing that means
**powering the meter down and taking the top cover off** to move J132 to the
left.

Neither gate answers a query, so **the only way either is discovered is a
refused `CAL`** — and what the meter reports when it refuses is undocumented:
the Calibration Manual says J132 blocks `CAL` but never says what comes back,
and it says nothing at all about a wrong security code. So the driver does not
infer. It checks `CALNUM?`, the calibration counter, which a refused `CAL`
cannot advance, and it names *both* gates whichever error bit came back —
because an operator who is locked out needs to hear about the jumper either
way:

```
    offset-front: FAILED - NVRAM STATE UNKNOWN [CAL 0] -- CalibrationLockoutError: CAL 0 (offset) reported ERR?=1 [hardware (see AUXERR?)], AUXERR?=16384 [cal RAM write/protection failure]. Calibration RAM is write-protected, so nothing was stored. Calibration access on the 3458A is gated by two things, and NEITHER can be queried remotely: (1) the security code set with SECURE [...] and (2) internal jumper J132 on the 03458-66505 (-66515 for option 001) assembly. With J132 in the RIGHT position neither CAL nor ACAL will run even with the correct security code, and clearing it means powering the meter down and removing the top cover to move J132 to the LEFT position (instrument front facing you). The 3458A has no rear-panel calibration switch. See Calibration Manual p.21-23.
```

A lockout exits **4**, not 1 — see the exit-status table.

### Always dry-run first — but know what a dry run does

`--adjust` **alone is the dry run**; `--adjust-commit` is the second, explicit
flag. The header says which one you are in:

```
--- Adjustment ---------------------------------------------------------------
  procedure      HP 3458A external adjustment (CAL — not ACAL)
  meter          hp3458a  dmm1:22
  mode           dry run (the default) -- everything but the CAL commands really runs
  session record runs/3458a-2026-08-02.log
  Nothing will be written to calibration memory.  Pass --adjust-commit to write.
  ID?            HP3458A
```

A dry run is **not a simulation**. It opens the meter, reads `CALNUM?`,
`CALSTR?` and `TEMP?`, walks the whole sequence, and *really runs* every
non-committing step — `DCV 1.00E-01`, `OCOMP ON`, `OHMF`, both `DELAY`
settings of the p.44 settling test, and a reading at each one. Only the four
`CAL`s and the `ACAL AC` are held back, and each is printed with the exact
text that would have been sent:

```
── step 8/13: dc-gain ──
  SKIPPED — dry run. This step would have asked:
    Running the dc gain adjustment. About 2 minutes.
  Calibration Manual p.42, step 4: 'Execute the CAL command specifying the
  exact output voltage of the standard.' The meter measures the standard on
  its 10 V range, adjusts its gain to agree, then measures and stores its own
  internal 7 V reference against it. This also performs the equivalent of an
  ACAL DCV (p.88).

  the value sent is the CERTIFIED value: 10.000013 V
  would send (NOT sent — dry run): CAL 10.000013
  this step writes non-volatile memory and is being SKIPPED because this is a dry run
  dry run: CAL 10.000013 was NOT sent, so the dc gain constant and the stored value of the internal 7 V reference are unchanged.
```

That is what makes the rehearsal worth an hour: you find the lead in the wrong
jack, the decade error in a typed certificate and the short that was never
fitted *before* the command that cannot be undone. Without `--adjust-commit`
the driver is opened without `allow_calibration_write`, so the procedure's
refusal and the driver's refusal are **two independent locks** and neither
relies on the other.

> **A dry run still leaves the meter reconfigured.** Nothing was written to
> calibration memory, but the setup steps really did change the function,
> range, `OCOMP` and `DELAY`, and this procedure does not put them back. The
> run says so — and a committing run prints the same sentence, because it is
> just as true there:

```
  warning: dry run: no CAL was sent and calibration memory is untouched — but the setup steps really did change the meter's function, range, OCOMP and DELAY, and this procedure does not put them back. Reconfigure it (or PRESET NORM) before using it to measure anything.
```

The security-code prompt appears on a dry run too. Nothing is transmitted with
it, because no `CAL` is sent.

### Committing

`--adjust-commit` sends them. Every write still needs **its own typed
confirmation** — `y` at the step prompt is not enough:

```
── step 4/13: offset-front ──
  Running CAL 0 — the FRONT terminal offset adjustment. About 5 minutes.
  Calibration Manual p.40: 'Execute the CAL 0 command. The multimeter
  automatically performs the front terminal offset adjustment.' It stores
  offset constants for DCV, DCI, OHM and OHMF for THIS Terminals switch position.
  Do not touch the short or the terminals while it runs.
  (If a security code is set it is appended on the wire; it is not recorded.)
  sends: CAL 0
  THIS STEP WRITES NON-VOLATILE CALIBRATION MEMORY.
  → press Enter when the action above is complete (Ctrl-C to abort): 
  → OK with step offset-front? [Y = run it / anything else = skip it] y
  → Override and accept writing the FRONT terminal offset constants to the 3458A's NON-VOLATILE calibration memory (CAL 0), overwriting the existing ones irreversibly? [type "yes" to commit / anything else = SKIP this step permanently] yes
```

Anything other than `yes` **skips that step permanently** — there is no second
chance later in the run, and the order guard then stops everything that
depended on it. `CALNUM?` is captured before and after; on a committing run a
counter that did not advance is warned about loudly, because it is the only
independent evidence that calibration memory really changed. It ends:

```
--- Calibration memory -------------------------------------------------------
  ** COMMITTED: new calibration constants are in the 3458A's non-volatile
     memory.  The previous ones are gone and are not recoverable.

  Verify it INDEPENDENTLY before quoting it -- against a calibrator, not
  against the artefacts it was just adjusted with:
    caldmm-cal-verify --source fluke5700a:<addr> --meter hp3458a:dmm1:22 --dut meter
  The session record is runs/3458a-2026-08-02-commit.log

[exit status 0]
```

The last two steps are not decorative: `ACAL AC` (ch.3 p.45 step 7,
disconnect everything first) and a 15-minute stand for the relays to
thermally stabilize. Readings taken before that are not of the meter you just
adjusted.

### Ctrl-C during a `CAL` looks like a hang. It is not.

> **Ctrl-C is deferred, and the resistance adjustment takes 12 minutes.**
> There is no documented remote abort for a 3458A `CAL`. The runner installs a
> handler that sets a flag and stops at the **end of the current step**, so a
> single line appears and then *nothing happens* for the rest of the
> adjustment — up to **12 minutes** on `resistance`, 5 on each offset, 2 on dc
> gain, 2 min 45 s on `ACAL AC`. The meter is working, not hung.

That one line is:

```
  Ctrl-C noted: stopping at the end of this step.
  The command in flight cannot be aborted remotely. Let it finish; do NOT cycle power.
  A 3458A has no STBY key and no remote abort: let the command finish. It is written either
  way. Do NOT cut power or press Reset — Calibration Manual p.87: an interrupted autocal
  leaves the constants invalid and costs an ACAL ALL (16 min, plus a 30 min settle).
```

and a second Ctrl-C says the same rather than escalating:

```
  already stopping — the command in flight has no remote abort. Let it finish; do NOT cycle power.
```

(Until this pass the second message read "Use the front-panel STBY key or line
power" — the 5700A's truth, printed on every instrument. A 3458A has no STBY
key, and line power is the one thing p.87 forbids. The 5700A's own procedures
still say it, in their own step hints.)

**Do not reach for the power switch.** The `CAL` in flight completes and is
written either way, so killing power buys nothing — and a 3458A powered off or
interrupted during a calibration comes back with `ERR?` bit 10,
`1024,"CALIBRATION REQUIRED"`, its autocal constants invalid. The only fix is
a full `ACAL ALL`: **16 minutes**, plus 30 minutes of thermal settling
afterwards, and that one must not be interrupted either. Let the step finish.

Once it does, the run stops cleanly at the next step boundary, the record is
written, and the remaining steps are listed as `never reached`. What was
already committed stays committed — which is why an interrupted run that wrote
something exits `0` and not `3`.

### The pre-flight is not overridable

There is no `--force` for an adjustment. The pre-flight runs in two passes —
the instrument-only half **before** you are asked for two calibration
certificates, so a wrong instrument on the bus does not cost you the typing —
and nothing is sent when it refuses:

```
--- Refused ------------------------------------------------------------------
  the meter is not in a state this procedure will start from, and NOTHING
  WAS SENT:
    - ambient 33 °C is outside the 15-30 °C band the 3458A's adjustment is specified over (Calibration Manual ch.3, Preliminary Adjustment Procedure step 1). Constants computed outside it are not covered by any specification, and they are written irreversibly.

[exit status 2]
```

What **blocks**: an instrument whose `ID?` says it is not a 3458A, or that
does not implement the driver's calibration methods; a free-running meter
(`TARM AUTO` — its streaming readings would be parsed as the `ERR?` response
after a `CAL`); an ambient outside **15–30 °C**, which is tighter than the
5700A's 15–35 °C band; a missing certified value for either standard; a
malformed security code; and a committing run whose meter was opened without
the write opt-in.

What only **warns**: an unknown or stale (> 24 h) time since the last `ACAL`,
and an entered uncertainty worse than the artefact chapter 3 specifies.
Chapter 3 does not require a recent ACAL before adjusting — but every 3458A
accuracy column except 24-hour assumes one within 24 h and ±1 °C, so the trial
readings the setup steps take are only as good as the ACAL behind them.

Two conditions **nothing can check for you**: the meter must have been powered
on for **4 hours** (there is no `ONTIME?` on a 3458A), and the shorts and
standards need their settling time. The safety brief asks you to confirm the
first yourself.

## A model with no procedure at all

Exit 2, before anything is opened. The shipped meter, the 3458A, registers an
`external-adjust` procedure (artefacts by hand, or `--source` optionally), so
the refusal below is what a model added without one gets:

```console
$ caldmm-cal-verify --meter someothermeter:14 --adjust
error: no procedure 'external-adjust' for model 'someothermeter'; no procedures are registered for that model
```

## `--adjust` exit status, and the state you must not misread

Common to every meter's procedure.

| status | meaning |
|---|---|
| `0` | the sequence ran (dry run complete, or something was committed) |
| `1` | nothing was committed and the sequence did not finish — a step failed, or a typed confirmation was declined |
| `2` | usage error, a pre-flight refusal, or a procedure that could not start (unimplemented, unregistered, or an entry it would not accept) — no `CAL` was sent |
| `3` | interrupted, with nothing written |
| `4` | **the non-volatile state is UNKNOWN** |
| `5` | the session record is not where it was asked to be |

The order of those branches is the design, not an implementation detail. The
answer about calibration memory is decided **first**: unknown NVRAM outranks
everything, a confirmed write outranks an interrupt (the constants are stored
whatever else went wrong, and reporting 3 would invite a pointless redo), and
only then does an interrupt outrank the plain failure. Nothing branches on
"did the run fail" — a declined commit has `failed is False` and stored
nothing, and must never read as a success. Exit `5` wins over all of them,
including `4`: an unknown NVRAM state can be resolved later with `CALNUM?`,
but the account of what was done disappears when the terminal is closed.

**Exit 4 is neither success nor failure, and it is the one you must act on.**
A `CAL` reached the meter and its outcome could not be read back — a GPIB
timeout on the `ERR?` that follows, a meter that stopped answering, a lockout.
The write may well have landed, and recording that as "not committed" would
make the audit trail state the opposite of what happened at the one
irreversible step in the procedure. So the meter is declared *possibly
adjusted* and you go and look:

```
--- Calibration memory -------------------------------------------------------
  !! THE STATE OF NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN.
     CAL 10000.021 reached the meter and the outcome could NOT be read back.
     This run is NEITHER a success NOR a failure.  Treat the meter as
     POSSIBLY ADJUSTED until somebody checks, by hand:

         CALNUM?   the number of times it has been adjusted
         CALSTR?   the stored calibration message

     It read 42 before this session.  A higher number means the
     adjustment went in; the same number means it did not.
     Do not use this meter as a reference until you know which it is.

[exit status 4]
```

`CALNUM?` before the run is captured for exactly this moment, and it is
printed here so you do not have to go and find the record first. **The right
next action is to read `CALNUM?`, not to send another `CAL` on top of it** —
and not to use the meter as a reference until you know which of the two
happened. If it did advance, the meter is adjusted but the rest of the
sequence did not run: everything after the failed step is listed as `never
reached`, and the whole run has to be repeated from step 1 once you know
where you are.

A **lockout lands on exit 4 too**, even though the driver's own message says
"nothing was stored": the classifier does not treat a driver's opinion as
proof that a command that went out did nothing. `CALNUM?` settles it either
way — and if it did not advance, fix the security code or J132 and start
again. One caveat when reading the log rather than the terminal: the
framework's generic warning inside the record names the *calibrator's* queries
(`CAL_DATE?` / `CAL_RPT?`). On a 3458A the two to read are the ones printed
above.

### The record

`--adjust-record BASE` writes `BASE.log`. It is optional, and forgetting it
does not lose the account of an irreversible operation: the basename then
defaults to `MODEL-adjust-<timestamp>`, e.g.
`hp3458a-adjust-20260802T143000.log`. The path is printed before the first
step, an existing file is **never** overwritten, and the path is *proved
writable* before the meter is opened — a typo'd or unmounted destination
passes "does not already exist" trivially and would otherwise be discovered at
the end of an hour, with the meter already adjusted. A missing parent
directory is created and **announced**, because creating it silently is
exactly what makes a typo succeed:

```
  note: created the directory 'runs' for the session record -- check that is where you meant it to go
```

The log carries the command line, the caldmm version and git revision, the
model, instrument, `ID?` and serial, `--operator` and `--notes`, the result
summary — every command that was sent, every command that was held back, the
standards as entered with their uncertainties, every warning — the step log,
and the "Not covered by this procedure" notes. It contains **no security
code**, by construction.

It is written the instant a result exists, before the summary is printed and
before anything else that could itself fail, and from every exception path
including the interrupt and the one where `finalize` itself broke. If it
cannot be written at all the run exits `5`, says so, and falls back to a
temporary file it tells you to copy somewhere permanent.

`--operator` and `--notes` go into it. Use them: this is the permanent record
of an operation that cannot be repeated to reproduce it.

---

## See also

- [`docs/5700a/fluke5700a-console-howto.md`](5700a/fluke5700a-console-howto.md) — triaging
  the calibrator itself, and reading a `CAL_RPT?` capture.
- The module docstrings under `src/caldmm/cal/` — start at `roles.py` (the
  seam between the engine and the instruments), then `uncertainty.py` (the
  GUM budget) and `verdict.py` (the decision rule) — record the design and
  its known limitations.
- [`docs/3458a/3458A_Programming_Guide.md`](3458a/3458A_Programming_Guide.md)
  — the 3458A command set behind
  [Adjusting a 3458A](#adjusting-a-3458a---adjust-artefacts-by-hand):
  `CAL`/`ACAL`/`CALNUM?`/`SECURE`, the `ERR?` bit weights, and the
  `1024,"CALIBRATION REQUIRED"` an interrupted calibration leaves behind.

# 3458A performance verification: the Calibration Manual vs. this toolkit

This note explains how `caldmm-cal-verify --dut meter` relates to Chapter 4, "Performance
Verification Tests", of the *Keysight 3458A Calibration Manual* (03458-90017, edition 11,
2023). It covers which of the manual's test points the toolkit runs, where its method
differs, and how the test-card limits compare with the data-sheet tolerances the verdicts
use. The points themselves live in `caldmm.cal.plan.MANUFACTURER_TESTS`, and
`tests/cal/test_manufacturer_tests.py` checks every number below against that table and
`hp3458a.yaml`.

## What a full run covers

`--points full` is the default. It generates two kinds of points:

* **The ladder:** every range of every function the 3458A and 5700A share, at 10/50/90/100 %
  of range, both polarities, with one tone per specification band.
* **Every row of Tables 4-1 to 4-6**, each labelled `[CM 4-x #n]`. Two supplementary points,
  labelled `[SUP …]`, cover the ranges Chapter 4 never touches: the 10 mV ac range in ANA, and
  the 1 GΩ range at 100 MΩ.

| Function | 3458A ranges | Points from the ladder | Points from the manual |
|---|---|---|---|
| DCV | 100 mV – 1000 V (5) | zero, ±10/50/90/100 % | Table 4-1 offsets, Table 4-2 gain |
| ACV, `SETACV SYNC` | 10 mV – 1000 V (6) | 10–100 % at 1 kHz, one tone per band | — |
| ACV, `SETACV ANA` | 10 mV – 1000 V (6) | — | Table 4-3 (the manual's own DUT configuration), and SUP 10 mV |
| OHMF (4-wire) | 10 Ω – 100 MΩ | the 5700A's resistor catalogue, and its short | Table 4-6 gain, 4-wire offset |
| OHM (2-wire) | 10 Ω – 1 GΩ | 100 MΩ (2-wire only on the 5700A) | 2-wire offset, and SUP 100 MΩ on the 1 GΩ range |
| DCI | 100 nA – 1 A (8) | zero, ±10/50/90/100 % | Table 4-4 offsets, Table 4-5 gain |
| ACI | 100 µA – 1 A (5) | 10–100 % at 1 kHz, one tone per band | — (Chapter 4 has no ac-current test) |

`caldmm-cal-analyze` prints a **Range coverage** table: every 3458A range with its verdict
counts. It also prints a **Manufacturer test card** table: every row of Tables 4-1 to 4-6,
with |error| against the card limits for both the standard unit and Option 002.

### Not covered

* **The frequency counter** (FREQUENCY TESTS card: 1 Hz and 10 MHz from a 3325A). The 5700A
  cannot make 1 Hz or 10 MHz, and its ±0.01 % frequency accuracy is no better than the
  3458A's own limits.
* **Rear-terminal offsets.** These need the leads moved and the front-panel Terminals switch
  thrown. The toolkit tests the front terminals only.
* **The 100 nA – 10 µA dc-current ranges** are measured, but Keysight states their figures
  only as "typical" (data sheet p.21, footnote 6). A typical figure is not a specification,
  so these points are always INDETERMINATE (`dut-typical-only`), whichever way the error
  goes. The 5700A's floor of about 7 nA would not support a verdict there anyway.
* **AC current above 5 kHz.** The data sheet marks those columns "typical performance"
  (p.33, footnote 3), so such points would be unspecified and none are planned.

## One re-cable

A plan's wiring groups are **terminal sets**, not functions:

1. **Voltage/ohms terminals: DCV, ACV, 4-wire and 2-wire ohms.** Wire this once:
   * 5700A OUTPUT HI/LO to the 3458A HI/LO inputs;
   * 5700A SENSE HI/LO to the 3458A Ω SENSE HI/LO inputs;
   * GUARD to GUARD.

   The calibrator switches EXTSENSE itself: on for 4-wire ohms, off otherwise. The 3458A's
   sense inputs are simply unused by the other functions.
2. **Current terminals: DCI, ACI.** Move the lead on the 3458A HI input to its **I** input.
   LO, guard and the sense leads stay where they are.

That re-cable is the only manual step. At the group boundary the engine puts the source in
standby, prompts, and then proves the new wiring at a safe level before measuring.

## Where the method differs from the manual's

| Manual | Here | Why |
|---|---|---|
| Transfer-standard method: a 90-day 3458A Opt 002 measures the source, then the UUT measures it | Direct: the 5700A's own specified uncertainty (`UNCERT?`) is the reference | No second 3458A is assumed. The budget carries the 5700A's uncertainty, and every point has a TUR and a guard band |
| Offsets against a low-thermal copper **short** (DCV, ohms) or an **open** input (DCI) | The 5700A's 0 V, 0 Ω short or 0 A output, through the test leads | Keeps the run unattended. Lead thermal EMF and 2-wire lead resistance show up as offset; pass `--thermal-emf` to budget the EMF |
| `MATH NULL` and a 5-minute dwell per dc range | No null, and the range re-selected per point | The data sheet's "no MATH NULL, fixed range < 4 min" adder is applied instead (p.12 footnote 5). See below |
| `ACAL` per function, then 15–30 min settling | `--acal`: one `ACAL ALL` and 30 min of settling (`--acal-settle`) before the first point, with the calibrator in standby | Same result, one unattended wait |
| TEMP? within 5 °C of `CAL? 59` (DCV) and `CAL? 60` (ohms, DCI) | Read at start, and a run outside the window is refused. Each row's own `TEMP?` is then judged against that window (±1 °C for `--interval 24h`), and against the ACAL temperature ±1 °C for ac. A row outside is INDETERMINATE, including under `--force` | The manual's precondition (p.56–57), and the data sheet's Tcal / ACAL windows |
| ACAL within 24 h and ±1 °C before every test | `--acal` runs one, and each row is checked against its time and temperature. `--acal-recent` states one was run. With neither, every row is INDETERMINATE | The data sheet's figures hold only "within 24 hours and ±1 °C of last ACAL" |
| 2-wire offset against a short at the terminals | The 5700A's short through the test leads. `--lead-resistance OHMS` corrects for the leads, with a 20 % uncertainty term. Without it, the short is INDETERMINATE | The leads add tens of milliohms against a 250 mΩ limit |
| ACV DUT config: `SETACV ANA`, `ACBAND 10,2E6`, `LFILTER ON` | ANA points: `ACBAND 10,2E6`, `NPLC 20` | NPLC > 10 makes the p.29 low-frequency adder zero at ACBAND low 10 Hz |

## Test-card limits vs. data-sheet tolerances

The verdict is judged against the **data sheet** (`hp3458a.yaml`, audited cell by cell
against `docs/3458a/3458a-specifications.md`). The card's 1-year limits are Keysight's
acceptance limits for its transfer-standard procedure. They are not the data-sheet
figures, and in places they are tighter. "Ratio" below is data-sheet tolerance ÷ card limit
(standard unit). Above 1 means a unit can PASS here and still exceed the card, which is why
the report prints the card columns beside the verdict.

| Test | Point | Card (Std) | Data sheet, 1 year | Ratio | Why |
|---|---|---:|---:|---:|---|
| 4-1 #1–#3 | DCV zero, 100 mV–10 V ranges | 1.06 / 1.06 / 2.3 µV | 2.0 / 2.0 / 3.0 µV | 1.3–1.9 | no-null/short-dwell adder (17, 1.7, 0.25 ppm of range) |
| 4-1 #4–#5 | DCV zero, 100 V / 1000 V ranges | 36 / 100 µV | 30 / 100 µV | 0.83–1.00 | |
| 4-2 #1 | 100 mV | 2.12 µV | 2.90 µV | 1.37 | the same adder |
| 4-2 #2–#8 | 1 V – 1000 V | | | 0.92–1.00 | |
| 4-3 #1–#10 | ACV, ANA | 25 µV … 0.544 V | 30 µV … 0.62 V | **1.10–1.27** | the card is tighter than the current data sheet's ANA table at every point |
| 4-4, 4-5 | DCI offsets and gain | | | 0.77–0.89 | |
| ohms offsets, 4-6 | OHM / OHMF | | | 0.71–1.00 | |

The card's Option 002 column is tighter on the dc-volts gain rows (4-2). The data sheet's
Option 002 dc-volts figures are carried too, in the `options: "002"` block of
`hp3458a.yaml`. **Pass `--meter-option 002` for an Option 002 unit.** Without it, the unit is
judged at the standard figures, which are *looser*: the error allowed for 10 V on the 100 V
range is 130 µV instead of 90 µV, so the verdict is too lenient.

## AC test points are capped by what the 5700A can source

Every ac-volts point is checked against the 5700A Series II limits before it is planned,
using the *Extended Specifications* AC voltage tables and Figure A (Volt-Hertz capability):

* volt × hertz ≤ 2.2 × 10⁷;
* the 1100 V range covers 15 Hz – 1 kHz only, and at most 250 V from 15 to 50 Hz;
* outputs no lower than 10 % of the sourcing range;
* frequencies within the specified 10 Hz – 1 MHz bands.

The 1100 V range's 6 mA load limit is also respected against the 3458A's 1 MΩ ‖ 140 pF
input. The generated maximum at each frequency is 250 V at 15–30 Hz, 700 V (the 3458A's own
ac maximum) from 70 Hz to 1 kHz, 220 V up to 70 kHz, 100 V at 200 kHz, 44 V at 500 kHz and
10 V at 1 MHz. A point beyond these limits is listed under "not planned" with the 5700A
fault it would raise.

Above 100 kHz the 5700A's 50 Ω millivolt ranges drive the 3458A's input capacitance (about
140 pF plus the lead). That makes the reading about 1000 ppm low at 1 MHz. The toolkit does
not correct for it, but carries the full attenuation as a rectangular uncertainty term
(`HF_LOAD_CAPACITANCE_F` in `caldmm.cal.engine`).

## If the 5700A is overdue

If the 5700A is past its own calibration interval (`CAL_DAYS?` beyond `CAL_INTV?`) while it
is the reference, the run still states verdicts. Every row carries
`reference-calibration-overdue`, and the header records how many days over it is: its
`UNCERT?` is computed for an interval that has already elapsed.


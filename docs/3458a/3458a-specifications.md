# Keysight 3458A — specification tables (extract)

**Why this file exists.** `docs/3458a/3458a-ug.pdf` contains no specifications: its
Appendix A (p.409) reads only *"For the specifications and characteristics of the 3458A
multimeter, refer to the datasheet at
http://literature.cdn.keysight.com/litweb/pdf/5965-4971E.pdf."* The figures in
`src/caldmm/cal/data/hp3458a.yaml` therefore came from that data sheet — a 46-page
document, *"3458A Multimeter — Reliably Accurate Measurement Every Time"*, Keysight
Technologies. This repository's `.gitignore` excludes `*.pdf`, so no PDF here is tracked
and the repository's actual convention is a committed markdown extract (as with
`docs/3458a/3458A_Programming_Guide.md`).

**This file is that extract**, covering every page `hp3458a.yaml` transcribes from, so
that each figure in the spec file can be audited from version control alone. A copy of
the source PDF is kept alongside it at `docs/3458a/3458a-datasheet.pdf` (untracked;
md5 `facc927d6a23e775fb7c13da0f85f146`).

Page numbers below are the data sheet's own printed page numbers.
`tests/cal/test_3458a_extract.py` machine-compares every table here against
`hp3458a.yaml`, so the two cannot drift.

**Numeric cells are verbatim**, including trailing zeros — the data sheet's digit
counts differ between rows (the 10 Ω range's full scale prints `12.00000`, the 10 kΩ
range's `12.000000`) and that is reproduced. The only normalisation is to *row labels*:
the data sheet typesets the 1 V row as `1V` in the p.12, p.25, p.28 and p.30 range
tables and as `1 V` elsewhere; it is written `1 V` throughout here.

Two pages here (p.27 and p.32, the high frequency temperature coefficient) are extracted
for a figure the spec file deliberately does **not** carry. They are included because
"not carried" is only auditable if the figure is written down somewhere the test suite can
see it.

Not extracted (the spec file does not use them): system/digitizing/frequency/period
specifications, ACDCV and ACDCI, subsampling and level-triggering characteristics, and the
general/environmental sections.

---

## p.9 — Introduction: what "range" means, and the worked examples

*(The introduction is on the data sheet's p.9; Examples 3, 4 and 5 run over onto p.10-11.
They are kept together here because they are one argument.)*

> The Keysight 3458A accuracy is specified as a part per million (ppm) of the reading plus
> a ppm of range for DCV, ohms, and DCl. **In ACV and ACl, the specification is percent of
> reading plus percent of range. Range means the name of the scale, e.g. 1 V, 10 V, etc.;
> range does not mean the full-scale reading, e.g. 1.2 V, 12 V, etc.** These accuracies are
> valid for a specific time from the last calibration.

> **Absolute versus relative accuracy**
> All 3458A accuracy specifications are relative to the calibration standards.
> Absolute accuracy of the 3458A is determined by adding these relative accuracies to the traceability of your calibration standard.
> For DCV, 2 ppm is the traceability error from the factory.
> That means that the absolute error relative to the U.S. National Institute of Standards and Technology (NIST) is 2 ppm in addition to the DCV accuracy specifications.
> When you recalibrate the 3458A, your actual traceability error will depend upon the errors from your calibration standards.
> These errors will likely be different from the error of 2 ppm.

**EXAMPLE 1** — Relative accuracy; 24-hour operating temperature is Tcal ± 1 °C

> The 24-hour accuracy specification for a 10 V DC measurement on the 10 V range is
> 0.5 ppm + 0.05 ppm. […]
> `(0.5/1,000,000 x 10 V) + (0.05/1,000,000 x 10 V) = ± 5.5 μV or 0.55 ppm of 10 V`

**EXAMPLE 3** — Operating temperature is 38 °C; without ACAL

> `(4.1 ppm x 10 V) + (0.05 ppm x 10 V) = 42 μV`
> Temperature coefficient (specification is per °C):
> `(0.5 ppm x 10 V + 0.01 ppm x 10 V) x 14°C = 71 μV`

*(That second line confirms the tempco is also `ppm of reading + ppm of range` per °C, and
that the without-ACAL column is the one used when ACAL has not been run.)*

**EXAMPLE 4** — Operating temperature is 38 °C; with ACAL

> Assuming the same conditions as Example 3, using ACAL significantly reduces the error due
> to temperature difference from calibration temperature. Operating temperature is 10 ºC
> beyond the standard range of Tcal ± 5 ºC.
> `(4.1 ppm x 10 V) + (0.05 ppm x 10 V) = 42 μV`
> Temperature coefficient (specification is per ºC):
> `(0.15 ppm x 10 V + 0.01 ppm x 10 V) x 10 °C = 16 μV`
> `Total error = 58 μV`

*(The multiplier is the 10 °C **beyond** Tcal ± 5 °C, not the full 15 °C departure: the
with-ACAL tempco is the adder for leaving the window, and the window is centred on Tcal.)*

**EXAMPLE 5** — Absolute accuracy; 90 day

> Assuming the same conditions as Example 4, but now add the traceability error to establish
> absolute accuracy.
> `(4.1 ppm x 10 V) + (0.05 ppm x 10 V) = 42 μV`
> `(0.15 ppm x 10 V + 0.01 ppm x 10 V) x 10 ºC = 16 μV`
> factory traceability error of 2 ppm:
> `(2 ppm x 10 V) = 20 μV`
> `Total absolute error = 78 μV`

---

## p.12 — DC Voltage

| Range | Full scale | Maximum resolution | Input impedance | Temp. coeff. without ACAL¹ | Temp. coeff. with ACAL² |
|---|---|---|---|---|---|
| 100 mV | 120.00000 | 10 nV | > 10 GΩ | 1.2 + 1 | 0.15 + 1 |
| 1 V | 1.20000000 | 10 nV | > 10 GΩ | 1.2 + 0.1 | 0.15 + 0.1 |
| 10 V | 12.0000000 | 100 nV | > 10 GΩ | 0.5 + 0.01 | 0.15 + 0.01 |
| 100 V | 120.000000 | 1 µV | 10 MΩ ± 1% | 2 + 0.4 | 0.15 + 0.1 |
| 1000 V | 1050.00000 | 10 µV | 10 MΩ ± 1% | 2 + 0.04 | 0.15 + 0.01 |

Temperature coefficient is `(ppm of reading + ppm of range) / °C`.

**Accuracy³** `[ppm of reading (ppm of reading for Option 002) + ppm of range]`

| Range | 24 hour⁴ | 90 day⁵ | 1 year⁵ | 2 year⁵ |
|---|---|---|---|---|
| 100 mV | 2.5 + 3 | 5.0 (3.5) + 3 | 9 (5) + 3 | 14 (10) + 3 |
| 1 V | 1.5 + 0.3 | 4.6 (3.1) + 0.3 | 8 (4) + 0.3 | 14 (10) + 0.3 |
| 10 V | 0.5 + 0.05 | 4.1 (2.6) + 0.05 | 8 (4) + 0.05 | 14 (10) + 0.05 |
| 100 V | 2.5 + 0.3 | 6.0 (4.5) + 0.3 | 10 (6) + 0.3 | 14 (10) + 0.3 |
| 1000 V⁶ | 2.5 + 0.1 | 6.0 (4.5) + 0.1 | 10 (6) + 0.1 | 14 (10) + 0.1 |

1. Additional error from Tcal or last ACAL ± 1 °C.
2. Additional error from Tcal ± 5 °C.
3. Specifications are for PRESET; NPLC 100.
4. For fixed range (> 4 min.), MATH NULL and Tcal ± 1 °C
5. Specifications for 90-day, 1 year and 2 year are within 24-hours and ± 1 °C of last
   ACAL; Tcal ± 5 °C; MATH NULL and fixed range.

   ppm of reading specifications for high stability (Option 002) are in parentheses.

   Without MATH NULL, add 0.15 ppm of range to 10 V, 0.7 ppm of range to 0.1V. Without
   MATH NULL and for fixed range less than 4 minutes, add 0.25 ppm of range to 10 V,
   1.7 ppm of range to 1 V and 17 ppm of range to 0.1 V.

   Add 2 ppm pf reading additional error for factory traceability to US NIST. Traceability
   error is the absolute error relative to National Standard associated with the source of
   last external calibration.
6. Add 12 ppm X (Vin / 1000)² additional error for inputs > 100 vV.

## p.13 — DC Voltage continued

**Transfer accuracy/linearity**, 10 min, Tref ± 0.5 °C `(ppm of reading + ppm of range)`

| Range | 10 min, Tref ± 0.5 °C |
|---|---|
| 100 mV | 0.5 + 0.5 |
| 1 V | 0.3 + 0.1 |
| 10 V | 0.05 + 0.05 |
| 100 V | 0.5 + 0.1 |
| 1000 V | 1.5 + 0.05 |

Conditions: following 4-hour warm-up; full scale to 10% of full scale; measurements on the
1000 V range are within 5% of the initial measurement value and following measurement
settling; Tref is the starting ambient temperature; measurements are made on a fixed range
(> 4 min.) using accepted metrology practices.

**Settling characteristics** — For first reading or range change error, add 0.0001% of
input voltage step additional error. Reading setting times are affected by source impedance
and cable dielectric absorption characteristics.

---

## p.17 — Resistance (two-wire and four-wire ohms, OHM and OHMF functions)

| Range | Full scale | Max resolution | Current source¹ | Test voltage | Open circuit | Max load resistance (OHMF) | Max series offset (OCOMP ON) | TC without ACAL² | TC with ACAL³ |
|---|---|---|---|---|---|---|---|---|---|
| 10 Ω | 12.00000 | 10 µΩ | 10 mA | 0.1 V | 12 V | 20 Ω | 0.01 V | 3 + 1 | 1 + 1 |
| 100 Ω | 120.00000 | 10 µΩ | 1 mA | 0.1 V | 12 V | 200 Ω | 0.01 V | 3 + 1 | 1 + 1 |
| 1 kΩ | 1.2000000 | 100 µΩ | 1 mA | 1.0 V | 12 V | 150 Ω | 0.1 V | 3 + 0.1 | 1 + 0.1 |
| 10 kΩ | 12.000000 | 1 mΩ | 100 µA | 1.0 V | 12 V | 1.5 kΩ | 0.1 V | 3 + 0.1 | 1 + 0.1 |
| 100 kΩ | 120.00000 | 10 mΩ | 50 µA | 5.0 V | 12 V | 1.5 kΩ | 0.5 V | 3 + 0.1 | 1 + 0.1 |
| 1 MΩ | 1.2000000 | 100 mΩ | 5 µA | 5.0 V | 12 V | 1.5 kΩ | — | 3 + 1 | 1 + 1 |
| 10 MΩ | 12.000000 | 1 Ω | 500 nA | 5.0 V | 12 V | 1.5 kΩ | — | 20 + 20 | 5 + 2 |
| 100 MΩ⁴ | 120.00000 | 10 Ω | 500 nA | 5.0 V | 5 V | 1.5 kΩ | — | 100 + 20 | 25 + 2 |
| 1 GΩ⁴ | 1.2000000 | 100 Ω | 500 nA | 5.0 V | 5 V | 1.5 kΩ | — | 1000 + 20 | 250 + 2 |

*(Footnote numbering on this page continues from the previous section.)*

7. Current source is ± 3% absolute accuracy.
8. Additional error from Tcal or last ACAL ± 1 °C
9. Additional error from Tcal ± 5 °C.
10. Measurement is computed from 10 MΩ in parallel with input.

**Accuracy¹** `(ppm of reading + ppm of range)`

| Range | 24-hour² | 90 day³ | 1 year³ | 2 year³ |
|---|---|---|---|---|
| 10 Ω | 5 + 3 | 15 + 5 | 15 + 5 | 20 + 10 |
| 100 Ω | 3 + 3 | 10 + 5 | 12 + 5 | 20 + 10 |
| 1 kΩ | 2 + 0.2 | 8 + 0.5 | 10 + 0.5 | 15 + 1 |
| 10 kΩ | 2 + 0.2 | 8 + 0.5 | 10 + 0.5 | 15 + 1 |
| 100 kΩ | 2 + 0.2 | 8 + 0.5 | 10 + 0.5 | 15 + 1 |
| 1 MΩ | 10 + 1 | 12 + 2 | 15 + 2 | 20 + 4 |
| 10 MΩ | 50 + 5 | 50 + 10 | 50 + 10 | 75 + 10 |
| 100 MΩ | 500 + 10 | 500 + 10 | 500 + 10 | 0.1% + 10 |
| 1 GΩ | 0.5% + 10 | 0.5% + 10 | 0.5% + 10 | 1% + 10 |

1. Specifications are for PRESET;NPLC 100; OCOMP ON; OHMF.
2. Tcal ± 1 °C.
3. Specifications for 90-day, 1 year and 2 year are within 24-hours and ± 1 °C of last
   ACAL; Tcal ± 5 °C.
   Add 3 ppm of reading additional error for factory traceability of 10 kΩ to US NIST.
   Traceability is the absolute error relative to National Standards associated with the
   source of last external calibration.

## p.18 — Resistance continued

**Two-wire ohms accuracy**

> For two-wire ohms (OHM) accuracy, add the following offset errors to the four-wire ohms
> (OHMF) accuracy.
> **24 Hour: 50 mΩ. 90 Day: 150 mΩ. 1 Year: 250 mΩ. 2 Year: 500 mΩ.**

**Settling characteristics** — For first reading error following range change, add the total
90-day measurement error for the current range. Preprogrammed settling delay times are for
< 200 pF external circuit capacitance.

---

## p.21 — DC Current (DCI function)

| Range | Full scale | Max resolution | Shunt resistance | Burden voltage | TC without ACAL¹ | TC with ACAL² |
|---|---|---|---|---|---|---|
| 100 nA | 120.000 | 1 pA | 545.2 kΩ | 0.055 V | 10 + 200 | 2 + 50 |
| 1 µA | 1.200000 | 1 pA | 45.2 kΩ | 0.045 V | 2 + 20 | 2 + 5 |
| 10 µA | 12.000000 | 1 pA | 5.2 kΩ | 0.055 V | 10 + 4 | 2 + 1 |
| 100 µA | 120.00000 | 10 pA | 730 Ω | 0.075 V | 10 + 3 | 2 + 1 |
| 1 mA | 1.2000000 | 100 pA | 100 Ω | 0.100 V | 10 + 2 | 2 + 1 |
| 10 mA | 12.000000 | 1 nA | 10 Ω | 0.100 V | 10 + 2 | 2 + 1 |
| 100 mA | 120.00000 | 10 nA | 1 Ω | 0.250 V | 25 + 2 | 2 + 1 |
| 1 A | 1.0500000 | 100 nA | 0.1 Ω | < 1.5 V | 25 + 3 | 2 + 2 |

**Accuracy³** `(ppm of reading + ppm of range)`

| Range | 24-hour⁴ | 90 day⁵ | 1 year⁵ | 2 year⁵ |
|---|---|---|---|---|
| 100 nA⁶ | 10 + 400 | 30 + 400 | 30 + 400 | 35 + 400 |
| 1 µA⁶ | 10 + 40 | 15 + 40 | 20 + 40 | 25 + 40 |
| 10 µA⁶ | 10 + 7 | 15 + 10 | 20 + 10 | 25 + 10 |
| 100 µA | 10 + 6 | 15 + 8 | 20 + 8 | 25 + 8 |
| 1 mA | 10 + 4 | 15 + 5 | 20 + 5 | 25 + 5 |
| 10 mA | 10 + 4 | 15 + 5 | 20 + 5 | 25 + 5 |
| 100 mA | 25 + 4 | 30 + 5 | 35 + 5 | 40 + 5 |
| 1 A | 100 + 10 | 100 + 10 | 110 + 10 | 115 + 10 |

1. Additional error from Tcal or Last ACAL ± 1 °C.
2. Additional error from Tcal ± 5 °C.
3. Specifications are for PRESET;NPLC 100.
4. Tcal ± 1 °C.
5. Specifications for 90-day, 1 year, and 2 year are within 24-hours and ± 1 °C of last
   ACAL; Tcal ± 5 °C.
   Add 5 ppm of reading additional error for factory traceability to US NIST. Traceability
   error is the sum of the 10 V and 10 kΩ traceability values.
6. **Typical accuracy.**

---

## p.24 — AC Voltage, general information

> The Keysight 3458A supports three techniques for measuring true rms AC voltage […]
> selected through the SETACV command.

* **SETACV SYNC** — Synchronously sub-sampled computed true rms technique. Excellent
  linearity and the most accurate results. Requires a repetitive input signal. Bandwidth
  1 Hz to 10 MHz.
* **SETACV ANA** — Analog computing true rms conversion. The technique at power-up or
  following a reset. Works with any signal within its 10 Hz to 2 MHz bandwidth and gives
  the fastest measurement speeds.
* **SETACV RNDM** — Random sampled computed true rms technique. Excellent linearity but the
  lowest overall accuracy of the three; no repetitive input required, so it suits wideband
  noise. Bandwidth 20 Hz to 10 MHz.

**Selection table**

| Technique | Frequency range | Best accuracy | Repetitive signal required | Readings/s min | Readings/s max |
|---|---|---|---|---|---|
| Synchronous sub-sampled | 1 Hz – 10 MHz | 0.010% | Yes | 0.025 | 10 |
| Analog | 10 Hz – 2 MHz | 0.03% | No | 0.8 | 50 |
| Random sampled | 20 Hz – 10 MHz | 0.1% | no | 0.025 | 45 |

---

## p.25 — Synchronous sub-sampled mode (ACV function, SETACV SYNC)

| Range | Full scale | Maximum resolution | Input impedance | Temperature coefficient¹ |
|---|---|---|---|---|
| 10 mV | 12.00000 | 10 nV | 1 MΩ ± 15% with <140pF | 0.002 + 0.02 |
| 100 mV | 120.00000 | 10 nV | 1 MΩ ± 15% with <140pF | 0.001 + 0.0001 |
| 1 V | 1.2000000 | 100 nV | 1 MΩ ± 15% with <140pF | 0.001 + 0.0001 |
| 10 V | 12.000000 | 1 µV | 1 MΩ ± 2% with <140pF | 0.001 + 0.0001 |
| 100 V | 120.00000 | 10 µV | 1 MΩ ± 2% with <140pF | 0.001 + 0.0001 |
| 1000 V | 700.0000 | 100 µV | 1 MΩ ± 2% with <140pF | 0.001 + 0.0001 |

Temperature coefficient is `(% of reading + % of range) / °C`.

**AC accuracy²** — 24-hour to 2 year `(% of reading + % of range)`, **ACBAND ≤ 2 MHz**

| Range | 1 Hz to 40 Hz³ | 40 Hz to 1 kHz³ | 1 kHz to 20 kHz³ | 20 kHz to 50 kHz³ | 50 kHz to 100 kHz | 100 kHz to 300 kHz | 300 kHz to 1 MHz | 1 MHz to 2 MHz |
|---|---|---|---|---|---|---|---|---|
| 10 mV | 0.03 + 0.03 | 0.02 + 0.011 | 0.03 + 0.011 | 0.1 + 0.011 | 0.5 + 0.011 | 4.0 + 0.02 | | |
| 100 mV – 10V | 0.007 + 0.004 | 0.007 + 0.002 | 0.014 + 0.002 | 0.03 + 0.002 | 0.08 + 0.002 | 0.3 + 0.01 | 1 + 0.01 | 1.5 + 0.01 |
| 100 V | 0.02 + 0.004 | 0.02 + 0.002 | 0.02 + 0.002 | 0.035 + 0.002 | 0.12 + 0.002 | 0.4 + 0.01 | 1.5 + 0.01 | |
| 1000 V | 0.04 + 0.004 | 0.04 + 0.002 | 0.06 + 0.002 | 0.12 + 0.002 | 0.3 + 0.002 | | | |

1. Additional error beyond ± 1ºC, but within + 5 ºC of last ACAL. **For ACBAND > 2MHz, use
   10 mV range temperature coefficient for all ranges.**
2. Specifications apply full scale to 10% of full scale, DC < 10% of AC sine wave input,
   crest factor = 1.4, and PRESET. Within 24-hours and ± 1 °C of last ACAL. Lo to guard
   switch on. Peak (AC + DC) input limited to 5x full scale for all ranges in ACV function.
   Add 2ppm of reading additional error for factory traceability of 10 V DC to US NIST.
3. LFILTER ON recommended.

**AC accuracy, ACBAND > 2 MHz** `(% of reading + % of range)`

| Range | 45 Hz to 100 kHz | 100 kHz to 1 MHz | 1 MHz to 4 MHz | 4 MHz to 8 MHz | 8 MHz to 10 MHz |
|---|---|---|---|---|---|
| 10 mV | 0.09 + 0.06 | 1.2 + 0.05 | 7 + 0.07 | 20 + 0.08 | |
| 100 mV – 10V | 0.09 + 0.06 | 2.0 + 0.05 | 4 + 0.07 | 4 + 0.08 | 15 + 0.1 |
| 100 V | 0.12 + 0.002 | | | | |
| 1000 V | 0.3 + 0.01 | | | | |

---

## p.27 — AC Voltage: high frequency temperature coefficient

*(Printed in the SYNC half of the AC Voltage section; the identical table appears again on
p.32 in the RNDM half. `hp3458a.yaml` deliberately does **not** carry it — a band has one
`tempco` slot and it already holds the ACAL-referenced figure, which is a different regime
with a different datum — so the figures are recorded here and named on each `*_wide` band's
note.)*

**High frequency temperature coefficient** — For outside Tcal ± 5 °C add the following
error. `(% of reading)/ °C`

| Range | 2 – 4 MHz | 4 – 10 MHz |
|---|---|---|
| 10 mV – 1 V | 0.02 | 0.08 |
| 10 V – 1000 V | 0.08 | 0.08 |

---

## p.28 — Analog mode (ACV function, SETACV ANA)

| Range | Full scale | Maximum resolution | Input impedance | Temperature coefficient¹ |
|---|---|---|---|---|
| 10 mV | 12.00000 | 10 nV | 1 MΩ ± 15% with < 140 pF | 0.003 + 0.006² |
| 100 mV | 120.0000 | 100 nV | 1 MΩ ± 15% with < 140 pF | 0.002 + 0.0 |
| 1 V | 1.200000 | 1 µV | 1 MΩ ± 15% with < 140 pF | 0.002 + 0.0 |
| 10 V | 12.00000 | 10 µV | 1 MΩ ± 2% with < 140 pF | 0.002 + 0.0 |
| 100 V | 120.0000 | 100 µV | 1 MΩ ± 2% with < 140 pF | 0.002 + 0.0 |
| 1000 V | 700.000 | 1 mV | 1 MΩ ± 2% with < 140 pF | 0.002 + 0.0 |

**AC accuracy³** — 24-hour to 2 year `(% of reading + % of range)`, **ACBAND ≤ 2 MHz**

| Range | 10 Hz to 20 Hz | 20 Hz to 40 Hz | 40 Hz to 100 Hz | 100 Hz to 20 kHz | 20 kHz to 50 kHz | 50 kHz to 100 kHz | 100 kHz to 250 kHz | 250 kHz to 500 kHz | 500 kHz to 1 MHz | 1 MHz to 2 MHz |
|---|---|---|---|---|---|---|---|---|---|---|
| 10 mV | 0.4+0.32 | 0.15+0.25 | 0.06+0.25 | 0.02+0.25 | 0.15+0.25 | 0.7+0.35 | 4+0.7 | | | |
| 100 mV – 10 V | 0.4+0.02 | 0.15+0.02 | 0.06+0.01 | 0.02+0.01 | 0.15+0.04 | 0.6+0.08 | 2+0.5 | 3+0.6 | 5+2 | 10+5 |
| 100 V | 0.4+0.02 | 0.15+0.02 | 0.06+0.01 | 0.03+0.01 | 0.15+0.04 | 0.6+0.08 | 2+0.5 | 3+0.6 | 5+2 | |
| 1000 V | 0.42+0.03 | 0.17+0.03 | 0.08+0.02 | 0.06+0.02 | 0.15+0.04 | 0.6+0.2 | | | | |

1. Additional error beyond ± 1 °C, but within ± 5 °C of last ACAL.
2. Specifications apply full scale to ¼ full scale.
3. Specifications apply full scale to 1/20 full scale, sinewave input, crest factor = 1.4,
   and PRESET. Within 24-hours and ± 1 °C of last ACAL. Lo to guard switch on. Maximum DC
   is limited to 400 V in ACV functions. Add 2 ppm of reading additional error for factory
   traceability of 10 V DC to US NIST.
4. Additional error beyond ± 1 °C, but within ± 5 °C of last ACAL.
   `(% of Reading + % of Range)/ °C`

---

## p.30 — AC Voltage: settling, maximum input, and RNDM ranges

**Settling characteristics** — For first reading or range change error using default delays,
add 0.01% of input step additional error. The following data applies for DELAY 0.

| Function | ACBAND low | DC component | Settling time |
|---|---|---|---|
| ACV | ≥ 10 kHz | DC < 10% AC | 0.5 sec to 0.01% |
| ACV | ≥ 10 kHz | DC > 10% AC | 0.9 sec to 0.01% |
| ACDC | 10 Hz – 1 kHz | | 0.5 second to 0.01% |
| ACDC | 1 kHz – 10 kHz | | 0.08 sec to 0.01% |
| ACDC | ≥ 10 kHz | | 0.015 sec to 0.01% |

**Maximum input**

| | Rated input | Non-destructive |
|---|---|---|
| HI to LO | ± 1000 V pk | ± 1200 V pk |
| LO to guard | ± 200 V pk | ± 350 V pk |
| Guard to Earth | ± 500 V pk | ± 1000 V pk |
| **Volt – Hz product** | **1x10⁸** | |

**Random sampled mode (ACV function, SETACV RNDM)**

| Range | Full scale | Maximum resolution | Input impedance | Temperature coefficient¹ |
|---|---|---|---|---|
| 10 mV | 12.00000 | 1 µV | 1 MΩ ± 15% with < 140 pF | 0.002 + 0.02 |
| 100 mV | 120.0000 | 10 µV | 1 MΩ ± 15% with < 140 pF | 0.001 + 0.00001 |
| 1 V | 1.200000 | 100 µV | 1 MΩ ± 15% with < 140 pF | 0.001 + 0.00001 |
| 10 V | 12.00000 | 1 mV | 1 MΩ ± 2% with < 140 pF | 0.001 + 0.00001 |
| 100 V | 120.0000 | 10 mV | 1 MΩ ± 2% with < 140 pF | 0.001 + 0.00001 |
| 1000 V | 700.000 | 100 mV | 1 MΩ ± 2% with < 140 pF | 0.001 + 0.00001 |

1. Additional error beyond ± 1 °C, but within ± 5 °C of last ACAL. **For ACBAND > 2 MHz,
   use 10 mV range temperature coefficient for all ranges.**

---

## p.31 — Random sampled mode accuracy

**AC accuracy¹** — 24-hour to 2 year `(% of reading + % of range)`

| Range | ≤2 MHz 20 Hz to 100 kHz | ≤2 MHz 100 kHz to 300 kHz | ≤2 MHz 300 kHz to 1 MHz | ≤2 MHz 1 MHz to 2 MHz | >2 MHz 20 Hz to 100 kHz | >2 MHz 100 kHz to 1 MHz | >2 MHz 1 MHz to 4 MHz | >2 MHz 4 MHz to 8 MHz | >2 MHz 8 MHz to 10 MHz |
|---|---|---|---|---|---|---|---|---|---|
| 10 mV | 0.5+0.02 | 4+0.02 | | | 0.1+0.05 | 1.2+0.05 | 7+0.07 | 20+0.08 | |
| 100 mV-10 V | 0.08+0.002 | 0.3+0.01 | 1+0.01 | 1.5+0.01 | 0.1+0.05 | 2+0.05 | 4+0.07 | 4+0.08 | 15+0.1 |
| 100 V | 0.12+0.002 | 0.4+0.01 | 1.5+0.01 | | 0.12+0.002 | | | | |
| 1000 V | 0.3+0.01 | | | | 0.3+0.01 | | | | |

*(The data sheet prints this as two side-by-side tables, "ACBAND ≤ 2 MHz" on the left and
"ACBAND > 2 MHz" on the right; they are merged here with the regime in each column
heading.)*

1. Specifications apply from full scale to 5% of full scale, DC < 10% of AC, sine wave
   input, crest factor = 1.4, and PRESET. Within 24-hours and ± 1 °C of last ACAL. LO to
   guard switch on. Add 2 ppm of reading additional error for factory traceability of 10V
   DC to US NIST. Maximum DC is limited to 400V in ACV function.
2. Additional error beyond ± 1 °C, but within ± 5 °C of last ACAL. `(% of reading)/ °C`.
   For ACBAND > 2 MHz, use 10 mV range temperature coefficient for all ranges.
3. Flatness error including instrument loading.

**Additional errors** — apply as appropriate to your measurement setup `(% of reading)`

| Source R | 0 – 1 MHz | 1 – 4 MHz | 4 – 8 MHz | 8 – 10 MHz |
|---|---|---|---|---|
| 0 Ω | 0 | 2 | 5 | 5 |
| 50 Ω terminated | 0.003 | 0 | 0 | 0 |
| 75 Ω terminated | 0.004 | 2 | 5 | 5 |
| 50 Ω | 0.005 | 3 | 7 | 10 |

| Crest factor | Resolution multiplier |
|---|---|
| 1 – 2 | (Resolution in %) x 1 |
| 2 – 3 | (Resolution in %) x 3 |
| 3 – 4 | (Resolution in %) x 5 |
| 4 – 5 | (Resolution in %) x 8 |

**Common mode rejection** — For 1 kΩ imbalance in LO lead, > 90 dB, DC – 60 Hz

---

## p.32 — Random sampled mode: high frequency temperature coefficient

*(The same table as p.27, printed again in the RNDM half of the AC Voltage section.)*

**High frequency temperature coefficient** — For outside Tcal ± 5 °C add the following
error `(% of reading)/ °C`

| Range | 2 – 4 MHz | 4 – 10 MHz |
|---|---|---|
| 10 mV – 1 V | 0.02 | 0.08 |
| 10 V – 1000 V | 0.08 | 0.08 |

---

## p.33 — AC Current (ACI and ACDCI functions)

| Range | Full scale | Maximum resolution | Shunt resistance | Burden voltage | Temperature coefficient¹ |
|---|---|---|---|---|---|
| 100 µA | 120.0000 | 100 pA | 730 Ω | 0.1 V | 0.002 + 0 |
| 1 mA | 1.200000 | 1 nA | 100 Ω | 0.1 V | 0.002 + 0 |
| 10 mA | 12.00000 | 10 nA | 10 Ω | 0.1 V | 0.002 + 0 |
| 100 mA | 120.0000 | 100 nA | 1 Ω | 0.25 V | 0.002 + 0 |
| 1 A | 1.050000 | 1 µA | 0.1 Ω | < 1.5 V | 0.002 + 0 |

Temperature coefficient is `(% of reading + % of range) / °C`.

**AC accuracy²** — 24-hour to 2 year `(% reading + % range)`

| Range | 10 Hz to 20 Hz | 20 Hz to 45 Hz | 45 Hz to 100 Hz | 100 Hz to 5 kHz | 5 kHz to 20 kHz³ | 20 kHz to 50 kHz³ | 50 kHz to 100 kHz³ |
|---|---|---|---|---|---|---|---|
| 100 µA⁴ | 0.4+0.03 | 0.15+0.03 | 0.06+0.03 | 0.06+0.03 | | | |
| 1 mA – 100 mA | 0.4+0.02 | 0.15+0.02 | 0.06+0.02 | 0.03+0.02 | 0.06+0.02 | 0.4+0.04 | 0.55+0.15 |
| 1 A | 0.4+0.02 | 0.16+0.02 | 0.08+0.02 | 0.1+0.02 | 0.3+0.02 | 1+0.04 | |

1. Additional error beyond ± 1 ºC, but within ± 5 ºC of last ACAL.
2. Specifications apply full scale to 1/20 full scale, for sine wave inputs, crest factor =
   1.4, and following PRESET within 24-hours and ± 1 ºC of last ACAL.
   Add 5 ppm of reading additional error for factory traceability to US NIST.
   Traceability is the sum of the 10 V and 10 kΩ traceability values.
3. **Typical performance.**
4. 1 kHz maximum on the 100 µA range.

*(`hp3458a.yaml` carries the four specified columns and **not** the three marked typical
(footnote 3): a typical figure is not a specification, so an ac-current point above 5 kHz is
reported as unspecified rather than judged.  Footnote 4 stops the 100 µA range's last band
at 1 kHz.)*

**Low frequency error** (p.34, `% of reading`) — zero in the "ACBAND low 10 Hz – 1 kHz,
NPLC > 10" column at every signal frequency listed (10 Hz – 10 kHz); up to 0.9 % in the
others.  `hp3458a.yaml` pins `ACBAND 10,100000` and `NPLC 20` on the ACI function so that
this table's adder is zero and need not be carried.


# Fluke 5700A/5720A Wideband AC Voltage (Option 5700A-03) — specifications

Transcribed from the **5700A/5720A Operators Manual Supplement, issue 5
(1/09), change #1 (40695)**, which replaces Operators Manual page 1-42 in its
entirety.

The supplement's figures differ from the 1996 Operators Manual print in the
**11 mV row** (90 days 0.3→0.35, 180 days 0.35→0.5) and add footnote [1] to
the flatness table. The supplement is the authority; the 1996 page is
superseded.

Specifications apply **to the end of the cable and 50 Ω termination used for
calibration**. Like every other table in this instrument's specification
section, the figures are absolute uncertainty at 99 % confidence, ±5 °C from
calibration temperature (`CAL_TEMP?`), and `UNCERT?` remains the run-time
authority (`limits_authority: instrument`).

## Absolute uncertainty, 30 Hz – 500 kHz

± (% output + µV):

| Range   | dBm  | Resolution | 24 Hours   | 90 Days    | 180 Days  | 1 Year   |
|---------|------|------------|------------|------------|-----------|----------|
| 1.1 mV  | -46  | 10 nV      | 0.4 + 0.4  | 0.5 + 0.4  | 0.6 + 0.4 | 0.8 + 2  |
| 3 mV    | -37  | 10 nV      | 0.4 + 1    | 0.45 + 1   | 0.5 + 1   | 0.7 + 3  |
| 11 mV   | -26  | 100 nV     | 0.2 + 4    | 0.35 + 4   | 0.5 + 4   | 0.7 + 8  |
| 33 mV   | -17  | 100 nV     | 0.2 + 10   | 0.3 + 10   | 0.45 + 10 | 0.6 + 16 |
| 110 mV  | -6.2 | 1 µV       | 0.2 + 40   | 0.3 + 40   | 0.45 + 40 | 0.6 + 40 |
| 330 mV  | +3.4 | 1 µV       | 0.2 + 100  | 0.25 + 100 | 0.35 + 100| 0.5 + 100|
| 1.1 V   | +14  | 10 µV      | 0.2 + 400  | 0.25 + 400 | 0.35 + 400| 0.5 + 400|
| 3.5 V   | +24  | 10 µV      | 0.15 + 500 | 0.2 + 500  | 0.3 + 500 | 0.4 + 500|

The specification table's "3 mV" range corresponds to range identifier
`WB3_3MV` (guide §10; the eight identifiers are `WB1_1MV WB3_3MV WB11MV
WB33MV WB110MV WB330MV WB1_1V WB3_5V`).

## Amplitude flatness, 1 kHz reference

± (% output + floor indicated), by voltage-range column:

| Frequency      | Freq. resolution | 1.1 mV       | 3 mV         | >3 mV       | Tempco ±ppm/°C | Settling (s) | Harmonics (dB) |
|----------------|------------------|--------------|--------------|-------------|----------------|--------------|----------------|
| 10 – 30 Hz     | 0.01 Hz          | 0.3          | 0.3          | 0.3         | 100            | 7            | -40            |
| 30 – 120 Hz    | 0.01 Hz          | 0.1          | 0.1          | 0.1         | 100            | 7            | -40            |
| 120 – 1.2 kHz  | 0.1 Hz           | 0.1          | 0.1          | 0.1         | 100            | 5            | -40            |
| 1.2 k – 12 kHz | 1 Hz             | 0.1          | 0.1          | 0.1         | 100            | 5            | -40            |
| 12 k – 120 kHz | 10 Hz            | 0.1          | 0.1          | 0.1         | 100            | 5            | -40            |
| 120 k – 1.2 MHz| 100 Hz           | 0.2 + 3 µV   | 0.1 + 3 µV   | 0.1 + 3 µV  | 100            | 5            | -40            |
| 1.2 M – 2 MHz [1] | 100 kHz       | 0.2 + 3 µV   | 0.1 + 3 µV   | 0.1 + 3 µV  | 100            | 0.5          | -40            |
| 2 M – 10 MHz   | 100 kHz          | 0.4 + 3 µV   | 0.3 + 3 µV   | 0.2 + 3 µV  | 100            | 0.5          | -40            |
| 10 M – 20 MHz  | 1 MHz            | 0.6 + 3 µV   | 0.5 + 3 µV   | 0.4 + 3 µV  | 150            | 0.5          | -34            |
| 20 M – 30 MHz  | 1 MHz            | 1.5 + 15 µV  | 1.5 + 3 µV   | 1 + 3 µV    | 300            | 0.5          | -34            |

[1] For output voltages < 50 % of full range in the 33 mV, 110 mV, 330 mV,
1.1 V and 3.5 V ranges, add 0.1 % to the amplitude flatness specification.

The `[1]` glyph is typeset next to the *1.2 M – 2 MHz* row label in all three
prints (MS5 `5700a-operators-supplement-2009.txt:61`, MS9
`5700a-operators-supplement-2014.txt:1461`, and the Extended Specifications
`5700a-extended-specifications.txt:1354`, where the row label wraps and the
marker lands on its continuation line). The footnote text itself names no
frequency: its conditions are the output level and the range, and what it
modifies is "the amplitude flatness specification". It is therefore read as a
note on the flatness **table**, applying wherever a flatness row is used —
which is how the audit and `fluke5700a.yaml` treat it. Reading it as a note on
the 1.2 – 2 MHz row alone would leave the 2 – 30 MHz rows, where flatness is
largest, uncorrected.

## Additional operating information

- dBm reference = 50 Ω; 0.22361 V across 50 Ω = 1 mW = 0 dBm.
- Range boundaries are at voltage points; dBm levels are approximate.
- **Minimum output: 300 µV (−57 dBm).**
- Frequency uncertainty ±0.01 %; resolution 11 999 counts to 1.1999 MHz,
  119 counts to 30 MHz.
- Overload protection: a short circuit on the wideband output causes no
  damage; normal operation is restored after settling once removed.

## How `fluke5700a.yaml` encodes this

The WBV function's per-band limits are the **absolute-uncertainty row plus
the flatness row**, floors added linearly:

- 30 Hz – 500 kHz: the absolute-uncertainty row alone (flatness inside this
  band is already included in the absolute figures — that is the band the
  absolute table is stated for).
- 10 – 30 Hz: absolute + 0.3 % (no extra floor).
- 500 kHz – 2 MHz, 2 – 10 MHz, 10 – 20 MHz, 20 – 30 MHz: absolute + the
  flatness percentage for the range's column, floor + the flatness floor.
  The 120 k – 1.2 MHz flatness row governs its 500 kHz – 1.2 MHz portion and
  carries the same figures as the 1.2 – 2 MHz row, so the two are merged
  into one 500 kHz – 2 MHz band.

### Footnote [1], the below-half-range flatness adder

Footnote [1] is carried as a `below_frac_adder: {below_frac: 0.5,
pct_reading: 0.1}` on the individual **bands** of the five ranges it names
(33 mV, 110 mV, 330 mV, 1.1 V, 3.5 V) — not on the range as a whole.

Band level, because the footnote adds to the **amplitude flatness**
specification, and the 30 Hz – 500 kHz band is the one band whose limit uses
no flatness row: it is the band the absolute-uncertainty table is itself
stated for. A range-level adder would reach that band too and make it 0.1 %
too loose. The other five bands of each named range (10 – 30 Hz, 500 kHz –
2 MHz, 2 – 10 MHz, 10 – 20 MHz, 20 – 30 MHz) each carry it. The three ranges
the footnote does not name — 1.1 mV, 3.3 mV, 11 mV — carry it nowhere.

The comparison is strict, as the footnote's "< 50 %" is: at exactly half
scale nothing is added. Worked example, 0.5 V at 10 MHz on the 1.1 V range,
1 year: 0.5 % absolute + 0.4 % flatness = 0.9 %, and 0.5 V < 0.55 V, so
+0.1 % → **1.0 % + 403 µV = 5.403 mV** (0.9 % + 403 µV = 4.903 mV without
the footnote).

Not carried (recorded in the YAML header): the tempco column (the ±5 °C
window is part of the absolute figures, as everywhere else in the file), and
the harmonic-distortion column.

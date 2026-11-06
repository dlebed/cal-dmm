# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`caldmm` is a Python 3.11+ toolkit for exactly two instruments: the **Fluke
5700A/5720A Series II calibrator** and the **HP/Agilent/Keysight 3458A DMM**.
It provides their drivers, and a calibration subsystem that verifies either one
against the other and drives their external adjustments. It is generic: no
lab, host, database or unit-specific data belongs here. Keep it that way, with
no serial numbers, unit logs, captures or site paths.

## Commands

```bash
pip install -e ".[dev]"                          # core + pytest
pip install -e ".[serial,gpib,dev]"              # Linux bench
pip install -e ".[serial,visa,dev]"              # Windows bench
pytest -q                                        # full suite, offline
pytest tests/cal/test_engine.py -q               # one file
```

There is no lint/format config; match the surrounding style. The suite must
stay runnable with **no hardware and no interface libraries installed**.
`conftest.py` stubs `serial`, `Gpib`, `pyvisa` and `smbus` so driver modules
import anyway. Do not add hardware I/O to tests.

## Architecture

- **`drivers/gpib.py`**: the only place a GPIB device is opened.
  `Gpib(bus, pad, timeout=Tcode)` keeps linux-gpib's signature and returns
  either the native `Gpib.Gpib` or a `VisaGpibDevice` (pyvisa,
  `GPIB<bus>::<pad>::INSTR`). The backend comes from `CALDMM_GPIB_BACKEND`,
  otherwise VISA on Windows, otherwise linux-gpib if it imports. Drivers import
  it as `from caldmm.drivers import gpib as Gpib`, so tests patch
  `caldmm.drivers.hp3458a.Gpib.Gpib` or `patch.object(fluke5700a, "Gpib")`.
- **`drivers/hp3458a.py`**: implements `MultimeterBase` (`drivers/_base.py`).
- **`drivers/fluke5700a.py`**: a calibrator sources rather than measures, so
  it is a plain class over a `Transport` protocol (`GpibTransport`,
  `SerialTransport`), not a `MultimeterBase`.
- **`cal/`**: instrument specification tables (`cal/data/*.yaml`), the
  JCGM 100 (GUM) uncertainty engine, JCGM 106 / ILAC-G8 verdicts, plan
  generation, the streaming result schema, the execution engine, the
  `CAL_RPT?` parser and the adjustment-procedure framework. Start at
  `cal/roles.py`, the seam between the engine and the instruments.
  **Import invariant: every module in `cal/` imports with no hardware library
  present.** `caldmm.drivers` may appear in exactly two of them,
  `cal/adapters.py` and `cal/dmm_procedures.py`, and in both only **lazily,
  inside a function**. One `sys.meta_path` blocker test per module enforces
  this.
- **Adding a procedure means adding an import**: a procedure reaches the
  registry only once its module is imported, so a new model's procedure module
  must be imported in `apps/cal_verify.py`. A missing import does not fail
  loudly; `--adjust` just reports that the model has no procedure.
- **Adding a model** means a YAML in `cal/data/`, an adapter in
  `cal/adapters.py` registered in `ADAPTERS`, and golden tests that write the
  limit arithmetic out longhand from the manufacturer's document.
- **`apps/`**: `cal_verify`, `cal_analyze`, `fluke5700a_console`,
  `fluke5700a_selfcal`. **`tools/`**: `acal`, `interactive`. **`lib/`**:
  console prompts, CSV, formatting, signals, stats.

- **Wiring groups are terminal sets** (`quantities.Terminals`), not
  functions: DCV, ACV and 2/4-wire ohms share one 4-wire cable set, and DCI/ACI
  need the HI lead on the meter's current input. `QUANTITY_ORDER` puts the
  voltage/ohms functions first and current next, so a full plan re-cables
  once. A `PointGroup` can have several `(quantity, connection)` segments.
  Keep both properties: an operator prompt is the only manual step.
- **Manufacturer verification points** live in `plan.MANUFACTURER_TESTS`
  (3458A Cal Manual Tables 4-1 to 4-6, with the test-card limits). The default
  `point_set="full"` merges them into the ladder, labelled `[CM 4-x #n]`.
  The card limits are **reported, never used as tolerance**; the data sheet
  is. `tests/cal/test_manufacturer_tests.py` also pins every AC point inside
  the 5700A's Volt-Hertz / 1100 V-range envelope.

Instrument names in `MODEL:NAME:ADDR` are free-form operator labels. Never
infer a model, channel or role from one.

## Instrument rules that bite

- **5700A: `EXTGUARD?` and `EXTSENSE?` are not queries.** The instrument
  never answers them and queues faults. Read `ISR?` instead: bit 0 OPER,
  bit 1 EXGARD, bit 2 EXSENS, 2048 REMOTE, 4096 SETTLED.
- **5700A: use short timeouts for ad-hoc status checks** and drain `FAULT?`
  to 0 afterwards. A hung long-timeout query can wedge the bus for every
  later session.
- **5700A standby is not a detector.** A meter reading of the output while
  in standby is not a defined impedance. Gate on `TERM?` or operator
  confirmation, then prove the wiring with a small live output before
  anything ≥ 22 V.
- **5700A resistance, 4-wire:** `EXTSENSE OFF` does not tie the sense posts to
  the output internally. Use `EXTSENSE ON` for 4-wire ohms, except 100 MΩ,
  which is 2-wire only.
- **3458A: read `CAL? 72` after every `ACAL`**, not only `CAL? 2`. Constant
  72 is the ADC-gain drift diagnostic (Service Note 3458A-18A, nominal 1.0,
  limits 0.96/1.04); `CAL? 2` is normally unchanged by an ACAL. Do not
  extrapolate a 72 drift rate fitted inside one window; it is not secular.
- **3458A `TEMP?`:** don't poll it faster than every 30 minutes, and treat a
  reading taken with high ac voltage on the input as invalid.
- **One owner per GPIB device.** Never let two scripts drive one instrument;
  a device clear from one aborts the other's read.
- **Order verification runs by wiring** (voltage functions together, current
  functions back to back) so each terminal set is connected once. Adjustments
  follow the manufacturer's mandated order instead.
- **Filter the `±9.9e37` invalid-reading sentinel** in any analysis over
  measured values.

## Docs

- `docs/calibration-howto.md` is the user workflow. Keep its transcripts in
  step with the CLI.
- **PDFs are not tracked** (`*.pdf` in `.gitignore`). Commit a markdown
  extract instead, like `docs/3458a/3458a-specifications.md`, which
  `tests/cal/test_3458a_extract.py` checks against `hp3458a.yaml`. Do not
  commit verbatim copies of copyrighted manuals or articles.

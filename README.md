# caldmm

Python drivers and a verification / calibration toolkit for two instruments:

| Instrument | Role | Interfaces | Driver |
|---|---|---|---|
| **Fluke 5700A / 5720A Series II** | multifunction calibrator (source) | GPIB, RS-232C | `caldmm.drivers.fluke5700a` |
| **HP / Agilent / Keysight 3458A** | 8½-digit DMM (meter) | GPIB | `caldmm.drivers.hp3458a` |

On top of the drivers sits a calibration subsystem that verifies either
instrument against the other, in **both directions**:

- **meter as DUT:** is the 3458A right? The 5700A is the reference.
- **calibrator as DUT:** is the 5700A right? A golden 3458A is the reference.
  For dc volts this is usually the honest question, because a 3458A's 1-year
  DCV specification is tighter than a 5700A's.

Every point carries a JCGM 100 (GUM) uncertainty budget and a JCGM 106 /
ILAC-G8 conformity verdict (PASS / FAIL / INDETERMINATE, with guard bands and
a TUR floor). Results stream to a CSV that survives a crash and can be resumed.
The toolkit also drives the guided **3458A external adjustment** (`CAL` against
10 V / 10 kΩ artefacts or the 5700A) and the **5700A artifact calibration**
(`CAL_ZERO`, `CAL_REF` against external 10 V / 1 Ω / 10 kΩ standards).

Runs on **Linux** (linux-gpib) and **Windows** (NI-VISA or Keysight IO
Libraries via pyvisa). The test suite needs no hardware and no interface
libraries.

## Installation

Python 3.11 or newer.

```bash
pip install -e .                    # core
pip install -e ".[serial]"          # 5700A over RS-232C (pyserial)
pip install -e ".[gpib]"            # Linux: linux-gpib Python binding
pip install -e ".[visa]"            # Windows / macOS: pyvisa
pip install -e ".[dev]"             # pytest
```

### GPIB backends

All GPIB I/O goes through `caldmm.drivers.gpib`, which opens a device with
linux-gpib's `Gpib(bus, pad, timeout=Tcode)` signature and picks a backend:

| Platform | Default backend | What you need installed |
|---|---|---|
| Linux | `linux-gpib` (when the `Gpib` module imports) | linux-gpib kernel driver + Python binding |
| Windows | `visa` | NI-VISA + NI-488.2, **or** Keysight IO Libraries Suite; `pip install pyvisa` |
| other | `visa` | any VISA library pyvisa can load |

Override with environment variables:

```bash
CALDMM_GPIB_BACKEND=visa        # or linux-gpib
CALDMM_VISA_LIBRARY=@py         # passed to pyvisa.ResourceManager (empty = system VISA)
```

With VISA, `bus,pad` maps to the resource `GPIB<bus>::<pad>::INSTR`, so GPIB
board 0, address 22 is `GPIB0::22::INSTR`, exactly as NI MAX or Keysight
Connection Expert show it.

### Addresses

Instruments are given as `MODEL[:NAME]:ADDR`. `NAME` is your own label for the
unit and never implies a model or a role.

| Spelling | Meaning |
|---|---|
| `22` | GPIB primary address 22 on bus 0 |
| `1,22` or `gpib1,22` | GPIB bus (board) 1, address 22 |
| `/dev/ttyUSB0`, `/dev/ttyUSB0@19200` | serial port (Linux), optional baud |
| `COM3`, `COM3@19200` | serial port (Windows), optional baud |

## Command-line tools

| Command | What it does |
|---|---|
| `caldmm-cal-verify` | Plan and run a verification (every range and function, one re-cable), or an external adjustment (`--adjust`) |
| `caldmm-cal-analyze` | Offline PDF / markdown report from a result CSV: verdicts, range coverage, manufacturer test card; compares as-found / as-left runs |
| `caldmm-5700a-selfcal` | Guided 5700A `CAL_ZERO`, `CAL_CHK`, artifact calibration, `CAL_RPT?` capture and diff |
| `caldmm-5700a-console` | Interactive 5700A service console: status, fault queue, diagnostics, manual sourcing |
| `caldmm-acal` | Periodic 3458A `ACAL` scheduler that logs `CAL? 72` drift |
| `caldmm-interactive` | REPL for a 3458A or a raw GPIB device |

### Verify a 3458A against a 5700A

One run covers every range of every 3458A function the 5700A can drive: DCV,
ACV in both SYNC and ANA, 4- and 2-wire ohms up to 1 GΩ, DCI, and ACI. It also
includes every test point of the 3458A Calibration Manual's performance
verification (Tables 4-1 to 4-6). The only manual step is **one re-cable**:

1. Connect 5700A OUTPUT and SENSE to the 3458A's HI/LO and Ω SENSE inputs, plus
   guard. DCV, ACV and both resistance wirings run unattended through this one
   cable set.
2. At the prompt, move the HI lead to the 3458A's current (I) input. DCI and ACI
   then run unattended.

```bash
# Preview the plan: points, the two wiring groups, spec ratios, run time. Touches nothing.
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter \
    -o runs/3458a-verify.csv --dry-run

# Run it: ACAL ALL + 30 min settling first, then the whole plan
# (--lead-resistance: measured round-trip ohms of the leads, for the 2-wire points;
#  add --meter-option 002 for an Option 002 unit)
caldmm-cal-verify --source fluke5700a:4 --meter hp3458a:22 --dut meter \
    --acal --lead-resistance 0.032 --ambient 23.1 -o runs/3458a-verify.csv

# Report: verdicts, range coverage, and the Cal Manual test-card cross-check
caldmm-cal-analyze runs/3458a-verify.csv
```

`--points manufacturer` runs only the Calibration Manual's 42 points, in about
30 minutes. Before anything is energised, the run checks the 3458A's `TEMP?`
against its `CAL? 59/60` adjustment temperatures (±5 °C, as the manual
requires). Every AC point is held inside the 5700A's Volt-Hertz and 1100 V
range limits. See [`docs/3458a/cal-manual-verification.md`](docs/3458a/cal-manual-verification.md)
for how the run maps onto the manual, and where the manual's test-card limits
differ from the data sheet.

### Verify a 5700A against a golden 3458A

```bash
caldmm-cal-verify --source fluke5700a:cal1:4 --meter hp3458a:dmm1:22 \
    --dut source --quantities DCV --ambient 23.1 -o runs/5700a-dcv.csv
```

### Windows example

```powershell
$env:CALDMM_GPIB_BACKEND = "visa"     # the default on Windows anyway
caldmm-cal-verify --source fluke5700a:COM3@19200 --meter hp3458a:0,22 --dut meter --dry-run -o run.csv
caldmm-5700a-console --port COM3
```

### Adjust a 3458A

```bash
caldmm-cal-verify --meter hp3458a:22 --adjust            # dry run: rehearses, writes nothing
caldmm-cal-verify --meter hp3458a:22 --adjust --adjust-commit ...
```

See [`docs/calibration-howto.md`](docs/calibration-howto.md) for the whole
workflow: plan direction, wiring proofs, resuming, reading verdicts, the
3458A adjustment and the 5700A artifact calibration.

## Library use

```python
from caldmm.drivers.hp3458a import HP3458A, Func
from caldmm.drivers.fluke5700a import Fluke5700A

dmm = HP3458A(gpib_address=22, nplc=100, func=Func.DCV)
with Fluke5700A(gpib_address=4) as cal:
    cal.set_output(10.0, "V")    # STBY, OUT 10 V; see the driver for the full API
    cal.operate()
    print(dmm.read_single_sample())
    cal.standby()
```

```python
# Specifications, uncertainty and verdicts with no hardware at all
from caldmm.cal.specs import load_model
spec = load_model("hp3458a")
```

## Repository layout

```
src/caldmm/
  drivers/      gpib.py (backend factory), hp3458a.py, fluke5700a.py, _base.py
  cal/          specs, plan, engine, uncertainty (GUM), verdict (JCGM 106),
                result CSV schema, CAL_RPT? parser, adjustment procedures
  cal/data/     hp3458a.yaml, fluke5700a.yaml, fluke5700a_faults.yaml
  apps/         cal_verify, cal_analyze, fluke5700a_console, fluke5700a_selfcal
  tools/        acal, interactive
  lib/          console prompts, CSV, formatting, signals, stats
docs/
  calibration-howto.md            the verification / adjustment workflow
  3458a/                          programming guide, specification extract,
                                  Cal Manual verification mapping
  5700a/                          console how-to, fault codes, wideband specs
tests/                            offline test suite (no hardware)
```

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

`conftest.py` stubs `Gpib`, `serial`, `pyvisa` and `smbus`, so the suite runs
anywhere. Tests never touch hardware.

## Safety

The 5700A sources up to 1100 V and 2.2 A. `caldmm-cal-verify` asks for
confirmation before any plan that sources ≥ 22 V, proves the wiring at a safe
level before every group, and always returns the calibrator to standby on exit,
including on Ctrl-C. Read the safety sections of the how-to before running a
full plan, and never leave a high-voltage run unattended.

## Disclaimer

The specification tables are transcribed from manufacturer documents and are
tested against the markdown extracts in `docs/`, but verify them against your
own instruments' documentation before relying on a verdict.

## License

MIT. See [`LICENSE`](LICENSE).

"""Tests for the Fluke 5700A calibration-report parser (``CAL_RPT?`` text).

The inputs here are synthetic.  A real report captured from your own
instrument is the best regression fixture for this parser; keep it next to
your instrument records rather than in this repository.
"""

import subprocess
import sys
import textwrap
from datetime import date, datetime
from pathlib import Path

import pytest

from caldmm.cal.calreport import (
    CSV_COLUMNS,
    CalReport,
    ReportKind,
    diff_reports,
    format_summary,
    normalize_text,
    parse_faults,
    parse_report,
    parse_reports,
    to_csv_rows,
)



# --------------------------------------------------------------- the capture

class TestCapture:
    def test_carriage_returns_alone_still_split_lines(self):
        assert normalize_text("a\rb") == "a\nb"


class TestReportDiscovery:
    def test_parse_report_on_text_with_no_report_raises(self):
        with pytest.raises(ValueError):
            parse_report("Fault: 1303: DC Zero Is Needed (Every 30 Days)\n")

    def test_no_report_yields_an_empty_list(self):
        assert parse_reports("1> \nFault: 2516: Couldn't Cal 220V DC Gain\n") == []


# ------------------------------------------------------------------ faults

class TestFaults:
    def test_no_faults_in_clean_text(self):
        assert parse_faults("nothing to see here\n") == []


# -------------------------------------------------------------- robustness

class TestRobustness:
    def test_a_continuation_row_with_no_range_is_warned_not_crashed(self):
        text = textwrap.dedent("""\
            FLUKE CORPORATION   S/N 9999   5700A OUTPUT CALIBRATION
            PRINTED ON  1/02/26 AT 03:04:05 5700A  S/N 9999
                        DC VOLTAGE OUTPUT SHIFTS
            RANGE Point     Zero Shift       Full Scale Shift          Spec   Shift
                                                                      (+/-) (% spec)
                   -FS     0.00003 mV     0.00048 mV   2.16   ppm   9.91 ppm   21.79
            """)
        report = parse_reports(text)[0]
        assert report.shifts == ()
        assert any("continuation row before any range" in w
                   for w in report.warnings)


    def test_empty_and_whitespace_input(self):
        assert parse_reports("") == []
        assert parse_reports("   \n\n\x0c\x00\n") == []
        assert parse_faults("") == []


# -------------------------------------------------------------- invariants


IMPORT_HYGIENE_SCRIPT = textwrap.dedent("""\
    import sys

    BLOCKED = {"serial", "Gpib", "pyvisa", "smbus", "smbus2"}

    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError("blocked for this test: " + fullname)
            return None

    sys.meta_path.insert(0, Blocker())

    import caldmm.cal.calreport as cr

    text = (
        "FLUKE CORPORATION   S/N 9999   5700A OUTPUT CALIBRATION\\n"
        "PRINTED ON  1/02/26 AT 03:04:05 5700A  S/N 9999\\n"
        "Fault: 1303: DC Zero Is Needed (Every 30 Days)\\n"
    )
    reports = cr.parse_reports(text)
    assert len(reports) == 1, reports
    assert reports[0].serial == "9999", reports[0].serial
    assert len(cr.parse_faults(text)) == 1

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal pulled in drivers: " + repr(leaked)
    print("OK")
    """)


def test_calreport_imports_with_no_hardware_libraries():
    """``cal.calreport`` must parse with no interface library importable."""
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "OK"

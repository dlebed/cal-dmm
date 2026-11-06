"""``docs/3458a/3458a-specifications.md`` must agree with ``hp3458a.yaml``.

The 3458A's specification tables are not in the 3458A User's Guide -- its
Appendix A is a redirect to a data sheet -- and ``.gitignore`` excludes every
PDF, so the committed markdown extract is the only in-repository record of
where ``hp3458a.yaml``'s figures came from.  A record nothing checks is a
record that rots, so this module re-parses the extract and compares it, cell by
cell, against the loaded model.

It needs no PDF and no network: the extract is the source of truth here, and
the extract itself was verified against the data sheet when it was written.
"""

import math
import re
from pathlib import Path

import pytest

from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.specs import load_model, select_band

EXTRACT = Path(__file__).resolve().parents[2] / "docs" / "3458a" / "3458a-specifications.md"
INTERVALS = ("24h", "90d", "1y", "2y")

#: ``5.0 (3.5) + 3`` -> the standard-instrument figure; the parenthesised
#: Option 002 value is deliberately not carried by the spec file.
_PAREN = re.compile(r"\([^)]*\)")


@pytest.fixture(scope="module")
def extract():
    assert EXTRACT.is_file(), f"the committed extract is missing: {EXTRACT}"
    return EXTRACT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def spec():
    return load_model("hp3458a")


def _section(text, heading):
    """The extract's text under *heading*, up to the next ``## `` heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end != -1 else len(text)]


def _tables(text, heading):
    """Every markdown table under *heading*, as lists of cell lists."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    body = text[start:end if end != -1 else len(text)]
    tables, current = [], []
    for line in body.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue                      # the ---|--- separator row
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _table_with(text, heading, first_cell):
    for table in _tables(text, heading):
        for row in table:
            if row and row[0] == first_cell:
                return table
    raise AssertionError(f"no table under {heading!r} starting a row with {first_cell!r}")


def _pair(cell):
    """``0.1% + 10`` -> (1000.0 ppm, 10.0 ppm); ``2.5 + 3`` -> (2.5, 3.0)."""
    cleaned = _PAREN.sub(" ", cell)
    left, right = (part.strip() for part in cleaned.split("+"))
    return _ppm(left), _ppm(right)


def _ppm(token):
    token = token.strip()
    if token.endswith("%"):
        return float(token[:-1]) * 1e4
    return float(token)


_SUPERSCRIPT = re.compile(r"[\u00b9\u00b2\u00b3\u2074\u2075\u2076]")


def _pct_pair(cell):
    """A percent-of-reading/percent-of-range cell, normalised to ppm.

    Footnote superscripts ride along on some cells (the ANA 10 mV tempco is
    printed ``0.003 + 0.006`` with a superscript 2); strip them.
    """
    cleaned = _SUPERSCRIPT.sub("", _PAREN.sub(" ", cell))
    left, right = (part.strip() for part in cleaned.split("+"))
    return float(left) * 1e4, float(right) * 1e4


def _row(table, label):
    for row in table:
        if row[0] == label:
            return row
    raise AssertionError(f"row {label!r} not in the extract table")


# --------------------------------------------------------------------- DC V

DCV_ROWS = {"100 mV": 0.1, "1 V": 1.0, "10 V": 10.0, "100 V": 100.0,
            "1000 V⁶": 1000.0}


class TestDcVoltsExtract:
    def test_accuracy_every_cell(self, extract, spec):
        table = _table_with(extract, "## p.12", "100 mV")
        # the ranges table also starts with "100 mV"; pick the accuracy one
        table = [t for t in _tables(extract, "## p.12")
                 if t[0][1].startswith("24 hour")][0]
        ranges = {r.fs: r for r in spec.function(Quantity.DCV).ranges}
        for label, fs in DCV_ROWS.items():
            cells = _row(table, label)[1:5]
            for interval, cell in zip(INTERVALS, cells):
                rdg, rng = _pair(cell)
                terms = ranges[fs].limits[interval]
                assert terms.ppm_reading == pytest.approx(rdg), (label, interval)
                assert terms.ppm_range == pytest.approx(rng), (label, interval)

    def test_with_acal_tempco(self, extract, spec):
        table = [t for t in _tables(extract, "## p.12")
                 if t[0][1] == "Full scale"][0]
        ranges = {r.fs: r for r in spec.function(Quantity.DCV).ranges}
        for label, fs in {"100 mV": 0.1, "1 V": 1.0, "10 V": 10.0,
                          "100 V": 100.0, "1000 V": 1000.0}.items():
            rdg, rng = _pair(_row(table, label)[5])       # "with ACAL" column
            tempco = ranges[fs].tempco
            assert tempco.ppm_reading_per_c == pytest.approx(rdg), label
            assert tempco.ppm_range_per_c == pytest.approx(rng), label

    def test_the_worked_example_is_quoted_and_holds(self, extract, spec):
        """Example 1's arithmetic is the *printed row*, which is `base_terms`.

        The applied limit is larger, because the data sheet's row assumes MATH
        NULL on a range held over 4 minutes and this code base does neither --
        see ``TestNullAndDwellConditions``.
        """
        from caldmm.cal.specs import evaluate_limit
        assert "± 5.5 μV or 0.55 ppm of 10 V" in extract
        result = evaluate_limit(spec, Quantity.DCV, 10.0, interval="24h")
        assert result.base_terms.limit(10.0, 10.0) == pytest.approx(5.5e-6)
        assert result.limit == pytest.approx(8.0e-6)   # + 0.25 ppm of range

    def test_the_range_definition_is_quoted(self, extract):
        # This sentence is what makes `fs` the range NAME on the ac ranges too.
        assert "Range means the name of the scale" in extract
        assert "range does not mean the full-scale reading" in extract


# --------------------------------------------------------------------- Ohms

OHM_ROWS = {"10 Ω": 10.0, "100 Ω": 100.0, "1 kΩ": 1e3, "10 kΩ": 1e4,
            "100 kΩ": 1e5, "1 MΩ": 1e6, "10 MΩ": 1e7, "100 MΩ": 1e8,
            "1 GΩ": 1e9}


class TestResistanceExtract:
    def test_accuracy_every_cell(self, extract, spec):
        table = [t for t in _tables(extract, "## p.17")
                 if t[0][1].startswith("24-hour")][0]
        ranges = {r.fs: r for r in spec.function(Quantity.RES).ranges}
        for label, fs in OHM_ROWS.items():
            for interval, cell in zip(INTERVALS, _row(table, label)[1:5]):
                rdg, rng = _pair(cell)
                terms = ranges[fs].limits[interval]
                assert terms.ppm_reading == pytest.approx(rdg), (label, interval)
                assert terms.ppm_range == pytest.approx(rng), (label, interval)

    def test_two_wire_adder(self, extract, spec):
        printed = re.search(
            r"\*\*24 Hour: (\S+) mΩ\. 90 Day: (\S+) mΩ\. "
            r"1 Year: (\S+) mΩ\. 2 Year: (\S+) mΩ\.\*\*", extract)
        assert printed, "the two-wire offset sentence is missing from the extract"
        two_wire = spec.function(Quantity.RES).connections[Connection.TWO_WIRE]
        for interval, milliohms in zip(INTERVALS, printed.groups()):
            adder = two_wire.adder_per_interval[interval]
            assert adder.abs_floor == pytest.approx(float(milliohms) * 1e-3)


# --------------------------------------------------------------------- DC I

DCI_ROWS = {"100 nA⁶": 1e-7, "1 µA⁶": 1e-6, "10 µA⁶": 1e-5, "100 µA": 1e-4,
            "1 mA": 1e-3, "10 mA": 1e-2, "100 mA": 0.1, "1 A": 1.0}


class TestDcCurrentExtract:
    def test_accuracy_every_cell(self, extract, spec):
        table = [t for t in _tables(extract, "## p.21")
                 if t[0][1].startswith("24-hour")][0]
        ranges = {r.fs: r for r in spec.function(Quantity.DCI).ranges}
        for label, fs in DCI_ROWS.items():
            for interval, cell in zip(INTERVALS, _row(table, label)[1:5]):
                rdg, rng = _pair(cell)
                terms = ranges[fs].limits[interval]
                assert terms.ppm_reading == pytest.approx(rdg), (label, interval)
                assert terms.ppm_range == pytest.approx(rng), (label, interval)

    def test_the_typical_rows_are_flagged_in_the_spec_file(self, extract, spec):
        assert "**Typical accuracy.**" in extract
        ranges = {r.fs: r for r in spec.function(Quantity.DCI).ranges}
        for fs in (1e-7, 1e-6, 1e-5):
            assert "TYPICAL" in (ranges[fs].note or ""), fs


# --------------------------------------------------------------------- AC I

ACI_LABELS = {"100 µA⁴": (1e-4,), "1 mA – 100 mA": (1e-3, 1e-2, 0.1),
              "1 A": (1.0,)}


class TestAcCurrentExtract:
    """p.33: the four specified columns are carried, the typical ones not."""

    def test_accuracy_every_specified_cell(self, extract, spec):
        table = [t for t in _tables(extract, "## p.33")
                 if t[0][1].startswith("10 Hz to 20 Hz")][0]
        headers = table[0]
        ranges = {r.fs: r for r in spec.function(Quantity.ACI).ranges}
        checked = 0
        for row in table[1:]:
            for fs in ACI_LABELS[row[0]]:
                for header, cell in zip(headers[1:], row[1:]):
                    if not cell:
                        continue
                    lo, hi = _band_edges(header)
                    if fs == 1e-4 and lo >= 1000.0:
                        continue
                    probe = min((lo + hi) / 2, 999.0 if fs == 1e-4 else hi)
                    band = select_band(ranges[fs], probe, None)
                    if header.endswith("³"):
                        assert band is None, (row[0], header, "typical")
                        continue
                    assert band is not None, (row[0], header)
                    rdg, rng = _pct_pair(cell)
                    terms = band.limits["1y"]
                    assert terms.ppm_reading == pytest.approx(rdg), (row[0], header)
                    assert terms.ppm_range == pytest.approx(rng), (row[0], header)
                    checked += 1
        assert checked == 4 + 3 * 4 + 4

    def test_the_100ua_range_stops_at_1khz(self, spec):
        (range_spec,) = [r for r in spec.function(Quantity.ACI).ranges
                         if r.fs == 1e-4]
        assert max(b.freq_high_hz for b in range_spec.bands) == 1000.0

    def test_resolution_and_full_scale(self, extract, spec):
        table = _table_with(extract, "## p.33", "100 µA")
        ranges = {r.fs: r for r in spec.function(Quantity.ACI).ranges}
        expected = {"100 µA": (1e-4, 1e-10, 1.2), "1 mA": (1e-3, 1e-9, 1.2),
                    "10 mA": (1e-2, 1e-8, 1.2), "100 mA": (0.1, 1e-7, 1.2),
                    "1 A": (1.0, 1e-6, 1.05)}
        for label, (fs, resolution, overrange) in expected.items():
            assert _row(table, label)
            assert ranges[fs].resolution == pytest.approx(resolution)
            assert ranges[fs].overrange == pytest.approx(overrange)


# --------------------------------------------------------------------- AC V

def _band_edges(header):
    """``1 kHz to 20 kHz³`` -> (1000.0, 20000.0)."""
    cleaned = re.sub(r"[¹²³⁴⁵⁶]", "", header).replace("≤2 MHz ", "").replace(">2 MHz ", "")
    match = re.match(r"^\s*([\d.]+)\s*(Hz|kHz|MHz)?\s+to\s+([\d.]+)\s*(Hz|kHz|MHz)\s*$",
                     cleaned)
    if not match:
        return None
    scale = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, None: 1.0}
    lo = float(match.group(1)) * scale[match.group(2) or match.group(4)]
    hi = float(match.group(3)) * scale[match.group(4)]
    return lo, hi


ACV_LABELS = {
    "10 mV": 0.01, "100 mV – 10V": 0.1, "100 mV – 10 V": 0.1,
    "100 mV-10 V": 0.1, "100 V": 100.0, "1000 V": 1000.0,
}


def _check_acv_table(spec, table, mode, tag):
    headers = table[0]
    ranges = {r.fs: r for r in spec.function(Quantity.ACV).ranges}
    checked = 0
    for row in table[1:]:
        fs = ACV_LABELS.get(row[0])
        if fs is None:
            continue
        for header, cell in zip(headers[1:], row[1:]):
            if not cell:
                continue
            edges = _band_edges(header)
            assert edges, f"{tag}: unparsable band header {header!r}"
            lo, hi = edges
            band = select_band(ranges[fs], (lo + hi) / 2, mode)
            assert band is not None, f"{tag} {row[0]} {header}: no band in the spec file"
            rdg, rng = _pct_pair(cell)
            terms = band.limits["1y"]
            assert terms.ppm_reading == pytest.approx(rdg), (tag, row[0], header)
            assert terms.ppm_range == pytest.approx(rng), (tag, row[0], header)
            checked += 1
    assert checked, f"{tag}: nothing compared"
    return checked


class TestAcVoltsExtract:
    def test_sync_below_2mhz(self, extract, spec):
        table = [t for t in _tables(extract, "## p.25")
                 if t[0][1].startswith("1 Hz to 40 Hz")][0]
        assert _check_acv_table(spec, table, "sync", "SYNC<=2MHz") == 26

    def test_sync_above_2mhz(self, extract, spec):
        table = [t for t in _tables(extract, "## p.25")
                 if t[0][1].startswith("45 Hz to 100 kHz")][0]
        assert _check_acv_table(spec, table, "sync_wide", "SYNC>2MHz") == 11

    def test_ana(self, extract, spec):
        table = [t for t in _tables(extract, "## p.28")
                 if t[0][1].startswith("10 Hz to 20 Hz")][0]
        assert _check_acv_table(spec, table, "ana", "ANA") == 32

    def test_rndm(self, extract, spec):
        table = [t for t in _tables(extract, "## p.31")
                 if t[0][1].startswith("≤2 MHz")][0]
        headers = table[0]
        ranges = {r.fs: r for r in spec.function(Quantity.ACV).ranges}
        checked = 0
        for row in table[1:]:
            fs = ACV_LABELS.get(row[0])
            if fs is None:
                continue
            for header, cell in zip(headers[1:], row[1:]):
                if not cell:
                    continue
                mode = "rndm" if header.startswith("≤2 MHz") else "rndm_wide"
                lo, hi = _band_edges(header)
                band = select_band(ranges[fs], (lo + hi) / 2, mode)
                assert band is not None, (row[0], header)
                rdg, rng = _pct_pair(cell)
                assert band.limits["1y"].ppm_reading == pytest.approx(rdg), (row[0], header)
                assert band.limits["1y"].ppm_range == pytest.approx(rng), (row[0], header)
                checked += 1
        assert checked == 21

    @pytest.mark.parametrize("heading, mode, probe_hz", [
        ("## p.25", "sync", 1000.0),
        ("## p.28", "ana", 1000.0),
        ("## p.30", "rndm", 1000.0),
    ])
    def test_tempco_column(self, extract, spec, heading, mode, probe_hz):
        table = [t for t in _tables(extract, heading)
                 if t[0][1] == "Full scale"][0]
        ranges = {r.fs: r for r in spec.function(Quantity.ACV).ranges}
        labels = {"10 mV": 0.01, "100 mV": 0.1, "1 V": 1.0, "10 V": 10.0,
                  "100 V": 100.0, "1000 V": 1000.0}
        for label, fs in labels.items():
            rdg, rng = _pct_pair(_row(table, label)[4])
            band = select_band(ranges[fs], probe_hz, mode)
            assert band is not None and band.tempco is not None, (mode, label)
            assert band.tempco.ppm_reading_per_c == pytest.approx(rdg), (mode, label)
            assert band.tempco.ppm_range_per_c == pytest.approx(rng), (mode, label)

    def test_volt_hertz_product(self, extract, spec):
        assert "**Volt – Hz product** | **1x10⁸**" in extract
        assert spec.function(Quantity.ACV).volt_hertz_max == 1e8

    def test_the_1000v_full_scale_is_carried_as_max_out(self, extract, spec):
        table = [t for t in _tables(extract, "## p.25")
                 if t[0][1] == "Full scale"][0]
        assert _row(table, "1000 V")[1] == "700.0000"
        kilovolt = [r for r in spec.function(Quantity.ACV).ranges
                    if r.fs == 1000.0][0]
        assert all(b.max_out == 700.0 for b in kilovolt.bands)


_UNITS = {
    "pA": 1e-12, "nA": 1e-9, "µA": 1e-6, "mA": 1e-3, "A": 1.0,
    "nV": 1e-9, "µV": 1e-6, "mV": 1e-3, "V": 1.0,
    "µΩ": 1e-6, "mΩ": 1e-3, "Ω": 1.0,
}


def _quantity_value(cell):
    """``10 nV`` -> 1e-8, ``100 µΩ`` -> 1e-4."""
    number, unit = cell.replace("μ", "µ").split()
    return float(number) * _UNITS[unit]


class TestResolutionColumn:
    """The extract's display columns were the one place nothing compared.

    That is where the 10 kΩ full-scale digit slip hid.  ``Maximum resolution``
    is the display column the spec file *does* carry, so comparing it puts real
    coverage on that half of every range table.
    """

    @pytest.mark.parametrize("heading, quantity, labels", [
        ("## p.12", Quantity.DCV,
         {"100 mV": 0.1, "1 V": 1.0, "10 V": 10.0, "100 V": 100.0,
          "1000 V": 1000.0}),
        ("## p.17", Quantity.RES,
         {"10 Ω": 10.0, "100 Ω": 100.0, "1 kΩ": 1e3, "10 kΩ": 1e4,
          "100 kΩ": 1e5, "1 MΩ": 1e6, "10 MΩ": 1e7, "100 MΩ⁴": 1e8,
          "1 GΩ⁴": 1e9}),
        ("## p.21", Quantity.DCI,
         {"100 nA": 1e-7, "1 µA": 1e-6, "10 µA": 1e-5, "100 µA": 1e-4,
          "1 mA": 1e-3, "10 mA": 1e-2, "100 mA": 0.1, "1 A": 1.0}),
        # The spec file carries the SYNC resolutions for ac volts; ANA and
        # RNDM differ and are recorded as a documented gap.
        ("## p.25", Quantity.ACV,
         {"10 mV": 0.01, "100 mV": 0.1, "1 V": 1.0, "10 V": 10.0,
          "100 V": 100.0, "1000 V": 1000.0}),
    ])
    def test_resolution_matches_the_spec_file(self, extract, spec, heading,
                                              quantity, labels):
        table = [t for t in _tables(extract, heading)
                 if t[0][1] == "Full scale"][0]
        column = next(i for i, h in enumerate(table[0])
                      if h in ("Maximum resolution", "Max resolution"))
        ranges = {r.fs: r for r in spec.function(quantity).ranges}
        for label, fs in labels.items():
            printed = _quantity_value(_row(table, label)[column])
            assert ranges[fs].resolution == pytest.approx(printed), (heading, label)

    def test_full_scale_digit_counts_are_verbatim(self, extract):
        # The data sheet prints the 10 Ω range's full scale with five decimals
        # and the 10 kΩ range's with six.  Both are 12; only the digit strings
        # differ, so no arithmetic check can catch a dropped zero -- but the
        # extract's whole value is being verbatim, so pin the two directly.
        table = [t for t in _tables(extract, "## p.17")
                 if t[0][1] == "Full scale"][0]
        assert _row(table, "10 Ω")[1] == "12.00000"
        assert _row(table, "10 kΩ")[1] == "12.000000"

    def test_the_normalisation_note_is_present(self, extract):
        # The one place the extract is not character-for-character verbatim.
        assert "Numeric cells are verbatim" in extract
        assert "The only normalisation is to *row labels*" in extract


# ---------------------------------------------------------------------------
# The conditions the audit found missing (H09, M75, M76/M03, M77/L108, L109,
# H06, M01/M04, M69).  Every figure below is compared against the extract
# rather than typed twice, so the extract stays the single in-repository
# record and nothing here can drift from it.
# ---------------------------------------------------------------------------

#: Range *names* carry a unit, and each range table's "Full scale" column is
#: printed in that same unit (the 1 kOhm row's full scale is ``1.2000000``,
#: meaning 1.2 kOhm).  Scaling by the label's unit is what turns the printed
#: full scale into an overrange multiple.
_RANGE_NAME_UNITS = {
    "nA": 1e-9, "µA": 1e-6, "mA": 1e-3, "A": 1.0,
    "mV": 1e-3, "V": 1.0,
    "Ω": 1.0, "kΩ": 1e3, "MΩ": 1e6, "GΩ": 1e9,
}


def _range_name(label):
    """``100 mV⁶`` -> (0.1, 1e-3): the range name's value and its unit scale."""
    number, unit = _SUPERSCRIPT.sub("", label).replace("μ", "µ").split()
    scale = _RANGE_NAME_UNITS[unit]
    return float(number) * scale, scale


def _range_at(ranges, fs):
    """The range whose ``fs`` is *fs*, tolerant of decimal round-off.

    ``100 nA`` reconstructs as ``100 * 1e-9 = 1.0000000000000001e-07`` while
    the spec file writes ``1.0e-7``; a dict lookup on the raw float misses.
    """
    for range_spec in ranges:
        if math.isclose(range_spec.fs, fs, rel_tol=1e-9):
            return range_spec
    raise AssertionError(f"no range with fs {fs!r}")


def _full_scale_table(extract, heading):
    return [t for t in _tables(extract, heading) if t[0][1] == "Full scale"][0]


class TestHighVoltageAdder:
    """H09 -- DCV footnote 6, ``12 ppm x (Vin/1000)^2`` for inputs > 100 V.

    The corroboration is Keysight's own 1-year performance test card
    (``3458a-cal-manual.txt:2371-2378``, PDF p.78): its 100 V gain-test limit
    is 1.114 mV against a bare datasheet 1.03 mV (1.082x) and its 1000 V limit
    is 23.96 mV.  23.96 / 22.1 = 1.084 -- the same margin -- while
    23.96 / 10.1 (the limit without the adder) would be 2.37, which no other
    row on the card shows.
    """

    def test_footnote_6_is_in_the_extract(self, extract):
        assert "Add 12 ppm X (Vin / 1000)² additional error for inputs > 100 vV." \
            in extract
        # ...and the accuracy row is the one the footnote marks.
        table = [t for t in _tables(extract, "## p.12")
                 if t[0][1].startswith("24 hour")][0]
        assert _row(table, "1000 V⁶")[0] == "1000 V⁶"

    def test_the_term_is_carried_on_the_1000v_range(self, spec):
        kilovolt = [r for r in spec.function(Quantity.DCV).ranges
                    if r.fs == 1000.0][0]
        for interval in INTERVALS:
            quadratic = kilovolt.limits[interval].quadratic
            assert len(quadratic) == 1, interval
            term = quadratic[0]
            assert term.ppm_at_ref == pytest.approx(12.0)
            assert term.ref == pytest.approx(1000.0)
            assert term.above == pytest.approx(100.0)

    def test_no_other_dc_range_carries_it(self, spec):
        for range_spec in spec.function(Quantity.DCV).ranges:
            if range_spec.fs == 1000.0:
                continue
            for interval in INTERVALS:
                assert range_spec.limits[interval].quadratic == (), range_spec.fs

    @pytest.mark.parametrize("value, interval, expected", [
        # 1 y at 1000 V: 10 ppm x 1000 V + 0.1 ppm x 1000 V + 12 ppm x 1000 V
        #   = 10 mV + 0.1 mV + 12 mV
        (1000.0, "1y", 22.1e-3),
        # 900 V: 9 mV + 0.1 mV + 12e-6 x 0.9^2 x 900 V = 9 + 0.1 + 8.748 mV
        (900.0, "1y", 17.848e-3),
        # 500 V: 5 mV + 0.1 mV + 12e-6 x 0.5^2 x 500 V = 5 + 0.1 + 1.5 mV
        (500.0, "1y", 6.6e-3),
        # 24 h at 1000 V: 2.5 + 0.1 + 12 mV
        (1000.0, "24h", 14.6e-3),
        # The threshold itself is outside the footnote's "inputs > 100 V".
        (100.0, "1y", 1.1e-3),
    ])
    def test_the_limit_on_the_1000v_range(self, spec, value, interval, expected):
        from caldmm.cal.specs import evaluate_limit
        result = evaluate_limit(spec, Quantity.DCV, value, range_fs=1000.0,
                                interval=interval)
        assert result.limit == pytest.approx(expected)

    def test_the_cal_manual_card_corroborates_it(self, spec):
        from caldmm.cal.specs import evaluate_limit
        card_1000v, card_100v = 0.02396, 0.001114     # cal manual p.78
        with_adder = evaluate_limit(spec, Quantity.DCV, 1000.0,
                                    range_fs=1000.0, interval="1y").limit
        hundred = evaluate_limit(spec, Quantity.DCV, 100.0,
                                 range_fs=100.0, interval="1y").limit
        assert hundred == pytest.approx(1.03e-3)
        # The card's margin over the datasheet is the same on both rows only
        # when the 12 mV adder is in the 1000 V figure.
        assert card_1000v / with_adder == pytest.approx(card_100v / hundred,
                                                        rel=0.01)
        # Without it the 1000 V limit would be 10.1 mV, i.e. 2.37x -- an
        # outlier no other row on the card shows.
        assert card_1000v / 10.1e-3 > 2.0

    def test_describe_prints_the_term(self, spec):
        kilovolt = [r for r in spec.function(Quantity.DCV).ranges
                    if r.fs == 1000.0][0]
        text = kilovolt.limits["1y"].describe("V")
        assert "12 ppm rdg x (|x|/1000 V)^2 above 100 V" in text

    def test_the_spec_file_no_longer_says_it_is_uncarried(self):
        yaml_text = (Path(__file__).resolve().parents[2] / "src" / "caldmm"
                     / "cal" / "data" / "hp3458a.yaml").read_text(encoding="utf-8")
        assert "not carried here" not in yaml_text
        assert "Not expressible as a three-term line" not in yaml_text


class TestFactoryTraceability:
    """M75 -- the per-function traceability adders the data sheet states."""

    @pytest.mark.parametrize("heading, quantity, expected", [
        ("## p.12", Quantity.DCV, 2.0),
        ("## p.17", Quantity.RES, 3.0),
        ("## p.21", Quantity.DCI, 5.0),
        ("## p.25", Quantity.ACV, 2.0),
        ("## p.33", Quantity.ACI, 5.0),
    ])
    def test_the_figure_matches_the_extract(self, extract, spec, heading,
                                            quantity, expected):
        # The p.12 footnote is printed "Add 2 ppm pf reading" in the data
        # sheet; the extract reproduces the typo, so accept either spelling.
        printed = re.search(r"Add (\d+) ?ppm (?:of|pf) reading additional error "
                            r"for factory traceability",
                            _section(extract, heading))
        assert printed, f"{heading}: the traceability sentence is missing"
        assert float(printed.group(1)) == expected
        traceability = spec.function(quantity).traceability
        assert traceability is not None, quantity
        assert traceability.ppm_reading == pytest.approx(expected)

    def test_the_absolute_versus_relative_paragraph_is_quoted(self, extract):
        assert "All 3458A accuracy specifications are relative to" in extract
        assert "For DCV, 2 ppm is the traceability error from the factory" in extract
        # ...and that the factory figure is only a default.
        assert ("When you recalibrate the 3458A, your actual traceability error "
                "will depend upon the errors from your calibration standards."
                in extract)

    def test_traceability_never_enters_a_tolerance(self, spec):
        from caldmm.cal.specs import evaluate_limit
        # 10 V, 1 year: 8 ppm x 10 V + 0.05 ppm x 10 V = 80.5 uV printed, plus
        # the 0.25 ppm-of-range regime adder = 83.0 uV applied.  Neither
        # carries the 2 ppm (20 uV) of traceability.
        result = evaluate_limit(spec, Quantity.DCV, 10.0, interval="1y")
        assert result.base_terms.limit(10.0, 10.0) == pytest.approx(80.5e-6)
        assert result.limit == pytest.approx(83.0e-6)

    def test_every_function_states_one(self, spec):
        for quantity in (Quantity.DCV, Quantity.DCI, Quantity.RES, Quantity.ACV):
            assert spec.function(quantity).traceability is not None, quantity
            assert "certificate" in spec.function(quantity).traceability.note

    def test_the_header_no_longer_calls_them_excluded(self):
        yaml_text = (Path(__file__).resolve().parents[2] / "src" / "caldmm"
                     / "cal" / "data" / "hp3458a.yaml").read_text(encoding="utf-8")
        assert "Those belong in an uncertainty budget, not in a tolerance." \
            not in yaml_text


class TestNullAndDwellConditions:
    """M76 / M03 -- the DCV table's own conditions, and the adders when they fail."""

    def test_the_conditions_are_in_the_extract(self, extract):
        section = _section(extract, "## p.12")
        assert "For fixed range (> 4 min.), MATH NULL and Tcal ± 1 °C" in section
        assert ("Without MATH NULL, add 0.15 ppm of range to 10 V, 0.7 ppm of "
                "range to 0.1V.") in section
        assert ("add 0.25 ppm of range to 10 V,\n   1.7 ppm of range to 1 V and "
                "17 ppm of range to 0.1 V.") in section

    def test_the_function_records_the_conditions(self, spec):
        note = spec.function(Quantity.DCV).note
        assert note is not None
        for condition in ("MATH NULL", "4 min", "Tcal"):
            assert condition in note, condition

    @pytest.mark.parametrize("fs, ppm_range", [
        # The "without MATH NULL AND fixed range < 4 minutes" column, which is
        # the regime every reading in this code base is taken in: the adapter
        # never sends MATH NULL and re-selects the range for every point.
        (0.1, 17.0),
        (1.0, 1.7),
        (10.0, 0.25),
    ])
    def test_each_range_carries_the_adder_the_datasheet_gives_it(
            self, spec, fs, ppm_range):
        adder = [r for r in spec.function(Quantity.DCV).ranges
                 if r.fs == fs][0].regime_adder
        assert adder is not None, fs
        assert adder.ppm_range == pytest.approx(ppm_range), fs
        assert adder.ppm_reading == 0.0 and adder.abs_floor == 0.0, fs
        assert "MATH NULL" in adder.note and "4 min" in adder.note, fs

    def test_the_ranges_the_datasheet_gives_no_adder_for_have_none(self, spec):
        for fs in (100.0, 1000.0):
            range_spec = [r for r in spec.function(Quantity.DCV).ranges
                          if r.fs == fs][0]
            assert range_spec.regime_adder is None, fs
            assert "no MATH NULL adder" in range_spec.note, fs

    @pytest.mark.parametrize("value, fs, interval, printed, applied", [
        # printed row -> applied limit, per the extract's p.12 accuracy table
        # plus the footnote-5 adder at :454-456.
        (0.1, 0.1, "1y", 1.2e-6, 2.9e-6),      # 0.9 + 0.3 uV, + 1.7 uV
        (1.0, 1.0, "1y", 8.3e-6, 10.0e-6),     # 8.0 + 0.3 uV, + 1.7 uV
        (10.0, 10.0, "1y", 80.5e-6, 83.0e-6),  # 80 + 0.5 uV, + 2.5 uV
        (10.0, 10.0, "24h", 5.5e-6, 8.0e-6),   # 5.0 + 0.5 uV, + 2.5 uV
        (100.0, 100.0, "1y", 1.03e-3, 1.03e-3),   # no adder on this range
    ])
    def test_the_applied_limit_carries_it_and_the_printed_row_does_not(
            self, spec, value, fs, interval, printed, applied):
        from caldmm.cal.specs import evaluate_limit
        result = evaluate_limit(spec, Quantity.DCV, value, range_fs=fs,
                                interval=interval)
        assert result.base_terms.limit(value, fs) == pytest.approx(printed)
        assert result.limit == pytest.approx(applied)

    def test_the_range_tables_still_match_the_extract_untouched(self, spec):
        # The adder is applied by evaluate_limit, never folded into `limits:`,
        # so TestDcVoltsExtract's cell-by-cell comparison stays meaningful.
        hundred_mv = [r for r in spec.function(Quantity.DCV).ranges
                      if r.fs == 0.1][0]
        assert hundred_mv.limits["1y"].ppm_range == pytest.approx(3.0)
        assert hundred_mv.limits["1y"].regime == ()

    def test_it_is_named_in_the_description(self, spec):
        from caldmm.cal.specs import evaluate_limit
        text = evaluate_limit(spec, Quantity.DCV, 10.0,
                              interval="1y").terms.describe("V")
        assert text == ("8 ppm rdg + 0.05 ppm rng "
                        "+0.25 ppm rng (no MATH NULL, fixed range < 4 min)")


class TestTemperatureSemantics:
    """M77 / L108 -- one datum for the dc tables, one window, stated once."""

    def test_the_footnotes_are_in_the_extract(self, extract):
        section = _section(extract, "## p.12")
        assert "1. Additional error from Tcal or last ACAL ± 1 °C." in section
        assert "2. Additional error from Tcal ± 5 °C." in section
        # The ac tables are referenced to the last ACAL, not to Tcal.
        assert ("Additional error beyond ± 1ºC, but within + 5 ºC of last ACAL"
                in _section(extract, "## p.25"))

    def test_the_window_is_relative_to_tcal(self, spec):
        temperature = spec.temperature
        assert temperature.relative_to == "cal_temp"
        assert temperature.half_width_c == pytest.approx(5.0)
        assert temperature.half_width_for("24h") == pytest.approx(1.0)
        for interval in ("90d", "1y", "2y"):
            assert temperature.half_width_for(interval) == pytest.approx(5.0)

    def test_no_window_is_invented_without_a_reference(self, spec):
        assert spec.temperature.spec_window_c is None
        assert spec.temperature.window_for("1y") is None
        assert spec.temperature.within_window(45.0, "1y") is True

    def test_the_window_resolves_from_cal_temp(self, spec):
        # CAL? 59 on this unit reads 23.0 C.
        assert spec.temperature.window_for("1y", reference_c=23.0) == (18.0, 28.0)
        assert spec.temperature.window_for("24h", reference_c=23.0) == (22.0, 24.0)
        assert spec.temperature.within_window(29.0, "1y", reference_c=23.0) is False

    def test_the_header_states_both_regimes(self):
        yaml_text = (Path(__file__).resolve().parents[2] / "src" / "caldmm"
                     / "cal" / "data" / "hp3458a.yaml").read_text(encoding="utf-8")
        assert "`temperature:` is therefore absent" not in yaml_text
        assert "beyond Tcal +/- 1 C but within +5 C of the last ACAL" not in yaml_text
        assert "CAL? 59" in yaml_text


class TestHighFrequencyTempco:
    """L109 -- the outside-Tcal +/- 5 C, 2-10 MHz coefficient."""

    @pytest.mark.parametrize("heading", ["## p.27", "## p.32"])
    def test_the_table_is_in_the_extract(self, extract, heading):
        section = _section(extract, heading)
        assert "For outside Tcal ± 5 °C" in section
        table = _tables(extract, heading)[0]
        assert _row(table, "10 mV – 1 V")[1:3] == ["0.02", "0.08"]
        assert _row(table, "10 V – 1000 V")[1:3] == ["0.08", "0.08"]

    def test_the_spec_file_declares_it_uncarried_with_the_figures(self):
        yaml_text = (Path(__file__).resolve().parents[2] / "src" / "caldmm"
                     / "cal" / "data" / "hp3458a.yaml").read_text(encoding="utf-8")
        assert "High frequency temperature coefficient" in yaml_text
        assert "0.02 %/C" in yaml_text and "0.08 %/C" in yaml_text
        assert "p.27" in yaml_text and "p.32" in yaml_text

    def test_the_wide_bands_point_at_it(self, spec):
        # Every band a *_wide mode can reach above 2 MHz names the coefficient,
        # so the engine can surface it on the point rather than losing it.
        for fs in (0.01, 0.1, 1.0, 10.0):
            range_spec = [r for r in spec.function(Quantity.ACV).ranges
                          if r.fs == fs][0]
            for band in range_spec.bands:
                if not set(band.modes) & {"sync_wide", "rndm_wide"}:
                    continue
                if band.freq_high_hz <= 2.0e6:
                    continue
                assert band.note and "outside Tcal" in band.note, (fs, band.freq_low_hz)

    def test_the_carried_band_tempcos_are_the_acal_referenced_ones(self, spec):
        # Unchanged: the band tempco slot is the "beyond +/- 1 C of last ACAL"
        # column, a different regime from the high-frequency figure.
        ten_mv = [r for r in spec.function(Quantity.ACV).ranges
                  if r.fs == 0.01][0]
        band = select_band(ten_mv, 3.0e6, "sync_wide")
        assert band.tempco.ppm_reading_per_c == pytest.approx(20.0)


class TestOhmsTestCurrent:
    """H06 (data side) -- the current each ohms range sources."""

    def test_every_range_matches_the_extract(self, extract, spec):
        table = _full_scale_table(extract, "## p.17")
        column = table[0].index("Current source¹")
        ranges = {r.fs: r for r in spec.function(Quantity.RES).ranges}
        for label, fs in {"10 Ω": 10.0, "100 Ω": 100.0, "1 kΩ": 1e3,
                          "10 kΩ": 1e4, "100 kΩ": 1e5, "1 MΩ": 1e6,
                          "10 MΩ": 1e7, "100 MΩ⁴": 1e8, "1 GΩ⁴": 1e9}.items():
            printed = _quantity_value(_row(table, label)[column])
            assert ranges[fs].test_current_a == pytest.approx(printed), label

    def test_the_two_points_the_5700a_pairing_turns_on(self, spec):
        # The 19 ohm and 190 ohm catalogue values are read on these ranges,
        # and 1 mA is an eighth of the 5700A's 8 mA full-spec load minimum.
        ranges = {r.fs: r for r in spec.function(Quantity.RES).ranges}
        assert ranges[100.0].test_current_a == pytest.approx(1e-3)
        assert ranges[1000.0].test_current_a == pytest.approx(1e-3)
        assert ranges[10000.0].test_current_a == pytest.approx(100e-6)

    def test_only_ohms_states_one(self, spec):
        for quantity in (Quantity.DCV, Quantity.DCI, Quantity.ACV):
            for range_spec in spec.function(quantity).ranges:
                assert range_spec.test_current_a is None, (quantity, range_spec.fs)


class TestOverrange:
    """M01 / M04 (data side) -- what the meter may be *configured* on.

    The multiple is the data sheet's own "Full scale" column divided by the
    range name: 120.00000 mV on the 100 mV range is 1.2, and 1050.00000 V on
    the 1000 V range is 1.05.  The 3458A User's Guide RANGE table agrees
    (``3458a-user-guide.txt:10236-10260``, PDF p.273).
    """

    @pytest.mark.parametrize("heading, quantity, labels", [
        ("## p.12", Quantity.DCV,
         ["100 mV", "1 V", "10 V", "100 V", "1000 V"]),
        ("## p.17", Quantity.RES,
         ["10 Ω", "100 Ω", "1 kΩ", "10 kΩ", "100 kΩ", "1 MΩ", "10 MΩ",
          "100 MΩ⁴", "1 GΩ⁴"]),
        ("## p.21", Quantity.DCI,
         ["100 nA", "1 µA", "10 µA", "100 µA", "1 mA", "10 mA", "100 mA",
          "1 A"]),
        ("## p.25", Quantity.ACV,
         ["10 mV", "100 mV", "1 V", "10 V", "100 V", "1000 V"]),
    ])
    def test_overrange_is_the_printed_full_scale(self, extract, spec, heading,
                                                 quantity, labels):
        table = _full_scale_table(extract, heading)
        ranges = spec.function(quantity).ranges
        for label in labels:
            fs, unit = _range_name(label)
            printed = float(_row(table, label)[1]) * unit
            expected = printed / fs
            range_spec = _range_at(ranges, fs)
            if expected < 1.0:
                # The ac 1000 V range reads only to 700 V rms: there is no
                # overrange to claim, and max_out already caps it.
                assert range_spec.overrange is None, (heading, label)
                continue
            assert range_spec.overrange == pytest.approx(expected), (heading, label)

    def test_the_two_five_percent_ranges(self, spec):
        dcv = {r.fs: r for r in spec.function(Quantity.DCV).ranges}
        dci = {r.fs: r for r in spec.function(Quantity.DCI).ranges}
        assert dcv[1000.0].overrange == pytest.approx(1.05)
        assert dci[1.0].overrange == pytest.approx(1.05)

    def test_overrange_does_not_widen_a_tolerance(self, spec):
        from caldmm.cal.specs import evaluate_limit, select_range
        function = spec.function(Quantity.DCV)
        # 11 V is *configurable* on the 10 V range but *specified* on 100 V.
        assert select_range(function, 11.0).fs == 100.0
        assert select_range(function, 11.0, for_meter_config=True).fs == 10.0
        # 100 V range, 1 year: 10 ppm x 11 V + 0.3 ppm x 100 V = 140 uV.
        assert evaluate_limit(spec, Quantity.DCV, 11.0,
                              interval="1y").limit == pytest.approx(140e-6)


class TestModeReadingFloors:
    """M69 (data side) -- each SETACV technique's own validity floor."""

    def test_the_footnotes_are_in_the_extract(self, extract):
        assert ("Specifications apply full scale to 10% of full scale"
                in _section(extract, "## p.25"))
        assert ("Specifications apply full scale to ¼ full scale"
                in _section(extract, "## p.28"))
        assert ("Specifications apply from full scale to 5% of full scale"
                in _section(extract, "## p.31"))
        # ...and the 10 mV range's full scale, which the fractions are of.
        assert _row(_full_scale_table(extract, "## p.25"), "10 mV")[1] == "12.00000"

    @pytest.mark.parametrize("mode, expected", [
        # 10 % of 12 mV = 1.2 mV = 0.12 x the 10 mV range name (p.25 fn 2)
        ("sync", 0.12),
        ("sync_wide", 0.12),
        # 1/4 of 12 mV = 3.0 mV = 0.30 (p.28 fn 2, the 10 mV row's own)
        ("ana", 0.30),
        # 5 % of 12 mV = 0.6 mV = 0.06 (p.31 fn 1)
        ("rndm", 0.06),
        ("rndm_wide", 0.06),
    ])
    def test_the_10mv_range_states_each_floor(self, spec, mode, expected):
        ten_mv = [r for r in spec.function(Quantity.ACV).ranges
                  if r.fs == 0.01][0]
        assert ten_mv.min_reading_frac_for(mode) == pytest.approx(expected)

    def test_the_unpinned_floor_stays_the_most_restrictive(self, spec):
        ten_mv = [r for r in spec.function(Quantity.ACV).ranges
                  if r.fs == 0.01][0]
        assert ten_mv.min_reading_frac == pytest.approx(0.30)
        assert ten_mv.min_reading_frac_for(None) == pytest.approx(0.30)

    def test_a_sync_point_below_the_ana_floor_is_now_specified(self, spec):
        from caldmm.cal.specs import evaluate_limit
        # 2.2 mV is 0.22 of the 10 mV range: below ANA's 0.30 floor but well
        # inside SYNC's 0.12.  p.25 SYNC 1 kHz-20 kHz: 0.03 % + 0.011 %
        #   300 ppm x 2.2 mV + 110 ppm x 10 mV = 0.66 uV + 1.1 uV = 1.76 uV
        result = evaluate_limit(spec, Quantity.ACV, 2.2e-3, range_fs=0.01,
                                freq_hz=1000.0, mode="sync", interval="1y")
        assert result is not None
        assert result.limit == pytest.approx(1.76e-6)
        # ...and ANA still says nothing there.
        assert evaluate_limit(spec, Quantity.ACV, 2.2e-3, range_fs=0.01,
                              freq_hz=1000.0, mode="ana", interval="1y") is None


class TestOption002:
    """The parenthesised p.12 figures, carried as ``options: "002"``."""

    def test_every_parenthesised_cell(self, extract, spec):
        table = [t for t in _tables(extract, "## p.12")
                 if t[0][1].startswith("24 hour")][0]
        opt = spec.with_option("002")
        ranges = {r.fs: r for r in opt.function(Quantity.DCV).ranges}
        base = {r.fs: r for r in spec.function(Quantity.DCV).ranges}
        checked = 0
        for label, fs in DCV_ROWS.items():
            row = _row(table, label)
            for interval, cell in zip(INTERVALS, row[1:5]):
                inner = re.search(r"\(([\d.]+)\)", cell)
                terms = ranges[fs].limits[interval]
                if inner is None:
                    assert terms == base[fs].limits[interval], (label, interval)
                    continue
                assert terms.ppm_reading == pytest.approx(float(inner.group(1)))
                assert terms.ppm_reading < base[fs].limits[interval].ppm_reading
                assert terms.ppm_range == base[fs].limits[interval].ppm_range
                checked += 1
        assert checked == 15

    def test_it_is_not_applied_by_default(self, spec):
        assert spec.applied_options == ()
        assert spec.with_option("2").applied_options == ("002",)

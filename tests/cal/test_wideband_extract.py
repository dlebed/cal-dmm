"""Audit fluke5700a.yaml's WBV function against the MS5 wideband tables.

The YAML's per-band limits are *derived*: absolute-uncertainty row plus
amplitude-flatness row, floors added linearly (see
docs/5700a/fluke5700a-wideband-specifications.md).  This test re-derives every band
of every range from the two source tables transcribed below — typed in
independently of the generator that produced the YAML — so a slip in either
transcription or arithmetic fails loudly, cell by cell.

Source: 5700A/5720A Operators Manual Supplement issue 5 (1/09), change #1
(40695), replacing OM p.1-42.
"""

import pytest

from caldmm.cal.specs import load_model

# (range_id, fs, resolution, flatness column, {interval: (% output, floor uV)})
ABSOLUTE = [
    ("WB1_1MV", 1.1e-3, 1e-8, "1.1mV",
     {"24h": (0.4, 0.4), "90d": (0.5, 0.4), "180d": (0.6, 0.4),
      "1y": (0.8, 2.0)}),
    ("WB3_3MV", 3.3e-3, 1e-8, "3mV",
     {"24h": (0.4, 1.0), "90d": (0.45, 1.0), "180d": (0.5, 1.0),
      "1y": (0.7, 3.0)}),
    # The 11 mV row is where [MS5] differs from the 1996 print: 90 d is
    # 0.35 (not 0.3) and 180 d is 0.5 (not 0.35).
    ("WB11MV", 11e-3, 1e-7, ">3mV",
     {"24h": (0.2, 4.0), "90d": (0.35, 4.0), "180d": (0.5, 4.0),
      "1y": (0.7, 8.0)}),
    ("WB33MV", 33e-3, 1e-7, ">3mV",
     {"24h": (0.2, 10.0), "90d": (0.3, 10.0), "180d": (0.45, 10.0),
      "1y": (0.6, 16.0)}),
    ("WB110MV", 0.11, 1e-6, ">3mV",
     {"24h": (0.2, 40.0), "90d": (0.3, 40.0), "180d": (0.45, 40.0),
      "1y": (0.6, 40.0)}),
    ("WB330MV", 0.33, 1e-6, ">3mV",
     {"24h": (0.2, 100.0), "90d": (0.25, 100.0), "180d": (0.35, 100.0),
      "1y": (0.5, 100.0)}),
    ("WB1_1V", 1.1, 1e-5, ">3mV",
     {"24h": (0.2, 400.0), "90d": (0.25, 400.0), "180d": (0.35, 400.0),
      "1y": (0.5, 400.0)}),
    ("WB3_5V", 3.5, 1e-5, ">3mV",
     {"24h": (0.15, 500.0), "90d": (0.2, 500.0), "180d": (0.3, 500.0),
      "1y": (0.4, 500.0)}),
]

# The YAML's band plan: (f_lo, f_hz, {column: (% adder, floor adder uV)}).
# 30 Hz - 500 kHz is the absolute table's own band (no adder); the 120 k -
# 1.2 M and 1.2 M - 2 M flatness rows carry identical figures and are merged.
FLATNESS = [
    (10.0, 30.0,
     {"1.1mV": (0.3, 0.0), "3mV": (0.3, 0.0), ">3mV": (0.3, 0.0)}),
    (30.0, 500e3,
     {"1.1mV": (0.0, 0.0), "3mV": (0.0, 0.0), ">3mV": (0.0, 0.0)}),
    (500e3, 2e6,
     {"1.1mV": (0.2, 3.0), "3mV": (0.1, 3.0), ">3mV": (0.1, 3.0)}),
    (2e6, 10e6,
     {"1.1mV": (0.4, 3.0), "3mV": (0.3, 3.0), ">3mV": (0.2, 3.0)}),
    (10e6, 20e6,
     {"1.1mV": (0.6, 3.0), "3mV": (0.5, 3.0), ">3mV": (0.4, 3.0)}),
    (20e6, 30e6,
     {"1.1mV": (1.5, 15.0), "3mV": (1.5, 3.0), ">3mV": (1.0, 3.0)}),
]

INTERVALS = ("24h", "90d", "180d", "1y")

# Footnote [1] of the flatness table (supplement line 66, PDF p.2):
#
#   "[1] For output voltages < 50 % of full range in the 33 mV, 110 mV,
#    330 mV, 1.1 V, and 3.5 V ranges, add 0.1 % to the amplitude flatness
#    specification."
#
# It adds to the *flatness* specification, so it reaches exactly the bands
# whose limits use a flatness row -- every band except 30 Hz - 500 kHz, which
# is the band the absolute-uncertainty table is stated for.
FOOTNOTE_1_RANGES = ("WB33MV", "WB110MV", "WB330MV", "WB1_1V", "WB3_5V")
FOOTNOTE_1_ABSENT_BAND = (30.0, 500e3)
FOOTNOTE_1_FRACTION = 0.5
FOOTNOTE_1_PPM = 1000.0          # 0.1 % of output


@pytest.fixture(scope="module")
def wbv():
    function = load_model("fluke5700a").function("WBV")
    assert function is not None, "fluke5700a.yaml declares no WBV function"
    return function


class TestStructure:
    def test_all_eight_ranges_in_order(self, wbv):
        assert [r.range_id for r in wbv.ranges] == [
            row[0] for row in ABSOLUTE]

    def test_full_scales_and_resolutions(self, wbv):
        for row, range_spec in zip(ABSOLUTE, wbv.ranges):
            assert range_spec.fs == pytest.approx(row[1]), row[0]
            assert range_spec.resolution == pytest.approx(row[2]), row[0]

    def test_the_minimum_output_is_300uV(self, wbv):
        for range_spec in wbv.ranges:
            assert range_spec.min_out == pytest.approx(300e-6), \
                range_spec.range_id

    def test_a_50_ohm_system(self, wbv):
        for range_spec in wbv.ranges:
            assert range_spec.source_impedance_ohm == pytest.approx(50.0)

    def test_band_edges(self, wbv):
        edges = [(band.freq_low_hz, band.freq_high_hz)
                 for band in wbv.ranges[0].bands]
        assert edges == [(f_lo, f_hi) for f_lo, f_hi, _ in FLATNESS]


class TestEveryCell:
    @pytest.mark.parametrize("row", ABSOLUTE, ids=lambda row: row[0])
    def test_bands_are_absolute_plus_flatness(self, wbv, row):
        range_id, _fs, _res, column, absolute = row
        range_spec = next(r for r in wbv.ranges if r.range_id == range_id)
        assert len(range_spec.bands) == len(FLATNESS)
        for (f_lo, f_hi, adders), band in zip(FLATNESS, range_spec.bands):
            pct_add, floor_add_uV = adders[column]
            for interval in INTERVALS:
                pct, floor_uV = absolute[interval]
                expected_ppm = (pct + pct_add) * 1e4
                expected_floor = (floor_uV + floor_add_uV) * 1e-6
                terms = band.limits[interval]
                assert terms.ppm_reading == pytest.approx(expected_ppm), (
                    range_id, f_lo, f_hi, interval)
                assert terms.abs_floor == pytest.approx(expected_floor), (
                    range_id, f_lo, f_hi, interval)


class TestFootnoteOne:
    """The conditional +0.1 % below half range, cell by cell."""

    @pytest.mark.parametrize("row", ABSOLUTE, ids=lambda row: row[0])
    def test_only_the_five_named_ranges_carry_it(self, wbv, row):
        range_id = row[0]
        range_spec = next(r for r in wbv.ranges if r.range_id == range_id)
        carried = any(band.below_frac_adder is not None
                      for band in range_spec.bands)
        assert carried == (range_id in FOOTNOTE_1_RANGES), range_id

    @pytest.mark.parametrize("range_id", FOOTNOTE_1_RANGES)
    def test_every_flatness_band_of_a_named_range_carries_it(self, wbv,
                                                             range_id):
        range_spec = next(r for r in wbv.ranges if r.range_id == range_id)
        for band in range_spec.bands:
            edges = (band.freq_low_hz, band.freq_high_hz)
            adder = band.below_frac_adder
            if edges == FOOTNOTE_1_ABSENT_BAND:
                assert adder is None, (range_id, edges)
                continue
            assert adder is not None, (range_id, edges)
            assert adder.below_frac == pytest.approx(FOOTNOTE_1_FRACTION)
            assert adder.terms.ppm_reading == pytest.approx(FOOTNOTE_1_PPM)
            assert adder.terms.ppm_range == 0.0
            assert adder.terms.abs_floor == 0.0

    def test_the_range_level_key_is_not_used(self, wbv):
        # A range-level adder would also reach the 30 Hz - 500 kHz band, which
        # does not use a flatness row at all.  Band level, always.
        for range_spec in wbv.ranges:
            assert range_spec.below_frac_adder is None, range_spec.range_id

    @pytest.mark.parametrize("range_id, fs", [
        ("WB33MV", 33e-3), ("WB110MV", 0.11), ("WB330MV", 0.33),
        ("WB1_1V", 1.1), ("WB3_5V", 3.5),
    ])
    def test_the_condition_is_strict_at_half_scale(self, wbv, range_id, fs):
        range_spec = next(r for r in wbv.ranges if r.range_id == range_id)
        band = next(b for b in range_spec.bands
                    if (b.freq_low_hz, b.freq_high_hz) == (10e6, 20e6))
        assert band.below_frac_adder.applies(0.5 * fs - 1e-9, fs)
        assert not band.below_frac_adder.applies(0.5 * fs, fs)

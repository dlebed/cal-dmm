"""Golden tests for the 3458A and 5700A specification files.

Every limit assertion below writes the arithmetic out longhand from the source
document, so that a transcription slip shows up as a failing multiplication
rather than as a plausible-looking number.  The citations name the page or
section the figures came from.

Sources:
  * 3458A -- Keysight 3458A data sheet (5965-4971E), pages 12-31; extract in
    docs/3458a/3458a-specifications.md
  * 5700A -- Fluke 5700A/5720A Series II Operator and Remote Programming
    manuals, sections 11 and 18
"""

import textwrap

import pytest

from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.specs import (
    IntervalBasis,
    LimitsAuthority,
    PairRole,
    Role,
    available_models,
    evaluate_limit,
    SpecError,
    load_model,
    parse_model_text,
    select_band,
    select_range,
)

MODELS = ["fluke5700a", "hp3458a"]


@pytest.fixture(scope="module")
def dmm3458():
    return load_model("hp3458a")


@pytest.fixture(scope="module")
def cal5700():
    return load_model("fluke5700a")


# ------------------------------------------------------------------ shipping

class TestEveryShippedFileLoads:

    def test_every_advertised_model_actually_loads(self):
        # `for m in available_models(): load_model(m)` is what a plan
        # generator, an analyzer and a --list-models flag all write.
        for model_id in available_models():
            assert load_model(model_id).model_id == model_id


# --------------------------------------------------------------------- 3458A

class TestHP3458AHeader:
    def test_role_and_authority(self, dmm3458):
        assert dmm3458.role is Role.METER
        # A meter cannot report its own accuracy: this file is the authority.
        assert dmm3458.limits_authority is LimitsAuthority.FILE

    def test_interval_basis_is_acal_not_calendar(self, dmm3458):
        # The whole point of the field: every 3458A interval other than 24h is
        # gated on "within 24-hours and +/-1 C of last ACAL", not on a date.
        assert dmm3458.interval_basis is IntervalBasis.SINCE_ACAL

    def test_overload_sentinel(self, dmm3458):
        assert dmm3458.overload_sentinel == 9.9e37
        assert dmm3458.is_overload(9.9e37)
        assert dmm3458.is_overload(-9.9e37)
        assert not dmm3458.is_overload(1e35)
        assert not dmm3458.is_overload(1000.0)

    def test_intervals(self, dmm3458):
        assert dmm3458.intervals == ("24h", "90d", "1y", "2y")
        assert dmm3458.default_interval == "1y"

    def test_functions(self, dmm3458):
        assert set(dmm3458.quantities) == {
            Quantity.DCV, Quantity.DCI, Quantity.RES, Quantity.ACV,
            Quantity.ACI}


class TestHP3458ADcVolts:
    """The printed rows are ``base_terms``; ``limit`` adds the regime adder.

    Every dc-volts figure on p.12 is written for MATH NULL on a range held over
    four minutes, and this code base does neither, so the data sheet's
    "without MATH NULL and for fixed range less than 4 minutes" adder
    (``3458a-datasheet.txt:454-456``) applies to every reading: 0.25 ppm of
    range on 10 V, 1.7 on 1 V, 17 on 0.1 V.  The tests below pin both numbers,
    because the printed row is what the extract audits and the applied limit is
    what a verdict uses.
    """

    def test_the_datasheet_own_worked_example(self, dmm3458):
        # Data sheet p.9 EXAMPLE 1, quoted verbatim:
        #   "The 24-hour accuracy specification for a 10 V DC measurement on
        #    the 10 V range is 0.5 ppm + 0.05 ppm ...
        #    (0.5/1,000,000 x 10 V) + (0.05/1,000,000 x 10 V) = +/- 5.5 uV"
        # This is also what proves "ppm of range" is ppm of the RANGE NAME and
        # not of the printed 12.0000000 V full scale.
        result = evaluate_limit(dmm3458, Quantity.DCV, 10.0, interval="24h")
        assert result.range_fs == 10.0
        assert result.base_terms.limit(10.0, 10.0) == pytest.approx(5.5e-6)
        assert result.limit == pytest.approx(8.0e-6)      # + 0.25 ppm of range

    def test_1v_one_year(self, dmm3458):
        # p.12, 1 V range, 1 year: 8 + 0.3
        #   8 ppm x 1 V + 0.3 ppm x 1 V = 8.0 uV + 0.3 uV = 8.3 uV
        result = evaluate_limit(dmm3458, Quantity.DCV, 1.0, interval="1y")
        assert result.base_terms.limit(1.0, 1.0) == pytest.approx(8.3e-6)
        assert result.limit == pytest.approx(10.0e-6)     # + 1.7 ppm of range

    def test_zero_is_exactly_the_range_term(self, dmm3458):
        # p.12, 100 mV range, 90 day: 5.0 + 3
        #   5.0 ppm x 0 + 3 ppm x 0.1 V = 0.3 uV
        result = evaluate_limit(dmm3458, Quantity.DCV, 0.0, interval="90d")
        assert result.range_fs == 0.1
        assert result.base_terms.limit(0.0, 0.1) == pytest.approx(0.3e-6)
        # The regime adder is ppm of range, so it survives at zero: + 1.7 uV.
        assert result.limit == pytest.approx(2.0e-6)

    def test_two_year_is_the_loosest(self, dmm3458):
        # p.12, 10 V range: 24h 0.5+0.05 ... 2y 14+0.05
        #   14 ppm x 10 V + 0.05 ppm x 10 V = 140 uV + 0.5 uV = 140.5 uV
        result = evaluate_limit(dmm3458, Quantity.DCV, 10.0, interval="2y")
        assert result.base_terms.limit(10.0, 10.0) == pytest.approx(140.5e-6)
        assert result.limit == pytest.approx(143.0e-6)    # + 0.25 ppm of range

    def test_input_impedance_switches_at_100v(self, dmm3458):
        ranges = {r.fs: r.input_impedance_ohm for r in
                  dmm3458.function(Quantity.DCV).ranges}
        assert ranges[10.0] == 1e10       # ">10 GOhm"
        assert ranges[100.0] == 1e7       # 10 MOhm +/-1 %


class TestHP3458ADcCurrent:
    def test_10ma_one_year(self, dmm3458):
        # p.21, 10 mA range, 1 year: 20 + 5
        #   20 ppm x 10 mA + 5 ppm x 10 mA = 200 nA + 50 nA = 250 nA
        result = evaluate_limit(dmm3458, Quantity.DCI, 0.01, interval="1y")
        assert result.range_fs == pytest.approx(0.01)
        assert result.limit == pytest.approx(250e-9)

    def test_1a_24_hour(self, dmm3458):
        # p.21, 1 A range, 24 hour: 100 + 10
        #   100 ppm x 1 A + 10 ppm x 1 A = 100 uA + 10 uA = 110 uA
        result = evaluate_limit(dmm3458, Quantity.DCI, 1.0, interval="24h")
        assert result.limit == pytest.approx(110e-6)


class TestHP3458AResistance:
    def test_10k_four_wire_one_year(self, dmm3458):
        # p.17, 10 kOhm range, 1 year: 10 + 0.5, for OHMF with OCOMP ON
        #   10 ppm x 10 kOhm + 0.5 ppm x 10 kOhm = 0.1 Ohm + 0.005 Ohm
        result = evaluate_limit(dmm3458, Quantity.RES, 10000.0,
                                connection="4w", interval="1y")
        assert result.func == "OHMF"
        assert result.limit == pytest.approx(0.105)

    def test_two_wire_adder_is_per_interval_not_flat(self, dmm3458):
        # p.18: "add the following offset errors to the four-wire ohms
        # accuracy.  24 Hour: 50 mOhm.  90 Day: 150 mOhm.  1 Year: 250 mOhm.
        # 2 Year: 500 mOhm."  A single figure would be ten times too loose at
        # 24 hours, which passes a meter that should fail.
        #   24h: 2 ppm x 10 k + 0.2 ppm x 10 k + 0.05 = 0.02 + 0.002 + 0.05
        day = evaluate_limit(dmm3458, Quantity.RES, 10000.0,
                             connection="2w", interval="24h")
        assert day.func == "OHM"
        assert day.limit == pytest.approx(0.072)
        #   1y:  10 ppm x 10 k + 0.5 ppm x 10 k + 0.25 = 0.1 + 0.005 + 0.25
        year = evaluate_limit(dmm3458, Quantity.RES, 10000.0,
                              connection="2w", interval="1y")
        assert year.limit == pytest.approx(0.355)
        assert day.adder.abs_floor == pytest.approx(0.05)
        assert year.adder.abs_floor == pytest.approx(0.25)

    def test_100_ohm_24_hour(self, dmm3458):
        # p.17, 100 Ohm range, 24 hour: 3 + 3
        #   3 ppm x 100 + 3 ppm x 100 = 0.3 mOhm + 0.3 mOhm = 0.6 mOhm
        result = evaluate_limit(dmm3458, Quantity.RES, 100.0,
                                connection="4w", interval="24h")
        assert result.limit == pytest.approx(0.6e-3)

    def test_the_percent_rows_normalise_to_ppm(self, dmm3458):
        # p.17, 1 GOhm range, 1 year: 0.5 % + 10 ppm
        #   0.005 x 1e9 + 10 ppm x 1e9 = 5e6 + 1e4 = 5.01e6 Ohm
        result = evaluate_limit(dmm3458, Quantity.RES, 1.0e9,
                                connection="4w", interval="1y")
        assert result.terms.ppm_reading == pytest.approx(5000.0)
        assert result.limit == pytest.approx(5.01e6)


class TestHP3458AAcVoltsModeAxis:
    """SETACV is a real axis: the same point has three different tolerances."""

    def test_1khz_1v_in_each_technique(self, dmm3458):
        # p.25 SYNC, "100 mV - 10 V" row, 1 kHz to 20 kHz: 0.014 % + 0.002 %
        #   140 ppm x 1 V + 20 ppm x 1 V = 140 uV + 20 uV = 160 uV
        sync = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                              mode="sync", interval="1y")
        assert sync.limit == pytest.approx(160e-6)
        # p.28 ANA, "100 mV - 10 V" row, 100 Hz to 20 kHz: 0.02 % + 0.01 %
        #   200 ppm x 1 V + 100 ppm x 1 V = 200 uV + 100 uV = 300 uV
        ana = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                             mode="ana", interval="1y")
        assert ana.limit == pytest.approx(300e-6)
        # p.31 RNDM, "100 mV - 10 V" row, 20 Hz to 100 kHz: 0.08 % + 0.002 %
        #   800 ppm x 1 V + 20 ppm x 1 V = 800 uV + 20 uV = 820 uV
        rndm = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                              mode="rndm", interval="1y")
        assert rndm.limit == pytest.approx(820e-6)
        # The data sheet's own ranking of the three techniques (p.24).
        assert sync.limit < ana.limit < rndm.limit

    def test_the_mode_reaches_the_apply_block(self, dmm3458):
        result = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                                mode="sync", interval="1y")
        assert result.apply["ac_method"] == "SYNC"
        assert result.mode.id == "sync"

    def test_ac_band_is_deliberately_not_pinned(self, dmm3458):
        # ACBAND's low edge sets the 3458A's reading time and the data sheet
        # constrains only the high edge, so no ac_band is asserted here.
        for mode in dmm3458.function(Quantity.ACV).modes:
            assert "ac_band" not in mode.apply

    def test_ana_has_no_band_above_2mhz(self, dmm3458):
        # p.24: the analog technique's bandwidth stops at 2 MHz.
        assert evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=4.0e6,
                              mode="ana", interval="1y") is None
        # SYNC in its >2 MHz regime does have one (p.25 lower table):
        #   1 MHz to 4 MHz, "100 mV - 10 V": 4 % + 0.07 %
        #   0.04 x 1 V + 700 ppm x 1 V = 40 mV + 0.7 mV = 40.7 mV
        wide = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=2.5e6,
                              mode="sync_wide", interval="1y")
        assert wide.limit == pytest.approx(40.7e-3)

    def test_shared_band_boundary_with_no_role_takes_the_higher_band(self, dmm3458):
        # Band edges are inclusive, so 4 MHz is in both the 1-4 MHz and the
        # 4-8 MHz rows.  With no role given the caller gets the historical
        # convention, the higher band: 4 MHz to 8 MHz, "100 mV - 10 V":
        #   4 % + 0.08 % = 0.04 x 1 V + 800 ppm x 1 V = 40 mV + 0.8 mV
        #   = 40.8 mV
        edge = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=4.0e6,
                              mode="sync_wide", interval="1y")
        assert edge.band.freq_low_hz == 4.0e6
        assert edge.limit == pytest.approx(40.8e-3)

    def test_a_dut_takes_the_tighter_row_at_a_shared_edge(self, dmm3458):
        # 3458a-datasheet.txt:990 (PDF p.25), SYNC "100 mV - 10 V" row:
        #   "40 Hz to 1 kHz  0.007 + 0.002" and "1 kHz to 20 kHz  0.014 + 0.002"
        # 1 kHz is printed in both.  As the DUT the tighter row applies:
        #   70 ppm x 1 V + 20 ppm x 1 V = 70 uV + 20 uV = 90 uV
        dut = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                             mode="sync", interval="1y", role=PairRole.DUT)
        assert (dut.band.freq_low_hz, dut.band.freq_high_hz) == (40.0, 1000.0)
        assert dut.limit == pytest.approx(90e-6)
        # As the reference the looser row is the conservative one:
        #   140 ppm x 1 V + 20 ppm x 1 V = 160 uV
        ref = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                             mode="sync", interval="1y",
                             role=PairRole.REFERENCE)
        assert ref.band.freq_low_hz == 1000.0
        assert ref.limit == pytest.approx(160e-6)
        # 4 MHz, sync_wide: 1-4 MHz is 4 % + 0.07 % = 40.7 mV, tighter than
        # the 4-8 MHz row's 40.8 mV.
        wide = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=4.0e6,
                              mode="sync_wide", interval="1y",
                              role=PairRole.DUT)
        assert wide.limit == pytest.approx(40.7e-3)

    def test_10mv_range_carries_its_own_row(self, dmm3458):
        # p.25 SYNC "10 mV" row, 40 Hz to 1 kHz: 0.02 % + 0.011 %
        #   200 ppm x 10 mV + 110 ppm x 10 mV = 2.0 uV + 1.1 uV = 3.1 uV
        result = evaluate_limit(dmm3458, Quantity.ACV, 0.01, freq_hz=100.0,
                                mode="sync", interval="1y")
        assert result.range_fs == pytest.approx(0.01)
        assert result.limit == pytest.approx(3.1e-6)

    def test_1000v_range_stops_at_700v_rms(self, dmm3458):
        # p.25 "Full scale" column: the 1000 V ac range reads to 700.0000 V.
        # 500 V at 100 Hz, SYNC 40 Hz - 1 kHz row: 0.04 % + 0.002 %
        #   400 ppm x 500 V + 20 ppm x 1000 V = 0.2 V + 0.02 V = 0.22 V
        ok = evaluate_limit(dmm3458, Quantity.ACV, 500.0, freq_hz=100.0,
                            range_fs=1000.0, mode="sync", interval="1y")
        assert ok.limit == pytest.approx(0.22)
        assert evaluate_limit(dmm3458, Quantity.ACV, 750.0, freq_hz=100.0,
                              range_fs=1000.0, mode="sync", interval="1y") is None

    def test_volt_hertz_ceiling(self, dmm3458):
        # p.30 "Maximum input": Volt-Hz product 1x10^8.
        # 100 V x 300 kHz = 3e7, inside; 100 V x 2 MHz = 2e8, outside.
        assert evaluate_limit(dmm3458, Quantity.ACV, 100.0, freq_hz=3.0e5,
                              mode="sync", interval="1y") is not None
        assert evaluate_limit(dmm3458, Quantity.ACV, 100.0, freq_hz=2.0e6,
                              mode="sync", interval="1y") is None

    def test_below_the_specified_fraction_of_range(self, dmm3458):
        # The 10 mV range's floor is now stated per mode (M69): the most
        # restrictive is still ANA's 1/4 of the 12 mV full scale = 3 mV = 0.30
        # of the range name, and it is what an unpinned evaluation gets...
        ten_mv = [r for r in dmm3458.function(Quantity.ACV).ranges
                  if r.fs == 0.01][0]
        assert ten_mv.min_reading_frac_for(None) == pytest.approx(0.30)
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.002, freq_hz=1000.0,
                              mode="ana", interval="1y") is None
        # ...but SYNC is specified to 10 % of full scale = 1.2 mV = 0.12, so a
        # 2 mV SYNC reading is specified and must not be dropped (p.25
        # footnote 2, 3458a-datasheet.txt:999).  0.03 % rdg + 0.011 % rng
        #   300 ppm x 2 mV + 110 ppm x 10 mV = 0.6 uV + 1.1 uV = 1.7 uV
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.002, freq_hz=1000.0,
                              mode="sync", interval="1y").limit \
            == pytest.approx(1.7e-6)
        # Below SYNC's own floor it goes back to unspecified.
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.001, freq_hz=1000.0,
                              mode="sync", interval="1y") is None


# ---------------------------------------------------------------------- 1281


# --------------------------------------------------------------------- 5700A

class TestFluke5700AHeader:
    def test_role_and_authority(self, cal5700):
        assert cal5700.role is Role.SOURCE
        # UNCERT? computes the real figure in firmware; these tables are a
        # cross-check, and saying so in the data is the only way a report can.
        assert cal5700.limits_authority is LimitsAuthority.INSTRUMENT

    def test_a_source_has_no_reading_sentinel(self, cal5700):
        # Explicitly null, not silently absent -- the loader requires the key.
        assert cal5700.overload_sentinel is None
        assert not cal5700.is_overload(1e30)
        assert cal5700.is_overload(float("nan"))

    def test_intervals_match_cal_intv(self, cal5700):
        # CAL_INTV accepts 1, 90, 180 and 365 days, and nothing else.
        assert cal5700.intervals == ("24h", "90d", "180d", "1y")
        assert cal5700.interval_basis is IntervalBasis.SINCE_CAL


class TestFluke5700AEnvelope:
    """The envelope is the point of this file; check it against the driver."""

    def _ids(self, spec, quantity):
        return [r.range_id for r in spec.function(quantity).ranges]

    def test_dc_volts_range_ids(self, cal5700):
        from caldmm.drivers.fluke5700a import DC_VOLTS_RANGES
        assert (sorted(self._ids(cal5700, Quantity.DCV))
                == sorted(r.range_id for r in DC_VOLTS_RANGES))

    def test_ac_volts_range_ids(self, cal5700):
        from caldmm.drivers.fluke5700a import AC_VOLTS_RANGES
        assert (sorted(self._ids(cal5700, Quantity.ACV))
                == sorted(r.range_id for r in AC_VOLTS_RANGES))

    def test_dc_current_range_ids(self, cal5700):
        from caldmm.drivers.fluke5700a import DC_CURRENT_RANGES
        assert (sorted(self._ids(cal5700, Quantity.DCI))
                == sorted(r.range_id for r in DC_CURRENT_RANGES))

    def test_ac_current_range_ids(self, cal5700):
        from caldmm.drivers.fluke5700a import AC_CURRENT_RANGES
        assert (sorted(self._ids(cal5700, Quantity.ACI))
                == sorted(r.range_id for r in AC_CURRENT_RANGES))

    def test_resistance_catalogue_is_17_values_plus_a_short(self, cal5700):
        from caldmm.drivers.fluke5700a import RESISTANCE_VALUES
        ranges = cal5700.function(Quantity.RES).ranges
        assert len(ranges) == 18
        assert sorted(r.range_id for r in ranges) == sorted(
            rid for _, rid in RESISTANCE_VALUES)
        # The short is a genuine 0 ohm range, not a fake small one.
        short = [r for r in ranges if r.range_id == "OHM0"][0]
        assert short.fs == 0.0

    def test_resistance_values_match_the_driver(self, cal5700):
        from caldmm.drivers.fluke5700a import RESISTANCE_VALUES
        by_id = {r.range_id: r.fs for r in cal5700.function(Quantity.RES).ranges}
        for value, range_id in RESISTANCE_VALUES:
            assert by_id[range_id] == pytest.approx(value)

    def test_every_resistance_is_characterized(self, cal5700):
        # OUT? returns a characterized value up to 500 ppm from nominal; the
        # readback, not the request, is the reference.
        assert all(r.characterized
                   for r in cal5700.function(Quantity.RES).ranges)

    def test_four_wire_everywhere_except_100_megohm(self, cal5700):
        for r in cal5700.function(Quantity.RES).ranges:
            has_4w = Connection.FOUR_WIRE in r.connections
            assert has_4w is (r.range_id != "OHM100M"), r.range_id

    def test_rcomp_only_at_19k_and_below(self, cal5700):
        for r in cal5700.function(Quantity.RES).ranges:
            has_rcomp = Connection.TWO_WIRE_RCOMP in r.connections
            assert has_rcomp is (r.fs <= 19.0e3), r.range_id

    def test_dc_1100v_is_the_only_dc_range_with_a_minimum(self, cal5700):
        mins = {r.range_id: r.min_out for r in
                cal5700.function(Quantity.DCV).ranges}
        assert mins["DC1100V"] == 100.0
        assert all(v == 0.0 for k, v in mins.items() if k != "DC1100V")

    def test_ac_minimum_output_is_ten_percent_of_range(self, cal5700):
        for r in cal5700.function(Quantity.ACV).ranges:
            assert r.min_out == pytest.approx(0.1 * r.fs), r.range_id

    def test_the_9_microamp_exception(self, cal5700):
        # AC 220 uA is 9 uA, not 10 % of range -- the documented exception.
        mins = {r.range_id: r.min_out for r in
                cal5700.function(Quantity.ACI).ranges}
        assert mins["AC220UA"] == pytest.approx(9e-6)
        assert mins["AC2_2MA"] == pytest.approx(2.2e-4)

    def test_source_impedance_on_the_220mv_range(self, cal5700):
        # 50 ohm into a 10 MOhm meter is 5 ppm -- larger than the 220 mV
        # range's own 0.75 uV floor at 220 mV output (3.4 ppm).
        by_id = {r.range_id: r for r in cal5700.function(Quantity.DCV).ranges}
        assert by_id["DC220MV"].source_impedance_ohm == 50.0
        assert by_id["DC2_2V"].source_impedance_ohm is None

    def test_volt_hertz_ceiling(self, cal5700):
        assert cal5700.function(Quantity.ACV).volt_hertz_max == 2.2e7
        # 220 V at 200 kHz is 4.4e7 V-Hz -- fault 820 territory.
        assert evaluate_limit(cal5700, Quantity.ACV, 220.0, freq_hz=2.0e5,
                              interval="1y") is None
        # 220 V at 50 kHz is 1.1e7, inside the envelope.
        assert evaluate_limit(cal5700, Quantity.ACV, 220.0, freq_hz=5.0e4,
                              interval="1y") is not None

    def test_1100v_ac_window_and_250v_cap(self, cal5700):
        # 15 Hz - 1 kHz only, and 250 V maximum from 15 to 50 Hz.
        assert evaluate_limit(cal5700, Quantity.ACV, 200.0, freq_hz=20.0,
                              range_fs=1100.0, interval="1y") is not None
        assert evaluate_limit(cal5700, Quantity.ACV, 300.0, freq_hz=20.0,
                              range_fs=1100.0, interval="1y") is None
        # 300 V is fine once above 50 Hz...
        assert evaluate_limit(cal5700, Quantity.ACV, 300.0, freq_hz=400.0,
                              range_fs=1100.0, interval="1y") is not None
        # ...but the range does not exist at all above 1 kHz or below 15 Hz.
        assert evaluate_limit(cal5700, Quantity.ACV, 300.0, freq_hz=2000.0,
                              range_fs=1100.0, interval="1y") is None
        assert evaluate_limit(cal5700, Quantity.ACV, 300.0, freq_hz=12.0,
                              range_fs=1100.0, interval="1y") is None

    def test_a_role_cannot_pick_a_row_that_cannot_make_the_output(self, cal5700):
        """50 Hz is the shared edge of the 250 V-capped row and the full one.

        The 15-50 Hz row is by far the looser (460 vs 90 ppm at 1 year), but it
        stops at 250 V, so at 1000 V it is not a candidate at all: a role
        chooses among the rows that can actually be used here, and can never
        turn a specified point into NO_SPEC for one side of the comparison.
        """
        # 90 ppm x 1000 V + 4 mV = 90 mV + 4 mV = 94 mV, whatever the role
        for role in (None, PairRole.DUT, PairRole.REFERENCE):
            result = evaluate_limit(cal5700, Quantity.ACV, 1000.0, freq_hz=50.0,
                                    range_fs=1100.0, interval="1y", role=role)
            assert result is not None
            assert result.band.freq_low_hz == 50.0
            assert result.limit == pytest.approx(94e-3)
        # At 200 V both rows can make the output, so there the role does
        # decide: 460 ppm x 200 V + 20 mV = 112 mV for the reference against
        # 90 ppm x 200 V + 4 mV = 22 mV for the DUT.
        ref = evaluate_limit(cal5700, Quantity.ACV, 200.0, freq_hz=50.0,
                             range_fs=1100.0, interval="1y",
                             role=PairRole.REFERENCE)
        assert (ref.band.freq_low_hz, ref.limit) == (15.0, pytest.approx(112e-3))
        dut = evaluate_limit(cal5700, Quantity.ACV, 200.0, freq_hz=50.0,
                             range_fs=1100.0, interval="1y", role=PairRole.DUT)
        assert (dut.band.freq_low_hz, dut.limit) == (50.0, pytest.approx(22e-3))

    def test_ac_2_2a_starts_at_20hz(self, cal5700):
        # The function's other ranges start at 10 Hz; this one does not.
        assert evaluate_limit(cal5700, Quantity.ACI, 1.0, freq_hz=15.0,
                              interval="1y") is None
        assert evaluate_limit(cal5700, Quantity.ACI, 1.0, freq_hz=50.0,
                              interval="1y") is not None


class TestFluke5700ALimitCrossCheck:
    """Spot checks of the transcribed tables -- the cross-check against UNCERT?."""

    def test_dc_10v_one_year(self, cal5700):
        # 18.3, 11 V range, 1 year, 99 %: 8 ppm + 4 uV
        #   8 ppm x 10 V + 4 uV = 80 uV + 4 uV = 84 uV
        result = evaluate_limit(cal5700, Quantity.DCV, 10.0, interval="1y")
        assert result.range_spec.range_id == "DC11V"
        assert result.limit == pytest.approx(84e-6)

    def test_dc_1v_24_hour(self, cal5700):
        # 18.3, 2.2 V range, 24 hours: 3.5 ppm + 1.2 uV
        #   3.5 ppm x 1 V + 1.2 uV = 3.5 uV + 1.2 uV = 4.7 uV
        result = evaluate_limit(cal5700, Quantity.DCV, 1.0, interval="24h")
        assert result.range_spec.range_id == "DC2_2V"
        assert result.limit == pytest.approx(4.7e-6)

    def test_dc_zero_is_the_floor_alone(self, cal5700):
        # There is no ppm-of-range term on this instrument at all: at zero
        # output the whole specification is the absolute floor.
        #   220 mV range, 1 year: 9 ppm + 0.8 uV -> 0.8 uV
        result = evaluate_limit(cal5700, Quantity.DCV, 0.0, interval="1y")
        assert result.range_spec.range_id == "DC220MV"
        assert result.limit == pytest.approx(0.8e-6)
        assert result.terms.ppm_range == 0.0

    def test_dc_current_10ma(self, cal5700):
        # 18.6, 22 mA range, 1 year: 60 ppm + 100 nA
        #   60 ppm x 10 mA + 100 nA = 600 nA + 100 nA = 700 nA
        result = evaluate_limit(cal5700, Quantity.DCI, 0.01, interval="1y")
        assert result.range_spec.range_id == "DC22MA"
        assert result.limit == pytest.approx(700e-9)

    def test_dc_current_100ma_floor_is_microamps_not_nanoamps(self, cal5700):
        # 18.6, 220 mA range, 24 hours: 55 ppm + 1 uA.  The floor column
        # switches from nA to uA at this row; getting it wrong is a factor of
        # 1000.
        #   55 ppm x 100 mA + 1 uA = 5.5 uA + 1 uA = 6.5 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 0.1, interval="24h")
        assert result.range_spec.range_id == "DC220MA"
        assert result.limit == pytest.approx(6.5e-6)

    def test_ac_volts_1v_1khz(self, cal5700):
        # 18.4, 2.2 V range, 40 Hz - 20 kHz, 1 year: 85 ppm + 7 uV
        #   85 ppm x 1 V + 7 uV = 85 uV + 7 uV = 92 uV
        result = evaluate_limit(cal5700, Quantity.ACV, 1.0, freq_hz=1000.0,
                                interval="1y")
        assert result.range_spec.range_id == "AC2_2V"
        assert result.limit == pytest.approx(92e-6)

    def test_ac_volts_220v_floor_is_millivolts(self, cal5700):
        # 18.4, 220 V range: the floor column is in mV on this row and the
        # 1100 V row only.
        #   40 Hz - 20 kHz, 1 year: 90 ppm + 1 mV
        #   90 ppm x 200 V + 1 mV = 18 mV + 1 mV = 19 mV
        result = evaluate_limit(cal5700, Quantity.ACV, 200.0, freq_hz=1000.0,
                                interval="1y")
        assert result.range_spec.range_id == "AC220V"
        assert result.limit == pytest.approx(19e-3)

    def test_ac_current_100ma(self, cal5700):
        # 18.7, 220 mA range, 40 Hz - 1 kHz, 1 year: 180 ppm + 4 uA
        #   180 ppm x 100 mA + 4 uA = 18 uA + 4 uA = 22 uA
        result = evaluate_limit(cal5700, Quantity.ACI, 0.1, freq_hz=100.0,
                                interval="1y")
        assert result.range_spec.range_id == "AC220MA"
        assert result.limit == pytest.approx(22e-6)

    def test_ac_current_below_the_9_microamp_floor(self, cal5700):
        assert evaluate_limit(cal5700, Quantity.ACI, 5e-6, freq_hz=100.0,
                              interval="1y") is None

    def test_resistance_10k(self, cal5700):
        # 18.5, 10 kOhm, 1 year, 99 %: 14 ppm of the characterized value
        #   14 ppm x 10 kOhm = 0.14 Ohm
        result = evaluate_limit(cal5700, Quantity.RES, 10000.0,
                                connection="4w", interval="1y")
        assert result.range_spec.range_id == "OHM10K"
        assert result.limit == pytest.approx(0.14)

    def test_resistance_1_ohm_24_hour(self, cal5700):
        # 18.5, 1 Ohm, 24 hours: 85 ppm -> 85 uOhm
        result = evaluate_limit(cal5700, Quantity.RES, 1.0,
                                connection="4w", interval="24h")
        assert result.range_spec.range_id == "OHM1"
        assert result.limit == pytest.approx(85e-6)

    def test_the_short_is_an_absolute_floor(self, cal5700):
        # 18.5: "0 (short) ... 50 uOhm" at every interval.  A ppm-of-reading
        # spec on a 0 ohm output would be identically zero, which is why this
        # row is an abs_floor and why fs may be 0 here.
        result = evaluate_limit(cal5700, Quantity.RES, 0.0,
                                connection="4w", interval="1y")
        assert result.range_spec.range_id == "OHM0"
        assert result.limit == pytest.approx(50e-6)

    def test_100_megohm_has_no_four_wire_spec(self, cal5700):
        assert evaluate_limit(cal5700, Quantity.RES, 100.0e6,
                              connection="4w", interval="1y") is None
        # 18.5, 100 MOhm, 1 year: 130 ppm -> 130 ppm x 1e8 = 13000 Ohm
        two_wire = evaluate_limit(cal5700, Quantity.RES, 100.0e6,
                                  connection="2w", interval="1y")
        assert two_wire.limit == pytest.approx(13000.0)

    def test_rcomp_is_refused_above_19k(self, cal5700):
        assert evaluate_limit(cal5700, Quantity.RES, 100.0e3,
                              connection="2w_rcomp", interval="1y") is None
        assert evaluate_limit(cal5700, Quantity.RES, 19.0e3,
                              connection="2w_rcomp", interval="1y") is not None


# ------------------------------------------------ H11: two-wire compensation

# (range_id, nominal, 1-year 4-wire limit, the range's two-wire adder in ohms)
#
# 5700a-operators-manual-1996.txt:1960-2020 (PDF p.49), "Resistance Secondary
# Performance Specifications", column "Two-Wire Adder / Active Compensation
# [4] / Lead Resistance 0.1 Ohm, 1 Ohm / +/-mOhm".  The 0.1 Ohm-lead column is
# the one carried (this bench's 5440A-7002 low-thermal leads are well under
# 1 Ohm, and with the 5700A as DUT the smaller adder is the conservative
# limit); the 0 - 190 Ohm rows read "2 + 4uV/Im" there, and 4 uV / Im is
# 0.5 mOhm at that group's IL = 8 mA, the largest it can be inside the
# full-spec load window.  The 1 Ohm column, for a bench with heavier leads,
# is 4.5 / 15 / 15 / 60 / 120 mOhm.
TWO_WIRE_ADDERS = [
    ("OHM0",     0.0,       50e-6,       2.5e-3),
    ("OHM1",     1.0,       110e-6,      2.5e-3),
    ("OHM1_9",   1.9,       209e-6,      2.5e-3),
    ("OHM10",    10.0,      330e-6,      2.5e-3),
    ("OHM19",    19.0,      589e-6,      2.5e-3),
    ("OHM100",   100.0,     2e-3,        2.5e-3),
    ("OHM190",   190.0,     3.8e-3,      2.5e-3),
    ("OHM1K",    1000.0,    15e-3,       10e-3),
    ("OHM1_9K",  1900.0,    28.5e-3,     10e-3),
    ("OHM10K",   10000.0,   0.14,        50e-3),
    ("OHM19K",   19000.0,   0.266,       100e-3),
]


class TestFluke5700ATwoWireAdder:
    """The two-wire active-compensation adder, per range.

    ``5700a-operators-manual-1996.txt:1952`` (PDF p.48): "Specifications apply
    to displayed value. 4-wire connections, except 100 MOhm."  The two-wire
    figure is a separate column of the secondary table, so crediting a
    ``2w_rcomp`` point with the four-wire number is up to 20x too tight at
    1 Ohm.
    """

    @pytest.mark.parametrize("range_id, nominal, four_wire, adder",
                             TWO_WIRE_ADDERS,
                             ids=[row[0] for row in TWO_WIRE_ADDERS])
    def test_rcomp_is_the_four_wire_figure_plus_the_adder(
            self, cal5700, range_id, nominal, four_wire, adder):
        result = evaluate_limit(cal5700, Quantity.RES, nominal,
                                connection="2w_rcomp", interval="1y")
        assert result.range_spec.range_id == range_id
        assert result.limit == pytest.approx(four_wire + adder)
        assert result.adder.abs_floor == pytest.approx(adder)

    @pytest.mark.parametrize("range_id, nominal, four_wire, adder",
                             TWO_WIRE_ADDERS,
                             ids=[row[0] for row in TWO_WIRE_ADDERS])
    def test_four_wire_is_untouched(self, cal5700, range_id, nominal,
                                    four_wire, adder):
        result = evaluate_limit(cal5700, Quantity.RES, nominal,
                                connection="4w", interval="1y")
        assert result.limit == pytest.approx(four_wire)
        assert result.adder is None

    def test_one_ohm_rcomp_is_the_headline_case(self, cal5700):
        # 110 ppm x 1 Ohm = 110 uOhm four-wire; the adder is 2 mOhm of lead
        # plus 4 uV / 8 mA = 0.5 mOhm, so 2.61 mOhm -- 24x the four-wire
        # figure, and the file used to return the four-wire figure.
        result = evaluate_limit(cal5700, Quantity.RES, 1.0,
                                connection="2w_rcomp", interval="1y")
        assert result.limit == pytest.approx(2.61e-3)

    def test_the_adder_does_not_move_with_the_interval(self, cal5700):
        # The secondary table has no interval axis: one adder, four intervals.
        for interval in ("24h", "90d", "180d", "1y"):
            result = evaluate_limit(cal5700, Quantity.RES, 10000.0,
                                    connection="2w_rcomp", interval=interval)
            assert result.adder.abs_floor == pytest.approx(50e-3), interval

    def test_ten_kilohm_rcomp(self, cal5700):
        # 14 ppm x 10 kOhm = 0.14 Ohm, + 50 mOhm = 0.19 Ohm -- the figure the
        # audit finding quotes for 0.1 Ohm leads.
        result = evaluate_limit(cal5700, Quantity.RES, 10000.0,
                                connection="2w_rcomp", interval="1y")
        assert result.limit == pytest.approx(0.19)

    def test_plain_two_wire_keeps_the_four_wire_figure_and_says_why(
            self, cal5700):
        # The manual states no uncompensated two-wire adder: the four-wire
        # figure holds at the terminals and the lead resistance is the
        # operator's.  Keeping the connection silent would hide that.
        function = cal5700.function(Quantity.RES)
        two_wire = function.connections[Connection.TWO_WIRE]
        assert two_wire.adder is None
        assert two_wire.note is not None
        assert "lead" in two_wire.note.lower()
        assert evaluate_limit(cal5700, Quantity.RES, 10000.0,
                              connection="2w", interval="1y").limit == \
            pytest.approx(0.14)

    def test_the_two_wire_only_hundred_megohm_gains_nothing(self, cal5700):
        # 100 MOhm is the one value the primary table is *not* four-wire for,
        # so its own figure already is the two-wire figure.
        result = evaluate_limit(cal5700, Quantity.RES, 100.0e6,
                                connection="2w", interval="1y")
        assert result.limit == pytest.approx(13000.0)
        assert result.adder is None


# ------------------------------------------------- H06: load-current window

# (range_id, IL, IU, IMAX, under-current unit, four-wire K, its unit,
#  two-wire-comp K, over-current unit, four-wire over-current K)
#
# 5700a-operators-manual-1996.txt:1977-2009 (PDF p.49) for IL/IU/IMAX and
# :2037-2071 (PDF p.50) for the derating constants.
LOAD_CURRENTS = [
    ("OHM0",     8e-3,    500e-3,  500e-3, "mA", 0.3,   "mohm", 4.4,    None, None),
    ("OHM1",     8e-3,    100e-3,  700e-3, "mA", 300.0, "ppm",  4.4,    "mA", 4e-5),
    ("OHM1_9",   8e-3,    100e-3,  500e-3, "mA", 160.0, "ppm",  4.4,    "mA", 1.5e-4),
    ("OHM10",    8e-3,    11e-3,   220e-3, "mA", 30.0,  "ppm",  4.4,    "mA", 1.6e-3),
    ("OHM19",    8e-3,    11e-3,   160e-3, "mA", 16.0,  "ppm",  4.4,    "mA", 3e-3),
    ("OHM100",   8e-3,    11e-3,   70e-3,  "mA", 3.5,   "ppm",  4.4,    "mA", 1e-2),
    ("OHM190",   8e-3,    11e-3,   50e-3,  "mA", 2.5,   "ppm",  4.4,    "mA", 1.9e-2),
    ("OHM1K",    1e-3,    2e-3,    22e-3,  "mA", 0.4,   "ppm",  4.4,    "mA", 0.1),
    ("OHM1_9K",  1e-3,    1.5e-3,  16e-3,  "mA", 0.4,   "ppm",  4.4,    "mA", 0.19),
    ("OHM10K",   100e-6,  500e-6,  7e-3,   "uA", 50.0,  "ppm",  5000.0, "mA", 2.0),
    ("OHM19K",   50e-6,   250e-6,  5e-3,   "uA", 50.0,  "ppm",  5000.0, "mA", 3.8),
    ("OHM100K",  10e-6,   100e-6,  1e-3,   "uA", 7.5,   "ppm",  None,   "uA", 2e-5),
    ("OHM190K",  5e-6,    50e-6,   500e-6, "uA", 4.0,   "ppm",  None,   "uA", 3.8e-5),
    ("OHM1M",    5e-6,    20e-6,   100e-6, "uA", 1.0,   "ppm",  None,   "uA", 1.5e-4),
    ("OHM1_9M",  2.5e-6,  10e-6,   50e-6,  "uA", 0.53,  "ppm",  None,   "uA", 2.9e-4),
    ("OHM10M",   0.5e-6,  2e-6,    10e-6,  "uA", 0.2,   "ppm",  None,   "uA", 1e-3),
    ("OHM19M",   0.25e-6, 1e-6,    5e-6,   "uA", 0.53,  "ppm",  None,   "uA", 1.9e-3),
    ("OHM100M",  50e-9,   200e-9,  1e-6,   "uA", 0.1,   "ppm",  None,   None, None),
]


class TestFluke5700ALoadCurrent:
    """Every catalogue resistance carries its full-spec load window and K's.

    The window is why a 19 Ohm point read by a 3458A at 1 mA is not covered by
    the 31 ppm one-year figure: IL is 8 mA there, and the manual's derating
    adds 16 x (8 - 1) / (8 x 1) = 14 ppm on top.
    """

    @pytest.fixture(scope="class")
    def by_id(self, cal5700):
        return {r.range_id: r for r in cal5700.function(Quantity.RES).ranges}

    def test_every_catalogue_value_carries_one(self, by_id):
        assert sorted(by_id) == sorted(row[0] for row in LOAD_CURRENTS)
        for range_id, range_spec in by_id.items():
            assert range_spec.load_current is not None, range_id

    @pytest.mark.parametrize("row", LOAD_CURRENTS, ids=[r[0] for r in LOAD_CURRENTS])
    def test_the_whole_table_row_by_row(self, by_id, row):
        (range_id, il, iu, imax, unit, k4w, k4w_unit, k2w,
         over_unit, k_over) = row
        load = by_id[range_id].load_current
        assert load.min_a == pytest.approx(il), range_id
        assert load.max_a == pytest.approx(iu), range_id
        assert load.imax_a == pytest.approx(imax), range_id
        assert load.unit == unit, range_id
        assert load.k_under_4w == pytest.approx(k4w), range_id
        assert load.k_under_4w_unit == k4w_unit, range_id
        if k2w is None:
            assert load.k_under_2w is None, range_id
        else:
            assert load.k_under_2w == pytest.approx(k2w), range_id
        assert load.over_unit == over_unit, range_id
        if k_over is None:
            assert load.k_over_4w is None, range_id
        else:
            assert load.k_over_4w == pytest.approx(k_over), range_id

    def test_the_two_wire_column_stops_where_rcomp_does(self, by_id):
        # Note 4 (OM:2018): "Active two-wire compensation may be selected for
        # values less than 100 kOhm."  The derating table's two-wire column
        # ends at exactly the same place.
        for range_id, range_spec in by_id.items():
            has_k2w = range_spec.load_current.k_under_2w is not None
            allows = Connection.TWO_WIRE_RCOMP in range_spec.connections
            assert has_k2w == allows, range_id

    def test_the_nineteen_ohm_point_a_dmm_actually_reads(self, by_id):
        # A 3458A or 34401A reads 19 Ohm on its 100 Ohm range at 1 mA, well
        # under IL = 8 mA: 16 x (8 - 1) / (8 x 1) = 14 ppm, against a 31 ppm
        # one-year specification.
        load = by_id["OHM19"].load_current
        assert not load.in_window(1e-3)
        assert load.under_error(1e-3) == pytest.approx((14.0, "ppm"))

    def test_one_ohm_on_a_34401a(self, by_id):
        # 300 x (8 - 1) / (8 x 1) = 262.5 ppm, more than twice the 110 ppm
        # one-year specification of the value itself.
        assert by_id["OHM1"].load_current.under_error(1e-3) == \
            pytest.approx((262.5, "ppm"))

    def test_one_point_nine_megohm_at_500_nanoamps(self, by_id):
        # The uA half of the unit rule: 0.53 x (2.5 - 0.5) / (2.5 x 0.5)
        #   = 0.848 ppm at 500 nA against IL = 2.5 uA
        assert by_id["OHM1_9M"].load_current.under_error(500e-9) == \
            pytest.approx((0.848, "ppm"))

    def test_a_meter_inside_the_window_adds_nothing(self, by_id):
        # A 34420A reads 190 Ohm at 10 mA, inside 8 - 11 mA.
        load = by_id["OHM190"].load_current
        assert load.in_window(10e-3)
        assert load.under_error(10e-3) is None
        assert load.over_error(10e-3) is None

    def test_the_short_derates_in_milliohms_not_ppm(self, by_id):
        # "Error is in mOhm for all two-wire comp values and four-wire short":
        # a ppm of a 0 Ohm output would be identically zero.
        load = by_id["OHM0"].load_current
        assert load.k_under_4w_unit == "mohm"
        # 0.3 x (8 - 1) / (8 x 1) = 0.2625 mOhm at 1 mA
        assert load.under_error(1e-3) == pytest.approx((0.2625, "mohm"))

    def test_ten_kilohm_takes_microamps_under_and_milliamps_over(self, by_id):
        # The one row where the two equations disagree about the unit.
        load = by_id["OHM10K"].load_current
        # under: 50 x (100 - 10) / (100 x 10) = 4.5 ppm at 10 uA
        assert load.under_error(10e-6) == pytest.approx((4.5, "ppm"))
        # over: 2.0 x (1.0^2 - 0.5^2) = 1.5 ppm at 1 mA
        assert load.over_error(1e-3) == pytest.approx((1.5, "ppm"))

    def test_the_short_and_the_hundred_megohm_state_no_over_current_k(
            self, by_id):
        # Both cells are blank in the derating table.
        assert by_id["OHM0"].load_current.k_over_4w is None
        assert by_id["OHM0"].load_current.over_error(20e-3) is None
        assert by_id["OHM100M"].load_current.k_over_4w is None

    def test_it_is_not_folded_into_any_limit(self, cal5700):
        # The derating is a test-condition adder, not part of the tolerance:
        # evaluate_limit does not know the meter's ohms current.
        result = evaluate_limit(cal5700, Quantity.RES, 19.0,
                                connection="4w", interval="1y")
        assert result.limit == pytest.approx(31e-6 * 19.0)


# ---------------------------------------------------------- M79, M78, M54

class TestFluke5700ACurrentSquaredAdder:
    """``5700a-operators-manual-1996.txt:2176-2178`` (PDF p.52), footnote [1].

        "1. Add to uncertainty specifications:
             +/-200 x I2 ppm for >100 mA on 220 mA range
             +/-10 x I2 ppm for >1 A on 2.2 A range"
    """

    def test_full_scale_on_the_2_2a_range(self, cal5700):
        # 10 x 2.2^2 = 48.4 ppm on top of 95 ppm:
        #   143.4 ppm x 2.2 A + 30 uA = 315.48 uA + 30 uA = 345.48 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 2.2, interval="1y")
        assert result.range_spec.range_id == "DC2_2A"
        assert result.limit == pytest.approx(345.48e-6)

    def test_at_the_threshold_nothing_is_added(self, cal5700):
        # The footnote says "> 1 A", so 1 A itself is the plain row:
        #   95 ppm x 1 A + 30 uA = 95 uA + 30 uA = 125 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 1.0, interval="1y")
        assert result.range_spec.range_id == "DC2_2A"
        assert result.limit == pytest.approx(125e-6)

    def test_halfway_up_the_2_2a_range(self, cal5700):
        # 10 x 1.5^2 = 22.5 ppm: 117.5 ppm x 1.5 A + 30 uA
        #   = 176.25 uA + 30 uA = 206.25 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 1.5, interval="1y")
        assert result.limit == pytest.approx(206.25e-6)

    def test_full_scale_on_the_220ma_range(self, cal5700):
        # 200 x 0.22^2 = 9.68 ppm on top of 70 ppm:
        #   79.68 ppm x 220 mA + 1 uA = 17.5296 uA + 1 uA = 18.5296 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 0.22, interval="1y")
        assert result.range_spec.range_id == "DC220MA"
        assert result.limit == pytest.approx(18.5296e-6)

    def test_at_the_220ma_threshold_nothing_is_added(self, cal5700):
        # 70 ppm x 100 mA + 1 uA = 7 uA + 1 uA = 8 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 0.1, interval="1y")
        assert result.limit == pytest.approx(8e-6)

    def test_the_adder_applies_at_every_interval(self, cal5700):
        # 200 x 0.15^2 = 4.5 ppm on top of the 24-hour 55 ppm:
        #   59.5 ppm x 150 mA + 1 uA = 8.925 uA + 1 uA = 9.925 uA
        result = evaluate_limit(cal5700, Quantity.DCI, 0.15, interval="24h")
        assert result.limit == pytest.approx(9.925e-6)

    def test_the_lower_ranges_carry_no_adder(self, cal5700):
        # 60 ppm x 10 mA + 100 nA = 700 nA, unchanged.
        result = evaluate_limit(cal5700, Quantity.DCI, 0.01, interval="1y")
        assert result.range_spec.range_id == "DC22MA"
        assert result.limit == pytest.approx(700e-9)
        assert result.terms.quadratic == ()

    def test_ac_current_is_not_touched(self, cal5700):
        # The footnote is on the dc table only.
        result = evaluate_limit(cal5700, Quantity.ACI, 0.1, freq_hz=100.0,
                                interval="1y")
        assert result.terms.quadratic == ()


class TestFluke5700AWidebandFlatnessFootnote:
    """``5700a-operators-supplement-2009.txt:66-67`` (PDF p.2), footnote [1].

        "[1] For output voltages < 50 % of full range in the 33 mV, 110 mV,
         330 mV, 1.1 V, and 3.5 V ranges, add 0.1 % to the amplitude flatness
         specification."
    """

    def test_half_a_volt_at_ten_megahertz(self, cal5700):
        # 1.1 V range, 10 - 20 MHz: 0.5 % absolute + 0.4 % flatness = 0.9 %,
        # and 0.5 V is below 0.55 V, so +0.1 % -> 1.0 %:
        #   1.0 % x 0.5 V + 403 uV = 5 mV + 0.403 mV = 5.403 mV
        result = evaluate_limit(cal5700, Quantity.WBV, 0.5, freq_hz=10.0e6,
                                interval="1y")
        assert result.range_spec.range_id == "WB1_1V"
        assert result.limit == pytest.approx(5.403e-3)
        assert result.terms.ppm_reading == pytest.approx(10_000.0)

    def test_at_exactly_half_range_nothing_is_added(self, cal5700):
        # "< 50 %" is strict: 0.9 % x 0.55 V + 403 uV = 4.95 mV + 0.403 mV
        result = evaluate_limit(cal5700, Quantity.WBV, 0.55, freq_hz=10.0e6,
                                interval="1y")
        assert result.limit == pytest.approx(5.353e-3)

    def test_the_absolute_band_is_untouched(self, cal5700):
        # 30 Hz - 500 kHz is the band the absolute table is stated for; the
        # footnote adds to the *flatness* specification, which that band does
        # not use.  0.5 % x 0.5 V + 400 uV = 2.5 mV + 0.4 mV = 2.9 mV
        result = evaluate_limit(cal5700, Quantity.WBV, 0.5, freq_hz=1000.0,
                                interval="1y")
        assert result.limit == pytest.approx(2.9e-3)

    def test_the_low_frequency_band_takes_it_too(self, cal5700):
        # 10 - 30 Hz: 0.5 % + 0.3 % = 0.8 %, +0.1 % -> 0.9 %
        #   0.9 % x 0.5 V + 400 uV = 4.5 mV + 0.4 mV = 4.9 mV
        result = evaluate_limit(cal5700, Quantity.WBV, 0.5, freq_hz=20.0,
                                interval="1y")
        assert result.limit == pytest.approx(4.9e-3)

    def test_the_33mv_range(self, cal5700):
        # 15 mV is below half of 33 mV.  10 - 20 MHz 1 year is 1.0 %, +0.1 %:
        #   1.1 % x 15 mV + 19 uV = 165 uV + 19 uV = 184 uV
        result = evaluate_limit(cal5700, Quantity.WBV, 0.015, freq_hz=10.0e6,
                                interval="1y")
        assert result.range_spec.range_id == "WB33MV"
        assert result.limit == pytest.approx(184e-6)

    def test_the_three_low_ranges_are_not_named_by_the_footnote(self, cal5700):
        # 1.1 mV, 3.3 mV and 11 mV are absent from the footnote's list.
        # 5 mV on the 11 mV range, 10 - 20 MHz, 1 year: 1.1 % + 11 uV
        #   = 55 uV + 11 uV = 66 uV, with no adder.
        result = evaluate_limit(cal5700, Quantity.WBV, 0.005, freq_hz=10.0e6,
                                interval="1y")
        assert result.range_spec.range_id == "WB11MV"
        assert result.limit == pytest.approx(66e-6)
        for range_spec in cal5700.function(Quantity.WBV).ranges:
            if range_spec.range_id in ("WB1_1MV", "WB3_3MV", "WB11MV"):
                assert range_spec.below_frac_adder is None
                for band in range_spec.bands:
                    assert band.below_frac_adder is None, range_spec.range_id


class TestFluke5700ATemperatureWindow:
    """``5700a-operators-manual-1996.txt:1220`` (PDF p.33) and :1301-1302 (p.35).

        "Absolute Uncertainty / +/- 5 C from calibration temperature"
        "Temperature coefficient is an adder to uncertainty specifications
         that does not apply unless operating more than +/-5 C from
         calibration temperature."

    The centre is CAL_TEMP?, read from the instrument, so the file states the
    half-width and nothing else.
    """

    def test_the_window_is_relative_to_the_calibration_temperature(self,
                                                                   cal5700):
        assert cal5700.temperature.relative_to == "cal_temp"
        assert cal5700.temperature.half_width_c == pytest.approx(5.0)

    def test_no_absolute_window_is_invented(self, cal5700):
        assert cal5700.temperature.spec_window_c is None
        assert cal5700.temperature.window_for("1y") is None

    def test_a_cal_temp_resolves_it(self, cal5700):
        assert cal5700.temperature.window_for("1y", reference_c=23.0) == \
            pytest.approx((18.0, 28.0))

    def test_the_window_is_the_same_at_every_interval(self, cal5700):
        for interval in ("24h", "90d", "180d", "1y"):
            assert cal5700.temperature.half_width_for(interval) == \
                pytest.approx(5.0), interval

    def test_a_run_more_than_five_degrees_off_is_out(self, cal5700):
        temperature = cal5700.temperature
        assert temperature.within_window(27.0, "1y", reference_c=23.0)
        assert temperature.within_window(28.0, "1y", reference_c=23.0)
        assert not temperature.within_window(29.0, "1y", reference_c=23.0)
        assert not temperature.within_window(17.0, "1y", reference_c=23.0)


class TestFluke5700ASpecConfidence:
    """``5700a-operators-manual-1996.txt:1107-1110`` (PDF p.30), section 1-24,
    and :1213-1214 (p.33).

        "the primary performance specifications, which are provided at both
         the 99 % and 95 % confidence levels"
        "Fluke guarantees performance verification using specifications stated
         to 99% confidence level."

    The 99 % column is the one this file transcribes, so the file says so.
    """

    def test_the_tables_are_stated_at_99_percent(self, cal5700):
        assert cal5700.spec_confidence == pytest.approx(0.99)


class TestRangeSelectionOnTheNewModels:
    @pytest.mark.parametrize("value, expected_fs", [
        (0.05, 0.1), (0.5, 1.0), (5.0, 10.0), (50.0, 100.0), (500.0, 1000.0),
    ])
    def test_3458a_dcv_picks_the_smallest_holding_range(
            self, dmm3458, value, expected_fs):
        chosen = select_range(dmm3458.function(Quantity.DCV), value)
        assert chosen.fs == expected_fs

    def test_5700a_dcv_range_ladder(self, cal5700):
        function = cal5700.function(Quantity.DCV)
        assert select_range(function, 0.2).range_id == "DC220MV"
        assert select_range(function, 1.0).range_id == "DC2_2V"
        assert select_range(function, 10.0).range_id == "DC11V"
        assert select_range(function, 20.0).range_id == "DC22V"
        assert select_range(function, 100.0).range_id == "DC220V"
        assert select_range(function, 1000.0).range_id == "DC1100V"
        assert select_range(function, 2000.0) is None


# ---------------------------------------------------- the B2 schema additions

MINIMAL = textwrap.dedent("""
    schema_version: 1
    model_id: toy
    display_name: Toy
    role: meter
    overload_sentinel: 9.9e37
    interval_basis: since_cal
    intervals: [1y]
    functions:
      - quantity: DCV
        func: DCV
        ranges:
          - fs: 10.0
            limits:
              1y: {ppm_reading: 10.0, ppm_range: 1.0}
    """)


def _load(text):
    return parse_model_text(text, "toy.yaml")


def _sub(text, old, new):
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new)


class TestLimitsAuthority:
    """Added for the 5700A: only that instrument computes its own uncertainty."""

    def test_defaults_to_file(self):
        assert _load(MINIMAL).limits_authority is LimitsAuthority.FILE

    def test_instrument_round_trips(self):
        text = _sub(MINIMAL, "role: meter",
                    "role: meter\nlimits_authority: instrument")
        assert _load(text).limits_authority is LimitsAuthority.INSTRUMENT

    def test_unknown_value_is_rejected(self):
        text = _sub(MINIMAL, "role: meter",
                    "role: meter\nlimits_authority: whatever")
        with pytest.raises(SpecError, match="limits_authority"):
            _load(text)


class TestRangeLevelAdditions:
    def test_min_out_gates_the_evaluation(self):
        text = _sub(MINIMAL, "fs: 10.0", "fs: 10.0\n        min_out: 1.0")
        spec = _load(text)
        assert evaluate_limit(spec, Quantity.DCV, 0.5) is None
        assert evaluate_limit(spec, Quantity.DCV, 2.0) is not None

    def test_source_impedance_is_carried(self):
        text = _sub(MINIMAL, "fs: 10.0",
                    "fs: 10.0\n        source_impedance_ohm: 50.0")
        assert _load(text).function(Quantity.DCV).ranges[0].source_impedance_ohm == 50.0

    def test_characterized_must_be_a_boolean(self):
        text = _sub(MINIMAL, "fs: 10.0", "fs: 10.0\n        characterized: yes-please")
        with pytest.raises(SpecError, match="characterized"):
            _load(text)

    def test_range_connections_narrow_the_functions(self):
        text = _sub(MINIMAL, "func: DCV\n    ranges:",
                    "connections:\n      4w: {func: DCV}\n      2w: {func: DCV}\n    ranges:")
        text = _sub(text, "fs: 10.0", "fs: 10.0\n        connections: [4w]")
        spec = _load(text)
        assert evaluate_limit(spec, Quantity.DCV, 1.0, connection="4w") is not None
        assert evaluate_limit(spec, Quantity.DCV, 1.0, connection="2w") is None

    def test_unknown_range_connection_is_rejected(self):
        text = _sub(MINIMAL, "fs: 10.0", "fs: 10.0\n        connections: [6w]")
        with pytest.raises(SpecError, match="unknown connection"):
            _load(text)

    def test_empty_range_connection_list_is_rejected(self):
        text = _sub(MINIMAL, "fs: 10.0", "fs: 10.0\n        connections: []")
        with pytest.raises(SpecError, match="omit the key"):
            _load(text)

    def test_zero_full_scale_is_allowed_for_a_short(self):
        # The 5700A's OHM0 is a genuine 0 ohm output whose whole specification
        # is an absolute floor.
        text = _sub(MINIMAL, "fs: 10.0", "fs: 0.0")
        text = _sub(text, "{ppm_reading: 10.0, ppm_range: 1.0}", "{abs_floor: 5.0e-5}")
        spec = _load(text)
        result = evaluate_limit(spec, Quantity.DCV, 0.0)
        assert result.limit == pytest.approx(5e-5)

    def test_negative_full_scale_is_still_a_typo(self):
        text = _sub(MINIMAL, "fs: 10.0", "fs: -10.0")
        with pytest.raises(SpecError, match="must not be negative"):
            _load(text)


class TestBandAndFunctionAdditions:
    BANDED = textwrap.dedent("""
        schema_version: 1
        model_id: toy
        display_name: Toy
        role: source
        overload_sentinel: null
        interval_basis: since_cal
        intervals: [1y]
        functions:
          - quantity: ACV
            func: ACV
            volt_hertz_max: 1.0e+4
            ranges:
              - fs: 100.0
                bands:
                  - freq_hz: [10, 50]
                    max_out: 25.0
                    limits: {1y: {ppm_reading: 100.0}}
                  - freq_hz: [50, 1000]
                    limits: {1y: {ppm_reading: 200.0}}
        """)

    def test_band_max_out_refuses_the_point(self):
        spec = _load(self.BANDED)
        assert evaluate_limit(spec, Quantity.ACV, 20.0, freq_hz=20.0) is not None
        assert evaluate_limit(spec, Quantity.ACV, 30.0, freq_hz=20.0) is None
        # above the capped band the same amplitude is fine
        assert evaluate_limit(spec, Quantity.ACV, 30.0, freq_hz=100.0) is not None

    def test_volt_hertz_max_refuses_the_point(self):
        spec = _load(self.BANDED)
        # 20 V x 100 Hz = 2000 V-Hz, inside; 50 V x 500 Hz = 25000, outside
        assert evaluate_limit(spec, Quantity.ACV, 20.0, freq_hz=100.0) is not None
        assert evaluate_limit(spec, Quantity.ACV, 50.0, freq_hz=500.0) is None
        assert spec.function(Quantity.ACV).volt_hertz_max == 1.0e4


class TestAdderPerInterval:
    TWO_WIRE = textwrap.dedent("""
        schema_version: 1
        model_id: toy
        display_name: Toy
        role: meter
        overload_sentinel: 9.9e37
        interval_basis: since_cal
        intervals: [24h, 1y]
        functions:
          - quantity: RES
            connections:
              4w: {func: OHMF}
              2w:
                func: OHM
                adder_per_interval:
                  24h: {abs_floor: 0.05}
                  1y:  {abs_floor: 0.25}
            ranges:
              - fs: 1000.0
                limits:
                  24h: {ppm_reading: 2.0}
                  1y:  {ppm_reading: 10.0}
        """)

    def test_the_adder_follows_the_interval(self):
        spec = _load(self.TWO_WIRE)
        # 24h: 2 ppm x 1000 = 0.002, + 0.05 = 0.052
        assert evaluate_limit(spec, Quantity.RES, 1000.0, connection="2w",
                              interval="24h").limit == pytest.approx(0.052)
        # 1y: 10 ppm x 1000 = 0.01, + 0.25 = 0.26
        assert evaluate_limit(spec, Quantity.RES, 1000.0, connection="2w",
                              interval="1y").limit == pytest.approx(0.26)

    def test_four_wire_carries_no_adder(self):
        spec = _load(self.TWO_WIRE)
        result = evaluate_limit(spec, Quantity.RES, 1000.0, connection="4w",
                                interval="1y")
        assert result.adder is None
        assert result.limit == pytest.approx(0.01)

    def test_both_adder_spellings_at_once_is_rejected(self):
        text = _sub(self.TWO_WIRE, "adder_per_interval:",
                    "adder: {abs_floor: 0.2}\n        adder_per_interval:")
        with pytest.raises(SpecError, match="mutually exclusive"):
            _load(text)

    def test_an_undeclared_interval_in_the_adder_is_rejected(self):
        text = _sub(self.TWO_WIRE, "24h: {abs_floor: 0.05}",
                    "90d: {abs_floor: 0.05}")
        with pytest.raises(SpecError, match="unknown interval"):
            _load(text)

    def test_the_flat_adder_still_works(self):
        # The 34401A's lead resistance really is one figure for every interval.
        text = _sub(self.TWO_WIRE,
                    "adder_per_interval:\n          24h: {abs_floor: 0.05}\n"
                    "          1y:  {abs_floor: 0.25}",
                    "adder: {abs_floor: 0.2}")
        spec = _load(text)
        for interval, base in (("24h", 0.002), ("1y", 0.01)):
            result = evaluate_limit(spec, Quantity.RES, 1000.0,
                                    connection="2w", interval=interval)
            assert result.limit == pytest.approx(base + 0.2)


class TestDiscreteResistanceCatalogue:
    """The 5700A's resistances are 18 catalogue entries, not a ladder.

    Every one of these values is what ``OUT?`` legitimately returns after
    asking for the nominal below it -- the calibrator sources a *characterized*
    value up to 500 ppm away.  Before ``discrete``, each fell through to the
    next entry up, and every step of that ladder is looser.
    """

    @pytest.mark.parametrize("readback, asked_for, range_id, ppm", [
        (19003.8,     19.0e3,  "OHM19K",  14.0),   # was OHM100K at 16 ppm
        (190_047.5,   190.0e3, "OHM190K", 16.0),   # was OHM1M at 23 ppm
        (1_000_250.0, 1.0e6,   "OHM1M",   23.0),   # was OHM1_9M at 24 ppm
        (1_900_475.0, 1.9e6,   "OHM1_9M", 24.0),   # was OHM10M at 46 ppm
        (10_002_500.0, 10.0e6, "OHM10M",  46.0),   # was OHM19M at 55 ppm
        (19_001_900.0, 19.0e6, "OHM19M",  55.0),   # was OHM100M, no 4-wire spec
    ])
    def test_a_characterized_readback_does_not_fall_through(
            self, cal5700, readback, asked_for, range_id, ppm):
        # Unqualified, it belongs to no range at all...
        assert select_range(cal5700.function(Quantity.RES), readback) is None
        # ...and naming the range the operator actually asked for gives the
        # specification that really applies.
        result = evaluate_limit(cal5700, Quantity.RES, readback,
                                range_fs=asked_for, connection="4w",
                                interval="1y")
        assert result.range_spec.range_id == range_id
        assert result.terms.ppm_reading == pytest.approx(ppm)
        assert result.limit == pytest.approx(ppm * 1e-6 * readback)

    def test_the_19m_case_would_have_lost_its_four_wire_spec(self, cal5700):
        # 100 MOhm has no four-wire sensing, so falling through from 19 MOhm
        # did not merely loosen the limit, it deleted the row.
        assert evaluate_limit(cal5700, Quantity.RES, 19_001_900.0,
                              connection="4w", interval="1y") is None
        assert evaluate_limit(cal5700, Quantity.RES, 19_001_900.0,
                              range_fs=19.0e6, connection="4w",
                              interval="1y") is not None

    def test_exact_catalogue_members_still_autoselect(self, cal5700):
        for value, range_id in [(0.0, "OHM0"), (1.9, "OHM1_9"), (100.0, "OHM100"),
                                (10.0e3, "OHM10K"), (100.0e6, "OHM100M")]:
            chosen = select_range(cal5700.function(Quantity.RES), value)
            assert chosen is not None and chosen.range_id == range_id

    def test_only_resistance_is_discrete(self, cal5700):
        assert all(r.discrete for r in cal5700.function(Quantity.RES).ranges)
        for quantity in (Quantity.DCV, Quantity.ACV, Quantity.DCI, Quantity.ACI):
            assert not any(r.discrete
                           for r in cal5700.function(quantity).ranges)
        # ...so the voltage ladder still autoranges normally.
        assert select_range(cal5700.function(Quantity.DCV), 7.3).range_id == "DC11V"


class TestAcModeAmbiguityRaises:
    """An unqualified 3458A ac query must not silently pick a technique."""

    @pytest.mark.parametrize("freq_hz", [30.0, 1000.0, 60_000.0, 200_000.0])
    def test_unqualified_query_raises(self, dmm3458, freq_hz):
        with pytest.raises(SpecError, match="no defensible default"):
            evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=freq_hz,
                           interval="1y")

    def test_the_arithmetic_the_raise_prevents(self, dmm3458):
        # At 30 Hz the highest-freq_low tie-break would have chosen ANA's
        # [20, 40] band over SYNC's [1, 40]:
        #   SYNC 0.007 % + 0.004 % ->  70 ppm x 1 V +  40 ppm x 1 V = 110 uV
        #   ANA  0.15  % + 0.02  % -> 1500 ppm x 1 V + 200 ppm x 1 V = 1700 uV
        # A meter running SYNC judged against ANA is judged ~15x too loosely.
        sync = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=30.0,
                              mode="sync", interval="1y")
        ana = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=30.0,
                             mode="ana", interval="1y")
        assert sync.limit == pytest.approx(110e-6)
        assert ana.limit == pytest.approx(1700e-6)
        assert ana.limit / sync.limit > 15

    def test_naming_the_mode_always_works(self, dmm3458):
        for mode in ("sync", "ana", "rndm"):
            assert evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=1000.0,
                                  mode=mode, interval="1y") is not None

    def test_an_uncontested_frequency_does_not_raise(self, dmm3458):
        # 5 MHz is reachable only in the two ACBAND>2 MHz modes, and they share
        # one band there, so there is nothing to choose and no reason to raise.
        result = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=5.0e6,
                                interval="1y")
        assert result is not None
        assert result.mode.id == "sync_wide"


    # Band edges are the case a per-sibling-band contest gets wrong: covers()
    # is inclusive at both ends, so at exactly 40 Hz the *adjacent* [20, 40]
    # medium-filter band is still covering even though filt_medium has its own
    # [40, 100] band and would never be judged on [20, 40] here.  Probing only
    # 30 Hz and 1 kHz missed all of it; 100 Hz is an ordinary ac verification
    # point and a plan generator would have crashed on it.
    EDGES = [3, 5, 10, 20, 40, 100, 200, 1000, 5000, 20000, 50000, 100000]


    @pytest.mark.parametrize("freq_hz", [5.0, 3.0e6, 5.0e6, 9.0e6])
    def test_the_3458a_still_resolves_where_only_one_technique_reaches(
            self, dmm3458, freq_hz):
        # 5 Hz is SYNC-only (ANA starts at 10 Hz, RNDM at 20 Hz); above 2 MHz
        # only the two wide modes reach, and they share a band there.
        result = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=freq_hz,
                                interval="1y")
        assert result is not None
        assert result.mode.id in ("sync", "sync_wide")

    def test_the_3458a_5hz_point(self, dmm3458):
        # p.25 SYNC, 100 mV - 10 V, 1 Hz to 40 Hz: 0.007 % + 0.004 %
        #   70 ppm x 1 V + 40 ppm x 1 V = 70 uV + 40 uV = 110 uV
        result = evaluate_limit(dmm3458, Quantity.ACV, 1.0, freq_hz=5.0,
                                interval="1y")
        assert result.mode.id == "sync"
        assert result.limit == pytest.approx(110e-6)


class TestReviewFollowUps:
    def test_3458a_acv_carries_a_tempco_on_every_band(self, dmm3458):
        for range_spec in dmm3458.function(Quantity.ACV).ranges:
            for band in range_spec.bands:
                assert band.tempco is not None, (range_spec.fs, band.freq_low_hz)

    def test_3458a_acv_tempco_is_per_technique(self, dmm3458):
        # p.25 SYNC, 100 mV - 10 V: 0.001 % + 0.0001 % per degC
        # p.30 RNDM, same ranges:   0.001 % + 0.00001 % per degC
        # A band shared by both techniques had to be split to say that.
        volt = [r for r in dmm3458.function(Quantity.ACV).ranges if r.fs == 1.0][0]
        sync = select_band(volt, 2.0e5, "sync").tempco
        rndm = select_band(volt, 2.0e5, "rndm").tempco
        assert sync.ppm_reading_per_c == pytest.approx(10.0)
        assert sync.ppm_range_per_c == pytest.approx(1.0)
        assert rndm.ppm_range_per_c == pytest.approx(0.1)
        # p.28 ANA on the 10 mV range: 0.003 % + 0.006 % per degC
        ten_mv = [r for r in dmm3458.function(Quantity.ACV).ranges
                  if r.fs == 0.01][0]
        ana = select_band(ten_mv, 1000.0, "ana").tempco
        assert ana.ppm_reading_per_c == pytest.approx(30.0)
        assert ana.ppm_range_per_c == pytest.approx(60.0)

    def test_3458a_wide_modes_use_the_10mv_tempco_on_every_range(self, dmm3458):
        # p.25/p.30 footnote 1: "For ACBAND > 2 MHz, use 10 mV range
        # temperature coefficient for all ranges."
        for fs in (0.01, 1.0, 1000.0):
            range_spec = [r for r in dmm3458.function(Quantity.ACV).ranges
                          if r.fs == fs][0]
            band = select_band(range_spec, 5.0e4, "sync_wide")
            assert band.tempco.ppm_reading_per_c == pytest.approx(20.0)
            assert band.tempco.ppm_range_per_c == pytest.approx(200.0)

    def test_5700a_ac_settling_covers_the_low_frequency_case(self, cal5700):
        # Guide 11.5: 7 s below 20 Hz, and every ac range has a 10-20 Hz band.
        assert cal5700.function(Quantity.ACV).settle_s == 7.0
        by_id = {r.range_id: r for r in cal5700.function(Quantity.ACV).ranges}
        # ...plus the 2 s the 1100 V range adds, and it starts at 15 Hz.
        assert by_id["AC1100V"].settle_s == 9.0

    def test_5700a_ac_millivolt_ranges_carry_their_output_impedance(self, cal5700):
        # Guide 18.4 operating characteristics: "Output impedance 50 Ohm
        # (2.2 mV, 22 mV, 220 mV ranges)".
        by_id = {r.range_id: r for r in cal5700.function(Quantity.ACV).ranges}
        for range_id in ("AC2_2MV", "AC22MV", "AC220MV"):
            assert by_id[range_id].source_impedance_ohm == 50.0
        for range_id in ("AC2_2V", "AC22V", "AC220V", "AC1100V"):
            assert by_id[range_id].source_impedance_ohm is None

    def test_no_binary_float_dust_in_the_generated_file(self):
        # These files get read against a datasheet by a human; a min_out of
        # 0.00022000000000000003 makes a reviewer stop and check.
        from importlib import resources
        text = (resources.files("caldmm.cal.data")
                / "fluke5700a.yaml").read_text("utf-8")
        for line in text.splitlines():
            token = line.split("#", 1)[0]
            assert "0000000000" not in token and "9999999999" not in token, line

    def test_adder_per_interval_must_be_complete(self):
        text = _sub(TestAdderPerInterval.TWO_WIRE,
                    "          1y:  {abs_floor: 0.25}\n", "")
        with pytest.raises(SpecError, match="must name every declared interval"):
            _load(text)

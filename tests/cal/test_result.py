"""Tests for the result row, the CSV schema and the run summary.

The headline is the round trip: a row carrying a five-component uncertainty
budget is written, loaded back, and the *reconstructed* budget must give the
same ``u_c``, ``nu_eff``, ``k`` and ``U`` as the original.  That is the whole
claim this module makes -- a report that cannot be re-derived from its own data
file is not evidence -- and everything else here defends one corner of it.
"""

import datetime
import math
import os
import subprocess
import sys
import textwrap

import pytest

from caldmm.cal import uncertainty as unc
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.roles import (
    Environment,
    MeterConfig,
    Point,
    Reading,
    SourceState,
    Uncertainty,
)
from caldmm.cal.result import (
    CSV_COLUMNS,
    LIMIT_DISAGREEMENT_FACTOR,
    ResultError,
    ResultRow,
    ResultWriter,
    RunHeader,
    _escape,
    _fmt_cell,
    _unescape,
    header_from_lines,
    header_to_lines,
    load_results,
    open_results,
    summarize_run,
    write_results,
)
from caldmm.cal.verdict import (
    FLAG_AMBIENT_UNKNOWN,
    FLAG_EMPTY_BUDGET,
    FLAG_NO_TEMPERATURE_WINDOW,
    FLAG_TEMPERATURE_OUT_OF_WINDOW,
    FLAG_TUR_BELOW_FLOOR,
    TemperatureStatus,
    Verdict,
    VerdictError,
    evaluate,
    gates_for,
)

STAMP = datetime.datetime(2026, 8, 1, 12, 34, 56)


# --------------------------------------------------------------- fixtures

def full_budget() -> unc.UncertaintyBudget:
    """A budget with one of every kind of component, in volts."""
    return unc.UncertaintyBudget().add(
        # 3.5 ppm of 10 V at 95 %, the 5700A's own figure for its output.
        unc.from_calibrator_uncert("5700A 10 V output", 35e-6, 0.95,
                                   source="UNCERT? 3.5 PPM"),
        unc.type_a("repeatability",
                   [10.0000012, 10.0000010, 10.0000015, 10.0000011]),
        unc.resolution_component("3458A resolution", 1e-6),
        unc.thermal_emf_component("thermal EMF", 0.5e-6),
        unc.tempco_component("3458A tempco", 0.15, 1.4, 10.0),
    )


def make_row(**overrides) -> ResultRow:
    """A complete, realistic 10 V dc row; *overrides* replace any field."""
    budget = overrides.pop("budget", None)
    if budget is None:
        budget = full_budget()
    conformity = evaluate(1.2e-6, 8.5e-6, budget, ambient_c=23.1,
                          temperature_window_c=(18.0, 28.0))
    fields = dict(
        quantity=Quantity.DCV,
        nominal=10.0,
        timestamp=STAMP,
        run_id="20260801T110000-3458a",
        point_index=3,
        group_index=1,
        label="10 V dc",
        note="spec ratio 2.4",
        frequency_hz=0.0,
        connection=Connection.TWO_WIRE,
        range_arg=10.0,
        range_id="10V",
        mode=None,
        interval="1y",
        source_model="fluke5700a",
        source_value=10.0,
        source_unit="V",
        source_range_id="11V",
        source_uncertainty=Uncertainty(3.5, "PPM", 0.95, 365),
        source_settled=True,
        source_operate=True,
        meter_model="hp3458a",
        mean=10.0000012,
        stdev=2.1e-7,
        n=16,
        discarded=2,
        overloads=0,
        resolution=1e-6,
        meter_func="DCV",
        meter_range_arg=10.0,
        meter_range_fs=10.0,
        meter_nplc=100.0,
        meter_settle_s=2.0,
        applied={"nplc": 100, "azero": "ON"},
        ambient_c=23.147125,
        humidity_pct=41.0,
        env_timestamp=datetime.datetime(2026, 8, 1, 11, 59, 3),
        env_source="probe",
        reference_value=10.0,
        error=conformity.error,
        tolerance_limit=conformity.tolerance_limit,
        spec_limit=35e-6,
        verdict=conformity.verdict,
        expanded_uncertainty=conformity.expanded_uncertainty,
        coverage_factor=conformity.coverage_factor,
        effective_dof=conformity.effective_dof,
        tur=conformity.tur,
        guard_band=conformity.guard_band,
        acceptance_limit=conformity.acceptance_limit,
        margin=conformity.margin,
        confidence=conformity.confidence,
        temperature_status=conformity.temperature_status,
        flags=conformity.flags,
        rule=conformity.rule,
        reason=conformity.reason,
        budget=budget,
    )
    fields.update(overrides)
    return ResultRow(**fields)


def make_header(**overrides) -> RunHeader:
    fields = dict(
        run_id="20260801T110000-3458a",
        started=datetime.datetime(2026, 8, 1, 11, 0, 0),
        plan_name="hp3458a 1y verification",
        dut_role="meter",
        interval="1y",
        confidence=0.95,
        tur_floor=4.0,
        guard_band_factor=1.0,
        temperature_policy="gate",
        source_model="fluke5700a",
        source_idn="FLUKE,5700A,5555001,1.4+2.0+1.4",
        source_options=("5700A-03", "5725A"),
        source_cal_days=112,
        source_zero_days=3,
        source_cal_interval_days=365,
        source_cal_confidence="CONF95",
        # A CAL_TEMP? answer with a fraction, so rounding would be caught.
        source_cal_temp_c=23.087890625,
        source_warm_minutes=45,
        run_kind="as-left",
        counterpart="3458a-1y-asfound.csv",
        adjust_record="hp3458a-adjust-20260801T100000.log",
        meter_model="hp3458a",
        meter_idn="HP3458A",
        meter_calnum=42,
        meter_acal_age_h=6.53125,
        # A real probe reading, not a %g-exact round number -- the point of
        # this fixture is that the header does not quietly round it.
        meter_temp_c=41.24375,
        ambient_c=23.147125,
        humidity_pct=41.078125,
        ambient_timestamp=datetime.datetime(2026, 8, 1, 11, 59),
        ambient_source="probe",
        caldmm_version="0.1.0",
        git_revision="a30ed41",
    )
    fields.update(overrides)
    return RunHeader(**fields)


def roundtrip(tmp_path, header, rows):
    path = str(tmp_path / "run.csv")
    write_results(path, header, rows)
    return load_results(path)


# ------------------------------------------------------- the headline test

class TestBudgetRoundTrip:
    def test_reconstructed_budget_gives_the_same_numbers(self, tmp_path):
        """The claim the whole module exists for."""
        original = full_budget()
        assert len(original) == 5
        row = make_row(budget=original)

        _, loaded = roundtrip(tmp_path, make_header(), [row])
        rebuilt = loaded[0].budget

        assert len(rebuilt) == len(original)
        assert rebuilt.combined() == original.combined()
        assert rebuilt.effective_dof() == original.effective_dof()
        assert rebuilt.coverage_factor() == original.coverage_factor()
        assert rebuilt.expanded() == original.expanded()
        assert rebuilt.expanded(0.99) == original.expanded(0.99)

    def test_every_component_field_survives(self, tmp_path):
        original = full_budget()
        _, loaded = roundtrip(tmp_path, make_header(),
                              [make_row(budget=original)])
        for before, after in zip(original.components,
                                 loaded[0].budget.components):
            assert after.label == before.label
            assert after.value == before.value          # exact: repr round trip
            assert after.dof == before.dof
            assert after.type == before.type
            assert after.distribution == before.distribution
            assert after.sensitivity == before.sensitivity
            assert after.source == before.source

    def test_infinite_dof_survives(self, tmp_path):
        """A pure Type B component carries ``inf`` and must come back as ``inf``."""
        _, loaded = roundtrip(tmp_path, make_header(), [make_row()])
        dofs = [c.dof for c in loaded[0].budget.components]
        assert math.inf in dofs
        assert 3.0 in dofs                              # n-1 of the Type A term

    def test_empty_budget_round_trips_as_empty(self, tmp_path):
        row = make_row(budget=unc.UncertaintyBudget())
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert len(loaded[0].budget) == 0

    def test_budget_is_one_json_cell(self, tmp_path):
        """The variable-length budget must not change the row's shape."""
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(),
                      [make_row(), make_row(budget=unc.UncertaintyBudget())])
        data = [line for line in open(path).read().splitlines()
                if line and not line.startswith("#")]
        assert len(data) == 3                           # header + two rows
        widths = {len(line.split(";")) for line in data}
        assert len(widths) == 1, "rows differ in column count"

    def test_row_type_is_enforced(self):
        with pytest.raises(ResultError, match="UncertaintyBudget"):
            ResultRow(quantity=Quantity.DCV, nominal=10.0, budget=1.7e-6)


# --------------------------------------------------------------- ppm rules

class TestPpm:
    def test_ppm_present_at_a_normal_point(self):
        row = make_row()
        assert row.error_ppm == pytest.approx(0.12)
        assert row.tolerance_ppm == pytest.approx(0.85)

    def test_zero_point_has_no_ppm_but_keeps_the_absolute_limit(self):
        row = make_row(nominal=0.0, source_value=0.0, reference_value=0.0,
                       mean=1.1e-7, error=1.1e-7, tolerance_limit=4e-6)
        assert row.is_zero_point
        assert row.error_ppm is None
        assert row.tolerance_ppm is None
        assert row.tolerance_limit == 4e-6              # still there

        cells = row.to_cells()
        assert cells["Error_ppm"] == ""
        assert cells["Tol_Limit_ppm"] == ""
        assert cells["Tol_Limit"] != ""

    def test_zero_point_survives_the_round_trip(self, tmp_path):
        row = make_row(nominal=0.0, source_value=0.0, reference_value=0.0,
                       mean=1.1e-7, error=1.1e-7, tolerance_limit=4e-6)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].error_ppm is None
        assert loaded[0].tolerance_ppm is None
        assert loaded[0].tolerance_limit == pytest.approx(4e-6)

    def test_a_reference_inside_its_own_limit_is_a_zero_point(self):
        """A short read as 120 nV against a 4 uV limit is not a 120 nV setpoint."""
        row = make_row(nominal=0.0, reference_value=1.2e-7, error=1.2e-7,
                       tolerance_limit=4e-6)
        assert row.is_zero_point
        assert row.error_ppm is None

    def test_a_reference_outside_its_limit_is_not_a_zero_point(self):
        row = make_row(nominal=1e-3, reference_value=1e-3, error=1e-8,
                       tolerance_limit=4e-6)
        assert not row.is_zero_point
        assert row.error_ppm == pytest.approx(10.0)

    def test_the_claim_tested_names_the_interval_the_instrument_used(self):
        """M64: in --dut source the claim under test is UNCERT? at CAL_INTV.

        The header already carried both numbers; nothing said what they meant
        together, so a report headed "interval 1y" could be testing the
        24-hour claim at every point.
        """
        header = make_header(dut_role="source", interval="1y",
                             source_cal_interval_days=1,
                             source_cal_confidence="CONF99")
        assert header.claim_tested == "UNCERT? at 1 d / CONF99"
        assert header.claim_disagrees_with_interval is True

    def test_the_claim_agrees_when_the_two_intervals_match(self):
        header = make_header(dut_role="source", interval="1y",
                             source_cal_interval_days=365,
                             source_cal_confidence="CONF95")
        assert header.claim_tested == "UNCERT? at 365 d / CONF95"
        assert header.claim_disagrees_with_interval is False

    def test_a_meter_dut_tests_no_such_claim(self):
        header = make_header(dut_role="meter", source_cal_interval_days=1,
                             source_cal_confidence="CONF99")
        assert header.claim_tested == ""
        assert header.claim_disagrees_with_interval is False

    def test_an_unrecorded_interval_makes_no_claim(self):
        header = make_header(dut_role="source", source_cal_interval_days=None,
                             source_cal_confidence="")
        assert header.claim_tested == ""

    def test_a_zero_setpoint_is_a_zero_point_however_the_meter_read(self):
        """M19/L79: the *setpoint* decides, not the meter's own offset.

        In a source-DUT run the reference is the meter's mean, so a 0 V point
        whose meter sat 2 uV off its zero used to report an error of
        +1,000,000 ppm and a limit of 400,000 ppm -- ppm of the meter's own
        offset, which is arithmetic, not information.  Eleven of the 156
        points of the shipped source-DUT plan are zero setpoints.
        """
        row = make_row(nominal=0.0, source_value=0.0, mean=2e-6,
                       reference_value=2e-6, error=2e-6, tolerance_limit=8e-7)
        assert row.is_zero_point
        assert row.error_ppm is None
        assert row.tolerance_ppm is None
        assert row.error == pytest.approx(2e-6)         # the absolute error
        assert row.tolerance_limit == pytest.approx(8e-7)

    def test_ppm_base_is_the_reference_not_the_nominal(self):
        """Resistance: the reference is the characterized OUT?, not the request."""
        row = make_row(quantity=Quantity.RES, nominal=10000.0,
                       source_value=10004.6, reference_value=10004.6,
                       mean=10004.75, error=0.15, tolerance_limit=0.14,
                       source_unit="OHM")
        assert row.unit == "OHM"
        # Against the characterized 10004.6 ohm the error is 14.993 ppm;
        # against the 10000 ohm nominal it would read 15.000 ppm.  A 7 ppb
        # difference here, but the same mistake on the *error* itself is
        # 4.6 ohm instead of 0.15 -- see the from_measurement test below.
        assert row.error_ppm == pytest.approx(14.9931, abs=1e-4)
        assert 0.15 / 10000.0 * 1e6 == pytest.approx(15.0000, abs=1e-4)
        assert row.error_ppm != pytest.approx(15.0, abs=1e-3)

    def test_derived_ppm_columns_are_recomputed_not_trusted(self, tmp_path):
        """A hand-edited ppm cell must not survive: derived is derived."""
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(), [make_row()])
        text = open(path).read().replace(";+0.120000;", ";+999.000000;")
        open(path, "w").write(text)
        _, loaded = load_results(path)
        assert loaded[0].error_ppm == pytest.approx(0.12)


# ------------------------------------------------------------ cross-check

class TestLimitCrossCheck:
    def test_no_flag_when_the_two_limits_agree(self):
        # spec table 35 uV against UNCERT? 3.5 ppm of 10 V = 35 uV.
        row = make_row(spec_limit=35e-6)
        assert row.source_uncert_limit == pytest.approx(35e-6)
        assert row.limit_disagree is False

    def test_no_flag_just_inside_the_threshold(self):
        row = make_row(spec_limit=35e-6 * (LIMIT_DISAGREEMENT_FACTOR - 0.01))
        assert row.limit_disagree is False

    def test_flag_when_the_spec_table_is_much_larger(self):
        row = make_row(spec_limit=35e-6 * (LIMIT_DISAGREEMENT_FACTOR + 0.01))
        assert row.limit_disagree is True

    def test_flag_when_the_instrument_claims_much_more_than_the_table(self):
        row = make_row(spec_limit=35e-6 / (LIMIT_DISAGREEMENT_FACTOR + 0.01))
        assert row.limit_disagree is True

    def test_no_flag_when_only_one_limit_exists(self):
        assert make_row(spec_limit=None).limit_disagree is False
        assert make_row(source_uncertainty=None).limit_disagree is False

    def test_flag_survives_the_round_trip(self, tmp_path):
        row = make_row(spec_limit=200e-6)
        assert row.limit_disagree
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].limit_disagree is True
        assert loaded[0].spec_limit == pytest.approx(200e-6)
        assert loaded[0].source_uncert_limit == pytest.approx(35e-6)

    def test_absolute_uncertainty_unit_is_not_scaled(self):
        row = make_row(source_uncertainty=Uncertainty(4e-5, "V", 0.99, 90))
        assert row.source_uncert_limit == pytest.approx(4e-5)


# --------------------------------------------------------- meter statistics

class TestReadingStatistics:
    def test_overloads_and_discards_survive(self, tmp_path):
        row = make_row(n=12, discarded=3, overloads=2)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert (loaded[0].n, loaded[0].discarded, loaded[0].overloads) \
            == (12, 3, 2)

    def test_zero_counts_are_written_as_zero_not_blank(self):
        cells = make_row(overloads=0, discarded=0).to_cells()
        assert cells["Overloads"] == "0"
        assert cells["Discarded"] == "0"

    def test_a_single_sample_has_no_stdev(self, tmp_path):
        row = make_row(n=1, stdev=math.nan)
        assert row.stdev is None
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].stdev is None

    def test_ambient_timestamp_survives(self, tmp_path):
        """Polled once a group, points run for hours: staleness is visible."""
        polled = datetime.datetime(2026, 8, 1, 11, 59, 3)
        row = make_row(env_timestamp=polled, timestamp=STAMP)
        assert row.to_cells()["Env_Time"] == polled.isoformat()
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].env_timestamp == polled
        assert loaded[0].timestamp == STAMP
        assert loaded[0].env_timestamp != loaded[0].timestamp

    def test_from_measurement_carries_the_ambient_timestamp(self):
        polled = datetime.datetime(2026, 8, 1, 11, 59, 3)
        budget = unc.UncertaintyBudget().add(
            unc.from_confidence("ref", 5e-6, 0.95))
        row = ResultRow.from_measurement(
            point=Point(quantity=Quantity.DCV, value=10.0),
            conformity=evaluate(0.0, 8.5e-6, budget), budget=budget,
            environment=Environment(ambient_c=23.1, humidity_pct=41.0,
                                    timestamp=polled, source="probe"))
        assert row.env_timestamp == polled

    def test_applied_knobs_survive(self, tmp_path):
        row = make_row(applied={"nplc": 100, "azero": "ON", "ocomp": False})
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert dict(loaded[0].applied) == {"nplc": 100, "azero": "ON",
                                           "ocomp": False}


# --------------------------------------------------- the raw samples column

#: Deliberately awkward floats: repr round-trips them, anything less does not.
AWKWARD_SAMPLES = (10.0000012, 0.1 + 0.2 + 9.7, 10.0 + 1.0 / 3.0e6,
                   10.0000011, 9.9999987)


class TestSamplesColumn:
    """The Type A term must be recomputable from the file alone."""

    def test_samples_round_trip_bit_exactly(self, tmp_path):
        row = make_row(samples=AWKWARD_SAMPLES, n=len(AWKWARD_SAMPLES))
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].samples == AWKWARD_SAMPLES

    def test_the_stored_stdev_recomputes_from_the_samples_cell(self, tmp_path):
        """The audit this column exists for: s from the raw data, exactly."""
        import statistics

        reading = Reading.from_samples(AWKWARD_SAMPLES)
        row = make_row(samples=reading.samples, mean=reading.mean,
                       stdev=reading.stdev, n=reading.n)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        # Bit-exact against the true statistic: the samples reloaded exactly,
        # so the same computation gives the same float.
        assert statistics.stdev(loaded[0].samples) == reading.stdev
        assert statistics.fmean(loaded[0].samples) == reading.mean
        # ... and the stored Stdev cell is only an 11-digit rendering of it.
        assert loaded[0].stdev == pytest.approx(reading.stdev, rel=1e-9)

    def test_excluded_sentinels_are_not_in_the_list(self, tmp_path):
        """``Reading.from_samples`` already dropped them; the file must not
        resurrect an overload into the evidence for the mean."""
        reading = Reading.from_samples([10.0, 9.9e37, 10.2],
                                       overload=lambda v: v > 1e37)
        row = make_row(samples=reading.samples, n=reading.n,
                       overloads=reading.overloads)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].samples == (10.0, 10.2)
        assert loaded[0].overloads == 1

    def test_samples_are_one_json_cell(self):
        cells = make_row(samples=AWKWARD_SAMPLES).to_cells()
        values = __import__("json").loads(cells["Samples"])
        assert tuple(values) == AWKWARD_SAMPLES

    def test_no_samples_is_a_blank_cell(self):
        assert make_row(samples=()).to_cells()["Samples"] == ""

    def test_meter_temp_round_trips(self, tmp_path):
        row = make_row(meter_temp_c=38.751953125)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].meter_temp_c == pytest.approx(38.751953125)

    def test_meter_temp_nan_is_none(self):
        assert make_row(meter_temp_c=math.nan).meter_temp_c is None

    def test_a_pre_v2_file_still_loads_with_empty_samples(self, tmp_path):
        """A file written before the columns existed is 'not recorded'."""
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(),
                      [make_row(samples=AWKWARD_SAMPLES,
                                meter_temp_c=38.75)])
        # Strip the two v2 columns out of the column line and every row,
        # which is exactly what a v1 writer never produced.
        lines = open(path).read().splitlines()
        out = []
        drop: list[int] = []
        for line in lines:
            if line.startswith("#"):
                out.append(line)
                continue
            cells = line.split(";")
            if not drop:                              # the column line
                drop = [cells.index("Samples"), cells.index("Meter_Temp_C")]
            out.append(";".join(cell for index, cell in enumerate(cells)
                                if index not in drop))
        with open(path, "w") as handle:
            handle.write("\n".join(out) + "\n")

        _, loaded = load_results(path)
        assert loaded[0].samples == ()
        assert loaded[0].meter_temp_c is None

    def test_a_malformed_samples_cell_names_its_column(self):
        cells = make_row().to_cells()
        cells["Time"] = STAMP.isoformat()
        cells["Samples"] = '{"not": "a list of numbers"}'
        with pytest.raises(ResultError, match="Samples"):
            ResultRow.from_cells(cells)

    def test_from_measurement_carries_samples_and_meter_temp(self):
        budget = unc.UncertaintyBudget().add(
            unc.from_confidence("ref", 5e-6, 0.95))
        reading = Reading.from_samples(AWKWARD_SAMPLES)
        row = ResultRow.from_measurement(
            point=Point(quantity=Quantity.DCV, value=10.0),
            conformity=evaluate(0.0, 8.5e-6, budget), budget=budget,
            reading=reading, meter_temp_c=38.75)
        assert row.samples == reading.samples
        assert row.meter_temp_c == 38.75


# ------------------------------------------------------------- _fmt_cell

class TestFmtCell:
    def test_none_is_empty(self):
        for col in CSV_COLUMNS:
            assert _fmt_cell(col, None) == ""

    def test_nan_is_empty(self):
        assert _fmt_cell("Error", math.nan) == ""
        assert _fmt_cell("Tol_Limit", math.nan) == ""

    def test_ints(self):
        assert _fmt_cell("Point", 3) == "3"
        assert _fmt_cell("N", 16.0) == "16"
        assert _fmt_cell("Src_Uncert_Days", 365) == "365"

    def test_booleans(self):
        assert _fmt_cell("Src_Operate", True) == "1"
        assert _fmt_cell("Src_Operate", False) == "0"
        assert _fmt_cell("Limit_Disagree", False) == "0"

    def test_floats_are_signed_exponential(self):
        assert _fmt_cell("Error", 1.2e-6) == "+1.2000000000E-06"
        assert _fmt_cell("Error", -1.2e-6) == "-1.2000000000E-06"

    def test_ppm_carries_a_sign(self):
        assert _fmt_cell("Error_ppm", 0.12) == "+0.120000"
        assert _fmt_cell("Error_ppm", -0.12) == "-0.120000"
        assert _fmt_cell("Rel_Margin", -0.5) == "-0.5"

    def test_ratios_are_plain_decimals(self):
        assert _fmt_cell("k", 2.0) == "2"
        assert _fmt_cell("Confidence", 0.95) == "0.95"
        assert _fmt_cell("TUR", 4.25) == "4.25"

    def test_a_small_tur_keeps_its_significant_figures(self):
        """A fixed 6-decimal format would leave 2.4e-04 as "0.000240"."""
        assert _fmt_cell("TUR", 2.4e-4) == "0.00024"
        assert float(_fmt_cell("TUR", 0.24223190330957)) \
            == pytest.approx(0.24223190330957)

    def test_timestamps_are_iso_8601(self):
        assert _fmt_cell("Env_Time", STAMP) == "2026-08-01T12:34:56"

    def test_infinite_dof(self):
        assert _fmt_cell("Nu_Eff", math.inf) == "inf"
        assert _fmt_cell("Nu_Eff", 12.5) == "+1.2500000000E+01"

    def test_enums_use_their_stable_spelling(self):
        assert _fmt_cell("Quantity", Quantity.DCV) == "DCV"
        assert _fmt_cell("Connection", Connection.FOUR_WIRE) == "4w"
        assert _fmt_cell("Verdict", Verdict.PASS) == "pass"
        assert _fmt_cell("Temp_Status", TemperatureStatus.AMBIENT_UNKNOWN) \
            == "ambient_unknown"

    def test_flags_join_with_commas(self):
        assert _fmt_cell("Flags", (FLAG_TUR_BELOW_FLOOR, FLAG_AMBIENT_UNKNOWN))\
            == "tur-below-floor,ambient-temperature-unknown"

    def test_empty_json_columns_are_blank(self):
        assert _fmt_cell("Mtr_Applied", {}) == ""
        assert _fmt_cell("Budget", []) == ""

    def test_text_never_contains_the_delimiter(self):
        cell = _fmt_cell("Reason", "a; b\nc\\d")
        assert ";" not in cell
        assert "\n" not in cell
        assert _unescape(cell) == "a; b\nc\\d"

    @pytest.mark.parametrize("text", [
        "a;b", "a\\;b", "back\\slash", "line\nbreak", "\\x3B literal",
        "|error| = 1.2e-06 (AL = 8.5e-06); note: TUR = 0.24",
        "", "no escapes at all",
    ])
    def test_escape_round_trips(self, text):
        assert _unescape(_escape(text)) == text
        assert ";" not in _escape(text)


class TestEmptyCellsReloadAsNone:
    def test_optional_numbers(self, tmp_path):
        row = make_row(range_arg=None, meter_nplc=None, resolution=None,
                       stdev=None, humidity_pct=None, ambient_c=None,
                       env_timestamp=None, spec_limit=None)
        cells = row.to_cells()
        for col in ("Range_Arg", "Mtr_NPLC", "Resolution", "Stdev",
                    "Humidity_Pct", "Ambient_C", "Env_Time", "Spec_Limit"):
            assert cells[col] == ""
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        after = loaded[0]
        assert after.range_arg is None
        assert after.meter_nplc is None
        assert after.resolution is None
        assert after.stdev is None
        assert after.humidity_pct is None
        assert after.ambient_c is None
        assert after.env_timestamp is None
        assert after.spec_limit is None

    def test_optional_text(self, tmp_path):
        row = make_row(mode=None, range_id=None, source_range_id=None,
                       meter_ac_filter=None)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].mode is None
        assert loaded[0].range_id is None
        assert loaded[0].source_range_id is None
        assert loaded[0].meter_ac_filter is None

    def test_no_spec_row_has_no_limit(self, tmp_path):
        budget = full_budget()
        conformity = evaluate(1.2e-6, None, budget)
        row = make_row(tolerance_limit=conformity.tolerance_limit,
                       margin=conformity.margin,
                       guard_band=conformity.guard_band,
                       acceptance_limit=conformity.acceptance_limit,
                       tur=conformity.tur, verdict=conformity.verdict,
                       budget=budget)
        assert row.tolerance_limit is None              # nan normalised away
        assert row.relative_margin is None
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].verdict is Verdict.NO_SPEC
        assert loaded[0].tolerance_limit is None
        assert loaded[0].margin is None
        assert loaded[0].tur is None


# ------------------------------------------------------------- file format

class TestFileFormat:
    def test_semicolon_delimited_with_iso_time_first(self, tmp_path):
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(), [make_row()])
        lines = [line for line in open(path).read().splitlines()
                 if line and not line.startswith("#")]
        assert lines[0].split(";") == ["Time"] + CSV_COLUMNS
        cells = lines[1].split(";")
        assert len(cells) == len(CSV_COLUMNS) + 1
        assert datetime.datetime.fromisoformat(cells[0]) == STAMP

    def test_header_block_leads_the_file(self, tmp_path):
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(), [make_row()])
        lines = open(path).read().splitlines()
        assert lines[0].startswith("#")
        first_data = next(i for i, line in enumerate(lines)
                          if not line.startswith("#"))
        assert lines[first_data].startswith("Time;")

    def test_reason_with_semicolons_stays_on_one_line(self, tmp_path):
        row = make_row()
        assert ";" in row.reason, "fixture no longer exercises the escaping"
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].reason == row.reason
        assert loaded[0].rule == row.rule

    def test_several_rows(self, tmp_path):
        rows = [make_row(point_index=i, label=f"point {i}") for i in range(5)]
        _, loaded = roundtrip(tmp_path, make_header(), rows)
        assert [r.point_index for r in loaded] == [0, 1, 2, 3, 4]
        assert [r.label for r in loaded] == [f"point {i}" for i in range(5)]

    def test_truncated_line_is_rejected(self, tmp_path):
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(), [make_row()])
        with open(path, "a") as handle:
            handle.write("2026-08-01T13:00:00;short;row\n")
        with pytest.raises(ResultError, match="cells for"):
            load_results(path)

    def test_a_file_without_a_column_line_is_rejected(self, tmp_path):
        path = str(tmp_path / "empty.csv")
        with open(path, "w") as handle:
            handle.write("# run_id=x\n")
        with pytest.raises(ResultError, match="no column header"):
            load_results(path)

    def test_empty_run_writes_a_readable_file(self, tmp_path):
        header, rows = roundtrip(tmp_path, make_header(), [])
        assert rows == []
        assert header.run_id == "20260801T110000-3458a"


# --------------------------------------------------------------- durability

class TestDurability:
    """A run is ~200 points over two hours.  Nothing may be held in memory."""

    def test_a_generator_fed_write_grows_the_file_as_it_yields(self, tmp_path):
        path = str(tmp_path / "run.csv")
        sizes = []

        def rows():
            for index in range(5):
                sizes.append(os.path.getsize(path))
                yield make_row(point_index=index)

        write_results(path, make_header(), rows())
        # One observation before each yield: the first sees header only, and
        # every later one must be strictly larger than the one before it.
        assert sizes == sorted(sizes)
        assert len(set(sizes)) == 5, f"file did not grow between rows: {sizes}"

    def test_a_crash_mid_run_keeps_every_row_already_written(self, tmp_path):
        path = str(tmp_path / "run.csv")

        class Bang(RuntimeError):
            pass

        def rows():
            for index in range(3):
                yield make_row(point_index=index, label=f"point {index}")
            raise Bang("instrument dropped off the bus at point 3")

        with pytest.raises(Bang):
            write_results(path, make_header(), rows())

        header, loaded = load_results(path)
        assert [r.point_index for r in loaded] == [0, 1, 2]
        assert header.run_id == "20260801T110000-3458a"
        assert loaded[0].budget.combined() == full_budget().combined()

    def test_ctrl_c_partway_through_leaves_a_loadable_file(self, tmp_path):
        path = str(tmp_path / "run.csv")
        writer = open_results(path, make_header())
        try:
            for index in range(4):
                writer.write(make_row(point_index=index))
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        finally:
            writer.close()
        _, loaded = load_results(path)
        assert len(loaded) == 4

    def test_writer_is_a_context_manager(self, tmp_path):
        path = str(tmp_path / "run.csv")
        with open_results(path, make_header()) as writer:
            assert isinstance(writer, ResultWriter)
            writer.write(make_row())
            writer.write(make_row(point_index=1))
            assert writer.rows_written == 2
            assert writer.path == path
        _, loaded = load_results(path)
        assert len(loaded) == 2

    def test_append_resumes_instead_of_truncating(self, tmp_path):
        path = str(tmp_path / "run.csv")
        with open_results(path, make_header()) as writer:
            writer.write(make_row(point_index=0, label="group 1"))
        with open_results(path, make_header(), append=True) as writer:
            writer.write(make_row(point_index=1, label="group 2"))
            assert writer.rows_written == 1              # this writer's own
        header, loaded = load_results(path)
        assert [r.label for r in loaded] == ["group 1", "group 2"]
        assert header.run_id == "20260801T110000-3458a"
        # The provenance block is written once, not once per resume.
        text = open(path).read()
        assert text.count("# run_id=") == 1

    def test_append_to_a_missing_file_writes_the_header(self, tmp_path):
        path = str(tmp_path / "new.csv")
        with open_results(path, make_header(), append=True) as writer:
            writer.write(make_row())
        header, loaded = load_results(path)
        assert len(loaded) == 1
        assert header == make_header()

    def test_a_second_write_results_replaces_the_file(self, tmp_path):
        """write_results is the whole-run convenience; it starts clean."""
        path = str(tmp_path / "run.csv")
        write_results(path, make_header(), [make_row(), make_row()])
        write_results(path, make_header(), [make_row()])
        _, loaded = load_results(path)
        assert len(loaded) == 1

    def test_append_refuses_a_file_with_a_different_schema(self, tmp_path):
        path = str(tmp_path / "old.csv")
        with open(path, "w") as handle:
            handle.write("# run_id=older\n")
            handle.write("Time;Run_Id;Point;Verdict\n")
            handle.write("2026-01-01T00:00:00;old;0;pass\n")
        with pytest.raises(ResultError, match="two schemas"):
            open_results(path, make_header(), append=True)

    def test_a_row_without_a_timestamp_is_rejected(self, tmp_path):
        """Better a loud failure than the file-write clock in the Time column."""
        path = str(tmp_path / "run.csv")
        with pytest.raises(ResultError, match="no timestamp"):
            write_results(path, make_header(), [make_row(timestamp=None)])

    def test_the_rejection_names_the_point(self, tmp_path):
        path = str(tmp_path / "run.csv")
        with pytest.raises(ResultError, match="point 7"):
            write_results(path, make_header(),
                          [make_row(point_index=7, timestamp=None)])

    def test_rows_written_before_a_rejection_survive(self, tmp_path):
        path = str(tmp_path / "run.csv")
        with pytest.raises(ResultError):
            write_results(path, make_header(),
                          [make_row(point_index=0), make_row(timestamp=None)])
        _, loaded = load_results(path)
        assert [r.point_index for r in loaded] == [0]

    def test_writer_rejects_a_non_row(self, tmp_path):
        with pytest.raises(ResultError, match="ResultRow"):
            with open_results(str(tmp_path / "run.csv"),
                              make_header()) as writer:
                writer.write("not a row")


# -------------------------------------------------- derived decision fields

class TestDerivedDecisionFields:
    """A loaded row's U must be the U its own budget gives, to the last bit."""

    def test_u_k_and_nu_eff_come_from_the_budget(self, tmp_path):
        budget = full_budget()
        _, loaded = roundtrip(tmp_path, make_header(),
                              [make_row(budget=budget)])
        row = loaded[0]
        assert row.coverage_factor == budget.coverage_factor(0.95)
        assert row.effective_dof == budget.effective_dof()
        assert row.expanded_uncertainty \
            == budget.coverage_factor(0.95) * budget.combined()

    def test_the_written_cell_is_only_a_rounding_of_the_truth(self, tmp_path):
        """Reading U back would leave the row disagreeing with its own budget."""
        row = make_row()
        cell = float(row.to_cells()["U"])
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].expanded_uncertainty == pytest.approx(cell)
        assert loaded[0].expanded_uncertainty \
            == row.budget.coverage_factor(0.95) * row.budget.combined()

    def test_tur_margin_and_acceptance_limit_are_recomputed(self, tmp_path):
        _, loaded = roundtrip(tmp_path, make_header(), [make_row()])
        row = loaded[0]
        assert row.tur == row.tolerance_limit / row.expanded_uncertainty
        assert row.margin == row.tolerance_limit - abs(row.error)
        assert row.acceptance_limit == row.tolerance_limit - row.guard_band

    def test_a_corrupted_derived_cell_is_ignored(self, tmp_path):
        path = str(tmp_path / "run.csv")
        row = make_row()
        write_results(path, make_header(), [row])
        text = open(path).read()
        cells = row.to_cells()
        text = text.replace(f";{cells['U']};", ";+9.9990000000E-01;")
        text = text.replace(f";{cells['TUR']};", ";999;")
        open(path, "w").write(text)
        _, loaded = load_results(path)
        assert loaded[0].expanded_uncertainty == pytest.approx(3.509e-5,
                                                               rel=1e-3)
        assert loaded[0].tur == pytest.approx(0.2422, rel=1e-3)

    def test_the_verdict_is_reproducible_from_the_loaded_row(self, tmp_path):
        """The premise: a report regenerates from the file, not from memory."""
        row = make_row()
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        after = loaded[0]
        again = evaluate(after.error, after.tolerance_limit, after.budget,
                         ambient_c=after.ambient_c,
                         temperature_window_c=(18.0, 28.0))
        assert again.verdict is after.verdict
        assert again.expanded_uncertainty == after.expanded_uncertainty
        assert again.coverage_factor == after.coverage_factor
        assert again.tur == after.tur
        assert again.margin == pytest.approx(after.margin, abs=1e-18)
        assert again.flags == after.flags

    def test_guard_band_is_read_back_because_the_budget_cannot_give_it(
            self, tmp_path):
        # Guarded rejection: w is negative, and nothing in the budget says so.
        row = make_row(guard_band=-3.5e-5)
        _, loaded = roundtrip(tmp_path, make_header(), [row])
        assert loaded[0].guard_band == pytest.approx(-3.5e-5)
        assert loaded[0].acceptance_limit \
            == loaded[0].tolerance_limit + 3.5e-5


# ------------------------------------------------------ schema completeness

class TestSchemaCompleteness:
    def test_values_produces_exactly_the_columns(self):
        """A column added and forgotten in values() would blank every row."""
        assert set(make_row().values()) == set(CSV_COLUMNS)

    def test_to_cells_is_loud_about_a_missing_column(self, monkeypatch):
        monkeypatch.setattr("caldmm.cal.result.CSV_COLUMNS",
                            CSV_COLUMNS + ["Invented"])
        with pytest.raises(ResultError, match="Invented"):
            make_row().to_cells()

    def test_every_column_is_unique(self):
        assert len(CSV_COLUMNS) == len(set(CSV_COLUMNS))
        assert "Time" not in CSV_COLUMNS         # CsvWriter prepends it


class TestParseErrors:
    def base_cells(self):
        cells = make_row().to_cells()
        cells["Time"] = STAMP.isoformat()
        return cells

    @pytest.mark.parametrize("col,bad", [
        ("Quantity", "DVC"),
        ("Connection", "3w"),
        ("Verdict", "maybe"),
        ("Temp_Status", "warmish"),
    ])
    def test_unknown_enum_spellings_name_their_column(self, col, bad):
        cells = self.base_cells()
        cells[col] = bad
        with pytest.raises(ResultError, match=col):
            ResultRow.from_cells(cells)

    def test_a_bad_number_names_its_column(self):
        cells = self.base_cells()
        cells["Mean"] = "ten volts"
        with pytest.raises(ResultError, match="Mean"):
            ResultRow.from_cells(cells)

    def test_a_bad_timestamp_names_its_column(self):
        cells = self.base_cells()
        cells["Env_Time"] = "yesterday"
        with pytest.raises(ResultError, match="Env_Time"):
            ResultRow.from_cells(cells)

    def test_malformed_budget_json(self):
        cells = self.base_cells()
        cells["Budget"] = "[{not json"
        with pytest.raises(ResultError, match="Budget"):
            ResultRow.from_cells(cells)


# ----------------------------------------------------------------- header

class TestRunHeader:
    def test_every_provenance_field_round_trips(self):
        header = make_header()
        back = header_from_lines(header_to_lines(header))
        assert back == header

    def test_floats_are_not_rounded_on_the_way_out(self):
        """``%g`` would store 23.147125 C as 23.1471 and lose the equality."""
        header = make_header(ambient_c=23.147125, humidity_pct=41.078125,
                             meter_temp_c=41.24375, meter_acal_age_h=6.53125,
                             confidence=0.9973002039367398)
        back = header_from_lines(header_to_lines(header))
        assert back.ambient_c == 23.147125
        assert back.humidity_pct == 41.078125
        assert back.meter_temp_c == 41.24375
        assert back.meter_acal_age_h == 6.53125
        assert back.confidence == 0.9973002039367398
        assert back == header

    @pytest.mark.parametrize("value", [
        23.147125, 1e-7, 1.0 / 3.0, 1234567.891011, -40.0, 0.1 + 0.2,
    ])
    def test_awkward_floats_survive_exactly(self, value):
        back = header_from_lines(header_to_lines(make_header(
            ambient_c=value, meter_temp_c=value)))
        assert back.ambient_c == value
        assert back.meter_temp_c == value

    def test_ambient_unknown_round_trips(self):
        header = make_header(ambient_c=None, humidity_pct=None,
                             ambient_timestamp=None, ambient_source="")
        assert header.ambient_known is False
        back = header_from_lines(header_to_lines(header))
        assert back.ambient_c is None
        assert back.humidity_pct is None
        assert back.ambient_timestamp is None
        assert back.ambient_known is False
        assert back == header

    def test_ambient_known_is_written_explicitly(self):
        lines = header_to_lines(make_header())
        assert "# ambient_known=1" in lines
        lines = header_to_lines(make_header(ambient_c=None))
        assert "# ambient_known=0" in lines

    def test_nan_ambient_is_unknown(self):
        header = make_header(ambient_c=math.nan)
        assert header.ambient_c is None
        assert header.ambient_known is False

    def test_source_options_round_trip(self):
        back = header_from_lines(header_to_lines(make_header()))
        assert back.source_options == ("5700A-03", "5725A")
        back = header_from_lines(
            header_to_lines(make_header(source_options=())))
        assert back.source_options == ()

    def test_calibration_ages_round_trip(self):
        back = header_from_lines(header_to_lines(make_header()))
        assert back.source_cal_days == 112
        assert back.source_zero_days == 3
        assert back.source_cal_interval_days == 365
        assert back.source_cal_confidence == "CONF95"
        assert back.source_warm_minutes == 45
        assert back.meter_calnum == 42
        assert back.meter_acal_age_h == 6.53125
        assert back.meter_temp_c == 41.24375

    def test_zero_days_is_not_confused_with_unknown(self):
        """Zeroed today is not the same as never asked."""
        back = header_from_lines(header_to_lines(make_header(
            source_zero_days=0, source_warm_minutes=0)))
        assert back.source_zero_days == 0
        assert back.source_warm_minutes == 0
        back = header_from_lines(header_to_lines(make_header(
            source_zero_days=None)))
        assert back.source_zero_days is None

    def test_decision_knobs_round_trip(self):
        back = header_from_lines(header_to_lines(make_header(
            confidence=0.99, tur_floor=10.0, guard_band_factor=0.5,
            temperature_policy="strict")))
        assert back.confidence == 0.99
        assert back.tur_floor == 10.0
        assert back.guard_band_factor == 0.5
        assert back.temperature_policy == "strict"

    def test_unknown_keys_are_preserved(self):
        lines = header_to_lines(make_header()) + ["# future_key=future value"]
        back = header_from_lines(lines)
        assert back.extra["future_key"] == "future value"
        # ... and are written out again.
        assert "# future_key=future value" in header_to_lines(back)

    def test_run_kind_defaults_to_standalone(self):
        """A run that never said is not half of a calibration cycle."""
        assert RunHeader().run_kind == "standalone"
        assert RunHeader().counterpart is None
        assert RunHeader().adjust_record is None
        assert RunHeader().source_cal_temp_c is None

    def test_a_pre_v2_header_reads_as_standalone(self):
        """Old files carry no run_kind line at all."""
        lines = [line for line in header_to_lines(make_header())
                 if not any(key in line for key in
                            ("run_kind", "counterpart", "adjust_record",
                             "source_cal_temp_c"))]
        back = header_from_lines(lines)
        assert back.run_kind == "standalone"
        assert back.counterpart is None
        assert back.adjust_record is None
        assert back.source_cal_temp_c is None

    def test_a_blank_run_kind_reads_as_standalone(self):
        back = header_from_lines(["# run_kind="])
        assert back.run_kind == "standalone"

    def test_linkage_round_trips(self):
        back = header_from_lines(header_to_lines(make_header()))
        assert back.run_kind == "as-left"
        assert back.counterpart == "3458a-1y-asfound.csv"
        assert back.adjust_record == "hp3458a-adjust-20260801T100000.log"
        assert back.source_cal_temp_c == 23.087890625

    def test_idn_with_a_newline_stays_on_one_line(self):
        header = make_header(source_idn="FLUKE,5700A\n,1.4")
        lines = header_to_lines(header)
        assert sum(1 for line in lines if "source_idn" in line) == 1
        assert header_from_lines(lines).source_idn == "FLUKE,5700A\n,1.4"

    def test_header_survives_the_file(self, tmp_path):
        header = make_header()
        back, _ = roundtrip(tmp_path, header, [make_row()])
        assert back == header

    def test_banner_line_is_ignored(self):
        assert header_from_lines(["# caldmm calibration results"]) \
            == RunHeader()


# ---------------------------------------------------------- from_measurement

class TestFromMeasurement:
    def test_builds_a_row_from_the_run_objects(self):
        point = Point(quantity=Quantity.DCV, value=10.0, range_arg=10.0,
                      label="10 V dc", interval="1y")
        state = SourceState(actual_value=10.0000004, unit="V",
                            range_id="11V",
                            uncertainty=Uncertainty(3.5, "PPM", 0.95, 365),
                            settled=True, operate=True)
        reading = Reading.from_samples([10.0000012, 10.0000010, 9.9e37],
                                       overload=lambda v: v > 1e30,
                                       discarded=2, resolution=1e-6, unit="V")
        config = MeterConfig(func="DCV", range_arg=10.0, range_fs=10.0,
                             nplc=100.0, resolution=1e-6, settle_s=2.0,
                             applied={"nplc": 100})
        budget = full_budget()
        conformity = evaluate(reading.mean - state.actual_value, 8.5e-6,
                              budget, ambient_c=23.1,
                              temperature_window_c=(18.0, 28.0))
        row = ResultRow.from_measurement(
            point=point, state=state, reading=reading, config=config,
            conformity=conformity, budget=budget,
            environment=Environment(ambient_c=23.1, humidity_pct=41.0,
                                    source="probe"),
            source_model="fluke5700a", meter_model="hp3458a",
            run_id="r1", point_index=3, group_index=1, spec_limit=35e-6)

        assert row.source_value == 10.0000004
        assert row.reference_value == 10.0000004      # the readback, not 10.0
        assert row.nominal == 10.0
        assert row.overloads == 1
        assert row.discarded == 2
        assert row.n == 2
        assert row.error == conformity.error
        assert row.verdict is conformity.verdict
        assert row.applied == {"nplc": 100}
        assert row.env_source == "probe"

    def test_resistance_reference_is_the_characterized_value(self):
        """500 ppm off nominal by design; using the nominal invents the error."""
        point = Point(quantity=Quantity.RES, value=10000.0,
                      connection=Connection.FOUR_WIRE)
        state = SourceState(actual_value=10004.6, unit="OHM")
        reading = Reading.from_samples([10004.75, 10004.74])
        budget = unc.UncertaintyBudget().add(
            unc.from_confidence("5700A ohms", 0.09, 0.95))
        conformity = evaluate(reading.mean - state.actual_value, 0.14, budget)
        row = ResultRow.from_measurement(
            point=point, state=state, reading=reading, config=None,
            conformity=conformity, budget=budget)
        assert row.nominal == 10000.0
        assert row.source_value == 10004.6
        assert row.reference_value == 10004.6
        assert row.error == pytest.approx(0.145)
        # Against the nominal the "error" would be 4.745 ohm -- 33x the limit.
        assert abs(row.error) < 0.2

    def test_reference_can_be_the_meter_when_the_source_is_the_dut(self):
        point = Point(quantity=Quantity.DCV, value=10.0)
        state = SourceState(actual_value=10.0000004, unit="V")
        reading = Reading.from_samples([10.0000012, 10.0000010])
        budget = unc.UncertaintyBudget().add(
            unc.from_confidence("3458A", 5e-6, 0.95))
        conformity = evaluate(state.actual_value - reading.mean, 8.5e-6, budget)
        row = ResultRow.from_measurement(
            point=point, state=state, reading=reading, config=None,
            conformity=conformity, budget=budget,
            reference_value=reading.mean)
        assert row.reference_value == reading.mean

    def test_missing_state_and_config_are_tolerated(self):
        point = Point(quantity=Quantity.DCV, value=10.0)
        budget = unc.UncertaintyBudget()
        conformity = evaluate(0.0, 8.5e-6, budget)
        row = ResultRow.from_measurement(point=point, conformity=conformity,
                                         budget=budget)
        assert row.source_value is None
        assert row.reference_value is None
        assert row.is_zero_point
        assert row.error_ppm is None


# ---------------------------------------------------------------- summary

#: The temperature status ``evaluate`` pairs with each temperature flag.  A
#: fixture that set the flag and left the status alone would be a row no real
#: run can produce -- and it is exactly the incoherence that hid the
#: flag-for-block bug, because a status-based count read it as "in window".
_STATUS_FOR_FLAG = {
    FLAG_TEMPERATURE_OUT_OF_WINDOW: TemperatureStatus.OUT_OF_WINDOW,
    FLAG_AMBIENT_UNKNOWN: TemperatureStatus.AMBIENT_UNKNOWN,
    FLAG_NO_TEMPERATURE_WINDOW: TemperatureStatus.NO_WINDOW,
}


def indeterminate_row(flags, **overrides):
    """An ``INDETERMINATE`` row whose flags and temperature status agree."""
    for flag, status in _STATUS_FOR_FLAG.items():
        if flag in flags:
            overrides.setdefault("temperature_status", status)
    return make_row(verdict=Verdict.INDETERMINATE, flags=flags, **overrides)


class TestSummarizeRun:
    def rows(self):
        return [
            make_row(verdict=Verdict.PASS, margin=7.3e-6,
                     tolerance_limit=8.5e-6, flags=()),
            make_row(verdict=Verdict.PASS, margin=1.0e-6,
                     tolerance_limit=8.5e-6, flags=()),
            make_row(verdict=Verdict.FAIL, margin=-2.0e-6,
                     tolerance_limit=8.5e-6, flags=()),
            indeterminate_row((FLAG_TUR_BELOW_FLOOR,), margin=6.0e-6),
            indeterminate_row((FLAG_TUR_BELOW_FLOOR,), margin=6.1e-6),
            indeterminate_row((FLAG_TEMPERATURE_OUT_OF_WINDOW,), margin=6.2e-6),
            indeterminate_row((), margin=1e-7),
            make_row(verdict=Verdict.NO_SPEC, tolerance_limit=None,
                     margin=None, flags=()),
        ]

    def test_counts_per_verdict(self):
        summary = summarize_run(self.rows())
        assert summary.total == 8
        assert summary[Verdict.PASS] == 2
        assert summary[Verdict.FAIL] == 1
        assert summary[Verdict.INDETERMINATE] == 4
        assert summary[Verdict.NO_SPEC] == 1

    def test_tur_floor_and_temperature_are_distinguished(self):
        """The number that says "your reference isn't good enough here"."""
        summary = summarize_run(self.rows())
        assert summary.indeterminate_tur == 2
        assert summary.indeterminate_temperature == 1
        assert summary.indeterminate_guard_band == 1
        assert (summary.indeterminate_tur
                + summary.indeterminate_temperature
                + summary.indeterminate_guard_band) \
            == summary[Verdict.INDETERMINATE]

    def test_a_flag_the_policy_does_not_block_on_is_not_a_temperature_cause(
            self):
        """The 3458A/5700A case: neither model declares an absolute window, so
        every row carries ``no-temperature-window`` -- and under ``gate`` that
        blocks nothing.  Counting the flag put a whole run of coarse-reference
        points in the temperature bucket and left the guard band at zero."""
        rows = [
            indeterminate_row((FLAG_NO_TEMPERATURE_WINDOW,
                               FLAG_TUR_BELOW_FLOOR)),
            indeterminate_row((FLAG_NO_TEMPERATURE_WINDOW,), margin=1e-7),
        ]

        summary = summarize_run(rows)

        assert summary.indeterminate_tur == 1
        assert summary.indeterminate_temperature == 0
        assert summary.indeterminate_guard_band == 1
        # ... and every row still *carries* the condition, which a report
        # has to print even though it blocked nothing
        assert all(row.carries_temperature_condition for row in rows)

    def test_blocked_by_temperature_follows_the_policy(self):
        row = indeterminate_row((FLAG_NO_TEMPERATURE_WINDOW,))

        assert row.temperature_status not in gates_for("gate")
        assert row.temperature_status in gates_for("strict")
        assert not row.blocked_by_temperature()
        assert row.blocked_by_temperature("strict")
        assert row.carries_temperature_condition

    def test_a_pass_is_never_blocked_by_temperature(self):
        row = make_row(verdict=Verdict.PASS,
                       temperature_status=TemperatureStatus.AMBIENT_UNKNOWN,
                       flags=(FLAG_AMBIENT_UNKNOWN,))
        assert not row.blocked_by_temperature()
        assert not row.carries_temperature_condition

    def test_the_same_rows_under_strict_are_temperature_blocked(self):
        rows = [
            indeterminate_row((FLAG_NO_TEMPERATURE_WINDOW,
                               FLAG_TUR_BELOW_FLOOR)),
            indeterminate_row((FLAG_NO_TEMPERATURE_WINDOW,), margin=1e-7),
        ]

        summary = summarize_run(rows, temperature_policy="strict")

        assert summary.indeterminate_temperature == 2
        assert summary.indeterminate_guard_band == 0

    def test_an_empty_budget_is_not_a_guard_band_point(self):
        """``U = 0`` means the guard band is zero; the error never landed in one."""
        summary = summarize_run([indeterminate_row((FLAG_EMPTY_BUDGET,))])

        assert summary.indeterminate_no_budget == 1
        assert summary.indeterminate_guard_band == 0
        assert "with no budget" in summary.line()

    def test_an_unknown_policy_is_rejected(self):
        with pytest.raises(VerdictError, match="temperature policy"):
            summarize_run([indeterminate_row(())],
                          temperature_policy="whatever")

    def test_a_point_blocked_by_both_counts_in_both(self):
        summary = summarize_run([
            indeterminate_row((FLAG_TUR_BELOW_FLOOR, FLAG_AMBIENT_UNKNOWN))])
        assert summary.indeterminate_tur == 1
        assert summary.indeterminate_temperature == 1
        assert summary.indeterminate_guard_band == 0

    def test_a_pass_carrying_the_tur_flag_is_not_counted(self):
        """Only INDETERMINATE points were blocked; a FAIL past TL still fails."""
        summary = summarize_run([
            make_row(verdict=Verdict.FAIL, flags=(FLAG_TUR_BELOW_FLOOR,))])
        assert summary.indeterminate_tur == 0
        assert summary[Verdict.FAIL] == 1

    def test_worst_is_ranked_on_the_relative_margin(self):
        summary = summarize_run(self.rows())
        # The FAIL is out of tolerance by 24 % of its limit; nothing else is
        # negative, so it wins -- and the winner is a whole row, not a number.
        assert summary.worst_relative_margin == pytest.approx(-2.0e-6 / 8.5e-6)
        assert summary.worst is not None
        assert summary.worst.verdict is Verdict.FAIL

    def test_worst_among_conforming_points_is_the_nearest_to_its_limit(self):
        rows = [r for r in self.rows() if r.verdict is not Verdict.FAIL]
        summary = summarize_run(rows)
        assert summary.worst_relative_margin == pytest.approx(1e-7 / 8.5e-6)

    def test_ranking_is_dimensionless_across_quantities(self):
        volts = make_row(label="10 V", margin=1e-6, tolerance_limit=8.5e-6)
        ohms = make_row(quantity=Quantity.RES, label="10 kohm",
                        reference_value=10004.6, margin=0.01,
                        tolerance_limit=0.14)
        # The ohms row has the larger raw margin but is nearer its limit.
        summary = summarize_run([volts, ohms])
        assert summary.worst is ohms

    def test_points_without_a_limit_are_counted_but_never_ranked(self):
        summary = summarize_run([
            make_row(verdict=Verdict.NO_SPEC, tolerance_limit=None,
                     margin=None)])
        assert summary.total == 1
        assert summary.worst is None
        assert summary.worst_relative_margin is None

    def test_overloads_and_discards_are_totalled(self):
        summary = summarize_run([make_row(overloads=1, discarded=2),
                                 make_row(overloads=3, discarded=4)])
        assert summary.overloads == 4
        assert summary.discarded == 6

    def test_flagged_counts_rows_not_flags(self):
        summary = summarize_run([
            indeterminate_row((FLAG_TUR_BELOW_FLOOR, FLAG_AMBIENT_UNKNOWN)),
            make_row(verdict=Verdict.PASS, flags=()),
        ])
        assert summary.flagged == 1

    def test_empty_run(self):
        summary = summarize_run([])
        assert summary.total == 0
        assert summary.worst is None
        assert "0 pass" in summary.line()

    def test_line_names_the_tur_subset(self):
        line = summarize_run(self.rows()).line()
        assert "2 below the TUR floor" in line
        assert "1 temperature-gated" in line

    def test_rejects_a_non_row(self):
        with pytest.raises(ResultError, match="ResultRow"):
            summarize_run([object()])


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

    import caldmm.cal.result as result

    row = result.ResultRow(
        quantity=__import__("caldmm.cal.quantities",
                            fromlist=["Quantity"]).Quantity.DCV,
        nominal=10.0, reference_value=10.0, error=1.2e-6,
        tolerance_limit=8.5e-6)
    assert abs(row.error_ppm - 0.12) < 1e-12
    assert result.summarize_run([row]).total == 1

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal.result pulled in drivers: " + repr(leaked)
    assert "caldmm.cal.adapters" not in sys.modules, "cal.result imported adapters"
    print("OK")
    """)


def test_result_imports_with_no_hardware_libraries():
    """``cal.result`` is model and serialisation: no drivers, no adapters.

    A subprocess so the root conftest's mock stubs are not in the way.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

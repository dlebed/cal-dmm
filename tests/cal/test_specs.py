"""Tests for the specification model, the strict loader and limit evaluation.

Golden limit tests write the arithmetic out longhand from the manufacturers'
specification tables.  They are what catches a schema that silently drops the
range term.
"""

import subprocess
import sys
import textwrap
from typing import NamedTuple

import pytest

from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.specs import (
    APPLY_KNOBS,
    IntervalBasis,
    LimitTerms,
    PairRole,
    QuadraticTerm,
    Role,
    SpecError,
    Tempco,
    available_models,
    evaluate_limit,
    load_model,
    parse_model_text,
    select_band,
    select_range,
)

MODEL = "hp3458a"


@pytest.fixture(scope="module")
def spec():
    return load_model(MODEL)


# ---------------------------------------------------------------- LimitTerms

class TestLimitTerms:
    def test_three_terms(self):
        # 10 ppm of 5 V + 2 ppm of 10 V full scale + 1 uV
        #   = 50 uV + 20 uV + 1 uV = 71 uV
        terms = LimitTerms(ppm_reading=10.0, ppm_range=2.0, abs_floor=1e-6)
        assert terms.limit(5.0, 10.0) == pytest.approx(71e-6)

    def test_zero_reading_is_exactly_the_range_term(self):
        terms = LimitTerms(ppm_reading=35.0, ppm_range=5.0)
        assert terms.limit(0.0, 10.0) == pytest.approx(5.0 * 10.0 * 1e-6)

    def test_negative_reading_uses_magnitude(self):
        terms = LimitTerms(ppm_reading=35.0, ppm_range=5.0)
        assert terms.limit(-10.0, 10.0) == terms.limit(10.0, 10.0)

    def test_addition_sums_every_term(self):
        total = (LimitTerms(10.0, 2.0, 1e-6) + LimitTerms(1.0, 0.5, 2e-6))
        assert total == LimitTerms(11.0, 2.5, 3e-6)


class TestTempco:
    def test_adder_scales_with_temperature_delta(self):
        # 34401A 10 V range: 0.0005 % rdg + 0.0001 % rng per degree C
        tempco = Tempco(ppm_reading_per_c=5.0, ppm_range_per_c=1.0)
        # 5 degrees off: (5 ppm * 10 V + 1 ppm * 10 V) * 5 = 300 uV
        assert tempco.adder(10.0, 10.0, 5.0) == pytest.approx(300e-6)
        assert tempco.adder(10.0, 10.0, -5.0) == pytest.approx(300e-6)


# ---------------------------------------------------- shipped 34401A file

class TestShippedModel:
    def test_available_models_finds_the_shipped_file(self):
        assert MODEL in available_models()


    def test_overload_sentinel_detection(self, spec):
        assert spec.is_overload(9.9e37)
        assert spec.is_overload(-9.9e37)
        assert spec.is_overload(float("nan"))
        assert not spec.is_overload(10.0)


    def test_dcv_ranges(self, spec):
        assert [r.fs for r in spec.function(Quantity.DCV).ranges] == [
            0.1, 1.0, 10.0, 100.0, 1000.0]

    def test_input_impedance_is_carried(self, spec):
        by_fs = {r.fs: r for r in spec.function(Quantity.DCV).ranges}
        # 0.1/1/10 V: selectable 10 M or >10 G.  100 V/1000 V: fixed 10 M,
        # which is a real loading error against a 50 ohm source.
        assert by_fs[10.0].input_impedance_ohm == pytest.approx(1e10)
        assert by_fs[100.0].input_impedance_ohm == pytest.approx(1e7)
        assert by_fs[1000.0].input_impedance_ohm == pytest.approx(1e7)


# --------------------------------------------------- golden limits: dc volts

class TestGoldenDcVolts:


    def test_above_every_range_has_no_spec(self, spec):
        assert evaluate_limit(spec, Quantity.DCV, 2000.0) is None


# ------------------------------------------------- golden limits: resistance

# --------------------------------------------------- golden limits: currents

class TestGoldenCurrent:


    def test_1a_ac_between_20_and_45_hz(self, spec):
        # 3458A data sheet p.33, 1 A row, 20 Hz to 45 Hz: 0.16 % + 0.02 %.
        # 0.0016 x 1 A + 0.0002 x 1 A = 1.8 mA.
        result = evaluate_limit(spec, Quantity.ACI, 1.0, freq_hz=35.0,
                                interval="1y")
        assert result.limit == pytest.approx(1.8e-3)

    def test_ac_current_above_5khz_is_typical_only_so_unspecified(self, spec):
        # p.33 footnote 3: the 5-100 kHz columns are "typical performance".
        assert evaluate_limit(spec, Quantity.ACI, 1e-3, freq_hz=10e3,
                              interval="1y") is None

    def test_the_100ua_ac_range_stops_at_1khz(self, spec):
        # p.33 footnote 4: "1 kHz maximum on the 100 uA range".
        assert evaluate_limit(spec, Quantity.ACI, 1e-4, freq_hz=1e3,
                              interval="1y").limit == pytest.approx(90e-9)
        assert evaluate_limit(spec, Quantity.ACI, 1e-4, freq_hz=2e3,
                              interval="1y") is None


# --------------------------------------------------- golden limits: ac volts

class TestGoldenAcVolts:


    def test_below_five_percent_of_range_has_no_spec(self, spec):
        # p.218 note [4]: ac specs are for a sinewave input above 5 % of range.
        assert evaluate_limit(spec, Quantity.ACV, 1e-3, freq_hz=1000.0) is None
        assert evaluate_limit(spec, Quantity.ACV, 0.0, freq_hz=1000.0) is None

    def test_missing_frequency_on_a_banded_range_raises(self, spec):
        with pytest.raises(SpecError, match="freq_hz is required"):
            evaluate_limit(spec, Quantity.ACV, 1.0)


# ------------------------------------------------------- shared band edges

class _Outcome(NamedTuple):
    """What one ``evaluate_limit`` call did, for sweeping over many points."""

    limit: float | None
    mode: str | None
    error: str | None


def _limit_or_error(spec, quantity, value, **kwargs) -> _Outcome:
    try:
        result = evaluate_limit(spec, quantity, value, **kwargs)
    except SpecError as exc:
        return _Outcome(None, None, str(exc))
    if result is None:
        return _Outcome(None, None, None)
    return _Outcome(result.limit,
                    None if result.mode is None else result.mode.id, None)


class TestSharedBandEdges:
    """The tie-break at a frequency printed in two adjacent specification rows.

    Band endpoints are inclusive, so a datasheet whose rows meet at 20 kHz,
    50 kHz and 100 kHz leaves the edge tone in both rows.  Keysight's own
    verification tables settle it for a meter under test: they apply the
    *tighter* row at every shared edge.  Those limits are the golden cases
    below, quoted from the 34401A Service Guide with line numbers into
    ``34401a-service.txt`` (the extract the spec file was transcribed from).
    """


    def test_unknown_role_is_a_caller_error(self, spec):
        with pytest.raises(ValueError):
            evaluate_limit(spec, Quantity.ACV, 1.0, freq_hz=100_000.0,
                           interval="24h", role="unit_under_test")


    def test_select_band_rejects_an_unknown_role_even_with_one_candidate(self, spec):
        # 1 kHz is interior, so only one band covers it and the tie-break is
        # never reached -- the typo must still be caught.
        range_spec = select_range(spec.function(Quantity.ACV), 1.0)
        with pytest.raises(ValueError):
            select_band(range_spec, 5000.0, "filt_slow", role="referrence")


# ------------------------------------------ M01/H06 data side: 34401A only

class TestOverrangeIsCarried:
    """M01 (data side): 34401a-user.txt:9110 (PDF p.216) footnote [3] and

    34401a-user.txt:9228 (PDF p.218) footnote [3]: "20% overrange on all
    ranges, except 1000 Vdc / 750 Vac, 3 A range."
    """


    def test_every_resistance_range(self, spec):
        # The DC table's "Range" column header itself carries the [3]
        # marker (34401a-user.txt:9022), so it covers Resistance too --
        # no range there is excluded.
        for range_spec in spec.function(Quantity.RES).ranges:
            assert range_spec.overrange == pytest.approx(1.2), range_spec.fs


class TestResistanceTestCurrentData:
    """H06 (data side): 34401a-user.txt:8549-8556 (PDF p.203), the "Power

    Dissipation Effects" table.
    """

    EXPECTED_A = {
        100.0: 0.001, 1e3: 0.001, 1e4: 0.0001, 1e5: 0.00001,
        1e6: 0.000005, 1e7: 0.0000005, 1e8: 0.0000005,
    }


# ------------------------------------------------------------ range picking

class TestRangeSelection:
    @pytest.mark.parametrize("value,expected_fs", [
        (0.05, 0.1),
        (0.1, 0.1),
        (0.11, 1.0),
        (5.0, 10.0),
        (10.0, 10.0),
        (-10.0, 10.0),
        (10.1, 100.0),
        (999.0, 1000.0),
    ])
    def test_smallest_range_that_holds_the_value(self, spec, value, expected_fs):
        function = spec.function(Quantity.DCV)
        assert select_range(function, value).fs == pytest.approx(expected_fs)

    def test_explicit_range_is_honoured(self, spec):
        function = spec.function(Quantity.DCV)
        assert select_range(function, 1.0, range_fs=1000.0).fs == 1000.0

    def test_unknown_explicit_range_has_no_spec(self, spec):
        assert evaluate_limit(spec, Quantity.DCV, 1.0, range_fs=42.0) is None

    def test_value_larger_than_every_range(self, spec):
        assert select_range(spec.function(Quantity.DCV), 5000.0) is None


# ------------------------------------------------------------ caller errors

class TestCallerErrors:
    def test_unknown_interval_raises_and_lists_the_declared_ones(self, spec):
        with pytest.raises(SpecError) as excinfo:
            evaluate_limit(spec, Quantity.DCV, 10.0, interval="1yr")
        assert "1yr" in str(excinfo.value)
        assert "24h, 90d, 1y" in str(excinfo.value)


    def test_quantity_the_model_lacks_returns_none(self, spec):
        assert evaluate_limit(spec, Quantity.WBV, 1.0, freq_hz=1000.0) is None

    def test_connection_the_function_lacks_returns_none(self, spec):
        assert evaluate_limit(spec, Quantity.DCV, 10.0, connection="4w") is None


# ------------------------------------------------------------------- loader

MINIMAL = textwrap.dedent("""\
    schema_version: 1
    model_id: toy
    display_name: "Toy meter"
    role: meter
    overload_sentinel: 9.9e+37
    interval_basis: since_cal
    intervals: [1y]
    default_interval: 1y
    functions:
      - quantity: DCV
        func: DCV
        apply: {nplc: 10}
        ranges:
          - fs: 10.0
            limits: {1y: {pct_reading: 0.0035, pct_range: 0.0005}}
    """)


RANGE_LIMITS_LINE = \
    "        limits: {1y: {pct_reading: 0.0035, pct_range: 0.0005}}\n"


def _load(text):
    return parse_model_text(text, "toy.yaml")


def _sub(text, old, new):
    """Replace *old* with *new*, refusing to silently do nothing.

    A no-op replace would leave the document valid and the test vacuous.
    """
    assert old in text, f"fixture no longer contains {old!r}"
    return text.replace(old, new)


def _drop_line(text, needle):
    """Delete the one line containing *needle*, whatever its indentation."""
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if needle not in line]
    assert len(kept) == len(lines) - 1, \
        f"expected exactly one line containing {needle!r}"
    return "".join(kept)


class TestLoaderStrictness:
    def test_minimal_document_loads(self):
        spec = _load(MINIMAL)
        assert spec.model_id == "toy"
        assert spec.function(Quantity.DCV).ranges[0].fs == 10.0
        # range_arg defaults to the full scale when not given
        assert spec.function(Quantity.DCV).ranges[0].range_arg == 10.0

    @pytest.mark.parametrize("value", ["0.0", "-1.0e-3"])
    def test_a_non_positive_test_current_is_refused(self, value):
        """``test_current_a`` is a sourced current in amps.

        Zero or negative is a typo, and it is a typo that matters: the pairing
        in ``plan`` reads it to decide whether a meter's ohms test current
        falls inside the calibrator's stated load-current window, and a 0 A
        one would silently look like "no current at all" instead of failing
        loudly at load.
        """
        text = _sub(MINIMAL, "      - fs: 10.0\n",
                    f"      - fs: 10.0\n"
                    f"        test_current_a: {value}\n")
        with pytest.raises(SpecError, match="test_current_a"):
            _load(text)

    def test_a_positive_test_current_is_carried(self):
        text = _sub(MINIMAL, "      - fs: 10.0\n",
                    "      - fs: 10.0\n"
                    "        test_current_a: 1.0e-3\n")
        assert _load(text).function(Quantity.DCV).ranges[0].test_current_a \
            == pytest.approx(1e-3)

    def test_unknown_top_level_key(self):
        with pytest.raises(SpecError) as excinfo:
            _load(MINIMAL + "acuracy: 1\n")
        message = str(excinfo.value)
        assert "toy.yaml" in message
        assert "<root>" in message
        assert "'acuracy'" in message

    def test_unknown_key_inside_apply(self):
        text = _sub(MINIMAL, "apply: {nplc: 10}", "apply: {nplc: 10, filtr: on}")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        message = str(excinfo.value)
        assert "'filtr'" in message
        assert "functions[0].apply" in message
        assert "ac_filter" in message           # names the allowed knobs

    def test_unknown_key_inside_a_range(self):
        text = _sub(MINIMAL, "      - fs: 10.0\n",
                    "      - fs: 10.0\n        zs: 3\n")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        message = str(excinfo.value)
        assert "'zs'" in message
        assert "functions[0].ranges[0]" in message

    def test_unknown_key_inside_limits_terms(self):
        text = _sub(MINIMAL, "{pct_reading: 0.0035, pct_range: 0.0005}",
                               "{pct_reading: 0.0035, pct_rnge: 0.0005}")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        message = str(excinfo.value)
        assert "'pct_rnge'" in message
        assert "functions[0].ranges[0].limits.1y" in message

    def test_undeclared_interval_is_rejected(self):
        text = _sub(MINIMAL, "{1y: {pct_reading", "{1yr: {pct_reading")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'1yr'" in str(excinfo.value)
        assert "declared intervals" in str(excinfo.value)

    def test_both_spellings_of_one_term_is_rejected(self):
        text = _sub(MINIMAL, "{pct_reading: 0.0035, pct_range: 0.0005}",
                               "{pct_reading: 0.0035, ppm_reading: 35}")
        with pytest.raises(SpecError, match="use one spelling"):
            _load(text)

    def test_apply_knob_type_is_checked(self):
        text = _sub(MINIMAL, "apply: {nplc: 10}", "apply: {nplc: true}")
        with pytest.raises(SpecError, match="must not be a boolean"):
            _load(text)

    def test_limits_and_bands_are_mutually_exclusive(self):
        text = _sub(MINIMAL, RANGE_LIMITS_LINE,
                    "        limits: {1y: {pct_reading: 0.0035}}\n"
                    "        bands: []\n")
        with pytest.raises(SpecError, match="mutually exclusive"):
            _load(text)

    def test_range_without_any_limits(self):
        text = _sub(MINIMAL, RANGE_LIMITS_LINE, "")
        with pytest.raises(SpecError, match="either 'limits' or 'bands'"):
            _load(text)

    def test_band_naming_an_undeclared_mode(self):
        text = _sub(MINIMAL, RANGE_LIMITS_LINE,
                    "        bands:\n"
                    "          - freq_hz: [10, 100]\n"
                    "            modes: [ghost]\n"
                    "            limits: {1y: {pct_reading: 0.1}}\n")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'ghost'" in str(excinfo.value)
        assert "functions[0].ranges[0].bands[0].modes[0]" in str(excinfo.value)

    def test_unsupported_schema_version(self):
        text = _sub(MINIMAL, "schema_version: 1", "schema_version: 2")
        with pytest.raises(SpecError, match="unsupported schema_version"):
            _load(text)

    def test_duplicate_quantity_is_rejected(self):
        text = MINIMAL + (
            "  - quantity: DCV\n"
            "    func: DCV\n"
            "    ranges:\n"
            "      - fs: 1.0\n"
            "        limits: {1y: {pct_reading: 0.01}}\n")
        with pytest.raises(SpecError, match="duplicate function"):
            _load(text)

    def test_bad_adapter_string(self):
        text = MINIMAL + "adapter: caldmm.cal.adapters.Toy\n"
        with pytest.raises(SpecError, match="package.module:ClassName"):
            _load(text)

    def test_unknown_quantity(self):
        text = _sub(MINIMAL, "quantity: DCV", "quantity: TEMP")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'TEMP'" in str(excinfo.value)

    def test_default_interval_must_be_declared(self):
        text = _sub(MINIMAL, "default_interval: 1y", "default_interval: 5y")
        with pytest.raises(SpecError, match="not in intervals"):
            _load(text)


class TestPercentAndPpmAgree:
    def test_same_spec_two_spellings(self):
        percent = _load(MINIMAL)
        ppm = _load(_sub(MINIMAL, 
            "{pct_reading: 0.0035, pct_range: 0.0005}",
            "{ppm_reading: 35, ppm_range: 5}"))
        for value in (0.0, 1.0, 10.0):
            left = evaluate_limit(percent, Quantity.DCV, value)
            right = evaluate_limit(ppm, Quantity.DCV, value)
            assert left.limit == pytest.approx(right.limit)
            assert left.terms.ppm_reading == pytest.approx(right.terms.ppm_reading)
            assert left.terms.ppm_range == pytest.approx(right.terms.ppm_range)


class TestUnsignedExponents:
    """PyYAML is YAML 1.1: its float resolver demands a *signed* exponent.

    ``9.9e37``, ``1.0e10`` and ``1e-3`` would otherwise load as strings and
    fail much later inside a type check.  ``_SpecLoader`` resolves floats the
    YAML 1.2 way instead, which has to hold everywhere a number can appear --
    including inside ``apply:``, which is where the next spec files will put
    exponent-form delays and resolutions.
    """

    @pytest.mark.parametrize("literal,expected", [
        ("9.9e37", 9.9e37),
        ("9.9e+37", 9.9e37),
        ("1.0e10", 1.0e10),
        ("1e35", 1e35),          # the Wavetek 1281 sentinel, task B2
        ("-1.5e-3", -1.5e-3),
    ])
    def test_unsigned_exponents_resolve_as_floats(self, literal, expected):
        spec = _load(_sub(MINIMAL, "overload_sentinel: 9.9e+37",
                          f"overload_sentinel: {literal}"))
        assert isinstance(spec.overload_sentinel, float)
        assert spec.overload_sentinel == pytest.approx(expected)

    def test_exponent_form_inside_apply(self):
        """The regression the reviewer found: `apply:` bypassed the coercion."""
        text = _sub(MINIMAL, "apply: {nplc: 10}",
                    "apply: {nplc: 10, delay_s: 1e-3, resolution: 1e-6}")
        function = _load(text).function(Quantity.DCV)
        assert function.apply["delay_s"] == pytest.approx(1e-3)
        assert function.apply["resolution"] == pytest.approx(1e-6)

    def test_exponent_form_in_a_range_field(self):
        text = _sub(MINIMAL, "      - fs: 10.0\n",
                    "      - fs: 10.0\n        input_impedance_ohm: 1e10\n")
        assert _load(text).function(Quantity.DCV).ranges[0] \
            .input_impedance_ohm == pytest.approx(1e10)

    def test_integers_stay_integers_in_apply(self):
        # The 1.2 float regex would match "10" too; the inherited int resolver
        # is consulted first, so an NPLC of 10 does not silently become a float
        # in a command string.
        assert _load(MINIMAL).function(Quantity.DCV).apply["nplc"] == 10

    def test_non_numeric_string_is_rejected(self):
        text = _sub(MINIMAL, "overload_sentinel: 9.9e+37",
                    "overload_sentinel: overload")
        with pytest.raises(SpecError, match="must be a number"):
            _load(text)


class TestRequiredHeaderKeys:
    """`overload_sentinel` and `interval_basis` may not be defaulted.

    Both defaults were 34401A-shaped: a missing sentinel silently disables the
    +/-9.9e37 filter, and a missing basis reports a 3458A's ACAL-relative
    figures as "since calibration".
    """

    def test_missing_overload_sentinel_is_rejected(self):
        text = _sub(MINIMAL, "overload_sentinel: 9.9e+37\n", "")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'overload_sentinel'" in str(excinfo.value)
        assert "missing required key" in str(excinfo.value)

    def test_missing_interval_basis_is_rejected(self):
        text = _sub(MINIMAL, "interval_basis: since_cal\n", "")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'interval_basis'" in str(excinfo.value)

    def test_explicit_null_sentinel_is_allowed(self):
        # Saying "this model has none" out loud is the point; defaulting is not.
        spec = _load(_sub(MINIMAL, "overload_sentinel: 9.9e+37",
                          "overload_sentinel: null"))
        assert spec.overload_sentinel is None

    def test_non_finite_is_an_overload_even_without_a_sentinel(self):
        spec = _load(_sub(MINIMAL, "overload_sentinel: 9.9e+37",
                          "overload_sentinel: null"))
        assert spec.is_overload(float("nan"))
        assert spec.is_overload(float("inf"))
        assert not spec.is_overload(10.0)

    def test_interval_basis_round_trips(self):
        for basis in IntervalBasis:
            spec = _load(_sub(MINIMAL, "interval_basis: since_cal",
                              f"interval_basis: {basis.value}"))
            assert spec.interval_basis is basis


class TestTemperatureWindow:
    """The window is per interval, because on real instruments it is.

    The 34401A's 24-hour figures hold at 23 +/- 1 C and its 90-day and 1-year
    figures at 23 +/- 5 C (p.216).  One model-wide window passes a 24-hour
    verdict taken at 27 C.
    """


    def test_unnamed_interval_falls_back_to_the_default_window(self):
        text = _sub(MINIMAL, "intervals: [1y]\n",
                    "intervals: [1y]\n"
                    "temperature: {spec_window_c: [18.0, 28.0], tempco_ref_c: 23.0}\n")
        loaded = _load(text)
        assert loaded.temperature.window_for("1y") == (18.0, 28.0)
        assert loaded.temperature.window_for(None) == (18.0, 28.0)


    def test_per_interval_names_an_undeclared_interval(self):
        text = _sub(MINIMAL, "intervals: [1y]\n",
                    "intervals: [1y]\n"
                    "temperature: {per_interval: {24h: [22.0, 24.0]}}\n")
        with pytest.raises(SpecError) as excinfo:
            _load(text)
        assert "'24h'" in str(excinfo.value)
        assert "temperature.per_interval.24h" in str(excinfo.value)

    def test_inverted_window_is_rejected(self):
        text = _sub(MINIMAL, "intervals: [1y]\n",
                    "intervals: [1y]\n"
                    "temperature: {spec_window_c: [28.0, 18.0]}\n")
        with pytest.raises(SpecError, match="below low edge"):
            _load(text)


class TestInputImpedanceIsConditional:
    """The recorded 10 G is a *non-default, volatile* meter state.

    `INP:IMP:AUTO` powers on OFF, i.e. a fixed 10 M on every dc voltage range.
    Against a 1 kOhm source that is 100 ppm of loading error that would
    otherwise be scored against a 10 G assumption.
    """


    def test_the_fixed_10m_ranges_are_unaffected(self, spec):
        # 100 V and 1000 V are 10 M whatever INP:IMP:AUTO says.
        for fs in (100.0, 1000.0):
            result = evaluate_limit(spec, Quantity.DCV, fs, range_fs=fs)
            assert result.range_spec.input_impedance_ohm == pytest.approx(1e7)

    def test_knob_is_in_the_closed_set_and_type_checked(self):
        assert APPLY_KNOBS["input_impedance_auto"] == (bool,)
        text = _sub(MINIMAL, "apply: {nplc: 10}",
                    "apply: {nplc: 10, input_impedance_auto: 10}")
        with pytest.raises(SpecError, match="input_impedance_auto"):
            _load(text)


class TestPerRangeApply:
    """A knob whose right value changes with the range.

    The 3458A's resistance ``delay_s`` has to grow with the range -- the test
    current falls from 10 mA to 500 nA across the ladder, so the same cable
    relaxation current is decades more of the reading at the top -- and one
    figure on the function would be either too short at 10 MOhm or a wasted
    second a point at 10 Ohm.
    """

    RANGE_APPLY = _sub(MINIMAL, RANGE_LIMITS_LINE,
                       RANGE_LIMITS_LINE + "        apply: {delay_s: 0.4}\n")

    def test_a_range_may_carry_its_own_knobs(self):
        spec = _load(self.RANGE_APPLY)
        range_spec = spec.function(Quantity.DCV).ranges[0]
        assert range_spec.apply["delay_s"] == pytest.approx(0.4)

    def test_a_range_with_no_apply_carries_an_empty_one(self):
        assert _load(MINIMAL).function(Quantity.DCV).ranges[0].apply == {}

    def test_the_range_knob_reaches_the_result(self):
        result = evaluate_limit(_load(self.RANGE_APPLY), Quantity.DCV, 10.0)
        assert result.apply["delay_s"] == pytest.approx(0.4)
        # and the function's own knobs are still there
        assert result.apply["nplc"] == 10

    def test_the_range_overrides_the_function(self):
        text = _sub(self.RANGE_APPLY, "apply: {nplc: 10}",
                    "apply: {nplc: 10, delay_s: 0.05}")
        result = evaluate_limit(_load(text), Quantity.DCV, 10.0)
        assert result.apply["delay_s"] == pytest.approx(0.4)

    def test_the_vocabulary_is_the_same_closed_set(self):
        text = _sub(MINIMAL, RANGE_LIMITS_LINE,
                    RANGE_LIMITS_LINE + "        apply: {filtr: on}\n")
        with pytest.raises(SpecError, match="filtr"):
            _load(text)

    def test_types_are_checked_on_a_range_too(self):
        text = _sub(MINIMAL, RANGE_LIMITS_LINE,
                    RANGE_LIMITS_LINE + "        apply: {delay_s: true}\n")
        with pytest.raises(SpecError, match="delay_s"):
            _load(text)


class TestAcFilterSettling:
    """Filter settling is a datasheet number and belongs on the mode.

    p.204 and p.214: the fast filter settles in 0.1 s, the medium in 1 s, the
    slow in 7 s.  `filt_slow` is the default mode for nearly every ac point in
    the file, so an executor that waited the function's `settle_s` and then
    triggered would read an unsettled filter.
    """


    def test_dc_functions_have_no_filter_delay(self, spec):
        assert "delay_s" not in evaluate_limit(spec, Quantity.DCV, 10.0).apply


class TestPercentConversionIsExact:
    def test_no_binary_float_dust(self):
        # 0.28 * 1e4 is 2800.0000000000005 in binary floating point.
        text = _sub(MINIMAL, "pct_reading: 0.0035", "pct_reading: 0.28")
        terms = _load(text).function(Quantity.DCV).ranges[0].limits["1y"]
        assert terms.ppm_reading == 2800.0

    def test_equality_holds_against_the_ppm_spelling(self):
        percent = _sub(MINIMAL, "pct_reading: 0.0035, pct_range: 0.0005",
                       "pct_reading: 0.28, pct_range: 0.03")
        ppm = _sub(MINIMAL, "{pct_reading: 0.0035, pct_range: 0.0005}",
                   "{ppm_reading: 2800, ppm_range: 300}")
        assert (_load(percent).function(Quantity.DCV).ranges[0].limits["1y"]
                == _load(ppm).function(Quantity.DCV).ranges[0].limits["1y"])


class TestRegistry:
    def test_unknown_model_lists_what_is_available(self):
        with pytest.raises(SpecError) as excinfo:
            load_model("hp3458a_does_not_exist")
        message = str(excinfo.value)
        assert "hp3458a_does_not_exist" in message
        assert MODEL in message

    def test_load_model_is_cached(self):
        assert load_model(MODEL) is load_model(MODEL)


# ------------------------------------------------- conditional spec adders

HEADER = textwrap.dedent("""\
    schema_version: 1
    model_id: toy
    display_name: "Toy meter"
    role: meter
    overload_sentinel: 9.9e+37
    interval_basis: since_cal
    intervals: [1y]
    default_interval: 1y
    functions:
    """)


HV_MODEL = HEADER + textwrap.dedent("""\
      - quantity: DCV
        func: DCV
        ranges:
          - fs: 1000.0
            ppm_reading_hv: 12.0
            hv_ref_v: 1000.0
            hv_above_v: 100.0
            limits: {1y: {ppm_reading: 10.0, ppm_range: 0.1}}
    """)


class TestHighVoltageQuadraticTerm:
    """3458a-datasheet.txt:443 and :460 (PDF p.12), DCV footnote 6.

        "1000 V6   2.5 + 0.1 ... 10 (6) + 0.1 ..."
        "6. Add 12 ppm X (Vin / 1000)2 additional error for inputs > 100 vV."
        (the trailing "vV" is the extract's OCR of "V")

    Read as ppm *of reading* -- the tighter of the two readings the datasheet
    leaves open, and so the conservative one for a DUT tolerance.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(HV_MODEL)

    def test_full_scale_matches_the_datasheet(self, spec):
        # 10 ppm * 1000 V + 0.1 ppm * 1000 V = 10.0 mV + 0.1 mV = 10.1 mV
        # + 12 ppm * (1000/1000)^2 * 1000 V = 12 mV -> 22.1 mV
        result = evaluate_limit(spec, Quantity.DCV, 1000.0)
        assert result.limit == pytest.approx(22.1e-3)

    def test_the_term_is_quadratic_not_flat(self, spec):
        # 500 V: 5.0 mV + 0.1 mV base, adder 12 ppm * 0.25 * 500 V = 1.5 mV
        assert evaluate_limit(spec, Quantity.DCV, 500.0).limit \
            == pytest.approx(6.6e-3)
        # 900 V: 9.0 mV + 0.1 mV base, adder 12 ppm * 0.81 * 900 V = 8.748 mV
        assert evaluate_limit(spec, Quantity.DCV, 900.0).limit \
            == pytest.approx(17.848e-3)

    def test_below_the_threshold_nothing_is_added(self, spec):
        # 50 V: 0.5 mV + 0.1 mV, and the footnote says "inputs > 100 V", so
        # exactly 100 V is still outside it.
        assert evaluate_limit(spec, Quantity.DCV, 50.0).limit \
            == pytest.approx(0.6e-3)
        assert evaluate_limit(spec, Quantity.DCV, 100.0).limit \
            == pytest.approx(1.1e-3)

    def test_negative_input_uses_the_magnitude(self, spec):
        assert evaluate_limit(spec, Quantity.DCV, -1000.0).limit \
            == pytest.approx(22.1e-3)

    def test_describe_prints_the_term(self, spec):
        described = evaluate_limit(spec, Quantity.DCV, 1000.0).terms.describe("V")
        assert "10 ppm rdg" in described
        assert "12 ppm rdg x (|x|/1000 V)^2" in described
        assert "above 100 V" in described

    def test_the_reference_is_required_and_positive(self):
        text = _drop_line(HV_MODEL, "hv_ref_v")
        with pytest.raises(SpecError, match="hv_ref_v"):
            _load(text)
        text = _sub(HV_MODEL, "hv_ref_v: 1000.0", "hv_ref_v: 0.0")
        with pytest.raises(SpecError, match="must be positive"):
            _load(text)

    def test_the_threshold_is_required(self):
        text = _drop_line(HV_MODEL, "hv_above_v")
        with pytest.raises(SpecError, match="hv_above_v"):
            _load(text)

    def test_conditions_without_a_coefficient_are_a_typo(self):
        text = _drop_line(HV_MODEL, "ppm_reading_hv")
        with pytest.raises(SpecError, match="ppm_reading_hv"):
            _load(text)

    def test_terms_add_without_losing_the_quadratic(self):
        term = QuadraticTerm(ppm_at_ref=12.0, ref=1000.0, above=100.0)
        total = LimitTerms(10.0, 0.1, quadratic=(term,)) + LimitTerms(1.0)
        assert total.quadratic == (term,)
        assert total.limit(1000.0, 1000.0) == pytest.approx(23.1e-3)


ISQ_MODEL = HEADER + textwrap.dedent("""\
      - quantity: DCI
        func: DCI
        ranges:
          - fs: 0.22
            ppm_reading_isq: 200.0
            isq_ref_a: 1.0
            isq_above_a: 0.1
            limits: {1y: {ppm_reading: 70.0, abs_floor: 1.0e-6}}
          - fs: 2.2
            ppm_reading_isq: 10.0
            isq_ref_a: 1.0
            isq_above_a: 1.0
            limits: {1y: {ppm_reading: 95.0, abs_floor: 30.0e-6}}
    """)


class TestAmplitudeSquaredTerm:
    """5700a-operators-supplement-2014.txt:1085-1087 (PDF p.19), footnote 1.

        "1. Add to uncertainty specifications: +/-200 x I2 ppm for >100 mA on
         220 mA range; +/-10 x I2 ppm for >1 A on 2.2 A range"

    ``I`` is in amperes, so ``isq_ref_a: 1.0`` makes ``ppm_reading_isq`` the
    manual's own coefficient.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(ISQ_MODEL)

    def test_2_2a_full_scale(self, spec):
        # base 95 ppm * 2.2 A + 30 uA = 209 + 30 = 239 uA (108.6 ppm)
        # adder 10 * 2.2^2 = 48.4 ppm -> 48.4e-6 * 2.2 A = 106.48 uA
        #   total 345.48 uA = 157.0 ppm of reading, as the manual has it
        result = evaluate_limit(spec, Quantity.DCI, 2.2)
        assert result.limit == pytest.approx(345.48e-6)
        assert result.ppm == pytest.approx(157.03, rel=1e-4)

    def test_220ma_full_scale(self, spec):
        # base 70 ppm * 0.22 A + 1 uA = 15.4 + 1 = 16.4 uA (74.5 ppm)
        # adder 200 * 0.22^2 = 9.68 ppm -> 9.68e-6 * 0.22 A = 2.1296 uA
        result = evaluate_limit(spec, Quantity.DCI, 0.22)
        assert result.limit == pytest.approx(18.5296e-6)
        assert result.ppm == pytest.approx(84.2, rel=1e-3)

    def test_below_the_threshold_nothing_is_added(self, spec):
        # 50 mA on the 220 mA range: 70 ppm * 0.05 + 1 uA = 4.5 uA
        assert evaluate_limit(spec, Quantity.DCI, 0.05).limit \
            == pytest.approx(4.5e-6)
        # and 1 A on the 2.2 A range is the threshold itself, not above it:
        # 95 ppm * 1 A + 30 uA = 125 uA
        assert evaluate_limit(spec, Quantity.DCI, 1.0, range_fs=2.2).limit \
            == pytest.approx(125e-6)

    def test_describe_prints_the_term(self, spec):
        described = evaluate_limit(spec, Quantity.DCI, 2.2).terms.describe("A")
        assert "10 ppm rdg x (|x|/1 A)^2" in described
        assert "above 1 A" in described

    def test_the_coefficient_and_its_conditions_are_required_together(self):
        text = _drop_line(ISQ_MODEL, "isq_above_a: 0.1")
        with pytest.raises(SpecError, match="isq_above_a"):
            _load(text)
        text = _drop_line(ISQ_MODEL, "ppm_reading_isq: 200.0")
        with pytest.raises(SpecError, match="ppm_reading_isq"):
            _load(text)


CONNECTION_ADDER_MODEL = HEADER + textwrap.dedent("""\
      - quantity: RES
        func: OHMF
        connections:
          4w: {func: OHMF}
          2w_rcomp: {func: OHM, adder: {abs_floor: 0.2}}
        ranges:
          - fs: 1000.0
            limits: {1y: {ppm_reading: 15.0}}
          - fs: 10000.0
            connection_adders: {2w_rcomp: {abs_floor: 0.05}}
            limits: {1y: {ppm_reading: 14.0}}
    """)


class TestPerRangeConnectionAdders:
    """5700a-operators-manual-1996.txt:1958-2020 (PDF p.48-49).

    The "Two-Wire Adder, Active Compensation" column is per *range*
    (10 mOhm at 1 kOhm, 50 mOhm at 10 kOhm, 100 mOhm at 19 kOhm with 0.1 Ohm
    leads), which one per-connection adder cannot express.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(CONNECTION_ADDER_MODEL)

    def test_the_declared_basis_connection_is_untouched(self, spec):
        # 14 ppm * 10 kOhm = 0.14 Ohm
        result = evaluate_limit(spec, Quantity.RES, 10_000.0, connection="4w")
        assert result.limit == pytest.approx(0.14)
        assert result.adder is None

    def test_per_range_adder_applies_to_its_connection(self, spec):
        result = evaluate_limit(spec, Quantity.RES, 10_000.0,
                                connection="2w_rcomp")
        assert result.limit == pytest.approx(0.19)      # 0.14 + 0.05
        assert result.adder == LimitTerms(abs_floor=0.05)

    def test_per_range_wins_over_per_connection(self, spec):
        # The function declares 0.2 Ohm for this connection and the range says
        # 0.05 Ohm.  The range's figure replaces it: neither the function's
        # alone (0.34 Ohm) nor both stacked (0.39 Ohm).
        function = spec.function(Quantity.RES)
        assert function.connections[Connection.TWO_WIRE_RCOMP].adder \
            == LimitTerms(abs_floor=0.2)
        result = evaluate_limit(spec, Quantity.RES, 10_000.0,
                                connection="2w_rcomp")
        assert result.adder == LimitTerms(abs_floor=0.05)
        assert result.limit == pytest.approx(0.19)

    def test_a_range_that_says_nothing_keeps_the_per_connection_adder(self, spec):
        # 15 ppm * 1 kOhm = 0.015 Ohm, + the function's 0.2 Ohm
        result = evaluate_limit(spec, Quantity.RES, 1000.0,
                                connection="2w_rcomp")
        assert result.limit == pytest.approx(0.215)

    def test_lookup_helper(self, spec):
        ranges = {r.fs: r for r in spec.function(Quantity.RES).ranges}
        assert ranges[10_000.0].adder_for(Connection.TWO_WIRE_RCOMP) \
            == LimitTerms(abs_floor=0.05)
        assert ranges[10_000.0].adder_for(Connection.FOUR_WIRE) is None
        assert ranges[1000.0].adder_for(Connection.TWO_WIRE_RCOMP) is None

    def test_a_connection_the_function_never_declared_is_rejected(self):
        text = _sub(CONNECTION_ADDER_MODEL, "connection_adders: {2w_rcomp:",
                    "connection_adders: {2w:")
        with pytest.raises(SpecError, match="not declared by this function"):
            _load(text)

    def test_an_unknown_connection_name_is_rejected(self):
        text = _sub(CONNECTION_ADDER_MODEL, "connection_adders: {2w_rcomp:",
                    "connection_adders: {3w:")
        with pytest.raises(SpecError, match="'3w'"):
            _load(text)


BELOW_FRAC_MODEL = HEADER + textwrap.dedent("""\
      - quantity: WBV
        func: WBV
        ranges:
          - fs: 1.1
            below_frac_adder: {below_frac: 0.5, pct_reading: 0.1}
            bands:
              - freq_hz: [2000000, 10000000]
                limits: {1y: {pct_reading: 0.9, abs_floor: 403.0e-6}}
              - freq_hz: [30, 500000]
                below_frac_adder: {below_frac: 0.5, pct_reading: 0.02}
                limits: {1y: {pct_reading: 0.06, abs_floor: 403.0e-6}}
    """)


class TestBelowFractionAdder:
    """5700a-operators-supplement-2014.txt:1476-1477 (PDF p.26), footnote [1].

        "[1] For output voltages < 50 % of full range in the 33 mV, 110 mV,
         330 mV, 1.1 V, and 3.5 V ranges, add 0.1 % to the amplitude flatness
         specification."
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(BELOW_FRAC_MODEL)

    def test_below_half_range_the_adder_applies(self, spec):
        # 0.5 V is below 0.55 V: (0.9 % + 0.1 %) * 0.5 V + 403 uV
        #   = 5.0 mV + 0.403 mV = 5.403 mV
        result = evaluate_limit(spec, Quantity.WBV, 0.5, freq_hz=10.0e6)
        assert result.limit == pytest.approx(5.403e-3)
        assert result.terms.ppm_reading == pytest.approx(10_000.0)

    def test_at_and_above_half_range_it_does_not(self, spec):
        # 0.55 V is exactly half of 1.1 V, so the footnote's "< 50 %" is false
        assert evaluate_limit(spec, Quantity.WBV, 0.55, freq_hz=10.0e6).limit \
            == pytest.approx(0.009 * 0.55 + 403e-6)
        assert evaluate_limit(spec, Quantity.WBV, 0.6, freq_hz=10.0e6).limit \
            == pytest.approx(0.009 * 0.6 + 403e-6)

    def test_a_band_overrides_the_range(self, spec):
        # A band that states its own adder replaces the range's, rather than
        # stacking with it: (0.06 % + 0.02 %) * 0.5 V + 403 uV
        #   = 400 uV + 403 uV = 803 uV
        result = evaluate_limit(spec, Quantity.WBV, 0.5, freq_hz=1000.0)
        assert result.limit == pytest.approx(803e-6)

    def test_the_fraction_is_validated(self):
        text = _sub(BELOW_FRAC_MODEL, "below_frac: 0.5, pct_reading: 0.1",
                    "below_frac: 0.0, pct_reading: 0.1")
        with pytest.raises(SpecError, match="below_frac"):
            _load(text)
        text = _sub(BELOW_FRAC_MODEL, "below_frac: 0.5, pct_reading: 0.1",
                    "below_frac: 1.5, pct_reading: 0.1")
        with pytest.raises(SpecError, match="below_frac"):
            _load(text)

    def test_the_fraction_is_required(self):
        text = _sub(BELOW_FRAC_MODEL, "{below_frac: 0.5, pct_reading: 0.1}",
                    "{pct_reading: 0.1}")
        with pytest.raises(SpecError, match="below_frac"):
            _load(text)


# ------------------------------------------------- relative spec conditions

class TestRelativeTemperatureWindow:
    """5700a-operators-manual-1996.txt:1219 (PDF p.33) and :1301-1302 (p.35).

        "Absolute Uncertainty +/- 5 C from calibration temperature"
        "Temperature coefficient is an adder to uncertainty specifications
         that does not apply unless operating more than +/-5 C from
         calibration temperature."

    The centre is not in the file: it is CAL_TEMP?, read from the instrument
    at run time.
    """

    RELATIVE = _sub(MINIMAL, "intervals: [1y]\n",
                    "intervals: [1y]\n"
                    "temperature: {relative_to: cal_temp, half_width_c: 5.0}\n")

    def test_the_window_is_built_around_the_run_time_reference(self):
        temperature = _load(self.RELATIVE).temperature
        assert temperature.relative_to == "cal_temp"
        assert temperature.half_width_c == 5.0
        assert temperature.window_for(reference_c=23.0) == (18.0, 28.0)
        assert temperature.window_for(reference_c=26.5) == (21.5, 31.5)

    def test_within_window_uses_it(self):
        temperature = _load(self.RELATIVE).temperature
        assert temperature.within_window(27.0, reference_c=23.0)
        assert not temperature.within_window(29.0, reference_c=23.0)

    def test_without_a_reference_there_is_no_window(self):
        # Nothing is invented: with no CAL_TEMP? in hand the model has only
        # whatever absolute window it declares, and this one declares none.
        temperature = _load(self.RELATIVE).temperature
        assert temperature.window_for() is None
        assert temperature.within_window(40.0) is True

    def test_an_absolute_window_still_answers_when_no_reference_is_given(self):
        text = _sub(self.RELATIVE,
                    "temperature: {relative_to: cal_temp, half_width_c: 5.0}",
                    "temperature: {relative_to: cal_temp, half_width_c: 5.0,"
                    " spec_window_c: [18.0, 28.0]}")
        temperature = _load(text).temperature
        assert temperature.window_for() == (18.0, 28.0)
        assert temperature.window_for(reference_c=30.0) == (25.0, 35.0)

    def test_the_half_width_can_be_per_interval(self):
        text = _sub(MINIMAL, "intervals: [1y]\n",
                    "intervals: [24h, 1y]\n"
                    "temperature:\n"
                    "  relative_to: acal_temp\n"
                    "  half_width_c: 5.0\n"
                    "  half_width_per_interval: {24h: 1.0}\n")
        text = _sub(text, "{1y: {pct_reading", "{24h: {pct_reading: 0.0025},"
                                               " 1y: {pct_reading")
        temperature = _load(text).temperature
        assert temperature.window_for("24h", reference_c=23.0) == (22.0, 24.0)
        assert temperature.window_for("1y", reference_c=23.0) == (18.0, 28.0)

    def test_an_unknown_reference_is_rejected(self):
        text = _sub(self.RELATIVE, "relative_to: cal_temp",
                    "relative_to: room_temp")
        with pytest.raises(SpecError, match="room_temp"):
            _load(text)

    def test_a_half_width_without_a_reference_is_rejected(self):
        text = _sub(self.RELATIVE, "{relative_to: cal_temp, half_width_c: 5.0}",
                    "{half_width_c: 5.0}")
        with pytest.raises(SpecError, match="relative_to"):
            _load(text)

    def test_a_reference_without_a_half_width_is_rejected(self):
        text = _sub(self.RELATIVE, "{relative_to: cal_temp, half_width_c: 5.0}",
                    "{relative_to: cal_temp}")
        with pytest.raises(SpecError, match="half_width_c"):
            _load(text)

    def test_the_half_width_must_be_positive(self):
        text = _sub(self.RELATIVE, "half_width_c: 5.0", "half_width_c: 0.0")
        with pytest.raises(SpecError, match="must be positive"):
            _load(text)


class TestStatedConfidence:
    """5700a-operators-manual-1996.txt:1107-1116 (PDF p.30) and :1214 (p.33).

        "Fluke guarantees performance verification using specifications
         stated to 99% confidence level"
    """

    def test_absent_by_default(self):
        assert _load(MINIMAL).spec_confidence is None

    def test_a_stated_confidence_is_carried(self):
        spec = _load(_sub(MINIMAL, "role: meter\n",
                          "role: meter\nspec_confidence: 0.99\n"))
        assert spec.spec_confidence == pytest.approx(0.99)

    @pytest.mark.parametrize("value", ["0.0", "1.0", "1.5", "-0.1", "99"])
    def test_a_confidence_outside_the_open_unit_interval_is_rejected(self, value):
        text = _sub(MINIMAL, "role: meter\n",
                    f"role: meter\nspec_confidence: {value}\n")
        with pytest.raises(SpecError, match="spec_confidence"):
            _load(text)


class TestTraceability:
    """3458a-datasheet.txt:282-287 (PDF p.9) and :458 (p.12).

        "All 3458A accuracy specifications are relative to the calibration
         standards ... For DCV, 2 ppm is the traceability error from the
         factory"
        "Add 2 ppm of reading additional error for factory traceability to
         US NIST"
    """

    TRACED = _sub(MINIMAL, "    apply: {nplc: 10}\n",
                  "    apply: {nplc: 10}\n"
                  "    traceability: {ppm_reading: 2.0, confidence: 0.99,"
                  ' note: "factory traceability to US NIST"}\n')

    def test_absent_by_default(self):
        assert _load(MINIMAL).function(Quantity.DCV).traceability is None

    def test_the_block_is_carried(self):
        traceability = _load(self.TRACED).function(Quantity.DCV).traceability
        assert traceability.ppm_reading == 2.0
        assert traceability.abs_floor == 0.0
        assert traceability.confidence == pytest.approx(0.99)
        assert traceability.k is None
        assert "NIST" in traceability.note

    def test_it_never_touches_the_tolerance(self):
        # A traceability figure is an uncertainty input, not a tolerance: the
        # limit is the same with and without it.
        assert evaluate_limit(_load(self.TRACED), Quantity.DCV, 10.0).limit \
            == evaluate_limit(_load(MINIMAL), Quantity.DCV, 10.0).limit

    def test_half_width_at_a_reading(self):
        traceability = _load(self.TRACED).function(Quantity.DCV).traceability
        assert traceability.half_width(10.0) == pytest.approx(20e-6)

    def test_a_coverage_factor_is_the_other_spelling(self):
        text = _sub(self.TRACED, "confidence: 0.99", "k: 2.0")
        traceability = _load(text).function(Quantity.DCV).traceability
        assert traceability.k == 2.0
        assert traceability.confidence is None

    def test_both_spellings_at_once_are_rejected(self):
        text = _sub(self.TRACED, "confidence: 0.99", "confidence: 0.99, k: 2.0")
        with pytest.raises(SpecError, match="use one spelling"):
            _load(text)

    def test_a_confidence_outside_the_open_unit_interval_is_rejected(self):
        text = _sub(self.TRACED, "confidence: 0.99", "confidence: 1.0")
        with pytest.raises(SpecError, match="confidence"):
            _load(text)

    def test_a_non_positive_coverage_factor_is_rejected(self):
        text = _sub(self.TRACED, "confidence: 0.99", "k: 0.0")
        with pytest.raises(SpecError, match="must be positive"):
            _load(text)

    def test_an_empty_block_is_rejected(self):
        text = _sub(self.TRACED,
                    '{ppm_reading: 2.0, confidence: 0.99,'
                    ' note: "factory traceability to US NIST"}',
                    '{note: "nothing here"}')
        with pytest.raises(SpecError, match="ppm_reading"):
            _load(text)


# --------------------------------------------------------- range mechanics

OVERRANGE_MODEL = HEADER + textwrap.dedent("""\
      - quantity: DCV
        func: DCV
        ranges:
          - fs: 10.0
            overrange: 1.2
            limits: {1y: {ppm_reading: 35.0, ppm_range: 5.0}}
          - fs: 100.0
            overrange: 1.2
            limits: {1y: {ppm_reading: 45.0, ppm_range: 6.0}}
          - fs: 1000.0
            limits: {1y: {ppm_reading: 45.0, ppm_range: 6.0}}
    """)


class TestOverrange:
    """34401a-user.txt:9110 (PDF p.216) spec footnote [3].

        "20% overrange on all ranges, except 1000 Vdc, 3 A range."

    The overrange is what a meter may be *configured* on; the tolerance is
    still evaluated on the range's nominal full scale, because that is what
    the ppm-of-range term is written against.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(OVERRANGE_MODEL)

    def test_holds_is_strict_by_default(self, spec):
        ten = spec.function(Quantity.DCV).ranges[0]
        assert ten.holds(10.0)
        assert not ten.holds(11.0)
        assert ten.holds(11.0, allow_overrange=True)
        assert not ten.holds(13.0, allow_overrange=True)

    def test_a_range_without_an_overrange_never_stretches(self, spec):
        thousand = spec.function(Quantity.DCV).ranges[2]
        assert not thousand.holds(1100.0, allow_overrange=True)

    def test_meter_configuration_uses_the_overrange(self, spec):
        function = spec.function(Quantity.DCV)
        assert select_range(function, 11.0).fs == 100.0
        assert select_range(function, 11.0, for_meter_config=True).fs == 10.0
        assert select_range(function, 13.0, for_meter_config=True).fs == 100.0

    def test_the_tolerance_stays_on_the_nominal_range(self, spec):
        # 11 V is judged on the 100 V range, whose ppm-of-range term is
        # written against 100 V: 45 ppm * 11 V + 6 ppm * 100 V = 1.095 mV
        result = evaluate_limit(spec, Quantity.DCV, 11.0)
        assert result.range_fs == 100.0
        assert result.limit == pytest.approx(45e-6 * 11.0 + 6e-6 * 100.0)

    def test_an_overrange_below_one_is_rejected(self):
        text = _sub(OVERRANGE_MODEL, "overrange: 1.2", "overrange: 0.9")
        with pytest.raises(SpecError, match="overrange"):
            _load(text)


TEST_CURRENT_MODEL = HEADER + textwrap.dedent("""\
      - quantity: RES
        connections:
          4w: {func: OHMF}
        ranges:
          - fs: 100.0
            test_current_a: 1.0e-3
            limits: {1y: {ppm_reading: 12.0, ppm_range: 5.0}}
    """)


class TestTestCurrent:
    """The current an ohms range sources, for the calibrator load window.

    ``3458a-user-guide.txt:3053`` (Table 3-6, PDF p.89): the 100 ohm range
    sources 1 mA.  The 5700A states its 19 ohm uncertainty only for an 8-11 mA
    load, so the pairing has to be able to see this.
    """

    def test_it_is_parsed(self):
        assert _load(TEST_CURRENT_MODEL).function(
            Quantity.RES).ranges[0].test_current_a == pytest.approx(1e-3)

    def test_a_range_without_one_says_none(self):
        assert _load(OVERRANGE_MODEL).function(
            Quantity.DCV).ranges[0].test_current_a is None

    def test_it_never_touches_a_tolerance(self):
        # 12 ppm x 100 ohm + 5 ppm x 100 ohm = 1.2 mohm + 0.5 mohm
        spec = _load(TEST_CURRENT_MODEL)
        assert evaluate_limit(spec, Quantity.RES, 100.0).limit \
            == pytest.approx(1.7e-3)

    @pytest.mark.parametrize("bad", ["0.0", "-1.0e-3"])
    def test_a_non_positive_current_is_rejected(self, bad):
        text = _sub(TEST_CURRENT_MODEL, "test_current_a: 1.0e-3",
                    f"test_current_a: {bad}")
        with pytest.raises(SpecError, match="test_current_a"):
            _load(text)


MODE_FLOOR_MODEL = HEADER + textwrap.dedent("""\
      - quantity: ACV
        func: ACV
        modes:
          - {id: sync, freq_hz: [1, 2000000], apply: {ac_method: SYNC}}
          - {id: ana, freq_hz: [10, 2000000], apply: {ac_method: ANA}}
        ranges:
          - fs: 0.01
            min_reading_frac: 0.30
            min_reading_frac_by_mode: {sync: 0.12}
            bands:
              - {freq_hz: [1000, 20000], modes: [sync],
                 limits: {1y: {pct_reading: 0.03, pct_range: 0.011}}}
              - {freq_hz: [1000, 20000], modes: [ana],
                 limits: {1y: {pct_reading: 0.1, pct_range: 0.05}}}
    """)


class TestModeAwareReadingFloor:
    """3458a-datasheet.txt:999 (PDF p.25) SYNC footnote 2 vs :1209 (p.28) ANA.

        SYNC: "Specifications apply full scale to 10% of full scale"
        ANA:  "Specifications apply full scale to 1/4 full scale"

    One per-range floor has to be the most restrictive of the three
    techniques, which then removes points that the pinned mode specifies.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(MODE_FLOOR_MODEL)

    def test_the_pinned_mode_floor_wins(self, spec):
        # 2.2 mV is above SYNC's 0.12 * 10 mV = 1.2 mV floor
        result = evaluate_limit(spec, Quantity.ACV, 0.0022, freq_hz=1000.0,
                                mode="sync")
        assert result is not None
        assert result.limit == pytest.approx(0.0003 * 0.0022 + 0.00011 * 0.01)

    def test_a_mode_without_its_own_floor_keeps_the_range_floor(self, spec):
        # ... and 2.2 mV is below ANA's 0.30 * 10 mV = 3.0 mV
        assert evaluate_limit(spec, Quantity.ACV, 0.0022, freq_hz=1000.0,
                              mode="ana") is None

    def test_an_unpinned_point_keeps_the_range_floor(self, spec):
        assert evaluate_limit(spec, Quantity.ACV, 0.0022, freq_hz=1000.0) is None

    def test_below_every_floor_is_still_unspecified(self, spec):
        assert evaluate_limit(spec, Quantity.ACV, 0.001, freq_hz=1000.0,
                              mode="sync") is None

    def test_lookup_helper(self, spec):
        range_spec = spec.function(Quantity.ACV).ranges[0]
        assert range_spec.min_reading_frac_for("sync") == 0.12
        assert range_spec.min_reading_frac_for("ana") == 0.30
        assert range_spec.min_reading_frac_for(None) == 0.30

    def test_an_undeclared_mode_is_rejected(self):
        text = _sub(MODE_FLOOR_MODEL, "min_reading_frac_by_mode: {sync: 0.12}",
                    "min_reading_frac_by_mode: {synk: 0.12}")
        with pytest.raises(SpecError, match="synk"):
            _load(text)


# ------------------------------------------------------- load-current window

LOAD_CURRENT_MODEL = HEADER + textwrap.dedent("""\
      - quantity: RES
        func: OHMS
        connections: [4w, 2w_rcomp]
        ranges:
          - fs: 100.0
            discrete: true
            load_current:
              min_a: 8.0e-3
              max_a: 11.0e-3
              imax_a: 70.0e-3
              unit: mA
              k_under_4w: 3.5
              k_under_4w_unit: ppm
              k_under_2w: 4.4
              over_unit: mA
              k_over_4w: 1.0e-2
            limits: {1y: {ppm_reading: 20.0}}
          - fs: 10000.0
            discrete: true
            load_current:
              min_a: 100.0e-6
              max_a: 500.0e-6
              imax_a: 7.0e-3
              unit: uA
              k_under_4w: 50.0
              k_under_4w_unit: ppm
              k_under_2w: 5000.0
              over_unit: mA
              k_over_4w: 2.0
            limits: {1y: {ppm_reading: 14.0}}
    """)


class TestLoadCurrentWindow:
    """5700a-operators-manual-1996.txt:1960-2009 (PDF p.49) and :2030-2085 (p.50).

        "Full Spec Load Range IL - IU"  ...  "Refer to current derating
         factors table for loads outside of this range."

        "For I < IL, errors occur due to thermally generated voltages within
         the 5720A. ... Error = K(IL - I)/(IL x I) ... Error is in mOhm for all
         two-wire comp values and four-wire short, and in ppm for the remaining
         four-wire values. ... I and IL are expressed in mA for short to
         1.9 kOhm; I and IL are expressed in uA for 10 kOhm to 100 MOhm"

        "For IU < I < IMAX errors occur due to self-heating ... Error in ppm =
         K(I2-IU2) ... I and IU are expressed in mA for short to 19 kOhm; uA
         for 100 kOhm to 100 MOhm"

    The unit rule is the trap: the same table switches the under-current
    current unit at 10 kOhm and the over-current one at 100 kOhm, so a range
    can (and the 10 kOhm one does) take uA in one equation and mA in the other.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(LOAD_CURRENT_MODEL)

    @pytest.fixture(scope="class")
    def ohm100(self, spec):
        return spec.function(Quantity.RES).ranges[0].load_current

    @pytest.fixture(scope="class")
    def ohm10k(self, spec):
        return spec.function(Quantity.RES).ranges[1].load_current

    def test_the_window_is_carried_in_amperes(self, ohm100, ohm10k):
        assert ohm100.min_a == pytest.approx(8e-3)
        assert ohm100.max_a == pytest.approx(11e-3)
        assert ohm100.imax_a == pytest.approx(70e-3)
        assert ohm10k.min_a == pytest.approx(100e-6)
        assert ohm10k.max_a == pytest.approx(500e-6)

    def test_the_window_edges_are_inclusive(self, ohm100):
        # "Full Spec Load Range IL - IU" is a closed interval: the derating
        # table is for loads *outside* it.
        assert ohm100.in_window(8e-3)
        assert ohm100.in_window(11e-3)
        assert ohm100.in_window(9e-3)
        assert not ohm100.in_window(7.999e-3)
        assert not ohm100.in_window(11.001e-3)

    def test_under_current_four_wire_is_ppm_in_milliamps(self, ohm100):
        # 3.5 x (8 - 1) / (8 x 1) = 3.0625 ppm at 1 mA
        assert ohm100.under_error(1e-3) == pytest.approx((3.0625, "ppm"))

    def test_under_current_two_wire_is_milliohms(self, ohm100):
        # 4.4 x (8 - 1) / (8 x 1) = 3.85 mOhm at 1 mA
        value, unit = ohm100.under_error(1e-3, two_wire=True)
        assert value == pytest.approx(3.85)
        assert unit == "mohm"

    def test_the_current_unit_switches_at_10k(self, ohm10k):
        # 50 x (100 - 10) / (100 x 10) = 4.5 ppm at 10 uA -- in MICROamps.
        # Read in mA the same row would give 4500 ppm.
        assert ohm10k.under_error(10e-6) == pytest.approx((4.5, "ppm"))
        # 5000 x (100 - 10) / (100 x 10) = 450 mOhm
        assert ohm10k.under_error(10e-6, two_wire=True) == \
            pytest.approx((450.0, "mohm"))

    def test_inside_the_window_there_is_no_under_current_error(self, ohm100):
        assert ohm100.under_error(8e-3) is None
        assert ohm100.under_error(10e-3) is None

    def test_over_current_is_ppm_and_quadratic(self, ohm100):
        # 1e-2 x (20^2 - 11^2) = 1e-2 x 279 = 2.79 ppm at 20 mA
        assert ohm100.over_error(20e-3) == pytest.approx((2.79, "ppm"))
        assert ohm100.over_error(11e-3) is None

    def test_the_over_current_unit_is_its_own(self, ohm10k):
        # IU is 500 uA but the over-current equation takes mA on this row:
        #   2.0 x (1.0^2 - 0.5^2) = 1.5 ppm at 1 mA
        assert ohm10k.over_error(1e-3) == pytest.approx((1.5, "ppm"))

    def test_a_window_needs_both_edges(self):
        text = _drop_line(LOAD_CURRENT_MODEL, "max_a: 11.0e-3")
        with pytest.raises(SpecError, match="max_a"):
            _load(text)

    def test_the_edges_must_be_ordered(self):
        text = _sub(LOAD_CURRENT_MODEL, "max_a: 11.0e-3", "max_a: 4.0e-3")
        with pytest.raises(SpecError, match="max_a"):
            _load(text)

    def test_imax_is_not_below_the_window(self):
        text = _sub(LOAD_CURRENT_MODEL, "imax_a: 70.0e-3", "imax_a: 9.0e-3")
        with pytest.raises(SpecError, match="imax_a"):
            _load(text)

    def test_the_current_unit_is_validated(self):
        text = _sub(LOAD_CURRENT_MODEL, "\n        unit: mA",
                    "\n        unit: A")
        with pytest.raises(SpecError, match="unit"):
            _load(text)

    def test_the_error_unit_is_validated(self):
        text = _sub(LOAD_CURRENT_MODEL, "k_under_4w_unit: ppm",
                    "k_under_4w_unit: ohm")
        with pytest.raises(SpecError, match="k_under_4w_unit"):
            _load(text)

    def test_an_over_current_constant_needs_its_unit(self):
        text = _sub(LOAD_CURRENT_MODEL,
                    "        over_unit: mA\n        k_over_4w: 1.0e-2",
                    "        k_over_4w: 1.0e-2")
        with pytest.raises(SpecError, match="over_unit"):
            _load(text)

    def test_a_dangling_over_current_unit_is_refused(self):
        text = _sub(LOAD_CURRENT_MODEL, "        k_over_4w: 1.0e-2\n", "")
        with pytest.raises(SpecError, match="over_unit"):
            _load(text)

    @pytest.mark.parametrize("key, blanked", [
        ("min_a", "        min_a: 8.0e-3\n"),
        ("max_a", "        max_a: 11.0e-3\n"),
        ("unit", "\n        unit: mA"),
    ])
    def test_a_required_key_written_with_no_value_is_refused(self, key,
                                                             blanked):
        # `min_a:` with nothing after it parses as None, which `_check_keys`
        # counts as present.  Left unguarded it reaches the arithmetic and
        # raises TypeError from inside the loader instead of a SpecError
        # naming the file and the path.
        text = _sub(LOAD_CURRENT_MODEL, blanked.rstrip("\n"),
                    blanked.rstrip("\n").split(":")[0] + ":")
        with pytest.raises(SpecError, match=key):
            _load(text)

    def test_a_derating_constant_needs_its_error_unit(self):
        # Symmetric with the over-current pair: a half-written row must not
        # quietly default to ppm, because the one row where it is not ppm is
        # the short, and a ppm of a 0 ohm output is identically zero.
        text = _sub(LOAD_CURRENT_MODEL, "        k_under_4w_unit: ppm\n", "")
        with pytest.raises(SpecError, match="k_under_4w_unit"):
            _load(text)

    def test_a_range_without_the_key_says_nothing(self):
        assert _load(MINIMAL).function(Quantity.DCV).ranges[0].load_current \
            is None

    def test_it_is_never_part_of_a_tolerance(self, spec):
        # A load-current derating is a *test-condition* adder the planner
        # applies knowing the meter's ohms current; evaluate_limit does not
        # know it and must not invent it.
        result = evaluate_limit(spec, Quantity.RES, 100.0, connection="4w",
                                interval="1y")
        assert result.limit == pytest.approx(20e-6 * 100.0)


class TestShippedFilesStillLoad:
    def test_every_shipped_model_loads_unchanged(self):
        # Every schema extension is optional, and every shipped file parses.
        # (fluke5700a.yaml now names several of them -- see test_specs_b2.py.)
        for model_id in available_models():
            spec = load_model(model_id)
            assert spec.model_id == model_id
            for function in spec.functions.values():
                assert function.ranges


REGIME_MODEL = HEADER + textwrap.dedent("""\
      - quantity: DCV
        func: DCV
        connections:
          2w: {func: DCV}
          4w: {func: DCVF, adder: {abs_floor: 1.0e-6}}
        ranges:
          - fs: 0.1
            regime_adder: {ppm_range: 17.0, note: "no MATH NULL, range < 4 min"}
            limits: {1y: {ppm_reading: 9.0, ppm_range: 3.0}}
          - fs: 1000.0
            ppm_reading_hv: 12.0
            hv_ref_v: 1000.0
            hv_above_v: 100.0
            regime_adder: {ppm_reading: 1.0, note: "regime"}
            limits: {1y: {ppm_reading: 10.0, ppm_range: 0.1}}
    """)


class TestRegimeAdder:
    """An adder for the conditions a run is actually taken in.

    ``3458a-datasheet.txt:454-456`` (PDF p.12): "Without MATH NULL and for
    fixed range less than 4 minutes, add 0.25 ppm of range to 10 V, 1.7 ppm of
    range to 1 V and 17 ppm of range to 0.1 V."  Nothing in this code base
    issues MATH NULL or holds a range four minutes, so that regime is not a
    condition to be checked -- it is the one every reading is taken in, and the
    adder is unconditional.
    """

    @pytest.fixture(scope="class")
    def spec(self):
        return _load(REGIME_MODEL)

    def test_it_is_parsed_onto_the_range(self, spec):
        adder = spec.function(Quantity.DCV).ranges[0].regime_adder
        assert adder.ppm_range == pytest.approx(17.0)
        assert adder.ppm_reading == 0.0
        assert adder.abs_floor == 0.0
        assert adder.note == "no MATH NULL, range < 4 min"

    def test_a_range_without_one_says_none(self):
        assert _load(OVERRANGE_MODEL).function(
            Quantity.DCV).ranges[0].regime_adder is None

    def test_it_is_added_to_every_evaluation(self, spec):
        # 9 ppm x 0.1 V + 3 ppm x 0.1 V = 1.2 uV, + 17 ppm x 0.1 V = 2.9 uV
        result = evaluate_limit(spec, Quantity.DCV, 0.1)
        assert result.limit == pytest.approx(2.9e-6)

    def test_the_printed_row_is_left_alone(self, spec):
        """``base_terms`` is what the data sheet prints; ``terms`` is applied."""
        result = evaluate_limit(spec, Quantity.DCV, 0.1)
        assert result.base_terms.ppm_range == pytest.approx(3.0)
        assert result.base_terms.limit(0.1, 0.1) == pytest.approx(1.2e-6)
        assert result.base_terms.regime == ()
        assert len(result.terms.regime) == 1
        # ...and the range's own `limits:` table is untouched, so the extract
        # audit still compares against the data sheet's cells.
        assert spec.function(Quantity.DCV).ranges[0].limits[
            "1y"].ppm_range == pytest.approx(3.0)

    def test_describe_names_it(self, spec):
        text = evaluate_limit(spec, Quantity.DCV, 0.1).terms.describe("V")
        assert text == ("9 ppm rdg + 3 ppm rng "
                        "+17 ppm rng (no MATH NULL, range < 4 min)")

    def test_it_stacks_with_the_connection_adder(self, spec):
        # 1.2 uV row + 1 uV wiring adder + 1.7 uV regime adder
        result = evaluate_limit(spec, Quantity.DCV, 0.1, connection="4w")
        assert result.limit == pytest.approx(3.9e-6)

    def test_it_stacks_with_the_quadratic_term(self, spec):
        # 10 ppm x 1000 V + 0.1 ppm x 1000 V + 12 ppm x 1000 V = 22.1 mV,
        # + the regime adder's 1 ppm of reading = 23.1 mV
        result = evaluate_limit(spec, Quantity.DCV, 1000.0, range_fs=1000.0)
        assert result.limit == pytest.approx(23.1e-3)

    def test_an_empty_block_is_rejected(self):
        text = _sub(REGIME_MODEL,
                    'regime_adder: {ppm_range: 17.0, note: "no MATH NULL, range < 4 min"}',
                    'regime_adder: {note: "no terms"}')
        with pytest.raises(SpecError, match="empty specification"):
            _load(text)

    def test_a_missing_note_is_rejected(self):
        text = _sub(REGIME_MODEL,
                    'regime_adder: {ppm_range: 17.0, note: "no MATH NULL, range < 4 min"}',
                    "regime_adder: {ppm_range: 17.0}")
        with pytest.raises(SpecError, match="note"):
            _load(text)

    def test_a_blank_note_is_rejected(self):
        text = _sub(REGIME_MODEL,
                    'regime_adder: {ppm_range: 17.0, note: "no MATH NULL, range < 4 min"}',
                    'regime_adder: {ppm_range: 17.0, note: "   "}')
        with pytest.raises(SpecError, match="note"):
            _load(text)

    def test_an_unknown_key_is_rejected(self):
        text = _sub(REGIME_MODEL,
                    'regime_adder: {ppm_range: 17.0, note: "no MATH NULL, range < 4 min"}',
                    'regime_adder: {ppm_rnage: 17.0, note: "typo"}')
        with pytest.raises(SpecError, match="ppm_rnage"):
            _load(text)

    def test_only_the_3458a_dc_ranges_declare_one(self):
        """Inert for every file that omits it -- pinned across the catalogue."""
        declared = {(model_id, function.quantity.name, range_spec.fs)
                    for model_id in available_models()
                    for function in load_model(model_id).functions.values()
                    for range_spec in function.ranges
                    if range_spec.regime_adder is not None}
        assert declared == {("hp3458a", "DCV", 0.1), ("hp3458a", "DCV", 1.0),
                            ("hp3458a", "DCV", 10.0)}


class TestHp3458aWideAcModesStayAbove2MHz:
    """H10 -- the ``ACBAND > 2 MHz`` tables must not be reachable below 2 MHz.

    The data sheet heads the two SYNC tables ``ACBAND <= 2 MHz``
    (``3458a-datasheet.txt:984``, PDF p.25) and ``ACBAND > 2 MHz`` (``:1007``),
    i.e. the regime is chosen by the *declared bandwidth*, not by the tone --
    which is why the wide table has a 45 Hz to 100 kHz column at all.  The
    adapter sets ACBAND from the test point's own frequency, so at any tone at
    or below 2 MHz the instrument is in the ``<= 2 MHz`` regime and only that
    regime's rows may be selected.  Restricting the two ``*_wide`` modes to
    2-10 MHz is what keeps the planner honest; the rows themselves stay in the
    file, for the extract audit and for an operator who names the mode.
    """

    @pytest.fixture(scope="class")
    def dmm3458(self):
        return load_model("hp3458a")

    @pytest.mark.parametrize("mode_id", ["sync_wide", "rndm_wide"])
    def test_the_mode_span_starts_just_above_2mhz(self, dmm3458, mode_id):
        # The data sheet's regime is ACBAND *> 2 MHz* and this schema's edges
        # are inclusive, so the strict ">" is spelled as +1 Hz.
        mode = dmm3458.function(Quantity.ACV).mode(mode_id)
        assert mode.freq_low_hz == pytest.approx(2000001.0)
        assert mode.freq_high_hz == pytest.approx(10.0e6)
        assert not mode.covers(1.9e6)
        assert not mode.covers(2.0e6)
        assert mode.covers(2000001.0)
        assert mode.covers(3.0e6)

    def test_no_range_picks_a_wide_mode_at_exactly_2mhz(self, dmm3458):
        """At 2.000000 MHz the engine sends ACBAND (2e6, 2e6): the <= 2 MHz regime."""
        from caldmm.cal.plan import _pick_mode

        function = dmm3458.function(Quantity.ACV)
        for range_spec in function.ranges:
            picked = _pick_mode(function, range_spec, range_spec.fs * 0.5,
                                2.0e6, "1y")
            assert picked is None or not picked.endswith("_wide"), range_spec.fs

    def test_the_10mv_range_is_unspecified_at_exactly_2mhz(self, dmm3458):
        """Not "plain sync" -- the <= 2 MHz 10 mV row is blank above 300 kHz.

        With an inclusive 2000000 edge the planner picked ``sync_wide`` here
        and judged a 5 mV reading against 357 uV.  The honest answer in the
        regime the instrument is actually in is that there is no row.
        """
        from caldmm.cal.plan import _pick_mode

        function = dmm3458.function(Quantity.ACV)
        ten_mv = [r for r in function.ranges if r.fs == 0.01][0]
        assert _pick_mode(function, ten_mv, 0.005, 2.0e6, "1y") is None
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.005, range_fs=0.01,
                              freq_hz=2.0e6, mode="sync", interval="1y") is None

    def test_the_1v_range_takes_plain_sync_at_exactly_2mhz(self, dmm3458):
        # p.25 SYNC, ACBAND <= 2 MHz, 100 mV - 10 V, 1 MHz to 2 MHz:
        #   1.5 % + 0.01 % -> 15000 ppm x 0.5 V + 100 ppm x 1 V = 7.6 mV
        from caldmm.cal.plan import _pick_mode

        function = dmm3458.function(Quantity.ACV)
        one_volt = [r for r in function.ranges if r.fs == 1.0][0]
        assert _pick_mode(function, one_volt, 0.5, 2.0e6, "1y") == "sync"
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.5, range_fs=1.0,
                              freq_hz=2.0e6, mode="sync",
                              interval="1y").limit == pytest.approx(7.6e-3)

    @pytest.mark.parametrize("freq_hz, limit", [
        # p.25 SYNC, ACBAND <= 2 MHz, 10 mV range:
        #   50-100 kHz  0.5 % + 0.011 % -> 50 uV + 1.1 uV = 51.1 uV
        (70.0e3, 51.1e-6),
        #   100-300 kHz 4.0 % + 0.02 %  -> 400 uV + 2 uV  = 402 uV
        (200.0e3, 402e-6),
    ])
    def test_the_10mv_range_picks_plain_sync(self, dmm3458, freq_hz, limit):
        from caldmm.cal.plan import _pick_mode

        function = dmm3458.function(Quantity.ACV)
        ten_mv = [r for r in function.ranges if r.fs == 0.01][0]
        assert _pick_mode(function, ten_mv, 0.01, freq_hz, "1y") == "sync"
        assert evaluate_limit(dmm3458, Quantity.ACV, 0.01, range_fs=0.01,
                              freq_hz=freq_hz, mode="sync",
                              interval="1y").limit == pytest.approx(limit)

    def test_above_2mhz_the_wide_mode_is_still_chosen(self, dmm3458):
        from caldmm.cal.plan import _pick_mode

        function = dmm3458.function(Quantity.ACV)
        ten_mv = [r for r in function.ranges if r.fs == 0.01][0]
        assert _pick_mode(function, ten_mv, 0.01, 3.0e6, "1y") == "sync_wide"

    def test_no_generated_plan_pins_a_wide_mode_below_2mhz(self):
        from caldmm.cal.plan import generate_plan

        for dut_role in ("meter", "source"):
            plan = generate_plan("fluke5700a", "hp3458a", dut_role=dut_role,
                                 quantities=[Quantity.ACV], interval="1y")
            for point in plan.points:
                if point.mode and point.mode.endswith("_wide"):
                    assert point.frequency_hz > 2.0e6, (dut_role, point)

    def test_naming_a_wide_mode_still_evaluates_its_row(self, dmm3458):
        # The >2 MHz rows are real data -- they apply when ACBAND says so --
        # so an explicit mode= still reaches them; only the planner's default
        # is constrained.  p.25 wide table, 10 mV, 45 Hz-100 kHz: 0.09 % +
        # 0.06 % -> 9 uV + 6 uV = 15 uV.
        result = evaluate_limit(dmm3458, Quantity.ACV, 0.01, range_fs=0.01,
                                freq_hz=70.0e3, mode="sync_wide", interval="1y")
        assert result.limit == pytest.approx(15e-6)


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

    import caldmm.cal.quantities
    import caldmm.cal.specs

    spec = caldmm.cal.specs.load_model("hp3458a")
    assert spec.model_id == "hp3458a"
    assert caldmm.cal.specs.evaluate_limit(
        spec, caldmm.cal.quantities.Quantity.DCV, 10.0).limit > 0

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal pulled in drivers: " + repr(leaked)
    print("OK")
    """)


def test_cal_imports_with_no_hardware_libraries():
    """The architectural invariant, pinned in a clean interpreter.

    ``cal`` must load and evaluate a spec with ``Gpib``, ``serial``, ``pyvisa``
    and ``smbus`` absent and un-importable, and without dragging in a driver.
    A subprocess is used so the root conftest's mock stubs are not in the way.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

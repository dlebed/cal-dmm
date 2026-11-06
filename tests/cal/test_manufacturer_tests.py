"""The 3458A Calibration Manual's own verification points, and a full run.

Pins four things a fully automated 3458A verification depends on:

* every row of Cal Manual Tables 4-1 to 4-6 is in the default plan, on the
  range, tone, wiring and ac technique the manual names;
* every range of every 3458A function is exercised;
* the whole run needs exactly **one** re-cable (voltage/ohms, then current);
* no ac-volts point asks the 5700A for more than its Series II Extended
  Specifications allow -- restated here from that document, independently of
  ``fluke5700a.yaml``, so a spec-file slip cannot hide itself.

See ``docs/3458a/cal-manual-verification.md``.
"""

import math
from types import SimpleNamespace

import pytest

from caldmm.cal.plan import (
    MANUFACTURER_TESTS,
    PlanError,
    generate_plan,
    manufacturer_test_for,
    manufacturer_tests,
    validate_plan,
)
from caldmm.cal.quantities import Connection, Quantity, Terminals
from caldmm.cal.specs import PairRole, evaluate_limit, load_model


@pytest.fixture(scope="module")
def hp3458a():
    return load_model("hp3458a")


@pytest.fixture(scope="module")
def full_plan():
    return generate_plan("fluke5700a", "hp3458a", dut_role="meter")


@pytest.fixture(scope="module")
def manual_plan():
    return generate_plan("fluke5700a", "hp3458a", dut_role="meter",
                         point_set="manufacturer")


def _manual_rows():
    return [t for t in manufacturer_tests("hp3458a") if not t.supplementary]


# ------------------------------------------------------------- the table

class TestTheTable:
    def test_it_is_tables_4_1_to_4_6_and_the_ohms_offsets(self):
        counts = {}
        for test in _manual_rows():
            table = test.test_id.split(" #")[0]
            counts[table] = counts.get(table, 0) + 1
        assert counts == {"CM 4-1": 5, "CM 4-2": 8, "CM 4-3": 10, "CM 4-4": 5,
                          "CM 4-5": 5, "CM ohms 2w offset": 1,
                          "CM ohms 4w offset": 1, "CM 4-6": 7}

    def test_ids_are_unique(self):
        ids = [t.test_id for t in manufacturer_tests("hp3458a")]
        assert len(ids) == len(set(ids))

    def test_only_the_3458a_has_one(self):
        assert set(MANUFACTURER_TESTS) == {"hp3458a"}
        assert manufacturer_tests("fluke5700a") == ()

    @pytest.mark.parametrize("test_id, value, range_fs, frequency", [
        ("CM 4-2 #3", 1.0, 10.0, 0.0),       # 1 V on the 10 V range
        ("CM 4-2 #5", -10.0, 10.0, 0.0),
        ("CM 4-3 #3", 1.0, 10.0, 1e3),       # 10 % of the 10 V ac range
        ("CM 4-3 #4", 10.0, 10.0, 20.0),
        ("CM 4-3 #8", 10.0, 10.0, 1e6),
        ("CM 4-3 #10", 700.0, 1000.0, 1e3),
        ("CM 4-6 #7", 1e7, 1e7, 0.0),
    ])
    def test_rows_match_the_manual(self, test_id, value, range_fs, frequency):
        test = next(t for t in manufacturer_tests("hp3458a")
                    if t.test_id == test_id)
        assert (test.value, test.range_fs, test.frequency_hz) == \
            (value, range_fs, frequency)

    def test_the_ac_rows_use_the_manuals_dut_configuration(self):
        """SETACV ANA, ACBAND 10,2E6 (p.65-66); NPLC > 10 zeroes p.29."""
        for test in manufacturer_tests("hp3458a"):
            if test.quantity is Quantity.ACV:
                assert test.mode == "ana"
                assert tuple(test.apply["ac_band"]) == (10.0, 2.0e6)
                assert test.apply["nplc"] > 10

    def test_a_result_row_is_matched_by_setpoint(self):
        row = SimpleNamespace(quantity=Quantity.ACV, nominal=10.0,
                              range_arg=10.0, frequency_hz=1e6,
                              connection=Connection.TWO_WIRE, mode="ana")
        assert manufacturer_test_for("hp3458a", row).test_id == "CM 4-3 #8"
        sync = SimpleNamespace(**{**vars(row), "mode": "sync"})
        assert manufacturer_test_for("hp3458a", sync) is None


# ------------------------------------------------- the card vs data sheet

class TestCardLimits:
    """The comparison ``docs/3458a/cal-manual-verification.md`` states."""

    @staticmethod
    def _ratio(spec, test):
        result = evaluate_limit(
            spec, test.quantity, test.value, range_fs=test.range_fs,
            freq_hz=test.frequency_hz or None, connection=test.connection,
            mode=test.mode, interval="1y", role=PairRole.DUT)
        return result.limit / test.card_limit

    def test_every_manual_row_is_specified(self, hp3458a):
        for test in _manual_rows():
            assert self._ratio(hp3458a, test) > 0, test.test_id

    def test_current_and_ohms_are_tighter_than_the_card(self, hp3458a):
        for test in _manual_rows():
            if test.quantity in (Quantity.DCI, Quantity.RES):
                assert self._ratio(hp3458a, test) <= 1.0 + 1e-9, test.test_id

    def test_the_card_is_tighter_than_the_ana_data_sheet(self, hp3458a):
        """Why the report prints the card beside the verdict."""
        ratios = [self._ratio(hp3458a, t) for t in _manual_rows()
                  if t.quantity is Quantity.ACV]
        assert len(ratios) == 10
        assert 1.09 < min(ratios) and max(ratios) < 1.28, ratios

    def test_opt002_is_never_looser_than_standard(self):
        for test in _manual_rows():
            assert test.card_limit_opt002 <= test.card_limit, test.test_id


# --------------------------------------------------------------- the plan

class TestFullPlan:
    def test_it_validates(self, full_plan):
        assert validate_plan(full_plan, "fluke5700a", "hp3458a") == []

    def test_every_manual_row_is_planned(self, full_plan):
        planned = {manufacturer_test_for("hp3458a", p).test_id
                   for p in full_plan.points
                   if manufacturer_test_for("hp3458a", p) is not None}
        wanted = {t.test_id for t in manufacturer_tests("hp3458a")}
        assert planned == wanted
        labelled = [p for p in full_plan.points if p.label.startswith("[")]
        assert len(labelled) == len(wanted)
        assert not [s for s in full_plan.skipped
                    if s.point.label.startswith("[")]

    def test_one_recable(self, full_plan):
        groups = full_plan.groups()
        assert [g.terminals for g in groups] == [Terminals.VOLTAGE,
                                                 Terminals.CURRENT]
        assert set(groups[0].quantities) == {Quantity.DCV, Quantity.ACV,
                                             Quantity.RES}
        assert set(groups[1].quantities) == {Quantity.DCI, Quantity.ACI}

    def test_every_range_of_every_function_is_exercised(self, full_plan,
                                                        hp3458a):
        """All of them -- one wiring or the other -- including ACI and 1 Gohm."""
        touched = {(p.quantity, float(p.range_arg)) for p in full_plan.points}
        missing = []
        for quantity, function in hp3458a.functions.items():
            for range_spec in function.ranges:
                arg = (range_spec.range_arg if range_spec.range_arg is not None
                       else range_spec.fs)
                if (quantity, float(arg)) not in touched:
                    missing.append(f"{quantity.name} {arg:g}")
        assert missing == []

    def test_both_ac_techniques_on_every_ac_volts_range(self, full_plan,
                                                       hp3458a):
        for range_spec in hp3458a.function(Quantity.ACV).ranges:
            modes = {p.mode for p in full_plan.points
                     if p.quantity is Quantity.ACV
                     and p.range_arg == range_spec.range_arg}
            assert {"sync", "ana"} <= modes, (range_spec.fs, modes)

    def test_both_resistance_wirings_are_run(self, full_plan):
        wirings = {p.connection for p in full_plan.points
                   if p.quantity is Quantity.RES}
        assert wirings == {Connection.FOUR_WIRE, Connection.TWO_WIRE}


class TestManufacturerOnly:
    def test_it_is_the_manuals_rows_alone(self, manual_plan):
        assert len(manual_plan.points) == len(_manual_rows()) == 42
        assert all(p.label.startswith("[CM ") for p in manual_plan.points)
        assert len(manual_plan.groups()) == 2

    def test_a_source_dut_has_no_table(self):
        with pytest.raises(PlanError, match="no manufacturer"):
            generate_plan("fluke5700a", "hp3458a", dut_role="source",
                          point_set="manufacturer")

    def test_narrowing_options_narrow_the_rows_too(self):
        plan = generate_plan("fluke5700a", "hp3458a", dut_role="meter",
                             quantities=["DCV"], include_negative=False,
                             include_zero=False)
        assert all(p.value > 0 for p in plan.points)


# ------------------------------------- the 5700A's ac-volts envelope

#: 5700A/5720A Series II Extended Specifications, AC Voltage tables and
#: Figure A ("Volt-Hertz Capability"); the 1100 V range's maximum load
#: current from "Maximum Current Limits".  Restated, not read from the spec
#: file.
VOLT_HERTZ = 2.2e7
F_MIN, F_MAX = 10.0, 1.0e6                 # the specified bands
HV_BAND = (15.0, 1000.0)                   # 1100 V range, no 5725A
HV_LOW_F_MAX_V = 250.0                     # "Maximum output 250 V from 15-50 Hz"
HV_LOAD_MAX_A = 6e-3
#: The 3458A's ac input (1 Mohm +/- 2 % with < 140 pF) plus a test lead.
DUT_R, DUT_C = 1.0e6, 140e-12 + 150e-12


@pytest.mark.parametrize("point_set", ["full", "manufacturer", "ladder"])
def test_no_ac_point_exceeds_the_5700a(point_set):
    plan = generate_plan("fluke5700a", "hp3458a", dut_role="meter",
                         point_set=point_set)
    acv = [p for p in plan.points if p.quantity is Quantity.ACV]
    assert acv
    for point in acv:
        volts, hertz = abs(point.value), point.frequency_hz
        label = point.describe()
        assert volts * hertz <= VOLT_HERTZ * (1 + 1e-12), label
        assert F_MIN <= hertz <= F_MAX, label
        if volts > 220.0:                      # only the 1100 V range makes it
            assert HV_BAND[0] <= hertz <= HV_BAND[1], label
            if hertz < 50.0:
                assert volts <= HV_LOW_F_MAX_V, label
            load = volts * math.hypot(1.0 / DUT_R,
                                      2 * math.pi * hertz * DUT_C)
            assert load <= HV_LOAD_MAX_A, label


def test_the_ac_envelope_is_reached_not_just_respected():
    """The caps bind where they should: 700 V at 1 kHz, 44 V at 500 kHz."""
    plan = generate_plan("fluke5700a", "hp3458a", dut_role="meter")
    top = {}
    for point in plan.points:
        if point.quantity is Quantity.ACV:
            top[point.frequency_hz] = max(top.get(point.frequency_hz, 0.0),
                                          abs(point.value))
    assert top[1000.0] == 700.0
    assert top[500e3] == pytest.approx(44.0)
    assert top[1e6] == 10.0

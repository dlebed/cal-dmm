"""Tests for :mod:`caldmm.cal.plan`.

Everything here runs against the **real shipped specification files**, so the
plans asserted on are the plans the system will actually execute.  A plan built
from a fixture would prove that the generator works on a fixture.
"""

import subprocess
import sys
import textwrap
from dataclasses import replace

import pytest

from caldmm.cal import plan as plan_module
from caldmm.cal import verdict as verdict_module
from caldmm.cal.plan import (
    _magnitudes,
    _pick_mode,
    _range_by_id,
    CONNECTION_ORDER,
    DEFAULT_CONFIDENCE,
    DEFAULT_GUARD_FACTOR,
    DEFAULT_PERCENTAGES,
    DEFAULT_TUR_FLOOR,
    PREFERRED_FREQUENCIES,
    TEMPERATURE_POLICIES,
    PlanError,
    PlanPoint,
    SkippedPoint,
    TestPlan,
    driver_envelope,
    generate_plan,
    load_plan,
    needs_range_lock,
    plan_from_dict,
    plan_to_dict,
    point_problems,
    range_lock_anchor,
    representative_frequencies,
    save_plan,
    spec_ratio_note,
    validate_plan,
)
from caldmm.cal.quantities import Connection, Quantity, Terminals
from caldmm.cal.roles import Point, PointLike
from caldmm.cal.specs import (
    load_model,
    parse_model_text,
    select_band,
    select_mode,
    select_range,
)


@pytest.fixture(scope="module")
def source():
    return load_model("fluke5700a")


@pytest.fixture(scope="module")
def hp3458a():
    return load_model("hp3458a")


@pytest.fixture(scope="module")
def meter_spec():
    return load_model("hp3458a")


@pytest.fixture(params=["driver-envelope", "spec-only"])
def envelope(request, monkeypatch):
    """Run a check twice: with the driver's ``validate_output``, and without it.

    The root ``conftest.py`` stubs ``Gpib``, so under pytest the driver import
    succeeds and its envelope covers for the file-side checks -- which means a
    test that only ever runs with the driver present cannot tell whether
    ``plan.py`` checks anything at all.  **The environment this module is built
    for is the other one**: on a machine with no linux-gpib
    ``driver_envelope()`` is ``None`` and the file-side checks are all there is.
    """
    if request.param == "spec-only":
        monkeypatch.setattr(plan_module, "_import_validate_output",
                            lambda module_name: None)
        assert driver_envelope(load_model("fluke5700a")) is None
    else:
        assert driver_envelope(load_model("fluke5700a")) is not None
    return request.param


@pytest.fixture(scope="module")
def meter_plan(source, meter_spec):
    return generate_plan(source, meter_spec, dut_role="meter")


@pytest.fixture(scope="module")
def source_plan(source, hp3458a):
    return generate_plan(source, hp3458a, dut_role="source")


# ------------------------------------------------------------ reconciliation

def test_plan_point_is_a_roles_point():
    """The plan extends the shared point rather than competing with it."""
    point = PlanPoint(quantity=Quantity.DCV, value=10.0)
    assert isinstance(point, Point)
    assert isinstance(point, PointLike)
    assert point.unit == "V"


def test_replace_preserves_the_plan_fields():
    """``dataclasses.replace(point, value=...)`` is what the engine does."""
    point = PlanPoint(quantity=Quantity.DCV, value=10.0, samples=3,
                      note="hand-tuned")
    moved = replace(point, value=1.0)
    assert type(moved) is PlanPoint
    assert (moved.value, moved.samples, moved.note) == (1.0, 3, "hand-tuned")


def test_decision_rule_defaults_match_verdict():
    """The mirrored constants must not drift from their source of truth."""
    assert DEFAULT_CONFIDENCE == verdict_module.DEFAULT_CONFIDENCE
    assert DEFAULT_GUARD_FACTOR == verdict_module.DEFAULT_GUARD_FACTOR
    assert DEFAULT_TUR_FLOOR == verdict_module.DEFAULT_TUR_FLOOR
    assert TEMPERATURE_POLICIES == verdict_module.TEMPERATURE_POLICIES


# -------------------------------------------------------------- plan-level

def test_dut_role_is_validated():
    with pytest.raises(PlanError, match="dut_role"):
        TestPlan(dut_role="both", source_model_id="fluke5700a",
                 meter_model_id="hp3458a")


def test_temperature_policy_is_validated():
    with pytest.raises(PlanError, match="temperature_policy"):
        TestPlan(dut_role="meter", source_model_id="fluke5700a",
                 meter_model_id="hp3458a", temperature_policy="ignore")


def test_generate_rejects_a_source_in_the_meter_slot(source):
    with pytest.raises(PlanError, match="not a meter"):
        generate_plan(source, source, dut_role="meter")


def test_generate_rejects_an_undeclared_interval(source, meter_spec):
    with pytest.raises(PlanError, match="no '5y' interval"):
        generate_plan(source, meter_spec, dut_role="meter", interval="5y")


# ------------------------------------------------------- meter as the DUT

def test_meter_dut_points_sit_on_the_meter_ranges(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV])
    range_args = {r.range_arg for r in meter_spec.function(Quantity.DCV).ranges}
    assert plan.points
    for point in plan.points:
        assert point.range_arg in range_args
        assert point.range_id is None      # a source range id would be wrong here


def test_meter_dut_dc_count_is_what_the_percentages_imply(source, meter_spec):
    """5 dc ranges x (one zero + four percentages, each polarity)."""
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV])
    ranges = len(meter_spec.function(Quantity.DCV).ranges)
    per_range = 1 + 2 * len(DEFAULT_PERCENTAGES)
    assert len(plan.points) == ranges * per_range
    assert not plan.skipped


def test_no_negative_points_when_not_asked(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV], include_negative=False)
    assert all(p.value >= 0 for p in plan.points)


def test_no_zero_points_when_not_asked(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV], include_zero=False)
    assert all(p.value != 0.0 for p in plan.points)


def test_every_generated_point_passes_the_driver_envelope(source, meter_plan):
    """The offline 5700A envelope, straight from the driver."""
    validate_output = driver_envelope(source)
    assert validate_output is not None, "the driver check must really run"
    for point in meter_plan.points:
        problems = validate_output(point.value, point.unit, point.frequency_hz,
                                   range_id=point.range_id)
        assert not problems, f"{point.describe()}: {problems}"


def test_every_generated_point_passes_the_driver_envelope_source_dut(
        source, source_plan):
    validate_output = driver_envelope(source)
    for point in source_plan.points:
        problems = validate_output(point.value, point.unit, point.frequency_hz,
                                   range_id=point.range_id)
        assert not problems, f"{point.describe()}: {problems}"


# ------------------------------------------------------ source as the DUT

def test_source_dut_points_carry_cardinal_range_ids(source_plan, source):
    known = {r.range_id
             for function in source.functions.values()
             for r in function.ranges if r.range_id}
    assert source_plan.points
    for point in source_plan.points:
        assert point.range_id in known, point.describe()
        assert point.range_arg is None


def test_source_dut_covers_every_dc_volts_range(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    planned = {p.range_id for p in plan.points}
    assert planned == {"DC220MV", "DC2_2V", "DC11V", "DC22V", "DC220V",
                       "DC1100V"}


# ------------------------------------------------------------- resistance

def test_resistance_generates_only_catalogue_values(source, hp3458a):
    catalogue = {r.fs: r.range_id
                 for r in source.function(Quantity.RES).ranges}
    for role in ("meter", "source"):
        plan = generate_plan(source, hp3458a, dut_role=role,
                             quantities=[Quantity.RES])
        assert plan.points
        for point in plan.points:
            assert point.value in catalogue, point.describe()
            assert point.range_id == catalogue[point.value]


def test_resistance_never_invents_a_ladder_value(source, hp3458a):
    """90 % of 19 kohm is not a thing the calibrator can source."""
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.RES])
    values = {p.value for p in plan.points}
    for absent in (5000.0, 17100.0, 9500.0, 90000.0):
        assert absent not in values


def test_resistance_short_comes_first(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.RES])
    assert plan.points[0].value == 0.0
    assert plan.points[0].range_id == "OHM0"


def test_the_100_megohm_standard_falls_back_to_two_wire(source, hp3458a):
    """L92: the one value with no four-wire mode is not worth losing.

    "Four-wire sensing is available for all resistance values except 100 MOhm"
    ([OM 1996] line 4293, PDF p.102), and the default one-connection-per-
    quantity rule therefore dropped the 5700A's 100 Mohm standard from every
    plan -- with it the 34401A's own 100 Mohm verification point (Service
    Guide p.75) and the top of the 3458A's 100 Mohm range.
    """
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.RES])
    point = next((p for p in plan.points if p.value == 1.0e8), None)
    assert point is not None, "the 100 Mohm standard is still being dropped"
    assert point.connection is Connection.TWO_WIRE
    assert "two-wire" in point.note, point.note
    # ... as the last segment of the one voltage/ohms group: the cable set is
    # 4-wire throughout, and the 2-wire reading uses the same leads
    groups = plan.groups()
    assert len(groups) == 1
    quantity, connection, count = groups[-1].segments[-1]
    assert connection is Connection.TWO_WIRE and count == 1
    assert plan.points[-1] is point


def test_an_explicit_four_wire_request_still_drops_it(source, hp3458a):
    """The fallback is a default, not an override of what was asked for."""
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.RES],
                         connections=[Connection.FOUR_WIRE])
    assert 1.0e8 not in {p.value for p in plan.points}
    reasons = [r for entry in plan.skipped for r in entry.reasons
               if entry.point.value == 1.0e8]
    assert any("4w is not available on the OHM100M range" in r
               for r in reasons), reasons


def test_two_wire_reaches_the_100_megohm_standard(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.RES],
                         connections=[Connection.TWO_WIRE])
    assert 1.0e8 in {p.value for p in plan.points}


# ------------------------------------------------- the 1100 V dc minimum

def test_the_1100v_range_keeps_its_ten_percent_point(source, hp3458a, envelope):
    """110 V is *above* the 100 V minimum, so the point is legal and kept.

    The rule that bites on this range is the minimum output, not the
    percentage: anything below 100 V on DC1100V faults, and 10 % of 1100 V is
    110 V.  Asserted explicitly because "the 10 % point is invalid there" is
    the intuitive-but-wrong reading of the same rule.
    """
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    on_range = {p.value for p in plan.points if p.range_id == "DC1100V"}
    assert 110.0 in on_range


def test_the_1100v_range_drops_points_below_its_minimum_output(source, hp3458a,
                                                               envelope):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV], percentages=(5, 10, 100))
    on_range = {p.value for p in plan.points if p.range_id == "DC1100V"}
    assert 55.0 not in on_range
    dropped = [entry for entry in plan.skipped
               if entry.point.range_id == "DC1100V"
               and entry.point.value == 55.0]
    assert dropped, plan.skipped
    assert any("below the 100 V minimum output" in reason
               for reason in dropped[0].reasons), dropped[0].reasons


def test_the_1100v_zero_point_is_dropped_with_a_reason(source, hp3458a,
                                                       envelope):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    dropped = [entry for entry in plan.skipped
               if entry.point.range_id == "DC1100V" and entry.point.value == 0.0]
    assert dropped
    assert "minimum output" in str(dropped[0])


def test_the_plan_size_does_not_depend_on_the_driver_being_installed(source,
                                                                    hp3458a,
                                                                    envelope):
    """The file-side envelope has to stand on its own, point for point.

    Not just the same count: the same setpoints.  If a rule lives only in the
    driver, a laptop-generated plan quietly gains a point the calibrator will
    fault on, and nothing says so until the bench.
    """
    plan = generate_plan(source, hp3458a, dut_role="source")
    # The skips include the whole WBV function, offered by fluke5700a.yaml and
    # dropped with "hp3458a does not measure WBV".  The count moved from
    # (156, 35): eight AC2_2MV points came back when the 3458A's SYNC reading
    # floor stopped being read as ANA's (M69), ten more are the values that
    # stand in where the pair cannot reach a percentage of range (M70, M71) --
    # among them 1000 V dc on DC1100V and 250 V at 30 Hz on AC1100V -- and the
    # 100 Mohm standard moved out of `skipped` into a two-wire point (L92).
    # Then (175, 26) -> (211, 35) when hp3458a.yaml gained ACI (data sheet
    # p.33): 36 ac-current points, and 9 skips -- the 7 kHz tones the 3458A
    # specifies only as "typical", and 1.1-2.2 A above its 1 A top range.
    assert (len(plan.points), len(plan.skipped)) == (211, 35)


def test_a_point_beyond_the_meter_is_dropped_saying_so(source, hp3458a):
    """1100 V exists on the calibrator and not on the 3458A."""
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    dropped = [entry for entry in plan.skipped
               if entry.point.value == 1100.0]
    assert dropped
    assert any("hp3458a has no DCV range that reads 1100 V (top range 1000 V)"
               in reason for reason in dropped[0].reasons), dropped[0].reasons


# --------------------------------------------------------- ac minimum out

def test_the_ten_percent_ac_point_sits_on_the_minimum_and_is_kept(source,
                                                                  hp3458a):
    """10 % of range *is* the 5700A's ac minimum output, exactly.

    Kept on purpose: the minimum is a legal output and the most stressing
    low-end point the calibrator can make.
    """
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.ACV])
    on_range = {p.value for p in plan.points if p.range_id == "AC22V"}
    assert 2.2 in on_range


def test_ten_percent_of_a_22_millivolt_range_lands_on_the_minimum(source):
    """``0.022 * 10 / 100`` is ``0.0022000000000000005`` in binary floating point.

    One ulp above the stated 2.2 mV minimum output here, and on another range
    one ulp below -- where the point is refused for being under a minimum it is
    supposed to be sitting exactly on.  ``2.2 V`` on ``AC22V`` cannot witness
    this: ``22 * 10 / 100`` is exact whatever the code does.
    """
    range_spec = _range_by_id(source.function(Quantity.ACV), "AC22MV")
    assert range_spec.min_out == 0.0022
    assert _magnitudes(range_spec, (10.0,), False, Quantity.ACV) == [0.0022]


def test_generated_setpoints_are_clean_decimals(source, hp3458a):
    """No plan value may carry binary-float dust into the saved YAML.

    ``90 % of 22 mV`` is ``0.019799999999999998`` if the arithmetic is left
    alone; that is the number an operator would read in the file and the number
    a report would print.
    """
    plan = generate_plan(source, hp3458a, dut_role="source")
    assert 0.0198 in {p.value for p in plan.points}
    dusty = [p.describe() for p in plan.points
             if float(f"{p.value:.12g}") != p.value]
    assert not dusty, dusty


def test_the_ac_220ua_range_keeps_its_nine_microamp_floor(source, meter_spec,
                                                          envelope):
    """9 uA is not 10 % of 220 uA -- the documented exception."""
    problems = point_problems(
        PlanPoint(quantity=Quantity.ACI, value=9.0e-6, frequency_hz=1000.0,
                  range_id="AC220UA"),
        source, meter_spec, dut_role="source")
    assert not [p for p in problems if "minimum output" in p]
    below = point_problems(
        PlanPoint(quantity=Quantity.ACI, value=8.0e-6, frequency_hz=1000.0,
                  range_id="AC220UA"),
        source, meter_spec, dut_role="source")
    assert any("below the 9e-06 A minimum output" in p for p in below), below


# ---------------------------------------------------------- the 1281 case


# ------------------------------------------------------------- frequency

def test_every_band_of_a_range_contains_a_generated_frequency(meter_spec):
    function = meter_spec.function(Quantity.ACV)
    for range_spec in function.ranges:
        chosen = representative_frequencies(range_spec)
        assert chosen
        for band in range_spec.bands:
            assert any(band.covers(f) for f in chosen), \
                f"{band.freq_low_hz}-{band.freq_high_hz} Hz has no point"


def test_the_amplitude_sweep_runs_at_one_kilohertz(source, meter_spec):
    """One tone for the whole amplitude ladder, so two ranges compare.

    A re-homed sweep point (M71) is below full scale at some other tone by
    construction -- it is the frequency sweep, not the amplitude ladder -- and
    says so on the point, so it is excluded by its note rather than by its
    value.
    """
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.ACV])
    for range_spec in meter_spec.function(Quantity.ACV).ranges:
        on_range = [p for p in plan.points if p.range_arg == range_spec.range_arg]
        ladder = [p for p in on_range
                  if abs(p.value) < range_spec.fs and "stands in for" not in p.note]
        assert ladder
        assert all(p.frequency_hz == 1000.0 for p in ladder)


def test_the_frequency_sweep_falls_back_to_the_top_point(source, meter_spec):
    """Percentages without a 100 must not silently drop the frequency sweep."""
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.ACV], percentages=(10, 50),
                         point_set="ladder")
    on_ten_volts = [p for p in plan.points if p.range_arg == 10.0]
    tones = {p.frequency_hz for p in on_ten_volts}
    assert len(tones) > 1, tones
    swept = {p.value for p in on_ten_volts if p.frequency_hz != 1000.0}
    assert swept == {5.0}


def test_an_explicit_frequency_list_is_honoured(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.ACV],
                         frequencies=[55.0, 1000.0, 10000.0])
    assert {p.frequency_hz for p in plan.points} <= {55.0, 1000.0, 10000.0}


def test_ac_points_pin_the_meters_mode(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV])
    declared = set(hp3458a.function(Quantity.ACV).mode_ids)
    assert plan.points
    assert all(p.mode in declared for p in plan.points)


@pytest.mark.parametrize("frequency_hz", [30.0, 500e3])
def test_the_pinned_mode_is_the_tightest_not_the_spec_default(hp3458a,
                                                              frequency_hz):
    """The whole reason `_pick_mode` exists, pinned to literals.

    On the 3458A's 1 V ac range SYNC's bands and ANA's have different geometry,
    so `select_band` + `select_mode` -- the spec layer's own default -- lands on
    ANA at both of these frequencies purely because of where a band edge falls.
    ANA is 15x looser at 30 Hz and 7x looser at 500 kHz, so a plan that took the
    default would judge the meter against the wrong column and pass one that
    should fail.  Asserting only "the mode is one the model declares" cannot see
    that: every mode satisfies it.
    """
    function = hp3458a.function(Quantity.ACV)
    range_spec = select_range(function, 1.0)
    default = select_mode(function,
                          select_band(range_spec, frequency_hz), frequency_hz)
    assert default.id == "ana", "the hazard this test guards has moved"

    picked = _pick_mode(function, range_spec, 1.0, frequency_hz, "1y")
    assert picked == "sync"

    tightest = select_band(range_spec, frequency_hz, picked).limits["1y"]
    loosest = select_band(range_spec, frequency_hz, default.id).limits["1y"]
    assert tightest.limit(1.0, 1.0) < loosest.limit(1.0, 1.0) / 5


@pytest.mark.parametrize("frequency_hz", [30.0, 500e3])
def test_a_generated_plan_carries_the_tightest_mode(source, hp3458a,
                                                    frequency_hz):
    """The same fact, end to end, on the points the run will execute."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV])
    on_point = [p for p in plan.points
                if p.range_arg == 1.0 and p.frequency_hz == frequency_hz]
    assert on_point, f"no 1 V point at {frequency_hz:g} Hz to check"
    assert all(p.mode == "sync" for p in on_point), \
        [(p.describe(), p.mode) for p in on_point]


def test_the_frequency_ladder_is_round_numbers():
    assert 1000.0 in PREFERRED_FREQUENCIES
    assert all(f > 0 for f in PREFERRED_FREQUENCIES)
    assert list(PREFERRED_FREQUENCIES) == sorted(PREFERRED_FREQUENCIES)


# ---------------------------------------------------------------- grouping

def test_groups_share_one_terminal_set(meter_plan):
    total = 0
    for group in meter_plan.groups():
        assert group.points
        total += len(group)
        for point in group:
            assert Terminals.of(point.quantity) is group.terminals
        assert group.points[0].quantity is group.quantity
        assert sum(n for _q, _c, n in group.segments) == len(group)
        assert meter_plan.points[group.start] is group.points[0]
    assert total == len(meter_plan.points)


def test_a_full_3458a_plan_rewires_exactly_once(source, hp3458a):
    """Voltage, ac and both resistance wirings share one cable set; only the
    current functions move a lead.  That is the whole operator workload."""
    plan = generate_plan(source, hp3458a, dut_role="meter")
    groups = plan.groups()
    assert [g.terminals for g in groups] == [Terminals.VOLTAGE,
                                             Terminals.CURRENT]
    assert groups[0].quantities == (Quantity.DCV, Quantity.ACV, Quantity.RES)
    assert groups[1].quantities == (Quantity.DCI, Quantity.ACI)


def test_a_generated_plan_rewires_once_per_group(source, meter_spec):
    """Every (quantity, connection) appears in exactly one contiguous run."""
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         connections=[Connection.FOUR_WIRE,
                                      Connection.TWO_WIRE])
    keys = [(q, c) for g in plan.groups() for q, c, _n in g.segments]
    assert len(keys) == len(set(keys)), keys


def test_both_wirings_produce_two_contiguous_resistance_groups(source,
                                                              meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.RES],
                         connections=[Connection.FOUR_WIRE,
                                      Connection.TWO_WIRE])
    groups = plan.groups()
    assert len(groups) == 1
    assert [c for _q, c, _n in groups[0].segments] == [Connection.FOUR_WIRE,
                                                       Connection.TWO_WIRE]


def test_groups_follow_the_plans_own_order():
    """A hand-edited plan that alternates gets the groups it asked for."""
    def point(quantity, connection, value):
        return PlanPoint(quantity=quantity, value=value, connection=connection)

    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a",
                    points=(point(Quantity.DCV, Connection.TWO_WIRE, 1.0),
                            point(Quantity.DCI, Connection.TWO_WIRE, 1e-3),
                            point(Quantity.RES, Connection.FOUR_WIRE, 10.0)))
    assert [len(g) for g in plan.groups()] == [1, 1, 1]
    # and alternating wirings on one terminal set are one group, three segments
    plan = replace(plan, points=(
        point(Quantity.RES, Connection.FOUR_WIRE, 1.0),
        point(Quantity.RES, Connection.TWO_WIRE, 1.0),
        point(Quantity.RES, Connection.FOUR_WIRE, 10.0)))
    (group,) = plan.groups()
    assert [n for _q, _c, n in group.segments] == [1, 1, 1]


def test_zero_comes_first_within_its_range(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV])
    seen: set[float] = set()
    for point in plan.points:
        if point.range_arg not in seen:
            seen.add(point.range_arg)
            assert point.value == 0.0, point.describe()


def test_points_ascend_by_range(source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV])
    order = [p.range_arg for p in plan.points]
    first_seen = list(dict.fromkeys(order))
    assert first_seen == sorted(first_seen)


def test_connection_order_puts_four_wire_first():
    assert CONNECTION_ORDER[0] is Connection.FOUR_WIRE


# ------------------------------------------------------------- validation

def test_a_generated_plan_validates(meter_plan, source_plan, source, meter_spec,
                                    hp3458a):
    assert validate_plan(meter_plan, source, meter_spec) == []
    assert validate_plan(source_plan, source, hp3458a) == []


def test_validate_catches_an_ohms_point_with_a_frequency(source, hp3458a):
    plan = TestPlan(dut_role="source", source_model_id="fluke5700a",
                    meter_model_id="hp3458a",
                    points=(PlanPoint(quantity=Quantity.RES, value=10000.0,
                                      frequency_hz=1000.0,
                                      connection=Connection.FOUR_WIRE,
                                      range_id="OHM10K"),))
    problems = validate_plan(plan, source, hp3458a)
    assert any("dc quantity but the point states 1000 Hz" in p
               for p in problems), problems


def test_validate_catches_a_non_catalogue_resistance(source, hp3458a):
    plan = TestPlan(dut_role="source", source_model_id="fluke5700a",
                    meter_model_id="hp3458a",
                    points=(PlanPoint(quantity=Quantity.RES, value=5000.0,
                                      connection=Connection.FOUR_WIRE),))
    problems = validate_plan(plan, source, hp3458a)
    assert any("discrete catalogue, not a continuum" in p
               for p in problems), problems


def test_validate_catches_an_ac_point_with_no_mode(source, hp3458a):
    """Deleting ``mode:`` from a 3458A ac point is not a harmless edit.

    SYNC's bands and ANA's do not line up at 30 Hz, so the spec layer refuses to
    pick a default rather than judging the point against ANA's much looser row.
    """
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a",
                    points=(PlanPoint(quantity=Quantity.ACV, value=1.0,
                                      frequency_hz=30.0, range_arg=1.0),))
    problems = validate_plan(plan, source, hp3458a)
    assert any("pass mode=" in p for p in problems), problems


def test_validate_catches_skipped_contradicting_points(source, hp3458a):
    """A plan cannot both run a point and report it as dropped."""
    point = PlanPoint(quantity=Quantity.DCV, value=10.0, range_arg=10.0)
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a", points=(point,),
                    skipped=(SkippedPoint(replace(point, label="stale"),
                                          ("hand-edited back in",)),))
    problems = validate_plan(plan, source, hp3458a)
    assert any("also in points" in p for p in problems), problems


def test_validate_catches_an_empty_plan(source, hp3458a):
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a")
    assert "the plan has no points" in validate_plan(plan, source, hp3458a)


def test_validate_catches_a_nonsense_confidence(source, hp3458a):
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a", confidence=95.0,
                    points=(PlanPoint(quantity=Quantity.DCV, value=10.0),))
    problems = validate_plan(plan, source, hp3458a)
    assert any("confidence must be a fraction" in p for p in problems)


# ------------------------------------------------------------- YAML round trip

def test_yaml_round_trip_is_lossless(tmp_path, source_plan):
    path = tmp_path / "plan.yaml"
    save_plan(source_plan, path)
    assert load_plan(path) == source_plan


def test_skipped_survives_the_round_trip(tmp_path, source_plan):
    """It is the record of what the operator asked for and did not get."""
    assert source_plan.skipped
    path = tmp_path / "plan.yaml"
    save_plan(source_plan, path)
    reloaded = load_plan(path)
    assert reloaded.skipped == source_plan.skipped
    assert reloaded.skipped[0].reasons


def test_a_saved_plan_is_hand_editable(tmp_path, source, meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.DCV])
    path = tmp_path / "plan.yaml"
    save_plan(plan, path)
    text = path.read_text(encoding="utf-8")
    assert "quantity: DCV" in text and "connection: 2w" in text
    edited = text.replace("samples: 10", "samples: 25")
    path.write_text(edited, encoding="utf-8")
    assert load_plan(path).samples == 25


def test_per_point_overrides_round_trip(tmp_path):
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a", samples=4,
                    points=(PlanPoint(quantity=Quantity.DCV, value=10.0,
                                      samples=64, discards=3, settle_s=12.0,
                                      apply={"nplc": 100}, note="the point"),))
    path = tmp_path / "plan.yaml"
    save_plan(plan, path)
    reloaded = load_plan(path)
    assert reloaded == plan
    assert reloaded.samples_for(reloaded.points[0]) == 64
    assert reloaded.discards_for(reloaded.points[0]) == 3


def test_plan_defaults_apply_where_a_point_says_nothing():
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a", samples=7, discards=2,
                    points=(PlanPoint(quantity=Quantity.DCV, value=10.0),))
    assert plan.samples_for(plan.points[0]) == 7
    assert plan.discards_for(plan.points[0]) == 2


def test_strict_loading_rejects_an_unknown_plan_key():
    data = plan_to_dict(TestPlan(dut_role="meter",
                                 source_model_id="fluke5700a",
                                 meter_model_id="hp3458a"))
    data["nplc"] = 10
    with pytest.raises(PlanError) as excinfo:
        plan_from_dict(data, "plan.yaml")
    assert "plan.yaml: <root>: unknown key 'nplc'" in str(excinfo.value)


def test_strict_loading_names_the_path_of_a_bad_point_key():
    data = plan_to_dict(TestPlan(
        dut_role="meter", source_model_id="fluke5700a",
        meter_model_id="hp3458a",
        points=(PlanPoint(quantity=Quantity.DCV, value=1.0),
                PlanPoint(quantity=Quantity.DCV, value=2.0))))
    data["points"][1]["freq_hz"] = 1000.0
    with pytest.raises(PlanError) as excinfo:
        plan_from_dict(data, "plan.yaml")
    assert "points[1]: unknown key 'freq_hz'" in str(excinfo.value)


def test_strict_loading_rejects_an_unknown_apply_knob():
    data = plan_to_dict(TestPlan(
        dut_role="meter", source_model_id="fluke5700a",
        meter_model_id="hp3458a",
        points=(PlanPoint(quantity=Quantity.DCV, value=1.0),)))
    data["points"][0]["apply"] = {"integration": 10}
    with pytest.raises(PlanError) as excinfo:
        plan_from_dict(data, "plan.yaml")
    assert "points[0].apply: unknown key 'integration'" in str(excinfo.value)


@pytest.mark.parametrize("key", ["source_model_id", "meter_model_id",
                                 "dut_role"])
def test_strict_loading_rejects_an_explicit_null_at_the_root(key):
    data = plan_to_dict(TestPlan(
        dut_role="meter", source_model_id="fluke5700a",
        meter_model_id="hp3458a",
        points=(PlanPoint(quantity=Quantity.DCV, value=1.0),)))
    data[key] = None
    with pytest.raises(PlanError) as excinfo:
        plan_from_dict(data, "plan.yaml")
    assert f"<root>: {key!r} must not be null" in str(excinfo.value)


def test_strict_loading_rejects_a_null_point_value(source, hp3458a):
    """``value: null`` used to reach ``float(None)`` from inside validate_plan."""
    data = plan_to_dict(TestPlan(
        dut_role="meter", source_model_id="fluke5700a",
        meter_model_id="hp3458a",
        points=(PlanPoint(quantity=Quantity.DCV, value=1.0),)))
    data["points"][0]["value"] = None
    with pytest.raises(PlanError) as excinfo:
        plan_from_dict(data, "plan.yaml")
    assert "points[0]: 'value' must not be null" in str(excinfo.value)


def test_strict_loading_rejects_an_unknown_quantity():
    data = plan_to_dict(TestPlan(
        dut_role="meter", source_model_id="fluke5700a",
        meter_model_id="hp3458a",
        points=(PlanPoint(quantity=Quantity.DCV, value=1.0),)))
    data["points"][0]["quantity"] = "DCVOLTS"
    with pytest.raises(PlanError, match="unknown quantity"):
        plan_from_dict(data, "plan.yaml")


def test_unsigned_exponents_load_as_numbers(tmp_path):
    """PyYAML follows YAML 1.1, where ``1e-3`` is a *string*."""
    path = tmp_path / "plan.yaml"
    path.write_text(textwrap.dedent("""\
        schema_version: 1
        dut_role: meter
        source_model_id: fluke5700a
        meter_model_id: hp3458a
        points:
          - quantity: DCV
            value: 1e-3
            settle_s: 2e1
        """), encoding="utf-8")
    point = load_plan(path).points[0]
    assert point.value == pytest.approx(1e-3)
    assert point.settle_s == pytest.approx(20.0)


# ------------------------------------------------------------- housekeeping

def test_every_generated_point_carries_a_spec_ratio(meter_plan):
    assert all(p.note.startswith("spec ratio ") for p in meter_plan.points)


def test_the_spec_ratio_exposes_a_hopeless_dc_current_point(source, hp3458a):
    """The bottom of the 3458A's dc-current range against a 5700A.

    The meter is specified to ~4e-11 A there and the calibrator's own floor at
    that output is 1e-8 A, so the comparison is arithmetically incapable of a
    verdict and the run will return INDETERMINATE however well both instruments
    behave.  Written onto the point so nobody reads that as breakage.
    """
    note = spec_ratio_note(
        PlanPoint(quantity=Quantity.DCI, value=1.0e-8, range_arg=1.0e-7),
        source, hp3458a, dut_role="meter")
    ratio = float(note.split()[2])
    assert ratio < 0.01, note
    assert "hp3458a" in note and "fluke5700a" in note

    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.DCI])
    hopeless = [p for p in plan.points if float(p.note.split()[2]) < 0.01]
    assert len(hopeless) == 18, len(hopeless)


def test_point_counts_size_the_run(meter_plan):
    counts = meter_plan.point_counts()
    assert sum(counts.values()) == len(meter_plan.points)
    assert counts["DCV"] > 0


def test_skipped_point_renders_readably():
    entry = SkippedPoint(PlanPoint(quantity=Quantity.DCV, value=1100.0),
                         ("no range holds it",))
    assert "1100 V" in str(entry) and "no range holds it" in str(entry)


# ------------------------------------------------------------ import hygiene

IMPORT_HYGIENE_SCRIPT = textwrap.dedent("""\
    import sys

    BLOCKED = {"Gpib", "serial", "pyvisa", "smbus"}

    class Blocker:
        def find_module(self, fullname, path=None):
            return self.find_spec(fullname, path)
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError("blocked for this test: " + fullname)
            return None

    sys.meta_path.insert(0, Blocker())

    import caldmm.cal.plan as plan

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "importing cal.plan pulled in drivers: " + repr(leaked)
    assert "caldmm.cal.adapters" not in sys.modules, "cal.plan imported adapters"

    spec = plan.load_model("hp3458a")
    source = plan.load_model("fluke5700a")
    built = plan.generate_plan(source, spec, dut_role="meter",
                               quantities=["DCV"])
    assert built.points, "no points generated without a driver present"
    assert plan.validate_plan(built, source, spec) == []

    # The driver imports its interface libraries lazily, so its envelope is
    # available even with Gpib/serial/pyvisa blocked -- and asking for it is
    # the first and only thing that loads the driver.
    assert plan.driver_envelope(source) is not None
    assert "caldmm.drivers.fluke5700a" in sys.modules
    assert "caldmm.cal.adapters" not in sys.modules
    print("OK")
    """)


def test_plan_imports_and_generates_with_no_hardware_libraries():
    """A plan must be writable on a laptop with no linux-gpib.

    The driver envelope is a *cross-check*; the specification file carries the
    same rules, so generation and validation still work when the driver cannot
    be imported.  A subprocess is used so the root conftest's mock stubs are not
    in the way.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout


# ------------------------------------------------------- the range lock (H05)

def test_a_below_full_scale_point_says_its_range_is_locked(source, hp3458a):
    """110 V is 10 % of the 1100 V range and autoranges onto the 220 V one.

    The plan credits DC1100V, so the run has to put the output there with
    ``RANGELCK`` ([SM 2007] Table 3-19 note 3, line 5477: "Use Range Lock to
    obtain 1V on 11V and 10V on 22V range").  The point says so, because the
    operator reading the plan is the one who has to believe the range column.
    """
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    pinned = [p for p in plan.points
              if p.range_id == "DC1100V" and p.value == 110.0]
    assert pinned, "the 10 % point of the 1100 V range vanished"
    assert "range lock" in pinned[0].note, pinned[0].note


def test_a_naturally_ranged_point_says_nothing_about_a_lock(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.DCV])
    natural = [p for p in plan.points
               if p.range_id == "DC220V" and p.value == 220.0]
    assert natural
    assert "range lock" not in natural[0].note, natural[0].note


def test_every_pinned_point_can_be_put_on_the_range_it_names(source, hp3458a,
                                                             meter_spec):
    """H05: a point the calibrator cannot realise on its planned range is not
    planned.  The alternative is a report crediting a range the output was
    never on."""
    for meter in (hp3458a, meter_spec):
        plan = generate_plan(source, meter, dut_role="source")
        for point in plan.points:
            function = source.function(point.quantity)
            range_spec = _range_by_id(function, point.range_id)
            assert range_spec is not None, point.describe()
            if not needs_range_lock(function, range_spec, point.value):
                continue
            assert range_lock_anchor(
                function, range_spec,
                frequency_hz=point.frequency_hz) is not None, point.describe()


LOCKLESS_RANGE = textwrap.dedent("""\
    schema_version: 1
    model_id: toy5700a
    display_name: "Toy calibrator"
    role: source
    limits_authority: file
    overload_sentinel: 9.9e+37
    interval_basis: since_cal
    intervals: [1y]
    default_interval: 1y
    functions:
      - quantity: ACV
        func: ACV
        ranges:
          - fs: 220.0
            range_id: AC220V
            bands:
              - freq_hz: [10.0, 1000.0]
                limits: {1y: {ppm_reading: 100.0}}
          - fs: 1100.0
            range_id: AC1100V
            min_out: 110.0
            bands:
              - freq_hz: [10.0, 1000.0]
                max_out: 200.0
                limits: {1y: {ppm_reading: 100.0}}
    """)


def test_a_range_no_output_can_select_refuses_the_point(hp3458a):
    """The lock is realisable only if some output autoranges onto the range.

    Here the 1100 V range is capped at 200 V by its own band, below the 220 V
    range's full scale, so nothing selects it and 150 V can only ever be
    sourced on the range below.  A toy file, because the real 5700A has no
    such range and the rule must still be tested.
    """
    toy = parse_model_text(LOCKLESS_RANGE, "toy.yaml")
    function = toy.function(Quantity.ACV)
    range_spec = _range_by_id(function, "AC1100V")
    assert needs_range_lock(function, range_spec, 150.0)
    assert range_lock_anchor(function, range_spec, frequency_hz=30.0) is None

    problems = point_problems(
        PlanPoint(quantity=Quantity.ACV, value=150.0, frequency_hz=30.0,
                  range_id="AC1100V"),
        toy, hp3458a, dut_role="source")
    assert any("cannot be put on the AC1100V range" in p
               for p in problems), problems


def test_the_anchor_is_the_largest_output_the_range_can_make(source):
    """What the adapter programs before ``RANGELCK ON``.

    Full scale where the range is unconstrained, and the band's own ceiling
    where it is not: the 5700A's 1100 V ac range is limited to 250 V between
    15 and 50 Hz ([OM 1996] guide 18.4 note 1), and an anchor of 1100 V there
    would fault instead of selecting the range.
    """
    dcv = source.function(Quantity.DCV)
    assert range_lock_anchor(dcv, _range_by_id(dcv, "DC1100V")) == 1100.0
    acv = source.function(Quantity.ACV)
    top = _range_by_id(acv, "AC1100V")
    assert range_lock_anchor(acv, top, frequency_hz=1000.0) == 1100.0
    assert range_lock_anchor(acv, top, frequency_hz=30.0) == 250.0


def test_a_catalogue_resistance_never_needs_a_lock(source):
    """Every resistance is the full scale of its own discrete range."""
    function = source.function(Quantity.RES)
    for range_spec in function.ranges:
        assert not needs_range_lock(function, range_spec, range_spec.fs)


# ------------------------------------------ resistance load current (H06)

@pytest.mark.parametrize("value, meter_fs, ppm", [
    (19.0, 100.0, 16.0 * (8.0 - 1.0) / (8.0 * 1.0)),        # 14 ppm
    (100.0, 100.0, 3.5 * (8.0 - 1.0) / (8.0 * 1.0)),        # 3.0625 ppm
    (1900.0, 10000.0, 0.4 * (1.0 - 0.1) / (1.0 * 0.1)),     # 3.6 ppm
])
def test_the_3458a_pairing_carries_the_load_current_derating(source, hp3458a,
                                                             value, meter_fs,
                                                             ppm):
    """OM 1996 lines 2064-2070: ``Error = K(IL - I)/(IL x I)``, added to the
    calibrator's own uncertainty when the meter draws less than IL."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.RES])
    point = next(p for p in plan.points if p.value == value)
    assert point.range_arg == meter_fs
    assert point.load_adder == pytest.approx(ppm * 1e-6 * value)
    assert f"{ppm:.3g} ppm" in point.note, point.note


def test_a_reading_inside_the_window_carries_no_adder(source, hp3458a):
    """The 3458A's 10 ohm range sources 10 mA, inside the 8-11 mA window, so
    the 5700A's figure applies as written and nothing is added."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.RES])
    for value in (1.0, 1.9, 10.0, 1000.0, 10000.0):
        point = next(p for p in plan.points if p.value == value)
        assert point.load_adder is None, point.note
        assert "full-spec load" not in point.note, point.note


WIDE_OHMS_METER = textwrap.dedent("""\
    schema_version: 1
    model_id: toyohms
    display_name: "Toy ohmmeter"
    role: meter
    overload_sentinel: 9.9e+37
    interval_basis: since_cal
    intervals: [1y]
    default_interval: 1y
    functions:
      - quantity: RES
        func: OHMS
        connections: [4w, 2w]
        ranges:
          - fs: 200.0
            test_current_a: 1.0e-3
            limits: {1y: {ppm_reading: 100.0}}
          - fs: 1000.0
            test_current_a: 1.0e-2
            limits: {1y: {ppm_reading: 100.0}}
    """)


def test_a_meter_range_inside_the_window_is_preferred(source):
    """Given a choice, the pairing takes the range that keeps the 5700A inside
    its stated load window rather than the smallest range that fits.

    No shipped meter offers such a choice at any catalogue value, so the rule
    is exercised on a toy file: 190 ohm fits the 200 ohm range at 1 mA and the
    1 kohm range at 10 mA, and only the second is inside 8-11 mA.
    """
    toy = parse_model_text(WIDE_OHMS_METER, "toy.yaml")
    plan = generate_plan(source, toy, dut_role="meter",
                         quantities=[Quantity.RES])
    point = next(p for p in plan.points if p.value == 190.0)
    assert point.range_arg == 1000.0          # the 10 mA range, not the 200 ohm
    assert point.load_adder is None, point.note


# ------------------------------------------ the shared band edge (M67/H08)

def test_the_reference_tone_is_judged_on_the_tighter_row_for_a_dut(source,
                                                                   hp3458a):
    """1 kHz is exactly the 3458A's SYNC 40 Hz-1 kHz / 1 kHz-20 kHz boundary.

    A DUT has to meet both rows, and Fluke's own practice is to assess a
    border point "to the smaller of the two specifications"
    (``paper-test-point-selection-gust.txt`` lines 162-165).  70+20 ppm on the
    1 V range is 90 uV; the looser row is 140+20 = 160 uV.
    """
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV])
    point = next(p for p in plan.points
                 if p.range_arg == 1.0 and p.frequency_hz == 1000.0
                 and p.value == 1.0)
    assert point.mode == "sync"
    assert "hp3458a 9e-05" in point.note, point.note


def test_the_same_edge_is_the_looser_row_for_the_reference(source, hp3458a):
    """The other side of H08: a reference contributes the row it is *least*
    sure of, so the comparison is not made better than it is."""
    note = spec_ratio_note(
        PlanPoint(quantity=Quantity.ACV, value=1.0, frequency_hz=1000.0,
                  range_id="AC2_2V", mode="sync"),
        source, hp3458a, dut_role="source")
    assert "hp3458a 0.00016" in note, note


# ------------------------------- the per-mode reading floor (M69)

def test_the_sync_floor_keeps_the_5700a_2_2_millivolt_ac_range(source,
                                                               hp3458a):
    """The 3458A's 10 mV ac range is specified to 1/4 of full scale in ANA and
    to 10 % of it in SYNC (datasheet lines 999 and 1209), and every one of
    these points is pinned to SYNC.  The range-wide 0.30 removed the whole
    5700A 2.2 mV ac range -- which Fluke verifies at 2 mV across all
    frequencies -- for a floor that does not apply to the mode in use.
    """
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.ACV])
    on_range = {p.value for p in plan.points if p.range_id == "AC2_2MV"}
    assert {0.0022, 0.00198} <= on_range, sorted(on_range)
    assert all(p.mode == "sync" for p in plan.points
               if p.range_id == "AC2_2MV")


def test_a_point_below_the_pinned_modes_own_floor_is_still_dropped(source,
                                                                   hp3458a):
    """SYNC's floor is 10 % of the 12 V full scale, 1.2 mV: the 10 % and 50 %
    points of the 2.2 mV range are below it and stay out, with the mode's own
    percentage in the reason."""
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.ACV])
    on_range = {p.value for p in plan.points if p.range_id == "AC2_2MV"}
    assert 0.00022 not in on_range and 0.0011 not in on_range
    reasons = [r for entry in plan.skipped for r in entry.reasons
               if entry.point.range_id == "AC2_2MV"]
    assert any("12 % of range floor" in r for r in reasons), reasons


# ------------------------------- the reachable maximum (M70) and sweep (M71)


def test_the_1000_volt_ac_range_reaches_its_specified_700_volts(source,
                                                                hp3458a):
    """The 3458A's 1000 V ac range is specified to 700 V and the Cal Manual
    tests it there at 1 kHz (Table 4-3, line 1947); 90 % and 100 % of the
    range name are beyond the range's own ceiling."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV])
    at_one_khz = {p.value for p in plan.points
                  if p.range_arg == 1000.0 and p.frequency_hz == 1000.0}
    assert 700.0 in at_one_khz, sorted(at_one_khz)


def test_the_top_ac_range_keeps_a_frequency_sweep(source, hp3458a):
    """M71: the sweep is pinned to the 100 %-of-range magnitude, which on
    every top ac range is exactly the magnitude that cannot be made."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV])
    tones = {p.frequency_hz for p in plan.points if p.range_arg == 1000.0}
    assert len(tones) > 1, tones


def test_the_1100_volt_ac_range_is_exercised_below_50_hertz(source, hp3458a):
    """Where the 5700A's own low-frequency limitation lives: its 1100 V ac
    range is capped at 250 V between 15 and 50 Hz, so the 1100 V sweep
    magnitude cannot be made there and the whole band went untested."""
    plan = generate_plan(source, hp3458a, dut_role="source",
                         quantities=[Quantity.ACV])
    low = [p for p in plan.points
           if p.range_id == "AC1100V" and p.frequency_hz < 50.0]
    assert low, "the 15-50 Hz band of the 1100 V range has no point"
    assert 250.0 in {p.value for p in low}


# ------------------------- the manufacturers' own columns (L93/L94, M68)


def test_other_models_keep_their_own_default_interval(source, hp3458a):
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.DCV])
    assert plan.interval == hp3458a.default_interval


def test_the_manufacturer_preset_tests_the_break_points(source, hp3458a):
    """M68: the band edges are where the manufacturers' own verification
    tables put their points -- the 3458A Cal Manual tests the 10 V range at
    20 Hz, 1 kHz, 20 kHz, 100 kHz and 1 MHz (Table 4-3, line 1936) -- and a
    centre-of-band ladder never lands on one."""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV], frequencies="manufacturer")
    tones = {p.frequency_hz for p in plan.points if p.range_arg == 10.0}
    assert {20.0, 1000.0, 20000.0, 100000.0, 1000000.0} <= tones, sorted(tones)


def test_the_default_ladder_is_unchanged(source, hp3458a):
    """The preset is opt-in: the default stays the centre-tone ladder.

    (The full point set adds the Cal Manual's own break-point tones as ANA
    points; the generated ladder itself does not move.)"""
    plan = generate_plan(source, hp3458a, dut_role="meter",
                         quantities=[Quantity.ACV], point_set="ladder")
    tones = {p.frequency_hz for p in plan.points if p.range_arg == 10.0}
    assert 1000.0 in tones
    assert not ({20.0, 20000.0, 100000.0} & tones), sorted(tones)


def test_an_unknown_frequency_preset_is_refused(source, hp3458a):
    with pytest.raises(PlanError, match="manufacturer"):
        generate_plan(source, hp3458a, dut_role="meter",
                      quantities=[Quantity.ACV], frequencies="breakpoints")


def test_the_load_derating_survives_the_yaml_round_trip(tmp_path, source,
                                                        meter_spec):
    plan = generate_plan(source, meter_spec, dut_role="meter",
                         quantities=[Quantity.RES])
    path = tmp_path / "plan.yaml"
    save_plan(plan, path)
    assert load_plan(path) == plan
    assert any(p.load_adder for p in load_plan(path).points)


# --------------------------------------------------------------------------
# The range axis is keyed on dut_role, and that is why Range_Id looks empty
# --------------------------------------------------------------------------

class TestTheRangeAxisIsRoleKeyed:
    """``Range_Id`` blank in every row of a meter run is the design, not a bug.

    Reported after two bench runs whose ``Range_Id`` column was empty at 0/15
    while ``Src_Range_Id`` was full at 15/15, and worth pinning rather than
    re-diagnosing: ``range_id`` is a **source-side** identifier (the 5700A's
    §10 names, ``DC220MV``) and only ``fluke5700a.yaml`` defines any.  A meter
    has no such thing -- its ranges are numbers -- so a meter-DUT plan pins the
    axis with ``range_arg`` instead, exactly as this module's header says and
    as ``cal_analyze.point_key`` and ``cal_verify.plan_point_key`` both rely
    on.  The column is not redundant with ``Range_Arg`` either: they are
    populated in opposite roles, and dropping ``Range_Id`` would erase the axis
    of every calibrator-under-test run.
    """

    def test_a_source_dut_plan_carries_a_range_id_on_every_point(self, source,
                                                                 hp3458a):
        plan = generate_plan(source, hp3458a, dut_role="source",
                             quantities=[Quantity.DCV])
        assert plan.points
        assert all(point.range_id for point in plan.points)
        assert all(point.range_arg is None for point in plan.points)
        assert {"DC220MV", "DC1100V"} <= {p.range_id for p in plan.points}

    def test_a_meter_dut_plan_carries_a_range_arg_instead(self, source,
                                                          hp3458a):
        plan = generate_plan(source, hp3458a, dut_role="meter",
                             quantities=[Quantity.DCV])
        assert plan.points
        assert all(point.range_id is None for point in plan.points)
        assert all(point.range_arg is not None for point in plan.points)


    def test_the_result_row_reflects_the_split(self, source, hp3458a):
        """The CSV columns, end to end, for the two roles."""
        from caldmm.cal.result import ResultRow

        for role, filled, blank in (("source", "Range_Id", "Range_Arg"),
                                    ("meter", "Range_Arg", "Range_Id")):
            plan = generate_plan(source, hp3458a, dut_role=role,
                                 quantities=[Quantity.DCV])
            point = plan.points[-1]
            cells = ResultRow(quantity=point.quantity,
                              nominal=float(point.value),
                              range_id=point.range_id,
                              range_arg=point.range_arg).to_cells()
            assert cells[filled] != "", f"{role}: {filled} should be populated"
            assert cells[blank] == "", f"{role}: {blank} has no meaning here"

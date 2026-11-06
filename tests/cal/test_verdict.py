"""Tests for the conformity decision: TUR, guard bands and the verdict.

The cases are built from the real shipped specification data wherever the
number matters, so the arithmetic under test is the arithmetic the bench will
actually meet:

* an HP 34401A on dc volts verified against an HP 3458A -- 400 uV against
  6.2 uV, TUR ~64, the comfortable case;
* a Fluke 5700A at 10 kohm verified against that same 3458A -- 0.14 ohm against
  0.119 ohm, **TUR 1.18**, the case that must never be allowed to print PASS.

Numeric constants are asserted against literals (``1.959963984540054``,
``4.0``, ``0.0004``), never against the module's own constants: an assertion
that reads ``DEFAULT_TUR_FLOOR`` stays green when ``DEFAULT_TUR_FLOOR`` is
wrong.

Exact boundaries are probed with the doubles the module itself reports for
``AL`` and ``TL`` and with :func:`math.nextafter` either side of them, which is
the only way to pin *which side of a boundary is inclusive* without the test
silently agreeing with whatever the implementation happens to do.
"""

import math
import subprocess
import sys
import textwrap

import pytest

from caldmm.cal.quantities import Quantity
from caldmm.cal.specs import evaluate_limit, load_model
from caldmm.cal.uncertainty import (
    UncertaintyBudget,
    from_confidence,
    from_half_width,
    type_a,
)
from caldmm.cal.verdict import (
    DecisionRule,
    TemperatureStatus,
    Verdict,
    VerdictError,
    classify_temperature,
    evaluate,
    format_conformity,
    summarize,
)

# Two-sided normal quantiles, written out so the tests do not depend on the
# module's own idea of them.
Z95 = 1.959963984540054
Z99 = 2.5758293035489004


def budget_with_U(U, confidence=0.95):
    """A one-component budget whose expanded uncertainty is exactly *U*.

    ``from_confidence`` divides by the same quantile the budget later
    multiplies by, so a lone component round-trips bit-exactly -- which is what
    makes the boundary probes below meaningful.
    """
    return UncertaintyBudget().add(from_confidence("reference", U, confidence))


# --------------------------------------------------------------- fixtures

@pytest.fixture(scope="module")
def dmm_point():
    """34401A dc 10 V, 1-year: TL = 400 uV, window 18-28 C."""
    limit = evaluate_limit(load_model("hp3458a"), Quantity.DCV, 10.0,
                           interval="1y")
    assert limit is not None
    return limit


@pytest.fixture(scope="module")
def dmm_24h_point():
    """34401A dc 10 V, 24-hour: TL = 190 uV, window 22-24 C."""
    limit = evaluate_limit(load_model("hp3458a"), Quantity.DCV, 10.0,
                           interval="24h")
    assert limit is not None
    return limit


@pytest.fixture(scope="module")
def reference_3458a_dcv():
    """A 3458A 24-hour dc 10 V spec as a rectangular reference bound."""
    limit = evaluate_limit(load_model("hp3458a"), Quantity.DCV, 10.0,
                           interval="24h")
    assert limit is not None
    return UncertaintyBudget().add(
        from_half_width("3458A 24h", limit.limit, "rectangular"))


class TestRealDataFixtures:
    """The shipped spec data really does give the numbers used below."""


    def test_3458a_dcv_10v_24h_is_8_uv(self, reference_3458a_dcv):
        """5.5 uV is the data sheet's printed row; 8.0 uV is what applies here.

        p.9 EXAMPLE 1 computes 0.5 + 0.05 ppm of 10 V = 5.5 uV, but every dc
        row on p.12 is written for MATH NULL on a range held over four
        minutes.  This code base does neither, so the data sheet's own adder
        for that regime -- 0.25 ppm of range on the 10 V range
        (``3458a-datasheet.txt:454-456``) -- applies to the reference exactly
        as it does to a DUT: 5.5 + 2.5 = 8.0 uV.
        """
        # 0.80 ppm of 10 V, rectangular: u = 8.0/sqrt(3), U95 = 1.96*u.
        assert reference_3458a_dcv.combined() == pytest.approx(
            8.0e-6 / math.sqrt(3.0), rel=1e-12)
        assert reference_3458a_dcv.expanded(0.95) == pytest.approx(
            Z95 * 8.0e-6 / math.sqrt(3.0), rel=1e-9)


# ------------------------------------------------------------- the TUR floor

class TestTurFloor:
    """The single most important behaviour in the module.

    A poor reference must never be able to certify conformity, and must always
    be able to reveal non-conformity.
    """

    TL = 100e-6
    U = 100e-6 / 0.9          # TUR = 0.9 exactly

    def test_tur_is_computed_and_stored(self):
        result = evaluate(10e-6, self.TL, budget_with_U(self.U))
        assert result.tur == pytest.approx(0.9, rel=1e-12)

    def test_point_well_inside_tolerance_with_tur_0_9_is_indeterminate(self):
        """Comfortably in spec, but the comparison cannot say so."""
        result = evaluate(10e-6, self.TL, budget_with_U(self.U))
        assert result.verdict is Verdict.INDETERMINATE
        assert result.verdict is not Verdict.PASS
        assert "tur-below-floor" in result.flags
        assert "TUR" in result.reason and "0.90" in result.reason

    def test_point_outside_tolerance_with_tur_0_9_still_fails(self):
        """The floor blocks PASS, not FAIL: a real failure stays a failure."""
        result = evaluate(300e-6, self.TL, budget_with_U(self.U))
        assert result.verdict is Verdict.FAIL
        assert result.margin == pytest.approx(100e-6 - 300e-6, rel=1e-12)
        # The poor ratio is still recorded and still printed.
        assert "tur-below-floor" in result.flags
        assert "note:" in result.reason

    def test_the_floor_is_four_to_one(self):
        """Just under 4.0 is blocked; just over is not.  Literal, not constant."""
        tl = 100e-6
        blocked = evaluate(1e-6, tl, budget_with_U(tl / 3.999))
        allowed = evaluate(1e-6, tl, budget_with_U(tl / 4.001))
        assert blocked.tur == pytest.approx(3.999, rel=1e-9)
        assert blocked.verdict is Verdict.INDETERMINATE
        assert allowed.tur == pytest.approx(4.001, rel=1e-9)
        assert allowed.verdict is Verdict.PASS

    def test_exactly_at_the_floor_is_allowed(self):
        """``TUR < floor`` blocks, so ``TUR == floor`` does not."""
        tl = 400e-6
        result = evaluate(1e-6, tl, budget_with_U(tl / 4.0))
        assert result.tur == pytest.approx(4.0, rel=1e-12)
        assert result.verdict is Verdict.PASS

    def test_a_stricter_floor_is_honoured(self):
        tl = 400e-6
        result = evaluate(1e-6, tl, budget_with_U(tl / 6.0), tur_floor=10.0)
        assert result.verdict is Verdict.INDETERMINATE
        assert "10:1" in result.reason
        assert "tur_floor=10" in result.rule

    def test_5700a_at_10k_against_a_3458a_cannot_pass(self):
        """The real comparison the brief names: 14 ppm against 10.5 ppm."""
        dut = evaluate_limit(load_model("fluke5700a"), Quantity.RES, 10000.0,
                             interval="1y")
        ref = evaluate_limit(load_model("hp3458a"), Quantity.RES, 10000.0,
                             interval="1y")
        assert dut.limit == pytest.approx(0.14, rel=1e-12)      # 14 ppm
        assert ref.limit == pytest.approx(0.105, rel=1e-12)     # 10.5 ppm
        budget = UncertaintyBudget().add(
            from_half_width("3458A 1y", ref.limit, "rectangular"))
        result = evaluate(0.005, dut, budget, ambient_c=23.0)
        assert result.tur == pytest.approx(
            0.14 / (Z95 * 0.105 / math.sqrt(3.0)), rel=1e-9)
        assert result.tur < 1.2
        assert result.verdict is Verdict.INDETERMINATE


    def test_tur_floor_zero_is_recorded_in_the_rule_string(self):
        """Disabling the floor is allowed, but never silent."""
        tl = 100e-6
        result = evaluate(10e-6, tl, budget_with_U(tl / 0.9),
                          rule="simple", tur_floor=0.0)
        assert result.verdict is Verdict.PASS
        assert result.tur == pytest.approx(0.9, rel=1e-12)
        assert "tur_floor=0" in result.rule
        assert "no TUR requirement" in result.rule
        assert "tur-below-floor" not in result.flags

    def test_a_zero_tolerance_limit_gives_tur_zero_not_a_crash(self):
        result = evaluate(0.0, 0.0, budget_with_U(1e-6))
        assert result.tur == 0.0
        assert result.verdict is Verdict.INDETERMINATE


# ----------------------------------------------------- boundary inclusivity

class TestGuardbandedBoundaries:
    """Which side of ``TL - U`` and ``TL`` is inclusive, pinned.

    ``TL = 50 uV``, ``U = 12 uV`` (TUR 4.17, comfortably above the floor so the
    zones are what is being tested), ``g = 1`` so ``AL = 38 uV``.
    """

    TL = 50e-6
    U = 12e-6

    def decide(self, magnitude):
        return evaluate(magnitude, self.TL, budget_with_U(self.U))

    def test_the_geometry(self):
        result = self.decide(0.0)
        assert result.expanded_uncertainty == 12e-6
        assert result.guard_band == pytest.approx(12e-6, rel=1e-12)
        assert result.acceptance_limit == pytest.approx(38e-6, rel=1e-12)
        assert result.pass_limit == result.acceptance_limit
        assert result.fail_limit == result.tolerance_limit
        assert result.tur == pytest.approx(50.0 / 12.0, rel=1e-12)

    def test_just_below_the_acceptance_limit_passes(self):
        al = self.decide(0.0).acceptance_limit
        assert self.decide(math.nextafter(al, 0.0)).verdict is Verdict.PASS

    def test_exactly_at_the_acceptance_limit_passes(self):
        """**Pinned: the pass boundary is inclusive.**"""
        al = self.decide(0.0).acceptance_limit
        assert self.decide(al).verdict is Verdict.PASS

    def test_just_above_the_acceptance_limit_is_indeterminate(self):
        al = self.decide(0.0).acceptance_limit
        assert (self.decide(math.nextafter(al, 1.0)).verdict
                is Verdict.INDETERMINATE)

    def test_inside_the_guard_band_is_indeterminate(self):
        result = self.decide(44e-6)
        assert result.verdict is Verdict.INDETERMINATE
        assert "guard band" in result.reason
        assert result.margin == pytest.approx(6e-6, rel=1e-9)

    def test_exactly_at_the_tolerance_limit_is_indeterminate(self):
        """**Pinned: the fail boundary is exclusive.**

        An error landing exactly on ``TL`` has not been *shown* to exceed it,
        so it is not a demonstrated non-conformity.
        """
        result = self.decide(self.TL)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.verdict is not Verdict.FAIL
        assert result.margin == 0.0

    def test_just_above_the_tolerance_limit_fails(self):
        result = self.decide(math.nextafter(self.TL, 1.0))
        assert result.verdict is Verdict.FAIL

    def test_the_decision_is_on_the_magnitude_of_the_error(self):
        for magnitude in (30e-6, 44e-6, 60e-6):
            assert (self.decide(magnitude).verdict
                    is self.decide(-magnitude).verdict)
        negative = self.decide(-60e-6)
        assert negative.error == -60e-6
        assert negative.margin == pytest.approx(-10e-6, rel=1e-9)

    def test_a_guard_factor_other_than_one(self):
        result = evaluate(40e-6, self.TL, budget_with_U(self.U),
                          guard_factor=0.5)
        assert result.guard_band == pytest.approx(6e-6, rel=1e-12)
        assert result.acceptance_limit == pytest.approx(44e-6, rel=1e-12)
        assert result.verdict is Verdict.PASS      # 40 < 44, was INDETERMINATE
        assert "w=0.5*U" in result.rule

    def test_a_guard_factor_of_zero_collapses_to_simple(self):
        result = evaluate(self.TL, self.TL, budget_with_U(self.U),
                          guard_factor=0.0)
        assert result.guard_band == 0.0
        assert result.pass_limit == result.fail_limit == self.TL
        assert result.verdict is Verdict.PASS

    def test_u_larger_than_tl_makes_the_acceptance_limit_negative(self):
        """TUR < 1 is geometrically "nothing can pass", as well as arithmetically."""
        result = evaluate(0.0, self.TL, budget_with_U(80e-6), tur_floor=0.0)
        assert result.acceptance_limit == pytest.approx(-30e-6, rel=1e-9)
        assert result.verdict is Verdict.INDETERMINATE


class TestRulesAreDifferent:

    TL = 50e-6
    U = 12e-6

    def test_simple_accepts_what_guardbanded_calls_indeterminate(self):
        """Identical inputs, different rule, different answer."""
        args = (45e-6, self.TL, budget_with_U(self.U))
        simple = evaluate(*args, rule="simple")
        guarded = evaluate(*args, rule="guardbanded")
        assert simple.verdict is Verdict.PASS
        assert guarded.verdict is Verdict.INDETERMINATE
        assert simple.guard_band == 0.0
        assert guarded.guard_band == pytest.approx(12e-6, rel=1e-12)
        # Same measurement, same uncertainty: only the rule differs.
        assert simple.error == guarded.error
        assert simple.expanded_uncertainty == guarded.expanded_uncertainty
        assert simple.margin == guarded.margin

    def test_simple_still_fails_outside_the_tolerance_limit(self):
        result = evaluate(60e-6, self.TL, budget_with_U(self.U), rule="simple")
        assert result.verdict is Verdict.FAIL
        assert "not taken into account" not in result.reason

    def test_simple_is_inclusive_at_the_tolerance_limit(self):
        assert evaluate(self.TL, self.TL, budget_with_U(self.U),
                        rule="simple").verdict is Verdict.PASS
        assert evaluate(math.nextafter(self.TL, 1.0), self.TL,
                        budget_with_U(self.U),
                        rule="simple").verdict is Verdict.FAIL

    def test_simple_ignores_uncertainty_but_not_the_tur_floor(self):
        """Shared risk is about the guard band, not about the reference."""
        result = evaluate(10e-6, self.TL, budget_with_U(self.TL / 0.9),
                          rule="simple")
        assert result.verdict is Verdict.INDETERMINATE

    def test_guardbanded_reject_mirrors_guardbanded(self):
        rejecting = evaluate(55e-6, self.TL, budget_with_U(self.U),
                             rule="guardbanded_reject")
        assert rejecting.guard_band == pytest.approx(-12e-6, rel=1e-12)
        assert rejecting.acceptance_limit == pytest.approx(62e-6, rel=1e-12)
        assert rejecting.pass_limit == pytest.approx(50e-6, rel=1e-12)
        assert rejecting.fail_limit == pytest.approx(62e-6, rel=1e-12)
        assert rejecting.verdict is Verdict.INDETERMINATE
        # The rule string must not claim a positive w while w is negative.
        assert "w=-1*U" in rejecting.rule
        # The same 55 uV is a plain FAIL under the acceptance-guarded rule.
        assert evaluate(55e-6, self.TL, budget_with_U(self.U),
                        rule="guardbanded").verdict is Verdict.FAIL

    def test_guardbanded_reject_boundaries(self):
        def decide(magnitude):
            return evaluate(magnitude, self.TL, budget_with_U(self.U),
                            rule="guardbanded_reject")

        reject_limit = decide(0.0).acceptance_limit          # TL + U
        assert decide(self.TL).verdict is Verdict.PASS
        assert (decide(math.nextafter(self.TL, 1.0)).verdict
                is Verdict.INDETERMINATE)
        assert decide(reject_limit).verdict is Verdict.INDETERMINATE
        assert (decide(math.nextafter(reject_limit, 1.0)).verdict
                is Verdict.FAIL)

    def test_the_rule_enum_accepts_its_spellings(self):
        assert DecisionRule("guardbanded") is DecisionRule.GUARDBANDED
        assert DecisionRule("GUARDBANDED_REJECT") is \
            DecisionRule.GUARDBANDED_REJECT
        assert DecisionRule("guardbanded-reject") is \
            DecisionRule.GUARDBANDED_REJECT
        with pytest.raises(ValueError):
            DecisionRule("guardband")

    def test_guardbanded_is_the_default(self):
        default = evaluate(45e-6, self.TL, budget_with_U(self.U))
        explicit = evaluate(45e-6, self.TL, budget_with_U(self.U),
                            rule=DecisionRule.GUARDBANDED)
        assert default.verdict is explicit.verdict
        assert default.rule == explicit.rule
        assert default.rule.startswith("guardbanded,")


# ------------------------------------------------------------- confidence

class TestConfidence:

    def test_u_and_tur_move_together_from_95_to_99(self):
        tl = 400e-6
        budget = UncertaintyBudget().add(
            from_confidence("reference", 40e-6, 0.95))
        at95 = evaluate(10e-6, tl, budget, confidence=0.95)
        at99 = evaluate(10e-6, tl, budget, confidence=0.99)

        assert at95.expanded_uncertainty == pytest.approx(40e-6, rel=1e-12)
        assert at95.coverage_factor == pytest.approx(Z95, rel=1e-12)
        assert at95.tur == pytest.approx(10.0, rel=1e-9)

        # Same u_c, wider quantile: U grows by z99/z95 and TUR shrinks by it.
        assert at99.coverage_factor == pytest.approx(Z99, rel=1e-12)
        assert at99.expanded_uncertainty == pytest.approx(
            40e-6 * Z99 / Z95, rel=1e-9)
        assert at99.tur == pytest.approx(10.0 * Z95 / Z99, rel=1e-9)
        assert at99.tur == pytest.approx(7.609, rel=1e-3)

        # ... and the guard band with it, so the acceptance limit tightens.
        assert at99.acceptance_limit < at95.acceptance_limit
        assert at99.confidence == 0.99
        assert "p=99%" in at99.rule

    def test_a_higher_confidence_can_flip_a_pass_to_indeterminate(self):
        tl = 100e-6
        budget = UncertaintyBudget().add(
            from_confidence("reference", 20e-6, 0.95))
        assert evaluate(78e-6, tl, budget,
                        confidence=0.95).verdict is Verdict.PASS
        assert evaluate(78e-6, tl, budget,
                        confidence=0.99).verdict is Verdict.INDETERMINATE

    def test_a_finite_dof_widens_k_beyond_the_normal_quantile(self):
        """k is t at nu_eff, never a hard-coded 2."""
        budget = UncertaintyBudget().add(
            type_a("repeats", [10.000001, 10.000003, 10.000002, 10.000004,
                               10.000002]))
        result = evaluate(1e-6, 400e-6, budget)
        assert result.effective_dof == pytest.approx(4.0, rel=1e-12)
        assert result.coverage_factor == pytest.approx(2.7764451051977987,
                                                       rel=1e-9)
        assert result.expanded_uncertainty == pytest.approx(
            2.7764451051977987 * budget.combined(), rel=1e-12)


# ---------------------------------------------------------------- NO_SPEC

class TestNoSpec:

    def test_a_none_limit_is_no_spec_with_the_error_still_recorded(self):
        result = evaluate(-123e-6, None, budget_with_U(12e-6))
        assert result.verdict is Verdict.NO_SPEC
        assert result.error == -123e-6
        assert result.tur is None
        assert math.isnan(result.tolerance_limit)
        assert math.isnan(result.acceptance_limit)
        assert math.isnan(result.guard_band)
        assert math.isnan(result.margin)
        # The uncertainty side is real: the measurement happened.
        assert result.expanded_uncertainty == 12e-6
        assert result.coverage_factor == pytest.approx(Z95, rel=1e-9)
        assert "no statement of conformity" in result.reason


    def test_the_5700a_minus_one_sentinel_is_no_spec(self):
        """``UNCERT?`` answers -1 when the firmware has no figure."""
        result = evaluate(1e-6, -1.0, budget_with_U(12e-6))
        assert result.verdict is Verdict.NO_SPEC
        assert "instrument reports no specification" in result.reason

    def test_a_no_spec_result_still_carries_its_flags(self):
        result = evaluate(1e-6, None, budget_with_U(12e-6),
                          ambient_c=30.0, temperature_window_c=(18.0, 28.0))
        assert result.verdict is Verdict.NO_SPEC
        assert result.temperature_status is TemperatureStatus.OUT_OF_WINDOW
        assert "temperature-out-of-window" in result.flags


class TestEmptyBudget:
    """No budget at all must never produce a PASS.

    ``U = 0`` makes ``tur`` undefined, which would leave the TUR floor inert
    for exactly the input that least deserves a pass -- a half-wired caller, or
    an accumulator that never received its components.
    """

    def test_an_empty_budget_can_never_pass(self):
        """The headline: comfortably inside TL, and still not a PASS."""
        result = evaluate(399e-6, 400e-6, UncertaintyBudget(), ambient_c=23.0,
                          temperature_window_c=(18.0, 28.0))
        assert result.verdict is Verdict.INDETERMINATE
        assert result.verdict is not Verdict.PASS
        assert "no-uncertainty-budget" in result.flags
        assert "no uncertainty components were supplied" in result.reason

    def test_it_gives_u_zero_and_tur_none_without_dividing_by_zero(self):
        result = evaluate(10e-6, 400e-6, UncertaintyBudget())
        assert result.expanded_uncertainty == 0.0
        assert result.tur is None            # documented choice, not inf
        assert result.coverage_factor == pytest.approx(Z95, rel=1e-9)

    def test_it_blocks_even_with_every_other_gate_disabled(self):
        """Not a TUR requirement: no knob turns it off."""
        result = evaluate(1e-6, 400e-6, UncertaintyBudget(), tur_floor=0.0,
                          rule="simple", temperature_policy="flag",
                          ambient_c=23.0)
        assert result.verdict is Verdict.INDETERMINATE

    def test_it_does_not_hide_a_failure(self):
        result = evaluate(500e-6, 400e-6, UncertaintyBudget())
        assert result.verdict is Verdict.FAIL
        assert "not evidence of anything" in result.reason

    def test_the_no_spec_path_flags_it_too(self):
        result = evaluate(1e-6, None, UncertaintyBudget())
        assert result.verdict is Verdict.NO_SPEC
        assert "no-uncertainty-budget" in result.flags


class TestZeroUncertaintyBudget:
    """A *non-empty* budget that combines to zero blocks a PASS too.

    No real measurement has ``U = 0``: such a budget is what a defective
    adapter path produces (one reading, no resolution figure, ``UNCERT?``
    returning 0 at a zero setpoint) — and with ``tur = None`` the TUR floor
    cannot fire, so without its own gate this was an unconditional PASS with
    no uncertainty requirement at all. It keeps its distinct flag, because
    the components ARE in the budget table for a reader to check; it shares
    the empty budget's verdict, because neither is evidence.
    """

    @staticmethod
    def zero_budget():
        return UncertaintyBudget().add(
            from_half_width("a bound of exactly zero", 0.0))

    def test_it_blocks_a_pass(self):
        result = evaluate(10e-6, 400e-6, self.zero_budget(), ambient_c=23.0,
                          temperature_window_c=(18.0, 28.0))
        assert result.expanded_uncertainty == 0.0
        assert result.tur is None
        assert result.verdict is Verdict.INDETERMINATE
        assert "combine to U = 0" in result.reason
        assert "zero-uncertainty" in result.flags
        assert "no-uncertainty-budget" not in result.flags

    def test_it_blocks_even_with_every_other_gate_disabled(self):
        result = evaluate(0.0, 400e-6, self.zero_budget(), tur_floor=0.0,
                          rule="simple", temperature_policy="flag",
                          ambient_c=23.0)
        assert result.verdict is Verdict.INDETERMINATE

    def test_the_tur_floor_cannot_fire_on_an_undefined_tur(self):
        result = evaluate(0.0, 400e-6, self.zero_budget(), tur_floor=10.0,
                          ambient_c=23.0, temperature_window_c=(18.0, 28.0))
        assert "tur-below-floor" not in result.flags

    def test_it_does_not_hide_a_failure(self):
        # Blocks PASS, never FAIL: an observation of non-conformity stands.
        result = evaluate(500e-6, 400e-6, self.zero_budget())
        assert result.verdict is Verdict.FAIL


# ------------------------------------------------------------ temperature

class TestTemperatureClassification:

    def test_the_four_states(self):
        assert classify_temperature(23.0, (18.0, 28.0)) is \
            TemperatureStatus.IN_WINDOW
        assert classify_temperature(30.0, (18.0, 28.0)) is \
            TemperatureStatus.OUT_OF_WINDOW
        assert classify_temperature(None, (18.0, 28.0)) is \
            TemperatureStatus.AMBIENT_UNKNOWN
        assert classify_temperature(23.0, None) is \
            TemperatureStatus.NO_WINDOW
        assert classify_temperature(None, None) is \
            TemperatureStatus.UNKNOWN

    def test_the_window_edges_are_inclusive(self):
        assert classify_temperature(18.0, (18.0, 28.0)) is \
            TemperatureStatus.IN_WINDOW
        assert classify_temperature(28.0, (18.0, 28.0)) is \
            TemperatureStatus.IN_WINDOW
        assert classify_temperature(math.nextafter(28.0, 100.0),
                                    (18.0, 28.0)) is \
            TemperatureStatus.OUT_OF_WINDOW

    def test_the_trap_this_replaces(self):
        """``within_window`` answers True for two very different situations.

        The 3458A declares no absolute window at all, so ``within_window``
        says 45 C is fine; the 34401A declares 22-24 C for its 24-hour figures
        and is never asked when nobody recorded the temperature.  Both read as
        "in spec" through that API, which is why this module classifies rather
        than asks.
        """
        assert load_model("hp3458a").temperature.window_for("24h") is None
        assert load_model("hp3458a").temperature.within_window(45.0) is True
        assert classify_temperature(45.0, None) is TemperatureStatus.NO_WINDOW


class TestTemperatureGate:


    def test_a_condition_the_caller_supplies_blocks_both_statements(self):
        """Warm-up and overdue zeros reach here as ``conditions`` (M65)."""
        conditions = [("source-not-warmed-up",
                       "the source had been warm for 5 min of the 30 its "
                       "specifications require")]
        for error in (10e-6, 900e-6):
            result = evaluate(error, 400e-6, budget_with_U(10e-6),
                              ambient_c=23.0,
                              temperature_window_c=(18.0, 28.0),
                              conditions=conditions)
            assert result.verdict is Verdict.INDETERMINATE
            assert "source-not-warmed-up" in result.flags
            assert "conditions-not-met" in result.flags
            assert "warm for 5 min" in result.reason

    def test_no_conditions_is_the_old_behaviour(self):
        result = evaluate(900e-6, 400e-6, budget_with_U(10e-6),
                          ambient_c=23.0, temperature_window_c=(18.0, 28.0))
        assert result.verdict is Verdict.FAIL
        assert "conditions-not-met" not in result.flags

    def test_a_tur_note_survives_a_failure_unqualified(self):
        """Under a TUR floor the limit still applies; only the measurement is
        coarse.  The note therefore needs no retraction clause."""
        result = evaluate(900e-6, 400e-6, budget_with_U(200e-6),
                          ambient_c=23.0, temperature_window_c=(18.0, 28.0))
        assert result.verdict is Verdict.FAIL
        assert "TUR = 2.00 is below the required 4:1" in result.reason
        assert "confirmed" not in result.reason


    def test_a_nan_ambient_with_no_window_is_unknown(self):
        assert classify_temperature(float("nan"), None) is \
            TemperatureStatus.UNKNOWN
        assert classify_temperature(float("nan"), (18.0, 28.0)) is \
            TemperatureStatus.AMBIENT_UNKNOWN

    def test_an_infinite_ambient_is_a_caller_error(self):
        with pytest.raises(VerdictError):
            evaluate(0.0, 1.0, budget_with_U(0.1), ambient_c=float("inf"))
        with pytest.raises(VerdictError):
            evaluate(0.0, 1.0, budget_with_U(0.1), ambient_c="warm")


    def test_a_model_with_no_window_is_flagged_but_not_blocked(self):
        """The 3458A and 5700A specify against their own stored Tcal."""
        limit = evaluate_limit(load_model("hp3458a"), Quantity.DCV, 10.0,
                               interval="24h")
        assert limit.temperature_window_c is None
        result = evaluate(1e-7, limit, budget_with_U(1e-6), ambient_c=23.0)
        assert result.temperature_status is TemperatureStatus.NO_WINDOW
        assert result.verdict is Verdict.PASS
        assert "no-temperature-window" in result.flags

    def test_nothing_known_at_all_is_flagged_twice(self):
        result = evaluate(10e-6, 400e-6, budget_with_U(40e-6))
        assert result.temperature_status is TemperatureStatus.UNKNOWN
        assert result.verdict is Verdict.PASS
        assert "no-temperature-window" in result.flags
        assert "ambient-temperature-unknown" in result.flags

    def test_strict_blocks_the_unknown_cases_too(self):
        result = evaluate(10e-6, 400e-6, budget_with_U(40e-6),
                          temperature_policy="strict")
        assert result.verdict is Verdict.INDETERMINATE
        assert "neither the ambient temperature nor" in result.reason
        assert "temperature=strict" in result.rule

    def test_strict_blocks_a_model_that_declares_no_window(self):
        result = evaluate(10e-6, 400e-6, budget_with_U(40e-6),
                          ambient_c=23.0, temperature_policy="strict")
        assert result.temperature_status is TemperatureStatus.NO_WINDOW
        assert result.verdict is Verdict.INDETERMINATE
        assert "stored calibration temperature" in result.reason


    def test_an_explicit_window_overrides_the_limit_result(self, dmm_24h_point):
        result = evaluate(10e-6, dmm_24h_point, budget_with_U(10e-6),
                          ambient_c=25.0, temperature_window_c=(18.0, 28.0))
        assert result.temperature_status is TemperatureStatus.IN_WINDOW

    def test_an_explicit_none_window_overrides_the_limit_result(
            self, dmm_24h_point):
        """Explicit ``None`` means "no window", not "use the default"."""
        result = evaluate(10e-6, dmm_24h_point, budget_with_U(10e-6),
                          ambient_c=25.0, temperature_window_c=None)
        assert result.temperature_status is TemperatureStatus.NO_WINDOW


# ------------------------------------------------ the budget is compulsory

class TestBudgetIsCompulsory:

    def test_a_bare_float_uncertainty_is_rejected(self):
        with pytest.raises(TypeError) as excinfo:
            evaluate(10e-6, 400e-6, 12e-6)
        assert "UncertaintyBudget" in str(excinfo.value)

    def test_an_int_is_rejected(self):
        with pytest.raises(TypeError):
            evaluate(10e-6, 400e-6, 0)

    def test_a_list_of_components_is_rejected(self):
        with pytest.raises(TypeError):
            evaluate(10e-6, 400e-6, [from_confidence("r", 12e-6, 0.95)])

    def test_there_is_no_convenience_overload(self):
        import caldmm.cal.verdict as module
        names = [n for n in dir(module)
                 if "evaluate" in n and not n.startswith("_")]
        assert names == ["evaluate"]


class TestValidation:

    def test_an_unknown_rule(self):
        with pytest.raises(ValueError):
            evaluate(0.0, 1.0, budget_with_U(0.1), rule="lenient")

    def test_an_unknown_temperature_policy(self):
        with pytest.raises(VerdictError):
            evaluate(0.0, 1.0, budget_with_U(0.1),
                     temperature_policy="ignore")

    def test_a_negative_guard_factor(self):
        with pytest.raises(VerdictError) as excinfo:
            evaluate(0.0, 1.0, budget_with_U(0.1), guard_factor=-1.0)
        assert "guardbanded_reject" in str(excinfo.value)

    def test_a_negative_tur_floor(self):
        with pytest.raises(VerdictError):
            evaluate(0.0, 1.0, budget_with_U(0.1), tur_floor=-1.0)

    def test_a_non_finite_error(self):
        with pytest.raises(VerdictError):
            evaluate(float("nan"), 1.0, budget_with_U(0.1))

    def test_a_malformed_temperature_window(self):
        with pytest.raises(VerdictError):
            evaluate(0.0, 1.0, budget_with_U(0.1), ambient_c=23.0,
                     temperature_window_c=(28.0, 18.0))

    def test_a_confidence_in_percent(self):
        from caldmm.cal.uncertainty import UncertaintyError
        with pytest.raises(UncertaintyError):
            evaluate(0.0, 1.0, budget_with_U(0.1), confidence=95.0)


# ------------------------------------------------------------ presentation

class TestFormatConformity:


    def test_no_spec_prints_n_a_rather_than_zero(self):
        text = format_conformity(evaluate(1e-6, None, budget_with_U(12e-6)),
                                 "V")
        assert "n/a" in text
        assert "NO_SPEC" in text


    def test_the_unit_can_be_overridden(self, dmm_point,
                                        reference_3458a_dcv):
        result = evaluate(100e-6, dmm_point, reference_3458a_dcv,
                          ambient_c=23.4)
        assert result.unit == "V"
        assert "[ppm]" in format_conformity(result, "ppm")


class TestSummarize:

    def make(self, verdicts_and_margins):
        results = []
        for error, limit in verdicts_and_margins:
            results.append(evaluate(error, limit, budget_with_U(10e-6),
                                    ambient_c=23.0, unit="V",
                                    temperature_window_c=(18.0, 28.0)))
        return results

    def test_counts_and_worst_margin(self):
        results = self.make([
            (10e-6, 400e-6),      # PASS,  margin  390 uV
            (500e-6, 400e-6),     # FAIL,  margin -100 uV
            (395e-6, 400e-6),     # INDETERMINATE (guard band), margin 5 uV
            (1e-6, None),         # NO_SPEC, margin nan
        ])
        summary = summarize(results)
        assert summary.total == 4
        assert summary[Verdict.PASS] == 1
        assert summary[Verdict.FAIL] == 1
        assert summary[Verdict.INDETERMINATE] == 1
        assert summary[Verdict.NO_SPEC] == 1
        assert summary.worst_margin == pytest.approx(-100e-6, rel=1e-9)
        assert summary.worst is results[1]

    def test_no_spec_never_wins_the_worst_margin(self):
        summary = summarize(self.make([(1e-6, None), (10e-6, 400e-6)]))
        assert summary.worst_margin == pytest.approx(390e-6, rel=1e-9)

    def test_all_no_spec_gives_no_worst_margin(self):
        summary = summarize(self.make([(1e-6, None), (2e-6, None)]))
        assert summary.worst_margin is None
        assert summary.worst is None
        assert summary[Verdict.NO_SPEC] == 2

    def test_the_summary_line(self):
        summary = summarize(self.make([(10e-6, 400e-6), (500e-6, 400e-6)]))
        line = summary.line()
        assert "2 points" in line
        assert "1 pass" in line and "1 fail" in line
        assert "0 indeterminate" in line and "0 no spec" in line
        assert "worst margin" in line and "-0.0001 V" in line
        assert "-25.0 % of TL" in line
        assert str(summary) == line

    def test_the_worst_point_is_ranked_on_relative_margin_not_absolute(self):
        """A mixed-unit sweep -- volts and ohms -- must rank by how close each
        point came to *its own* limit, not by which quantity carries the
        largest numbers.

        The two points are chosen so that **the two rankings disagree**: the
        ohms point has the larger absolute margin *and* the smaller relative
        one.  A pair where both orderings agree would pass under either
        implementation and protect nothing.
        """
        ohms = evaluate(0.99, 1.0, budget_with_U(0.002), unit="OHM",
                        ambient_c=23.0, temperature_window_c=(18.0, 28.0))
        volts = evaluate(10e-6, 400e-6, budget_with_U(1e-6), unit="V",
                         ambient_c=23.0, temperature_window_c=(18.0, 28.0))
        # Absolute margins: the ohms point's is 26x larger ...
        assert ohms.margin == pytest.approx(0.01, rel=1e-9)
        assert volts.margin == pytest.approx(390e-6, rel=1e-9)
        assert ohms.margin > volts.margin
        # ... but it has spent 99 % of its tolerance while the volts point has
        # spent 2.5 % of its, so the relative ordering is the other way round.
        assert ohms.relative_margin == pytest.approx(0.01, rel=1e-9)
        assert volts.relative_margin == pytest.approx(0.975, rel=1e-9)
        assert ohms.relative_margin < volts.relative_margin

        for order in ([ohms, volts], [volts, ohms]):
            summary = summarize(order)
            # Ranking on the raw margin would nominate `volts` here.
            assert summary.worst is ohms
            assert summary.worst_margin == pytest.approx(0.01, rel=1e-9)
            assert summary.worst_relative_margin == pytest.approx(0.01,
                                                                  rel=1e-9)
            # Reported in the winning point's own unit, not the other's.
            assert "0.01 OHM" in summary.line()
            assert "+1.0 % of TL" in summary.line()

    def test_a_zero_limit_point_is_counted_but_not_ranked(self):
        degenerate = evaluate(1e-6, 0.0, budget_with_U(1e-6), unit="V")
        normal = evaluate(10e-6, 400e-6, budget_with_U(10e-6), unit="V")
        summary = summarize([degenerate, normal])
        assert summary.total == 2
        assert summary.worst is normal

    def test_flagged_results_are_counted(self):
        flagged = evaluate(10e-6, 400e-6, budget_with_U(10e-6))   # no ambient
        clean = evaluate(10e-6, 400e-6, budget_with_U(10e-6), ambient_c=23.0,
                         temperature_window_c=(18.0, 28.0))
        summary = summarize([flagged, clean])
        assert summary.flagged == 1
        assert "1 flagged" in summary.line()

    def test_an_empty_run(self):
        summary = summarize([])
        assert summary.total == 0
        assert summary.worst_margin is None
        assert all(summary[v] == 0 for v in Verdict)
        assert "0 points" in summary.line()

    def test_it_rejects_anything_that_is_not_a_result(self):
        with pytest.raises(VerdictError):
            summarize([Verdict.PASS])


class TestResultShape:

    def test_the_result_is_frozen(self, dmm_point, reference_3458a_dcv):
        result = evaluate(100e-6, dmm_point, reference_3458a_dcv,
                          ambient_c=23.4)
        with pytest.raises(Exception):
            result.verdict = Verdict.FAIL

    def test_conclusive_is_pass_or_fail_only(self):
        assert evaluate(1e-6, 400e-6, budget_with_U(10e-6),
                        ambient_c=23.0,
                        temperature_window_c=(18.0, 28.0)).conclusive
        assert evaluate(500e-6, 400e-6, budget_with_U(10e-6)).conclusive
        assert not evaluate(395e-6, 400e-6, budget_with_U(10e-6)).conclusive
        assert not evaluate(1e-6, None, budget_with_U(10e-6)).conclusive

    def test_acceptance_limit_is_always_tl_minus_the_guard_band(self):
        for rule in ("simple", "guardbanded", "guardbanded_reject"):
            result = evaluate(10e-6, 400e-6, budget_with_U(40e-6), rule=rule)
            assert result.acceptance_limit == pytest.approx(
                result.tolerance_limit - result.guard_band, rel=1e-12)

    def test_relative_margin_is_dimensionless(self):
        result = evaluate(100e-6, 400e-6, budget_with_U(40e-6))
        assert result.margin == pytest.approx(300e-6, rel=1e-12)
        assert result.relative_margin == pytest.approx(0.75, rel=1e-12)
        # A point exactly on its limit has no margin left, in any unit.
        assert evaluate(400e-6, 400e-6,
                        budget_with_U(40e-6)).relative_margin == 0.0
        assert evaluate(500e-6, 400e-6, budget_with_U(40e-6)).relative_margin \
            == pytest.approx(-0.25, rel=1e-12)

    def test_relative_margin_is_none_without_a_usable_limit(self):
        assert evaluate(1e-6, None, budget_with_U(1e-6)).relative_margin is None
        assert evaluate(0.0, 0.0, budget_with_U(1e-6)).relative_margin is None

    def test_the_verdict_enum_spellings(self):
        assert Verdict("pass") is Verdict.PASS
        assert Verdict("no_spec") is Verdict.NO_SPEC
        with pytest.raises(ValueError):
            Verdict("maybe")


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

    import caldmm.cal.specs
    import caldmm.cal.uncertainty
    import caldmm.cal.verdict

    spec = caldmm.cal.specs.load_model("hp3458a")
    limit = caldmm.cal.specs.evaluate_limit(
        spec, caldmm.cal.quantities.Quantity.DCV, 10.0, interval="1y")
    budget = caldmm.cal.uncertainty.UncertaintyBudget().add(
        caldmm.cal.uncertainty.from_confidence("ref", 40e-6, 0.95))
    result = caldmm.cal.verdict.evaluate(100e-6, limit, budget,
                                          ambient_c=23.0)
    assert result.verdict is caldmm.cal.verdict.Verdict.PASS, result.reason
    assert caldmm.cal.verdict.format_conformity(result)

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal pulled in drivers: " + repr(leaked)
    print("OK")
    """)



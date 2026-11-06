"""Tests for the GUM uncertainty engine.

The two worked examples below are the valuable part.  The first reproduces
*JCGM 100:2008 (GUM)* Annex H.1, Table H.1 -- the end-gauge calibration -- and
the second is a hand-derived bench case from this project's own domain, with
every intermediate written out so a reader can check the arithmetic on paper
rather than trusting the module that is under test.

Every distribution divisor is asserted against a literal (``math.sqrt(3)``
etc.), never against ``DISTRIBUTIONS``: an assertion that reads the module's
own constant stays green when that constant is wrong.
"""

import math
import subprocess
import sys
import textwrap

import pytest
from scipy import stats

from caldmm.cal.uncertainty import (
    BUDGET_FIELDS,
    DISTRIBUTIONS,
    UncertaintyBudget,
    UncertaintyComponent,
    UncertaintyError,
    budget_from_rows,
    budget_to_rows,
    format_budget,
    from_calibrator_uncert,
    from_confidence,
    from_expanded,
    from_half_width,
    loading_correction,
    loading_fraction,
    normal_coverage_factor,
    resolution_component,
    tempco_component,
    thermal_emf_component,
    type_a,
)

# Two-sided normal quantiles, written out so the tests do not depend on the
# module's own idea of them.
Z95 = 1.959963984540054
Z99 = 2.5758293035489004


def std(label, u, dof=math.inf, sensitivity=1.0):
    """A component whose value is already a standard uncertainty."""
    return UncertaintyComponent(label=label, value=u, dof=dof, type="B",
                                distribution="normal",
                                sensitivity=sensitivity)


# ------------------------------------------------------- worked example 1

class TestGumH1:
    """GUM Annex H.1 (Table H.1): calibration of a 50 mm end gauge.

    Everything is carried in nanometres, as in the GUM.  The eight input
    quantities, their standard uncertainties and their degrees of freedom are
    H.1.3 and H.1.6; the sensitivity coefficients are H.1.4.

      1. u(l_S)  = 25 nm      c = 1                        nu = 18
         calibration certificate of the standard end gauge
      2. u(d_bar) = 5.8 nm    c = 1                        nu = 24
         mean of the observed differences (Type A)
      3. u(random)  = 3.9 nm  c = 1                        nu = 5
      4. u(system.) = 6.7 nm  c = 1                        nu = 8
         random and systematic effects of the comparator (Type A / B)
      5. u(alpha_S) = 1.2e-6 /C   c = -l_S*(theta_bar+delta_theta) = 0  nu = inf
         zero because the estimate of (theta_bar + delta_theta) is 0;
         u = 2e-6/sqrt(3), a rectangular bound with no stated reliability,
         so Table H.1 gives it infinite degrees of freedom
      6. u(theta)   = 0.41 C      c = -l_S*delta_alpha = 0             nu = inf
         zero because the estimate of delta_alpha is 0;
         u(theta) = sqrt(0.2^2 + (0.5/sqrt(2))^2) = 0.406 C -- a Type B
         normal 0.2 C plus a +/-0.5 C cyclic (U-shaped) swing
      7. u(delta_alpha) = 1e-6/sqrt(3) = 0.58e-6 /C        nu = 50
         c = -l_S*theta_bar = -(50 mm)(-0.1 C) = +5e6 nm.C
         |c|u = 2.89 nm  (GUM Table H.1: 2.9 nm)
      8. u(delta_theta) = 0.05/sqrt(3) = 0.0289 C          nu = 2
         c = -l_S*alpha_bar = -(50e6 nm)(11.5e-6 /C) = -575 nm/C
         |c|u = 16.60 nm (GUM Table H.1: 16.6 nm)

    The two finite Type B degrees of freedom come from GUM equation (G.3),
    nu = 0.5*(du/u)^-2: delta_alpha is believed good to 10 % (nu = 50) and
    delta_theta to 50 % (nu = 2).  The other Type B bounds are taken as
    exactly known and carry nu = inf.

    GUM H.1.6 publishes u_c(l) = 32 nm and nu_eff(l) = 16.7, then truncates to
    16 for the t-table: t_95(16) = 2.12 -> U_95 = 68 nm, t_99(16) = 2.92 ->
    U_99 = 93 nm.
    """

    @pytest.fixture(scope="class")
    def budget(self):
        l_S_nm = 50.0e6          # 50 mm in nm
        alpha_bar = 11.5e-6      # /C
        theta_bar = -0.1         # C
        return UncertaintyBudget((
            from_expanded("l_S calibration", 50.0, 2.0, dof=18.0,
                          source="certificate"),
            UncertaintyComponent("d_bar", 5.8, 24.0, "A", "normal"),
            UncertaintyComponent("comparator random", 3.9, 5.0, "A", "normal"),
            UncertaintyComponent("comparator systematic", 6.7, 8.0, "B",
                                 "normal"),
            from_half_width("alpha_S", 2.0e-6, "rectangular", sensitivity=0.0),
            UncertaintyComponent(
                "theta", math.sqrt(0.2 ** 2 + (0.5 / math.sqrt(2)) ** 2),
                math.inf, "B", "normal", sensitivity=0.0),
            from_half_width("delta_alpha", 1.0e-6, "rectangular", dof=50.0,
                            sensitivity=-l_S_nm * theta_bar),
            from_half_width("delta_theta", 0.05, "rectangular", dof=2.0,
                            sensitivity=-l_S_nm * alpha_bar),
        ))

    def test_certificate_component_is_25_nm(self, budget):
        # U = 50 nm at k = 2 -> u = 25 nm (GUM H.1.3.1).
        assert budget.components[0].value == pytest.approx(25.0)

    def test_theta_is_normal_plus_u_shaped(self):
        # 0.2 C normal, plus a +/-0.5 C cyclic swing: 0.5/sqrt(2) = 0.354 C.
        cyclic = from_half_width("cyclic", 0.5, "u_shaped")
        assert cyclic.value == pytest.approx(0.5 / math.sqrt(2))
        assert math.sqrt(0.2 ** 2 + cyclic.value ** 2) == pytest.approx(
            0.41, abs=0.005)

    def test_table_h1_contributions(self, budget):
        got = {label: value for label, value, _ in budget.contributions()}
        assert got["l_S calibration"] == pytest.approx(25.0)
        assert got["d_bar"] == pytest.approx(5.8)
        assert got["comparator random"] == pytest.approx(3.9)
        assert got["comparator systematic"] == pytest.approx(6.7)
        assert got["alpha_S"] == pytest.approx(0.0)
        assert got["theta"] == pytest.approx(0.0)
        # -l_S*theta_bar = +5e6 nm.C times 0.577e-6 /C = +2.89 nm
        assert got["delta_alpha"] == pytest.approx(2.887, abs=0.005)
        # -575 nm/C times 0.02887 C = -16.60 nm
        assert got["delta_theta"] == pytest.approx(-16.599, abs=0.005)

    def test_combined_matches_gum_32_nm(self, budget):
        # sqrt(25^2 + 5.8^2 + 3.9^2 + 6.7^2 + 2.887^2 + 16.599^2)
        #   = sqrt(625 + 33.64 + 15.21 + 44.89 + 8.33 + 275.53)
        #   = sqrt(1002.6) = 31.66 nm, which the GUM prints as 32 nm.
        assert budget.combined() == pytest.approx(31.664, abs=0.005)
        assert round(budget.combined()) == 32

    def test_effective_dof_matches_gum_16_7(self, budget):
        # nu_eff = u_c^4 / sum (c_i u_i)^4 / nu_i
        #        = 1002.6^2 / (25^4/18 + 5.8^4/24 + 3.9^4/5 + 6.7^4/8
        #                      + 2.887^4/50 + 16.599^4/2)
        #        = 1005215 / (21701 + 47.2 + 46.3 + 251.9 + 1.39 + 37960)
        #        = 1005215 / 60008 = 16.75
        assert budget.effective_dof() == pytest.approx(16.75, abs=0.02)

    def test_coverage_factor_and_expanded_uncertainty(self, budget):
        # The GUM truncates nu_eff to 16 before entering the t-table and gets
        # k = 2.12, U_95 = 2.12 * 32 = 68 nm.  We evaluate t at the exact
        # nu_eff = 16.75, which scipy supports and printed tables do not.
        assert stats.t.ppf(0.975, 16) == pytest.approx(2.12, abs=0.005)
        assert stats.t.ppf(0.995, 16) == pytest.approx(2.92, abs=0.005)

        k95 = budget.coverage_factor(0.95)
        assert k95 == pytest.approx(2.112, abs=0.002)
        assert k95 > 2.0                       # k = 2 would be wrong here
        assert budget.expanded(0.95) == pytest.approx(66.9, abs=0.2)

        # GUM H.1.6 also quotes the 99 % result: 2.92 * 32 = 93 nm.
        assert budget.coverage_factor(0.99) == pytest.approx(2.904, abs=0.002)
        assert budget.expanded(0.99) == pytest.approx(91.9, abs=0.2)

    def test_dominant_terms_are_the_certificate_then_delta_theta(self, budget):
        labels = [label for label, _, _ in budget.contributions()]
        assert labels[:2] == ["l_S calibration", "delta_theta"]
        # ...and delta_theta, with only nu = 2, is what drags nu_eff to 16.75.
        without = UncertaintyBudget(
            tuple(c for c in budget.components if c.label != "delta_theta"))
        assert without.effective_dof() > 20


# ------------------------------------------------------- worked example 2

class TestBenchExampleHandDerived:
    """Hand-derived: a 34401A verified at 100 mV against a 5700A.

    Not from any publication -- every number below is computed in the comment
    that introduces it, in microvolts.

      1. Repeatability, 10 readings, sample sd s = 8.0 uV (ddof = 1):
             u = 8.0/sqrt(10) = 2.5298 uV,   nu = 9
      2. 5700A UNCERT? = 1.4 uV, reported at 95 % confidence (CAL_CONF CONF95):
             u = 1.4/1.959964 = 0.71430 uV,  nu = inf
      3. 34401A 100 mV range, 6.5 digits -> 0.1 uV resolution:
             a = 0.05 uV, rectangular, u = 0.05/sqrt(3) = 0.028868 uV
      4. Thermal EMF in the loop, operator bound +/-1.0 uV, rectangular:
             u = 1.0/sqrt(3) = 0.57735 uV

      u_c^2 = 2.5298^2 + 0.71430^2 + 0.028868^2 + 0.57735^2
            = 6.4000 + 0.51022 + 0.00083333 + 0.33333 = 7.24439 uV^2
      u_c   = 2.69154 uV

      nu_eff = u_c^4 / (u_1^4/9)            [only term 1 has finite nu]
             = 7.24439^2 / (6.4000^2/9)
             = 52.4812 / 4.55111 = 11.5315

      k_95 = t.ppf(0.975, 11.5315) = 2.18867  ->  U_95 = 2.18867 * 2.69154
                                                       = 5.8909 uV
    """

    @pytest.fixture(scope="class")
    def budget(self):
        # 10 readings with sample sd exactly 8.0 uV: +/-a repeated 5 times has
        # s = a*sqrt(10/9), so a = 8.0*sqrt(9/10) = 7.5895 uV.
        a = 8.0 * math.sqrt(0.9)
        samples = [100.0 + a, 100.0 - a] * 5
        return UncertaintyBudget().add(
            type_a("34401A repeatability", samples),
            from_calibrator_uncert("5700A UNCERT?", 1.4, 0.95),
            resolution_component("34401A resolution", 0.1),
            thermal_emf_component("thermal EMF", 1.0),
        )

    def test_sample_sd_is_what_the_comment_says(self, budget):
        # u = s/sqrt(n) = 8.0/sqrt(10) = 2.52982 uV, nu = n-1 = 9
        repeat = budget.components[0]
        assert repeat.value == pytest.approx(8.0 / math.sqrt(10), rel=1e-12)
        assert repeat.dof == 9.0
        assert repeat.type == "A"

    def test_calibrator_uncert_is_a_confidence_not_a_bound(self, budget):
        cal = budget.components[1]
        assert cal.value == pytest.approx(1.4 / Z95, rel=1e-12)
        # The two wrong ways, pinned so nobody "fixes" the right one into them.
        assert cal.value != pytest.approx(1.4 / math.sqrt(3))   # rectangular
        assert cal.value != pytest.approx(1.4)                  # as-standard

    def test_resolution_and_thermal_emf(self, budget):
        assert budget.components[2].value == pytest.approx(
            0.05 / math.sqrt(3), rel=1e-12)
        assert budget.components[3].value == pytest.approx(
            1.0 / math.sqrt(3), rel=1e-12)

    def test_combined_and_effective_dof(self, budget):
        assert budget.variance() == pytest.approx(7.24439, abs=1e-5)
        assert budget.combined() == pytest.approx(2.69154, abs=1e-5)
        assert budget.effective_dof() == pytest.approx(11.5315, abs=1e-4)

    def test_coverage_and_expanded(self, budget):
        k = budget.coverage_factor(0.95)
        assert k == pytest.approx(stats.t.ppf(0.975, 11.5315), abs=1e-4)
        assert k == pytest.approx(2.18867, abs=1e-5)
        assert k > 2.0                         # 10 readings do not buy k = 2
        assert budget.expanded(0.95) == pytest.approx(5.8909, abs=1e-4)

    def test_repeatability_dominates_so_average_more(self, budget):
        label, contribution, share = budget.contributions()[0]
        assert label == "34401A repeatability"
        # 6.4000 / 7.24439 = 88.3 % of the variance
        assert share == pytest.approx(88.344, abs=0.005)
        assert contribution == pytest.approx(8.0 / math.sqrt(10), rel=1e-12)


# ------------------------------------------------------ distribution divisors

class TestDistributions:
    def test_rectangular_divides_by_root_three(self):
        c = from_half_width("rect", 3.0, "rectangular")
        assert c.value == pytest.approx(3.0 / 1.7320508075688772, rel=1e-15)
        assert c.value == pytest.approx(3.0 / math.sqrt(3.0), rel=1e-15)

    def test_triangular_divides_by_root_six(self):
        c = from_half_width("tri", 3.0, "triangular")
        assert c.value == pytest.approx(3.0 / 2.449489742783178, rel=1e-15)
        assert c.value == pytest.approx(3.0 / math.sqrt(6.0), rel=1e-15)

    def test_u_shaped_divides_by_root_two(self):
        c = from_half_width("u", 3.0, "u_shaped")
        assert c.value == pytest.approx(3.0 / 1.4142135623730951, rel=1e-15)
        assert c.value == pytest.approx(3.0 / math.sqrt(2.0), rel=1e-15)

    def test_normal_does_not_divide(self):
        assert from_half_width("n", 3.0, "normal").value == 3.0

    def test_defaults_to_rectangular(self):
        assert from_half_width("d", 3.0).value == pytest.approx(
            3.0 / math.sqrt(3.0))
        assert from_half_width("d", 3.0).distribution == "rectangular"

    def test_divisor_table_has_exactly_these_four(self):
        assert set(DISTRIBUTIONS) == {
            "normal", "rectangular", "triangular", "u_shaped"}

    def test_unknown_distribution_rejected(self):
        with pytest.raises(UncertaintyError, match="unknown distribution"):
            from_half_width("x", 1.0, "gaussian-ish")
        with pytest.raises(UncertaintyError, match="unknown distribution"):
            UncertaintyComponent("x", 1.0, math.inf, "B", "lognormal")

    def test_negative_half_width_rejected(self):
        with pytest.raises(UncertaintyError, match="half-width"):
            from_half_width("x", -1.0)


# ------------------------------------------------- coverage factor behaviour

class TestCoverageFactor:
    def test_all_infinite_dof_gives_infinite_nu_and_normal_k(self):
        budget = UncertaintyBudget((
            from_half_width("a", 1.0),
            from_confidence("b", 2.0, 0.95),
            from_expanded("c", 3.0, 2.0),
        ))
        assert budget.effective_dof() == math.inf
        assert round(budget.coverage_factor(0.95), 3) == 1.960
        assert round(budget.coverage_factor(0.99), 3) == 2.576

    def test_single_type_a_gives_nu_eff_exactly_n_minus_one(self):
        samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]      # n = 7
        budget = UncertaintyBudget((type_a("repeat", samples),))
        assert budget.effective_dof() == pytest.approx(6.0, rel=1e-12)
        assert budget.coverage_factor(0.95) == pytest.approx(
            stats.t.ppf(0.975, 6), rel=1e-12)
        # ... and that is 2.447, decisively not 2.
        assert budget.coverage_factor(0.95) == pytest.approx(2.4469, abs=1e-4)

    def test_type_a_sensitivity_does_not_change_nu_eff(self):
        # nu_eff is scale-invariant: c_i cancels between numerator and
        # denominator when there is one component.
        samples = [1.0, 2.0, 3.0, 4.0]
        plain = UncertaintyBudget((type_a("r", samples),))
        scaled = UncertaintyBudget((type_a("r", samples, sensitivity=17.0),))
        assert scaled.effective_dof() == pytest.approx(plain.effective_dof())

    def test_expanded_is_k_times_uc(self):
        budget = UncertaintyBudget((std("a", 2.0), std("b", 3.0)))
        assert budget.combined() == pytest.approx(math.sqrt(13.0))
        assert budget.expanded(0.95) == pytest.approx(
            budget.coverage_factor(0.95) * math.sqrt(13.0))

    def test_zero_uncertainty_does_not_divide_by_zero(self):
        budget = UncertaintyBudget((std("zero", 0.0, dof=4.0),))
        assert budget.combined() == 0.0
        assert budget.effective_dof() == math.inf
        assert budget.expanded(0.95) == 0.0

    def test_confidence_must_be_a_probability(self):
        budget = UncertaintyBudget((std("a", 1.0),))
        for bad in (95, 0.0, 1.0, -0.5):
            with pytest.raises(UncertaintyError, match="confidence"):
                budget.coverage_factor(bad)

    def test_normal_coverage_factor_literals(self):
        assert round(normal_coverage_factor(0.95), 3) == 1.960
        assert round(normal_coverage_factor(0.99), 3) == 2.576
        assert normal_coverage_factor(0.6827) == pytest.approx(1.0, abs=1e-4)


# ----------------------------------------------------- Type B constructors

class TestTypeBConstructors:
    def test_from_expanded_divides_by_the_coverage_factor(self):
        assert from_expanded("cert", 1.0, 2.0).value == 0.5

    def test_from_confidence_divides_by_the_normal_quantile(self):
        c = from_confidence("conf", 1.0, 0.95)
        assert c.value == pytest.approx(1.0 / 1.959963984540054, rel=1e-12)

    def test_expanded_and_confidence_are_not_interchangeable(self):
        # k = 2 is not "95 %": it is 95.45 % for infinite dof.  Confusing the
        # two is a 2 % error on the reference contribution, in the unsafe
        # direction if the certificate said 95 % and it was read as k = 2.
        at_k2 = from_expanded("cert", 1.0, 2.0).value
        at_95 = from_confidence("conf", 1.0, 0.95).value
        assert at_k2 != pytest.approx(at_95, rel=1e-6)
        assert at_95 > at_k2

    def test_from_confidence_at_99_uses_2576(self):
        c = from_confidence("conf", 1.0, 0.99)
        assert c.value == pytest.approx(1.0 / 2.5758293035489004, rel=1e-12)

    def test_from_confidence_with_finite_dof_divides_by_t_not_z(self):
        # A certificate quoting "U at 95 % confidence with nu_eff = 10" is
        # asserting U = t_95(10)*u, and t.ppf(0.975, 10) = 2.22814.  Dividing
        # by 1.95996 instead would put 0.51021 into the budget where 0.44880
        # belongs -- 14 % too big, and then Welch-Satterthwaite hands the same
        # nu = 10 back to the coverage factor and widens it again.
        c = from_confidence("cert", 1.0, 0.95, dof=10.0)
        assert stats.t.ppf(0.975, 10.0) == pytest.approx(2.228139, abs=1e-6)
        assert c.value == pytest.approx(1.0 / 2.228139, rel=1e-6)
        assert c.value == pytest.approx(0.448806, abs=1e-6)
        assert c.value != pytest.approx(1.0 / Z95, rel=1e-3)

    def test_finite_dof_certificate_round_trips_through_the_budget(self):
        # The property that makes it right: U in, U out.  The divisor going in
        # is the same quantile the budget applies coming out.
        for dof in (2.0, 4.0, 10.0, 60.0, 1e6):
            budget = UncertaintyBudget(
                (from_confidence("cert", 2.4e-6, 0.95, dof=dof),))
            assert budget.effective_dof() == pytest.approx(dof, rel=1e-9)
            assert budget.expanded(0.95) == pytest.approx(2.4e-6, rel=1e-9)
        # ...at 99 % too, and for the infinite-dof default.
        assert UncertaintyBudget(
            (from_confidence("cert", 2.4e-6, 0.99, dof=4.0),)
        ).expanded(0.99) == pytest.approx(2.4e-6, rel=1e-9)
        assert UncertaintyBudget(
            (from_confidence("cert", 2.4e-6, 0.95),)
        ).expanded(0.95) == pytest.approx(2.4e-6, rel=1e-9)

    def test_the_inflation_the_docstring_quotes(self):
        # t_p(nu)/z_p - 1, the factor by which dividing a finite-dof
        # certificate by the normal quantile would inflate U on the round
        # trip.  Pinned because the docstring quotes these three numbers.
        for dof, percent in ((10.0, 13.7), (4.0, 41.7), (2.0, 119.5)):
            inflated = UncertaintyComponent(
                "wrong", 1.0 / Z95, dof, "B", "normal")
            round_tripped = UncertaintyBudget((inflated,)).expanded(0.95)
            assert (round_tripped - 1.0) * 100 == pytest.approx(
                percent, abs=0.05)

    def test_infinite_dof_still_divides_by_the_normal_quantile(self):
        # The fix must not move the default: t at infinite dof is the normal.
        assert from_confidence("c", 1.0, 0.95).value == pytest.approx(
            1.0 / Z95, rel=1e-12)
        assert from_confidence("c", 1.0, 0.99).value == pytest.approx(
            1.0 / Z99, rel=1e-12)
        assert from_confidence("c", 1.0, 0.95).value == pytest.approx(
            normal_coverage_factor(0.95) ** -1, rel=1e-12)

    def test_calibrator_uncert_forwards_a_finite_dof_to_the_divisor(self):
        c = from_calibrator_uncert("5700A", 1.0, 0.95, dof=10.0)
        assert c.value == pytest.approx(1.0 / stats.t.ppf(0.975, 10.0),
                                        rel=1e-12)

    def test_from_confidence_rejects_a_bad_dof(self):
        with pytest.raises(UncertaintyError, match="degrees of freedom"):
            from_confidence("c", 1.0, 0.95, dof=0.0)

    def test_calibrator_uncert_follows_cal_conf(self):
        # Same UNCERT? number, two CAL_CONF settings, two standard
        # uncertainties -- the 99 % one is the smaller u for the same figure.
        at95 = from_calibrator_uncert("5700A", 4.5, 0.95)
        at99 = from_calibrator_uncert("5700A", 4.5, 0.99)
        assert at95.value == pytest.approx(4.5 / Z95, rel=1e-12)
        assert at99.value == pytest.approx(4.5 / Z99, rel=1e-12)
        assert at99.value < at95.value
        assert at95.source == "UNCERT?"
        assert at95.type == "B"
        assert at95.distribution == "normal"

    def test_calibrator_uncert_is_neither_rectangular_nor_standard(self):
        # Both wrong treatments give a *larger* u than the correct one,
        # because sqrt(3) = 1.732 and 1 are both smaller divisors than either
        # normal quantile.  Pinned with the actual percentages so nobody
        # "corrects" the conversion into one of them on the strength of a
        # remembered direction.
        u = from_calibrator_uncert("5700A", 4.5, 0.99).value
        rectangular = 4.5 / math.sqrt(3.0)
        assert rectangular > u
        assert rectangular / u == pytest.approx(Z99 / math.sqrt(3.0), rel=1e-9)
        assert rectangular / u == pytest.approx(1.487, abs=0.001)  # +49 %
        assert 4.5 / u == pytest.approx(Z99, rel=1e-12)            # +158 %

        u95 = from_calibrator_uncert("5700A", 4.5, 0.95).value
        assert (4.5 / math.sqrt(3.0)) / u95 == pytest.approx(1.132, abs=0.001)

    def test_negative_expanded_uncertainty_rejected(self):
        with pytest.raises(UncertaintyError):
            from_expanded("x", -1.0, 2.0)
        with pytest.raises(UncertaintyError):
            from_confidence("x", -1.0, 0.95)

    def test_zero_coverage_factor_rejected(self):
        with pytest.raises(UncertaintyError, match="coverage factor"):
            from_expanded("x", 1.0, 0.0)


class TestTypeA:
    def test_standard_error_of_the_mean(self):
        samples = [10.0, 12.0, 14.0, 16.0]      # mean 13, s = 2.5820
        c = type_a("repeat", samples)
        expected_s = math.sqrt(((3 ** 2) + (1 ** 2) + (1 ** 2) + (3 ** 2)) / 3)
        assert expected_s == pytest.approx(2.581989, abs=1e-6)
        assert c.value == pytest.approx(expected_s / 2.0, rel=1e-12)
        assert c.dof == 3.0
        assert c.type == "A"
        assert c.distribution == "normal"

    def test_uses_sample_not_population_sd(self):
        samples = [1.0, 2.0, 3.0, 4.0]
        c = type_a("repeat", samples)
        population_s = math.sqrt(sum((x - 2.5) ** 2 for x in samples) / 4)
        sample_s = math.sqrt(sum((x - 2.5) ** 2 for x in samples) / 3)
        assert c.value == pytest.approx(sample_s / 2.0, rel=1e-12)
        assert c.value != pytest.approx(population_s / 2.0, rel=1e-6)

    def test_rejects_fewer_than_two_samples(self):
        with pytest.raises(UncertaintyError, match="at least 2 samples"):
            type_a("repeat", [1.0])
        with pytest.raises(UncertaintyError, match="at least 2 samples"):
            type_a("repeat", [])

    def test_rejects_non_finite_samples(self):
        with pytest.raises(UncertaintyError):
            type_a("repeat", [1.0, float("nan")])
        with pytest.raises(UncertaintyError):
            type_a("repeat", [1.0, float("inf")])

    def test_identical_samples_give_zero_not_an_error(self):
        c = type_a("repeat", [5.0] * 4)
        assert c.value == 0.0
        assert c.dof == 3.0


# --------------------------------------------------------- sensitivity

class TestSensitivity:
    def test_c_of_two_contributes_four_times_the_variance(self):
        plain = std("a", 1.0)
        doubled = std("a", 1.0, sensitivity=2.0)
        assert doubled.variance == pytest.approx(4.0 * plain.variance)
        assert UncertaintyBudget((doubled,)).variance() == pytest.approx(4.0)
        assert UncertaintyBudget((doubled,)).combined() == pytest.approx(2.0)

    def test_negative_sensitivity_keeps_its_sign_in_the_contribution(self):
        c = std("a", 2.0, sensitivity=-3.0)
        assert c.contribution == pytest.approx(-6.0)
        assert c.variance == pytest.approx(36.0)
        assert UncertaintyBudget((c,)).combined() == pytest.approx(6.0)

    def test_zero_sensitivity_drops_out_entirely(self):
        budget = UncertaintyBudget((std("a", 1.0),
                                    std("huge", 1e6, sensitivity=0.0)))
        assert budget.combined() == pytest.approx(1.0)


# ------------------------------------------------------------- the budget

class TestBudget:
    def test_empty_budget_combines_to_zero_without_raising(self):
        budget = UncertaintyBudget()
        assert len(budget) == 0
        assert budget.combined() == 0.0
        assert budget.variance() == 0.0
        assert budget.effective_dof() == math.inf
        assert budget.expanded(0.95) == 0.0
        assert budget.contributions() == []

    def test_add_returns_a_new_budget_and_skips_none(self):
        budget = UncertaintyBudget()
        grown = budget.add(std("a", 1.0), None, std("b", 2.0))
        assert len(budget) == 0            # unchanged: frozen
        assert len(grown) == 2
        assert [c.label for c in grown] == ["a", "b"]

    def test_add_rejects_non_components(self):
        with pytest.raises(UncertaintyError):
            UncertaintyBudget((std("a", 1.0), 3.0))

    def test_root_sum_of_squares(self):
        budget = UncertaintyBudget((std("a", 3.0), std("b", 4.0)))
        assert budget.combined() == pytest.approx(5.0)

    def test_contributions_sum_to_100_and_descend(self):
        budget = UncertaintyBudget((
            std("small", 1.0), std("big", 4.0), std("middle", 2.0),
            std("negative", 3.0, sensitivity=-1.0),
        ))
        rows = budget.contributions()
        assert [label for label, _, _ in rows] == [
            "big", "negative", "middle", "small"]
        assert sum(share for _, _, share in rows) == pytest.approx(100.0)
        # 16/30, 9/30, 4/30, 1/30 of the variance
        assert [round(share, 4) for _, _, share in rows] == [
            pytest.approx(53.3333, abs=1e-3), pytest.approx(30.0, abs=1e-3),
            pytest.approx(13.3333, abs=1e-3), pytest.approx(3.3333, abs=1e-3)]

    def test_contributions_of_a_zero_budget_are_zero_percent(self):
        budget = UncertaintyBudget((std("a", 0.0), std("b", 0.0)))
        assert [share for _, _, share in budget.contributions()] == [0.0, 0.0]


class TestComponentValidation:
    def test_negative_standard_uncertainty_rejected(self):
        with pytest.raises(UncertaintyError, match=">= 0"):
            UncertaintyComponent("a", -1.0, math.inf, "B", "normal")

    def test_non_finite_value_rejected(self):
        with pytest.raises(UncertaintyError, match="finite"):
            UncertaintyComponent("a", math.inf, math.inf, "B", "normal")
        with pytest.raises(UncertaintyError, match="finite"):
            UncertaintyComponent("a", float("nan"), math.inf, "B", "normal")

    def test_zero_or_negative_dof_rejected(self):
        with pytest.raises(UncertaintyError, match="degrees of freedom"):
            UncertaintyComponent("a", 1.0, 0.0, "A", "normal")
        with pytest.raises(UncertaintyError, match="degrees of freedom"):
            UncertaintyComponent("a", 1.0, -3.0, "A", "normal")

    def test_infinite_dof_allowed(self):
        assert UncertaintyComponent("a", 1.0, math.inf, "B", "normal").dof \
            == math.inf

    def test_bad_type_rejected(self):
        with pytest.raises(UncertaintyError, match="type"):
            UncertaintyComponent("a", 1.0, math.inf, "C", "normal")

    def test_empty_label_rejected(self):
        with pytest.raises(UncertaintyError, match="label"):
            UncertaintyComponent("   ", 1.0, math.inf, "B", "normal")

    def test_component_is_frozen(self):
        c = std("a", 1.0)
        with pytest.raises(Exception):
            c.value = 2.0


# ------------------------------------------------- standard builders

class TestStandardBuilders:
    def test_resolution_is_half_an_lsd_rectangular(self):
        # 6.5 digits on the 10 V range: 10 uV per count.
        c = resolution_component("34401A resolution", 10e-6)
        assert c.value == pytest.approx(5e-6 / math.sqrt(3.0), rel=1e-12)
        assert c.value == pytest.approx(2.8868e-6, abs=1e-10)
        assert c.distribution == "rectangular"
        assert c.type == "B"
        assert c.dof == math.inf

    def test_thermal_emf_is_rectangular_over_the_operator_bound(self):
        c = thermal_emf_component("thermal EMF", 2e-6)
        assert c.value == pytest.approx(2e-6 / math.sqrt(3.0), rel=1e-12)
        assert c.distribution == "rectangular"
        assert c.source == "operator"

    def test_loading_fraction_is_negative_zs_over_zs_plus_zin(self):
        # 5700A 220 mV range (50 ohm out) into a 34401A at 10 Mohm.
        f = loading_fraction(50.0, 10e6)
        assert f == pytest.approx(-50.0 / (50.0 + 10e6), rel=1e-15)
        assert f * 1e6 == pytest.approx(-5.0, abs=0.001)   # -5.0 ppm
        assert f < 0.0                                     # always reads low

    def test_loading_correction_restores_the_true_value(self):
        reading = 0.1
        correction, component = loading_correction(reading, 50.0, 10e6)
        true = reading + correction
        # A meter of 10 Mohm across 50 ohm reads Zin/(Zs+Zin) of the truth.
        assert reading == pytest.approx(true * 10e6 / (10e6 + 50.0), rel=1e-15)
        assert correction == pytest.approx(0.1 * 50.0 / 10e6, rel=1e-15)
        assert correction == pytest.approx(500e-9, rel=1e-9)    # 500 nV

    def test_loading_uncertainty_is_a_fraction_of_the_correction(self):
        correction, component = loading_correction(
            0.1, 50.0, 10e6, relative_uncertainty=0.1)
        assert component.value == pytest.approx(
            0.1 * correction / math.sqrt(3.0), rel=1e-12)
        assert component.distribution == "rectangular"
        assert "50" in component.source and "ppm" in component.source

    def test_correcting_beats_bounding(self):
        # Uncorrected, the whole 5 ppm has to be carried as a bound; corrected,
        # only 10 % of it does -- and the bias is gone as well.
        _, bounded = loading_correction(0.1, 50.0, 10e6,
                                        relative_uncertainty=1.0)
        _, corrected = loading_correction(0.1, 50.0, 10e6,
                                          relative_uncertainty=0.1)
        assert bounded.value == pytest.approx(10.0 * corrected.value, rel=1e-9)

    def test_loading_rejects_impossible_impedances(self):
        with pytest.raises(UncertaintyError, match="z_input"):
            loading_fraction(50.0, 0.0)
        with pytest.raises(UncertaintyError, match="z_source"):
            loading_fraction(-1.0, 10e6)

    def test_tempco_component_scales_with_delta_t(self):
        # 3 ppm/C, 5 C off reference, on a 10 V reading:
        #   a = 3e-6 * 10 * 5 = 150 uV -> u = 150/sqrt(3) = 86.6 uV
        c = tempco_component("tempco", 3.0, 5.0, 10.0)
        assert c.value == pytest.approx(150e-6 / math.sqrt(3.0), rel=1e-12)
        assert c.distribution == "rectangular"
        assert "3 ppm/C" in c.source

    def test_tempco_uses_magnitudes_so_sign_of_delta_t_is_irrelevant(self):
        assert tempco_component("t", 3.0, -5.0, 10.0).value == pytest.approx(
            tempco_component("t", 3.0, 5.0, 10.0).value)

    def test_tempco_returns_none_when_delta_t_is_unknown(self):
        assert tempco_component("tempco", 3.0, None, 10.0) is None
        # ... and a None flows through add() without an if at the call site.
        budget = UncertaintyBudget().add(
            std("a", 1.0), tempco_component("tempco", 3.0, None, 10.0))
        assert len(budget) == 1


# ------------------------------------------------------------ presentation

class TestFormatBudget:
    @pytest.fixture
    def budget(self):
        return UncertaintyBudget((
            type_a("repeatability", [10.0, 11.0, 12.0, 13.0]),
            from_calibrator_uncert("5700A UNCERT?", 4.5, 0.99),
            resolution_component("resolution", 1.0),
        ))

    def test_table_has_a_row_per_component_and_a_summary(self, budget):
        text = format_budget(budget, "uV")
        assert "Uncertainty budget [uV]" in text
        for label in ("repeatability", "5700A UNCERT?", "resolution"):
            assert label in text
        assert "u_c" in text and "nu_eff" in text
        assert "k(95 %)" in text
        assert "expanded uncertainty" in text

    def test_numbers_in_the_summary_match_the_budget(self, budget):
        text = format_budget(budget, "uV", confidence=0.99)
        assert f"{budget.combined():.4g}" in text
        assert f"{budget.expanded(0.99):.4g}" in text
        assert f"{budget.coverage_factor(0.99):.3f}" in text
        assert "99 %" in text

    def test_rows_are_dominance_sorted_by_default(self, budget):
        lines = format_budget(budget, "uV").splitlines()
        order = [line.split()[0] for line in lines
                 if line and line[0].isalnum()]
        assert order.index("repeatability") < order.index("resolution")

    def test_sort_false_keeps_declaration_order(self):
        budget = UncertaintyBudget((std("tiny", 1e-9), std("huge", 1.0)))
        text = format_budget(budget, "V", sort=False)
        assert text.index("tiny") < text.index("huge")
        assert format_budget(budget, "V").index("huge") < \
            format_budget(budget, "V").index("tiny")

    def test_infinite_dof_prints_as_inf(self, budget):
        assert "inf" in format_budget(budget, "uV")

    def test_empty_budget_formats_without_raising(self):
        text = format_budget(UncertaintyBudget(), "V")
        assert "u_c = 0" in text


# ----------------------------------------------------------- serialisation

class TestSerialisation:
    @pytest.fixture
    def budget(self):
        return UncertaintyBudget((
            type_a("repeatability", [10.0, 11.5, 12.25, 13.0]),
            from_calibrator_uncert("5700A UNCERT?", 4.5, 0.99),
            from_half_width("thermal EMF", 2.0, "rectangular",
                            sensitivity=-1.5, source="operator"),
            from_half_width("cyclic ambient", 0.5, "u_shaped", dof=50.0),
        ))

    def test_rows_carry_exactly_the_declared_fields(self, budget):
        rows = budget_to_rows(budget)
        assert len(rows) == 4
        for row in rows:
            assert tuple(row) == BUDGET_FIELDS

    def test_round_trip_reproduces_every_field(self, budget):
        restored = budget_from_rows(budget_to_rows(budget))
        assert restored.components == budget.components
        for before, after in zip(budget.components, restored.components):
            assert after.label == before.label
            assert after.value == before.value          # exact, via repr
            assert after.dof == before.dof
            assert after.type == before.type
            assert after.distribution == before.distribution
            assert after.sensitivity == before.sensitivity
            assert after.source == before.source

    def test_round_trip_reproduces_the_derived_quantities(self, budget):
        restored = budget_from_rows(budget_to_rows(budget))
        assert restored.combined() == budget.combined()
        assert restored.effective_dof() == budget.effective_dof()
        assert restored.coverage_factor(0.95) == budget.coverage_factor(0.95)
        assert restored.expanded(0.95) == budget.expanded(0.95)

    def test_infinite_dof_survives_the_csv(self, budget):
        rows = budget_to_rows(budget)
        assert rows[1]["dof"] == "inf"
        assert budget_from_rows(rows).components[1].dof == math.inf

    def test_survives_a_real_csv_writer_and_reader(self, budget, tmp_path):
        import csv
        path = tmp_path / "budget.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(BUDGET_FIELDS))
            writer.writeheader()
            writer.writerows(budget_to_rows(budget))
        with path.open(newline="") as handle:
            restored = budget_from_rows(csv.DictReader(handle))
        assert restored.components == budget.components
        assert restored.expanded(0.95) == budget.expanded(0.95)


    def test_numeric_values_are_accepted_as_well_as_strings(self):
        rows = [{"label": "a", "value": 0.5, "dof": math.inf, "type": "B",
                 "distribution": "normal", "sensitivity": 1.0, "source": ""}]
        assert budget_from_rows(rows).combined() == pytest.approx(0.5)

    def test_missing_field_is_a_clear_error(self):
        rows = [{"label": "a", "value": "0.5", "type": "B",
                 "distribution": "normal", "sensitivity": "1.0"}]
        with pytest.raises(UncertaintyError, match="missing field"):
            budget_from_rows(rows)

    def test_absent_source_defaults_to_empty(self):
        rows = [{"label": "a", "value": "0.5", "dof": "inf", "type": "B",
                 "distribution": "normal", "sensitivity": "1.0"}]
        assert budget_from_rows(rows).components[0].source == ""

    def test_unparseable_dof_names_the_row(self):
        rows = [{"label": "a", "value": "0.5", "dof": "many", "type": "B",
                 "distribution": "normal", "sensitivity": "1.0"}]
        with pytest.raises(UncertaintyError, match="row 0 dof"):
            budget_from_rows(rows)

    def test_type_is_normalised_on_the_way_in(self):
        rows = [{"label": "a", "value": "0.5", "dof": "3", "type": " a ",
                 "distribution": " normal ", "sensitivity": "1.0"}]
        component = budget_from_rows(rows).components[0]
        assert component.type == "A"
        assert component.distribution == "normal"

    def test_empty_budget_round_trips(self):
        assert budget_from_rows(budget_to_rows(UncertaintyBudget())) == \
            UncertaintyBudget()


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

    import math
    from caldmm.cal.uncertainty import (
        UncertaintyBudget, from_confidence, type_a)

    budget = UncertaintyBudget((
        type_a("repeat", [1.0, 2.0, 3.0, 4.0]),
        from_confidence("cal", 1.0, 0.95),
    ))
    assert budget.combined() > 0
    assert math.isfinite(budget.effective_dof())
    assert budget.coverage_factor(0.95) > 2.0

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal pulled in drivers: " + repr(leaked)
    print("OK")
    """)


def test_uncertainty_imports_with_no_hardware_libraries():
    """The architectural invariant, pinned in a clean interpreter.

    ``cal.uncertainty`` must build and evaluate a budget with ``Gpib``,
    ``serial``, ``pyvisa`` and ``smbus`` absent and un-importable, and without
    dragging in a driver.  A subprocess is used so the root conftest's mock
    stubs are not in the way.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

"""Tests for the offline verification-report generator.

The headline is that **the report is re-derivable from its own data file**: the
analyzer reads nothing but the result CSV, and every uncertainty budget it
prints is rebuilt component by component from the row it belongs to, with
``u_c``, ``nu_eff``, ``k`` and ``U`` recomputed rather than read back.  That is
the claim the whole subsystem rests on, and the first class here defends it.

The second thing under test is that the two reasons a point is INDETERMINATE
are never blurred: "the TUR floor blocked it" says the *reference* is not good
enough, "the temperature gate blocked it" says the specification's stated
conditions were not shown to hold, and a report that merges them is misleading
in exactly the way the floor exists to prevent.

No hardware, no window, no network: matplotlib is forced onto Agg inside the
renderer, and every run below writes to ``tmp_path``.
"""

import datetime
import math
import os
import re

import pytest

from caldmm.apps import cal_analyze as ca
from caldmm.cal import uncertainty as unc
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.result import (
    ResultRow,
    RunHeader,
    load_results,
    summarize_run,
    write_results,
)
from caldmm.cal.roles import Uncertainty
from caldmm.cal.verdict import (
    FLAG_AMBIENT_UNKNOWN,
    FLAG_TUR_BELOW_FLOOR,
    Verdict,
    evaluate,
)

STAMP = datetime.datetime(2026, 8, 1, 11, 0, 0)


# --------------------------------------------------------------- fixtures

def good_budget(scale: float = 1.0) -> unc.UncertaintyBudget:
    """A five-component 10 V budget: calibrator, repeats, resolution, EMF, tempco.

    ``from_calibrator_uncert`` takes the **absolute** expanded uncertainty, so
    3.5e-6 V on a 10 V point is 0.35 ppm and the label has to say 0.35.  It said
    3.5, which described a budget ten times the one it built -- the shape of
    fixture that lets a rendering bug through because the fixture and the code
    disagree about what world they are in.  The whole file is the 0.35 ppm
    world: ``make_row``'s ``note`` already reads ``fluke5700a 3.5e-06 V`` and
    its spec ratio of 2.4 is 8.5e-06/3.5e-06.
    """
    return unc.UncertaintyBudget().add(
        unc.from_calibrator_uncert("5700A output", 3.5e-6 * scale, 0.95,
                                   source="UNCERT? 0.35 PPM"),
        unc.type_a("repeatability",
                   [10.0000012, 10.0000010, 10.0000015, 10.0000011]),
        unc.resolution_component("3458A resolution", 1e-6),
        unc.thermal_emf_component("thermal EMF", 0.3e-6),
        unc.tempco_component("3458A tempco", 0.15, 1.4, 10.0),
    )


def coarse_budget() -> unc.UncertaintyBudget:
    """A budget dominated by a reference no better than the DUT (low TUR).

    Always used on a **1 V** row whose note reads ``spec ratio 0.4 (hp3458a
    1.4e-05 / fluke5700a 3.5e-05 V)``, so the calibrator's figure is 3.5e-5 V
    = 35 ppm and the label says 35.  It used to be 6.0e-6 V labelled "60 PPM"
    -- a number that was neither the note's reference limit nor 60 ppm of
    anything, on the one row in the file whose entire purpose is that the
    reference is worse than the DUT.
    """
    return unc.UncertaintyBudget().add(
        unc.from_calibrator_uncert("5700A output", 3.5e-5, 0.95,
                                   source="UNCERT? 35 PPM"),
        unc.type_a("repeatability",
                   [1.0000012, 1.0000010, 1.0000019, 1.0000011]),
    )


def zero_budget() -> unc.UncertaintyBudget:
    """A short-circuit point's budget: thermal EMF and the display, nothing else."""
    return unc.UncertaintyBudget().add(
        unc.thermal_emf_component("thermal EMF", 0.2e-6),
        unc.resolution_component("3458A resolution", 1e-8),
        unc.type_a("repeatability", [1.4e-7, 1.5e-7, 1.6e-7, 1.5e-7]),
    )


def make_row(*, error, limit, budget, ambient_c=23.147125,
             window=(18.0, 28.0), reference_ppm=0.35, conditions=None,
             **overrides) -> ResultRow:
    """One executed point, decided by the real ``verdict.evaluate``.

    *reference_ppm* is the calibrator's figure for this point, and it drives
    **both** ``source_uncertainty`` (its live ``UNCERT?``) and ``spec_limit``
    (the same number from its spec table), so ``limit_disagree`` stays quiet at
    every nominal.  Pass the value the row's budget actually carries: a row
    whose budget says 35 ppm and whose ``spec_limit`` says 0.35 describes a
    bench that cannot exist, and a fixture describing an impossible world is
    how two defects got through review on this branch.
    """
    conformity = evaluate(error, limit, budget, ambient_c=ambient_c,
                          temperature_window_c=window, conditions=conditions)
    nominal = overrides.get("nominal", 10.0)
    fields = dict(
        quantity=Quantity.DCV,
        nominal=10.0,
        timestamp=STAMP,
        run_id="20260801T110000-3458a",
        point_index=1,
        group_index=0,
        label="10 V dc",
        note="spec ratio 2.4 (hp3458a 8.5e-06 / fluke5700a 3.5e-06 V)",
        frequency_hz=0.0,
        connection=Connection.TWO_WIRE,
        range_arg=10.0,
        range_id="10V",
        interval="1y",
        source_model="fluke5700a",
        source_value=nominal,
        source_unit="V",
        source_uncertainty=Uncertainty(reference_ppm, "PPM", 0.95, 365),
        source_settled=True,
        source_operate=True,
        meter_model="hp3458a",
        mean=nominal + error,
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
        ambient_c=ambient_c,
        humidity_pct=41.0,
        env_timestamp=datetime.datetime(2026, 8, 1, 10, 59, 3),
        env_source="probe",
        reference_value=nominal,
        error=conformity.error,
        tolerance_limit=conformity.tolerance_limit,
        # The same figure the source's own UNCERT? gives above, so
        # ``limit_disagree`` stays quiet at every nominal unless a test
        # overrides one of the two on purpose.  A fraction of ``nominal``
        # rather than a constant, so that holds on the 1 V and 0 V rows too.
        spec_limit=reference_ppm * 1e-6 * abs(nominal),
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
        started=STAMP,
        plan_name="hp3458a 1y verification against fluke5700a",
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
        source_warm_minutes=45,
        meter_model="hp3458a",
        meter_idn="HP3458A",
        meter_calnum=42,
        meter_acal_age_h=6.5,
        meter_temp_c=41.24375,
        ambient_c=23.147125,
        humidity_pct=41.078125,
        ambient_timestamp=datetime.datetime(2026, 8, 1, 10, 59),
        ambient_source="probe",
        caldmm_version="0.1.0",
        git_revision="a30ed41",
    )
    fields.update(overrides)
    return RunHeader(**fields)


def mixed_rows() -> list[ResultRow]:
    """A small run with one of every outcome that matters to a report."""
    rows = [
        # 1: a clean PASS -- TUR ~ 5, ambient in window
        make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                 point_index=1, group_index=0, label="10 V dc"),
        # 2: a FAIL, well past the limit
        make_row(error=90e-6, limit=40e-6, budget=good_budget(),
                 point_index=2, group_index=0, label="100 V dc",
                 nominal=100.0),
        # 3: INDETERMINATE because the reference is not good enough (TUR < 4)
        make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                 reference_ppm=35.0,
                 point_index=3, group_index=1, label="1 V dc", nominal=1.0,
                 note="spec ratio 0.4 (hp3458a 1.4e-05 / fluke5700a 3.5e-05 V)"),
        # 4: INDETERMINATE because nobody recorded the ambient
        make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                 ambient_c=None, point_index=4, group_index=1,
                 label="10 V dc (no ambient)"),
        # 5: NO_SPEC -- there is no claim to test
        make_row(error=4.0e-6, limit=None, budget=good_budget(),
                 point_index=5, group_index=1, label="1000 V dc",
                 nominal=1000.0),
        # 6: a zero point -- the reference is smaller than its own tolerance,
        #    so "ppm of reading" means nothing and must be blank
        make_row(error=1.5e-7, limit=4e-6, budget=zero_budget(),
                 point_index=6, group_index=2, label="short (zero)",
                 nominal=0.0),
        # 7: an ac point, so the frequency page has something to draw
        make_row(error=3.0e-4, limit=9.0e-4, budget=good_budget(scale=10.0),
                 point_index=7, group_index=3, label="1 V ac 1 kHz",
                 quantity=Quantity.ACV, nominal=1.0, frequency_hz=1000.0),
        make_row(error=8.0e-4, limit=9.0e-4, budget=good_budget(scale=10.0),
                 point_index=8, group_index=3, label="1 V ac 100 kHz",
                 quantity=Quantity.ACV, nominal=1.0, frequency_hz=100000.0),
    ]
    for index, row in enumerate(rows):
        object.__setattr__(row, "timestamp",
                           STAMP + datetime.timedelta(minutes=3 * index))
    return rows


def write_run(tmp_path, rows=None, header=None, name="run.csv") -> str:
    path = str(tmp_path / name)
    write_results(path, header or make_header(),
                  mixed_rows() if rows is None else rows)
    return path


def analyze(path, *extra) -> int:
    return ca.main([path, *extra])


# ------------------------------------------------------- the headline test

class TestBudgetReconstructsFromTheCsv:
    """A report that cannot be re-derived from its own data file is not evidence."""

    def test_reconstructed_budget_matches_the_original(self, tmp_path):
        originals = mixed_rows()
        path = write_run(tmp_path, originals)

        _, loaded = load_results(path)

        assert len(loaded) == len(originals)
        for before, after in zip(originals, loaded):
            assert (after.budget.combined()
                    == pytest.approx(before.budget.combined(), rel=1e-12))
            assert (after.budget.effective_dof()
                    == pytest.approx(before.budget.effective_dof(), rel=1e-12))
            assert (after.coverage_factor
                    == pytest.approx(before.coverage_factor, rel=1e-12))
            assert (after.expanded_uncertainty
                    == pytest.approx(before.expanded_uncertainty, rel=1e-12))

    def test_rendered_budget_prints_the_original_numbers(self, tmp_path):
        """The markdown's budget block is the original budget, digit for digit."""
        originals = mixed_rows()
        path = write_run(tmp_path, originals)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        for row in originals:
            expected = unc.format_budget(row.budget, row.unit,
                                         confidence=row.confidence)
            for line in expected.splitlines():
                if line.startswith(("combined standard", "effective degrees",
                                    "coverage factor", "expanded uncertainty")):
                    assert line in text, (
                        f"point {row.point_index}: {line!r} missing from the "
                        f"regenerated report")

    def test_every_point_gets_a_budget_and_a_dominant_term(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        for row in rows:
            assert f"### Point {row.point_index} —" in text
            label, percent = ca.dominant_contribution(row)
            assert 0.0 <= percent <= 100.0
            assert f"Dominated by: **{label}**" in text

    def test_dominant_term_is_the_largest_contributor(self, tmp_path):
        path = write_run(tmp_path)
        _, rows = load_results(path)

        row = rows[0]
        label, percent = ca.dominant_contribution(row)
        shares = [share for _, _, share in row.budget.contributions()]
        assert percent == pytest.approx(max(shares))
        assert label == row.budget.contributions()[0][0]


# ------------------------------------------ the two indeterminate causes

def causes_of(rows, header=None):
    """The four causes, as the analyzer computes them for a real run."""
    header = header or make_header()
    summary = summarize_run(rows,
                            temperature_policy=ca.policy_of(header)[0])
    return ca.indeterminate_causes(summary)


class TestIndeterminateCausesAreNotConflated:
    def test_tur_and_temperature_are_counted_separately(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        causes = causes_of(rows, header)

        assert causes["tur_floor"] == 1
        assert causes["temperature"] == 1
        assert causes["guard_band"] == 0
        assert causes["no_budget"] == 0
        # ... and the two blocked points are genuinely different points
        blocked_by_tur = [r for r in rows if r.indeterminate_by_tur]
        blocked_by_temp = [r for r in rows if r.blocked_by_temperature()]
        assert [r.point_index for r in blocked_by_tur] == [3]
        assert [r.point_index for r in blocked_by_temp] == [4]
        assert FLAG_TUR_BELOW_FLOOR in blocked_by_tur[0].flags
        assert FLAG_AMBIENT_UNKNOWN in blocked_by_temp[0].flags

    def test_a_point_blocked_by_both_is_counted_under_both(self, tmp_path):
        """Overlapping causes, not a partition: fixing one would not clear it."""
        row = make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                       reference_ppm=35.0, ambient_c=None, point_index=1,
                       label="1 V dc", nominal=1.0)
        assert row.verdict is Verdict.INDETERMINATE

        causes = causes_of([row])

        assert causes["tur_floor"] == 1
        assert causes["temperature"] == 1
        assert causes["guard_band"] == 0

    def test_unmet_conditions_are_their_own_cause(self):
        """M65/M85: the specification was not in force at this point.

        Not the guard band (that is a statement about the DUT) and not the TUR
        floor (that is a statement about the reference): the figures simply do
        not apply, so a report must say so in its own words.
        """
        row = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                       point_index=1,
                       conditions=[("source-not-warmed-up",
                                    "the source had been warm for 5 min of "
                                    "the 30 its specifications require")])
        assert row.verdict is Verdict.INDETERMINATE

        causes = causes_of([row])

        assert causes["conditions"] == 1
        assert causes["guard_band"] == 0
        assert causes["temperature"] == 0

    def test_the_report_names_each_unmet_condition(self):
        rows = [
            make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                     point_index=1,
                     conditions=[("source-not-warmed-up",
                                  "the source had been warm for 5 min of the "
                                  "30 its specifications require")]),
            make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                     point_index=2, nominal=1.0,
                     conditions=[("source-zeros-due",
                                  "the source's dc zeros are 45 days old")]),
        ]
        header = make_header()
        text = ca.format_markdown("run.csv", header, rows)

        first = text.split("## Provenance")[0]
        assert "Read this first" in first
        assert "warm" in first
        assert "dc zeros" in first
        assert "not counted as PASS or FAIL" in first

    def test_the_report_names_the_claim_a_source_run_tested(self):
        """M64: "interval 1y" in the header, 24-hour figures in the verdicts."""
        row = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                       point_index=1)
        header = make_header(dut_role="source", interval="1y",
                             source_cal_interval_days=1,
                             source_cal_confidence="CONF99")

        text = ca.format_markdown("run.csv", header, [row])

        assert "UNCERT? at 1 d / CONF99" in text
        first = text.split("## Provenance")[0]
        assert "claim tested" in first.lower()

    def test_a_matching_interval_is_not_flagged_as_a_disagreement(self):
        row = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                       point_index=1)
        header = make_header(dut_role="source", interval="1y",
                             source_cal_interval_days=365,
                             source_cal_confidence="CONF95")

        text = ca.format_markdown("run.csv", header, [row])
        first = text.split("## Provenance")[0]

        assert "UNCERT? at 365 d / CONF95" in text
        assert "claim tested" not in first.lower()

    def test_a_safety_abort_is_the_first_thing_the_report_says(self):
        """§8: a run stopped for the wiring is not a run with a bad point."""
        good = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                        point_index=0)
        stopped = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                           point_index=1, label="wiring proof before 10 V dc",
                           verdict=Verdict.NO_SPEC,
                           flags=("point-failed", "wiring-proof-failed"))
        text = ca.format_markdown("run.csv", make_header(), [good, stopped])

        first = text.split("## Provenance")[0]
        assert "stopped" in first.lower()
        assert "wiring-proof-failed" in first
        assert "not a complete verification" in first

    def test_an_unreadable_proof_is_surfaced_without_blaming_the_wiring(self):
        """The offline reader gets the same distinction the operator did.

        `wiring-proof-failed` is a claim about the bench; a proof the meter
        never answered supports no such claim, and a report that made one
        sent an operator to re-check leads that were fine.
        """
        good = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                        point_index=0)
        stopped = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                           point_index=1,
                           label="wiring proof before 190 kOhm",
                           verdict=Verdict.NO_SPEC,
                           flags=("point-failed", "wiring-proof-unreadable"))
        text = ca.format_markdown("run.csv", make_header(), [good, stopped])

        first = text.split("## Provenance")[0]
        assert "stopped" in first.lower()
        assert "wiring-proof-unreadable" in first
        assert "says nothing about the wiring" in first
        assert "the planned level was never applied" in first

    def test_a_standby_that_would_not_confirm_is_surfaced_first_of_all(self):
        """The one abort that leaves the terminals possibly live.

        The engine stops before it re-ranges the meter, so the run's last row
        is a `point-failed` like any other unless the rule names itself; a
        reader who is about to touch the leads has to be told from the top of
        the report, not from an `EngineError` that scrolled past.
        """
        good = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                        point_index=0)
        stopped = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                           point_index=1, label="1 A dc",
                           verdict=Verdict.NO_SPEC,
                           flags=("point-failed", "standby-not-confirmed"))
        text = ca.format_markdown("run.csv", make_header(), [good, stopped])

        first = text.split("## Provenance")[0]
        assert "standby-not-confirmed" in first
        assert "MAY STILL BE LIVE" in first
        assert "not a complete verification" in first
        # ... and it leads: nothing else in that block matters as much.
        assert (first.index("standby-not-confirmed")
                < first.index("not a complete verification"))

    def test_a_standby_that_would_not_confirm_is_in_the_flag_glossary(self):
        """The Flags section spells out what it means, like every other flag."""
        assert "standby-not-confirmed" in ca._FLAG_WORDS
        rows = [make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                         point_index=i, verdict=Verdict.NO_SPEC,
                         flags=("standby-not-confirmed",))
                for i in range(2)]
        text = ca.format_markdown("run.csv", make_header(), rows)
        assert ca._FLAG_WORDS["standby-not-confirmed"] in text

    def test_a_skipped_wiring_proof_leads_and_is_in_the_glossary(self):
        """§8 as amended: a proof that could not be taken is *reported*.

        The one thing it must not be is invisible: a run whose ac-current
        group carries no wiring proof is a run whose leads nobody checked at
        a safe level, and the reader has to be told so on the first screen.
        """
        row = make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                       point_index=1, label="1 A ac",
                       note="no wiring proof was taken before ACI 1 A: the "
                            "smallest level hp34401a ACI can read to "
                            "+/-10 % is 0.3 A, which is not below the 0.22 A "
                            "hazard threshold",
                       flags=("wiring-proof-not-resolvable",))
        text = ca.format_markdown("run.csv", make_header(), [row])
        first = text.split("## Provenance")[0]
        assert "no safe-level wiring proof" in first
        assert "hazard threshold" in first
        assert "wiring-proof-not-resolvable" in ca._FLAG_WORDS

    @pytest.mark.parametrize("flag", [
        "thermal-emf-default", "thermal-emf-not-converted",
        "loading-corrected", "loading-uncorrected-above-100-khz",
        "tempco-not-evaluated", "reference-confidence-unknown",
        "tolerance-limit-from-spec-table", "source-range-mismatch",
        "no-repeat-uncertainty", "wiring-proof-not-resolvable",
        "wiring-proof-unreadable",
    ])
    def test_every_engine_flag_a_report_can_carry_has_words(self, flag):
        """A flag with no gloss prints "carried by every point", which is not
        an explanation of anything -- and the 1 uV thermal-EMF default in
        particular is a *declared* figure that has to be named as one."""
        assert ca._FLAG_WORDS.get(flag), flag

    def test_the_thermal_emf_default_names_its_figure(self):
        assert "1 uV" in ca._FLAG_WORDS["thermal-emf-default"]

    def test_a_gross_error_abort_is_surfaced_too(self):
        stopped = make_row(error=1.0e-3, limit=14e-6, budget=good_budget(),
                           point_index=1,
                           flags=("gross-error-abort",))
        text = ca.format_markdown("run.csv", make_header(), [stopped])

        first = text.split("## Provenance")[0]
        assert "gross-error-abort" in first
        assert "10x" in first or "ten times" in first

    def test_a_zero_setpoint_prints_its_absolute_error_not_ppm(self):
        """M19: ppm of a meter's own offset is arithmetic, not information."""
        row = make_row(error=2e-6, limit=8e-7, budget=zero_budget(),
                       point_index=1, nominal=0.0, label="0 V dc",
                       source_value=0.0, mean=2e-6, reference_value=2e-6)
        header = make_header(dut_role="source")

        text = ca.format_markdown("run.csv", header, [row])

        assert "1e+06" not in text and "1000000" not in text
        assert row.error_ppm is None
        # The absolute error is still printed, in its own unit.
        assert "2.0000μV" in text

    def test_a_reference_outside_its_own_window_gets_its_own_bullet(self):
        """The engine's condition, and the one a whole run can turn on.

        A 5700A reference more than 5 C from its stored CAL_TEMP makes *every*
        point of a run INDETERMINATE; a report that shows the count without
        naming the reason sends the operator hunting.
        """
        rows = [make_row(error=1.0e-6, limit=14e-6, budget=good_budget(),
                         point_index=i, conditions=[(
                             "reference-temperature-out-of-window",
                             "the reference (fluke5700a) was at 27 C, outside "
                             "the 16.1 to 26.1 C window its own figures are "
                             "written for")])
                for i in range(2)]
        text = ca.format_markdown("run.csv", make_header(), rows)

        first = text.split("## Provenance")[0]
        assert "reference-temperature-out-of-window" in first
        assert "outside the temperature window its own figures" in first
        assert ca.CONDITION_WORDS["reference-temperature-out-of-window"]
        assert ca._FLAG_WORDS["reference-temperature-out-of-window"]

    def test_an_empty_budget_is_its_own_cause(self, tmp_path):
        row = make_row(error=1.0e-6, limit=14e-6,
                       budget=unc.UncertaintyBudget(), point_index=1)
        assert row.verdict is Verdict.INDETERMINATE

        causes = causes_of([row])

        assert causes["no_budget"] == 1
        assert causes["guard_band"] == 0, \
            "an absent budget is not 'the error landed in the guard band'"

    def test_the_report_states_both_causes_in_their_own_words(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        assert "TUR below the 4:1 floor" in text
        assert "temperature conditions not shown" in text
        # the meaning, not just the count
        assert "the **reference** is not good enough" in text
        assert "**specification's stated conditions**" in text
        assert ("A point blocked by two causes is counted under both"
                in text)

    def test_a_flag_the_policy_does_not_block_on_is_not_a_temperature_cause(
            self, tmp_path):
        """The real 3458A/5700A configuration: neither model declares an
        absolute window, so every row carries ``no-temperature-window`` and the
        ``gate`` policy blocks on none of it."""
        row = make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                 reference_ppm=35.0,
                       window=None, point_index=1, label="1 V dc",
                       nominal=1.0)
        assert row.verdict is Verdict.INDETERMINATE
        assert row.temperature_status.value == "no_window"
        assert row.carries_temperature_condition
        assert not row.blocked_by_temperature()

        causes = causes_of([row])

        assert causes["tur_floor"] == 1
        assert causes["temperature"] == 0
        statuses, blocked = ca.temperature_breakdown([row], "gate")
        assert blocked == 0 and sum(statuses.values()) == 1
        # ... and 'strict' does block on it
        assert ca.temperature_breakdown([row], "strict")[1] == 1

    def test_the_guard_band_bucket_survives_the_real_configuration(self,
                                                                   tmp_path):
        """**The bug this test exists for.**  Ten points under real conditions
        (``window=None``, so every row carries ``no-temperature-window``): the
        guard-band count read zero because the temperature *flag* was taken for
        a temperature *block*, and three genuine guard-band points vanished."""
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         window=None, point_index=i + 1) for i in range(4)]
        rows += [make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                 reference_ppm=35.0,
                          window=None, point_index=i + 5, nominal=1.0)
                 for i in range(3)]
        rows += [make_row(error=38e-6, limit=40e-6, budget=good_budget(),
                          window=None, point_index=i + 8) for i in range(3)]
        summary = summarize_run(rows)
        assert summary[Verdict.PASS] == 4
        assert summary[Verdict.INDETERMINATE] == 6

        causes = causes_of(rows)

        assert causes["tur_floor"] == 3
        assert causes["guard_band"] == 3, \
            "three genuine guard-band points, none of them temperature-blocked"
        assert causes["temperature"] == 0
        assert causes["no_budget"] == 0
        # every indeterminate point is accounted for
        assert (causes["tur_floor"] + causes["guard_band"]
                == summary[Verdict.INDETERMINATE])

    def test_a_single_guard_band_point_is_not_four_zeroes(self, tmp_path):
        """The worst shape of the bug: a cause table of all zeroes for a point
        whose own reason says it lies inside the guard band."""
        rows = [make_row(error=38e-6, limit=40e-6, budget=good_budget(),
                         window=None, point_index=1)]
        assert "lies inside the guard band" in rows[0].reason
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "| error inside the guard band | 1 |" in text
        assert "| TUR below the 4:1 floor | 0 |" in text
        assert "| temperature conditions not shown | 0 |" in text
        assert "1 point landed inside the guard band" in text

    def test_the_report_says_which_conditions_actually_blocked(self, tmp_path):
        rows = [make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                 reference_ppm=35.0,
                         window=None, point_index=1, nominal=1.0)]
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "**0** blocked a statement under the `gate` policy" in text
        assert "does **not** block a PASS under this policy" in text
        assert "| temperature conditions not shown | 0 |" in text
        # and neither the headline nor the top-of-report warning may claim
        # a temperature block that did not happen
        assert "temperature conditions were not shown to hold" not in text
        assert ("1 point carries a temperature condition the `gate` policy "
                "does not block on" in text)

    def test_a_real_temperature_block_is_still_called_one(self, tmp_path):
        path = write_run(tmp_path)          # point 4 has no recorded ambient
        header, rows = load_results(path)

        _, blocked = ca.temperature_breakdown(rows, header.temperature_policy)
        text = ca.format_markdown(path, header, rows)

        assert blocked == 1
        assert "temperature conditions were not shown to hold" in text
        assert "**1** blocked a statement under the `gate` policy" in text

    def test_the_guard_band_bucket_is_not_the_tur_bucket(self, tmp_path):
        """An error inside the guard band with a fine reference is the DUT's own."""
        row = make_row(error=38e-6, limit=40e-6, budget=good_budget(),
                       point_index=1)
        assert row.tur > 4.0, "the reference is fine here; the DUT is not"
        assert row.verdict is Verdict.INDETERMINATE

        causes = causes_of([row])

        assert causes["guard_band"] == 1
        assert causes["tur_floor"] == 0
        assert causes["temperature"] == 0

    def test_an_unrecognised_policy_is_reported_not_guessed(self, tmp_path):
        header = make_header(temperature_policy="whatever-comes-next")
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)

        policy, known = ca.policy_of(loaded_header)
        text = ca.format_markdown(path, loaded_header, rows)

        assert (policy, known) == ("gate", False)
        assert "unrecognised temperature policy" in text
        assert "**unrecognised by this build**" in text
        assert "taken under `gate`" in text


# ------------------------------------------------------------ the zero point

class TestZeroPoint:
    def test_ppm_is_blank_but_the_absolute_error_is_shown(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)
        zero = next(r for r in rows if r.label == "short (zero)")
        assert zero.is_zero_point
        assert zero.error_ppm is None

        text = ca.format_markdown(path, header, rows)
        line = next(l for l in text.splitlines()
                    if "| short (zero) |" in l)

        cells = [c.strip() for c in line.strip("|").split("|")]
        assert cells[9] == ca._q(zero.error, zero.unit), \
            "the absolute error must still be printed at a zero point"
        assert cells[10] == "", "ppm of a zero reading means nothing"
        assert cells[12] == "", "and neither does a ppm tolerance there"

    def test_a_nonzero_point_does_print_ppm(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)
        line = next(l for l in text.splitlines() if "| 10 V dc |" in l)

        cells = [c.strip() for c in line.strip("|").split("|")]
        assert re.match(r"^[-+]\d", cells[10]), cells


# ------------------------------------------------------------- output paths

class TestOutputPaths:
    def test_defaults_derive_from_the_csv_basename(self, tmp_path):
        path = write_run(tmp_path, name="2026-08-01-3458a-verify.csv")

        pdf, markdown = ca.output_paths(path)

        assert pdf == str(tmp_path / "2026-08-01-3458a-verify.pdf")
        assert markdown == str(tmp_path / "2026-08-01-3458a-verify-report.md")

    def test_output_overrides_the_pdf_only(self, tmp_path):
        path = write_run(tmp_path, name="run.csv")

        pdf, markdown = ca.output_paths(path, output=str(tmp_path / "x.pdf"))

        assert pdf == str(tmp_path / "x.pdf")
        assert markdown == str(tmp_path / "run-report.md")

    def test_the_cli_writes_both_default_paths(self, tmp_path, capsys):
        path = write_run(tmp_path, name="run.csv")

        assert analyze(path) == 0

        assert os.path.exists(str(tmp_path / "run-report.md"))
        assert os.path.exists(str(tmp_path / "run.pdf"))

    def test_dash_o_moves_the_pdf(self, tmp_path):
        path = write_run(tmp_path, name="run.csv")
        target = str(tmp_path / "elsewhere.pdf")

        assert analyze(path, "-o", target) == 0

        assert os.path.exists(target)
        assert not os.path.exists(str(tmp_path / "run.pdf"))

    def test_a_missing_file_is_an_error_not_a_traceback(self, tmp_path, capsys):
        assert ca.main([str(tmp_path / "nope.csv")]) == 2
        assert "no such file" in capsys.readouterr().err


# -------------------------------------------------------------- provenance

class TestProvenance:
    def test_both_instruments_and_the_decision_rule_are_stated(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        assert "FLUKE,5700A,5555001,1.4+2.0+1.4" in text   # the reference
        assert "HP3458A" in text                            # the DUT
        assert "5700A-03" in text                           # options
        assert "112" in text                                # CAL_DAYS?
        assert "CONF95" in text                             # CAL_CONF?
        assert "45" in text                                 # warm-up minutes
        assert "| CALNUM | — | 42 |" in text
        assert "6.5 h" in text                              # ACAL age
        assert "41.24" in text                              # internal temp
        assert "TUR floor: 4:1" in text
        assert "guard-band factor g: 1" in text
        assert "temperature policy: gate" in text
        assert "confidence: 95 %" in text
        assert "interval: 1y" in text
        assert "dut_role=meter" in text

    def test_ambient_carries_its_timestamp(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        assert "2026-08-01 10:59:00" in text
        assert "probe" in text

    def test_ambient_unknown_is_said_out_loud(self, tmp_path):
        header = make_header(ambient_c=None, humidity_pct=None,
                             ambient_timestamp=None, ambient_source="")
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         ambient_c=None)]
        path = write_run(tmp_path, rows, header)
        loaded_header, loaded_rows = load_results(path)
        assert not loaded_header.ambient_known

        text = ca.format_markdown(path, loaded_header, loaded_rows)

        assert "ambient: **unknown**" in text
        assert "The ambient temperature was never recorded" in text

    def test_a_flag_every_point_carries_is_one_line_not_a_table(self, tmp_path):
        """20 clean passes on real conditions must not print a 20-row table
        that says ``no-temperature-window`` twenty times."""
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         window=None, point_index=i + 1) for i in range(20)]
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "`no-temperature-window`: **all 20 points**" in text
        assert "neither model declares an absolute temperature window" in text
        assert text.count("no-temperature-window") == 1
        assert "No point carries a flag the others do not." in text
        assert "| # | label | flags |" not in text

    def test_the_flags_that_distinguish_a_point_keep_their_row(self, tmp_path):
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         window=None, point_index=i + 1) for i in range(3)]
        rows.append(make_row(error=1.0e-6, limit=14e-6, budget=coarse_budget(),
                 reference_ppm=35.0,
                             window=None, point_index=4, label="1 V dc",
                             nominal=1.0))
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "`no-temperature-window`: **all 4 points**" in text
        assert "| 4 | 1 V dc | tur-below-floor |" in text

    def test_the_load_current_derating_is_explained_and_quantified(self,
                                                                   tmp_path):
        """H06: the reader must be told why the calibrator's figure grew.

        The flag says *that* a derating was applied; the plan note the point
        already carries says by how much, and both have to reach the report.
        """
        note = ("read at 1 mA, below the 5700A's 8-11 mA full-spec load "
                "window for OHM19: add 14 ppm (0.000266 OHM) per the OM "
                "current derating")
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         window=None, point_index=i + 1, note=note,
                         flags=("load-current-derated",)) for i in range(2)]
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "`load-current-derated`: **all 2 points**" in text
        assert "full-spec load" in text
        assert ca._FLAG_WORDS["load-current-derated"]
        # ... and the number itself, from the point's own note.
        assert "0.000266 OHM" in text

    def test_ambient_unknown_is_not_claimed_when_the_points_have_one(self,
                                                                     tmp_path):
        """A header with no ambient but rows that carry one is not a run where
        "the ambient was never recorded" -- the report said both."""
        header = make_header(ambient_c=None, humidity_pct=None,
                             ambient_timestamp=None, ambient_source="")
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)
        assert not loaded_header.ambient_known
        assert any(row.ambient_c is not None for row in rows)

        text = ca.format_markdown(path, loaded_header, rows)

        assert "The ambient temperature was never recorded" not in text
        assert "per-point ambient: 23.147" in text

    def test_absent_scalars_do_not_grow_a_unit(self, tmp_path):
        header = make_header(source_cal_days=None, source_warm_minutes=None,
                             meter_temp_c=None, meter_acal_age_h=None,
                             meter_calnum=None, source_zero_days=None,
                             source_cal_interval_days=None)
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)

        text = ca.format_markdown(path, loaded_header, rows)

        for absurd in ("unknown d", "unknown min", "unknown °C", "unknown h"):
            assert absurd not in text
        assert "| since CAL (CAL_DAYS?) | unknown | — |" in text

    def test_a_limit_disagreement_is_surfaced(self, tmp_path):
        """The spec table against the unit's own UNCERT? -- a corrupt-constants
        detector that shows up nowhere else in an otherwise perfect run."""
        rows = mixed_rows()
        rows[0] = make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                           point_index=1, label="10 V dc", spec_limit=1e-3)
        assert rows[0].limit_disagree
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "disagree by more than 2x" in text
        assert "10 V dc" in text
        # ... and only the one point is named
        assert "1 point where the spec table" in text

    def test_no_disagreement_means_no_warning(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        assert not any(row.limit_disagree for row in rows)
        assert "disagree by more than" not in ca.format_markdown(path, header,
                                                                 rows)

    def test_the_run_duration_is_reported(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        assert "duration 21 min" in text

    def test_unknown_header_keys_survive_into_the_report(self, tmp_path):
        header = make_header(extra={"operator": "dl", "cable": "low-thermal"})
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)

        text = ca.format_markdown(path, loaded_header, rows)

        assert "operator: dl" in text
        assert "cable: low-thermal" in text


# ------------------------------------------------------- the spec-ratio warning

class TestSpecRatioWarning:
    def test_it_appears_when_the_reference_is_no_better_than_the_dut(self,
                                                                    tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        text = ca.format_markdown(path, header, rows)

        assert "## Read this first" in text
        assert "The reference is no better than the instrument under test" \
            in text
        assert "1 of 8 points" in text
        assert "mostly measuring fluke5700a rather than hp3458a" in text
        # ... and it is above the tables, not buried under them
        assert text.index("Read this first") < text.index("## Points")

    def test_it_is_absent_when_every_reference_is_better(self, tmp_path):
        rows = [r for r in mixed_rows() if (ca.spec_ratio(r) or 9) > 1.0]
        assert rows, "the fixture must still have some points"
        path = write_run(tmp_path, rows)
        header, loaded = load_results(path)

        text = ca.format_markdown(path, header, loaded)

        assert "The reference is no better than" not in text

    def test_the_ratio_is_parsed_out_of_the_plan_note(self):
        row = make_row(
            error=0.0, limit=1e-6, budget=good_budget(),
            note="spec ratio 0.00403 (hp3458a 4.03e-11 / fluke5700a 1e-08 A)")
        assert ca.spec_ratio(row) == pytest.approx(0.00403)

    def test_a_note_without_a_ratio_is_not_counted(self):
        row = make_row(error=0.0, limit=1e-6, budget=good_budget(),
                       note="operator note, no ratio here")
        assert ca.spec_ratio(row) is None
        ratios = ca.spec_ratio_summary([row])
        assert ratios.low == ()
        assert ratios.fraction == 0.0
        assert ratios.total == 1

    def test_the_threshold_is_configurable(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        # at 4:1 -- the calibration convention -- both the 0.4 and the 2.4
        # points are flagged
        summary = ca.spec_ratio_summary(rows, threshold=4.0)
        assert len(summary.low) == len(rows)

        text = ca.format_markdown(path, header, rows, spec_ratio_warn=4.0)
        assert f"{len(rows)} of {len(rows)} points" in text

    def test_the_summary_reports_the_worst_ratio(self, tmp_path):
        _, rows = load_results(write_run(tmp_path))

        summary = ca.spec_ratio_summary(rows)

        row, ratio = summary.worst
        assert ratio == pytest.approx(0.4)
        assert row.label == "1 V dc"
        assert summary.fraction == pytest.approx(1 / 8)


# ------------------------------------------------------------------ faults

class TestFaults:
    def test_codes_are_decoded_offline_and_grouped_by_assembly(self):
        header = make_header(extra={"faults": "3111,3104,808"})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [3111, 3104, 808]
        assert faults[0].assembly == "A15"
        assert faults[0].text == "HV +DC High Voltage Output Fault"
        assert faults[2].assembly is None, \
            "'DC dBm Not Allowed' names no assembly"
        assert faults[2].text == "DC dBm Not Allowed"

        grouped = ca.group_faults(faults)
        assert [key for key, _ in grouped] == ["A15", None]
        assert [f.code for f in grouped[0][1]] == [3111, 3104]
        assert [f.code for f in grouped[1][1]] == [808]

    def test_an_unknown_code_is_listed_rather_than_dropped(self):
        header = make_header(extra={"faults": "999999"})

        faults = ca.parse_faults(header)

        assert len(faults) == 1
        assert faults[0].code == 999999
        assert "unknown fault code" in faults[0].text

    def test_a_code_naming_two_assemblies_is_found_under_both(self):
        """23 of the 191 codes name a pair -- the 3117-3127 HV preamp block,
        which is exactly the A14 range this bench's unit fails in.  A
        single-assembly pattern dropped every one into "no assembly named"
        while still printing ``A14/A15:`` in the text."""
        header = make_header(extra={"faults": "3117"})

        faults = ca.parse_faults(header)

        assert faults[0].assemblies == ("A14", "A15")
        assert faults[0].text == "HV +DC Current Error Amp Fault"
        assert faults[0].prefix == "A14/A15"

        grouped = ca.group_faults(faults)
        assert [key for key, _ in grouped] == ["A14", "A15"]
        assert all(group[0].code == 3117 for _, group in grouped)

    def test_every_shipped_code_that_names_an_assembly_is_grouped(self):
        """Walk **all 191 shipped codes** and check every fault against an
        independent reading of its own text: whatever assembly the message
        names, anywhere in it, the fault must be grouped under.

        The earlier version of this test only looked at the *start* of the
        text, so it missed 3135 ``"Assembly A14 Not Responding"`` — a fault on
        the very board this bench's unit is expected to fail in, naming it in
        prose rather than as a prefix, quietly filed under "no assembly named".
        """
        table = ca.load_fault_table()
        header = make_header(
            extra={"faults": ",".join(str(code) for code in sorted(table))})
        # Deliberately *not* ca.fault_assemblies: an independent reading, or
        # this test only proves the parser agrees with itself.
        mentions = re.compile(r"\bA\d{1,3}\b")

        faults = ca.parse_faults(header)
        grouped = {name: {f.code for f in group}
                   for name, group in ca.group_faults(faults)}

        assert len(faults) == len(table)
        named = 0
        for fault in faults:
            expected = sorted(set(mentions.findall(table[fault.code])))
            assert sorted(fault.assemblies) == expected, \
                f"{fault.code}: {table[fault.code]!r}"
            for name in expected:
                named += 1
                assert fault.code in grouped.get(name, set()), \
                    f"{fault.code} names {name} but is not grouped under it"
        assert named, "the table must name some assemblies at all"
        # ... and nothing with an assembly in its text is in the unnamed group
        for code in grouped.get(None, set()):
            assert not mentions.search(table[code]), \
                f"{code} names an assembly but landed in 'no assembly named'"

    def test_a_prose_mention_is_grouped_and_keeps_its_sentence(self):
        """3135 is the code the prefix-only pattern lost."""
        header = make_header(extra={"faults": "3135"})

        faults = ca.parse_faults(header)

        assert faults[0].assemblies == ("A14",)
        assert faults[0].text == "Assembly A14 Not Responding", \
            "a prose mention is part of the sentence and must not be cut out"
        assert [key for key, _ in ca.group_faults(faults)] == ["A14"]

    def test_the_analyzer_and_the_console_agree_on_every_code(self):
        """One implementation, imported by both — the console's copy was the
        better one and the analyzer had reimplemented half of it."""
        from caldmm.apps import fluke5700a_console as console
        from caldmm.cal import calreport

        assert console.fault_assemblies is calreport.fault_assemblies
        assert ca.fault_assemblies is calreport.fault_assemblies
        for code, text in ca.load_fault_table().items():
            assert (console.fault_assemblies(text)
                    == calreport.fault_assemblies(text)), code

    def test_a_recorded_text_with_a_comma_survives_as_json(self):
        """21 of the 191 real messages contain a comma; the JSON form is the
        only one that can carry them, which is why it is the contract."""
        header = make_header(extra={
            "faults": '[[3122, "A14/A15: HV AC 1KHZ, Preamp Fault (lo)"], 3111]'})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [3122, 3111]
        assert faults[0].text == "HV AC 1KHZ, Preamp Fault (lo)"
        assert faults[0].assemblies == ("A14", "A15")
        assert faults[1].text == "HV +DC High Voltage Output Fault"

    def test_a_comma_in_a_bare_code_list_never_truncates_a_message(self):
        """The old ``code: text`` form split at the comma and dropped the tail.
        Bare codes take their text from the table, so nothing can be lost."""
        header = make_header(extra={"faults": "3122,3111"})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [3122, 3111]
        assert faults[0].text == "HV AC 1KHZ, Preamp Fault (lo)"

    def test_a_legacy_code_colon_text_entry_still_yields_its_code(self):
        header = make_header(extra={"faults": "3111: A15: whatever"})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [3111]
        assert faults[0].text == "HV +DC High Voltage Output Fault"

    def test_an_unparsable_entry_is_shown_not_dropped(self):
        header = make_header(extra={"faults": "3111,not-a-code"})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [3111, None]
        assert "unparsable fault entry" in faults[1].text
        assert faults[1].line().startswith("?: ")

    def test_malformed_json_is_reported_rather_than_raised(self):
        header = make_header(extra={"faults": '[3111, "unterminated'})

        faults = ca.parse_faults(header)

        assert [f.code for f in faults] == [None]
        assert "unparsable fault record" in faults[0].text

    def test_only_one_header_key_is_honoured(self):
        header = make_header(extra={"fault_codes": "3111",
                                    "source_faults": "3104"})
        assert ca.parse_faults(header) == []

    def test_no_faults_means_no_section(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        assert ca.parse_faults(header) == []
        assert "## Instrument faults" not in ca.format_markdown(path, header,
                                                                rows)

    def test_the_report_groups_them_under_their_assembly(self, tmp_path):
        header = make_header(extra={"faults": "3111,3104,808"})
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)

        text = ca.format_markdown(path, loaded_header, rows)

        assert "## Instrument faults" in text
        assert "### A15 (2 faults)" in text
        assert "### no assembly named (1 fault)" in text
        assert "`3111` — HV +DC High Voltage Output Fault" in text
        assert "reported 3 faults" in text          # and it is in the warnings

    def test_a_shared_fault_says_so_under_each_assembly(self, tmp_path):
        header = make_header(extra={"faults": "3117"})
        path = write_run(tmp_path, mixed_rows(), header)
        loaded_header, rows = load_results(path)

        text = ca.format_markdown(path, loaded_header, rows)

        assert "### A14 (1 fault)" in text
        assert "### A15 (1 fault)" in text
        assert "*(also names A15)*" in text
        assert "*(also names A14)*" in text
        assert "no assembly named" not in text

    def test_the_offline_table_is_the_drivers_table(self):
        table = ca.load_fault_table()
        assert table[3111] == "A15: HV +DC High Voltage Output Fault"
        assert len(table) > 150


# ---------------------------------------------------------------- compare

class TestCompare:
    def _before_after(self):
        before = [
            make_row(error=90e-6, limit=40e-6, budget=good_budget(),
                     point_index=1, label="10 V dc"),
            make_row(error=1.0e-6, limit=40e-6, budget=good_budget(),
                     point_index=2, label="1 V dc", nominal=1.0,
                     reference_value=1.0, mean=1.000001),
        ]
        after = [
            # adjusted: the failure is gone
            make_row(error=2.0e-6, limit=40e-6, budget=good_budget(),
                     point_index=1, label="10 V dc"),
            # and this one drifted out
            make_row(error=95e-6, limit=40e-6, budget=good_budget(),
                     point_index=2, label="1 V dc", nominal=1.0,
                     reference_value=1.0, mean=1.000095),
        ]
        return before, after

    def test_matched_points_carry_their_delta_and_transition(self):
        before, after = self._before_after()

        matched, only_before, only_after = ca.compare_runs(before, after)

        assert only_before == [] and only_after == []
        assert len(matched) == 2
        first, second = matched
        assert first.error_delta == pytest.approx(2.0e-6 - 90e-6)
        assert first.transition == "FAIL -> PASS"
        assert first.improved and not first.regressed
        assert second.transition == "PASS -> FAIL"
        assert second.regressed and not second.improved
        assert second.ratio_delta == pytest.approx((95e-6 - 1.0e-6) / 40e-6)

    def test_points_are_matched_on_identity_not_index(self):
        before, after = self._before_after()
        # the same two points, renumbered and re-ordered by a resume
        shuffled = [
            make_row(error=95e-6, limit=40e-6, budget=good_budget(),
                     point_index=7, label="1 V dc", nominal=1.0,
                     reference_value=1.0, mean=1.000095),
            make_row(error=2.0e-6, limit=40e-6, budget=good_budget(),
                     point_index=8, label="10 V dc"),
        ]

        matched, only_before, only_after = ca.compare_runs(before, shuffled)

        assert len(matched) == 2
        assert only_before == [] and only_after == []
        transitions = {d.after.label: d.transition for d in matched}
        assert transitions == {"10 V dc": "FAIL -> PASS",
                               "1 V dc": "PASS -> FAIL"}

    def test_unmatched_points_are_reported_not_dropped(self):
        before, after = self._before_after()
        extra = make_row(error=0.0, limit=40e-6, budget=good_budget(),
                         point_index=3, label="100 V dc", nominal=100.0,
                         reference_value=100.0, mean=100.0)

        matched, only_before, only_after = ca.compare_runs(before,
                                                           after + [extra])

        assert len(matched) == 2
        assert only_before == []
        assert [r.label for r in only_after] == ["100 V dc"]

    def test_the_cli_writes_a_comparison_section(self, tmp_path):
        before, after = self._before_after()
        before_path = write_run(tmp_path, before, name="before.csv")
        after_path = write_run(tmp_path, after, name="after.csv")

        assert analyze(after_path, "--compare", before_path) == 0

        text = open(str(tmp_path / "after-report.md"), encoding="utf-8").read()
        assert "## Comparison with `before.csv`" in text
        assert "FAIL -> PASS" in text
        assert "PASS -> FAIL" in text
        assert "2 changed verdict (1 no longer FAIL, 1 newly FAIL)" in text


# -------------------------------------------------------------------- PDF

def _pdf_pages(path: str) -> int:
    """Page count straight out of the file, without a PDF library."""
    data = open(path, "rb").read()
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


class TestPdf:
    def test_it_is_written_and_has_the_pages_it_says_it_has(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)
        out = str(tmp_path / "run.pdf")

        pages = ca.render_pdf(path, header, rows, out)

        assert pages >= 6, "front matter, five plots and the tables"
        assert _pdf_pages(out) == pages
        assert os.path.getsize(out) > 20000

    def test_full_budgets_adds_pages(self, tmp_path):
        path = write_run(tmp_path)
        header, rows = load_results(path)

        few = ca.render_pdf(path, header, rows, str(tmp_path / "few.pdf"))
        many = ca.render_pdf(path, header, rows, str(tmp_path / "many.pdf"),
                             full_budgets=True)

        assert many > few
        assert _pdf_pages(str(tmp_path / "many.pdf")) == many

    def test_compare_adds_its_own_page(self, tmp_path):
        before = [make_row(error=90e-6, limit=40e-6, budget=good_budget(),
                           point_index=1, label="10 V dc")]
        after = [make_row(error=2.0e-6, limit=40e-6, budget=good_budget(),
                          point_index=1, label="10 V dc")]
        before_path = write_run(tmp_path, before, name="before.csv")
        after_path = write_run(tmp_path, after, name="after.csv")
        header, rows = load_results(after_path)

        plain = ca.render_pdf(after_path, header, rows,
                              str(tmp_path / "plain.pdf"))
        with_compare = ca.render_pdf(after_path, header, rows,
                                     str(tmp_path / "cmp.pdf"),
                                     compare=(before_path, before))

        assert with_compare > plain

    def test_a_budget_block_reaches_the_page_unfolded(self, tmp_path):
        """``format_budget`` hands back one multi-line string; folding that at
        the page width instead of at its own newlines turns it into confetti."""
        path = write_run(tmp_path)
        _, rows = load_results(path)

        text_lines = ca._align_tables(ca._md_budgets(rows))
        page_lines = ca._wrap(text_lines, ca._PDF_PORTRAIT[1])

        assert any(l.startswith("combined standard uncertainty u_c = ")
                   for l in page_lines)
        assert any(l.startswith("expanded uncertainty U = k*u_c = ")
                   for l in page_lines)
        assert sum(1 for l in page_lines
                   if l.startswith("Uncertainty budget [")) == len(rows)

    def test_no_pdf_skips_it(self, tmp_path):
        path = write_run(tmp_path, name="run.csv")

        assert analyze(path, "--no-pdf") == 0

        assert os.path.exists(str(tmp_path / "run-report.md"))
        assert not os.path.exists(str(tmp_path / "run.pdf"))


# ------------------------------------------------------------- degenerate runs

class TestDegenerateRuns:
    def test_one_point_produces_a_report(self, tmp_path):
        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget())]
        path = write_run(tmp_path, rows, name="one.csv")

        assert analyze(path) == 0

        text = open(str(tmp_path / "one-report.md"), encoding="utf-8").read()
        assert "1 point: 1 pass" in text
        assert "### Point 1 —" in text
        assert _pdf_pages(str(tmp_path / "one.pdf")) >= 3

    def test_no_data_rows_produces_a_report(self, tmp_path):
        path = write_run(tmp_path, [], name="empty.csv")
        header, rows = load_results(path)
        assert rows == []

        assert analyze(path) == 0

        text = open(str(tmp_path / "empty-report.md"), encoding="utf-8").read()
        assert "0 points" in text
        assert "The run recorded no points." in text
        # the provenance still has to be there -- an empty run is still a run
        assert "FLUKE,5700A,5555001" in text
        assert _pdf_pages(str(tmp_path / "empty.pdf")) >= 1

    def test_a_run_with_no_limits_at_all_still_reports(self, tmp_path):
        rows = [make_row(error=4e-6, limit=None, budget=good_budget(),
                         point_index=i + 1) for i in range(3)]
        path = write_run(tmp_path, rows, name="nospec.csv")

        assert analyze(path) == 0

        text = open(str(tmp_path / "nospec-report.md"),
                    encoding="utf-8").read()
        assert "| NO_SPEC | 3 | 100 % |" in text


# ------------------------------------------------------------- the CLI itself

class TestConsoleOutput:
    def test_the_headline_names_both_indeterminate_causes(self, tmp_path,
                                                          capsys):
        path = write_run(tmp_path)

        analyze(path)

        out = capsys.readouterr().out
        assert "below the TUR floor (reference not good enough)" in out
        assert "outside the specification's stated conditions" in out
        assert "of them temperature" in out
        assert "WARNING: the reference is no better than the DUT" in out
        assert "report:" in out and "plots :" in out

    def test_print_dumps_the_report(self, tmp_path, capsys):
        path = write_run(tmp_path)

        analyze(path, "--no-pdf", "--print")

        assert "## Provenance" in capsys.readouterr().out

    def test_the_parser_documents_the_defaults(self):
        parser = ca.build_parser()
        args = parser.parse_args(["x.csv"])
        assert args.output is None and args.markdown is None
        assert args.spec_ratio_warn == ca.SPEC_RATIO_WARN
        assert args.full_budgets is False


class TestExitStatusIsAboutTheReport:
    """0 means "a report was written", not "the DUT passed".

    ``cal_verify`` exits 1 on a run that found a FAIL; this exits 0 on a file
    full of them.  The two answer different questions -- one performed the
    comparison, the other renders a file it did not produce and is re-run over
    the same file routinely -- and the divergence is stated in the module
    docstring and in ``--help``.  Pinned because it is the sort of difference a
    later "consistency" fix silently removes.
    """

    def _all_fail(self, tmp_path):
        rows = [make_row(error=900e-6, limit=40e-6, budget=good_budget(),
                         point_index=index, label=f"10 V dc #{index}")
                for index in (1, 2, 3)]
        assert all(row.verdict is Verdict.FAIL for row in rows)
        return write_run(tmp_path, rows=rows)

    def test_a_file_of_nothing_but_fails_still_exits_zero(self, tmp_path,
                                                          capsys):
        path = self._all_fail(tmp_path)
        assert analyze(path, "--no-pdf") == 0
        assert "0 pass, 3 fail" in capsys.readouterr().out

    def test_only_a_missing_file_is_nonzero(self, tmp_path):
        assert ca.main([str(tmp_path / "nope.csv"), "--no-pdf"]) == 2

    def test_help_states_the_difference_from_cal_verify(self):
        text = ca.build_parser().format_help()
        assert "exit status: 0 the report was written" in text
        assert "caldmm-cal-verify" in text


# ------------------------------------------------- as-found/as-left linkage

class TestAutoCompare:
    """An as-left run that names its counterpart is compared without asking."""

    def _pair(self, tmp_path):
        before = write_run(tmp_path, name="before.csv")
        after = write_run(tmp_path, name="after.csv",
                          header=make_header(run_kind="as-left",
                                             counterpart=before))
        return before, after

    def test_an_as_left_run_compares_against_its_counterpart(self, tmp_path,
                                                             capsys):
        before, after = self._pair(tmp_path)
        assert analyze(after, "--no-pdf") == 0
        report = open(after.replace(".csv", "-report.md")).read()
        assert "## Comparison with `before.csv`" in report
        assert "counterpart" in capsys.readouterr().out

    def test_an_explicit_compare_wins(self, tmp_path):
        _, after = self._pair(tmp_path)
        other = write_run(tmp_path, name="other.csv")
        assert analyze(after, "--no-pdf", "--compare", other) == 0
        report = open(after.replace(".csv", "-report.md")).read()
        assert "## Comparison with `other.csv`" in report
        assert "## Comparison with `before.csv`" not in report

    def test_a_missing_counterpart_is_a_warning_not_a_crash(self, tmp_path,
                                                            capsys):
        after = write_run(tmp_path, name="after.csv",
                          header=make_header(run_kind="as-left",
                                             counterpart="gone.csv"))
        assert analyze(after, "--no-pdf") == 0
        assert "gone.csv" in capsys.readouterr().err
        report = open(after.replace(".csv", "-report.md")).read()
        assert "## Comparison with" not in report

    def test_an_as_found_counterpart_is_never_swapped_in(self, tmp_path):
        """The counterpart of an as-found run is the *later* run; comparing
        against it as "before" would invert every delta."""
        before = write_run(tmp_path, name="later.csv")
        found = write_run(tmp_path, name="found.csv",
                          header=make_header(run_kind="as-found",
                                             counterpart=before))
        assert analyze(found, "--no-pdf") == 0
        report = open(found.replace(".csv", "-report.md")).read()
        assert "## Comparison with" not in report

    def test_the_counterpart_is_found_by_basename_next_to_the_file(
            self, tmp_path):
        """The pair travels together; the recorded path may be stale."""
        before = write_run(tmp_path, name="before.csv")
        header = make_header(run_kind="as-left",
                             counterpart="/somewhere/else/before.csv")
        after = write_run(tmp_path, name="after.csv", header=header)
        found, warning = ca.find_counterpart(after, header)
        assert warning is None
        assert found == before

    def test_linkage_reaches_the_provenance(self, tmp_path):
        _, after = self._pair(tmp_path)
        assert analyze(after, "--no-pdf") == 0
        report = open(after.replace(".csv", "-report.md")).read()
        assert "run kind: **as-left**" in report
        assert "counterpart run:" in report


# --------------------------------------------------- machine-readable diff

class TestCompareDiffCsv:
    """--compare also writes a diff CSV a spreadsheet can check."""

    def test_written_next_to_the_after_file(self, tmp_path):
        import csv as _csv

        before = write_run(tmp_path, name="before.csv")
        after = write_run(tmp_path, name="after.csv")
        assert analyze(after, "--no-pdf", "--compare", before) == 0
        diff_path = str(tmp_path / "after-diff.csv")
        assert os.path.exists(diff_path)
        with open(diff_path, newline="") as handle:
            reader = _csv.DictReader(handle)
            assert reader.fieldnames == ca.COMPARE_CSV_COLUMNS
            rows = list(reader)
        assert rows
        assert all(row["status"] == "both" for row in rows)
        # Identical runs: every delta is exactly zero, written as a repr.
        assert all(row["delta_error"] in ("", "0.0") for row in rows)

    def test_unmatched_points_are_kept_with_a_status(self, tmp_path):
        import csv as _csv

        all_rows = mixed_rows()
        # Identity-unique edits: the 100 kHz ac point exists only in "after",
        # the zero point only in "before".  (Rows 1 and 4 share an identity
        # on purpose -- both are 10 V dc -- so slicing at the front would
        # remove nothing as far as point_key is concerned.)
        before = write_run(tmp_path, rows=all_rows[:-1], name="before.csv")
        after = write_run(tmp_path,
                          rows=[row for row in all_rows
                                if row.label != "short (zero)"],
                          name="after.csv")
        analyze(after, "--no-pdf", "--compare", before)
        with open(str(tmp_path / "after-diff.csv"), newline="") as handle:
            statuses = [row["status"] for row in _csv.DictReader(handle)]
        assert "added" in statuses          # the last point, only in "after"
        assert "removed" in statuses        # the first, only in "before"
        assert "both" in statuses

    def test_the_numbers_are_reprs_of_the_loaded_values(self, tmp_path):
        before = write_run(tmp_path, name="before.csv")
        after = write_run(tmp_path, name="after.csv")
        _, before_rows = load_results(before)
        _, after_rows = load_results(after)
        matched, only_b, only_a = ca.compare_runs(before_rows, after_rows)
        rows = ca.compare_csv_rows(matched, only_b, only_a)
        first = next(row for row in rows if row["before_error"])
        assert float(first["before_error"]) == float(first["after_error"])
        # repr round-trips exactly; a %g rendering would not.
        delta = next(d for d in matched
                     if repr(float(d.before.error)) == first["before_error"])
        assert first["before_error"] == repr(float(delta.before.error))

    def test_no_compare_means_no_diff_file(self, tmp_path):
        path = write_run(tmp_path)
        analyze(path, "--no-pdf")
        assert not os.path.exists(str(tmp_path / "run-diff.csv"))


# ------------------------------------------------------------- temperature

class TestTemperature:
    """The run's thermal picture: provenance lines and the plot page."""

    def _rows(self, meter_temp_c=38.9):
        return [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         point_index=index, label=f"10 V dc #{index}",
                         meter_temp_c=meter_temp_c)
                for index in (1, 2, 3)]

    def test_temperatures_reach_the_provenance(self, tmp_path):
        path = write_run(tmp_path, rows=self._rows(),
                         header=make_header(source_cal_temp_c=23.42))
        assert analyze(path, "--no-pdf") == 0
        report = open(str(tmp_path / "run-report.md")).read()
        assert "CAL_TEMP?" in report and "23.42" in report
        assert "meter internal temperature: 38.900 to 38.900" in report

    def test_no_temperatures_no_claims(self, tmp_path):
        path = write_run(tmp_path)
        analyze(path, "--no-pdf")
        report = open(str(tmp_path / "run-report.md")).read()
        assert "meter internal temperature" not in report

    def test_the_plot_renders_with_any_series_present(self, tmp_path):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages

        with PdfPages(str(tmp_path / "t.pdf")) as pdf:
            pages = ca._plot_temperature(
                pdf, plt, make_header(source_cal_temp_c=23.42), self._rows())
        assert pages == 1

    def test_the_plot_is_skipped_without_per_point_data(self, tmp_path):
        """The source's one CAL_TEMP? number is not a plot."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages

        rows = [make_row(error=1.2e-6, limit=40e-6, budget=good_budget(),
                         ambient_c=None, humidity_pct=None)]
        with PdfPages(str(tmp_path / "t.pdf")) as pdf:
            pages = ca._plot_temperature(
                pdf, plt, make_header(source_cal_temp_c=23.42), rows)
        assert pages == 0


# --------------------------------------------- the calibration-cycle report

class TestAdjustLogMode:
    """--adjust-log binds the session record and the verification pair."""

    def _log(self, tmp_path):
        log = tmp_path / "3458a-adjust.log"
        log.write_text("caldmm-cal-verify --adjust session record\n"
                       "step 1  CAL 10.0000012 held back (dry run)\n")
        return str(log)

    def test_a_log_alone_is_a_text_only_report(self, tmp_path, capsys):
        log = self._log(tmp_path)
        assert ca.main(["--adjust-log", log, "--no-pdf"]) == 0
        report = open(str(tmp_path / "3458a-adjust-report.md")).read()
        assert "text-only" in report
        assert "step 1  CAL 10.0000012" in report      # the record, verbatim
        assert "## Comparison with" not in report
        assert "text-only" in capsys.readouterr().out

    def test_the_pdf_is_written_next_to_the_log(self, tmp_path):
        log = self._log(tmp_path)
        assert ca.main(["--adjust-log", log]) == 0
        assert os.path.exists(str(tmp_path / "3458a-adjust.pdf"))

    def test_the_pair_adds_the_comparison_and_the_diff(self, tmp_path):
        log = self._log(tmp_path)
        before = write_run(tmp_path, name="before.csv")
        after = write_run(tmp_path, name="after.csv")
        assert ca.main(["--adjust-log", log, "--as-found", before,
                        "--as-left", after, "--no-pdf"]) == 0
        report = open(str(tmp_path / "3458a-adjust-report.md")).read()
        assert "## Comparison with `before.csv`" in report
        assert "## Adjustment session record" in report
        assert "text-only" not in report
        assert os.path.exists(str(tmp_path / "3458a-adjust-diff.csv"))

    def test_the_as_found_is_discovered_from_the_as_left_header(self,
                                                                tmp_path,
                                                                capsys):
        log = self._log(tmp_path)
        before = write_run(tmp_path, name="before.csv")
        after = write_run(tmp_path, name="after.csv",
                          header=make_header(run_kind="as-left",
                                             counterpart=before))
        assert ca.main(["--adjust-log", log, "--as-left", after,
                        "--no-pdf"]) == 0
        report = open(str(tmp_path / "3458a-adjust-report.md")).read()
        assert "## Comparison with `before.csv`" in report
        assert "named by the as-left header" in capsys.readouterr().out

    def test_a_positional_csv_is_refused(self, tmp_path, capsys):
        path = write_run(tmp_path)
        assert ca.main([path, "--adjust-log", self._log(tmp_path)]) == 2
        assert "--as-found" in capsys.readouterr().err

    def test_as_found_without_adjust_log_is_refused(self, tmp_path, capsys):
        path = write_run(tmp_path)
        assert ca.main([path, "--as-found", path]) == 2
        assert "--adjust-log" in capsys.readouterr().err

    def test_a_missing_log_is_an_error(self, tmp_path):
        assert ca.main(["--adjust-log", str(tmp_path / "nope.log")]) == 2

    def test_a_missing_named_run_is_an_error(self, tmp_path):
        log = self._log(tmp_path)
        assert ca.main(["--adjust-log", log,
                        "--as-left", str(tmp_path / "nope.csv")]) == 2


class TestCoverageAndTestCard:
    """The two sections that answer "was all of the 3458A verified?"."""

    @staticmethod
    def _rows():
        # CM 4-2 #6 (10 V on the 10 V range, card 89.2 uV) within the card;
        # CM 4-2 #3 (1 V on the 10 V range, card 11.1 uV) outside it.
        good = make_row(error=2e-6, limit=83e-6, budget=good_budget(),
                        point_index=0, label="[CM 4-2 #6] DCV 10 V")
        bad = make_row(error=20e-6, limit=11e-6, budget=good_budget(),
                       nominal=1.0, point_index=1,
                       label="[CM 4-2 #3] DCV 1 V")
        return [good, bad]

    def test_coverage_lists_every_range_and_the_untouched_ones(self):
        rows = self._rows()
        coverage = ca.range_coverage(make_header(), rows)
        touched = [(q.name, fs, sum(c.values())) for q, _w, fs, c in coverage
                   if sum(c.values())]
        assert touched == [("DCV", 10.0, 2)]
        names = {q.name for q, _w, _fs, _c in coverage}
        assert {"DCV", "ACV", "DCI", "ACI", "RES"} <= names
        text = "\n".join(ca._md_coverage(make_header(), rows))
        assert "## Range coverage" in text
        assert "Not exercised:" in text and "ACI 2w" in text

    def test_the_test_card_judges_against_both_columns(self):
        text = "\n".join(ca._md_test_card(make_header(), self._rows()))
        assert "## Manufacturer test card" in text
        lines = {line.split("|")[1].strip(): line
                 for line in text.splitlines() if line.startswith("| CM ")}
        assert "| yes |" in lines["CM 4-2 #6"]
        assert "**no**" in lines["CM 4-2 #3"]
        assert "Not measured in this run:" in text

    def test_a_source_dut_run_has_neither_section(self):
        header = make_header(dut_role="source")
        assert ca._md_coverage(header, self._rows()) == []
        assert ca._md_test_card(header, self._rows()) == []

    def test_both_sections_are_in_the_report(self):
        text = ca.format_markdown("run.csv", make_header(),
                                           self._rows())
        assert "## Range coverage" in text
        assert "## Manufacturer test card" in text

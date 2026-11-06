"""Executing a plan: drive the roles, build a budget, decide, yield rows.

This is where a :class:`~caldmm.cal.plan.TestPlan` becomes data.  For every
point the engine asks the source how it is (:meth:`~caldmm.cal.roles.SourceRole.health`),
sources the point, configures the meter **from the readback**, waits, reads,
builds a GUM budget out of :mod:`caldmm.cal.uncertainty`'s builders, asks
:func:`caldmm.cal.verdict.evaluate` for a conformity decision and yields one
:class:`~caldmm.cal.result.ResultRow`.  It decides nothing itself: every
number in a row was produced by a module whose job that number is.

Why a generator
---------------

A run is on the order of 200 points over two hours, operator-attended.  A
function returning a list would hand a crash, a dropped ssh session or a Ctrl-C
at point 180 the power to destroy the whole record, and would leave the runner
nothing to show while it waited.  Yielding one row per point lets the runner
write and flush each row as it is produced (:func:`caldmm.cal.result.open_results`
does exactly that), print progress, and -- because a generator's ``finally``
runs on ``close()`` -- guarantees that abandoning the iteration **de-energizes
the source**.

Import hygiene
--------------

``roles``, ``plan``, ``specs``, ``quantities``, ``uncertainty``, ``verdict``,
``result`` and the stdlib.  **Never a driver, and never**
:mod:`caldmm.cal.adapters`: concrete roles are handed *in*, so the engine is
testable with fake roles and importable on a machine with no linux-gpib, no
pyserial and no pyvisa.  A subprocess test with a real ``sys.meta_path``
blocker pins that.

The four things this module exists to get right
-----------------------------------------------

**The meter is configured from the nominal, before the source is energized.**
A range chosen from a live output is a range chosen *after* the output has
already been driven into whatever the meter happened to be on: every dc-current
range of a generated source-DUT plan starts at 0 A, which selects the 3458A's
100 nA range and its 545 kohm shunt, and the 10 % point that follows demands
12 V through it against a 10 V compliance limit -- the calibrator trips, on
every range (H03).  So the order is configure, then apply, and the meter is
re-targeted from the readback only where that would change the *range*: the
5700A's resistance function sources a characterized value up to 500 ppm off the
nominal, and the readback -- not the request -- is the reference for the error,
but it is not usually a different range.  Neither point is re-validated:
:meth:`~caldmm.cal.specs.RangeSpec.holds` has no overrange allowance, so 7 of
the 17 meter-DUT resistance points would fail as "the 10 OHM range does not
read 10.005 OHM" while real hardware has some 20 % of headroom.

**Above 22 V and 220 mA the range is verified, not assumed.**  The meter's own
``range_readback()`` is compared with the range the adapter says it applied
*before* ``OPERATE``, and a mismatch aborts with the output still off; where a
meter cannot be fixed on a range at all, a 10 %-of-target pre-step keeps its
autoranger's step to one decade.  Above 220 mA that pre-step is unconditional,
because what it catches there -- a wrong shunt, a wrong terminal -- manual
ranging cannot.  And the meter is **never ranged down under a live output**:
a range step up while a lower level is live is how the meter gets from a proof
to its point, but a step down -- a probe's range, or a plan point below the one
before it -- is taken with the source in standby, because nothing between two
points of a group de-energizes on its own.  Global Constraints §9.

**Ambient temperature is a parameter, not a guess.**  :func:`caldmm.cal.verdict.evaluate`
refuses to state conformity *or* non-conformity where a model declares a
temperature window and no ambient was recorded, and both the 34401A and the
1281 declare 18-28 C -- so without a probe every one of their points is
``INDETERMINATE`` *by design*, and the fix is to pass a probe, never to loosen
``temperature_policy``.  The probe is its own parameter because it cannot be
detected: :class:`~caldmm.cal.roles.EnvironmentProbe` and
:class:`~caldmm.cal.roles.MeterRole` both expose ``read``, so
``isinstance(meter, EnvironmentProbe)`` is ``True`` for every meter.  A window
may be **relative** -- the 5700A's every table is written for "+/- 5 C from
calibration temperature" -- and then its centre comes from the instrument
(``CAL_TEMP?``, read once per run) rather than from the file; the same window
is checked for the reference, whose figures are no more valid outside it than
the DUT's.

**Which side supplies the tolerance limit is the whole of bidirectionality.**
With ``dut_role='meter'`` the limit comes from the meter's ``ModelSpec`` and
the source's own ``UNCERT?`` is the reference component of the budget; with
``dut_role='source'`` the limit comes from the source's ``UNCERT?`` (with its
spec table as a cross-check) and the meter's ``ModelSpec`` becomes the
reference component.  The error is ``meter mean - source actual`` in **both**
directions; nothing else swaps.

**Nothing is trusted that can be proved.**  Before the first point, at every
wiring group, on every resume and whenever the source changes range, a small
safe output -- 10 mV, 100 uA, or the ohms standard itself -- is sourced and the
meter is asked what it sees.  A lead in the wrong socket reads as nothing
*there*, at 10 mV, instead of as a catastrophe at 1000 V twenty minutes later.
And after every reading, an error above ten times the point's own tolerance
abandons the run: a DUT is allowed to be out of specification, but ten
tolerances is a connection, and every later point would measure the same
mistake.  Both write their row and stop the generator with the output in
standby, and no flag or option turns either off (Global Constraints §8).

**Groups are where the operator re-cables.**  At every boundary the source is
programmed to zero and put in standby *before* the callback fires -- standby
alone leaves the last setpoint selected, and the manual's procedure before
anything is touched is ``0 V ENTER`` *and* standby -- and the runner can start
from a group index, so a run interrupted at point 140 resumes at the boundary
rather than from the beginning.
"""

from __future__ import annotations

import inspect
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Iterator, Mapping, Sequence

from .plan import PointGroup, TestPlan
from .quantities import Connection, Quantity
from .result import ResultRow, interval_days
from .roles import (
    UNKNOWN_ENVIRONMENT,
    Environment,
    EnvironmentProbe,
    MeterConfig,
    MeterRole,
    Reading,
    SourceHealth,
    SourceRole,
    SourceState,
)
from .specs import (
    FunctionSpec,
    LimitResult,
    ModelSpec,
    PairRole,
    RangeSpec,
    SpecError,
    Tempco,
    Traceability,
    evaluate_limit,
    load_model,
    select_band,
    select_range,
)
from .uncertainty import (
    UncertaintyBudget,
    UncertaintyComponent,
    from_calibrator_uncert,
    from_confidence,
    from_expanded,
    from_half_width,
    loading_correction,
    resolution_component,
    thermal_emf_component,
    type_a,
)
from .verdict import (
    FLAG_AMBIENT_UNKNOWN,
    ConformityResult,
    DecisionRule,
    Verdict,
    classify_temperature,
    evaluate,
)

__all__ = [
    "EngineError",
    "SourceFaultError",
    "SafetyAbortError",
    "DEFAULT_SETTLE_MARGIN_S",
    "DEFAULT_ENVIRONMENT_INTERVAL_S",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_LOADING_RELATIVE_UNCERTAINTY",
    "AC_LOADING_RELATIVE_UNCERTAINTY",
    "LOADING_MAX_FREQUENCY_HZ",
    "DEFAULT_THERMAL_EMF_V",
    "WARMUP_MINUTES",
    "ZERO_INTERVAL_DAYS",
    "HAZARD_VOLTAGE_V",
    "HAZARD_CURRENT_A",
    "PRESTEP_FRACTION",
    "PROBE_TOLERANCE",
    "GROSS_ERROR_FACTOR",
    "WIRING_PROOF_FRACTION",
    "WIRING_PROOF_MIN_V",
    "WIRING_PROOF_MIN_A",
    "PROOF_MIN_RANGE_FRACTION",
    "THERMAL_EMF_LOW_OHMS",
    "FLAG_METER_RANGE_MISMATCH",
    "FLAG_PRESTEP_FAILED",
    "FLAG_WIRING_PROOF_FAILED",
    "FLAG_WIRING_PROOF_UNREADABLE",
    "FLAG_WIRING_PROOF_NOT_RESOLVABLE",
    "FLAG_STANDBY_NOT_CONFIRMED",
    "FLAG_GROSS_ERROR_ABORT",
    "FLAG_GROSS_CHECK_NOT_APPLICABLE",
    "FLAG_POINT_FAILED",
    "FLAG_NO_VALID_SAMPLES",
    "FLAG_OVERLOAD_SAMPLES",
    "FLAG_NO_TYPE_A",
    "FLAG_SOURCE_NOT_SETTLED",
    "FLAG_SAMPLING_STOPPED_EARLY",
    "FLAG_SAMPLING_NOT_CONVERGED",
    "EARLY_STOP_MIN_SAMPLES",
    "EARLY_STOP_SEM_FRACTION",
    "EARLY_STOP_TREND_FRACTION",
    "EARLY_STOP_MARGIN_FRACTION",
    "EARLY_STOP_NEAR_LIMIT_FRACTION",
    "sampling_converged",
    "FLAG_SOURCE_NOT_OPERATE",
    "FLAG_SOURCE_NOT_WARM",
    "FLAG_SOURCE_ZEROS_DUE",
    "FLAG_SOURCE_TRIPPED_MID_POINT",
    "FLAG_NO_REFERENCE_UNCERTAINTY",
    "FLAG_REFERENCE_FROM_SPEC_TABLE",
    "FLAG_LIMIT_FROM_SPEC_TABLE",
    "FLAG_REFERENCE_CONFIDENCE_UNKNOWN",
    "FLAG_TEMPCO_NOT_EVALUATED",
    "FLAG_DUT_TYPICAL_ONLY",
    "FLAG_LEAD_RESISTANCE_CORRECTED",
    "FLAG_LEAD_RESISTANCE_UNKNOWN",
    "FLAG_REFERENCE_OUT_OF_WINDOW",
    "FLAG_THERMAL_EMF_DEFAULT",
    "FLAG_THERMAL_EMF_NOT_CONVERTED",
    "FLAG_LOADING_UNCORRECTED_HF",
    "FLAG_LOADING_CORRECTED",
    "FLAG_AMBIENT_READ_FAILED",
    "FLAG_RANGE_MISMATCH",
    "FLAG_LOAD_DERATED",
    "GroupBoundary",
    "run_plan",
]


class EngineError(RuntimeError):
    """A run that cannot proceed: a bad argument, or a run-level failure."""


class SafetyAbortError(EngineError):
    """A safety rule stopped the run: the output may not go to this point.

    Raised for the checks that exist to protect the hardware and the operator
    rather than the record -- the meter's range read-back before a hazardous
    output (Global Constraints §9), the 10 %-of-target pre-step, the wiring
    proof and the gross-error abort (§8).  Every one of them writes its row
    first, so the file says what happened, and then stops the generator with
    the source in standby.

    :attr:`flag` is the row flag the abort wrote, so a runner can name the
    rule without parsing the message.
    """

    def __init__(self, message: str, *, flag: str = "",
                 group_index: int = 0, point_index: int = 0):
        super().__init__(message)
        self.flag = flag
        self.group_index = int(group_index)
        self.point_index = int(point_index)


class SourceFaultError(EngineError):
    """The source tripped or has a queued fault; the run was abandoned.

    Carries where it happened so a runner can prompt the operator and resume
    with ``start_group=exc.group_index`` once the cause is cleared.
    """

    def __init__(self, message: str, *, group_index: int = 0,
                 point_index: int = 0, health: SourceHealth | None = None):
        super().__init__(message)
        self.group_index = int(group_index)
        self.point_index = int(point_index)
        self.health = health


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: Added to whichever of the two settling times is longer.  Both figures are
#: nominal ("3 s" in a manual, "operational allowance" in a model file) and the
#: cost of half a second per point is 100 s in a 200-point run; the cost of
#: reading one point too early is a wrong number that looks like a real one.
DEFAULT_SETTLE_MARGIN_S = 0.5

# -- early termination of sampling (2026-11-01) ------------------------------
#
# A point is read in a block of ``min_samples`` (after the plan's discards),
# then one sample at a time up to the plan's ``samples``, stopping as soon as
# :func:`sampling_converged` accepts the whole block.
#
# **What "converged" means.**  The repeatability of the block is already in
# the budget as a Type A term with ``n - 1`` degrees of freedom, so a short
# block is never *hidden* -- it is paid for in ``U``.  What early stopping
# must not do is (a) let that Type A term grow to where it matters, or (b)
# stop on a reading that is still moving, whose short mean is biased in a way
# no budget term covers.  Both are judged against ``u_B``: the standard
# uncertainty of the reference plus the meter's resolution, the Type B floor
# that more samples cannot lower.  (a) ``sem <= 0.3 u_B`` grows ``u_c`` by at
# most 4.4 %.  (b) the least-squares slope of the block, projected over the
# samples not yet taken, would move the full block's mean by ``|slope| (N -
# n) / 2``; that must stay under ``0.2 u_B``.  Near the DUT's limit both
# fractions tighten to 0.1, because there the verdict turns on the mean.
#
# Replayed on every block of a full five-function verification (RES, DCV,
# ACV, DCI, ACI; 172 points): no verdict changed, no mean moved by more than
# 0.12 U, and 77 of the run's sampling minutes were saved -- against 52 for
# the first rule (2 % of the table limit, linear), which refused every point
# within 80 % of its limit and every zero point quantized at one count.
# 675 V / 750 V ac (a 0.6 U ramp over 10 samples) still runs the full count.
# A "settled far from the limit" shortcut was tried and rejected: it stopped
# those ramps at 3 and moved a drifting 0 A zero by 1.0 U -- right verdict,
# wrong number.

#: Samples always taken before early termination may stop a point.
EARLY_STOP_MIN_SAMPLES = 3
#: The standard deviation of the mean must be at most this share of ``u_B``.
EARLY_STOP_SEM_FRACTION = 0.3
#: The projected shift of the full block's mean, ``|slope| (N - n) / 2``, as
#: a share of ``u_B``.  This is what catches self-heating and slow settling,
#: which a spread test cannot see.
EARLY_STOP_TREND_FRACTION = 0.2
#: An error beyond this share of the DUT's limit is near it ...
EARLY_STOP_MARGIN_FRACTION = 0.8
#: ... and there both fractions above tighten to this one.
EARLY_STOP_NEAR_LIMIT_FRACTION = 0.1


def _block_slope(values: Sequence[float]) -> float:
    """Least-squares slope of *values* against sample index, per sample."""
    n = len(values)
    centre = (n - 1) / 2.0
    mean = sum(values) / n
    sxx = sum((i - centre) ** 2 for i in range(n))
    return sum((i - centre) * (v - mean) for i, v in enumerate(values)) / sxx


def sampling_converged(samples: Sequence[float], *, u_b: float | None,
                       planned: int | None = None,
                       error: float | None = None,
                       dut_limit: float | None = None,
                       min_samples: int = EARLY_STOP_MIN_SAMPLES,
                       ) -> tuple[bool, str]:
    """Whether a block of samples may stop early.  **No I/O.**

    Returns ``(ok, why)``; *why* names the first condition that failed, or
    says the block converged.  *u_b* is the Type B standard uncertainty the
    block is judged against (reference plus resolution), in the reading's
    unit; ``None``, zero, negative or non-finite means there is nothing to
    judge the spread against, and the block never converges.  *planned* is
    the plan's full count, over which a trend is projected; ``None`` or no
    samples left projects nothing.  The decision never depends on the mean
    except through *error*, which only ever makes it **stricter**.
    """
    n = len(samples)
    if n < max(int(min_samples), 2):
        return False, f"only {n} sample(s)"
    try:
        u_b = float(u_b) if u_b is not None else math.nan
    except (TypeError, ValueError):
        u_b = math.nan
    if not math.isfinite(u_b) or u_b <= 0.0:
        return False, "no limit to judge the spread against"
    values = [float(v) for v in samples]
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    sem = sd / math.sqrt(n)
    sem_fraction, trend_fraction = (EARLY_STOP_SEM_FRACTION,
                                    EARLY_STOP_TREND_FRACTION)
    near = ""
    if error is not None and dut_limit:
        try:
            is_near = (abs(float(error))
                       > EARLY_STOP_MARGIN_FRACTION * abs(float(dut_limit)))
        except (TypeError, ValueError):
            is_near = True
        if is_near:
            sem_fraction = trend_fraction = EARLY_STOP_NEAR_LIMIT_FRACTION
            near = " (near the limit)"
    if sem > sem_fraction * u_b:
        return False, (f"spread{near}: sem {sem:.3g} > "
                       f"{sem_fraction:g} x u_B {u_b:.3g}")
    left = max(int(planned) - n, 0) if planned else 0
    projected = abs(_block_slope(values)) * left / 2.0
    if projected > trend_fraction * u_b:
        return False, (f"trend{near}: projected shift {projected:.3g} over "
                       f"{left} more > {trend_fraction:g} x u_B {u_b:.3g}")
    return True, f"converged at n={n}"

#: How long a probe reading may be reused before the probe is asked again.
#: Ambient goes into the *verdict*, so it may not be read once and assumed for
#: two hours; but a probe backed by a logger or a database costs a round trip
#: per read, so it may not be read 200 times either.  Five minutes is far shorter than
#: any real bench thermal time constant and costs ~24 reads in a two-hour run.
DEFAULT_ENVIRONMENT_INTERVAL_S = 300.0

#: Consecutive per-point failures after which the run is abandoned.  One
#: failed point is a point; five in a row is a pulled GPIB cable, and grinding
#: on would produce 190 more rows of the same nothing.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

#: How well the two impedances of the loading correction are known, as a
#: fraction of the correction.  10 % is the right order for a datasheet input
#: impedance quoted as "10 Mohm +/-1 %" against a stated 50 ohm output.
DEFAULT_LOADING_RELATIVE_UNCERTAINTY = 0.1

#: The same fraction for **ac** points, where the manuals are looser: the
#: 3458A's ac input is "1 MOhm +/- 15% with <140 pF" on its 10 mV-1 V ranges
#: (``3458a-datasheet.txt:968-972``) and the 34401A's is "1 M +/-2% in
#: parallel with 100 pF".  The stated tolerance is the honest figure to carry
#: (L83).
AC_LOADING_RELATIVE_UNCERTAINTY = 0.15

#: Above this frequency no loading correction is applied at all (L78).  The
#: model is a resistive divider and the model files carry no input
#: capacitance, but on the 5700A's 50 ohm mV ac ranges the reactive term is
#: ~1000 ppm at 1 MHz against a ~50 ppm resistive correction -- 20x the thing
#: being corrected.  Correcting for the small term while ignoring the large
#: one would put a number in the row that is not the error.
LOADING_MAX_FREQUENCY_HZ = 100e3

#: The capacitance a 50 ohm calibrator output drives above
#: :data:`LOADING_MAX_FREQUENCY_HZ`, for the **uncertainty** that stands in
#: for the correction not applied there: the 3458A's ac input is "1 MOhm with
#: < 140 pF" (data sheet p.25/p.28) and about a metre of test lead adds about
#: as much again.  The output divides as ``1/sqrt(1 + (2 pi f R C)^2)``, which
#: on the 5700A's 50 ohm mV ranges is ~1000 ppm low at 1 MHz.
HF_LOAD_CAPACITANCE_F = 140e-12 + 150e-12

#: Bound on the round-trip resistance of a pair of test leads, in ohms, when
#: the operator has not measured it (``lead_resistance_ohm``).  A 2-wire
#: resistance reading includes it, so where it is material against the
#: point's tolerance an unknown value makes the point INDETERMINATE.
LEAD_RESISTANCE_BOUND_OHM = 0.1
#: Relative standard-uncertainty basis of a *measured* lead resistance: the
#: half-width, as a fraction of the value, of the rectangular term the
#: correction carries (leads warm and flex between measurement and use).
LEAD_RESISTANCE_RELATIVE_UNCERTAINTY = 0.2
#: Floor of that half-width, in ohms.
LEAD_RESISTANCE_FLOOR_OHM = 0.005

#: The bound assumed for thermal EMF in the measurement loop, in volts, when
#: the operator states none.  One microvolt is what a pair of clean copper
#: spade lugs in a settled bench does; the 5700A's own artifact calibration
#: requires low-thermal leads for exactly this reason
#: (``5700a-service-manual-2007.txt`` lines 2975-2980, PDF p.74).  It is the
#: dominant term at a zero setpoint -- the dc 220 mV range's 1-year floor is
#: 0.8 uV -- and a budget that omits it there is not GUM-complete (JCGM 100
#: 4.3.1).  Pass ``thermal_emf=0`` to state that there is none.
DEFAULT_THERMAL_EMF_V = 1e-6

#: Resistance at or below which a loop EMF is a first-order ohms error: 1 uV
#: at the 1 mA a 34401A or 3458A sources into its 100 ohm range is 1 mOhm,
#: against a 4 mOhm range term (L84).  Above it the ratio is negligible.
THERMAL_EMF_LOW_OHMS = 100.0

#: Minutes of warm-up below which the source's figures are not yet claimed.
#: The 5700A wants twice its off-time to a 30-minute maximum; the engine has no
#: way to know the off-time, so it flags against the maximum.
WARMUP_MINUTES = 30

#: Days after which the 5700A's dc zeros are due.
ZERO_INTERVAL_DAYS = 30

#: Output above which a point is treated as **hazardous**, in volts.  The same
#: 22 V the 5700A uses for the outputs a queued fault silently inhibits
#: (OM 1996, ``*Analog output`` states): above it the meter's configured range
#: is read back from the instrument and compared before ``OPERATE``.
HAZARD_VOLTAGE_V = 22.0

#: The same threshold in amps.  Above 220 mA a wrong shunt or a wrong terminal
#: is a fire, not a bad reading, so the pre-step below is unconditional there.
HAZARD_CURRENT_A = 0.220

#: The pre-step's fraction of the target.  A tenth is one decade -- enough to
#: prove the loop is what the plan thinks it is, small enough that a wrong
#: loop survives it.
PRESTEP_FRACTION = 0.10

#: How far a **probe** reading -- the wiring proof of §8 and the pre-step of
#: §9 -- may be from the level that was sourced, as a fraction of it.  One
#: constant for both, because they are one check asked at two moments: is what
#: the meter sees what the calibrator says it is putting out?  Deliberately
#: loose: this is a wiring check, not a measurement, and it has to pass on an
#: instrument that is out of specification.
PROBE_TOLERANCE = 0.10

#: How far a reading may be from what was sourced before the run is abandoned:
#: **ten times the point's own tolerance** (or ten times the reference's
#: expanded uncertainty where the point has no usable limit).  A DUT is
#: allowed to be out of specification -- that is the finding a verification
#: exists to make -- but an error of ten tolerances is not a DUT, it is a lead
#: on the wrong terminal, a shorted sense pair or the wrong function, and
#: every further point of the run is then measuring the same mistake.
GROSS_ERROR_FACTOR = 10.0

#: The wiring proof: a small safe output, checked loosely, before anything
#: else is energized.  ``max(10 mV, 0.1 % of the range's full scale)`` for
#: volts and ``max(100 uA, 0.1 % of full scale)`` for amps -- low enough to be
#: harmless into any mis-wiring, high enough to be unambiguous on any range of
#: any of these meters.  For resistance the standard itself is the safe level:
#: an ohms output sources no hazard, and reading the standard *is* the proof.
WIRING_PROOF_FRACTION = 0.001
WIRING_PROOF_MIN_V = 10e-3
WIRING_PROOF_MIN_A = 100e-6

#: The controller's amendment of §8, 2026-08-22: a proof is also **never
#: smaller than the meter can read**.  The level above is a property of the
#: *calibrator's* range and says nothing about the instrument that has to
#: judge it: 0.1 % of the 5700A's 2.2 A ac range is 2.2 mA, which is 0.22 % of
#: the smallest ac-current range a 34401A has, where its table states nothing
#: at all and its 0.04 %-of-range floor alone is twice the probe.  A +/-10 %
#: check there fails a correctly wired bench.  So the level is raised to at
#: least this fraction of the meter's lowest usable range for the function --
#: or that range's own ``min_reading_frac`` where the manufacturer states a
#: larger one -- and stays strictly below the hazard thresholds.  Where even
#: that cannot be met the proof is **skipped and flagged**, never faked.
PROOF_MIN_RANGE_FRACTION = 0.05

#: The point could not be measured; the row records what was known and no
#: verdict.  See :func:`run_plan` for the failure rule.
FLAG_POINT_FAILED = "point-failed"
#: Every sample was an invalid-reading sentinel.  There is no mean.
FLAG_NO_VALID_SAMPLES = "no-valid-samples"
#: At least one sentinel was excluded from the mean.  A plausible mean with a
#: non-zero overload count is the shape of a point that half happened.
FLAG_OVERLOAD_SAMPLES = "overload-samples"
#: Fewer than two surviving samples, so the budget carries no Type A term.
FLAG_NO_TYPE_A = "no-repeat-uncertainty"
#: The source reported that it had not settled when it was read back.
FLAG_SOURCE_NOT_SETTLED = "source-not-settled"
#: Early termination (``min_samples``) stopped sampling before the plan's
#: count because the block had converged -- see :func:`sampling_converged`.
FLAG_SAMPLING_STOPPED_EARLY = "sampling-stopped-early"
#: Early termination was enabled but the block never converged, so the
#: plan's full count was taken: a trend (heating, settling), spread, an
#: overload, or no reference uncertainty to judge the spread against.
FLAG_SAMPLING_NOT_CONVERGED = "sampling-not-converged"
#: The source reported that it was not in operate -- the meter read a
#: de-energized output, which looks like a 100 % error rather than a mistake.
FLAG_SOURCE_NOT_OPERATE = "source-not-in-operate"
FLAG_SOURCE_NOT_WARM = "source-not-warmed-up"
FLAG_SOURCE_ZEROS_DUE = "source-zeros-due"
#: The source tripped (or queued a fault) between ``apply`` and the reading.
#: The samples were taken through an output that was on its way to standby, so
#: the row is recorded and no verdict is reached.
FLAG_SOURCE_TRIPPED_MID_POINT = "source-tripped-mid-point"
#: Neither the reference's own uncertainty nor its specification table says
#: anything here, so no statement of conformity was attempted.
FLAG_NO_REFERENCE_UNCERTAINTY = "no-reference-uncertainty"
#: The reference's own figure was unavailable and its transcribed
#: specification table was used instead, as a rectangular bound.
FLAG_REFERENCE_FROM_SPEC_TABLE = "reference-uncertainty-from-spec-table"
#: The DUT is the source and it reported no ``UNCERT?`` for this output, so the
#: tolerance came from its transcribed table instead of from the instrument.
FLAG_LIMIT_FROM_SPEC_TABLE = "tolerance-limit-from-spec-table"
#: The reference stated an uncertainty but not the confidence level it holds
#: at, so it was taken as a rectangular bound rather than assuming 95 %.
FLAG_REFERENCE_CONFIDENCE_UNKNOWN = "reference-confidence-unknown"
#: The reference declares a temperature coefficient but the departure from its
#: reference temperature is unknown, so no tempco term could be built.
FLAG_TEMPCO_NOT_EVALUATED = "tempco-not-evaluated"
#: The **reference** was outside the temperature window its own figures are
#: written for -- for a calibrator, ``CAL_TEMP? +/- 5 C``.  A condition of the
#: claim, exactly as the DUT's own window is.
FLAG_REFERENCE_OUT_OF_WINDOW = "reference-temperature-out-of-window"
#: The thermal-EMF term is the **declared default** bound, not a figure the
#: operator measured.  A budget that omits its largest component at a zero or
#: a short is not GUM-complete; one that assumes a bound must say so.
FLAG_THERMAL_EMF_DEFAULT = "thermal-emf-default"
#: A thermal-EMF bound is in force but this point's quantity needs a
#: conversion the model files do not carry: ohms would need the meter's test
#: current, a current zero its shunt.  Recorded rather than silently omitted.
FLAG_THERMAL_EMF_NOT_CONVERTED = "thermal-emf-not-converted"
#: The finite-input-impedance correction was **not** applied: above ~100 kHz
#: the loading is dominated by a capacitance the model files do not carry, so
#: a purely resistive correction would be a number with no physics under it.
FLAG_LOADING_UNCORRECTED_HF = "loading-uncorrected-above-100-khz"
#: The DUT's figure at this point is "typical", not a specification
#: (:attr:`~caldmm.cal.specs.RangeSpec.typical`): an unmet condition, so the
#: point is INDETERMINATE whichever way the error went.
FLAG_DUT_TYPICAL_ONLY = "dut-typical-only"
#: A 2-wire resistance reading was corrected by the stated test-lead
#: resistance, which carries its own uncertainty term.
FLAG_LEAD_RESISTANCE_CORRECTED = "lead-resistance-corrected"
#: A 2-wire resistance point at which the unmeasured test-lead resistance is
#: material against the tolerance: an unmet condition (INDETERMINATE).
FLAG_LEAD_RESISTANCE_UNKNOWN = "lead-resistance-unknown"
#: The reading was corrected for finite meter input impedance, so ``Mean`` and
#: ``Error`` differ by more than ``Mean - Reference``.
FLAG_LOADING_CORRECTED = "loading-corrected"
#: The environment probe raised; the ambient is unknown for these points.
FLAG_AMBIENT_READ_FAILED = "ambient-read-failed"
#: The meter's own range read-back disagreed with the range the adapter says
#: it applied, at a point above the hazard thresholds.  Nothing was energized.
FLAG_METER_RANGE_MISMATCH = "meter-range-mismatch"
#: The 10 %-of-target pre-step did not read back the level that was sourced:
#: the loop is not what the plan thinks it is.  The full value was never
#: sourced.
FLAG_PRESTEP_FAILED = "prestep-failed"
#: The safe-level wiring proof did not read back what was sourced.  The
#: wiring is not what the plan assumes and the run was stopped before the
#: planned level was ever applied to it.
FLAG_WIRING_PROOF_FAILED = "wiring-proof-failed"
#: The safe-level wiring proof could not be **read**: the source stepped down
#: and the meter was asked, and the meter raised instead of returning a value
#: (a timeout, a driver refusal, a lost interface).  No proof was made either
#: way, so nothing is implied about the wiring -- which is the whole reason
#: this is not :data:`FLAG_WIRING_PROOF_FAILED`.  That flag is a statement
#: about the bench, and sending an operator to re-check leads that were never
#: in question is a worse outcome than the abort itself.  The planned level
#: was still never applied, and the run stops the same way.
FLAG_WIRING_PROOF_UNREADABLE = "wiring-proof-unreadable"
#: §8's proof was **not taken** here: no level this meter can read to +/-10 %
#: is also below the hazard thresholds, so any proof would either be judged
#: inside the meter's own floor (aborting a bench that is wired correctly) or
#: be a hazardous output taken before anything had been proved.  The points
#: were measured; they simply do not carry a proof, and the row says so rather
#: than the run either lying about one or refusing to start.
FLAG_WIRING_PROOF_NOT_RESOLVABLE = "wiring-proof-not-resolvable"
#: The source would not confirm standby before the meter was to be re-ranged
#: (§9, M47), so the run stopped **with the previous point's output possibly
#: still on the terminals**.  It is the one abort whose consequence is on the
#: bench rather than in the record, which is why it names itself on the row
#: instead of arriving only as the shutdown path's message: nothing was
#: re-ranged and nothing further was sourced, but the leads are not known to
#: be safe.
FLAG_STANDBY_NOT_CONFIRMED = "standby-not-confirmed"
#: A reading was off by more than :data:`GROSS_ERROR_FACTOR` times the point's
#: tolerance.  That is not a DUT out of specification, it is a wrong
#: connection, and every later point would measure the same mistake.
FLAG_GROSS_ERROR_ABORT = "gross-error-abort"
#: The gross-error check could not be made: the point has neither a usable
#: tolerance nor a reference uncertainty, so there is no scale to be ten times
#: of.  Recorded rather than silently skipped -- and *not* an abort, because
#: the alternative would be to judge grossness by Type A noise.
FLAG_GROSS_CHECK_NOT_APPLICABLE = "gross-check-not-applicable"
#: The source reported a different range from the one the plan pinned, so the
#: point was measured on one range and planned against another.  The row's
#: figures follow the **readback**; the flag says the plan disagreed.
FLAG_RANGE_MISMATCH = "source-range-mismatch"
#: The point was read outside the calibrator's full-spec load-current window,
#: so the derating the plan computed from the manual's own equations
#: (``PlanPoint.load_adder``, H06) was applied to the calibrator's figure: to
#: its budget term where it is the reference, to its tolerance where it is the
#: instrument under test.  The point's note carries the arithmetic.
FLAG_LOAD_DERATED = "load-current-derated"


# --------------------------------------------------------------------------
# Group boundaries
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GroupBoundary:
    """Handed to ``on_group`` when a new wiring group is about to start.

    The source is already in **standby** when this fires: the boundary is where
    the operator physically re-cables, and prompting for that with the output
    live would be the one genuinely dangerous thing this subsystem could do.

    :attr:`index` is the value to pass back as ``start_group`` to resume the
    run from here, which is what makes a two-hour run interruptible.
    """

    index: int
    total: int
    group: PointGroup
    #: Index of this group's first point in ``plan.points``.
    start: int
    #: Points this *run* never attempted because it began at ``start_group``.
    #: Constant for the whole run -- it is a property of where the run was
    #: resumed from, not of where it has got to.  A complete run reports 0 at
    #: every boundary; a run resumed at the third group reports the number of
    #: points in the first two, at every boundary, so a progress line can say
    #: "point 3 of 4 (2 skipped)" without the count growing as the run goes on.
    skipped: int = 0
    #: Offset of the first point of this group that the run will actually
    #: measure.  Non-zero only for the group a run was resumed *into* -- see
    #: ``start_point`` in :func:`run_plan`.  The operator is still prompted to
    #: re-cable: the wiring is a property of the group, not of where in it the
    #: run happens to begin.
    offset: int = 0
    #: Why the source could not be programmed to zero before this prompt, if
    #: it could not.  Empty is the normal case (and the case of a source that
    #: offers no ``zero_output``).  The output is in **standby** either way;
    #: what a non-empty value means is that the last setpoint is still
    #: selected, so an accidental ``OPR`` would re-apply it -- which is
    #: exactly what the OM's "press RESET, or 0 V ENTER" exists to prevent.
    zero_error: str = ""

    @property
    def quantity(self) -> Quantity:
        return self.group.quantity

    @property
    def connection(self):
        return self.group.connection

    @property
    def points(self) -> int:
        return len(self.group)

    @property
    def remaining(self) -> int:
        """Points this run will measure in this group."""
        return max(0, len(self.group) - self.offset)

    @property
    def label(self) -> str:
        return self.group.label

    def describe(self) -> str:
        text = (f"group {self.index + 1}/{self.total}: {self.label}, "
                f"{self.points} point{'' if self.points == 1 else 's'} "
                f"from #{self.start}")
        if self.offset:
            text += (f" (resuming at #{self.start + self.offset}, "
                     f"{self.remaining} left)")
        if self.zero_error:
            text += (f" -- WARNING: the output is in standby but could not be "
                     f"programmed to zero ({self.zero_error}), so the last "
                     f"setpoint is still selected")
        return text


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    """Timezone-aware wall clock, as every timestamp in this project is."""
    return datetime.now().astimezone()


def _merge_readings(first: Reading, more: Reading) -> Reading:
    """One reading from two consecutive blocks of the same point.

    Samples append in order, so the row's evidence is the whole block as
    taken; overloads add; ``discarded`` stays the first block's (only the
    first block discards); resolution and unit are the configured range's.
    """
    samples = tuple(first.samples) + tuple(more.samples)
    n = len(samples)
    mean = sum(samples) / n if n else math.nan
    stdev = (math.sqrt(sum((v - mean) ** 2 for v in samples) / (n - 1))
             if n > 1 else math.nan)
    return Reading(samples=samples, mean=mean, stdev=stdev, n=n,
                   discarded=first.discarded,
                   overloads=first.overloads + more.overloads,
                   resolution=first.resolution, unit=first.unit)


def _resolve_spec(given: ModelSpec | str | None, role: Any,
                  model_id: str) -> ModelSpec:
    """The :class:`~caldmm.cal.specs.ModelSpec` to judge one side against.

    An explicit argument wins; then the role's own ``spec`` when it carries one
    (every adapter does); then the plan's model id, loaded from file.  The
    role's is preferred over the plan's because it is the specification the
    role is actually applying -- ranges, knobs and all -- and judging against a
    different file than the one that configured the instrument is the kind of
    disagreement nothing downstream could detect.
    """
    if given is not None:
        return load_model(given) if isinstance(given, str) else given
    from_role = getattr(role, "spec", None)
    if isinstance(from_role, ModelSpec):
        return from_role
    return load_model(model_id)


def _accepts_discard(read: Callable) -> bool:
    """Whether a meter's ``read`` takes the ``discard`` keyword.

    :class:`~caldmm.cal.roles.MeterRole` declares ``read(self, n)`` and every
    adapter implements ``read(self, n, *, discard=0)``.  A plan that asks for
    discards and a meter that cannot honour them is caught **before** anything
    is energized rather than becoming 200 rows whose first sample was taken
    during a range change.
    """
    try:
        signature = inspect.signature(read)
    except (TypeError, ValueError):        # pragma: no cover - exotic callable
        return True
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "discard" and parameter.kind in (
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD):
            return True
    return False


def _range_by_fs(function: FunctionSpec, fs: float) -> RangeSpec | None:
    for candidate in function.ranges:
        if math.isclose(candidate.fs, float(fs), rel_tol=1e-9, abs_tol=0.0):
            return candidate
    return None


def _range_by_arg(function: FunctionSpec, range_arg: float) -> RangeSpec | None:
    for candidate in function.ranges:
        arg = candidate.range_arg if candidate.range_arg is not None \
            else candidate.fs
        if math.isclose(arg, float(range_arg), rel_tol=1e-9, abs_tol=0.0):
            return candidate
    return None


def _range_by_id(function: FunctionSpec, range_id: str) -> RangeSpec | None:
    for candidate in function.ranges:
        if candidate.range_id == range_id:
            return candidate
    return None


def _mode_for(function: FunctionSpec | None, point) -> str | None:
    """The point's spec mode, but only where *this* model declares it.

    A plan's ``mode`` is pinned from the **meter's** vocabulary (``sync``,
    ``ana``, ``filt_slow``), so handing it to the source's specification is a
    :class:`~caldmm.cal.specs.SpecError` about a mode nobody asked for.
    """
    wanted = getattr(point, "mode", None)
    if not wanted or function is None:
        return None
    return wanted if function.mode(wanted) is not None else None


def _settle_of(function: FunctionSpec | None,
               range_spec: RangeSpec | None) -> float:
    return max(float(getattr(function, "settle_s", None) or 0.0),
               float(getattr(range_spec, "settle_s", None) or 0.0))


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(float(value))


def _load_adder(point) -> float:
    """The plan's load-current derating for this point, or ``0.0`` (H06).

    :attr:`~caldmm.cal.plan.PlanPoint.load_adder` is **absolute and already in
    the quantity's own unit** -- the plan converted the manual's ppm (or the
    short's milliohms) against the value at planning time -- so nothing is
    converted here and nothing is scaled by the reading.

    Structural, like every other read of a point: the engine's seam is
    :class:`~caldmm.cal.roles.PointLike`, which promises four attributes, and
    a bare :class:`~caldmm.cal.roles.Point` carries no derating at all.
    ``None``, zero and a non-finite value all mean "nothing to add".
    """
    value = getattr(point, "load_adder", None)
    if value is None or not _finite(value):
        return 0.0
    return abs(float(value))


def _name_of(point) -> str:
    """A point's label for a message, from whatever the point actually has.

    :class:`~caldmm.cal.roles.PointLike` promises four attributes and nothing
    else -- ``describe()`` is a :class:`~caldmm.cal.plan.PlanPoint` method --
    so a message that reached for it would raise on the very structural type
    the roles exist to accept.
    """
    label = str(getattr(point, "label", "") or "")
    if label:
        return label
    describe = getattr(point, "describe", None)
    if callable(describe):
        return str(describe())
    return (f"{Quantity(point.quantity).name} "
            f"{float(point.value):g} {Quantity(point.quantity).unit}")


def _interval_id_for_days(spec: ModelSpec, days: float | None) -> str | None:
    """The interval id of *spec* that names *days*, if one does.

    Exact only: a unit set to a 180-day interval against a file that declares
    24 h, 90 d and 1 y is not "close to 90 days", and quietly rounding a
    calibration interval is how a certificate ends up naming a column that was
    never read.
    """
    if days is None:
        return None
    for interval in spec.intervals:
        named = interval_days(interval)
        if named is not None and math.isclose(named, float(days),
                                              rel_tol=1e-9, abs_tol=1e-9):
            return interval
    return None


def planned_source_range(source_spec: ModelSpec, point) -> RangeSpec | None:
    """The source range *point* will be produced on, *before* applying it.

    By identifier where the plan pinned one -- the calibrator's resistances
    are a discrete catalogue and resolving 19003.8 ohm by value lands in the
    wrong entry -- else by the nominal.  Used to decide when the source has
    changed range, which is one of the moments §8 requires a fresh wiring
    proof.

    Module level so that a plan's proofs can be checked offline, with no
    instrument and no executor: :func:`proof_level` is the other half.
    """
    function = source_spec.function(Quantity(point.quantity))
    if function is None:
        return None
    range_id = getattr(point, "range_id", None)
    if range_id:
        found = _range_by_id(function, range_id)
        if found is not None:
            return found
    return select_range(function, abs(float(point.value)))


def readable_meter_ranges(function: FunctionSpec,
                          frequency_hz: float = 0.0) -> list[RangeSpec]:
    """*function*'s ranges that can be **read** here, smallest first.

    Banded ranges are filtered by the frequency: a 34401A's 750 V ac range
    stops at 100 kHz, and a range whose table does not reach the tone is not a
    range a proof can be judged on.  With no frequency in hand (every dc
    function, and an ac point that never named one) nothing is filtered, which
    is the historical behaviour.

    Discrete "ranges" -- a calibrator's resistance catalogue -- are not a
    ladder and are excluded; on a meter there are none.
    """
    usable = []
    for range_spec in function.ranges:
        if range_spec.discrete or range_spec.fs <= 0.0:
            continue
        if range_spec.bands and frequency_hz \
                and select_band(range_spec, frequency_hz) is None:
            continue
        usable.append(range_spec)
    return sorted(usable, key=lambda r: r.fs)


def meter_range_floor(range_spec: RangeSpec, mode_id: str | None = None
                      ) -> float:
    """The smallest reading on *range_spec* a +/-10 % check can be made at.

    :data:`PROOF_MIN_RANGE_FRACTION` of full scale, or the manufacturer's own
    ``min_reading_frac`` where it is larger -- "the datasheet says nothing
    below this fraction of range" is exactly the condition that makes a probe
    unjudgeable, and on the 34401A's ac-current ranges it is 5 % rather than
    the 1-2 % a decade ladder would give.
    """
    stated = range_spec.min_reading_frac_for(mode_id) or 0.0
    return abs(float(range_spec.fs)) * max(PROOF_MIN_RANGE_FRACTION,
                                           float(stated))


def resolvable_proof_level(level: float, unit: str, *,
                           meter_spec: ModelSpec | None,
                           quantity: Quantity | None = None,
                           frequency_hz: float = 0.0,
                           mode_id: str | None = None
                           ) -> "tuple[float | None, str]":
    """Raise a §8 level to what the meter can read, or refuse it.

    The controller's amendment of 2026-08-22, and the one place it is
    computed: the engine and every adjustment procedure come through here, so
    a proof cannot mean one level in a verification and another in the
    adjustment that follows it.

    Returns ``(level, "")`` for a level that can be proved, and
    ``(None, reason)`` for one that cannot -- *reason* being the plain
    language a report prints.  Ohms are returned untouched: the standard is
    the level, there is nothing to raise it to, and a calibrator's ohms output
    is not a hazard.

    With no *meter_spec* the level is returned as it came, which is what a
    caller that does not know its meter can honestly do.
    """
    token = str(unit).strip().upper()
    if meter_spec is None or token not in ("V", "A"):
        return float(level), ""
    quantity = Quantity(quantity) if quantity is not None else None
    function = None if quantity is None else meter_spec.function(quantity)
    if function is None:
        return float(level), ""
    ranges = readable_meter_ranges(function, float(frequency_hz or 0.0))
    what = f"{meter_spec.model_id} {quantity.name}"
    if not ranges:
        return None, (f"{what} has no range that reads at "
                      f"{float(frequency_hz):g} Hz, so no wiring proof can be "
                      f"judged on it")
    threshold = HAZARD_VOLTAGE_V if token == "V" else HAZARD_CURRENT_A
    level = max(abs(float(level)), meter_range_floor(ranges[0], mode_id))
    # A raised level can land on a *higher* range, whose own floor is higher
    # again; walk up until the level clears the floor of the range that holds
    # it.  Bounded by the ladder, and every shipped pairing settles on the
    # first pass.
    for _ in range(len(ranges) + 1):
        holder = next((r for r in ranges
                       if r.holds(level, allow_overrange=True)), None)
        if holder is None:
            return None, (f"no {what} range holds a "
                          f"{level:g} {token} proof")
        floor = meter_range_floor(holder, mode_id)
        if level >= floor * (1.0 - 1e-12):
            break
        level = floor
    if level >= threshold:
        return None, (f"the smallest level {what} can read to "
                      f"+/-{PROBE_TOLERANCE * 100:g} % is {level:g} {token}, "
                      f"which is not below the {threshold:g} {token} hazard "
                      f"threshold; a proof there would be the hazard it "
                      f"exists to prevent")
    return level, ""


def proof_level(source_spec: ModelSpec, point,
                meter_spec: ModelSpec | None = None) -> float | None:
    """The safe level to prove *point*'s wiring at, or ``None``.

    ``max(10 mV, 0.1 % of the range's full scale)`` for volts and
    ``max(100 uA, 0.1 % of full scale)`` for amps -- at the point's own
    frequency, because an ac proof at dc proves nothing about an ac path --
    **and never below what the meter can read** (the controller's amendment of
    2026-08-22; see :func:`resolvable_proof_level`).  For resistance the
    standard itself: an ohms output sources no hazard, and reading the
    standard is the proof.

    ``None`` where this point cannot prove anything -- the ohms **short**,
    which reads near zero whether the leads are on the terminals or in the
    air, or a meter that can resolve no safe level at all (:func:`proof_note`
    is what says which, and the caller flags the second).  With the short the
    caller keeps looking: the proof is then taken at the next point that can
    carry one, which on a generated resistance group is the 1 ohm standard
    right after it.

    The first half of the level is deliberately *not* a fraction of the
    meter's range: it is a property of the calibrator's range, so the same
    wiring is proved at the same level whichever meter is on the other end of
    the leads.  The amendment is the floor under it, not a replacement.  Which
    range the level is **read** on is the meter's own question, and the answer
    is the smallest range that holds it -- see :meth:`_Executor.wiring_proof`.
    """
    level, _ = _proof_level_and_note(source_spec, point, meter_spec)
    return level


def proof_note(source_spec: ModelSpec, point,
               meter_spec: ModelSpec | None = None) -> str:
    """Why §8's proof cannot be taken at *point*, or ``""``.

    Empty for a point that *can* be proved and for one that has nothing to
    prove (the ohms short): only a level the meter cannot resolve gets words,
    because only that one has to be reported.
    """
    _, note = _proof_level_and_note(source_spec, point, meter_spec)
    return note


def _proof_level_and_note(source_spec: ModelSpec, point,
                          meter_spec: ModelSpec | None
                          ) -> "tuple[float | None, str]":
    """The level and the refusal, computed once.  See :func:`proof_level`."""
    quantity = Quantity(point.quantity)
    unit = quantity.unit
    if unit == "OHM":
        return (float(point.value) or None), ""
    range_spec = planned_source_range(source_spec, point)
    fs = (abs(float(range_spec.fs)) if range_spec is not None
          else abs(float(point.value)))
    if unit == "V":
        base = max(WIRING_PROOF_MIN_V, WIRING_PROOF_FRACTION * fs)
    elif unit == "A":
        base = max(WIRING_PROOF_MIN_A, WIRING_PROOF_FRACTION * fs)
    else:
        return None, ""
    return resolvable_proof_level(
        base, unit, meter_spec=meter_spec, quantity=quantity,
        frequency_hz=float(getattr(point, "frequency_hz", 0.0) or 0.0),
        mode_id=getattr(point, "mode", None))


# --------------------------------------------------------------------------
# The executor
# --------------------------------------------------------------------------

class _Executor:
    """One run's state: the roles, the two specifications and the knobs.

    Private on purpose -- :func:`run_plan` is the interface, and everything
    here is bookkeeping that a caller should not have to assemble.
    """

    def __init__(self, plan: TestPlan, source: SourceRole, meter: MeterRole, *,
                 source_spec: ModelSpec, meter_spec: ModelSpec,
                 probe: EnvironmentProbe | None,
                 run_id: str, rule, settle_margin_s: float,
                 environment_interval_s: float,
                 loading_relative_uncertainty: float,
                 thermal_emf: float | None,
                 reference_tempco_ref_c: float | None,
                 reference_traceability: Traceability | None = None,
                 min_samples: int | None = None,
                 meter_temperature_windows: Mapping | None = None,
                 lead_resistance_ohm: float | None = None,
                 extra_conditions: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = _now) -> None:
        self.plan = plan
        #: Absolute windows of the **meter's internal temperature** (its
        #: ``TEMP?``), per quantity, inside which its figures hold -- see
        #: :func:`run_plan`.  Empty: the meter's window is resolved as before.
        self.meter_temperature_windows = {
            Quantity(q): (float(w[0]), float(w[1]))
            for q, w in dict(meter_temperature_windows or {}).items()}
        self.lead_resistance_ohm = (None if lead_resistance_ohm is None
                                    else float(lead_resistance_ohm))
        self.extra_conditions = extra_conditions
        self.source = source
        self.meter = meter
        self.source_spec = source_spec
        self.meter_spec = meter_spec
        self.probe = probe
        self.run_id = run_id
        self.rule = DecisionRule(rule)
        self.settle_margin_s = float(settle_margin_s)
        self.environment_interval_s = float(environment_interval_s)
        self.loading_relative_uncertainty = float(loading_relative_uncertainty)
        self.thermal_emf = thermal_emf
        self.reference_tempco_ref_c = reference_tempco_ref_c
        self.reference_traceability = reference_traceability
        #: Early termination: ``None`` (the default) reads the plan's full
        #: count in one block, exactly as before.
        self.min_samples = (int(min_samples) if min_samples is not None
                            else None)
        #: Flags the sampling of the point being read put on its row.
        self._sampling_flags: tuple[str, ...] = ()
        self.sleep = sleep
        self.clock = clock
        self._environment = UNKNOWN_ENVIRONMENT
        self._environment_at: datetime | None = None
        self._environment_error: str | None = None
        #: Set by a safety rule that has already written its row; the run
        #: stops on it once that row has been yielded.
        self.pending_abort: EngineError | None = None
        #: Flags and a note the **next** row must carry, for a rule that fired
        #: between two points and has no row of its own -- today only §8's
        #: skipped proof (:meth:`skip_wiring_proof`).
        self.pending_flags: tuple[str, ...] = ()
        self.pending_note: str = ""
        #: The magnitude this engine last energized, and did not take off
        #: again.  Nothing between two points of a group de-energizes on its
        #: own, so this is what tells the ordinary path whether configuring
        #: the meter for the next point would range it **down** under a live
        #: output (§9).  Zero means "the output is off as far as this engine
        #: knows"; a standby it could not confirm leaves it alone.
        self.live_value = 0.0
        self._cal_temp: float | None = None
        self._cal_temp_read = False

    # -- identity ---------------------------------------------------------

    @property
    def dut_is_meter(self) -> bool:
        return self.plan.dut_is_meter

    def _interval(self, spec: ModelSpec, *, is_dut: bool) -> str | None:
        """The specification column this instrument is read from.

        The DUT's is the plan's -- that is the claim being tested.  The
        reference is always taken at its own default, because a reference is
        not being tested and its 24-hour column is not evidence about a bench
        instrument that was last adjusted a year ago.
        """
        if is_dut:
            return self.plan.interval or spec.default_interval
        return spec.default_interval

    # -- temperature ------------------------------------------------------

    def source_cal_temperature(self) -> float | None:
        """The ambient the source's calibration constants assume, read once.

        The 5700A's ``CAL_TEMP?``.  It is a stored constant, not a live
        sensor, so it cannot change during a run and is read once and cached
        -- and read lazily, because :func:`run_plan` validates its arguments
        before anything is touched and a constructor that queried an
        instrument would break that.  A source that does not offer the
        capability, or that raises, leaves it unknown: an invented centre for
        a relative window would be worse than no window at all.
        """
        if self._cal_temp_read:
            return self._cal_temp
        self._cal_temp_read = True
        probe = getattr(self.source, "cal_temperature", None)
        if probe is None:
            return None
        try:
            value = probe()
        except Exception:                  # noqa: BLE001 - provenance only
            return None
        if value is None or not math.isfinite(float(value)):
            return None
        self._cal_temp = float(value)
        return self._cal_temp

    def _temperature_centre(self, spec: ModelSpec) -> float | None:
        """The centre of *spec*'s window, where its window is a relative one.

        The instrument is the only thing that knows it: ``CAL_TEMP?`` for the
        calibrator, the ACAL/Tcal temperature for the meter (which no model
        file can state, so the operator supplies it as
        ``reference_tempco_ref_c``).
        """
        if spec is self.source_spec:
            return self.source_cal_temperature()
        return self.reference_tempco_ref_c

    def _window_for(self, spec: ModelSpec, *,
                    is_dut: bool) -> tuple[float, float] | None:
        """The absolute temperature window *spec*'s figures hold in.

        A relative window ("+/- 5 C from calibration temperature") resolves as
        soon as its centre is in hand, and answers instead of any declared
        absolute one; without a centre the declared absolute window answers and
        a model that declares neither has no window, as before.
        """
        return spec.temperature.window_for(
            self._interval(spec, is_dut=is_dut),
            self._temperature_centre(spec))

    # -- environment ------------------------------------------------------

    def environment(self, *, force: bool = False) -> Environment:
        """The ambient, re-read when the cached one is older than the interval.

        A probe failure is **not** fatal and is **not** silent: the ambient
        becomes unknown (which blocks a PASS honestly) and every row taken
        under it carries :data:`FLAG_AMBIENT_READ_FAILED`.  A two-hour run must
        not be lost to one database hiccup, and a stale number would be worse
        than none -- it looks like knowledge.
        """
        if self.probe is None:
            return UNKNOWN_ENVIRONMENT
        now = self.clock()
        if not force and self._environment_at is not None:
            age = (now - self._environment_at).total_seconds()
            if age < self.environment_interval_s:
                return self._environment
        try:
            environment = self.probe.read()
        except Exception as exc:                       # noqa: BLE001 - reported
            self._environment = UNKNOWN_ENVIRONMENT
            self._environment_error = f"{type(exc).__name__}: {exc}"
        else:
            self._environment = environment
            self._environment_error = None
        self._environment_at = now
        return self._environment

    # -- ranges -----------------------------------------------------------

    def _source_range(self, point, state: SourceState,
                      flags: list[str] | None = None) -> RangeSpec | None:
        """The source range this point was produced on.

        By identifier: the calibrator's resistances are a discrete catalogue
        sourced at *characterized* values, and resolving 19003.8 ohm by value
        falls through into the 100 kohm entry, whose specification is looser --
        i.e. it would pass a calibrator that should fail.

        **The readback wins over the plan** (H05).  ``RANGE?`` is what the
        output was actually on; ``point.range_id`` is what the plan asked for,
        and without a range lock the calibrator autoranges to the lowest range
        that holds the value, so the two disagree for every below-full-scale
        point on an unlocked run.  Preferring the plan there took the limit,
        the settle time and the loading correction from a range the output was
        never on.  A disagreement both sides of the file can name is flagged
        with :data:`FLAG_RANGE_MISMATCH` rather than resolved silently; a
        readback the model file does not list is not a disagreement, it is a
        gap in the file, and the plan's row still answers.
        """
        function = self.source_spec.function(Quantity(point.quantity))
        if function is None:
            return None
        planned_id = getattr(point, "range_id", None)
        actual = _range_by_id(function, state.range_id) if state.range_id \
            else None
        planned = _range_by_id(function, planned_id) if planned_id else None
        if flags is not None and actual is not None and planned is not None \
                and actual is not planned:
            flags.append(FLAG_RANGE_MISMATCH)
        if actual is not None:
            return actual
        if planned is not None:
            return planned
        return select_range(function, abs(float(state.actual_value)))

    def _meter_range(self, point, config: MeterConfig,
                     state: SourceState) -> RangeSpec | None:
        """The meter range that was actually configured, not one re-derived.

        ``MeterConfig`` records what the adapter applied; asking the spec layer
        again would re-run the range selection against a readback the range has
        no overrange allowance for.
        """
        function = self.meter_spec.function(Quantity(point.quantity))
        if function is None:
            return None
        if config is not None and config.range_fs is not None:
            found = _range_by_fs(function, config.range_fs)
            if found is not None:
                return found
        if config is not None and config.range_arg is not None:
            found = _range_by_arg(function, config.range_arg)
            if found is not None:
                return found
        return select_range(function, abs(float(state.actual_value)))

    # -- limits -----------------------------------------------------------

    def _limit(self, spec: ModelSpec, point, value: float,
               range_spec: RangeSpec | None, *, is_dut: bool,
               required: bool,
               interval: str | None = None) -> LimitResult | None:
        """One model's tolerance at this point, from its transcribed tables.

        Evaluated at the **sourced** value rather than at the nominal: that is
        the best estimate of what was on the terminals, it is what both
        instruments saw, and taking a %-of-reading limit against the DUT's own
        reading would let a failing instrument widen its own limit.

        The range is pinned by ``range_fs`` so that
        :func:`~caldmm.cal.specs.select_range` matches by full scale instead
        of asking ``holds()``, which has no overrange allowance and would drop
        a characterized 10.005 ohm off its own 10 ohm range.

        *interval* overrides the column this instrument is read from.  Used
        for one thing: the zero-point fallback of a source under test, where
        the claim actually being tested is the one the instrument's own
        ``CAL_INTV`` computes ``UNCERT?`` at (M64).

        ``role`` tells :func:`~caldmm.cal.specs.evaluate_limit` which side of
        the comparison this model is on, so a band edge shared by two rows is
        resolved by the tighter row for the instrument under test and by the
        looser one for the reference it is bounded against -- the lenient,
        role-free rule is what a *plan* uses, not what a verdict does.
        """
        quantity = Quantity(point.quantity)
        function = spec.function(quantity)
        if function is None:
            return None
        frequency = (float(point.frequency_hz)
                     if quantity.frequency_dependent else None)
        try:
            return evaluate_limit(
                spec, quantity, value,
                range_fs=None if range_spec is None else range_spec.fs,
                freq_hz=frequency, connection=point.connection,
                interval=(interval if interval is not None
                          else self._interval(spec, is_dut=is_dut)),
                mode=_mode_for(function, point),
                role=PairRole.DUT if is_dut else PairRole.REFERENCE)
        except SpecError:
            if required:
                # The DUT's own limit is the claim under test; a typo there
                # rendering as "no specification" is how a failing meter passes.
                raise
            return None

    def _claim_limit(self, point, value: float,
                     range_spec: RangeSpec | None, state: SourceState,
                     default: LimitResult) -> LimitResult:
        """*default*, re-read at the interval the instrument's own claim uses.

        ``UNCERT?`` is computed by the firmware at the unit's stored
        ``CAL_INTV``/``CAL_CONF``, and the answer carries that interval.  A
        table fallback taken at the *plan's* interval instead would test the
        1-year claim at the zero points of a run whose every other point
        tested the 24-hour one.  Unchanged when the instrument names no
        interval, or names one this model file has no column for.
        """
        interval = _interval_id_for_days(
            self.source_spec,
            None if state.uncertainty is None else state.uncertainty.interval_days)
        if interval is None or interval == self._interval(self.source_spec,
                                                          is_dut=True):
            return default
        alternative = self._limit(self.source_spec, point, value, range_spec,
                                  is_dut=True, required=False,
                                  interval=interval)
        return default if alternative is None else alternative

    # -- budget -----------------------------------------------------------

    def _reference_component(self, state: SourceState,
                             source_limit: LimitResult | None,
                             meter_limit: LimitResult | None,
                             flags: list[str]):
        """The reference instrument's contribution, or ``None``.

        With the meter under test the reference is the calibrator's own
        ``UNCERT?``, which is a **confidence statement at the level the
        instrument chose** (``CAL_CONF``) -- not a rectangular bound and not a
        standard uncertainty -- so it goes through
        :func:`~caldmm.cal.uncertainty.from_calibrator_uncert`.  With the
        source under test the reference is the meter, whose datasheet limit is
        a bound with no distribution attached and is therefore rectangular
        (GUM 4.3.7).

        Returns ``None`` when neither side states anything; the caller turns
        that into ``NO_SPEC`` rather than into a budget that quietly omits its
        reference term -- which would understate ``U``, overstate the TUR and
        hand out a PASS with nothing behind it.
        """
        if self.dut_is_meter:
            uncertainty = state.uncertainty
            absolute = state.expanded_uncertainty
            label = f"reference ({self.source_spec.model_id} UNCERT?)"
            # A figure of **zero** is not a figure.  A relative ``UNCERT?`` at
            # a zero setpoint is ppm of zero, and taking it at face value
            # builds a reference term of exactly 0: U understated by an order
            # of magnitude, TUR overstated by the same, and no flag, because
            # ``FLAG_ZERO_UNCERTAINTY`` only fires when the *whole* budget is
            # zero.  The table's own absolute floor is what such a point is
            # specified by (0.8 uV on the dc 220 mV range at 1 y).
            if uncertainty is not None and _finite(absolute) and absolute:
                if uncertainty.confidence:
                    return from_calibrator_uncert(
                        label, absolute, float(uncertainty.confidence),
                        source=(f"UNCERT? {uncertainty.value:g} "
                                f"{uncertainty.unit or 'abs'} at "
                                f"{float(uncertainty.confidence) * 100:g} %"))
                # The level is *not* guessed: assuming 95 % where the unit is
                # set to CONF99 understates the reference by a third.  A
                # rectangular bound is the conservative reading of "+/-x with
                # no level stated", and the flag says so out loud.
                flags.append(FLAG_REFERENCE_CONFIDENCE_UNKNOWN)
                return from_half_width(
                    label, absolute, "rectangular",
                    source="UNCERT? at an unstated confidence level")
            if source_limit is not None:
                flags.append(FLAG_REFERENCE_FROM_SPEC_TABLE)
                return self._from_spec_limit(self.source_spec, source_limit)
            return None
        if meter_limit is not None:
            return self._from_spec_limit(self.meter_spec, meter_limit)
        return None

    def _from_spec_limit(self, spec: ModelSpec, limit: LimitResult):
        """A specification-table limit as a budget component.

        **Rectangular only where the manufacturer states nothing.**  A
        published table is a bound with no distribution attached (GUM 4.3.7)
        *unless* the manufacturer says otherwise, and two of these do: the
        5700A's tables are "provided at both the 99 % and 95 % confidence
        levels", with "Fluke guarantees performance verification using
        specifications stated to 99% confidence level"
        (``5700a-operators-manual-1996.txt`` lines 1107-1116 and 1214), and
        the 1281's carry "[2] Combined uncertainties to 95% minimum confidence
        level" (``1281-user-manual.txt:12507-12508``).  Dividing a 99 % figure
        by sqrt(3) makes the standard uncertainty 49 % too large -- the
        conservative direction, so never a false PASS, but it depresses the
        TUR and turns PASS into INDETERMINATE at points already near the 4:1
        floor.  The 3458A and 34420A state nothing, so they stay rectangular;
        the 34401A's ">4 sigma" is a design-and-test margin, not a stated
        confidence, and is deliberately not read as one.
        """
        label = f"reference ({spec.model_id} spec)"
        confidence = spec.spec_confidence
        if confidence:
            return from_confidence(
                label, limit.limit, float(confidence),
                source=(f"{limit.describe()} at "
                        f"{float(confidence) * 100:g} % confidence"))
        return from_half_width(
            label, limit.limit, "rectangular",
            source=f"{limit.describe()}, no stated confidence")

    def _load_derating_component(self, point, state: SourceState,
                                 reference, flags: list[str]):
        """The plan's load-current derating, on the calibrator's side (H06).

        The 5700A's resistance uncertainty is stated **only** inside the Full
        Spec Load Range IL-IU ([OM 1996] lines 1960-2016, PDF p.49); outside it
        the manual gives an adder, ``K(IL - I)/(IL x I)`` below IL and
        ``K(I^2 - IU^2)`` above IU (lines 2064-2085, p.50).  The calibrator
        cannot know what current the paired meter sources, so ``UNCERT?``
        cannot include it and :func:`~caldmm.cal.plan.generate_plan` carries
        it on the point instead.  A plan that promises the adder in its note
        and a budget that never adds it understates ``u_ref`` at exactly the
        points the manual singles out -- 14 ppm against a 31 ppm figure at
        19 ohm, 262 ppm against 110 ppm at 1 ohm on a 34401A.

        **Only with the calibrator as the reference.**  With the calibrator
        under test the same number widens its *tolerance* instead, in
        :meth:`_decide`: the manual's accuracy statement at that load is the
        stated figure plus the derating, and adding it in both places would
        count one effect twice.

        The distribution follows whatever the reference term itself got, and
        the component's ``source`` says which: a stated confidence where the
        manufacturer states one (the table's 99 %, or the level ``UNCERT?``
        was quoted at), rectangular where none is stated (GUM 4.3.7).  The
        adder is part of the same manufacturer's statement about the same
        output, so treating it differently from the figure it is added to
        would be an assertion the manual does not make.

        Returns ``None`` when there is no reference term to add to: a point
        with no stated reference is already a ``NO_SPEC`` row, and a lone
        derating term in an otherwise referenceless budget would dress it up
        as a measurement.
        """
        if not self.dut_is_meter or reference is None:
            return None
        adder = _load_adder(point)
        if not adder:
            return None
        from_table = FLAG_REFERENCE_FROM_SPEC_TABLE in flags
        unstated = FLAG_REFERENCE_CONFIDENCE_UNKNOWN in flags
        flags.append(FLAG_LOAD_DERATED)
        label = f"load derating ({self.source_spec.model_id})"
        confidence = None
        origin = ""
        if from_table:
            confidence = self.source_spec.spec_confidence
            origin = "the spec table's"
        elif not unstated and state.uncertainty is not None:
            confidence = state.uncertainty.confidence
            origin = "UNCERT?'s"
        if confidence:
            return from_confidence(
                label, adder, float(confidence),
                source=(f"OM load-current derating, at {origin} "
                        f"{float(confidence) * 100:g} % confidence"))
        return from_half_width(
            label, adder, "rectangular",
            source="OM load-current derating, no stated confidence")

    def _traceability_component(self, point, value: float):
        """The reference meter's own calibration uncertainty, as its own term.

        Meter specifications are *relative to the standards the meter was
        calibrated against*: "Absolute accuracy of the 3458A is determined by
        adding these relative accuracies to the traceability of your
        calibration standard.  For DCV, 2 ppm is the traceability error from
        the factory" (``3458a-datasheet.txt:282-287``, PDF p.9; the
        per-function notes give 2 ppm dc volts, 3 ppm at 10 kohm, 5 ppm dc
        current).  The 34401A says the same ("[2] Relative to calibration
        standards") and the 1281 carries a "Calibration Uncertainty" column.

        Only when the meter is the **reference**: in that direction the
        comparison is absolute and this is a required GUM input (JCGM 100
        4.3), and omitting it understates ``u_ref`` -- the unsafe direction.
        When the meter is the one under test, its tolerance is its relative
        specification and nothing here belongs in it.

        The operator's ``reference_traceability`` wins over the file: the
        data sheet's figure is the *factory*'s, and a unit recalibrated
        elsewhere has its certificate's.
        """
        if self.dut_is_meter:
            return None
        traceability = self.reference_traceability
        if traceability is None:
            function = self.meter_spec.function(Quantity(point.quantity))
            traceability = getattr(function, "traceability", None)
        if traceability is None:
            return None
        half_width = traceability.half_width(value)
        if not half_width:
            return None
        label = f"reference traceability ({self.meter_spec.model_id})"
        note = f"; {traceability.note}" if traceability.note else ""
        if traceability.k:
            return from_expanded(
                label, half_width, float(traceability.k),
                source=f"traceability at k = {float(traceability.k):g}{note}")
        if traceability.confidence:
            return from_confidence(
                label, half_width, float(traceability.confidence),
                source=(f"traceability at "
                        f"{float(traceability.confidence) * 100:g} % "
                        f"confidence{note}"))
        return from_half_width(
            label, half_width, "rectangular",
            source=f"traceability, no stated confidence{note}")

    def _lead_resistance(self, point, flags: list[str]
                         ) -> tuple[float, UncertaintyComponent | None]:
        """``(correction, component)`` for the test leads of a 2-wire reading.

        A 2-wire resistance reading is the resistance at the calibrator's
        binding posts **plus** the round-trip resistance of the leads: a
        one-sided bias of tens of milliohms that, on the 3458A's 10 ohm range,
        is a tenth of its 250 mOhm 2-wire offset tolerance.  With the leads
        measured (``lead_resistance_ohm``) the reading is corrected and the
        value carries a rectangular term; without, :meth:`_decide` makes the
        point INDETERMINATE where the bound is material.
        """
        if Quantity(point.quantity) is not Quantity.RES \
                or Connection(point.connection) is not Connection.TWO_WIRE \
                or self.lead_resistance_ohm is None:
            return 0.0, None
        resistance = self.lead_resistance_ohm
        flags.append(FLAG_LEAD_RESISTANCE_CORRECTED)
        half_width = max(LEAD_RESISTANCE_RELATIVE_UNCERTAINTY * resistance,
                         LEAD_RESISTANCE_FLOOR_OHM)
        return -resistance, from_half_width(
            "2-wire test-lead resistance", half_width, "rectangular",
            source=f"stated {resistance:g} ohm, corrected")

    def _meter_window(self, point) -> tuple[float, float] | None:
        """The meter's internal-temperature window for *point*'s quantity."""
        return self.meter_temperature_windows.get(Quantity(point.quantity))

    def _tempco_component(self, reference_limit: LimitResult | None,
                          value: float, environment: Environment,
                          flags: list[str], *, point=None,
                          meter_temp: float | None = None):
        """The reference's temperature coefficient over its departure from Tcal.

        Only the **reference** carries one here, and only when its uncertainty
        came from its specification table.  A calibrator's ``UNCERT?`` already
        "includes stability, temperature coefficient, linearity, line and load
        regulation and the traceability to external standards", so adding a
        tempco to it double-counts; and the DUT's own tempco is not an
        uncertainty of the comparison -- whatever the DUT does at this
        temperature *is* the error being measured.

        Returns ``None`` when the departure is unknown, and flags it, rather
        than letting a silent zero pretend the instrument was at its reference
        temperature.  For a 3458A that is the normal state (its figures are
        relative to the ACAL temperature, which the model file cannot know);
        pass ``reference_tempco_ref_c`` to resolve it.

        **The coefficient is an adder outside a window, not a slope from a
        point.**  Every data sheet says so: "Operating temperature is 10 C
        beyond the standard range of Tcal +/- 5 C ... (0.15 ppm x 10 V + 0.01
        ppm x 10 V) x 10 C" (``3458a-datasheet.txt:355-366``, PDF p.10, for an
        ambient 15 C from Tcal); "You must add additional temperature
        coefficient errors to the accuracy specification if you are operating
        the multimeter outside a 23 C +/- 5 C temperature range"
        (``34401a-user.txt:9619-9624``, PDF p.227); the 34420A's tempco column
        is headed "28 C to 55 C" (``34420a-datasheet.txt:129``).  So the
        departure is measured from the **window edge** and is zero inside it;
        a model that declares no window at all has no dead band and the whole
        departure from its ``tempco_ref_c`` counts, which is the honest
        reading of a file that states one number and no window.
        """
        if reference_limit is None:
            return None
        range_spec = reference_limit.range_spec
        band = reference_limit.band
        tempco: Tempco | None = None
        if band is not None and band.tempco is not None:
            tempco = band.tempco
        elif range_spec.tempco is not None:
            tempco = range_spec.tempco
        if tempco is None:
            return None
        spec = self.meter_spec if not self.dut_is_meter else self.source_spec
        reference_c = self.reference_tempco_ref_c
        if reference_c is None:
            reference_c = spec.temperature.tempco_ref_c
        ambient = environment.ambient_for_verdict
        window = self._window_for(spec, is_dut=False)
        meter_window = (self._meter_window(point)
                        if spec is self.meter_spec else None)
        if meter_window is not None:
            # The meter's figures are relative to its OWN internal
            # temperature at adjustment (dc) or at ACAL (ac), so the departure
            # is its TEMP? now against that window -- never the room ambient,
            # which sits 10-20 C below the inside of a 3458A.
            ambient, window = meter_temp, meter_window
        if ambient is None or (window is None and reference_c is None):
            flags.append(FLAG_TEMPCO_NOT_EVALUATED)
            return None
        if window is None:
            excess = abs(float(ambient) - float(reference_c))
            source = (f"{float(ambient) - float(reference_c):+g} C from "
                      f"{float(reference_c):g} C, no window declared")
        else:
            low, high = window
            excess = max(0.0, float(ambient) - high, low - float(ambient))
            if not excess:
                return None
            source = (f"{excess:g} C beyond the {low:g} to {high:g} C window "
                      f"(ambient {float(ambient):g} C)")
        if not excess:
            return None
        half_width = tempco.adder(value, range_spec.fs, excess)
        return from_half_width(
            f"reference tempco ({spec.model_id})", half_width, "rectangular",
            source=source)

    def _loading(self, point, reading: Reading,
                 source_range: RangeSpec | None,
                 meter_range: RangeSpec | None, flags: list[str]):
        """``(correction, component)`` for finite meter input impedance.

        Voltage only: the divider model is what a finite ``Zin`` across a
        finite ``Zs`` does to a *voltage*, and applying it to a sourced current
        or a resistance would be arithmetic with no physics under it.

        A known systematic effect is **corrected**, with the uncertainty of the
        correction carried, rather than left in and bounded (GUM 3.2.4).  The
        5700A's 220 mV range is 50 ohm out; into a 34401A's 10 Mohm that is
        5 ppm -- larger than several of the floors in the same file, and
        entirely invisible unless someone computes it.

        **The model is a resistive divider, and it is only applied where that
        is the physics.**  A meter's ac input is not a resistance: the 3458A's
        is "1 MOhm +/- 15% with <140 pF" on its 10 mV-1 V ranges
        (``3458a-datasheet.txt:968-972``), and against the 5700A's 50 ohm mV
        ac output the reactive term is ~1000 ppm at 1 MHz where the resistive
        correction is ~50 ppm -- 20 times the thing being corrected.  Above
        :data:`LOADING_MAX_FREQUENCY_HZ` the correction is therefore not
        applied and the row says so; below it the ac branch carries the
        manuals' own +/-15 % rather than a flat 10 % (L78, L83).

        **Wideband is excluded**, although it is the one 5700A output that is
        50 ohm by construction.  Its figures "apply to the end of the cable
        and 50 Ohm termination used for calibration"
        (``5700a-operators-manual-1996.txt`` line 2528, PDF p.60), so the load
        is already inside the stated value and dividing it out again would be
        a bias, not a correction.  The millivolt ranges are the opposite case:
        no external sense below 2.2 V (OM 1772-1774, PDF p.45) and a
        calibration that measures the *unloaded* divider ratio into the
        internal cal amplifier (``5700a-service-manual-1996.txt`` 5374-5390,
        PDF pp. 118-119), so their figure is the open-circuit one and a real
        meter really does read low (L83).
        """
        quantity = Quantity(point.quantity)
        if quantity.unit != "V" or quantity is Quantity.WBV:
            return 0.0, None
        z_source = getattr(source_range, "source_impedance_ohm", None)
        z_input = getattr(meter_range, "input_impedance_ohm", None)
        if z_source is None or not z_input or not _finite(reading.mean):
            return 0.0, None
        frequency = float(getattr(point, "frequency_hz", 0.0) or 0.0)
        if frequency > LOADING_MAX_FREQUENCY_HZ:
            # Not corrected (the model files carry no input capacitance), but
            # not ignored either: the capacitive attenuation is a known
            # one-sided effect, budgeted as a rectangular term of its full
            # size rather than left out of U (GUM F.2.4.5).
            flags.append(FLAG_LOADING_UNCORRECTED_HF)
            omega_rc = (2.0 * math.pi * frequency * float(z_source)
                        * HF_LOAD_CAPACITANCE_F)
            attenuation = 1.0 - 1.0 / math.sqrt(1.0 + omega_rc ** 2)
            if attenuation <= 0.0:
                return 0.0, None
            return 0.0, from_half_width(
                "capacitive loading (uncorrected above 100 kHz)",
                attenuation * abs(float(reading.mean)), "rectangular",
                source=(f"{float(z_source):g} ohm source into "
                        f"{HF_LOAD_CAPACITANCE_F * 1e12:g} pF at "
                        f"{frequency:g} Hz: {attenuation * 1e6:.0f} ppm"))
        relative = (self.loading_relative_uncertainty if frequency <= 0.0
                    else max(self.loading_relative_uncertainty,
                             AC_LOADING_RELATIVE_UNCERTAINTY))
        correction, component = loading_correction(
            reading.mean, float(z_source), float(z_input),
            relative_uncertainty=relative)
        if correction:
            flags.append(FLAG_LOADING_CORRECTED)
        return correction, component

    def _thermal_emf(self, point, flags: list[str]):
        """The loop's thermal EMF, as a declared bound (M63, L84).

        Defaulted rather than opt-in: at a zero setpoint or a short it is the
        largest term in the budget -- the dc 220 mV range's 1-year floor is
        0.8 uV and a pair of ordinary copper joints is of that order -- so a
        budget without it overstates the quality of the measurement exactly
        where the measurement is hardest.  ``thermal_emf=0`` states that there
        is none; anything else is used as given and is not flagged as an
        assumption.

        For ohms and current the bound cannot be *converted*: a loop EMF
        becomes an ohms error as ``emf / I_test`` and a current error through
        the shunt, and no model file states either figure.  Rather than
        inventing a test current or dropping the term silently, such points
        carry :data:`FLAG_THERMAL_EMF_NOT_CONVERTED` -- which is the honest
        GUM answer: the component is recognised and its evaluation is
        recorded as not possible from the data in hand.
        """
        bound = (DEFAULT_THERMAL_EMF_V if self.thermal_emf is None
                 else float(self.thermal_emf))
        if not bound:
            return None
        quantity = Quantity(point.quantity)
        if quantity.unit == "V":
            if self.thermal_emf is None:
                flags.append(FLAG_THERMAL_EMF_DEFAULT)
            return thermal_emf_component("thermal emf", bound)
        value = abs(float(point.value))
        low_ohms = quantity is Quantity.RES and value <= THERMAL_EMF_LOW_OHMS
        current_zero = quantity.unit == "A" and value == 0.0
        if low_ohms or current_zero:
            flags.append(FLAG_THERMAL_EMF_NOT_CONVERTED)
        return None

    def _budget(self, *, point, state: SourceState, reading: Reading,
                environment: Environment, source_limit: LimitResult | None,
                meter_limit: LimitResult | None,
                source_range: RangeSpec | None,
                meter_range: RangeSpec | None,
                flags: list[str],
                meter_temp: float | None = None,
                ) -> tuple[UncertaintyBudget, float, UncertaintyComponent | None]:
        """``(budget, loading correction, the reference component or None)``.

        Assembled entirely from :mod:`caldmm.cal.uncertainty`'s builders: no
        statistics are invented here, and every component is in the quantity's
        own unit.  The reference component is handed back rather than a bare
        "was there one": the gross-error check needs *its* expanded
        uncertainty, which is not the combined ``U`` of everything else.
        """
        budget = UncertaintyBudget()
        if reading.n >= 2:
            budget = budget.add(type_a(
                "repeatability", reading.samples,
                source=f"{self.meter_spec.model_id}, n = {reading.n}"))
        else:
            # type_a raises below two samples, by design: a single reading has
            # no experimental standard deviation and a zero would drop the
            # repeat term out of the budget without saying so.
            flags.append(FLAG_NO_TYPE_A)

        reference = self._reference_component(state, source_limit, meter_limit,
                                              flags)
        budget = budget.add(reference)
        budget = budget.add(self._load_derating_component(
            point, state, reference, flags))
        budget = budget.add(self._traceability_component(
            point, float(state.actual_value)))

        if reading.resolution:
            budget = budget.add(resolution_component(
                f"{self.meter_spec.model_id} resolution", reading.resolution))

        correction, loading = self._loading(point, reading, source_range,
                                            meter_range, flags)
        budget = budget.add(loading)
        lead_correction, lead = self._lead_resistance(point, flags)
        correction += lead_correction
        budget = budget.add(lead)

        reference_limit = meter_limit if not self.dut_is_meter else (
            source_limit if FLAG_REFERENCE_FROM_SPEC_TABLE in flags else None)
        budget = budget.add(self._tempco_component(
            reference_limit, state.actual_value, environment, flags,
            point=point, meter_temp=meter_temp))

        budget = budget.add(self._thermal_emf(point, flags))

        return budget, correction, reference

    # -- one point --------------------------------------------------------

    def measure(self, point, *, point_index: int, group_index: int,
                health: SourceHealth,
                environment: Environment) -> ResultRow:
        """Configure, source, settle, read, decide -- and always return a row.

        Anything that goes wrong at this point is caught and turned into a
        recorded row carrying :data:`FLAG_POINT_FAILED`, the reason, and
        whatever was already known.  The run goes on: 193 good points and one
        that could not be read is a result, and abandoning the run would throw
        away the two hours that produced the rest.

        Three things stop a run instead of being absorbed, and all three still
        write their row first: a source trip or queued fault found *before*
        the point (the caller's check) or *during* it (M81), and a safety rule
        -- the meter's range read-back, the pre-step, the gross-error abort --
        which leaves a :class:`SafetyAbortError` in :attr:`pending_abort` for
        :func:`_iterate` to raise once the row is out.
        """
        state: SourceState | None = None
        config: MeterConfig | None = None
        reading: Reading | None = None
        try:
            # A point *below* the last one can only take the meter to a range
            # below what is still on the terminals, and nothing between two
            # points of a group de-energizes on its own.  Ranging up under a
            # live output is §9's rule and needs no standby; ranging down is
            # the hazard this whole ordering exists to prevent, so the output
            # comes off first.  The probes below step down by construction and
            # own their own standby.
            if abs(float(point.value)) < self.live_value:
                self._standby(point, point_index=point_index,
                              group_index=group_index)
            # The meter is configured from the NOMINAL, **before** the source
            # is energized (H03).  A range chosen from a live output is a
            # range chosen after the output has already been driven into
            # whatever the meter happened to be on -- on a dc-current range
            # that is the 3458A's 100 nA shunt, 545 kohm, and 22 uA into it
            # demands 12 V against a 10 V compliance limit.
            config = self.meter.configure(point)
            verified = self._verify_meter_range(
                point, config, point_index=point_index,
                group_index=group_index)
            config = self._prestep(point, config, point_index=point_index,
                                   group_index=group_index,
                                   range_verified=verified)
            state = self.source.apply(point)
            self.live_value = abs(float(point.value))
            # Re-targeted from the readback only where that would change the
            # range: the 5700A's resistances are characterized values up to
            # 500 ppm off the nominal, and a range that cannot hold the
            # readback is the wrong range.  Not re-validated:
            # RangeSpec.holds has no overrange allowance, and a characterized
            # 10.005 ohm is not "off range".
            config = self._retarget(point, config, state)
            wait = self._settle_seconds(point, config, state)
            if wait > 0.0:
                self.sleep(wait)
            reading = self._read(point, state=state, config=config)
            timestamp = self.clock()
            meter_temp = self._meter_temperature()
            after = self.source.health()
            if after.tripped or after.faults:
                # M81: the trip happened *during* this point.  The samples are
                # readings through an output that is no longer there, which
                # looks exactly like a catastrophic failure of the DUT, so the
                # row records what happened and makes no claim.
                return self._trip_row(
                    point, point_index=point_index, group_index=group_index,
                    health=after, environment=environment, state=state,
                    config=config, reading=reading, timestamp=timestamp,
                    meter_temp=meter_temp)
            if reading.n < 1:
                raise EngineError(
                    f"no valid samples: all {reading.overloads} reading(s) "
                    f"were the model's invalid-reading sentinel")
            return self._decide(point, point_index=point_index,
                                group_index=group_index, health=health,
                                environment=environment, state=state,
                                config=config, reading=reading,
                                timestamp=timestamp, meter_temp=meter_temp)
        except SafetyAbortError as exc:
            # A safety rule, not a bad point: the row is written so the file
            # says what happened, and :func:`_iterate` stops the run.
            self.pending_abort = exc
            return self._row(
                point, point_index=point_index, group_index=group_index,
                health=health, environment=environment, state=state,
                config=config, reading=reading, timestamp=self.clock(),
                conformity=self._no_decision(point, environment, str(exc)),
                budget=UncertaintyBudget(), reference_value=None,
                spec_limit=None,
                flags=[FLAG_POINT_FAILED] + ([exc.flag] if exc.flag else []))
        except Exception as exc:                       # noqa: BLE001 - recorded
            flags = [FLAG_POINT_FAILED]
            if reading is not None and reading.n < 1:
                flags.append(FLAG_NO_VALID_SAMPLES)
            return self._row(
                point, point_index=point_index, group_index=group_index,
                health=health, environment=environment, state=state,
                config=config, reading=reading, timestamp=self.clock(),
                conformity=self._no_decision(
                    point, environment,
                    f"{type(exc).__name__}: {exc}"),
                budget=UncertaintyBudget(), reference_value=None,
                spec_limit=None, flags=flags)

    # -- the wiring proof (Global Constraints §8) -------------------------

    def planned_source_range(self, point) -> RangeSpec | None:
        """This run's source range for *point*.  See the module function."""
        return planned_source_range(self.source_spec, point)

    def proof_level(self, point) -> float | None:
        """This run's proof level for *point*.  See the module function.

        The meter's specification is part of the answer since the controller's
        amendment of 2026-08-22: a level it cannot read to +/-10 % is not a
        proof, it is a false abort waiting for a correctly wired bench.
        """
        return proof_level(self.source_spec, point, self.meter_spec)

    def proof_note(self, point) -> str:
        """Why this run cannot prove *point*'s wiring, or ``""``."""
        return proof_note(self.source_spec, point, self.meter_spec)

    def skip_wiring_proof(self, point, note: str) -> None:
        """Record that §8's proof was skipped here, on the next row written.

        No abort and no silent pass: the points are measured, and the row that
        would have been preceded by a proof says in plain language why there
        was none.  :meth:`_row` carries the flag and the words onto it.
        """
        if FLAG_WIRING_PROOF_NOT_RESOLVABLE not in self.pending_flags:
            self.pending_flags += (FLAG_WIRING_PROOF_NOT_RESOLVABLE,)
        self.pending_note = (
            f"no wiring proof was taken before {_name_of(point)}: {note}")

    def _read_one(self, point) -> Reading:
        """One reading of *point*, with the plan's own discards honoured.

        **Not** a bare ``read(1)``.  Every generated plan throws its first
        sample away (:data:`~caldmm.cal.plan.DEFAULT_DISCARDS` is 1) and
        :func:`run_plan` refuses a meter that cannot honour that, because the
        first reading after a range change would otherwise be averaged in.
        Both probes below are taken *immediately* after a meter re-configure
        and a source step -- precisely the sample the plan says to discard --
        so a stale first reading would fail a +/-10 % check and abandon a run
        that nothing was wrong with.  A plan that asks for no discards still
        gets the plain ``read(1)`` a bare :class:`~caldmm.cal.roles.MeterRole`
        promises.
        """
        discard = self.plan.discards_for(point)
        if discard > 0:
            return self.meter.read(1, discard=discard)
        return self.meter.read(1)

    def _standby(self, point, *, point_index: int, group_index: int) -> None:
        """Take the output off before the meter is ranged for a probe (§9).

        A probe is by definition a step *down*, and the range that holds it is
        a step down with it.  Nothing between two points of a group
        de-energizes on its own -- :func:`_de_energize` runs at group
        boundaries and on the way out -- so without this the meter would be
        re-ranged with the **previous** point still on the terminals: on a
        34401A dc-current group the 0.1 A -> 1 A step means ``CURR:DC:RANG
        1E-02``, the 10 mA range and its 5 ohm shunt, while the calibrator is
        still sourcing 100 mA, and at 1 A the same mistake demands 5 V against
        the 2 V compliance limit for 1-2.2 A -- fault 1819, and a trip in the
        middle of a group whose earlier points are already written.

        A standby that will not confirm (the adapter reads ``ISR?`` back,
        M47) stops the run **before** the meter is touched: that is exactly
        when a range change must not happen.
        """
        try:
            self.source.standby()
        except Exception as exc:                       # noqa: BLE001 - fatal
            raise SafetyAbortError(
                f"the source did not confirm standby before the meter was "
                f"ranged for {_name_of(point)} "
                f"({type(exc).__name__}: {exc}); THE OUTPUT MAY STILL BE "
                f"LIVE, so the meter was left on the range it was on and "
                f"nothing further was sourced.  Press RESET on the front "
                f"panel and verify the standby light before touching the "
                f"terminals",
                flag=FLAG_STANDBY_NOT_CONFIRMED,
                group_index=group_index, point_index=point_index) from exc
        self.live_value = 0.0

    def _probe_at(self, probe, config: MeterConfig | None
                  ) -> tuple[SourceState, Reading, bool]:
        """Source *probe*, settle, read once, and check it loosely.

        The shared core of the two checks that stand between a plan and a
        pair of leads -- §8's wiring proof and §9's 10 % pre-step.  They ask
        the same question at two moments ("is what the meter sees what the
        calibrator says it is putting out?"), against one
        :data:`PROBE_TOLERANCE`, with one discard rule, so neither can drift
        away from the other.

        **The caller owns the standby and the meter.**  Both callers reach
        here with the output already off (:meth:`_standby`) and the meter
        already on a range that holds the probe -- the proof's own range for
        the wiring proof, the point's for the pre-step, which is a tenth of
        its own nominal.  Doing it here instead would put the standby
        *after* the range command it exists to precede.

        Returns ``(state, reading, ok)``; the callers own what a failure
        *means*, which is the only part that differs.
        """
        state = self.source.apply(probe)
        self.live_value = abs(float(probe.value))
        wait = self._settle_seconds(probe, config, state)
        if wait > 0.0:
            self.sleep(wait)
        reading = self._read_one(probe)
        actual = float(state.actual_value)
        mean = reading.mean
        ok = (_finite(mean)
              and abs(float(mean) - actual) <= PROBE_TOLERANCE * abs(actual))
        return state, reading, ok

    def _configure_probe(self, point) -> MeterConfig:
        """Configure for a probe -- a wiring proof or a 10 % pre-step.

        A probe is a +/-10 % check that never enters a verdict, so a meter
        that offers a fast probe configuration (the adapters'
        ``configure_probe``: 10 PLC, the fastest ac filter the tone allows)
        is read with it.  At 100 PLC the proof's two readings can be
        6.75 s of every point and the pre-step's another 6.75 s.  Every
        caller re-configures with the plan's own knobs, and the plan discards
        the first reading after that, before anything is measured.  A meter
        role without it is configured as usual.
        """
        probe = getattr(self.meter, "configure_probe", None)
        if callable(probe):
            return probe(point)
        return self.meter.configure(point)

    def wiring_proof(self, point, *, point_index: int, group_index: int,
                     health: SourceHealth, environment: Environment,
                     reason: str) -> ResultRow | None:
        """Source a small safe signal and check it loosely.  ``None`` if it passed.

        Global Constraints §8: before the first point of a run, at the start
        of every wiring group and every resumed group, and whenever the source
        changes range, the output goes to a level that cannot hurt anything
        and the meter is asked what it sees.  A lead in the wrong socket, a
        sense pair left shorted, a current path through the volts terminals --
        all of them read as "nothing" or "nonsense" here, at 10 mV, instead of
        at 1000 V twenty minutes later.

        **The output comes off first, and the proof is read on the range that
        holds the proof** -- not on the range the point will use.  The two
        halves are one rule.  A proof is one or two decades below the point,
        so the range that holds it is below the point's; asking for it with
        the previous point still live is precisely H03's hazard, and asking
        for the *point's* range instead would read a 2.2 mA proof at 0.07 % of
        a 34401A 3 A range, inside its own floor, and abort a bench that was
        wired correctly.  With the source in standby a step down costs
        nothing, and the meter is put **back up** on the point's own range --
        under the harmless proof level, before the planned amplitude -- by
        :meth:`measure`.

        On failure the row is written with :data:`FLAG_WIRING_PROOF_FAILED`
        and the run stops with the output in standby; the planned level is
        never applied to wiring that could not prove itself.

        **A proof that could not be read is not a proof that failed.**  A
        meter that returns a number far from what was sourced has said
        something about the bench; a meter that *raises* -- a driver timeout,
        a refused capability, an interface that went away -- has said nothing
        at all, and the two get different flags.  A 4-wire resistance run
        aborted twice on a bench that was wired correctly because a
        ``TimeoutError`` out of :meth:`_read_one` arrived under
        :data:`FLAG_WIRING_PROOF_FAILED` and sent the operator to check its
        leads.  The
        unreadable case carries :data:`FLAG_WIRING_PROOF_UNREADABLE` and names
        the underlying error instead, asserting nothing about the wiring.
        Both still stop the run with the output in standby: an unproved
        wiring is not sourced into either way.

        The one failure that is *not* a disproved wiring is the standby above
        it: if that does not confirm, no proof was made either way and the
        consequence is an output that may still be live, so the row keeps that
        rule's own :data:`FLAG_STANDBY_NOT_CONFIRMED` rather than being
        relabelled as a wiring the meter never saw.
        """
        level = self.proof_level(point)
        if level is None or not math.isfinite(level) or level == 0.0:
            return None
        unit = Quantity(point.quantity).unit
        proof = replace(point, value=level, range_id=None, range_arg=None,
                        settle_s=None,
                        label=f"wiring proof ({reason}) before "
                              f"{_name_of(point)}")
        state: SourceState | None = None
        config: MeterConfig | None = None
        reading: Reading | None = None
        # A disproved wiring until something says otherwise; the raising path
        # below downgrades it to "could not be read", which claims less.
        proof_flag = FLAG_WIRING_PROOF_FAILED
        try:
            self._standby(point, point_index=point_index,
                          group_index=group_index)
            config = self._configure_probe(proof)
            state, reading, ok = self._probe_at(proof, config)
            if ok:
                return None
            actual = float(state.actual_value)
            saw = ("nothing" if not _finite(reading.mean)
                   else f"{float(reading.mean):.6g} {unit}")
            message = (
                f"the wiring proof read {saw} where {actual:g} {unit} was "
                f"sourced, more than {PROBE_TOLERANCE * 100:g} % away; "
                f"the wiring is not what this plan assumes, so "
                f"{point.value:g} {unit} was not sourced into it")
        except SafetyAbortError as exc:
            # Only :meth:`_standby` raises this here, and it did so *before*
            # the meter was touched: nothing was proved, nothing was sourced,
            # and the terminals are not known to be safe.  Its own flag and
            # its own words, not this method's.
            self.pending_abort = exc
            return self._row(
                proof, point_index=point_index, group_index=group_index,
                health=health, environment=environment, state=None,
                config=None, reading=None, timestamp=self.clock(),
                conformity=self._no_decision(proof, environment, str(exc)),
                budget=UncertaintyBudget(), reference_value=None,
                spec_limit=None,
                flags=[FLAG_POINT_FAILED] + ([exc.flag] if exc.flag else []))
        except Exception as exc:                       # noqa: BLE001 - fatal
            # The meter never gave a value, so there is no comparison to
            # report and nothing has been learned about the wiring.  Say what
            # broke, and say only that.
            proof_flag = FLAG_WIRING_PROOF_UNREADABLE
            message = (f"the wiring proof at {level:g} {unit} could not be "
                       f"read from the meter ({type(exc).__name__}: {exc}); "
                       f"no reading was returned, so this says nothing about "
                       f"the wiring -- and {point.value:g} {unit} was not "
                       f"sourced")
        self.pending_abort = SafetyAbortError(
            message, flag=proof_flag, group_index=group_index,
            point_index=point_index)
        return self._row(
            proof, point_index=point_index, group_index=group_index,
            health=health, environment=environment, state=state,
            config=config, reading=reading, timestamp=self.clock(),
            conformity=self._no_decision(proof, environment, message),
            budget=UncertaintyBudget(), reference_value=None, spec_limit=None,
            flags=[FLAG_POINT_FAILED, proof_flag])

    def _check_gross_error(self, conformity: ConformityResult, point, *,
                           point_index: int, group_index: int,
                           reference, flags: list[str]) -> None:
        """Stop the run when a reading is off by ten tolerances (§8).

        A DUT out of specification is the finding a verification exists to
        make, and it is recorded and judged.  Ten times its tolerance is a
        different animal: a lead on the wrong terminal, the wrong function,
        the sense pair still shorted from the last group.  Grinding on would
        produce a hundred more rows of the same mistake and, on the way,
        source the rest of the plan into whatever is actually connected.

        Where the point has no usable limit the scale is **the reference
        component's** expanded uncertainty -- not the combined ``U`` of the
        whole budget.  The two are not interchangeable here: the no-limit case
        is very largely the case where there is *no reference term at all*, and
        the combined ``U`` is then Type A noise and a quantisation bound, tens
        of nanovolts, against which any ordinary reading is "a hundred
        tolerances".  With no reference there is no scale to judge grossness
        by, so the check is recorded as not applicable
        (:data:`FLAG_GROSS_CHECK_NOT_APPLICABLE`) rather than aborting a run
        on a point that was never going to be judged anyway.
        """
        error = abs(float(conformity.error))
        if not math.isfinite(error):
            return
        limit = conformity.tolerance_limit
        basis = "the point's tolerance"
        if not _finite(limit) or limit <= 0.0:
            limit = None
            basis = "the reference's expanded uncertainty"
            if reference is not None:
                limit = UncertaintyBudget().add(reference).expanded(
                    self.plan.confidence)
        if not _finite(limit) or limit <= 0.0:
            # No limit and no reference: nothing to be gross *against*.
            flags.append(FLAG_GROSS_CHECK_NOT_APPLICABLE)
            return
        if error <= GROSS_ERROR_FACTOR * float(limit):
            return
        flags.append(FLAG_GROSS_ERROR_ABORT)
        unit = conformity.unit or Quantity(point.quantity).unit
        self.pending_abort = SafetyAbortError(
            f"|error| = {error:.6g} {unit} is more than "
            f"{GROSS_ERROR_FACTOR:g} times {basis} "
            f"({float(limit):.6g} {unit}) at point {point_index} "
            f"({_name_of(point)}); that is a connection, not "
            f"an instrument out of specification.  Check the wiring and "
            f"resume with start_group={group_index}",
            flag=FLAG_GROSS_ERROR_ABORT, group_index=group_index,
            point_index=point_index)

    def _trip_row(self, point, *, point_index: int, group_index: int,
                  health: SourceHealth, environment: Environment,
                  state: SourceState | None, config: MeterConfig | None,
                  reading: Reading | None, timestamp: datetime,
                  meter_temp: float | None) -> ResultRow:
        """The row for a source that tripped or faulted *while* this point ran.

        Recorded, never judged: the reading was taken through an output that
        was on its way to standby, so the error it implies is the calibrator's
        event and not the DUT's.  The run stops on the
        :class:`SourceFaultError` this leaves behind, exactly as a trip found
        *before* a point does -- including on the last point of a plan, where
        there is no next point to catch it at.
        """
        listed = "; ".join(f"{code}: {text}" for code, text in health.faults)
        what = "tripped to standby" if health.tripped else "raised a fault"
        detail = f" ({listed})" if listed else ""
        message = (f"the source {what} while point {point_index} was being "
                   f"measured{detail}; the samples were taken through an "
                   f"output that was no longer the point that was asked for.  "
                   f"Clear the cause and resume with start_group={group_index}")
        self.pending_abort = SourceFaultError(
            message, group_index=group_index, point_index=point_index,
            health=health)
        return self._row(
            point, point_index=point_index, group_index=group_index,
            health=health, environment=environment, state=state,
            config=config, reading=reading, timestamp=timestamp,
            conformity=self._no_decision(point, environment, message),
            budget=UncertaintyBudget(), reference_value=None, spec_limit=None,
            flags=[FLAG_POINT_FAILED, FLAG_SOURCE_TRIPPED_MID_POINT],
            meter_temp=meter_temp)

    # -- manual ranging (Global Constraints §9) ---------------------------

    def _hazardous(self, point, config: MeterConfig | None) -> bool:
        """Whether this point (or its range) is above the hazard thresholds.

        The *range* counts as well as the point: a 10 V reading taken on a
        1000 V range is one keystroke away from 1000 V, and the range is what
        the read-back is checking.
        """
        unit = Quantity(point.quantity).unit
        if unit not in ("V", "A"):
            return False
        threshold = HAZARD_VOLTAGE_V if unit == "V" else HAZARD_CURRENT_A
        if abs(float(point.value)) > threshold:
            return True
        range_fs = getattr(config, "range_fs", None)
        return range_fs is not None and abs(float(range_fs)) > threshold

    def _verify_meter_range(self, point, config: MeterConfig | None, *,
                            point_index: int, group_index: int) -> bool:
        """Read the meter's range back and compare it, before ``OPERATE``.

        Only above the hazard thresholds, and only where the meter can answer:
        ``range_readback`` is an *optional* capability (see
        :class:`~caldmm.cal.roles.MeterRole`) and ``None`` means "this
        instrument cannot say", which is not the same as "the range is wrong".
        A meter that cannot fix its range at all has nothing to verify --
        there the 10 % pre-step below is the protection instead.

        Returns **whether the range was really verified**, which is what
        :meth:`_prestep_needed` needs: a read-back that could not be had
        leaves a hazardous point with no guard at all unless the pre-step
        takes over (the Wavetek 1281, which fixes its range with ``DCV
        <range>`` and has no range query in its command set).

        A mismatch aborts **before** anything is energized: the case this
        exists to prevent is a meter left on 100 mV meeting 1000 V.
        """
        if not self._hazardous(point, config):
            return False
        if not getattr(self.meter, "supports_manual_range", True):
            return False
        readback = getattr(self.meter, "range_readback", None)
        if readback is None:
            return False
        try:
            actual = readback()
        except Exception as exc:                       # noqa: BLE001 - fatal
            raise SafetyAbortError(
                f"could not read the meter's range back before energizing "
                f"{point.value:g} {Quantity(point.quantity).unit} "
                f"({type(exc).__name__}: {exc}); the output was not turned on",
                flag=FLAG_METER_RANGE_MISMATCH, group_index=group_index,
                point_index=point_index) from exc
        if actual is None:
            return False
        expected = getattr(config, "range_fs", None)
        if expected is None:
            return False
        if math.isclose(float(actual), float(expected),
                        rel_tol=1e-6, abs_tol=0.0):
            return True
        raise SafetyAbortError(
            f"the meter reports a range of {float(actual):g} where "
            f"{self.meter_spec.model_id} was configured for "
            f"{float(expected):g}, and this point sources "
            f"{point.value:g} {Quantity(point.quantity).unit}; the output was "
            f"not turned on",
            flag=FLAG_METER_RANGE_MISMATCH, group_index=group_index,
            point_index=point_index)

    def _prestep_needed(self, point, range_verified: bool = False) -> bool:
        """Whether §9's 10 %-of-target pre-step applies to this point.

        Always above 220 mA -- manual ranging does not waive it, because what
        it catches there is a wrong shunt or a wrong terminal, not a wrong
        range.  Above 22 V whenever the meter's range is not **verified**:
        either it cannot be fixed at all, or it can be fixed but not read back
        (the 1281 has no range query in its command set, so ``DCV 1E+03`` is
        believed and nothing checks it).  Both leave the same hole above 22 V,
        and the pre-step is what fills it -- one decade at a time, with a
        reading in between.
        """
        unit = Quantity(point.quantity).unit
        value = abs(float(point.value))
        if unit == "A":
            return value > HAZARD_CURRENT_A
        if unit == "V" and value > HAZARD_VOLTAGE_V:
            return not range_verified
        return False

    def _prestep(self, point, config: MeterConfig, *, point_index: int,
                 group_index: int, range_verified: bool = False
                 ) -> MeterConfig:
        """Source a tenth of the target, prove the loop, then let the caller on.

        Returns the :class:`~caldmm.cal.roles.MeterConfig` to run the point
        with: the meter is re-configured from the nominal afterwards, because
        a meter that autoranged down at the pre-step level must be back on the
        full value's range **before** the source steps up (§9).

        Unlike the wiring proof this reads on the **point's own** range and
        needs no standby of its own to get there: the caller has already put
        the meter on that range, and a tenth of the target is not a step down
        far enough to leave it -- every shipped pairing resolves the pre-step
        there to far inside the +/-10 % window (the widest is the 34401A's
        30 mA on its 3 A range, whose limit is 2.1 % of the level).  The one
        pairing whose table states no figure at that level is the 34401A's
        **ac** current, and there its two ranges -- 1 A and 3 A -- put a 30 mA
        reading below the stated minimum on both, so there is no lower range
        to prefer.
        """
        if not self._prestep_needed(point, range_verified):
            return config
        unit = Quantity(point.quantity).unit
        level = float(point.value) * PRESTEP_FRACTION
        # Configured from the *point*, so it is the point's own range, but
        # read fast (see _configure_probe); the re-configure below restores
        # the plan's knobs before the source steps up.
        probe_config = self._configure_probe(point)
        state, reading, ok = self._probe_at(replace(point, value=level),
                                            probe_config)
        # Back on the full value's range before the source steps up, whatever
        # the pre-step level left an autoranging meter on (§9).
        config = self.meter.configure(point)
        if not ok:
            actual = float(state.actual_value)
            saw = ("nothing" if not _finite(reading.mean)
                   else f"{float(reading.mean):.6g}")
            raise SafetyAbortError(
                f"the 10 % pre-step read {saw} {unit} where {actual:g} {unit} "
                f"was sourced, more than {PROBE_TOLERANCE * 100:g} % away; "
                f"{point.value:g} {unit} was not sourced",
                flag=FLAG_PRESTEP_FAILED, group_index=group_index,
                point_index=point_index)
        # The read-back above happened before this re-configure, so it was a
        # statement about a configuration the meter is no longer on.  Ask
        # again: what is energized against must be what was read back.
        self._verify_meter_range(point, config, point_index=point_index,
                                 group_index=group_index)
        return config

    def _retarget(self, point, config: MeterConfig,
                  state: SourceState) -> MeterConfig:
        """Re-configure the meter from the readback, but only if it must.

        The range is the only reason to: the reference for the error is the
        readback either way.  A plan that pinned ``range_arg`` has already
        decided, and re-sending a range mid-point costs a settle and can only
        move the meter off the range the plan chose.
        """
        if getattr(point, "range_arg", None) is not None:
            return config
        expected = getattr(config, "range_fs", None)
        if expected is None:
            return config
        function = self.meter_spec.function(Quantity(point.quantity))
        if function is None:
            return config
        actual = float(state.actual_value)
        wanted = select_range(function, abs(actual), for_meter_config=True)
        if wanted is None or math.isclose(wanted.fs, float(expected),
                                          rel_tol=1e-9, abs_tol=0.0):
            return config
        return self.meter.configure(replace(point, value=actual))

    def _meter_temperature(self) -> float | None:
        """The meter's internal temperature, where it offers one.  Guarded.

        Read once per point, after the reading, through the *optional*
        ``internal_temperature`` capability (see
        :class:`~caldmm.cal.roles.MeterRole`).  A meter without the method,
        a meter that answers ``None`` and a query that raises all record the
        same honest blank: the temperature is provenance for the report, and
        a provenance read must never cost a measured point.
        """
        probe = getattr(self.meter, "internal_temperature", None)
        if probe is None:
            return None
        try:
            value = probe()
        except Exception:                  # noqa: BLE001 - provenance only
            return None
        if value is None:
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    def _settle_seconds(self, point, config: MeterConfig,
                        state: SourceState) -> float:
        """The larger of the two settling needs, plus the margin.

        They are independent: the calibrator's is the time its output takes to
        arrive (7 s below 20 Hz on the ac ranges), the meter's is what it needs
        after a step and a range change.  Waiting the maximum is the only rule
        that is right in both directions; a point may override both.
        """
        override = getattr(point, "settle_s", None)
        if override is not None:
            source_need = float(override)
        else:
            function = self.source_spec.function(Quantity(point.quantity))
            source_need = _settle_of(function,
                                     self._source_range(point, state))
        meter_need = float(getattr(config, "settle_s", 0.0) or 0.0)
        return max(source_need, meter_need) + self.settle_margin_s

    def _read(self, point, *, state: SourceState | None = None,
              config: MeterConfig | None = None) -> Reading:
        self._sampling_flags = ()
        n = self.plan.samples_for(point)
        discard = self.plan.discards_for(point)
        first = (n if self.min_samples is None or state is None
                 else max(1, min(self.min_samples, n)))
        if first >= n:
            if discard > 0:
                return self.meter.read(n, discard=discard)
            return self.meter.read(n)
        reading = (self.meter.read(first, discard=discard) if discard > 0
                   else self.meter.read(first))
        u_b, dut_limit = self._early_stop_basis(point, state, config,
                                                reading)
        actual = float(state.actual_value)
        while True:
            taken = reading.n + reading.overloads
            if reading.overloads == 0 and reading.n >= 1:
                ok, _why = sampling_converged(
                    reading.samples, u_b=u_b, planned=n,
                    error=float(reading.mean) - actual,
                    dut_limit=dut_limit, min_samples=self.min_samples)
                if ok:
                    if taken < n:
                        self._sampling_flags = (FLAG_SAMPLING_STOPPED_EARLY,)
                    return reading
            if taken >= n:
                self._sampling_flags = (FLAG_SAMPLING_NOT_CONVERGED,)
                return reading
            reading = _merge_readings(reading, self.meter.read(1))

    def _early_stop_basis(self, point, state: SourceState,
                          config: MeterConfig | None, reading: Reading
                          ) -> tuple[float | None, float | None]:
        """``(u_b, dut_limit)`` for :func:`sampling_converged`.

        ``u_b`` is built from the **same** components :meth:`_decide` puts
        in the budget -- the reference term (``UNCERT?`` with the meter under
        test, the meter's table with the source under test) and the meter's
        resolution -- so the stopping rule and ``U`` agree on what "small"
        is.  The budget's other Type B terms (thermal EMF, loading, tempco)
        are left out: they only make ``u_B`` larger, so leaving them out
        makes the rule stricter, never looser.  No reference term, or any
        failure, gives ``None``, which never converges -- the safe direction.
        """
        try:
            actual = float(state.actual_value)
            source_range = self._source_range(point, state, [])
            meter_range = self._meter_range(point, config, state)
            source_limit = self._limit(self.source_spec, point, actual,
                                       source_range,
                                       is_dut=not self.dut_is_meter,
                                       required=False)
            meter_limit = self._limit(self.meter_spec, point, actual,
                                      meter_range, is_dut=self.dut_is_meter,
                                      required=False)
            reference = self._reference_component(state, source_limit,
                                                  meter_limit, [])
            u_ref = float(reference.value) if reference is not None else None
            u_res = 0.0
            if reading.resolution:
                u_res = float(resolution_component(
                    "resolution", reading.resolution).value)
        except Exception:                          # noqa: BLE001 - be safe
            return None, None
        dut = meter_limit if self.dut_is_meter else source_limit
        dut_limit = (abs(float(dut.limit)) if dut is not None and dut.limit
                     else None)
        if u_ref is None or not math.isfinite(u_ref):
            return None, dut_limit
        return math.hypot(u_ref, u_res), dut_limit

    def _decide(self, point, *, point_index: int, group_index: int,
                health: SourceHealth, environment: Environment,
                state: SourceState, config: MeterConfig, reading: Reading,
                timestamp: datetime,
                meter_temp: float | None = None) -> ResultRow:
        """Limits, budget, verdict and the row, for a point that was read."""
        flags: list[str] = list(self._sampling_flags)
        source_range = self._source_range(point, state, flags)
        meter_range = self._meter_range(point, config, state)
        actual = float(state.actual_value)

        # Both tables are consulted in both directions: the DUT's supplies the
        # claim, the reference's supplies the budget, and the source's is
        # stored either way as the cross-check against its own UNCERT?.
        source_limit = self._limit(self.source_spec, point, actual,
                                   source_range, is_dut=not self.dut_is_meter,
                                   required=False)
        meter_limit = self._limit(self.meter_spec, point, actual, meter_range,
                                  is_dut=self.dut_is_meter,
                                  required=self.dut_is_meter)

        budget, correction, reference = self._budget(
            point=point, state=state, reading=reading,
            environment=environment, source_limit=source_limit,
            meter_limit=meter_limit, source_range=source_range,
            meter_range=meter_range, flags=flags, meter_temp=meter_temp)
        has_reference = reference is not None

        # error = meter mean - source actual, in BOTH directions.  Only which
        # side's specification supplies the limit changes with dut_role.
        mean = float(reading.mean) + correction
        error = mean - actual

        limit: LimitResult | float | None
        window_given = False
        if self.dut_is_meter:
            limit = meter_limit
            window_given = limit is not None
            reference_value = actual
        else:
            uncert_limit = (state.uncertainty.absolute(actual)
                            if state.uncertainty is not None else None)
            # A *positive* figure only.  ``UNCERT?`` answers in whatever unit
            # suits the output -- "PPM, PCT, V, A, OHM, etc." (OM 1996 lines
            # 9055-9056) -- and at a **zero setpoint the bench unit answers
            # the range's absolute floor in volts**: ``UNCERT? +7.5000E-07,V,1``
            # at ``OUT 0 V`` on the 220 mV range,
            # which
            # is that range's 24-hour floor.  So this is the live path and the
            # fallback below is defensive: it catches a source that answers a
            # *relative* figure at a zero setpoint, where ppm of zero is 0.0
            # -- and a tolerance of zero is not a tight claim but the absence
            # of one, since no measurement can lie inside +/-0.  The table
            # carries the floor for exactly that case (0.8 uV on the dc 220 mV
            # range at 1 y, 50 uOhm on the short), and a generated source-DUT
            # plan puts a zero on every dc range.
            #
            # Deliberately *not* max(uncert_limit, source_limit.limit).  The
            # model file declares ``limits_authority: instrument``: a positive
            # UNCERT? is the claim under test, computed by the firmware from
            # the unit's own constants, CAL_INTV and CAL_CONF.  Taking whichever
            # of the two is looser would silently widen the claim -- most
            # plausibly when the unit's CAL_INTV is not the plan's interval,
            # which is the very disagreement ``ResultRow.limit_disagree``
            # exists to surface rather than absorb.
            if uncert_limit:
                limit = uncert_limit
            elif source_limit is not None:
                flags.append(FLAG_LIMIT_FROM_SPEC_TABLE)
                # ... at the interval the *instrument* computes UNCERT? for,
                # where it names one.  The claim under test at every other
                # point of the run is UNCERT? at the unit's own CAL_INTV -- on
                # the bench unit, 24 hours -- so taking the zero points'
                # floor from the plan's column would put two different claims
                # in one certificate (M64).
                fallback = self._claim_limit(point, actual, source_range,
                                             state, source_limit)
                limit = fallback.limit
            else:
                # The source states no figure for this output and its table
                # has no row for it either: there is no claim to test, which
                # is a NO_SPEC row and not a failed point.
                limit = None
            reference_value = mean

        if not has_reference:
            # No stated reference uncertainty means this comparison cannot
            # support a statement of conformity at any confidence.  The data is
            # recorded; the claim is not made.
            flags.append(FLAG_NO_REFERENCE_UNCERTAINTY)
            limit = None
            window_given = False

        # The calibrator under test, read outside its full-spec load window:
        # the claim at *this* load is the stated figure plus the manual's
        # derating (H06).  Its resistance figure -- from UNCERT? or from the
        # table, both -- is written for loads inside IL-IU only, and the
        # meter's test current is not something the calibrator can know, so
        # judging a point read below IL against the undecorated figure tests a
        # claim Fluke never made.  The plan computed the number; here it
        # becomes the tolerance the verdict is taken against.  Nothing goes
        # into the budget in this direction -- the reference is the *meter* --
        # which is why one effect cannot land twice.  After the check above,
        # so that a row that ends up with no limit does not also claim a
        # derating was applied to one.
        if not self.dut_is_meter and limit is not None:
            adder = _load_adder(point)
            if adder:
                limit = float(limit) + adder
                flags.append(FLAG_LOAD_DERATED)

        dut_spec = self.meter_spec if self.dut_is_meter else self.source_spec
        dut_window = self._window_for(dut_spec, is_dut=True)
        verdict_temperature = environment.ambient_for_verdict
        if self.dut_is_meter and self._meter_window(point) is not None:
            # Judged on the meter's internal TEMP? against its own window:
            # CAL? 59/60 +/- 5 C (+/- 1 C at 24 h) dc, last ACAL +/- 1 C ac.
            dut_window = self._meter_window(point)
            verdict_temperature = meter_temp
        kwargs: dict[str, Any] = {}
        if dut_window is not None or not window_given:
            # Passed explicitly whenever this engine can resolve it, because
            # a LimitResult carries only the *absolute* window its model file
            # declares -- and the instrument this project exists to test
            # declares a relative one, centred on a temperature only the
            # instrument knows (M50/M62/M78).
            kwargs["temperature_window_c"] = dut_window

        conditions = self._unmet_conditions(health)
        conditions += self._reference_temperature_conditions(environment)
        conditions += self._point_conditions(point, limit, source_limit,
                                             meter_limit)
        if self.extra_conditions is not None:
            conditions += list(self.extra_conditions(point, meter_temp,
                                                     timestamp))
        conformity = evaluate(
            error, limit, budget,
            conditions=conditions,
            rule=self.rule,
            confidence=self.plan.confidence,
            guard_factor=self.plan.guard_band_factor,
            tur_floor=self.plan.tur_floor,
            ambient_c=verdict_temperature,
            temperature_policy=self.plan.temperature_policy,
            unit=Quantity(point.quantity).unit,
            **kwargs)

        self._check_gross_error(conformity, point, point_index=point_index,
                                group_index=group_index, reference=reference,
                                flags=flags)

        return self._row(
            point, point_index=point_index, group_index=group_index,
            health=health, environment=environment, state=state,
            config=config, reading=reading, timestamp=timestamp,
            conformity=conformity, budget=budget,
            reference_value=reference_value,
            spec_limit=None if source_limit is None else source_limit.limit,
            flags=flags, meter_temp=meter_temp)

    def _point_conditions(self, point, limit, source_limit, meter_limit
                          ) -> list[tuple[str, str]]:
        """Conditions of the claim that are properties of this one point.

        * The DUT's figure is **typical** performance, not a specification:
          no conformity statement can be made against it, either way.
        * A 2-wire resistance reading whose unmeasured lead resistance
          (bounded by :data:`LEAD_RESISTANCE_BOUND_OHM`) is more than a tenth
          of the tolerance: the reading is not of the DUT alone.
        """
        conditions: list[tuple[str, str]] = []
        dut = meter_limit if self.dut_is_meter else source_limit
        range_spec = getattr(dut, "range_spec", None)
        if range_spec is not None and getattr(range_spec, "typical", False):
            conditions.append((
                FLAG_DUT_TYPICAL_ONLY,
                f"the DUT's figure on its {range_spec.fs:g} "
                f"{Quantity(point.quantity).unit} range is typical "
                f"performance, not a specification"))
        if Quantity(point.quantity) is Quantity.RES \
                and Connection(point.connection) is Connection.TWO_WIRE \
                and self.lead_resistance_ohm is None:
            tolerance = getattr(limit, "limit", limit)
            if tolerance is not None and _finite(tolerance) \
                    and LEAD_RESISTANCE_BOUND_OHM > 0.1 * abs(float(tolerance)):
                conditions.append((
                    FLAG_LEAD_RESISTANCE_UNKNOWN,
                    f"the 2-wire reading includes the test leads (up to "
                    f"{LEAD_RESISTANCE_BOUND_OHM:g} ohm), material against a "
                    f"{float(tolerance):.3g} ohm tolerance; state the measured "
                    f"lead resistance to correct it"))
        return conditions

    def _unmet_conditions(self, health: SourceHealth) -> list[tuple[str, str]]:
        """Stated conditions of the reference's specification that do not hold.

        The owner's ruling, and JCGM 106 read plainly: a specification holds
        under its stated conditions, so a row taken outside them is
        ``INDETERMINATE`` -- never a ``PASS`` (which would be a claim the
        reference's uncertainty does not cover) and never a counted ``FAIL``.
        Both figures below are the calibrator's own prerequisites, not
        house rules: "Specifications are valid after a warm-up period of twice
        the time the calibrator has been turned off, up to a maximum of 30
        minutes" and "a dc zeros calibration must be performed at least every
        30 days" (``5700a-operators-manual-1996.txt`` lines 1103-1105 and
        1084-1086, PDF pp. 30 and 29).

        The temperature is a condition too, but the verdict module owns that
        one: it is the module that knows the window and the policy.
        """
        conditions: list[tuple[str, str]] = []
        warm = health.warm_minutes
        if warm is not None and warm < WARMUP_MINUTES:
            conditions.append((
                FLAG_SOURCE_NOT_WARM,
                f"the source had been warm for {warm:g} min of the "
                f"{WARMUP_MINUTES} its specifications require"))
        days = health.days_since_zero
        if days is not None and days > ZERO_INTERVAL_DAYS:
            conditions.append((
                FLAG_SOURCE_ZEROS_DUE,
                f"the source's dc zeros are {days:g} days old and its "
                f"specifications require them every {ZERO_INTERVAL_DAYS}"))
        return conditions

    def _reference_temperature_conditions(
            self, environment: Environment) -> list[tuple[str, str]]:
        """The **reference**'s own temperature window, as a condition.

        The verdict module gates on the *DUT*'s window; the reference has one
        too, and a reference outside it is a reference whose stated
        uncertainty does not cover the point (M56).  For the 5700A that window
        is ``CAL_TEMP? +/- 5 C`` -- on the bench unit, 16.1 to 26.1 C -- and
        it has no ambient input of its own, so ``UNCERT?`` cannot be carrying
        the adder either.

        Silent where the reference declares no window, or where its relative
        window has no centre in hand: an unchecked condition is reported as
        nothing, never as met.
        """
        reference_spec = (self.source_spec if self.dut_is_meter
                          else self.meter_spec)
        window = self._window_for(reference_spec, is_dut=False)
        if window is None:
            return []
        low, high = window
        ambient = environment.ambient_for_verdict
        if ambient is None:
            return [(FLAG_AMBIENT_UNKNOWN,
                     f"the ambient temperature was not recorded and the "
                     f"reference's own figures hold only over {low:g} to "
                     f"{high:g} C")]
        if low <= float(ambient) <= high:
            return []
        return [(FLAG_REFERENCE_OUT_OF_WINDOW,
                 f"the reference ({reference_spec.model_id}) was at "
                 f"{float(ambient):g} C, outside the {low:g} to {high:g} C "
                 f"window its own figures are written for")]

    def _no_decision(self, point, environment: Environment,
                     reason: str) -> ConformityResult:
        """A ``NO_SPEC`` result for a point that never reached a decision.

        Built here rather than by :func:`~caldmm.cal.verdict.evaluate`, which
        needs a finite error and therefore a reading.  Every decision field is
        ``nan`` -- which :class:`~caldmm.cal.result.ResultRow` stores as empty
        -- so the row says "no claim" instead of carrying a zero somebody could
        take for a number.
        """
        dut_spec = self.meter_spec if self.dut_is_meter else self.source_spec
        window = self._window_for(dut_spec, is_dut=True)
        return ConformityResult(
            verdict=Verdict.NO_SPEC,
            error=math.nan, tolerance_limit=math.nan,
            expanded_uncertainty=math.nan, coverage_factor=math.nan,
            effective_dof=math.nan, tur=None, guard_band=math.nan,
            acceptance_limit=math.nan, margin=math.nan,
            rule="", reason=reason, confidence=self.plan.confidence,
            unit=Quantity(point.quantity).unit,
            temperature_status=classify_temperature(
                environment.ambient_for_verdict, window),
        )

    def _row(self, point, *, point_index: int, group_index: int,
             health: SourceHealth, environment: Environment,
             state: SourceState | None, config: MeterConfig | None,
             reading: Reading | None, timestamp: datetime,
             conformity: ConformityResult, budget: UncertaintyBudget,
             reference_value: float | None, spec_limit: float | None,
             flags: Sequence[str],
             meter_temp: float | None = None) -> ResultRow:
        """Assemble the row and append the engine's own flags to the verdict's."""
        row = ResultRow.from_measurement(
            point=point, conformity=conformity, budget=budget,
            state=state, reading=reading, config=config,
            environment=environment,
            source_model=self.source_spec.model_id,
            meter_model=self.meter_spec.model_id,
            run_id=self.run_id, point_index=point_index,
            group_index=group_index, reference_value=reference_value,
            spec_limit=spec_limit, timestamp=timestamp,
            meter_temp_c=meter_temp)
        # Whatever a between-points rule left for the next row, taken exactly
        # once: a second row must not inherit it.
        pending, self.pending_flags = self.pending_flags, ()
        note, self.pending_note = self.pending_note, ""
        if note:
            row = replace(row, note="; ".join(part for part in
                                              (row.note, note) if part))
        extra = list(flags) + list(pending) \
            + self._condition_flags(health, state, reading)
        if not extra:
            return row
        keep = [f for f in extra if f not in row.flags]
        return replace(row, flags=tuple(row.flags) + tuple(keep))

    def _condition_flags(self, health: SourceHealth, state: SourceState | None,
                         reading: Reading | None) -> list[str]:
        """What a report must print about the conditions this point was taken in."""
        flags: list[str] = []
        if health.warm_minutes is not None \
                and health.warm_minutes < WARMUP_MINUTES:
            flags.append(FLAG_SOURCE_NOT_WARM)
        if health.days_since_zero is not None \
                and health.days_since_zero > ZERO_INTERVAL_DAYS:
            flags.append(FLAG_SOURCE_ZEROS_DUE)
        if state is not None and not state.operate:
            flags.append(FLAG_SOURCE_NOT_OPERATE)
        if state is not None and not state.settled:
            flags.append(FLAG_SOURCE_NOT_SETTLED)
        if reading is not None and reading.overloads:
            flags.append(FLAG_OVERLOAD_SAMPLES)
        if self._environment_error is not None:
            flags.append(FLAG_AMBIENT_READ_FAILED)
        return flags


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def _de_energize(source: SourceRole) -> str:
    """Program the source's zero, then standby.  Returns any zero failure.

    Standby alone is not the manual's procedure: "Placing the instrument in
    standby may not be enough to avoid shock hazard, since the OPR key could be
    pressed accidentally.  Press reset and verify that standby is lit before
    making connections" and "press RESET, or 0 V ENTER.  Either action sets the
    calibrator to 0 mV in standby" (``5700a-operators-manual-1996.txt``
    lines 4141-4146 and 4311-4313, PDF pp. 99 and 102).  Without the zero, a
    group boundary hands the operator a pair of leads with up to 1100 V still
    *selected*, one key press away.

    ``zero_output`` is an **optional** capability (see
    :class:`~caldmm.cal.roles.SourceRole`): a source that does not offer one
    is de-energized exactly as before.  A zero that raises is reported, never
    fatal -- ``standby()`` is what protects the operator and it must run
    whatever else went wrong, including from a ``finally`` unwinding another
    exception.
    """
    error = ""
    zero = getattr(source, "zero_output", None)
    if zero is not None:
        try:
            zero()
        except Exception as exc:               # noqa: BLE001 - reported, not fatal
            error = f"{type(exc).__name__}: {exc}"
    source.standby()
    return error


def _de_energize_before_stopping(source: SourceRole
                                 ) -> "tuple[Exception | None, str]":
    """De-energize on the way to an abort, **before** the row is handed out.

    A generator stops at its ``yield``: everything after one runs only when
    the consumer comes back for the next row, and the consumer of a run is a
    runner that writes the row, prints a line and may prompt.  Handing out an
    abort row with the setpoint still live -- up to 1100 V, for a gross error
    that says the leads are not where the plan thinks they are -- would put
    the whole of that between the decision to stop and the output going off.
    So the output goes off first and the row is the second thing that happens.

    Returns ``(standby failure, zero failure)`` instead of raising either: the
    row must still reach the file, and the caller re-raises once it has.  The
    zero is the *second* half of the manual's procedure -- standby alone
    leaves up to 1100 V still selected, one key press away -- so a zero that
    did not take belongs in the message the operator reads, not in a variable
    nobody looks at.
    """
    try:
        zero_error = _de_energize(source)
    except Exception as exc:                   # noqa: BLE001 - re-raised later
        return exc, ""
    return None, zero_error


def _with_zero_failure(exc: EngineError, zero_error: str) -> EngineError:
    """Add a failed ``zero_output`` to an abort's own message.

    The same exception, so the flag and the indices a runner resumes from
    survive; only the words grow.
    """
    if not zero_error:
        return exc
    message = (f"{exc}  (The setpoint could not be programmed to zero on the "
               f"way out: {zero_error}.  The output is in standby but the "
               f"last value may still be *selected* -- press RESET and verify "
               f"the standby light before touching the terminals.)")
    exc.args = (message,) + tuple(exc.args[1:])
    return exc


def _standby_failed(exc: Exception) -> EngineError:
    """The error for a source that would not confirm standby."""
    return EngineError(
        f"the source did not confirm standby ({type(exc).__name__}: {exc}); "
        f"THE OUTPUT MAY STILL BE LIVE.  Press RESET on the front panel and "
        f"verify the standby light before touching the terminals")


def _check_health(health: SourceHealth, *, group_index: int,
                  point_index: int) -> None:
    """Refuse to measure through a tripped or faulted source.

    Both conditions make the *following* points meaningless, not just this one:
    a trip means the output is no longer energized, and a queued fault silently
    inhibits ``OPER`` for every output of 22 V and over -- so the run would go
    on recording readings taken through a de-energized calibrator that look
    like a catastrophic failure of the DUT.
    """
    if health.tripped:
        raise SourceFaultError(
            f"the source tripped to standby before point {point_index}; "
            f"readings taken through it would not be of the point that was "
            f"asked for.  Clear the cause and resume with "
            f"start_group={group_index}",
            group_index=group_index, point_index=point_index, health=health)
    if health.faults:
        listed = "; ".join(f"{code}: {text}" for code, text in health.faults)
        raise SourceFaultError(
            f"the source has queued fault(s) before point {point_index} "
            f"({listed}); a queued fault silently inhibits operate above 22 V.  "
            f"Clear the cause and resume with start_group={group_index}",
            group_index=group_index, point_index=point_index, health=health)


def run_plan(
    plan: TestPlan,
    source: SourceRole,
    meter: MeterRole,
    *,
    environment: EnvironmentProbe | None = None,
    source_spec: ModelSpec | str | None = None,
    meter_spec: ModelSpec | str | None = None,
    run_id: str = "",
    start_group: int = 0,
    start_point: int = 0,
    rule: DecisionRule | str = DecisionRule.GUARDBANDED,
    settle_margin_s: float = DEFAULT_SETTLE_MARGIN_S,
    environment_interval_s: float = DEFAULT_ENVIRONMENT_INTERVAL_S,
    loading_relative_uncertainty: float = DEFAULT_LOADING_RELATIVE_UNCERTAINTY,
    thermal_emf: float | None = None,
    reference_tempco_ref_c: float | None = None,
    reference_traceability: Traceability | None = None,
    min_samples: int | None = None,
    meter_temperature_windows: Mapping | None = None,
    lead_resistance_ohm: float | None = None,
    extra_conditions: Callable | None = None,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    on_group: Callable[[GroupBoundary], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = _now,
) -> Iterator[ResultRow]:
    """Execute *plan* against *source* and *meter*, yielding one row per point.

    Parameters
    ----------
    plan:
        What to run, and the decision rule's parameters -- ``interval``,
        ``confidence``, ``tur_floor``, ``guard_band_factor`` and
        ``temperature_policy`` are taken from it, because they are properties
        of the claim being tested rather than of any one point.
    source, meter:
        Concrete :class:`~caldmm.cal.roles.SourceRole` and
        :class:`~caldmm.cal.roles.MeterRole` objects.  They are handed in, not
        constructed: that is what keeps this module free of drivers.
    environment:
        An :class:`~caldmm.cal.roles.EnvironmentProbe`.  **Its own parameter
        on purpose**: ``EnvironmentProbe`` and ``MeterRole`` both expose
        ``read``, so ``isinstance(meter, EnvironmentProbe)`` is ``True`` and no
        amount of protocol checking can find a probe that was not passed.
        Without one, every point of a model that declares a temperature window
        (the 34401A and the 1281 both declare 18-28 C) is ``INDETERMINATE`` --
        that is the design, and the fix is a probe, not
        ``temperature_policy='flag'``.
    source_spec, meter_spec:
        Override the specification each side is judged against.  By default the
        role's own ``spec`` is used where it has one, else the plan's model id.
    start_group:
        Skip this many wiring groups.  A run interrupted at point 140 resumes
        with the ``group_index`` its last row (or a
        :class:`SourceFaultError`) carries, and
        :func:`caldmm.cal.result.open_results` with ``append=True`` reopens
        the same file.
    start_point:
        Offset **within** ``start_group`` of the first point to measure.  A run
        interrupted part-way through a group otherwise has only two options,
        and both are wrong: re-measure the points it already has (duplicate
        rows, which double-count in every summary and on a certificate) or skip
        to the next group (points silently never taken).  The group boundary is
        unaffected -- the source is still put in standby and ``on_group`` still
        fires, because the wiring belongs to the group and not to where in it
        the run resumes.
    reference_traceability:
        A :class:`~caldmm.cal.specs.Traceability` overriding the meter model's
        own, for a reference whose certificate states a figure different from
        the factory's -- which the 3458A's data sheet says it will: "When you
        recalibrate the 3458A, your actual traceability error will depend upon
        the errors from your calibration standards"
        (``3458a-datasheet.txt:282-287``).  Used only when the meter is the
        reference.
    rule, settle_margin_s, thermal_emf, reference_tempco_ref_c:
        The decision rule; the margin added to the longer of the two settling
        times; the bound on thermal EMF in the loop, in volts, which is the
        dominant term at a short or a zero -- ``None`` takes
        :data:`DEFAULT_THERMAL_EMF_V` and flags the row as having assumed it,
        ``0`` states that there is none; and **the reference's own
        calibration temperature** -- ``Tcal`` -- when its model file cannot
        state one, which is the centre of its relative temperature window and
        the edge its tempco is an adder beyond.  For a 3458A that is the
        external-calibration temperature; its data sheet's with-``ACAL``
        coefficients are "additional error from Tcal +/- 5 C"
        (``3458a-datasheet.txt:445-449``, PDF p.12), so the number to pass is
        the one on its certificate, with ``TEMP?`` after an ACAL as the
        practical stand-in when that is all there is.
    on_group:
        Called with a :class:`GroupBoundary` before each group, **with the
        source already programmed to zero and in standby**, so the runner can
        prompt the operator to re-cable.  Raising from it aborts the run with
        the output de-energized.  :attr:`GroupBoundary.zero_error` is non-empty
        when the zero itself failed, which the prompt must repeat: the output
        is off, but the last setpoint is still selected.
    sleep, clock:
        Injected for testing; a test suite that actually slept for 200 points
        would not be run.

    Yields
    ------
    :class:`~caldmm.cal.result.ResultRow`
        One per attempted point, in plan order, carrying its own timestamp --
        :class:`~caldmm.cal.result.ResultWriter` **rejects** a row without one
        rather than substituting the file-write clock.

    Raises
    ------
    SourceFaultError
        The source tripped or had a queued fault, before a point or during
        one.  The run stops; see :func:`_check_health`.
    SafetyAbortError
        A safety rule stopped the run: the wiring proof, the meter's range
        read-back, the 10 % pre-step or the gross-error abort.  The row saying
        why has already been yielded.
    EngineError
        A run-level failure: bad arguments, a meter that cannot honour the
        plan's discards, :data:`DEFAULT_MAX_CONSECUTIVE_FAILURES` consecutive
        per-point failures (a pulled cable, not a bad point), or a source that
        did not confirm standby on the way out -- which means the output may
        still be live and is never reported as a clean stop.

    Notes
    -----
    **The failure rule.**  A point that cannot be measured -- the source
    refuses it, the meter cannot be configured, the reading raises, every
    sample is a sentinel -- is *recorded* as a row with
    :data:`FLAG_POINT_FAILED`, the exception in ``Reason`` and no verdict, and
    the run continues.  Three things stop a run instead: a source trip or
    fault (which makes every later point meaningless),
    *max_consecutive_failures* failures in a row (which is a cable, not a
    point), and a **safety rule** -- a failed wiring proof, a meter range that
    does not read back, a failed pre-step, or an error above
    :data:`GROSS_ERROR_FACTOR` times the point's tolerance -- which raises
    :class:`SafetyAbortError` *after* writing the row that says why.  The
    run's rows are always a complete account of what was attempted.

    **On abort.**  The source is put in standby from a ``finally``, so
    ``close()``, ``break``, Ctrl-C and any exception all leave the output
    de-energized -- and so does every group boundary, before the operator is
    asked to touch anything.

    Argument checking happens **when this is called**, not on the first
    ``next()``: a bad ``start_group`` should be a traceback at the desk, not a
    surprise once the operator has walked away and the run has begun.
    """
    source_model = _resolve_spec(source_spec, source, plan.source_model_id)
    meter_model = _resolve_spec(meter_spec, meter, plan.meter_model_id)

    groups = plan.groups()
    start_group = int(start_group)
    start_point = int(start_point)
    if start_group < 0 or start_group > len(groups):
        raise EngineError(
            f"start_group must be between 0 and {len(groups)} for a plan with "
            f"{len(groups)} group(s), got {start_group}")
    if start_point < 0:
        raise EngineError(f"start_point must not be negative, got {start_point}")
    if start_group < len(groups) and start_point > len(groups[start_group]):
        raise EngineError(
            f"start_point {start_point} is past the end of group "
            f"{start_group}, which has {len(groups[start_group])} point(s); "
            f"pass start_group={start_group + 1} to skip it entirely")
    if start_point and start_group >= len(groups):
        raise EngineError(
            f"start_point={start_point} has no group to be an offset into: "
            f"start_group={start_group} is past the last group")
    if int(max_consecutive_failures) < 1:
        raise EngineError("max_consecutive_failures must be >= 1")
    if not _accepts_discard(meter.read) and any(plan.discards_for(point) > 0
                                                for point in plan.points):
        raise EngineError(
            f"the plan discards the first sample(s) of a point but "
            f"{meter_model.model_id}'s read() takes no discard= argument; the "
            f"first reading after a range change would be averaged in")

    executor = _Executor(
        plan, source, meter, source_spec=source_model, meter_spec=meter_model,
        probe=environment, run_id=run_id, rule=rule,
        settle_margin_s=settle_margin_s,
        environment_interval_s=environment_interval_s,
        loading_relative_uncertainty=loading_relative_uncertainty,
        thermal_emf=thermal_emf,
        reference_tempco_ref_c=reference_tempco_ref_c,
        reference_traceability=reference_traceability,
        min_samples=min_samples,
        meter_temperature_windows=meter_temperature_windows,
        lead_resistance_ohm=lead_resistance_ohm,
        extra_conditions=extra_conditions,
        sleep=sleep, clock=clock)
    return _iterate(executor, groups, start_group=start_group,
                    start_point=start_point,
                    max_consecutive_failures=int(max_consecutive_failures),
                    on_group=on_group)


def _iterate(executor: "_Executor", groups: Sequence[PointGroup], *,
             start_group: int, start_point: int, max_consecutive_failures: int,
             on_group: Callable[[GroupBoundary], None] | None
             ) -> Iterator[ResultRow]:
    """The run itself.  Separate so :func:`run_plan` can validate eagerly."""
    source = executor.source
    # How much of the plan this run will never attempt, fixed at the start:
    # what a resume skipped, not how far the run has got.
    skipped = (groups[start_group].start + start_point
               if start_group < len(groups) else 0)
    failures = 0
    try:
        for index in range(start_group, len(groups)):
            group = groups[index]
            # Only the group the run was resumed *into* starts part-way; every
            # later group starts at its own first point.
            first = start_point if index == start_group else 0
            # Zero and standby *before* the prompt: the boundary is where the
            # operator re-cables, and this is the only place in the subsystem
            # where a live output could reach a pair of hands.
            zero_error = _de_energize(source)
            executor.live_value = 0.0
            if on_group is not None:
                on_group(GroupBoundary(index=index, total=len(groups),
                                       group=group, start=group.start,
                                       skipped=skipped, offset=first,
                                       zero_error=zero_error))
            executor.environment(force=True)

            # Every group start -- including the one a run was resumed into,
            # where the operator has just re-cabled -- proves its wiring
            # before anything is measured through it (§8).
            proof_due, proof_reason = True, ("run start" if index == start_group
                                             else "group start")
            proved_range: RangeSpec | None = None

            for offset, point in enumerate(group.points[first:], start=first):
                point_index = group.start + offset
                health = source.health()
                _check_health(health, group_index=index,
                              point_index=point_index)
                # One ambient snapshot for the proof and the point it guards:
                # they are the same moment, and the probe is a shared database
                # connection, not a free reading.
                environment = executor.environment()
                source_range = executor.planned_source_range(point)
                if proved_range is not None and source_range is not None \
                        and source_range.range_id != proved_range.range_id:
                    proof_due, proof_reason = True, "source range change"
                # A point that cannot carry a proof -- the ohms short, which
                # reads near zero with the leads in the air -- does not
                # *consume* the one that is due: it stays due for the next
                # point that can.
                if proof_due and executor.proof_level(point) is None:
                    # Two different ``None``s.  The ohms short proves nothing
                    # and does not *consume* the proof; a level this meter
                    # cannot read means there is no proof to be had here at
                    # all, which is recorded on the row and left at that (§8,
                    # as amended 2026-08-22).
                    unreadable = executor.proof_note(point)
                    if unreadable:
                        executor.skip_wiring_proof(point, unreadable)
                        proof_due = False
                        proved_range = source_range
                elif proof_due:
                    failure = executor.wiring_proof(
                        point, point_index=point_index, group_index=index,
                        health=health, environment=environment,
                        reason=proof_reason)
                    proof_due = False
                    proved_range = source_range
                    if failure is not None:
                        standby_failed, zero_error = \
                            _de_energize_before_stopping(source)
                        yield failure
                        if standby_failed is not None:
                            raise _standby_failed(standby_failed)
                        raise _with_zero_failure(executor.pending_abort,
                                                 zero_error)
                row = executor.measure(
                    point, point_index=point_index, group_index=index,
                    health=health, environment=environment)
                if executor.pending_abort is not None:
                    # Off *first*, then the row: a generator stops at its
                    # yield, and the consumer's write-print-prompt would
                    # otherwise all happen with the setpoint still live.
                    standby_failed, zero_error = \
                        _de_energize_before_stopping(source)
                    yield row
                    if standby_failed is not None:
                        raise _standby_failed(standby_failed)
                    raise _with_zero_failure(executor.pending_abort,
                                             zero_error)
                yield row
                if FLAG_POINT_FAILED in row.flags:
                    failures += 1
                    if failures >= max_consecutive_failures:
                        raise EngineError(
                            f"{failures} consecutive points failed, ending at "
                            f"point {point_index} "
                            f"({row.label or row.quantity.name}); this is an "
                            f"instrument or a cable, not a point.  Resume with "
                            f"start_group={index}")
                else:
                    failures = 0
    finally:
        # Runs on close(), on Ctrl-C, on break and on any exception: a
        # generator that could leave 1000 V on the terminals because its
        # consumer stopped iterating would be the worst bug in this project.
        # The setpoint is programmed to zero as well, so the front panel a
        # ``close()`` hands back to local carries 0 mV and not the last point.
        try:
            _de_energize(source)
        except Exception as exc:                       # noqa: BLE001 - louder
            # A source adapter that *verifies* standby raises when the output
            # did not go off.  That must never be swallowed on the way out --
            # a live output reported as a clean stop is the one failure mode
            # this whole module is arranged to prevent -- so it replaces
            # whatever was propagating, and says what to do about it.
            raise _standby_failed(exc) from exc

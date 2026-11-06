"""Conformity decision: test uncertainty ratio, guard bands and the verdict.

This module turns a measured **error** plus a **GUM uncertainty budget** into a
statement of conformity, following **JCGM 106:2012** (the role of measurement
uncertainty in conformity assessment) and **ILAC-G8:2019** (guidelines on
decision rules and statements of conformity).

Why the signature is what it is
-------------------------------

:func:`evaluate` takes an :class:`~caldmm.cal.uncertainty.UncertaintyBudget`,
never a bare uncertainty number.  That is the whole architectural point: it is
structurally impossible to reach a verdict without having built an itemised
budget first, so every ``PASS`` in a report is backed by a table that says
where its uncertainty came from.  There is deliberately **no** convenience
overload taking a float -- ``evaluate(err, tl, 12e-6)`` would be a one-line
route around the only discipline this subsystem enforces.

The three zones
---------------

For a symmetric tolerance ``+/-TL`` and an expanded uncertainty ``U`` at the
decision confidence, a guard band ``w = g*U`` (JCGM 106 (13); ``g = 1`` by
default) gives an acceptance limit ``A = TL - w`` (JCGM 106 (14)).  The default
rule then reports three zones rather than two:

===========================  ===============================================
``|error| <= A``             ``PASS``   -- conformity demonstrated
``A < |error| <= TL``        ``INDETERMINATE`` -- inside tolerance but inside
                             the guard band: no statement at this confidence
``|error| > TL``             ``FAIL``   -- non-conformity demonstrated
===========================  ===============================================

Terminology, precisely: the *arithmetic* is JCGM 106 8.3.3 "guarded acceptance"
with ``w = U``, for which the specific consumer's risk at the acceptance limit
is exactly ``(1-p)/2``, i.e. **2.5 % at p = 0.95**: a true value must sit
``w/u = k = t_{(1+p)/2}(nu_eff)`` standard uncertainties beyond the measured
value to cross ``TL``, and that is the same quantile the guard band was built
from, so the tail is exactly the one the confidence level names -- whatever
``nu_eff`` is.  (The often-quoted 2.3 % is the ``k = 2`` normal approximation,
which is *not* what this module does: ``k`` is ``t`` at ``nu_eff``.)
The *reporting* of a middle zone is ILAC-G8:2019 4.2.3, the **non-binary**
statement with a guard band -- ILAC-G8 4.2.2, the binary form, forces every
point into pass or fail and has no indeterminate zone.  Calling the three-zone
rule "the binary rule" is a common slip; it is written down here because the
difference is exactly what a middle zone is for.

The TUR floor
-------------

``TUR = TL / U`` (ANSI/NCSL Z540.3-2006 3.11, whose denominator is ``U`` at
95 %).  **When ``TUR < tur_floor`` (default 4.0) the verdict may not be
``PASS``**; it is downgraded to ``INDETERMINATE``.  So is a decision taken with
**no budget at all**: an empty :class:`~caldmm.cal.uncertainty.UncertaintyBudget`
has ``U = 0``, which would otherwise make the floor inert and hand out a ``PASS``
to the one input that least deserves one.

This is the reason the module exists.  Verifying a Fluke 5700A at 10 kohm
against an HP 3458A is a 14 ppm tolerance against a reference whose own 1-year
specification is 10.5 ppm: a TUR near 1.  Most ac points are worse.  Without a
floor, a tool like this becomes a PASS-stamp generator, certifying comparisons
that are arithmetically incapable of supporting the claim they print.

The floor is **asymmetric on purpose**: it blocks ``PASS``, not ``FAIL``.  An
error that exceeds ``TL`` even when measured with a poor reference is still a
real, defensible failure -- the uncertainty would have to be wrong, not merely
large, for it to vanish.

Stated conditions are the *other* case, and they are symmetric
--------------------------------------------------------------

Where the TUR floor says "the measurement is coarse", an unmet **condition of
the specification** says "this specification is not in force here": the source
had not warmed up, its dc zeros are overdue, the ambient is outside the window
the figures are written for, or nobody recorded it.  A limit that does not
apply cannot be exceeded, so such a point is ``INDETERMINATE`` whichever way
the error went -- it is never a ``PASS`` and never a counted ``FAIL``.  That is
JCGM 106 and ILAC-G8 4.2 read plainly: a statement of conformity presupposes
the conditions of the specification it is made against.  The caller supplies
them as ``conditions=[(code, sentence), ...]``; the temperature gate is one of
them, and every one is flagged so a report can name it.

Temperature: "we do not know" is not "fine"
-------------------------------------------

:meth:`caldmm.cal.specs.TemperatureSpec.within_window` returns ``True`` when
the model declares no window, so an unrecorded ambient silently reads as "in
spec".  The 3458A and the 5700A both legitimately have no absolute window --
their figures are relative to a stored calibration temperature -- so that
default cannot simply be inverted.  :func:`classify_temperature` therefore
splits the four cases apart, and the default ``temperature_policy='gate'``
blocks ``PASS`` only where a stated condition exists and cannot be shown to
have been met (:data:`TemperatureStatus.OUT_OF_WINDOW`,
:data:`TemperatureStatus.AMBIENT_UNKNOWN`).  Where no absolute window exists,
the governing effect is the tempco relative to the instrument's own ``Tcal``,
which belongs in the *budget*
(:func:`caldmm.cal.uncertainty.tempco_component`) and not in this decision --
but the result is still flagged so a report cannot omit it.  ``'strict'``
escalates that case too; ``'flag'`` blocks nothing.

Boundary inclusivity
--------------------

Pinned, because this is exactly what silently differs between implementations
(and see :attr:`ConformityResult.pass_limit`):

* ``|error| <= pass_limit`` is ``PASS``  -- the pass boundary is **inclusive**.
* ``|error| > fail_limit`` is ``FAIL``   -- the fail boundary is **exclusive**,
  i.e. an error landing exactly on ``TL`` under the default rule is
  ``INDETERMINATE``, not ``FAIL``.

Comparisons are exact; no epsilon is applied.  A tolerance would make the
decision unauditable ("it passed because it was within 1e-12 of the limit") to
buy protection against a coincidence -- a measured error landing bit-exactly on
a limit -- that does not occur outside a test suite.

Import hygiene
--------------

Pure functions and frozen dataclasses.  No I/O, no instrument access, no file
reads.  Imports are the stdlib, :mod:`caldmm.cal.uncertainty` and
:mod:`caldmm.cal.specs` -- never a driver.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence

from .specs import LimitResult
from .uncertainty import UncertaintyBudget

__all__ = [
    "VerdictError",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_GUARD_FACTOR",
    "DEFAULT_TUR_FLOOR",
    "TEMPERATURE_POLICIES",
    "FLAG_CONDITIONS_NOT_MET",
    "FLAG_TUR_BELOW_FLOOR",
    "FLAG_TEMPERATURE_OUT_OF_WINDOW",
    "FLAG_AMBIENT_UNKNOWN",
    "FLAG_NO_TEMPERATURE_WINDOW",
    "FLAG_EMPTY_BUDGET",
    "FLAG_ZERO_UNCERTAINTY",
    "Verdict",
    "DecisionRule",
    "TemperatureStatus",
    "gates_for",
    "classify_temperature",
    "ConformityResult",
    "ConformitySummary",
    "evaluate",
    "format_conformity",
    "summarize",
]


class VerdictError(ValueError):
    """A malformed decision input: a bad rule, factor, floor or policy."""


#: Confidence level the decision is made at.  95 % is the ILAC-G8 default and
#: the level Z540.3's TUR is defined at.
DEFAULT_CONFIDENCE = 0.95

#: Guard-band multiplier ``g`` in ``w = g*U``.  ``1.0`` is JCGM 106's worked
#: guarded-acceptance case and gives exactly ``(1-p)/2`` — 2.5 % at 95 % —
#: specific consumer's risk at the acceptance limit (see the module
#: docstring: 2.3 % is the ``k = 2`` normal approximation, which this module
#: does not use).
DEFAULT_GUARD_FACTOR = 1.0

#: Smallest test uncertainty ratio that may support a ``PASS``.  4:1 is the
#: long-standing calibration convention (ANSI/NCSL Z540.3 5.3.b's alternative
#: to an explicit 2 % false-accept computation); some labs use 10:1.
DEFAULT_TUR_FLOOR = 4.0

#: Values ``temperature_policy`` accepts.  See the module docstring.
TEMPERATURE_POLICIES = ("gate", "flag", "strict")

#: At least one **stated condition** of the specification was not shown to
#: hold, so neither conformity nor non-conformity is stated.  The individual
#: conditions are flagged by their own codes alongside it; this one is the
#: marker a summary buckets on.
FLAG_CONDITIONS_NOT_MET = "conditions-not-met"

FLAG_TUR_BELOW_FLOOR = "tur-below-floor"
FLAG_TEMPERATURE_OUT_OF_WINDOW = "temperature-out-of-window"
FLAG_AMBIENT_UNKNOWN = "ambient-temperature-unknown"
FLAG_NO_TEMPERATURE_WINDOW = "no-temperature-window"
#: No components at all were supplied: there is no budget behind this decision.
FLAG_EMPTY_BUDGET = "no-uncertainty-budget"
#: Components were supplied but combine to ``u_c = 0``.
FLAG_ZERO_UNCERTAINTY = "zero-uncertainty"


class _Unset:
    """Sentinel: distinguishes "argument omitted" from an explicit ``None``."""

    __slots__ = ()

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------

class Verdict(Enum):
    """The outcome of a conformity decision.

    ``INDETERMINATE`` is a first-class answer, not a failure of the code: it is
    what an honest report says when the measurement cannot support either
    statement -- the error landed in the guard band, or the reference was not
    good enough (TUR), or the specification's stated conditions were not shown
    to hold.  ``NO_SPEC`` is different again: there is no claim to test.
    """

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    NO_SPEC = "no_spec"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None


class DecisionRule(Enum):
    """How measurement uncertainty enters the accept/reject decision.

    ``SIMPLE``
        Accept when ``|error| <= TL``; reject otherwise.  ILAC-G8:2019 4.2.1,
        "simple acceptance", ``w = 0``: measurement uncertainty is *not* taken
        into account and producer and consumer share the risk.  At TUR 4:1 the
        false-accept risk of this rule is about 0.8 %, but at TUR 1:1 it is
        about 16 % -- which is why it is available and not the default.

    ``GUARDBANDED``
        Accept when ``|error| <= TL - w``, reject when ``|error| > TL``,
        ``INDETERMINATE`` between.  JCGM 106 8.3.3 guarded acceptance with the
        non-binary reporting of ILAC-G8 4.2.3.  **The default.**

    ``GUARDBANDED_REJECT``
        Accept when ``|error| <= TL``, reject only when ``|error| > TL + w``,
        ``INDETERMINATE`` between.  JCGM 106 8.3.4 guarded rejection -- a
        *negative* guard band in that document's sign convention, so the
        acceptance interval is wider than the tolerance interval.  Use it when
        the cost of a false reject dominates: scrapping a good transfer
        standard, or failing a customer's instrument that would have to be
        shipped, torn down and recalibrated.  It protects the producer, and it
        must never be used for a statement of conformity a consumer relies on.
    """

    SIMPLE = "simple"
    GUARDBANDED = "guardbanded"
    GUARDBANDED_REJECT = "guardbanded_reject"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().lower().replace("-", "_")
                                       .upper())
        return None


class TemperatureStatus(Enum):
    """What is known about the ambient relative to the specification window.

    Four distinct states, because collapsing them is precisely the bug this
    module was asked to fix: ``TemperatureSpec.within_window`` answers ``True``
    for two of them ("in the window" and "there is no window") and is never
    asked about the third ("nobody recorded the temperature").
    """

    #: Ambient recorded and inside the window the figures are written for.
    IN_WINDOW = "in_window"
    #: Ambient recorded and outside that window: the limit does not apply.
    OUT_OF_WINDOW = "out_of_window"
    #: A window exists but no ambient was recorded -- the dangerous case.
    AMBIENT_UNKNOWN = "ambient_unknown"
    #: Ambient recorded, but the model states no absolute window (3458A,
    #: 5700A): its figures are relative to a stored calibration temperature.
    NO_WINDOW = "no_window"
    #: Neither is known.
    UNKNOWN = "unknown"


#: Statuses that block a ``PASS`` under each policy.
_TEMPERATURE_GATES: Mapping[str, frozenset] = MappingProxyType({
    "gate": frozenset({TemperatureStatus.OUT_OF_WINDOW,
                       TemperatureStatus.AMBIENT_UNKNOWN}),
    "flag": frozenset(),
    "strict": frozenset({TemperatureStatus.OUT_OF_WINDOW,
                         TemperatureStatus.AMBIENT_UNKNOWN,
                         TemperatureStatus.NO_WINDOW,
                         TemperatureStatus.UNKNOWN}),
})

def gates_for(policy: str) -> frozenset:
    """The temperature statuses *policy* blocks a ``PASS`` on.

    Public because a *flag* is not a *block*: a point carrying
    ``no-temperature-window`` is blocked under ``strict`` and not under the
    default ``gate``, and anything that reports "temperature-gated" without
    consulting this mapping is reporting the flag, not the decision.  Both the
    3458A and the 5700A declare no absolute window, so on the pairing this
    project exists to run, *every* row carries that flag -- and a summary that
    took the flag for the block would attribute a whole run of coarse-reference
    points to the room temperature.  Callers that need the same answer per row
    should use :meth:`caldmm.cal.result.ResultRow.blocked_by_temperature`.
    """
    try:
        return _TEMPERATURE_GATES[policy]
    except KeyError:
        raise VerdictError(
            f"unknown temperature policy {policy!r}; known: "
            f"{', '.join(TEMPERATURE_POLICIES)}") from None


_TEMPERATURE_FLAGS: Mapping[TemperatureStatus, tuple[str, ...]] = \
    MappingProxyType({
        TemperatureStatus.IN_WINDOW: (),
        TemperatureStatus.OUT_OF_WINDOW: (FLAG_TEMPERATURE_OUT_OF_WINDOW,),
        TemperatureStatus.AMBIENT_UNKNOWN: (FLAG_AMBIENT_UNKNOWN,),
        TemperatureStatus.NO_WINDOW: (FLAG_NO_TEMPERATURE_WINDOW,),
        TemperatureStatus.UNKNOWN: (FLAG_NO_TEMPERATURE_WINDOW,
                                    FLAG_AMBIENT_UNKNOWN),
    })


def classify_temperature(
    ambient_c: float | None,
    window_c: tuple[float, float] | None,
) -> TemperatureStatus:
    """Which of the four temperature states this point is in.

    Deliberately *not* a boolean.  ``True``/``False`` cannot express the
    difference between "the instrument was at 23.1 C and the spec holds over
    18-28 C", "the model states no absolute window", and "nobody wrote the
    temperature down", and it is the last of those that quietly turns into a
    ``PASS`` when the three are merged.

    A ``nan`` ambient is "not recorded", not "outside": a failed sensor read is
    the commonest way one arrives, and every ordered comparison against ``nan``
    is False, so a naive ``low <= t <= high`` would silently call it
    out-of-window and print a temperature that was never measured.
    """
    if ambient_c is not None and math.isnan(ambient_c):
        ambient_c = None
    if window_c is None:
        return (TemperatureStatus.UNKNOWN if ambient_c is None
                else TemperatureStatus.NO_WINDOW)
    low, high = window_c
    if ambient_c is None:
        return TemperatureStatus.AMBIENT_UNKNOWN
    return (TemperatureStatus.IN_WINDOW if low <= ambient_c <= high
            else TemperatureStatus.OUT_OF_WINDOW)


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConformityResult:
    """One conformity decision, with everything needed to defend it.

    Every field is in the quantity's own unit; ``unit`` is a label carried so a
    ``reason`` string can be printed on its own line in a report without the
    caller having to re-supply it.

    For ``NO_SPEC`` the limit-derived fields (``tolerance_limit``,
    ``guard_band``, ``acceptance_limit``, ``margin``) are ``nan`` and ``tur`` is
    ``None``: there is no limit, so there is no number to print, and ``0.0``
    would be a lie that arithmetic downstream would happily consume.  ``error``
    and the whole uncertainty side are still populated -- an unspecified point
    is still data.
    """

    verdict: Verdict
    error: float                 #: measured - reference
    tolerance_limit: float       #: TL, absolute, symmetric +/-TL
    expanded_uncertainty: float  #: U at the decision confidence
    coverage_factor: float       #: k actually used (t at nu_eff, not 2)
    effective_dof: float         #: nu_eff, Welch-Satterthwaite
    tur: float | None            #: TL/U; None when U == 0 or there is no limit
    guard_band: float            #: w, signed; negative is guarded rejection
    acceptance_limit: float      #: AL = TL - w
    margin: float                #: TL - |error|; negative is out of tolerance
    rule: str                    #: the rule and its parameters, for the report
    reason: str                  #: one sentence a human can read
    confidence: float = DEFAULT_CONFIDENCE
    unit: str = ""
    temperature_status: TemperatureStatus = TemperatureStatus.UNKNOWN
    #: Conditions a report must print even when the verdict is ``PASS``.
    flags: tuple[str, ...] = ()

    @property
    def pass_limit(self) -> float:
        """The boundary at or below which ``|error|`` is a ``PASS``.

        ``min(TL, AL)``, which is ``AL`` for guarded acceptance (``w > 0``) and
        ``TL`` for guarded rejection (``w < 0``) without either rule needing
        its own comparison.  **Inclusive**: ``|error| == pass_limit`` passes.
        """
        return min(self.tolerance_limit, self.acceptance_limit)

    @property
    def fail_limit(self) -> float:
        """The boundary above which ``|error|`` is a ``FAIL``.

        ``max(TL, AL)``.  **Exclusive**: ``|error| == fail_limit`` is
        ``INDETERMINATE`` under a guard-banded rule, and ``PASS`` under
        ``simple`` where the two boundaries coincide.
        """
        return max(self.tolerance_limit, self.acceptance_limit)

    @property
    def conclusive(self) -> bool:
        """True when a statement of conformity was actually made."""
        return self.verdict in (Verdict.PASS, Verdict.FAIL)

    @property
    def relative_margin(self) -> float | None:
        """``margin / TL``: how close this point came, as a fraction of its limit.

        Dimensionless, and therefore the only sound way to rank points against
        each other.  ``1.0`` is a perfect reading, ``0.0`` sits exactly on the
        limit and a negative value is out of tolerance by that fraction.
        ``None`` when there is no usable limit (``NO_SPEC``, or the degenerate
        ``TL = 0``), which is what keeps such points out of a run's "worst"
        ranking rather than letting them win it with a ``nan``.
        """
        if not math.isfinite(self.tolerance_limit) or self.tolerance_limit <= 0:
            return None
        return self.margin / self.tolerance_limit


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def _quantity(x: float, unit: str) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.4g} {unit}".strip()


def _fmt_dof(dof: float) -> str:
    if math.isinf(dof):
        return "inf"
    if float(dof).is_integer():
        return str(int(dof))
    return f"{dof:.4g}"


def _fmt_tur(tur: float | None) -> str:
    return "n/a" if tur is None else f"{tur:.2f}"


def _fmt_window(window: tuple[float, float] | None) -> str:
    if window is None:
        return "none declared"
    return f"{window[0]:g} to {window[1]:g} C"


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _finite(name: str, x) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise VerdictError(f"{name} must be a number, got {x!r}") from exc
    if not math.isfinite(value):
        raise VerdictError(f"{name} must be finite, got {value!r}")
    return value


def _window(window) -> tuple[float, float] | None:
    if window is None:
        return None
    try:
        low, high = window
    except (TypeError, ValueError) as exc:
        raise VerdictError(
            f"temperature_window_c must be a (low_c, high_c) pair, got "
            f"{window!r}") from exc
    low = _finite("temperature_window_c low edge", low)
    high = _finite("temperature_window_c high edge", high)
    if high < low:
        raise VerdictError(
            f"temperature_window_c high edge {high:g} C is below its low edge "
            f"{low:g} C")
    return (low, high)


def _ambient(ambient_c):
    """Validate a recorded ambient; ``nan`` becomes "not recorded".

    A failed ambient-probe read reaches here as ``nan``.  That is an *absent* reading,
    and mapping it to ``None`` routes it to
    :data:`TemperatureStatus.AMBIENT_UNKNOWN` -- which still blocks a ``PASS``
    under the default policy, but says so truthfully instead of reporting a
    temperature nobody measured as out of window.
    """
    if ambient_c is None:
        return None
    try:
        value = float(ambient_c)
    except (TypeError, ValueError) as exc:
        raise VerdictError(
            f"ambient_c must be a number or None, got {ambient_c!r}") from exc
    if math.isnan(value):
        return None
    if math.isinf(value):
        raise VerdictError(f"ambient_c must be finite, got {value!r}")
    return value


def _rule_text(rule: DecisionRule, guard_factor: float, tur_floor: float,
               confidence: float, temperature_policy: str) -> str:
    """The decision's provenance, as it must appear in a report."""
    parts = [rule.value]
    if rule is DecisionRule.GUARDBANDED:
        parts.append(f"w={guard_factor:g}*U")
    elif rule is DecisionRule.GUARDBANDED_REJECT:
        # Guarded rejection is a negative guard band in JCGM 106's convention,
        # and the stored ``guard_band`` is negative; print it that way.
        parts.append(f"w={-guard_factor:g}*U")
    parts.append(f"p={confidence * 100:g}%")
    if tur_floor > 0.0:
        parts.append(f"tur_floor={tur_floor:g}")
    else:
        # Never allowed to vanish silently: a report has to show that the
        # decision was taken without any test-uncertainty-ratio requirement.
        parts.append("tur_floor=0 (no TUR requirement)")
    if temperature_policy != "gate":
        parts.append(f"temperature={temperature_policy}")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

def evaluate(
    error: float,
    limit: LimitResult | float | None,
    budget: UncertaintyBudget,
    *,
    rule: DecisionRule | str = DecisionRule.GUARDBANDED,
    confidence: float = DEFAULT_CONFIDENCE,
    guard_factor: float = DEFAULT_GUARD_FACTOR,
    tur_floor: float = DEFAULT_TUR_FLOOR,
    ambient_c: float | None = None,
    temperature_window_c=_UNSET,
    temperature_policy: str = "gate",
    conditions: Sequence[tuple[str, str]] | None = None,
    unit: str | None = None,
) -> ConformityResult:
    """Decide conformity of *error* against *limit*, given *budget*.

    Parameters
    ----------
    error:
        Measured minus reference, in the quantity's unit, **already corrected**
        for every recognised systematic effect (GUM 3.2.4) and already filtered
        for the instrument's invalid-reading sentinel -- ``9.9e37`` is a finite
        float and this function cannot know it is not a reading.  Use
        :meth:`caldmm.cal.specs.ModelSpec.is_overload` upstream.
    limit:
        A :class:`~caldmm.cal.specs.LimitResult` (which also supplies the
        temperature window and the unit), an absolute tolerance as a float, or
        ``None`` for "no specification applies", which yields ``NO_SPEC``.  A
        **negative** float is also ``NO_SPEC``: it is how the Fluke 5700A's
        ``UNCERT?`` says it has no figure for the point (``-1.0``), and a
        negative tolerance is meaningless in any case.
    budget:
        The GUM budget for this comparison.  A bare number is rejected outright
        (``TypeError``); an *empty* budget -- or one whose components combine
        to ``U = 0`` -- is accepted but can never yield a ``PASS``; see the
        module docstring, :data:`FLAG_EMPTY_BUDGET` and
        :data:`FLAG_ZERO_UNCERTAINTY`.
    rule, guard_factor, tur_floor, confidence:
        The decision rule, its guard-band multiplier ``g`` in ``w = g*U``, the
        smallest TUR that may support a ``PASS``, and the level ``U`` and the
        decision are taken at.
    ambient_c, temperature_window_c, temperature_policy:
        The recorded ambient, the window the figures hold in (taken from a
        :class:`~caldmm.cal.specs.LimitResult` when one is passed and this is
        omitted -- it is already resolved for the right interval there), and
        what to do when the two cannot be reconciled.
    conditions:
        ``(code, sentence)`` pairs for **stated conditions of the
        specification that were not shown to hold** -- the source was not warm
        yet, its dc zeros are overdue, the reference was outside its own
        temperature window.  Each code is added to :attr:`flags` beside
        :data:`FLAG_CONDITIONS_NOT_MET`, and *any* condition makes the verdict
        ``INDETERMINATE``: a specification holds under its stated conditions,
        so with those conditions unshown there is neither a conformity nor a
        non-conformity to state (JCGM 106; ILAC-G8 4.2).  This is **not** the
        TUR floor's asymmetry -- there the limit still applies and only the
        measurement is coarse, so a failure past ``TL`` stands.  The
        temperature gate is expressed as a condition too, which is why an
        out-of-window ``FAIL`` no longer stands against a limit this module's
        own text says does not apply.

    Notes
    -----
    ``TUR`` is computed at *confidence*, so a decision taken at 99 % divides by
    a larger ``U`` and yields a TUR smaller by ``z_99/z_95 = 1.31`` than the
    same measurement at 95 %.  That is self-consistent and conservative, but
    ANSI/NCSL Z540.3-2006 3.11 defines TUR with ``U_95`` specifically: a 4:1
    floor is comparable with the published convention only at
    ``confidence=0.95``.
    """
    if not isinstance(budget, UncertaintyBudget):
        raise TypeError(
            "evaluate() takes an UncertaintyBudget, not a bare uncertainty "
            f"(got {type(budget).__name__}).  Build the budget with "
            "caldmm.cal.uncertainty: a verdict that cannot show where its "
            "uncertainty came from cannot be defended.")

    rule = DecisionRule(rule)
    if temperature_policy not in _TEMPERATURE_GATES:
        raise VerdictError(
            f"unknown temperature_policy {temperature_policy!r}; known: "
            f"{', '.join(TEMPERATURE_POLICIES)}")
    guard_factor = _finite("guard_factor", guard_factor)
    if guard_factor < 0.0:
        raise VerdictError(
            f"guard_factor must be >= 0, got {guard_factor!r}; a negative "
            "guard band is guarded *rejection* -- use "
            "rule='guardbanded_reject'")
    tur_floor = _finite("tur_floor", tur_floor)
    if tur_floor < 0.0:
        raise VerdictError(f"tur_floor must be >= 0, got {tur_floor!r}")
    error = _finite("error", error)
    # Range-checked by the budget's own ``_check_confidence`` below, which
    # rejects the classic ``confidence=95`` for ``0.95``.
    confidence = _finite("confidence", confidence)
    ambient_c = _ambient(ambient_c)

    # ---- the limit, its unit and its temperature window
    window_given = not isinstance(temperature_window_c, _Unset)
    window = _window(temperature_window_c) if window_given else None
    tolerance_limit: float | None
    if isinstance(limit, LimitResult):
        tolerance_limit = limit.limit
        if not window_given:
            window = limit.temperature_window_c
        if unit is None:
            unit = limit.unit
    elif limit is None:
        tolerance_limit = None
    else:
        tolerance_limit = _finite("tolerance limit", limit)
    if tolerance_limit is not None and (not math.isfinite(tolerance_limit)
                                        or tolerance_limit < 0.0):
        tolerance_limit = None      # the 5700A's -1.0 "no spec" sentinel
    unit = unit or ""

    # ---- uncertainty.  k is computed once and U derived from it, so the k a
    # report prints is provably the k the decision used.
    u_c = budget.combined()
    k = budget.coverage_factor(confidence)
    nu_eff = budget.effective_dof()
    U = k * u_c

    temperature_status = classify_temperature(ambient_c, window)
    flags: list[str] = list(_TEMPERATURE_FLAGS[temperature_status])
    unmet: list[tuple[str, str]] = [
        (str(code), str(text)) for code, text in (conditions or ())]
    if temperature_status in _TEMPERATURE_GATES[temperature_policy]:
        codes = _TEMPERATURE_FLAGS[temperature_status]
        unmet.append((codes[0] if codes else FLAG_CONDITIONS_NOT_MET,
                      _temperature_condition_unmet(temperature_status,
                                                   ambient_c, window)))
    for code, _ in unmet:
        if code not in flags:
            flags.append(code)
    if unmet:
        flags.append(FLAG_CONDITIONS_NOT_MET)
    # An *empty* budget and a budget whose components happen to combine to zero
    # are different failures and are flagged differently: the first is an
    # absent claim (nobody supplied anything, and it blocks a PASS below), the
    # second is a degenerate one the caller actually asserted, component by
    # component, and can be read back from the budget table.
    if len(budget) == 0:
        flags.append(FLAG_EMPTY_BUDGET)
    elif U == 0.0:
        flags.append(FLAG_ZERO_UNCERTAINTY)
    rule_text = _rule_text(rule, guard_factor, tur_floor, confidence,
                           temperature_policy)

    # ---- no specification: record the measurement, make no claim
    if tolerance_limit is None:
        why = ("the instrument reports no specification for this point"
               if limit is not None and not isinstance(limit, LimitResult)
               else "no specification applies at this point")
        return ConformityResult(
            verdict=Verdict.NO_SPEC,
            error=error,
            tolerance_limit=math.nan,
            expanded_uncertainty=U,
            coverage_factor=k,
            effective_dof=nu_eff,
            tur=None,
            guard_band=math.nan,
            acceptance_limit=math.nan,
            margin=math.nan,
            rule=rule_text,
            reason=(f"{why}; error {_quantity(error, unit)} recorded, no "
                    f"statement of conformity is possible"),
            confidence=confidence,
            unit=unit,
            temperature_status=temperature_status,
            flags=tuple(flags),
        )

    # ---- guard band and the two boundaries
    if rule is DecisionRule.SIMPLE:
        guard_band = 0.0
    elif rule is DecisionRule.GUARDBANDED:
        guard_band = guard_factor * U
    else:                                    # guarded rejection: w < 0
        guard_band = -guard_factor * U
    acceptance_limit = tolerance_limit - guard_band
    pass_limit = min(tolerance_limit, acceptance_limit)
    fail_limit = max(tolerance_limit, acceptance_limit)

    magnitude = abs(error)
    margin = tolerance_limit - magnitude
    # TUR: None rather than inf when U == 0.  "Infinite ratio" is not a number
    # a report should print, and U == 0 means the budget is empty or wholly
    # zero -- which the flags state outright instead.
    tur = None if U == 0.0 else tolerance_limit / U

    if magnitude <= pass_limit:
        verdict = Verdict.PASS
    elif magnitude > fail_limit:
        verdict = Verdict.FAIL
    else:
        verdict = Verdict.INDETERMINATE

    # ---- condition gates.  They block PASS and never FAIL: a claim of
    # conformity needs the conditions to hold, an observation of
    # non-conformity does not stop being an observation.
    #
    # Each gate is phrased twice.  The blocking phrasing explains why no PASS
    # may be stated; the note phrasing is appended to a FAIL or an
    # INDETERMINATE that survived the gate, and must not contradict the verdict
    # it is attached to -- "the tolerance limit does not apply" cannot sit in
    # the same sentence as a non-conformity asserted against that limit.
    blockers: list[str] = []
    notes: list[str] = []
    if tur is not None and tur_floor > 0.0 and tur < tur_floor:
        flags.append(FLAG_TUR_BELOW_FLOOR)
        text = (f"TUR = {_fmt_tur(tur)} is below the required {tur_floor:g}:1 "
                f"(TL = {_quantity(tolerance_limit, unit)}, "
                f"U = {_quantity(U, unit)})")
        blockers.append(text)
        # Under a TUR floor the limit still applies and the measurement is
        # merely coarse, so a failure past TL survives it unqualified.
        notes.append(text)
    if len(budget) == 0:
        blockers.append("no uncertainty components were supplied, so there is "
                        "no budget behind this comparison")
        notes.append("no uncertainty components were supplied, so U, k and TUR "
                     "above are not evidence of anything")
    elif U == 0.0:
        # No real measurement has U = 0; this is what a degenerate adapter
        # path produces, and with tur = None the TUR floor cannot fire — so
        # without this gate it was an unconditional PASS with no uncertainty
        # requirement at all.
        blockers.append("the supplied components combine to U = 0, which no "
                        "real measurement has, so the budget is not evidence")
        notes.append("the supplied components combine to U = 0, so TUR and "
                     "the guard band above are not evidence of anything")

    if unmet:
        # A specification holds under its stated conditions.  With one of them
        # unshown there is no claim to make in *either* direction -- the same
        # sentence, whichever way the error went, which is what keeps a
        # certificate from asserting non-conformity to a limit and saying in
        # the same breath that the limit does not apply.
        if verdict is Verdict.PASS:
            went = (f"|error| = {_quantity(magnitude, unit)} is within the "
                    f"acceptance limit {_quantity(pass_limit, unit)}")
        elif verdict is Verdict.FAIL:
            went = (f"|error| = {_quantity(magnitude, unit)} exceeds the "
                    f"tolerance limit {_quantity(fail_limit, unit)}")
        else:
            went = (f"|error| = {_quantity(magnitude, unit)} lies inside the "
                    f"guard band (AL = {_quantity(pass_limit, unit)}, "
                    f"TL = {_quantity(tolerance_limit, unit)})")
        verdict = Verdict.INDETERMINATE
        reason = (f"{went}, but the specification's stated conditions were "
                  f"not shown to hold, so neither conformity nor "
                  f"non-conformity is stated: "
                  + "; ".join(text for _, text in unmet))
        if blockers:
            reason += "; also: " + "; ".join(blockers)
    elif verdict is Verdict.PASS and blockers:
        verdict = Verdict.INDETERMINATE
        reason = (f"|error| = {_quantity(magnitude, unit)} is within the "
                  f"acceptance limit {_quantity(pass_limit, unit)}, but no "
                  f"PASS may be stated: " + "; ".join(blockers))
    else:
        reason = _zone_reason(verdict, rule, magnitude, tolerance_limit,
                              pass_limit, fail_limit, guard_band, U, tur,
                              confidence, unit)
        if notes:
            reason += "; note: " + "; ".join(notes)

    return ConformityResult(
        verdict=verdict,
        error=error,
        tolerance_limit=tolerance_limit,
        expanded_uncertainty=U,
        coverage_factor=k,
        effective_dof=nu_eff,
        tur=tur,
        guard_band=guard_band,
        acceptance_limit=acceptance_limit,
        margin=margin,
        rule=rule_text,
        reason=reason,
        confidence=confidence,
        unit=unit,
        temperature_status=temperature_status,
        flags=tuple(flags),
    )


def _temperature_condition(status: TemperatureStatus, ambient_c: float | None,
                           window: tuple[float, float] | None) -> str:
    """The neutral statement of fact, with no consequence attached to it."""
    if status is TemperatureStatus.OUT_OF_WINDOW:
        return (f"ambient {ambient_c:g} C is outside the specification window "
                f"{_fmt_window(window)}")
    if status is TemperatureStatus.AMBIENT_UNKNOWN:
        return ("the ambient temperature was not recorded and these figures "
                f"hold only over {_fmt_window(window)}")
    if status is TemperatureStatus.NO_WINDOW:
        return ("the model declares no absolute temperature window (its "
                "figures are relative to its own stored calibration "
                "temperature)")
    return ("neither the ambient temperature nor an absolute specification "
            "window is known")


def _temperature_condition_unmet(status: TemperatureStatus,
                                 ambient_c: float | None,
                                 window: tuple[float, float] | None) -> str:
    """The temperature as an unmet condition: the fact, then what to do.

    **One text, whichever way the error went.**  There used to be two -- a
    blocking phrasing ("so the tolerance limit does not apply") for a
    suppressed ``PASS`` and a softer note ("must be confirmed in-window") for
    a ``FAIL`` that was allowed to stand -- and they contradicted each other,
    which is what M85 was: a counted, exit-status-1 non-conformity asserted
    against a limit the same module said did not apply.  Now the condition
    blocks both statements, so it can say the same thing to both.
    """
    condition = _temperature_condition(status, ambient_c, window)
    if status is TemperatureStatus.OUT_OF_WINDOW:
        return (condition + ", so the tolerance limit does not apply; repeat "
                "in-window before any statement of conformity or "
                "non-conformity is made")
    if status is TemperatureStatus.AMBIENT_UNKNOWN:
        return (condition + "; record the ambient and repeat before any "
                "statement of conformity or non-conformity is made")
    if status is TemperatureStatus.NO_WINDOW:
        return condition + ", so the ambient cannot be checked here"
    return condition


def _zone_reason(verdict: Verdict, rule: DecisionRule, magnitude: float,
                 tolerance_limit: float, pass_limit: float, fail_limit: float,
                 guard_band: float, U: float, tur: float | None,
                 confidence: float, unit: str) -> str:
    """The sentence describing which zone ``|error|`` landed in."""
    if verdict is Verdict.PASS:
        if rule is DecisionRule.SIMPLE:
            return (f"|error| = {_quantity(magnitude, unit)} is within the "
                    f"tolerance limit {_quantity(tolerance_limit, unit)}; "
                    f"measurement uncertainty was not taken into account "
                    f"(shared risk), U = {_quantity(U, unit)}, "
                    f"TUR = {_fmt_tur(tur)}")
        return (f"|error| = {_quantity(magnitude, unit)} is within the "
                f"acceptance limit {_quantity(pass_limit, unit)} "
                f"(TL {_quantity(tolerance_limit, unit)}, guard band "
                f"{_quantity(abs(guard_band), unit)}), TUR = {_fmt_tur(tur)}")
    if verdict is Verdict.FAIL:
        boundary = ("the tolerance limit" if fail_limit == tolerance_limit
                    else "the rejection limit TL + w")
        return (f"|error| = {_quantity(magnitude, unit)} exceeds {boundary} "
                f"{_quantity(fail_limit, unit)} by "
                f"{_quantity(magnitude - fail_limit, unit)}")
    return (f"|error| = {_quantity(magnitude, unit)} lies inside the guard "
            f"band (AL = {_quantity(pass_limit, unit)}, "
            f"TL = {_quantity(tolerance_limit, unit)}, U = "
            f"{_quantity(U, unit)}): neither conformity nor non-conformity "
            f"can be stated at {confidence * 100:g} % confidence")


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

def format_conformity(result: ConformityResult, unit: str | None = None) -> str:
    """Plain-text decision block, to sit under :func:`~caldmm.cal.uncertainty.format_budget`.

    *unit* overrides the one carried on the result, for a caller that scaled
    the numbers (to ppm, say) on the way in.
    """
    if unit is None:
        unit = result.unit
    rows: list[tuple[str, str]] = [
        ("error", _quantity(result.error, unit)),
        ("tolerance limit TL", _quantity(result.tolerance_limit, unit)),
        (f"expanded uncertainty U ({result.confidence * 100:g} %)",
         _quantity(result.expanded_uncertainty, unit)),
        ("coverage factor k", f"{result.coverage_factor:.3f}"),
        ("effective dof nu_eff", _fmt_dof(result.effective_dof)),
        # Labelled with the level, because TUR is conventionally defined
        # against U_95 (Z540.3 3.11) and this one is taken at the decision's
        # own confidence -- the caveat belongs where a report author reads it.
        (f"test uncertainty ratio (at {result.confidence * 100:g} %)",
         _fmt_tur(result.tur)),
        ("guard band w", _quantity(result.guard_band, unit)),
        ("acceptance limit AL = TL - w",
         _quantity(result.acceptance_limit, unit)),
        ("margin TL - |error|", _quantity(result.margin, unit)),
        ("temperature", result.temperature_status.value),
        ("rule", result.rule),
        ("verdict", result.verdict.value.upper()),
    ]
    if result.flags:
        rows.append(("flags", ", ".join(result.flags)))
    width = max(len(label) for label, _ in rows)
    header = f"Conformity decision [{unit}]" if unit else "Conformity decision"
    lines = [header, "-" * max(len(header), width + 24)]
    lines += [f"{label:<{width}}  {value}" for label, value in rows]
    lines.append(f"{'reason':<{width}}  {result.reason}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ConformitySummary:
    """Counts per verdict and the worst point across a run."""

    counts: Mapping[Verdict, int] = field(
        default_factory=lambda: MappingProxyType({v: 0 for v in Verdict}))
    total: int = 0
    #: ``TL - |error|`` of the worst point, **in that point's own unit**.
    #: ``None`` when no result had a usable limit.
    worst_margin: float | None = None
    #: The same margin as a fraction of its own tolerance limit -- the
    #: dimensionless quantity the ranking is actually done on.
    worst_relative_margin: float | None = None
    worst: ConformityResult | None = None
    #: How many results carry at least one flag a report must print.
    flagged: int = 0

    def __getitem__(self, verdict) -> int:
        return self.counts[Verdict(verdict)]

    def line(self) -> str:
        """One line for the end of a CLI run."""
        order = (Verdict.PASS, Verdict.FAIL, Verdict.INDETERMINATE,
                 Verdict.NO_SPEC)
        parts = ", ".join(f"{self.counts[v]} {v.value.replace('_', ' ')}"
                          for v in order)
        point = f"{self.total} point{'' if self.total == 1 else 's'}: {parts}"
        if self.worst is not None and self.worst_margin is not None:
            point += (f"; worst margin "
                      f"{_quantity(self.worst_margin, self.worst.unit)}")
            if self.worst_relative_margin is not None:
                point += f" ({self.worst_relative_margin * 100:+.1f} % of TL)"
        if self.flagged:
            point += f"; {self.flagged} flagged"
        return point

    def __str__(self) -> str:
        return self.line()


def summarize(results: Sequence[ConformityResult]) -> ConformitySummary:
    """Counts per verdict, the worst point, and how many results are flagged.

    **The ranking is on the dimensionless**
    :attr:`ConformityResult.relative_margin` (``margin/TL``), not on the margin
    itself.  A 5700A sweep spans volts, ohms and amps from microamps to
    hundreds of volts; ranking raw margins would nominate whichever quantity
    happens to carry the largest numbers -- a 1 ohm margin on a 100 kohm point
    would outrank a 1 uV margin that is 2 % from failing -- and pointing the
    operator at the point closest to failing is the entire purpose of a run
    summary.  ``worst_margin`` is still reported in the winning point's own
    unit, because that is what an operator has to go and look at.

    Points with no usable limit -- ``NO_SPEC`` (``nan`` margin) and the
    degenerate ``TL = 0`` -- are counted but never ranked: they have no
    distance to a limit, and letting a ``nan`` win a comparison is how a run
    summary silently loses its real worst case.
    """
    counts = {v: 0 for v in Verdict}
    worst: ConformityResult | None = None
    worst_relative = None
    flagged = 0
    total = 0
    for result in results:
        if not isinstance(result, ConformityResult):
            raise VerdictError(
                f"summarize() takes ConformityResult, got {result!r}")
        total += 1
        counts[result.verdict] += 1
        if result.flags:
            flagged += 1
        relative = result.relative_margin
        if relative is None or math.isnan(relative):
            continue
        if worst is None or relative < worst_relative:
            worst, worst_relative = result, relative
    return ConformitySummary(
        counts=MappingProxyType(counts),
        total=total,
        worst_margin=None if worst is None else worst.margin,
        worst_relative_margin=worst_relative,
        worst=worst,
        flagged=flagged,
    )

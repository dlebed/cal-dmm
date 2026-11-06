"""GUM measurement-uncertainty budgets: components, combination, coverage.

This module implements the evaluation of measurement uncertainty of
**JCGM 100:2008 (GUM)** -- the law of propagation of uncertainty for
uncorrelated inputs (GUM 5.1.2), the Welch-Satterthwaite effective degrees of
freedom (GUM G.4.1) and the expanded uncertainty at a stated level of
confidence (GUM 6.2).  Task B4 turns a budget into a verdict; this module
deliberately knows nothing about pass, fail, TUR or guardbands, so that no
caller can reach a verdict without first having built a budget.

Why a budget and not a number
-----------------------------

A verification result that collapses to a single ppm figure cannot be
defended.  What has to survive into the report is the **itemised budget**: for
every input quantity, its standard uncertainty, the distribution it was
derived from, its degrees of freedom and its sensitivity coefficient.  That is
what :class:`UncertaintyBudget` holds, what :func:`format_budget` prints, and
what :func:`budget_to_rows` / :func:`budget_from_rows` store in and re-derive
from a CSV.  A report whose numbers cannot be recomputed from its own data
file is not evidence.

:meth:`UncertaintyBudget.contributions` sorted by dominance is the actionable
part: it says which term to attack.  If the Type A term dominates, average
more; if the calibrator dominates, nothing the operator does at the bench will
help.

The model
---------

Every component carries a **standard** uncertainty ``u_i`` -- already divided
by whatever divisor its distribution demands -- and a sensitivity coefficient
``c_i = du/dx_i``.  Combination is::

    u_c = sqrt( sum_i (c_i * u_i)^2 )                      # GUM (10), r_ij = 0
    nu_eff = u_c^4 / sum_i ( (c_i*u_i)^4 / nu_i )          # GUM (G.2b)
    k = t_p(nu_eff)                                        # GUM G.6.4
    U = k * u_c                                            # GUM 6.2.1

Correlations are **not** modelled: the covariance term of GUM (13) is assumed
zero.  That is the right assumption for a bench verification whose inputs are
a calibrator, a meter, a thermometer and an operator bound, and it is stated
here rather than left implicit.  If two components are ever known to be
correlated, combine them by hand into one component before adding it.

Units
-----

This module is unit-agnostic and does no scaling: every component in one
budget must be expressed in the **same** unit, and :func:`format_budget` takes
that unit's name purely as a label.  Mixing volts and ppm in one budget is a
caller error that no amount of arithmetic here can catch.

Import hygiene
--------------

Pure functions and frozen dataclasses.  No I/O, no instrument access, no file
reads.  Imports are the stdlib and ``scipy`` only -- not even
:mod:`caldmm.cal.specs`, so that a budget can be built from numbers that came
from anywhere.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, Sequence

from scipy import stats

__all__ = [
    "UncertaintyError",
    "DISTRIBUTIONS",
    "BUDGET_FIELDS",
    "normal_coverage_factor",
    "UncertaintyComponent",
    "UncertaintyBudget",
    "from_half_width",
    "from_expanded",
    "from_confidence",
    "from_calibrator_uncert",
    "type_a",
    "resolution_component",
    "thermal_emf_component",
    "loading_fraction",
    "loading_correction",
    "tempco_component",
    "format_budget",
    "budget_to_rows",
    "budget_from_rows",
]


class UncertaintyError(ValueError):
    """A malformed component, budget or conversion."""


#: Divisor that turns the **half-width** of a distribution into a standard
#: uncertainty.  ``normal`` is 1 because a value already expressed as a
#: standard uncertainty needs no division -- use :func:`from_expanded` or
#: :func:`from_confidence` to get there from a coverage factor or a
#: confidence level.
DISTRIBUTIONS: Mapping[str, float] = MappingProxyType({
    "normal":      1.0,
    "rectangular": math.sqrt(3.0),   # GUM 4.3.7: u = a/sqrt(3)
    "triangular":  math.sqrt(6.0),   # GUM 4.3.9: u = a/sqrt(6)
    # Arcsine, i.e. a cyclic effect that spends most of its time at the
    # extremes -- a room's temperature swing between thermostat set points.
    # GUM H.1.3.2 evaluates exactly this case as u = a/sqrt(2).
    "u_shaped":    math.sqrt(2.0),
})

#: Column order for :func:`budget_to_rows` / :func:`budget_from_rows`, i.e.
#: the CSV header of a stored budget.
BUDGET_FIELDS: tuple[str, ...] = (
    "label", "value", "dof", "type", "distribution", "sensitivity", "source",
)

_TYPES = ("A", "B")


# ------------------------------------------------------------------ helpers

def _finite(name: str, x) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise UncertaintyError(f"{name} must be a number, got {x!r}") from exc
    if not math.isfinite(value):
        raise UncertaintyError(f"{name} must be finite, got {value!r}")
    return value


def _dof(name: str, x) -> float:
    """Degrees of freedom: a positive number, or ``inf``/``'inf'``."""
    try:
        value = float(x)
    except (TypeError, ValueError) as exc:
        raise UncertaintyError(
            f"{name} must be a number or math.inf, got {x!r}") from exc
    if math.isnan(value) or value <= 0.0:
        raise UncertaintyError(
            f"{name} must be > 0 or math.inf, got {x!r}")
    return value


def _check_confidence(confidence: float) -> float:
    p = _finite("confidence", confidence)
    if not 0.0 < p < 1.0:
        raise UncertaintyError(
            f"confidence must be a probability strictly between 0 and 1 "
            f"(0.95, not 95), got {p!r}")
    return p


def normal_coverage_factor(confidence: float = 0.95) -> float:
    """Two-sided normal quantile for *confidence*: 1.960 at 95 %, 2.576 at 99 %.

    The coverage factor for an input whose degrees of freedom are effectively
    infinite, and hence the limit of :meth:`UncertaintyBudget.coverage_factor`
    and the divisor :meth:`UncertaintyComponent.from_confidence` uses at its
    default ``dof=inf``.  At a finite ``dof`` both use ``t`` instead.
    """
    return float(stats.norm.ppf((1.0 + _check_confidence(confidence)) / 2.0))


# --------------------------------------------------------------- component

@dataclass(frozen=True, slots=True)
class UncertaintyComponent:
    """One row of an uncertainty budget.

    ``value`` is always the **standard** uncertainty ``u_i``: already divided
    by the distribution's divisor, by the coverage factor, or by the normal
    quantile of a stated confidence level, as the case may be.  The named
    constructors below do that division, and callers should use them rather
    than dividing by hand -- ``a/sqrt(3)`` typed out at 40 call sites is
    ``a/sqrt(2)`` at one of them sooner or later.

    ``dof`` is ``math.inf`` for a Type B bound taken as exactly known, a
    finite ``n-1`` for a Type A evaluation, and may be a finite Type B
    estimate from GUM equation (G.3): ``nu_i ~ 0.5 * (du_i/u_i)^-2``, i.e. a
    bound believed good to 10 % carries ``nu_i = 50`` and one good to 50 %
    carries ``nu_i = 2``.

    ``sensitivity`` is ``c_i = df/dx_i``, the partial derivative of the
    measurand with respect to this input.  It may be negative; only its square
    enters the combination, but the sign is kept because a report that prints
    it lets a reader check the model.
    """

    label: str
    value: float
    dof: float
    type: Literal["A", "B"]
    distribution: str
    sensitivity: float = 1.0
    source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise UncertaintyError(
                f"component label must be a non-empty string, got "
                f"{self.label!r}")
        value = _finite(f"{self.label}: value", self.value)
        if value < 0.0:
            raise UncertaintyError(
                f"{self.label}: standard uncertainty must be >= 0, got "
                f"{value!r} (put the sign in the sensitivity coefficient)")
        object.__setattr__(self, "value", value)

        object.__setattr__(
            self, "dof", _dof(f"{self.label}: degrees of freedom", self.dof))

        if self.type not in _TYPES:
            raise UncertaintyError(
                f"{self.label}: type must be 'A' or 'B', got {self.type!r}")
        if self.distribution not in DISTRIBUTIONS:
            raise UncertaintyError(
                f"{self.label}: unknown distribution {self.distribution!r}; "
                f"known: {', '.join(sorted(DISTRIBUTIONS))}")
        object.__setattr__(
            self, "sensitivity",
            _finite(f"{self.label}: sensitivity", self.sensitivity))
        if not isinstance(self.source, str):
            raise UncertaintyError(
                f"{self.label}: source must be a string, got {self.source!r}")

    # ---- derived quantities

    @property
    def contribution(self) -> float:
        """``c_i * u_i``, signed, in the budget's unit."""
        return self.sensitivity * self.value

    @property
    def variance(self) -> float:
        """``(c_i * u_i)^2``, this component's share of ``u_c^2``."""
        return self.contribution ** 2

    # ---- named constructors (Type B conversions)

    @classmethod
    def from_half_width(
        cls,
        label: str,
        a: float,
        distribution: str = "rectangular",
        *,
        dof: float = math.inf,
        sensitivity: float = 1.0,
        source: str = "",
    ) -> "UncertaintyComponent":
        """A bound of half-width *a*, converted by its assumed distribution.

        ``rectangular`` -> ``a/sqrt(3)`` (GUM 4.3.7, the default and the right
        choice for a datasheet limit with no other information), ``triangular``
        -> ``a/sqrt(6)`` (GUM 4.3.9, when values near the centre are more
        likely), ``u_shaped`` -> ``a/sqrt(2)`` (an arcsine-distributed cyclic
        effect such as a room's temperature swing).  ``normal`` divides by 1
        and is therefore a way of saying "*a* is already standard".
        """
        divisor = DISTRIBUTIONS.get(distribution)
        if divisor is None:
            raise UncertaintyError(
                f"{label}: unknown distribution {distribution!r}; known: "
                f"{', '.join(sorted(DISTRIBUTIONS))}")
        half_width = _finite(f"{label}: half-width", a)
        if half_width < 0.0:
            raise UncertaintyError(
                f"{label}: half-width must be >= 0, got {half_width!r}")
        return cls(label=label, value=half_width / divisor, dof=dof, type="B",
                   distribution=distribution, sensitivity=sensitivity,
                   source=source)

    @classmethod
    def from_expanded(
        cls,
        label: str,
        U: float,
        k: float,
        *,
        dof: float = math.inf,
        sensitivity: float = 1.0,
        distribution: str = "normal",
        source: str = "",
    ) -> "UncertaintyComponent":
        """A certificate quoting ``U`` at a stated **coverage factor** ``k``.

        ``u = U/k``.  This is the ISO/IEC 17025 certificate case: "U = 2.4 uV,
        k = 2".  If the certificate quotes a *confidence level* instead of a
        coverage factor, use :meth:`from_confidence`; the two are not
        interchangeable (k = 2 is not 95 %).
        """
        factor = _finite(f"{label}: coverage factor", k)
        if factor <= 0.0:
            raise UncertaintyError(
                f"{label}: coverage factor must be > 0, got {factor!r}")
        expanded = _finite(f"{label}: expanded uncertainty", U)
        if expanded < 0.0:
            raise UncertaintyError(
                f"{label}: expanded uncertainty must be >= 0, got "
                f"{expanded!r}")
        return cls(label=label, value=expanded / factor, dof=dof, type="B",
                   distribution=distribution, sensitivity=sensitivity,
                   source=source)

    @classmethod
    def from_confidence(
        cls,
        label: str,
        U: float,
        confidence: float = 0.95,
        *,
        dof: float = math.inf,
        sensitivity: float = 1.0,
        source: str = "",
    ) -> "UncertaintyComponent":
        """A bound quoted at a **confidence level** rather than a factor.

        Divides by the two-sided ``t`` quantile at *dof*, which for the
        default ``dof=inf`` is the normal quantile: 1.960 at 95 %, 2.576 at
        99 %.

        A figure quoted as "+/-x at 95 % confidence" is a statement about a
        distribution, not a hard bound, and the divisor is the quantile of
        that distribution -- 1.960, not ``sqrt(3)`` = 1.732 and not 1.  Using
        ``sqrt(3)`` inflates ``u`` by 13 % at 95 % and 49 % at 99 %; using no
        divisor at all inflates it by 96 % and 158 %.

        **The divisor must follow the degrees of freedom.**  A certificate
        that quotes ``U`` at 95 % *and* a finite ``nu_eff`` is asserting
        ``U = t_95(nu)*u`` (GUM 6.3.3, G.6.4), so at ``nu = 10`` the divisor is
        2.228, not 1.960.  Dividing by the normal quantile and then handing the
        finite ``nu`` to Welch-Satterthwaite would inflate the component on the
        way in and re-widen it on the way out, by ``t_p(nu)/z_p - 1``: 14 % at
        ``nu = 10``, 42 % at ``nu = 4``, 120 % at ``nu = 2``.  Because the
        divisor is the same quantile the budget will later apply, a lone
        component built this way round-trips exactly:
        ``expanded(confidence)`` returns the ``U`` that was passed in.
        """
        expanded = _finite(f"{label}: expanded uncertainty", U)
        if expanded < 0.0:
            raise UncertaintyError(
                f"{label}: expanded uncertainty must be >= 0, got "
                f"{expanded!r}")
        p = _check_confidence(confidence)
        divisor = float(stats.t.ppf(
            (1.0 + p) / 2.0, _dof(f"{label}: degrees of freedom", dof)))
        return cls(label=label, value=expanded / divisor,
                   dof=dof, type="B", distribution="normal",
                   sensitivity=sensitivity, source=source)

    @classmethod
    def from_calibrator_uncert(
        cls,
        label: str,
        value: float,
        confidence: float,
        *,
        dof: float = math.inf,
        sensitivity: float = 1.0,
        source: str = "UNCERT?",
    ) -> "UncertaintyComponent":
        """The calibrator's own figure for the output it is presently sourcing.

        For the Fluke 5700A this is ``UNCERT?``, computed by the firmware from
        the programmed output, the range, the frequency, ``CAL_INTV`` and the
        stored calibration temperature.  **The conversion is the thing to get
        right, and it is easy to get wrong in either direction:**

        * It is **not a rectangular bound**.  Dividing by ``sqrt(3)`` because
          "it is a specification limit" gives a standard uncertainty 13 % too
          large at 95 % and 49 % too large at 99 % -- because ``sqrt(3)`` =
          1.732 is a smaller divisor than either normal quantile.
        * It is **not already a standard uncertainty**.  Using it as-is is 96 %
          too large at 95 % and 158 % too large at 99 %.

        Both mistakes therefore err on the *large* side, which is not the
        harmless direction it sounds: an inflated reference contribution
        wrecks the test-uncertainty ratio and, under a guardbanded decision
        rule, fails perfectly good instruments.  Understating would be worse
        still -- it would claim confidence that does not exist -- but neither
        is acceptable when the exact conversion costs one division.

        It is a **confidence statement**, and *the instrument chooses the
        level*: ``CAL_CONF`` selects ``CONF95`` or ``CONF99`` and ``UNCERT?``
        answers accordingly, so the same 10 V output legitimately reports two
        different numbers.  The correct handling is therefore
        :meth:`from_confidence` with the level the instrument reports --
        ``CAL_CONF?``, exposed as ``cal_conf()`` on the driver -- never a
        hard-coded divisor: pass ``0.95`` for ``CONF95`` and ``0.99`` for
        ``CONF99``.  Read the level from the instrument rather than assuming
        it; the manual's printed tables are the 99 % column, but ``CAL_CONF``
        is a stored setting that anyone can have changed.

        The 5700A manual states that absolute uncertainty "includes stability,
        temperature coefficient, linearity, line and load regulation, and the
        traceability to external standards", so nothing is added to this
        component for those effects -- doing so would double-count them.
        """
        return cls.from_confidence(label, value, confidence, dof=dof,
                                   sensitivity=sensitivity, source=source)

    @classmethod
    def type_a(
        cls,
        label: str,
        samples: Sequence[float],
        *,
        sensitivity: float = 1.0,
        source: str = "",
    ) -> "UncertaintyComponent":
        """Type A evaluation from repeated observations (GUM 4.2).

        The standard uncertainty of the **mean** is ``u = s/sqrt(n)`` with
        ``s`` the *sample* standard deviation (``ddof = 1``, GUM (4)/(5)), and
        the degrees of freedom are ``n - 1``.  Using the population standard
        deviation here would understate ``u`` and, worse, make ``nu_eff`` a
        lie.

        ``n < 2`` raises: a single reading has no experimental standard
        deviation, and returning zero would silently drop the whole repeat
        term out of the budget.
        """
        values = [_finite(f"{label}: sample", x) for x in samples]
        n = len(values)
        if n < 2:
            raise UncertaintyError(
                f"{label}: Type A evaluation needs at least 2 samples, got "
                f"{n} -- a single reading has no standard deviation")
        s = statistics.stdev(values)          # ddof = 1
        return cls(label=label, value=s / math.sqrt(n), dof=float(n - 1),
                   type="A", distribution="normal", sensitivity=sensitivity,
                   source=source)


# Module-level spellings of the constructors.  Bound to the same classmethods,
# so there is exactly one implementation of each conversion.
from_half_width = UncertaintyComponent.from_half_width
from_expanded = UncertaintyComponent.from_expanded
from_confidence = UncertaintyComponent.from_confidence
from_calibrator_uncert = UncertaintyComponent.from_calibrator_uncert
type_a = UncertaintyComponent.type_a


# ------------------------------------------------------------------ budget

@dataclass(frozen=True, slots=True)
class UncertaintyBudget:
    """An itemised budget: the components, and everything derived from them.

    Nothing is cached; the derived quantities are cheap and a budget that
    could go stale relative to its own components would be worse than useless
    in a report.
    """

    components: tuple[UncertaintyComponent, ...] = ()

    def __post_init__(self) -> None:
        components = tuple(self.components)
        for component in components:
            if not isinstance(component, UncertaintyComponent):
                raise UncertaintyError(
                    f"budget takes UncertaintyComponent, got {component!r}")
        object.__setattr__(self, "components", components)

    def __len__(self) -> int:
        return len(self.components)

    def __iter__(self):
        return iter(self.components)

    def add(self,
            *components: "UncertaintyComponent | None") -> "UncertaintyBudget":
        """Return a new budget with *components* appended (``None`` skipped).

        ``None`` is accepted so that a builder which legitimately has nothing
        to contribute -- :func:`tempco_component` with an unknown delta T --
        can be passed straight through without an ``if`` at every call site.
        """
        extra = tuple(c for c in components if c is not None)
        return UncertaintyBudget(self.components + extra)

    # ---- GUM combination

    def variance(self) -> float:
        """``u_c^2 = sum_i (c_i*u_i)^2`` (GUM (10) with all ``r_ij = 0``)."""
        return math.fsum(c.variance for c in self.components)

    def combined(self) -> float:
        """Combined standard uncertainty ``u_c``.  Empty budget -> ``0.0``."""
        return math.sqrt(self.variance())

    def effective_dof(self) -> float:
        """Welch-Satterthwaite effective degrees of freedom (GUM (G.2b)).

        ``nu_eff = u_c^4 / sum_i ( (c_i*u_i)^4 / nu_i )``.  Components with
        ``nu_i = inf`` contribute nothing to the denominator, so a budget of
        pure Type B bounds gives ``inf`` -- and hence the normal coverage
        factor.  ``u_c = 0`` (an empty budget, or one whose every component is
        zero) also gives ``inf`` rather than dividing by zero: with no
        uncertainty there is nothing for the ``t`` distribution to widen.
        """
        variance = self.variance()
        if variance <= 0.0:
            return math.inf
        denominator = math.fsum(
            c.variance ** 2 / c.dof
            for c in self.components if math.isfinite(c.dof))
        if denominator <= 0.0:
            return math.inf
        return variance ** 2 / denominator

    def coverage_factor(self, confidence: float = 0.95) -> float:
        """Two-sided Student-``t`` quantile at ``nu_eff`` (GUM G.6.4).

        ``k = t.ppf((1+p)/2, nu_eff)``, which converges to the normal quantile
        (1.960 at 95 %, 2.576 at 99 %) as ``nu_eff -> inf``.  It is **not**
        hard-coded to 2: k = 2 is roughly 95.45 % only for infinite degrees of
        freedom, and at ``nu_eff = 4`` the true 95 % factor is 2.78.

        GUM G.4.1 note 1 permits truncating a non-integer ``nu_eff`` down to
        the next lower integer before entering a printed t-table, which is
        marginally conservative.  ``scipy`` evaluates ``t`` at non-integer
        degrees of freedom directly, so the exact ``nu_eff`` is used here; the
        difference is well under 1 % (at ``nu_eff = 16.75``: 2.112 exact
        versus 2.120 truncated).
        """
        p = _check_confidence(confidence)
        return float(stats.t.ppf((1.0 + p) / 2.0, self.effective_dof()))

    def expanded(self, confidence: float = 0.95) -> float:
        """Expanded uncertainty ``U = k * u_c`` at *confidence* (GUM 6.2.1)."""
        return self.coverage_factor(confidence) * self.combined()

    def contributions(self) -> list[tuple[str, float, float]]:
        """``(label, c_i*u_i, percent of u_c^2)``, largest contributor first.

        Sorted by the **magnitude** of ``c_i*u_i`` (the value itself is signed
        and its sign carries no weight in the combination), so the first row
        is the term worth attacking.  Percentages are of the *variance*, which
        is what actually adds up: a component at 50 % of the variance is at
        71 % of ``u_c``.  They sum to 100 for any non-empty budget with a
        non-zero ``u_c``, and are all 0.0 when ``u_c`` is 0.
        """
        variance = self.variance()
        rows = [
            (c.label, c.contribution,
             100.0 * c.variance / variance if variance > 0.0 else 0.0)
            for c in self.components
        ]
        rows.sort(key=lambda row: abs(row[1]), reverse=True)
        return rows


# ------------------------------------------------- standard component builders

def resolution_component(
    label: str,
    resolution: float,
    *,
    sensitivity: float = 1.0,
    source: str = "",
) -> UncertaintyComponent:
    """Display resolution / quantisation of a digital indication.

    *resolution* is the **full** value of one least-significant digit; a
    reading can be wrong by at most half of it, so the half-width is
    ``resolution/2`` and the distribution is rectangular (GUM F.2.2.1): within
    that interval no value is more likely than another.  ``u = a/sqrt(3)``.

    Worth carrying even when it looks negligible: on a 6.5-digit meter at
    10 V it is 10 uV / 2 / sqrt(3) = 2.9 uV, which routinely dominates a
    short-interval budget where the repeat term is small.
    """
    res = _finite(f"{label}: resolution", resolution)
    if res < 0.0:
        raise UncertaintyError(
            f"{label}: resolution must be >= 0, got {res!r}")
    return UncertaintyComponent.from_half_width(
        label, res / 2.0, "rectangular", sensitivity=sensitivity,
        source=source or "display resolution")


def thermal_emf_component(
    label: str,
    bound: float,
    *,
    dof: float = math.inf,
    sensitivity: float = 1.0,
    source: str = "operator",
) -> UncertaintyComponent:
    """Thermal EMF in the measurement loop, as an operator-supplied bound.

    Rectangular: the operator knows a plausible worst case for the connectors
    and the copper-to-copper joints in use, not a distribution.  Cu-Cu is a
    fraction of a uV/K, Cu-solder a few, and anything with a spade lug into a
    dissimilar binding post can reach tens; the value belongs to the bench,
    not to this function, which is why there is no default.

    It matters most exactly where it is easiest to forget: at a zero or short
    test point, and on the low dc ranges, where it can be the largest term in
    the budget.  A report that buries it is hiding its own dominant term.
    """
    return UncertaintyComponent.from_half_width(
        label, bound, "rectangular", dof=dof, sensitivity=sensitivity,
        source=source)


def loading_fraction(z_source: float, z_input: float) -> float:
    """Fractional reading error from finite meter input impedance.

    A meter of input impedance ``Zin`` across a source of output impedance
    ``Zs`` reads ``Zin/(Zs+Zin)`` of the open-circuit voltage, i.e. a
    fractional error of ``-Zs/(Zs+Zin)`` -- always negative, always a *low*
    reading.
    """
    zs = _finite("z_source", z_source)
    zin = _finite("z_input", z_input)
    if zs < 0.0:
        raise UncertaintyError(f"z_source must be >= 0, got {zs!r}")
    if zin <= 0.0:
        raise UncertaintyError(f"z_input must be > 0, got {zin!r}")
    return -zs / (zs + zin)


def loading_correction(
    reading: float,
    z_source: float,
    z_input: float,
    *,
    label: str = "input loading",
    relative_uncertainty: float = 0.1,
    distribution: str = "rectangular",
    source: str = "",
) -> tuple[float, UncertaintyComponent]:
    """``(correction, component)`` for finite input impedance.

    Returns the **additive correction** to be applied to the reading --
    ``true = reading + correction``, exactly ``reading * Zs/Zin`` -- together
    with the uncertainty of that correction, whose half-width is
    ``relative_uncertainty * |correction|``, i.e. how well the two impedances
    are known.

    A known systematic effect must be **corrected**, with the uncertainty of
    the correction carried into the budget (GUM 3.2.4: a result is assumed
    corrected for all recognised significant systematic effects); it must not
    be left uncorrected and merely bounded.  Bounding a 5 ppm offset by +/-5 ppm
    rectangular puts 2.9 ppm into the budget and still leaves the result
    biased by 5 ppm.  Correcting it and carrying a 10 % uncertainty on the
    correction puts 0.29 ppm in and removes the bias.

    If the correction is deliberately *not* applied, note that
    ``relative_uncertainty=1.0`` is **not** the rigorous handling: it models a
    known one-sided bias as a symmetric rectangular ``+/-a`` about an unshifted
    estimate (``u = 0.577a``), when the effect is known to lie in ``[0, a]``
    and to be of one sign.  The conventional treatments are a rectangular
    distribution over ``[0, a]`` -- half-width ``a/2`` about an estimate
    shifted by ``a/2``, which is just the correction again with a 50 %
    uncertainty -- or leaving the estimate alone and adding ``|b|`` to the
    expanded uncertainty at the end.  ``relative_uncertainty=1.0`` is a blunt
    stand-in for the first, and is only defensible as a deliberately
    pessimistic bound.  Correcting is better than all of them.

    The case that motivates this in this project: the 5700A's 220 mV range has
    a 50 ohm output impedance, and a 34401A presents 10 Mohm on the higher dc
    ranges -- 5 ppm of reading, larger than some specification floors, and
    entirely invisible unless someone computes it.
    """
    value = _finite("reading", reading)
    fraction = loading_fraction(z_source, z_input)
    rel = _finite("relative_uncertainty", relative_uncertainty)
    if rel < 0.0:
        raise UncertaintyError(
            f"relative_uncertainty must be >= 0, got {rel!r}")
    # true = reading / (1 + fraction) = reading * (Zs+Zin)/Zin
    #      = reading + reading*Zs/Zin
    zs = float(z_source)
    zin = float(z_input)
    correction = value * zs / zin
    component = UncertaintyComponent.from_half_width(
        label, rel * abs(correction), distribution,
        source=source or f"Zs={zs:g}, Zin={zin:g}, {fraction * 1e6:+.2f} ppm")
    return correction, component


def tempco_component(
    label: str,
    ppm_per_c: float,
    delta_c: float | None,
    value: float,
    *,
    distribution: str = "rectangular",
    dof: float = math.inf,
    sensitivity: float = 1.0,
    source: str = "",
) -> UncertaintyComponent | None:
    """Temperature coefficient over a departure from the reference temperature.

    Half-width ``a = |ppm_per_c| * 1e-6 * |value| * |delta_c|``, rectangular:
    a datasheet tempco is a limit, not a measured slope, and nothing says the
    true coefficient is nearer the middle of it.  *value* is whatever
    magnitude the ppm applies to -- the reading for a "ppm of reading" tempco,
    the full scale for a "ppm of range" one -- and the result is in that same
    unit.

    Returns ``None`` when *delta_c* is ``None``, i.e. when the ambient or the
    instrument's calibration temperature is unknown.  The caller is expected
    to flag that row in the report as "temperature not recorded" rather than
    let a silent zero pretend the instrument was at its reference temperature.
    :meth:`UncertaintyBudget.add` accepts ``None`` for exactly this reason.
    """
    if delta_c is None:
        return None
    tempco = _finite(f"{label}: ppm_per_c", ppm_per_c)
    delta = _finite(f"{label}: delta_c", delta_c)
    magnitude = _finite(f"{label}: value", value)
    half_width = abs(tempco) * 1e-6 * abs(magnitude) * abs(delta)
    return UncertaintyComponent.from_half_width(
        label, half_width, distribution, dof=dof, sensitivity=sensitivity,
        source=source or f"{tempco:g} ppm/C over {delta:+g} C")


# ------------------------------------------------------------ presentation

def _fmt_number(x: float, width: int = 11) -> str:
    return f"{x:>{width}.4g}"


def _fmt_dof(dof: float) -> str:
    if math.isinf(dof):
        return "inf"
    if float(dof).is_integer():
        return str(int(dof))
    return f"{dof:.4g}"


def format_budget(
    budget: UncertaintyBudget,
    unit: str,
    *,
    confidence: float = 0.95,
    sort: bool = True,
) -> str:
    """Plain-text budget table, ready to drop into a report.

    Columns are label, distribution, type, degrees of freedom, ``u_i``,
    ``c_i``, ``c_i*u_i`` and percent of variance; the summary line carries
    ``u_c``, ``nu_eff``, ``k`` and ``U``.  Rows are ordered by dominance
    unless ``sort=False``, in which case declaration order is kept.

    *unit* is a label only.  Every component must already be in it.
    """
    p = _check_confidence(confidence)
    components = list(budget.components)
    if sort:
        components.sort(key=lambda c: abs(c.contribution), reverse=True)

    variance = budget.variance()
    label_width = max([len("component")] + [len(c.label) for c in components])
    dist_width = max([len("distribution")]
                     + [len(c.distribution) for c in components])

    header = (f"{'component':<{label_width}}  {'distribution':<{dist_width}}  "
              f"{'ty':<2}  {'dof':>6}  {'u_i':>11}  {'c_i':>11}  "
              f"{'c_i*u_i':>11}  {'%var':>6}")
    rule = "-" * len(header)
    lines = [f"Uncertainty budget [{unit}]", header, rule]

    for c in components:
        share = 100.0 * c.variance / variance if variance > 0.0 else 0.0
        lines.append(
            f"{c.label:<{label_width}}  {c.distribution:<{dist_width}}  "
            f"{c.type:<2}  {_fmt_dof(c.dof):>6}  {_fmt_number(c.value)}  "
            f"{_fmt_number(c.sensitivity)}  {_fmt_number(c.contribution)}  "
            f"{share:>6.2f}")

    u_c = budget.combined()
    nu_eff = budget.effective_dof()
    k = budget.coverage_factor(p)
    lines.append(rule)
    lines.append(f"combined standard uncertainty u_c = {u_c:.4g} {unit}")
    lines.append(f"effective degrees of freedom nu_eff = {_fmt_dof(nu_eff)}")
    lines.append(f"coverage factor k({p * 100:.4g} %) = {k:.3f}")
    lines.append(f"expanded uncertainty U = k*u_c = {k * u_c:.4g} {unit} "
                 f"({p * 100:.4g} % confidence)")
    return "\n".join(lines)


# ----------------------------------------------------------- serialisation

def budget_to_rows(budget: UncertaintyBudget) -> list[dict[str, str]]:
    """Budget -> CSV-ready rows, one dict per component, keys :data:`BUDGET_FIELDS`.

    Floats are written with ``repr``, which round-trips exactly through
    ``float()`` -- including ``inf`` for a Type B component's degrees of
    freedom.  Only the *inputs* are stored: ``u_c``, ``nu_eff``, ``k`` and
    ``U`` are all derivable, and storing a derived number next to its inputs
    is an invitation for the two to disagree.
    """
    return [
        {
            "label": c.label,
            "value": repr(c.value),
            "dof": repr(c.dof),
            "type": c.type,
            "distribution": c.distribution,
            "sensitivity": repr(c.sensitivity),
            "source": c.source,
        }
        for c in budget.components
    ]


def budget_from_rows(rows: Iterable[Mapping[str, object]]) -> UncertaintyBudget:
    """Rows -> budget, the inverse of :func:`budget_to_rows`.

    Accepts anything mapping-like with the :data:`BUDGET_FIELDS` keys, so a
    ``csv.DictReader`` can be handed straight in.  Numbers may arrive as
    strings or as floats.  Extra keys are ignored -- a stored result file will
    carry the measurement columns alongside these.  This is what makes a
    stored result auditable: the whole budget, and therefore ``u_c``,
    ``nu_eff`` and ``U``, is re-derivable offline from the file itself.
    """
    components = []
    for index, row in enumerate(rows):
        missing = [f for f in BUDGET_FIELDS if f not in row and f != "source"]
        if missing:
            raise UncertaintyError(
                f"budget row {index}: missing field(s) "
                f"{', '.join(missing)}")
        label = str(row["label"])
        components.append(UncertaintyComponent(
            label=label,
            value=_finite(f"row {index} value", row["value"]),
            dof=_dof(f"row {index} dof", row["dof"]),
            type=str(row["type"]).strip().upper(),
            distribution=str(row["distribution"]).strip(),
            sensitivity=_finite(f"row {index} sensitivity", row["sensitivity"]),
            source=str(row.get("source", "") or ""),
        ))
    return UncertaintyBudget(tuple(components))

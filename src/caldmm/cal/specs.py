"""Instrument specification model: limits, ranges, bands, modes and the loader.

This is the schema the rest of :mod:`caldmm.cal` keys off.  A per-model YAML
file under ``caldmm/cal/data/`` describes one instrument; this module parses it
strictly into frozen dataclasses and answers the only question that matters at
a test point: *what is the tolerance here?*

The limit model
---------------

A real DMM or calibrator specification is three terms, not two::

    limit(x, fs) = ppm_reading * |x| * 1e-6 + ppm_range * fs * 1e-6 + abs_floor

At ``x = 0`` this collapses to the range term, which is exactly what makes a
zero or short test point meaningful.  A "ppm of reading plus a floor" model
cannot express a real datasheet and is not built here.

:func:`evaluate_limit` therefore returns an **absolute** limit in the
quantity's unit.  Callers derive ppm from it when the reading is non-zero;
storing only ppm is what makes zero points nonsense.

Datasheets print percent (Fluke, Agilent) or ppm (3458A).  The loader accepts
both spellings and normalises to ppm at load time, so exactly one
representation reaches the rest of the code.

Conditions a three-term line cannot carry
-----------------------------------------

Manufacturers attach conditions to those three terms, and a schema that cannot
say them forces the transcriber to either invent a number or drop a footnote.
Every key below is **optional**; a file that names none of them behaves exactly
as before.

============================  ======  ====================================
YAML key                      Where   What it says
============================  ======  ====================================
``ppm_reading_hv``            range   ppm of reading growing with the
``hv_ref_v``                          square of the reading, applied only
``hv_above_v``                        above ``hv_above_v``.  3458A DCV
                                      footnote 6: 12 ppm x (Vin/1000)^2
                                      for inputs above 100 V.
``ppm_reading_isq``           range   the same shape for a current: 5700A
``isq_ref_a``                         dc-current footnote 1, +/-200 x I^2
``isq_above_a``                       ppm above 100 mA and +/-10 x I^2 ppm
                                      above 1 A.
``connection_adders``         range   per-range wiring penalty, keyed by
                                      connection.  Wins over the
                                      per-connection ``adder``.
``below_frac_adder``          range,  terms added only below a fraction of
                              band    full scale.  5700A wideband note [1].
``overrange``                 range   how far past ``fs`` the range still
                                      reads, as a multiple of ``fs``
                                      (1.2 = 20 %).  Used when
                                      *configuring* a meter, never when
                                      evaluating a tolerance.
``min_reading_frac_by_mode``  range   the per-mode form of
                                      ``min_reading_frac`` (3458A SYNC
                                      10 % of scale vs ANA 25 %).
``test_current_a``            range   the current an ohms range sources,
                                      in amps.  A calibrator's resistance
                                      uncertainty is stated only inside a
                                      load-current window, so the pairing
                                      has to know it.
``regime_adder``              range   an unconditional adder for the
                                      operating regime every run here is
                                      taken in -- the 3458A dc table's
                                      "without MATH NULL and on a fixed
                                      range under 4 minutes" penalty.
                                      Added by ``evaluate_limit`` on every
                                      evaluation, in both roles.
``traceability``              func    the calibration-source uncertainty
                                      the datasheet says to add for
                                      *absolute* accuracy.  Never part of
                                      a tolerance.
``relative_to``               temp    a validity window centred on a
``half_width_c``                      run-time temperature (the 5700A's
``half_width_per_interval``           CAL_TEMP?, the 3458A's Tcal) rather
                                      than on a fixed one.
``spec_confidence``           root    the confidence the tables are stated
                                      at (5700A 0.99), for the budget.
============================  ======  ====================================

Role at a shared band edge
--------------------------

Where two frequency bands meet, the edge tone is printed in both rows and no
datasheet breaks the tie.  Which row is the *safe* one depends on which side of
the comparison the model is on, so :func:`select_band` and
:func:`evaluate_limit` take a :class:`PairRole`.  See :class:`Band`.

Capabilities live here too
--------------------------

"What can this meter do" is ``model_spec.functions`` and ``function.ranges``.
There is deliberately no parallel capability hierarchy.

Import hygiene
--------------

Only :mod:`caldmm.cal.quantities`, the stdlib and ``yaml``.  No driver import,
ever -- the adapter layer is the only part of ``cal`` allowed to do that.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import yaml

from .quantities import Connection, Quantity

__all__ = [
    "SCHEMA_VERSION", "APPLY_KNOBS", "TEMPERATURE_REFERENCES", "SpecError",
    "LOAD_CURRENT_UNITS", "LOAD_ERROR_UNITS",
    "Role", "PairRole", "IntervalBasis", "LimitsAuthority",
    "LimitTerms", "QuadraticTerm", "BelowFractionAdder", "RegimeAdder", "Tempco",
    "LoadCurrent", "Mode", "Band", "RangeSpec",
    "ConnectionSpec", "Traceability", "FunctionSpec", "DriverRef",
    "TemperatureSpec", "ModelSpec", "LimitResult",
    "available_models", "load_model", "parse_model", "parse_model_text",
    "select_range", "select_band", "select_mode", "ambiguous_mode_siblings",
    "evaluate_limit",
]

SCHEMA_VERSION = 1

#: Package holding the shipped ``*.yaml`` specification files.
DATA_PACKAGE = "caldmm.cal.data"

#: The closed set of instrument knobs an ``apply:`` block may name.  Deliberately
#: not a method-call mini-DSL: an adapter maps these onto whatever the driver
#: calls them, and anything outside this set is rejected at load time.
APPLY_KNOBS: Mapping[str, tuple] = MappingProxyType({
    "nplc":                 (int, float),
    "ac_filter":            (str,),
    "ac_method":            (str,),
    "ac_band":              (str, list, tuple),
    "ocomp":                (bool,),
    "autozero":             (bool, str),
    "input_impedance_auto": (bool,),
    "resolution":           (int, float),
    "delay_s":              (int, float),
})

#: What a ``temperature.relative_to`` window may be centred on.  A closed set,
#: because the centre has to be a number some part of this project knows how to
#: go and read at run time: ``CAL_TEMP?`` on the 5700A, the ``ACAL``
#: temperature on the 3458A, the last self-calibration on the 1281.
TEMPERATURE_REFERENCES: tuple[str, ...] = ("cal_temp", "acal_temp",
                                           "selfcal_temp")

_EMPTY_MAP: Mapping[str, Any] = MappingProxyType({})


def _empty_map() -> "Mapping[str, Any]":
    """``default_factory`` for a mapping-typed dataclass field.

    A bare ``= _EMPTY_MAP`` default is rejected on **Python 3.11**: its
    dataclasses refuse any default whose type is unhashable, and
    ``mappingproxy`` is one (3.12 narrowed the check to ``list``/``dict``/
    ``set``).  Python 3.11 is the minimum supported version, so the factory form is the portable one.
    It hands back the *same* shared empty proxy, so nothing is allocated.
    """
    return _EMPTY_MAP
_ADAPTER_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class _SpecLoader(yaml.SafeLoader):
    """``SafeLoader`` with a YAML 1.2 float resolver.

    PyYAML implements YAML 1.1, whose float resolver demands a **signed**
    exponent: ``9.9e+37`` is a float but ``9.9e37`` is a *string*, as are
    ``1.0e10`` and ``1e-3``.  Nothing complains at parse time -- a spec file
    full of impedances, sentinels and delays loads perfectly and then fails
    much later inside a type check, or worse, silently compares as a string.
    A spec file is mostly numbers in exponent form, so resolve them the way
    YAML 1.2's core schema does and be done with it.
    """


#: YAML 1.2 core-schema float, i.e. the 1.1 form with the exponent sign made
#: optional.  Registered *after* the inherited resolvers, so integers still
#: resolve as integers (``_float`` widens them anyway).
_YAML_12_FLOAT = re.compile(r"""^(?:
     [-+]?(?:\.[0-9]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9]+)?
    |[-+]?\.(?:inf|Inf|INF)
    |\.(?:nan|NaN|NAN))$""", re.VERBOSE)

_SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float", _YAML_12_FLOAT, list("-+0123456789."))


class SpecError(ValueError):
    """Any problem loading, validating or interpreting a specification."""


class Role(Enum):
    """What the instrument can be in a calibration pair."""

    METER = "meter"
    SOURCE = "source"
    BOTH = "both"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None


class PairRole(Enum):
    """Which side of a calibration comparison a model is on at this point.

    Not to be confused with :class:`Role`, which is what an instrument *can
    be* (a meter, a source, or either).  ``PairRole`` is what it *is doing*
    in one particular comparison, and it exists because two of the questions
    this module answers have opposite safe answers on the two sides: the DUT's
    figures are a **tolerance** to be judged against, so the tighter reading is
    the honest one, while the reference's are an **uncertainty** to be added to
    a budget, so the looser reading is.  A caller that does not know passes
    nothing and gets the documented default.
    """

    DUT = "dut"
    REFERENCE = "reference"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None


class IntervalBasis(Enum):
    """What the specification intervals are measured *from*.

    A single "interval" axis lies about most instruments: the 3458A's specs
    run from its last ACAL and ``Tcal``, a typical bench DMM's from a
    calendar calibration date, and a self-calibrating meter's partly from its
    last self-cal.  A report that does not say which is not honest.
    """

    SINCE_CAL = "since_cal"
    SINCE_ACAL = "since_acal"
    SINCE_SELFCAL = "since_selfcal"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None


class LimitsAuthority(Enum):
    """Who decides the tolerance at a test point: this file, or the instrument.

    Every meter here is ``FILE`` -- a 34401A cannot tell you its own accuracy,
    so the transcribed datasheet is the only authority there is.  The Fluke
    5700A can: ``UNCERT?`` returns the firmware-computed uncertainty for the
    output that is actually programmed, honouring ``CAL_INTV``, ``CAL_CONF``,
    the range, the frequency and the calibration temperature.  Its transcribed
    tables are therefore a **cross-check**, not the verdict, and a consumer
    that treats them as the verdict is wrong in a way no test here can see.
    Marking the difference in the data is the only way that distinction
    survives into a report.
    """

    FILE = "file"
    INSTRUMENT = "instrument"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class QuadraticTerm:
    """A ppm-of-reading term that grows with the *square* of the reading.

    Two manufacturer footnotes have this shape and no three-term line can hold
    either of them:

    * 3458A DCV footnote 6 -- "Add 12 ppm X (Vin / 1000)2 additional error for
      inputs > 100 V" (``3458a-datasheet.txt:460``, PDF p.12).
    * 5700A dc-current footnote 1 -- "Add to uncertainty specifications:
      +/-200 x I2 ppm for >100 mA on 220 mA range; +/-10 x I2 ppm for >1 A on
      2.2 A range" (``5700a-operators-supplement-2014.txt:1085``, PDF p.19).

    Both read as **ppm of reading**: the added ppm is ``ppm_at_ref`` at
    ``|x| == ref`` and scales as ``(|x|/ref)^2``, so the absolute contribution
    is cubic in the reading.  The 3458A datasheet does not say whether its
    12 ppm is of reading or of range (the two agree at 1000 V and differ 2:1 at
    500 V); ppm of reading is the tighter figure and therefore the conservative
    one for a DUT tolerance.  For the 5700A the manual settles it -- an adder
    "to uncertainty specifications", which are ppm of output.

    Substituting a flat ``ppm_range`` for either is exact only at full scale
    and several times too loose below it, which is why this is a term of its
    own rather than a fudged constant.
    """

    #: ppm of reading contributed when ``|value| == ref``.
    ppm_at_ref: float
    #: The reading the coefficient is quoted at, in the quantity's unit.
    ref: float
    #: Applies only *strictly above* this magnitude -- the footnotes say
    #: "inputs > 100 V" and ">100 mA", so the threshold itself is outside.
    above: float = 0.0

    def ppm(self, value: float) -> float:
        """ppm of reading this term adds at *value*, zero below the threshold."""
        if abs(value) <= self.above:
            return 0.0
        return self.ppm_at_ref * (abs(value) / self.ref) ** 2

    def adder(self, value: float) -> float:
        """Absolute contribution at *value*, in the quantity's unit."""
        return self.ppm(value) * abs(value) * 1e-6

    def describe(self, unit: str = "") -> str:
        ref = f"{self.ref:g} {unit}".strip()
        above = f"{self.above:g} {unit}".strip()
        return (f"{self.ppm_at_ref:g} ppm rdg x (|x|/{ref})^2 "
                f"above {above}")


@dataclass(frozen=True, slots=True)
class RegimeAdder:
    """An adder for the operating regime a run is actually taken in.

    Some specification tables are written for conditions a real run does not
    meet, and state the penalty for not meeting them.  The 3458A's dc volts
    table is the case this exists for: it assumes MATH NULL on a range held
    more than four minutes, and says "Without MATH NULL and for fixed range
    less than 4 minutes, add 0.25 ppm of range to 10 V, 1.7 ppm of range to
    1 V and 17 ppm of range to 0.1 V" (``3458a-datasheet.txt:454-456``, PDF
    p.12).  This code base never issues MATH NULL and re-selects the range for
    every point, so that is the regime *every* reading here is taken in --
    which makes the adder unconditional rather than conditional, and a
    specification line that is evaluated without it simply wrong.

    It is therefore not a second range table and not a footnote to be read by a
    human: it is added by :func:`evaluate_limit` on every evaluation of the
    range that declares it, in both roles, and it prints itself with its own
    ``note`` so a report says *why* the number is bigger than the data sheet's
    printed row.  ``LimitResult.base_terms`` stays the printed row.

    A file that changes the regime -- an adapter that nulls each range and
    dwells four minutes -- changes or drops the key; the data sheet's figures
    for the other regimes belong in the YAML comment beside it.
    """

    ppm_reading: float = 0.0
    ppm_range: float = 0.0
    abs_floor: float = 0.0
    #: Why this is added, in a few words: it is printed in ``describe()``.
    note: str = ""

    def adder(self, value: float, fs: float) -> float:
        """Absolute contribution at *value* on a range of full scale *fs*."""
        return ((self.ppm_reading * abs(value) + self.ppm_range * abs(fs)) * 1e-6
                + self.abs_floor)

    def as_terms(self) -> "LimitTerms":
        """This adder as something ``LimitTerms.__add__`` can carry."""
        return LimitTerms(regime=(self,))

    def describe(self, unit: str = "") -> str:
        parts = []
        if self.ppm_reading:
            parts.append(f"{self.ppm_reading:g} ppm rdg")
        if self.ppm_range:
            parts.append(f"{self.ppm_range:g} ppm rng")
        if self.abs_floor:
            parts.append(f"{self.abs_floor:g} {unit}".strip())
        body = " + ".join(parts) if parts else "0"
        return f"+{body} ({self.note})" if self.note else f"+{body}"


@dataclass(frozen=True, slots=True)
class LimitTerms:
    """The three terms of a specification line, always in ppm plus an absolute.

    ``abs_floor`` is in the quantity's unit (volts, amps, ohms).

    ``quadratic`` carries any :class:`QuadraticTerm` the range attaches to this
    line.  It rides along inside ``LimitTerms`` -- rather than being applied by
    ``evaluate_limit`` -- so that every existing consumer of ``.limit()`` and
    ``.describe()`` gets it without being taught anything, and so that a line
    which has one cannot be quietly evaluated without it.

    ``regime`` carries any :class:`RegimeAdder` ``evaluate_limit`` added for
    the conditions the run is taken in.  It rides along rather than being
    summed into ``ppm_range`` so that ``describe()`` can say what it is and why
    -- a report that shows ``3 ppm rng`` where the data sheet prints ``3`` and
    a separate ``+17 ppm rng (no MATH NULL, fixed range < 4 min)`` is auditable;
    one that shows ``20 ppm rng`` is not.
    """

    ppm_reading: float = 0.0
    ppm_range: float = 0.0
    abs_floor: float = 0.0
    quadratic: tuple[QuadraticTerm, ...] = ()
    regime: tuple[RegimeAdder, ...] = ()

    def limit(self, value: float, fs: float) -> float:
        """Absolute tolerance at reading *value* on a range of full scale *fs*."""
        total = ((self.ppm_reading * abs(value) + self.ppm_range * abs(fs)) * 1e-6
                 + self.abs_floor)
        for term in self.quadratic:
            total += term.adder(value)
        for term in self.regime:
            total += term.adder(value, fs)
        return total

    def __add__(self, other: "LimitTerms") -> "LimitTerms":
        if not isinstance(other, LimitTerms):
            return NotImplemented
        return LimitTerms(self.ppm_reading + other.ppm_reading,
                          self.ppm_range + other.ppm_range,
                          self.abs_floor + other.abs_floor,
                          self.quadratic + other.quadratic,
                          self.regime + other.regime)

    def describe(self, unit: str = "") -> str:
        parts = []
        if self.ppm_reading:
            parts.append(f"{self.ppm_reading:g} ppm rdg")
        if self.ppm_range:
            parts.append(f"{self.ppm_range:g} ppm rng")
        if self.abs_floor:
            parts.append(f"{self.abs_floor:g} {unit}".strip())
        parts.extend(term.describe(unit) for term in self.quadratic)
        text = " + ".join(parts) if parts else "0"
        for term in self.regime:
            text += " " + term.describe(unit)
        return text


@dataclass(frozen=True, slots=True)
class BelowFractionAdder:
    """Terms that apply only *below* a fraction of full scale.

    The 5700A's wideband flatness footnote [1] --  "For output voltages < 50 %
    of full range in the 33 mV, 110 mV, 330 mV, 1.1 V, and 3.5 V ranges, add
    0.1 % to the amplitude flatness specification"
    (``5700a-operators-supplement-2014.txt:1476``, PDF p.26) -- is a condition
    on the *setpoint*, not a second range, and folding it into the row would
    make the row 0.1 % pessimistic over the top half of the range.

    The comparison is strict, as the footnote's "< 50 %" is: at exactly half
    scale nothing is added.
    """

    below_frac: float
    terms: LimitTerms
    note: str | None = None

    def applies(self, value: float, fs: float) -> bool:
        return fs > 0.0 and abs(value) < self.below_frac * fs

    def describe(self, unit: str = "") -> str:
        return (f"{self.terms.describe(unit)} below "
                f"{self.below_frac * 100:g} % of range")


@dataclass(frozen=True, slots=True)
class Tempco:
    """Temperature coefficient, per degree C, same three-term shape."""

    ppm_reading_per_c: float = 0.0
    ppm_range_per_c: float = 0.0
    abs_floor_per_c: float = 0.0

    def adder(self, value: float, fs: float, delta_c: float) -> float:
        """Absolute error added by being *delta_c* degrees off the reference."""
        terms = LimitTerms(self.ppm_reading_per_c, self.ppm_range_per_c,
                           self.abs_floor_per_c)
        return terms.limit(value, fs) * abs(delta_c)


#: Units the manual's derating equations express their currents in.  Amperes
#: are deliberately not one of them: the constants are quoted against these
#: units and rescaling K would turn a transcribed number into a derived one.
LOAD_CURRENT_UNITS = ("mA", "uA")
#: Units a derating error comes out in.
LOAD_ERROR_UNITS = ("ppm", "mohm")

#: How many of :data:`LOAD_CURRENT_UNITS` there are in one ampere.
_LOAD_CURRENT_PER_AMP = {"mA": 1e3, "uA": 1e6}


@dataclass(frozen=True, slots=True)
class LoadCurrent:
    """The load current a **source's** specification is stated for.

    A calibrator's resistance figure is not a property of the resistor alone:
    it holds while the meter draws a current inside the "Full Spec Load Range
    IL - IU", and outside that window the manufacturer states an adder rather
    than a wider tolerance
    (``5700a-operators-manual-1996.txt:1960-2009``, PDF p.49).  A 3458A reads
    19 ohm at 1 mA where IL is 8 mA, so the 31 ppm one-year figure does not
    apply as written; the derating table (``:2030-2085``, PDF p.50) says how
    much to add.

    Two equations, from the derating table's own notes::

        I < IL              Error = K(IL - I)/(IL x I)
        IU < I < IMAX       Error = K(I^2 - IU^2)          [ppm]

    **The unit rule is the trap.**  The manual expresses the currents in mA or
    uA depending on the row, and switches at a *different* row for the two
    equations -- 10 kohm upwards take uA under-current but 100 kohm upwards
    take uA over-current, so the 10 kohm and 19 kohm rows take uA in one and mA
    in the other.  ``unit`` and ``over_unit`` carry that per row rather than
    normalising K, and the under-current error is in mohm (not ppm) for the
    two-wire column and for the four-wire short.

    ``min_a``/``max_a``/``imax_a`` are in **amperes**, like every other
    physical quantity in a spec file; only the K's keep the manual's units.

    This is a **test-condition adder, not a tolerance**: :func:`evaluate_limit`
    ignores it, because the limit does not know what current the paired meter
    sources.  It belongs to plan generation and to the uncertainty budget.
    """

    #: IL, the smallest load current the specification is stated for.
    min_a: float
    #: IU, the largest load current the specification is stated for.
    max_a: float
    #: IMAX, the largest peak current the value tolerates at all.
    imax_a: float | None = None
    #: Unit ``I`` and ``IL`` are expressed in for the ``I < IL`` equation.
    unit: str = "mA"
    #: K for a four-wire connection below IL.
    k_under_4w: float | None = None
    #: Unit :attr:`k_under_4w` yields -- ``ppm`` everywhere except the short,
    #: where a ppm of a 0 ohm output would be identically zero.  A file that
    #: states ``k_under_4w`` must state this too; the default is for the case
    #: where neither is stated.
    k_under_4w_unit: str = "ppm"
    #: K for an actively compensated two-wire connection below IL, always in
    #: mohm.  ``None`` where the manual's column is blank, which is exactly
    #: where two-wire compensation is not offered.
    k_under_2w: float | None = None
    #: Unit ``I`` and ``IU`` are expressed in for the ``IU < I`` equation.
    over_unit: str | None = None
    #: K for a four-wire connection above IU, always in ppm.
    k_over_4w: float | None = None

    def in_window(self, current_a: float) -> bool:
        """True when *current_a* is inside the full-spec load range.

        Both edges are inclusive: the derating table is for loads *outside*
        IL - IU.
        """
        current = abs(current_a)
        return (self.min_a * (1.0 - 1e-12) <= current
                <= self.max_a * (1.0 + 1e-12))

    def under_error(self, current_a: float, *, two_wire: bool = False
                    ) -> tuple[float, str] | None:
        """``K(IL - I)/(IL x I)`` as ``(value, unit)``, or ``None``.

        ``None`` when the current is not below IL, or when the manual states
        no constant for this connection.  *two_wire* selects the actively
        compensated two-wire column.
        """
        constant = self.k_under_2w if two_wire else self.k_under_4w
        if constant is None:
            return None
        current = abs(current_a)
        if current <= 0.0 or current >= self.min_a:
            return None
        scale = _LOAD_CURRENT_PER_AMP[self.unit]
        i_low = self.min_a * scale
        i_now = current * scale
        unit = "mohm" if two_wire else self.k_under_4w_unit
        return (constant * (i_low - i_now) / (i_low * i_now), unit)

    def over_error(self, current_a: float) -> tuple[float, str] | None:
        """``K(I^2 - IU^2)`` in ppm as ``(value, "ppm")``, or ``None``.

        ``None`` when the current is not above IU, or when the manual's cell
        is blank.  Above :attr:`imax_a` the point is not permitted at all,
        which is the caller's check, not this one's.
        """
        if self.k_over_4w is None or self.over_unit is None:
            return None
        current = abs(current_a)
        if current <= self.max_a:
            return None
        scale = _LOAD_CURRENT_PER_AMP[self.over_unit]
        i_high = self.max_a * scale
        i_now = current * scale
        return (self.k_over_4w * (i_now * i_now - i_high * i_high), "ppm")


@dataclass(frozen=True, slots=True)
class Mode:
    """A model-defined measurement mode within one function.

    The 3458A's ``SETACV``/``ACBAND`` choices and the 34401A's ac filter are
    modes: they change both *what can be measured at a frequency* and *what the
    specification is*.  A schema that records only one of the two is wrong, so
    a mode carries its own validity window and its own ``apply`` knobs, while
    the per-mode specification lives in the bands that name it.
    """

    id: str
    freq_low_hz: float | None = None
    freq_high_hz: float | None = None
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    note: str | None = None

    def covers(self, freq_hz: float | None) -> bool:
        if freq_hz is None:
            return True
        if self.freq_low_hz is not None and freq_hz < self.freq_low_hz:
            return False
        if self.freq_high_hz is not None and freq_hz > self.freq_high_hz:
            return False
        return True


@dataclass(frozen=True, slots=True)
class Band:
    """A frequency band *within a range*.

    Bands live in ranges, not in functions, because a meter's 100-300 kHz spec
    does not exist on every range -- the 34401A's 750 V ac range stops at
    100 kHz while its 10 V range goes to 300 kHz.

    Endpoints are inclusive, so a table whose rows meet at 20 kHz leaves the
    edge tone printed in two rows.  Which row applies there is **role
    dependent**, and :func:`select_band` decides it:

    * as the **DUT** the tighter row applies.  That is not a convenience: it is
      what the manufacturers' own verification tables do.  The 34401A Service
      Guide judges 10 V at 50 kHz against +/- 14 mV, the 20-50 kHz row
      (``34401a-service.txt:2931``, PDF p.76), and 1 V at 100 kHz against
      +/- 6.3 mV, the 50-100 kHz row (``:3039``, p.78) -- while at 10 Hz it
      uses the 10 Hz-20 kHz row (``:2932``), which is the *higher* one there.
      "Tighter", not "lower", is the rule that fits all three.
    * as the **reference** the looser row applies, because the same figure is
      then an uncertainty to be added to a budget rather than a limit to be
      judged against, and understating it is what produces a false PASS.
    * with **no role given** the historical rule stands -- the
      higher-frequency band, ties broken by declaration order -- so callers
      that have not been taught the distinction do not silently change their
      numbers.

    Within one span the rule never applies: a range whose bands are a *mode
    partition* of one frequency row (the 34401A writes ``[20, 40]`` twice, once
    per ac filter, best first) is decided by declaration order, which is the
    author's deliberate ordering.
    """

    freq_low_hz: float
    freq_high_hz: float
    modes: tuple[str, ...] = ()
    limits: Mapping[str, LimitTerms] = field(default_factory=_empty_map)
    tempco: Tempco | None = None
    #: Largest output this band allows, when it is smaller than the range's
    #: full scale.  The 5700A's 1100 V ac range reaches only 250 V between
    #: 15 and 50 Hz; without this the plan generator would emit a 1000 V,
    #: 20 Hz point that the calibrator refuses.
    max_out: float | None = None
    #: Terms that apply only below a fraction of the range's full scale.  A
    #: band that states one replaces the range's rather than stacking with it,
    #: so a range can carry the general case and a band say "not here".
    below_frac_adder: BelowFractionAdder | None = None
    note: str | None = None

    def covers(self, freq_hz: float | None) -> bool:
        if freq_hz is None:
            return False
        return self.freq_low_hz <= freq_hz <= self.freq_high_hz


@dataclass(frozen=True, slots=True)
class RangeSpec:
    """One range of one function.

    ``range_arg`` is what a driver's ``max_input``/range parameter wants;
    ``range_id`` is a source-side range identifier (the 5700A's section 10
    names) when the model has one.
    """

    fs: float
    range_arg: float | None = None
    limits: Mapping[str, LimitTerms] = field(default_factory=_empty_map)
    bands: tuple[Band, ...] = ()
    tempco: Tempco | None = None
    min_reading_frac: float | None = None
    resolution: float | None = None
    input_impedance_ohm: float | None = None
    settle_s: float | None = None
    range_id: str | None = None
    #: Smallest magnitude a **source** can produce on this range, absolute, in
    #: the quantity's unit.  Distinct from ``min_reading_frac``, which is the
    #: meter-side "the datasheet says nothing below this fraction of range":
    #: the 5700A's figures are a mix of "10 % of range" and two flat absolutes
    #: (9 uA on the ac 220 uA range, 100 V on the dc 1100 V range), so a
    #: fraction cannot express them without turning a stated number into a
    #: derived one.
    min_out: float | None = None
    #: Output impedance of a source on this range.  50 ohm on the 5700A's
    #: 220 mV dc range is not decoration: into a 10 Mohm meter that is 5 ppm,
    #: which is larger than several of the floors in this very file.
    source_impedance_ohm: float | None = None
    #: True when the instrument sources a *characterized* value near the
    #: nominal rather than the nominal itself -- the 5700A's resistances are
    #: up to 500 ppm off nominal, and the readback, not the request, is the
    #: reference.
    characterized: bool = False
    #: Wiring topologies legal on this range specifically.  Empty means "every
    #: connection the function declares".  The 5700A has 4-wire sensing on
    #: every resistance except 100 Mohm, and RCOMP only at 19 kohm and below.
    connections: tuple[Connection, ...] = ()
    #: True when this "range" is a single catalogue value rather than a span.
    #: The 5700A's resistances are 17 discrete values plus a short; anything
    #: else faults.  Treating them as a ladder is actively dangerous, because
    #: the calibrator sources a *characterized* value near the nominal: a
    #: 19 kohm readback of 19003.8 ohm would fall through to the 100 kohm entry
    #: and be judged against a looser specification than the one that applies.
    discrete: bool = False
    #: How far past ``fs`` this range still reads, as a **multiple** of ``fs``:
    #: 1.2 is the 20 % overrange the 34401A and 34420A specify on every range
    #: but 1000 Vdc and 3 A.  It says what a meter may be *configured* on; it
    #: never widens a tolerance, because the ppm-of-range term is written
    #: against the nominal full scale and nothing else.
    overrange: float | None = None
    #: Wiring penalty for this range specifically, keyed by connection.  The
    #: 5700A's two-wire active-compensation adder is 10 mohm at 1 kohm and
    #: 100 mohm at 19 kohm, which one per-connection figure cannot express;
    #: a range that names a connection here replaces
    #: :attr:`ConnectionSpec.adder` for that connection rather than stacking.
    connection_adders: Mapping[Connection, LimitTerms] = field(default_factory=_empty_map)
    #: Terms that apply only below a fraction of ``fs``; see
    #: :class:`BelowFractionAdder`.  A band may override it.
    below_frac_adder: BelowFractionAdder | None = None
    #: Per-mode form of ``min_reading_frac``, keyed by mode id.  One floor per
    #: range has to be the most restrictive of the modes, which then deletes
    #: points the pinned mode does specify: the 3458A's 10 mV ac range is
    #: specified to 10 % of full scale in SYNC but only to 1/4 of it in ANA.
    min_reading_frac_by_mode: Mapping[str, float] = field(default_factory=_empty_map)
    #: The load-current window a **source's** figure for this range is stated
    #: for, and the manufacturer's derating constants outside it.  See
    #: :class:`LoadCurrent`; never part of a tolerance.
    load_current: LoadCurrent | None = None
    #: The current an ohms range sources, in amps -- the 3458A's 100 ohm range
    #: sources 1 mA (``3458a-user-guide.txt:3053``, Table 3-6, PDF p.89).  It
    #: is here because a calibrator states its resistance uncertainty only
    #: inside a load-current window: the 5700A's 19 ohm figure holds for
    #: 8-11 mA, and a meter reading it at 1 mA adds an error the calibrator's
    #: own specification does not cover.  Inert in this module -- nothing here
    #: reads it; the pairing in ``plan`` does.
    test_current_a: float | None = None
    #: The manufacturer states this range's figures as **typical**
    #: performance, not as a specification (3458A data sheet p.21 footnote 6
    #: on its 100 nA - 10 uA dc ranges).  The limits are still evaluated -- a
    #: report should show how far off the reading is -- but a typical figure
    #: is not a warranted limit, so the engine treats it as an unmet condition
    #: of the claim: such a point is INDETERMINATE, never PASS and never FAIL.
    typical: bool = False
    #: An adder for the operating regime every run in this code base is taken
    #: in -- see :class:`RegimeAdder`.  Unconditional: ``evaluate_limit`` adds
    #: it on every evaluation of this range, in both roles.
    regime_adder: RegimeAdder | None = None
    #: Instrument knobs this range needs that the function's own ``apply``
    #: cannot express, from the same closed :data:`APPLY_KNOBS` vocabulary.
    #: A knob whose right value *changes with the range* has nowhere else to
    #: live: the 3458A's resistance ``delay_s`` has to grow with the range
    #: because the cable's dielectric absorption is discharged through the
    #: range's own test current, and one figure for the whole function would
    #: either be too short at 10 Mohm or waste a second a point at 10 ohm.
    #: Overrides the function's value for the same knob; a mode overrides
    #: both, being nested inside the range.
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    note: str | None = None

    def holds(self, value: float, allow_overrange: bool = False) -> bool:
        """True when ``|value|`` fits on this range.

        Strict by default: 11 V does not fit a 10 V range, because that is the
        range whose *specification* covers it.  ``allow_overrange=True`` asks
        the other question -- can the instrument be *set* to this range and
        still read the value -- and is answered from :attr:`overrange`, which
        a range that does not declare one never grants.

        A ``discrete`` range holds only its own nominal value: a readback that
        is not a catalogue member belongs to no range, and the caller has to
        say which one it asked for.  Overrange is meaningless there.
        """
        if self.discrete:
            return math.isclose(abs(value), self.fs, rel_tol=1e-9, abs_tol=1e-12)
        ceiling = self.fs
        if allow_overrange and self.overrange is not None:
            ceiling *= self.overrange
        return abs(value) <= ceiling * (1.0 + 1e-12)

    def allows(self, connection: Connection) -> bool:
        """True when *connection* is legal on this range."""
        return not self.connections or connection in self.connections

    def adder_for(self, connection: Connection) -> LimitTerms | None:
        """This range's own wiring adder for *connection*, if it states one."""
        return self.connection_adders.get(connection)

    def min_reading_frac_for(self, mode_id: str | None = None) -> float | None:
        """The reading floor that applies, preferring the pinned mode's own.

        An unknown or unpinned mode falls back to the range-wide figure, which
        is the most restrictive of the modes and therefore never optimistic.
        """
        if mode_id is not None and mode_id in self.min_reading_frac_by_mode:
            return self.min_reading_frac_by_mode[mode_id]
        return self.min_reading_frac


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """One wiring topology of a function: which driver function, and any adder.

    The 34401A's resistance specs are for 4-wire ohms; in 2-wire without a math
    null you add 0.2 ohm of lead resistance.  That is an ``adder``, not a
    different range table.
    """

    connection: Connection
    func: str | None = None
    adder: LimitTerms | None = None
    #: Per-interval form of ``adder``, for the models whose wiring penalty
    #: grows with time since calibration.  The 34401A's 2-wire lead resistance
    #: is a flat 0.2 ohm; the 3458A's is 50 mohm at 24 hours and 500 mohm at
    #: two years, and collapsing that to one figure is wrong in both
    #: directions -- too tight at 2 years, and (far worse) ten times too loose
    #: at 24 hours, which passes a meter that should fail.
    adder_per_interval: Mapping[str, LimitTerms] = field(default_factory=_empty_map)
    note: str | None = None

    def adder_for(self, interval: str | None) -> LimitTerms | None:
        """The adder that applies for *interval*, or ``None`` when there is none."""
        if interval is not None and interval in self.adder_per_interval:
            return self.adder_per_interval[interval]
        return self.adder


@dataclass(frozen=True, slots=True)
class Traceability:
    """What the datasheet says to add for *absolute* accuracy.

    Meter specifications are relative to the standards the meter was
    calibrated against -- "All 3458A accuracy specifications are relative to
    the calibration standards.  Absolute accuracy of the 3458A is determined by
    adding these relative accuracies to the traceability of your calibration
    standard.  For DCV, 2 ppm is the traceability error from the factory"
    (``3458a-datasheet.txt:282-287``, PDF p.9; the per-function notes at
    ``:458``, ``:668`` and ``:838`` give 2 ppm dc volts, 3 ppm at 10 kohm,
    5 ppm dc current).

    This is an **uncertainty input, not a tolerance**: it is never added to a
    limit, and :func:`evaluate_limit` ignores it.  It belongs in the budget
    built when this meter is the reference, which is why it carries its own
    confidence (or coverage factor) instead of being folded into a spec line.
    ``confidence`` and ``k`` are two spellings of one thing; a file states at
    most one, and a file that states neither leaves the choice to the budget.
    """

    ppm_reading: float = 0.0
    abs_floor: float = 0.0
    confidence: float | None = None
    k: float | None = None
    note: str | None = None

    def half_width(self, value: float) -> float:
        """The traceability half-width at *value*, in the quantity's unit."""
        return self.ppm_reading * abs(value) * 1e-6 + self.abs_floor


@dataclass(frozen=True, slots=True)
class FunctionSpec:
    """One measurement function of a model."""

    quantity: Quantity
    func: str | None = None
    connections: Mapping[Connection, ConnectionSpec] = field(default_factory=_empty_map)
    modes: tuple[Mode, ...] = ()
    ranges: tuple[RangeSpec, ...] = ()
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    settle_s: float | None = None
    #: Volt-hertz ceiling for the whole function, when it has one: the product
    #: of amplitude and frequency an ac point may not exceed.  2.2e7 V*Hz on
    #: the 5700A (fault 820 beyond it).  ``None`` means "no such bound is
    #: recorded here", not "unbounded".
    volt_hertz_max: float | None = None
    #: The calibration-source uncertainty to add when this function's meter is
    #: the *reference*.  Never part of a tolerance -- see :class:`Traceability`.
    traceability: Traceability | None = None
    note: str | None = None

    def mode(self, mode_id: str) -> Mode | None:
        for candidate in self.modes:
            if candidate.id == mode_id:
                return candidate
        return None

    @property
    def mode_ids(self) -> tuple[str, ...]:
        return tuple(m.id for m in self.modes)


@dataclass(frozen=True, slots=True)
class DriverRef:
    """Where the driver for this model lives.  Strings only -- never imported here."""

    module: str
    cls: str
    func_enum: str | None = None


@dataclass(frozen=True, slots=True)
class TemperatureSpec:
    """The temperature window the accuracy figures are written for.

    The window is **per interval**, because it usually is on the instrument:
    the 34401A's 24-hour figures hold at 23 +/- 1 C but its 90-day and 1-year
    figures at 23 +/- 5 C.  A single model-wide window silently passes a
    24-hour verdict taken at 27 C.  ``spec_window_c`` is the fallback for any
    interval ``per_interval`` does not name.

    Some instruments state no absolute window at all: theirs is centred on a
    temperature only the instrument knows.  "Absolute Uncertainty +/- 5 C from
    calibration temperature" (``5700a-operators-manual-1996.txt:1219``, PDF
    p.33), and the 3458A's figures are written against ``Tcal``.  That is
    ``relative_to`` plus ``half_width_c``: the file states the width, the
    caller supplies the centre it read from the instrument (``CAL_TEMP?``,
    ``ACAL`` temperature) as ``reference_c``.  Nothing is invented -- without a
    centre in hand the relative window simply does not answer, and the model
    falls back to whatever absolute window it declares.
    """

    spec_window_c: tuple[float, float] | None = None
    tempco_ref_c: float | None = None
    per_interval: Mapping[str, tuple[float, float]] = field(default_factory=_empty_map)
    #: What the relative window is centred on: one of
    #: :data:`TEMPERATURE_REFERENCES`.  ``None`` means the model states only
    #: absolute windows (or none at all).
    relative_to: str | None = None
    #: Half-width of the relative window, in degrees C.
    half_width_c: float | None = None
    #: Per-interval half-widths, mirroring ``per_interval``: the 3458A's dc
    #: figures hold within Tcal +/- 1 C at 24 hours and Tcal +/- 5 C beyond it.
    half_width_per_interval: Mapping[str, float] = field(default_factory=_empty_map)

    def half_width_for(self, interval: str | None = None) -> float | None:
        """The relative half-width for *interval*, or ``None`` if not relative."""
        if interval is not None and interval in self.half_width_per_interval:
            return self.half_width_per_interval[interval]
        return self.half_width_c

    def window_for(self, interval: str | None = None,
                   reference_c: float | None = None) -> tuple[float, float] | None:
        """The absolute window these figures hold in, or ``None``.

        With a *reference_c* in hand a relative window resolves to absolute
        degrees and wins; without one the declared absolute window answers.
        """
        if reference_c is not None:
            half_width = self.half_width_for(interval)
            if half_width is not None:
                return (reference_c - half_width, reference_c + half_width)
        if interval is not None and interval in self.per_interval:
            return self.per_interval[interval]
        return self.spec_window_c

    def within_window(self, temperature_c: float,
                      interval: str | None = None,
                      reference_c: float | None = None) -> bool:
        window = self.window_for(interval, reference_c)
        if window is None:
            return True
        low, high = window
        return low <= temperature_c <= high


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything known about one instrument model."""

    model_id: str
    display_name: str
    role: Role
    functions: Mapping[Quantity, FunctionSpec]
    driver: DriverRef | None = None
    adapter: str | None = None
    #: Reading the instrument returns to mean "no valid reading": 9.9e37 on
    #: HP/Agilent, 1e35 on the Wavetek 1281.  A single such sample poisons an
    #: average, so every consumer must filter on the model's own value.
    overload_sentinel: float | None = None
    interval_basis: IntervalBasis = IntervalBasis.SINCE_CAL
    #: Whether the tables in this file are the verdict or only a cross-check.
    limits_authority: LimitsAuthority = LimitsAuthority.FILE
    intervals: tuple[str, ...] = ()
    default_interval: str | None = None
    temperature: TemperatureSpec = TemperatureSpec()
    #: The confidence level the tables in this file are stated at, as a
    #: fraction: the 5700A's are 99 % ("Fluke guarantees performance
    #: verification using specifications stated to 99% confidence level",
    #: ``5700a-operators-manual-1996.txt:1214``, PDF p.33) and the 1281's
    #: "95 % minimum".  ``None`` means the manufacturer states none, which is
    #: not the same as 100 %: a budget then has to treat the figure as a
    #: rectangular bound.
    spec_confidence: float | None = None
    notes: str | None = None
    source: str | None = None
    #: Factory options that change the figures, as declared in the file's
    #: ``options:`` block: ``{option: {quantity: {range fs: {interval:
    #: ppm_reading}}}}``.  Inert until :meth:`with_option` applies one.
    options: Mapping[str, Mapping] = field(default_factory=_empty_map)
    #: The options :meth:`with_option` has applied to this spec.
    applied_options: tuple[str, ...] = ()

    def function(self, quantity: Quantity) -> FunctionSpec | None:
        return self.functions.get(Quantity(quantity))

    def with_option(self, option: str) -> "ModelSpec":
        """This model with a factory option's figures in force.

        The 3458A data sheet prints Option 002's (high stability) dc-volts
        ppm-of-reading figures in parentheses beside the standard ones; they
        are **tighter**, so judging an Option 002 unit against the standard
        figures passes errors its own specification does not allow.  Only
        ``ppm_reading`` is replaced -- that is all the option changes.
        """
        key = str(option).strip().lstrip("0") or "0"
        table = None
        for name, body in self.options.items():
            if (str(name).strip().lstrip("0") or "0") == key:
                table, option = body, str(name)
                break
        if table is None:
            raise SpecError(
                f"{self.model_id} declares no option {option!r} (declared: "
                f"{', '.join(self.options) or 'none'})")
        functions = dict(self.functions)
        for quantity_name, per_range in table.items():
            quantity = Quantity(quantity_name)
            function = functions.get(quantity)
            if function is None:
                raise SpecError(f"{self.model_id} option {option}: no "
                                f"{quantity.name} function")
            ranges = []
            for range_spec in function.ranges:
                override = None
                for fs, per_interval in per_range.items():
                    if math.isclose(float(fs), range_spec.fs, rel_tol=1e-9):
                        override = per_interval
                if override is None:
                    ranges.append(range_spec)
                    continue
                limits = dict(range_spec.limits)
                for interval, ppm in override.items():
                    if interval not in limits:
                        raise SpecError(
                            f"{self.model_id} option {option}: "
                            f"{quantity.name} {range_spec.fs:g} has no "
                            f"{interval} row")
                    limits[interval] = replace(limits[interval],
                                               ppm_reading=float(ppm))
                ranges.append(replace(range_spec,
                                      limits=MappingProxyType(limits)))
            functions[quantity] = replace(function, ranges=tuple(ranges))
        return replace(self, functions=MappingProxyType(functions),
                       applied_options=self.applied_options + (option,))

    @property
    def quantities(self) -> tuple[Quantity, ...]:
        return tuple(self.functions)

    def is_overload(self, value: float) -> bool:
        """True when *value* is the model's invalid-reading sentinel.

        A missing or non-finite reading is an overload whatever the model
        declares; the sentinel comparison is on top of that, not instead of it.
        """
        if value is None or not math.isfinite(value):
            return True
        if self.overload_sentinel is None:
            return False
        return abs(value) >= abs(self.overload_sentinel) * 0.99


@dataclass(frozen=True, slots=True)
class LimitResult:
    """An evaluated limit, plus enough context to show the derivation."""

    quantity: Quantity
    value: float
    limit: float
    terms: LimitTerms
    interval: str
    connection: Connection
    range_spec: RangeSpec
    func: str | None = None
    band: Band | None = None
    mode: Mode | None = None
    base_terms: LimitTerms | None = None
    adder: LimitTerms | None = None
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    #: The window these figures hold in, already resolved for this interval --
    #: carried here so a consumer cannot accidentally check a 24-hour reading
    #: against the 1-year window.
    temperature_window_c: tuple[float, float] | None = None

    @property
    def unit(self) -> str:
        return self.quantity.unit

    @property
    def range_fs(self) -> float:
        return self.range_spec.fs

    @property
    def ppm(self) -> float | None:
        """The limit as ppm of reading, or ``None`` at a zero reading."""
        if self.value == 0.0:
            return None
        return self.limit / abs(self.value) * 1e6

    def describe(self) -> str:
        where = f"{self.value:g} {self.unit} on {self.range_spec.fs:g} range"
        if self.band is not None:
            where += f", {self.band.freq_low_hz:g}-{self.band.freq_high_hz:g} Hz"
        if self.mode is not None:
            where += f", mode {self.mode.id}"
        return (f"{where}, {self.interval}: "
                f"{self.terms.describe(self.unit)} = {self.limit:g} {self.unit}")


# --------------------------------------------------------------------------
# Strict parsing helpers
# --------------------------------------------------------------------------

class _Doc:
    """The document being parsed; carries the source name for error messages."""

    __slots__ = ("source",)

    def __init__(self, source: str) -> None:
        self.source = source

    def fail(self, path: str, message: str):
        raise SpecError(f"{self.source}: {path}: {message}")


def _type_name(node: Any) -> str:
    return type(node).__name__


def _mapping(doc: _Doc, node: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        doc.fail(path, f"expected a mapping, got {_type_name(node)}")
    for key in node:
        if not isinstance(key, str):
            doc.fail(path, f"key {key!r} is not a string")
    return node


def _sequence(doc: _Doc, node: Any, path: str) -> Sequence[Any]:
    if isinstance(node, (str, bytes)) or not isinstance(node, Sequence):
        doc.fail(path, f"expected a list, got {_type_name(node)}")
    return node


def _check_keys(doc: _Doc, node: Mapping[str, Any], path: str,
                allowed: Sequence[str], required: Sequence[str] = ()) -> None:
    allowed_set = set(allowed)
    for key in node:
        if key not in allowed_set:
            doc.fail(path, f"unknown key {key!r} "
                           f"(allowed: {', '.join(sorted(allowed_set))})")
    for key in required:
        if key not in node:
            doc.fail(path, f"missing required key {key!r}")


def _float(doc: _Doc, node: Mapping[str, Any], key: str, path: str,
           default: float | None = None) -> float | None:
    """Coerce ``node[key]`` to float.

    Exponent forms reach here already resolved as floats -- see
    :class:`_SpecLoader`, which fixes PyYAML's YAML 1.1 float resolver at the
    parse step rather than patching around it at every call site.
    """
    if key not in node or node[key] is None:
        return default
    raw = node[key]
    if isinstance(raw, bool):
        doc.fail(path, f"{key!r} must be a number, got a boolean")
    if isinstance(raw, (int, float)):
        return float(raw)
    doc.fail(path, f"{key!r} must be a number, got "
                   f"{_type_name(raw)} ({raw!r})")


def _freq_pair(doc: _Doc, node: Mapping[str, Any], key: str,
               path: str) -> tuple[float | None, float | None]:
    raw = node.get(key)
    if raw is None:
        return (None, None)
    seq = _sequence(doc, raw, f"{path}.{key}")
    if len(seq) != 2:
        doc.fail(f"{path}.{key}",
                 f"expected [low_hz, high_hz], got {len(seq)} element(s)")
    low = _float(doc, {"low_hz": seq[0]}, "low_hz", f"{path}.{key}")
    high = _float(doc, {"high_hz": seq[1]}, "high_hz", f"{path}.{key}")
    if low is not None and high is not None and high < low:
        doc.fail(f"{path}.{key}", f"high {high:g} Hz is below low {low:g} Hz")
    return (low, high)


_TERM_KEYS = ("ppm_reading", "ppm_range", "abs_floor", "pct_reading", "pct_range")


def _pct_to_ppm(pct: float) -> float:
    """Percent to ppm, without the binary-float dust.

    ``0.28 * 1e4`` is ``2800.0000000000005``.  Left alone that dust travels
    into every derived limit and makes two spellings of the same datasheet row
    compare unequal, so it is rounded off here -- well below any real
    specification's precision.
    """
    return round(pct * 1e4, 9)


def _parse_terms(doc: _Doc, node: Any, path: str,
                 extra_keys: Sequence[str] = ()) -> LimitTerms:
    """Parse a three-term specification line, normalising percent to ppm."""
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, tuple(_TERM_KEYS) + tuple(extra_keys))
    if not any(key in node for key in _TERM_KEYS):
        doc.fail(path, "empty specification: expected at least one of "
                       f"{', '.join(_TERM_KEYS)}")
    for ppm_key, pct_key in (("ppm_reading", "pct_reading"),
                             ("ppm_range", "pct_range")):
        if ppm_key in node and pct_key in node:
            doc.fail(path, f"{ppm_key!r} and {pct_key!r} both given; "
                           "use one spelling")

    ppm_reading = _float(doc, node, "ppm_reading", path, 0.0)
    ppm_range = _float(doc, node, "ppm_range", path, 0.0)
    if "pct_reading" in node:
        ppm_reading = _pct_to_ppm(_float(doc, node, "pct_reading", path, 0.0))
    if "pct_range" in node:
        ppm_range = _pct_to_ppm(_float(doc, node, "pct_range", path, 0.0))
    return LimitTerms(ppm_reading=ppm_reading, ppm_range=ppm_range,
                      abs_floor=_float(doc, node, "abs_floor", path, 0.0))


_TEMPCO_KEYS = ("ppm_reading_per_c", "ppm_range_per_c", "abs_floor_per_c",
                "pct_reading_per_c", "pct_range_per_c")


def _parse_tempco(doc: _Doc, node: Any, path: str) -> Tempco:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _TEMPCO_KEYS)
    for ppm_key, pct_key in (("ppm_reading_per_c", "pct_reading_per_c"),
                             ("ppm_range_per_c", "pct_range_per_c")):
        if ppm_key in node and pct_key in node:
            doc.fail(path, f"{ppm_key!r} and {pct_key!r} both given; "
                           "use one spelling")
    ppm_reading = _float(doc, node, "ppm_reading_per_c", path, 0.0)
    ppm_range = _float(doc, node, "ppm_range_per_c", path, 0.0)
    if "pct_reading_per_c" in node:
        ppm_reading = _pct_to_ppm(_float(doc, node, "pct_reading_per_c", path, 0.0))
    if "pct_range_per_c" in node:
        ppm_range = _pct_to_ppm(_float(doc, node, "pct_range_per_c", path, 0.0))
    return Tempco(ppm_reading_per_c=ppm_reading, ppm_range_per_c=ppm_range,
                  abs_floor_per_c=_float(doc, node, "abs_floor_per_c", path, 0.0))


def _parse_limits(doc: _Doc, node: Any, path: str,
                  intervals: Sequence[str],
                  quadratic: tuple[QuadraticTerm, ...] = ()
                  ) -> Mapping[str, LimitTerms]:
    """Parse a per-interval table of specification lines.

    *quadratic* is stated once on the range and attached to every interval's
    line here, because that is what the footnotes say -- "add 12 ppm X
    (Vin/1000)^2" qualifies the whole column, not one interval -- and because
    repeating it on four intervals is four chances to mistype it.
    """
    node = _mapping(doc, node, path)
    if not node:
        doc.fail(path, "no intervals given")
    out: dict[str, LimitTerms] = {}
    for interval, terms in node.items():
        if intervals and interval not in intervals:
            doc.fail(f"{path}.{interval}",
                     f"unknown interval {interval!r} "
                     f"(declared intervals: {', '.join(intervals)})")
        parsed = _parse_terms(doc, terms, f"{path}.{interval}")
        if quadratic:
            parsed = dataclasses.replace(parsed, quadratic=quadratic)
        out[interval] = parsed
    return MappingProxyType(out)


def _parse_apply(doc: _Doc, node: Any, path: str) -> Mapping[str, Any]:
    if node is None:
        return _EMPTY_MAP
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, tuple(APPLY_KNOBS))
    for key, value in node.items():
        types = APPLY_KNOBS[key]
        if bool not in types and isinstance(value, bool):
            doc.fail(path, f"{key!r} must not be a boolean")
        if not isinstance(value, types):
            wanted = "/".join(t.__name__ for t in types)
            doc.fail(path, f"{key!r} must be {wanted}, got {_type_name(value)}")
    return MappingProxyType(dict(node))


# --------------------------------------------------------------------------
# Node parsers
# --------------------------------------------------------------------------

_MODE_KEYS = ("id", "freq_hz", "apply", "note")


def _parse_mode(doc: _Doc, node: Any, path: str) -> Mode:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _MODE_KEYS, required=("id",))
    low, high = _freq_pair(doc, node, "freq_hz", path)
    return Mode(id=str(node["id"]), freq_low_hz=low, freq_high_hz=high,
                apply=_parse_apply(doc, node.get("apply"), f"{path}.apply"),
                note=node.get("note"))


_BAND_KEYS = ("freq_hz", "modes", "limits", "tempco", "max_out",
              "below_frac_adder", "note")

def _parse_below_frac_adder(doc: _Doc, node: Mapping[str, Any],
                            path: str) -> BelowFractionAdder | None:
    """Parse a ``below_frac_adder`` block, or ``None`` when there is none."""
    raw = node.get("below_frac_adder")
    if raw is None:
        return None
    sub_path = f"{path}.below_frac_adder"
    raw = _mapping(doc, raw, sub_path)
    if raw.get("below_frac") is None:
        doc.fail(sub_path, "missing required key 'below_frac' (the fraction of "
                           "full scale the adder applies below)")
    fraction = _float(doc, raw, "below_frac", sub_path)
    if not 0.0 < fraction <= 1.0:
        doc.fail(sub_path,
                 f"'below_frac' must be in (0, 1], got {fraction:g}")
    terms = _parse_terms(doc, raw, sub_path,
                         extra_keys=("below_frac", "note"))
    return BelowFractionAdder(below_frac=fraction, terms=terms,
                              note=raw.get("note"))


def _parse_regime_adder(doc: _Doc, node: Mapping[str, Any],
                        path: str) -> RegimeAdder | None:
    """Parse a ``regime_adder`` block, or ``None`` when there is none."""
    raw = node.get("regime_adder")
    if raw is None:
        return None
    sub_path = f"{path}.regime_adder"
    raw = _mapping(doc, raw, sub_path)
    # `_parse_terms` already rejects an empty block and both spellings of one
    # term; the note is required on top of that, because an unconditional
    # adder that cannot say why it is there is unauditable.
    terms = _parse_terms(doc, raw, sub_path, extra_keys=("note",))
    note = raw.get("note")
    if not note or not str(note).strip():
        doc.fail(sub_path, "'note' is required: an adder applied to every "
                           "evaluation of this range has to say which "
                           "operating regime it is for")
    return RegimeAdder(ppm_reading=terms.ppm_reading,
                       ppm_range=terms.ppm_range,
                       abs_floor=terms.abs_floor,
                       note=str(note).strip())


def _parse_band(doc: _Doc, node: Any, path: str, intervals: Sequence[str],
                mode_ids: Sequence[str],
                quadratic: tuple[QuadraticTerm, ...] = ()) -> Band:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _BAND_KEYS, required=("freq_hz", "limits"))
    low, high = _freq_pair(doc, node, "freq_hz", path)
    if low is None or high is None:
        doc.fail(f"{path}.freq_hz", "both band edges are required")

    modes: list[str] = []
    for index, mode_id in enumerate(_sequence(doc, node.get("modes", []),
                                              f"{path}.modes")):
        if mode_id not in mode_ids:
            doc.fail(f"{path}.modes[{index}]",
                     f"unknown mode {mode_id!r} "
                     f"(declared modes: {', '.join(mode_ids) or '(none)'})")
        modes.append(str(mode_id))

    tempco = None
    if node.get("tempco") is not None:
        tempco = _parse_tempco(doc, node["tempco"], f"{path}.tempco")
    return Band(freq_low_hz=low, freq_high_hz=high, modes=tuple(modes),
                limits=_parse_limits(doc, node["limits"], f"{path}.limits",
                                     intervals, quadratic),
                tempco=tempco,
                max_out=_float(doc, node, "max_out", path),
                below_frac_adder=_parse_below_frac_adder(doc, node, path),
                note=node.get("note"))


_RANGE_KEYS = ("fs", "range_arg", "limits", "bands", "tempco",
               "min_reading_frac", "min_reading_frac_by_mode", "resolution",
               "input_impedance_ohm",
               "settle_s", "range_id", "min_out", "source_impedance_ohm",
               "characterized", "connections", "connection_adders",
               "discrete", "overrange", "below_frac_adder", "load_current",
               "test_current_a", "typical", "regime_adder", "apply",
               "ppm_reading_hv", "hv_ref_v", "hv_above_v",
               "ppm_reading_isq", "isq_ref_a", "isq_above_a", "note")

_LOAD_CURRENT_KEYS = ("min_a", "max_a", "imax_a", "unit",
                      "k_under_4w", "k_under_4w_unit", "k_under_2w",
                      "over_unit", "k_over_4w")

#: The two quadratic-term spellings a range may carry, as
#: ``(coefficient key, reference key, threshold key)``.  One mechanism
#: (:class:`QuadraticTerm`), two names, because they are two different
#: manufacturer footnotes over two different quantities and a transcriber
#: should be able to grep for the one they are auditing.
_QUADRATIC_SPELLINGS = (
    ("ppm_reading_hv", "hv_ref_v", "hv_above_v"),
    ("ppm_reading_isq", "isq_ref_a", "isq_above_a"),
)


def _parse_quadratic(doc: _Doc, node: Mapping[str, Any],
                     path: str) -> tuple[QuadraticTerm, ...]:
    """Parse every quadratic term a range states, in declaration order.

    All three keys of a spelling are required together.  A coefficient with no
    threshold would silently apply everywhere and a threshold with no
    coefficient is a footnote someone started transcribing and did not finish;
    neither is a thing a spec file should be able to say by omission.
    """
    out: list[QuadraticTerm] = []
    for coeff_key, ref_key, above_key in _QUADRATIC_SPELLINGS:
        keys = (coeff_key, ref_key, above_key)
        present = [key for key in keys if node.get(key) is not None]
        if not present:
            continue
        missing = [key for key in keys if node.get(key) is None]
        if missing:
            doc.fail(path,
                     f"{', '.join(repr(k) for k in present)} given without "
                     f"{', '.join(repr(k) for k in missing)}; a quadratic "
                     "adder needs its coefficient, its reference and its "
                     "threshold")
        reference = _float(doc, node, ref_key, path)
        if reference <= 0.0:
            doc.fail(path, f"{ref_key!r} must be positive, got {reference:g}")
        threshold = _float(doc, node, above_key, path)
        if threshold < 0.0:
            doc.fail(path, f"{above_key!r} must not be negative")
        out.append(QuadraticTerm(ppm_at_ref=_float(doc, node, coeff_key, path),
                                 ref=reference, above=threshold))
    return tuple(out)


def _parse_load_current(doc: _Doc, node: Mapping[str, Any],
                        path: str) -> LoadCurrent | None:
    raw = node.get("load_current")
    if raw is None:
        return None
    sub_path = f"{path}.load_current"
    raw = _mapping(doc, raw, sub_path)
    _check_keys(doc, raw, sub_path, _LOAD_CURRENT_KEYS,
                required=("min_a", "max_a", "unit"))
    # `_check_keys` only asks whether the key is written.  `min_a:` with
    # nothing after it is written and is `None`, which would reach the
    # arithmetic below and raise a bare TypeError out of the loader instead of
    # a SpecError naming the file and the path.
    for key in ("min_a", "max_a", "unit"):
        if raw.get(key) is None:
            doc.fail(f"{sub_path}.{key}", "written with no value")

    min_a = _float(doc, raw, "min_a", sub_path)
    max_a = _float(doc, raw, "max_a", sub_path)
    if min_a <= 0.0:
        doc.fail(sub_path, f"'min_a' must be positive, got {min_a:g}")
    if max_a < min_a:
        doc.fail(sub_path, f"'max_a' ({max_a:g}) is below 'min_a' ({min_a:g}); "
                           "the full-spec load range runs IL to IU")
    imax_a = _float(doc, raw, "imax_a", sub_path)
    if imax_a is not None and imax_a < max_a:
        doc.fail(sub_path, f"'imax_a' ({imax_a:g}) is below 'max_a' "
                           f"({max_a:g}); the peak current cannot be under "
                           "the top of the full-spec range")

    def _current_unit(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        value = str(value)
        if value not in LOAD_CURRENT_UNITS:
            doc.fail(f"{sub_path}.{key}",
                     f"unknown current unit {value!r} "
                     f"(known: {', '.join(LOAD_CURRENT_UNITS)}); the derating "
                     "constants are quoted against the manual's own unit")
        return value

    unit = _current_unit("unit")
    over_unit = _current_unit("over_unit")

    k_under_4w = _float(doc, raw, "k_under_4w", sub_path)
    k_under_4w_unit = raw.get("k_under_4w_unit")
    if k_under_4w_unit is not None:
        k_under_4w_unit = str(k_under_4w_unit)
        if k_under_4w_unit not in LOAD_ERROR_UNITS:
            doc.fail(f"{sub_path}.k_under_4w_unit",
                     f"unknown error unit {k_under_4w_unit!r} "
                     f"(known: {', '.join(LOAD_ERROR_UNITS)})")
        if k_under_4w is None:
            doc.fail(sub_path, "'k_under_4w_unit' without 'k_under_4w'")
    elif k_under_4w is not None:
        # Defaulting to ppm would be silent and wrong on the one row where it
        # is not ppm -- the short, where a ppm of a 0 ohm output is
        # identically zero.  Both halves of the pair or neither, exactly as
        # the over-current pair below.
        doc.fail(sub_path, "'k_under_4w' without 'k_under_4w_unit'; the "
                           f"error unit is not always ppm (known: "
                           f"{', '.join(LOAD_ERROR_UNITS)})")
    k_under_2w = _float(doc, raw, "k_under_2w", sub_path)
    k_over_4w = _float(doc, raw, "k_over_4w", sub_path)
    for key, value in (("k_under_4w", k_under_4w), ("k_under_2w", k_under_2w),
                       ("k_over_4w", k_over_4w)):
        if value is not None and value < 0.0:
            doc.fail(f"{sub_path}.{key}", f"must not be negative, got {value:g}")
    # A dangling unit or a constant with no unit is a half-transcribed row,
    # and both halves are silent failures: the derating either vanishes or is
    # computed in the wrong unit, which is a factor of 1000 on this table.
    if k_over_4w is not None and over_unit is None:
        doc.fail(sub_path, "'k_over_4w' without 'over_unit'; the over-current "
                           "equation takes its own current unit, which is not "
                           "always the under-current one")
    if over_unit is not None and k_over_4w is None:
        doc.fail(sub_path, "'over_unit' without 'k_over_4w'")

    return LoadCurrent(min_a=min_a, max_a=max_a, imax_a=imax_a, unit=unit,
                       k_under_4w=k_under_4w,
                       k_under_4w_unit=k_under_4w_unit or "ppm",
                       k_under_2w=k_under_2w,
                       over_unit=over_unit, k_over_4w=k_over_4w)


def _parse_range_connection_adders(doc: _Doc, node: Mapping[str, Any],
                                   path: str) -> Mapping[Connection, LimitTerms]:
    raw = node.get("connection_adders")
    if raw is None:
        return _EMPTY_MAP
    raw = _mapping(doc, raw, f"{path}.connection_adders")
    if not raw:
        doc.fail(f"{path}.connection_adders",
                 "empty mapping; omit the key when the range adds nothing")
    out: dict[Connection, LimitTerms] = {}
    for name, body in raw.items():
        sub_path = f"{path}.connection_adders.{name}"
        try:
            connection = Connection(name)
        except ValueError:
            doc.fail(sub_path, f"unknown connection {name!r} "
                               f"(known: {', '.join(c.value for c in Connection)})")
        if connection in out:
            doc.fail(sub_path, f"duplicate connection {name!r}")
        out[connection] = _parse_terms(doc, body, sub_path)
    return MappingProxyType(out)


def _parse_min_reading_frac_by_mode(doc: _Doc, node: Mapping[str, Any],
                                    path: str,
                                    mode_ids: Sequence[str]) -> Mapping[str, float]:
    raw = node.get("min_reading_frac_by_mode")
    if raw is None:
        return _EMPTY_MAP
    sub_path = f"{path}.min_reading_frac_by_mode"
    raw = _mapping(doc, raw, sub_path)
    if not raw:
        doc.fail(sub_path, "empty mapping; omit the key when every mode shares "
                           "the range's floor")
    out: dict[str, float] = {}
    for mode_id in raw:
        if mode_id not in mode_ids:
            doc.fail(f"{sub_path}.{mode_id}",
                     f"unknown mode {mode_id!r} "
                     f"(declared modes: {', '.join(mode_ids) or '(none)'})")
        fraction = _float(doc, raw, mode_id, sub_path)
        if not 0.0 <= fraction <= 1.0:
            doc.fail(f"{sub_path}.{mode_id}",
                     f"must be a fraction of full scale in [0, 1], "
                     f"got {fraction:g}")
        out[str(mode_id)] = fraction
    return MappingProxyType(out)


def _parse_range_connections(doc: _Doc, node: Mapping[str, Any],
                             path: str) -> tuple[Connection, ...]:
    raw = node.get("connections")
    if raw is None:
        return ()
    out: list[Connection] = []
    for index, name in enumerate(_sequence(doc, raw, f"{path}.connections")):
        try:
            connection = Connection(name)
        except ValueError:
            doc.fail(f"{path}.connections[{index}]",
                     f"unknown connection {name!r} "
                     f"(known: {', '.join(c.value for c in Connection)})")
        if connection in out:
            doc.fail(f"{path}.connections[{index}]",
                     f"duplicate connection {name!r}")
        out.append(connection)
    if not out:
        doc.fail(f"{path}.connections",
                 "empty list; omit the key to mean 'every connection the "
                 "function declares'")
    return tuple(out)


def _parse_range(doc: _Doc, node: Any, path: str, intervals: Sequence[str],
                 mode_ids: Sequence[str]) -> RangeSpec:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _RANGE_KEYS, required=("fs",))
    has_limits = node.get("limits") is not None
    has_bands = node.get("bands") is not None
    if has_limits and has_bands:
        doc.fail(path, "'limits' and 'bands' are mutually exclusive; a banded "
                       "range carries its limits inside its bands")
    if not has_limits and not has_bands:
        doc.fail(path, "must define either 'limits' or 'bands'")

    fs = _float(doc, node, "fs", path)
    # ``fs == 0`` is legal for exactly one shape of range: a source's short.
    # The 5700A's ``OHM0`` is a 0 ohm output whose whole specification is a
    # 50 uohm floor, and forcing a fake positive full scale on it would put an
    # invented number into the range term.  Negative full scale is still a typo.
    if fs is None or fs < 0:
        doc.fail(path, "'fs' must not be negative")

    quadratic = _parse_quadratic(doc, node, path)

    bands: list[Band] = []
    if has_bands:
        for index, band in enumerate(_sequence(doc, node["bands"], f"{path}.bands")):
            bands.append(_parse_band(doc, band, f"{path}.bands[{index}]",
                                     intervals, mode_ids, quadratic))

    limits = _EMPTY_MAP
    if has_limits:
        limits = _parse_limits(doc, node["limits"], f"{path}.limits", intervals,
                               quadratic)

    tempco = None
    if node.get("tempco") is not None:
        tempco = _parse_tempco(doc, node["tempco"], f"{path}.tempco")

    flags = {}
    for key in ("characterized", "discrete"):
        value = node.get(key, False)
        if not isinstance(value, bool):
            doc.fail(path, f"{key!r} must be a boolean, got {_type_name(value)}")
        flags[key] = value

    overrange = _float(doc, node, "overrange", path)
    if overrange is not None and overrange < 1.0:
        doc.fail(path, f"'overrange' is a multiple of full scale and must be "
                       f"at least 1.0 (1.2 = 20 %), got {overrange:g}")

    test_current_a = _float(doc, node, "test_current_a", path)
    if test_current_a is not None and test_current_a <= 0.0:
        doc.fail(path, f"'test_current_a' is a sourced current in amps and "
                       f"must be positive, got {test_current_a:g}")

    return RangeSpec(
        fs=fs,
        range_arg=_float(doc, node, "range_arg", path, fs),
        limits=limits,
        bands=tuple(bands),
        tempco=tempco,
        min_reading_frac=_float(doc, node, "min_reading_frac", path),
        resolution=_float(doc, node, "resolution", path),
        input_impedance_ohm=_float(doc, node, "input_impedance_ohm", path),
        settle_s=_float(doc, node, "settle_s", path),
        range_id=(str(node["range_id"]) if node.get("range_id") else None),
        min_out=_float(doc, node, "min_out", path),
        source_impedance_ohm=_float(doc, node, "source_impedance_ohm", path),
        characterized=flags["characterized"],
        discrete=flags["discrete"],
        overrange=overrange,
        test_current_a=test_current_a,
        typical=bool(node.get("typical", False)),
        regime_adder=_parse_regime_adder(doc, node, path),
        connections=_parse_range_connections(doc, node, path),
        connection_adders=_parse_range_connection_adders(doc, node, path),
        below_frac_adder=_parse_below_frac_adder(doc, node, path),
        min_reading_frac_by_mode=_parse_min_reading_frac_by_mode(
            doc, node, path, mode_ids),
        load_current=_parse_load_current(doc, node, path),
        apply=_parse_apply(doc, node.get("apply"), f"{path}.apply"),
        note=node.get("note"),
    )


_CONNECTION_KEYS = ("func", "adder", "adder_per_interval", "note")
_FUNCTION_KEYS = ("quantity", "func", "connections", "modes", "ranges",
                  "apply", "settle_s", "volt_hertz_max", "traceability", "note")
_TRACEABILITY_KEYS = ("ppm_reading", "abs_floor", "confidence", "k", "note")


def _parse_traceability(doc: _Doc, node: Any, path: str) -> Traceability:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _TRACEABILITY_KEYS)
    if not any(node.get(key) is not None
               for key in ("ppm_reading", "abs_floor")):
        doc.fail(path, "empty traceability block: expected at least one of "
                       "ppm_reading, abs_floor")
    if node.get("confidence") is not None and node.get("k") is not None:
        doc.fail(path, "'confidence' and 'k' both given; use one spelling")
    confidence = _float(doc, node, "confidence", path)
    if confidence is not None and not 0.0 < confidence < 1.0:
        doc.fail(path, f"'confidence' is a fraction strictly between 0 and 1, "
                       f"got {confidence:g}")
    coverage = _float(doc, node, "k", path)
    if coverage is not None and coverage <= 0.0:
        doc.fail(path, f"'k' must be positive, got {coverage:g}")
    return Traceability(
        ppm_reading=_float(doc, node, "ppm_reading", path, 0.0),
        abs_floor=_float(doc, node, "abs_floor", path, 0.0),
        confidence=confidence, k=coverage, note=node.get("note"))


def _parse_connection(doc: _Doc, connection: Connection, node: Any, path: str,
                      default_func: str | None,
                      intervals: Sequence[str]) -> ConnectionSpec:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _CONNECTION_KEYS)
    if node.get("adder") is not None and node.get("adder_per_interval") is not None:
        doc.fail(path, "'adder' and 'adder_per_interval' are mutually "
                       "exclusive; a per-interval adder carries every interval "
                       "it applies to")
    adder = None
    note = node.get("note")
    if node.get("adder") is not None:
        adder_node = _mapping(doc, node["adder"], f"{path}.adder")
        adder = _parse_terms(doc, adder_node, f"{path}.adder",
                             extra_keys=("note",))
        note = adder_node.get("note", note)
    per_interval: Mapping[str, LimitTerms] = _EMPTY_MAP
    if node.get("adder_per_interval") is not None:
        per_interval = _parse_limits(doc, node["adder_per_interval"],
                                     f"{path}.adder_per_interval", intervals)
        # A partial table is the dangerous half of this feature: an interval
        # the file forgets falls back to `adder`, which is None here, so the
        # wiring penalty silently vanishes for that interval and the limit
        # comes out too tight -- or, with a flat adder present, too loose.
        # Neither is a thing a spec file should be able to say by omission.
        missing = [i for i in intervals if i not in per_interval]
        if missing:
            doc.fail(f"{path}.adder_per_interval",
                     "must name every declared interval; missing "
                     f"{', '.join(missing)}")
    func = node.get("func", default_func)
    if not func:
        doc.fail(path, "no 'func' here and none at the function level")
    return ConnectionSpec(connection=connection, func=str(func), adder=adder,
                          adder_per_interval=per_interval, note=note)


def _parse_function(doc: _Doc, node: Any, path: str,
                    intervals: Sequence[str]) -> FunctionSpec:
    node = _mapping(doc, node, path)
    _check_keys(doc, node, path, _FUNCTION_KEYS,
                required=("quantity", "ranges"))
    try:
        quantity = Quantity(node["quantity"])
    except ValueError:
        doc.fail(f"{path}.quantity",
                 f"unknown quantity {node['quantity']!r} "
                 f"(known: {', '.join(q.name for q in Quantity)})")

    default_func = node.get("func")
    if default_func is not None:
        default_func = str(default_func)

    modes: list[Mode] = []
    seen_modes: set[str] = set()
    for index, mode in enumerate(_sequence(doc, node.get("modes", []),
                                           f"{path}.modes")):
        parsed = _parse_mode(doc, mode, f"{path}.modes[{index}]")
        if parsed.id in seen_modes:
            doc.fail(f"{path}.modes[{index}]", f"duplicate mode id {parsed.id!r}")
        seen_modes.add(parsed.id)
        modes.append(parsed)
    mode_ids = tuple(m.id for m in modes)

    connections: dict[Connection, ConnectionSpec] = {}
    raw_connections = node.get("connections")
    if raw_connections is None:
        raw_connections = [Connection.TWO_WIRE.value]
    if isinstance(raw_connections, Mapping):
        items = list(raw_connections.items())
    else:
        items = [(name, {}) for name in
                 _sequence(doc, raw_connections, f"{path}.connections")]
    if not items:
        doc.fail(f"{path}.connections", "at least one connection is required")
    for name, body in items:
        sub_path = f"{path}.connections.{name}"
        try:
            connection = Connection(name)
        except ValueError:
            doc.fail(sub_path, f"unknown connection {name!r} "
                               f"(known: {', '.join(c.value for c in Connection)})")
        if connection in connections:
            doc.fail(sub_path, f"duplicate connection {name!r}")
        connections[connection] = _parse_connection(
            doc, connection, body or {}, sub_path, default_func, intervals)

    ranges: list[RangeSpec] = []
    for index, range_node in enumerate(_sequence(doc, node["ranges"],
                                                 f"{path}.ranges")):
        ranges.append(_parse_range(doc, range_node, f"{path}.ranges[{index}]",
                                   intervals, mode_ids))
    if not ranges:
        doc.fail(f"{path}.ranges", "at least one range is required")
    # Before the sort, so the path in any message is the one in the document.
    for index, range_spec in enumerate(ranges):
        for connection in range_spec.connection_adders:
            sub_path = (f"{path}.ranges[{index}].connection_adders"
                        f".{connection.value}")
            if connection not in connections:
                doc.fail(sub_path,
                         f"connection {connection.value!r} is not declared by "
                         f"this function (declared: "
                         f"{', '.join(c.value for c in connections)})")
            if not range_spec.allows(connection):
                doc.fail(sub_path,
                         f"connection {connection.value!r} is not legal on "
                         f"this range (it allows: "
                         f"{', '.join(c.value for c in range_spec.connections)})")
    ranges.sort(key=lambda r: r.fs)

    traceability = None
    if node.get("traceability") is not None:
        traceability = _parse_traceability(doc, node["traceability"],
                                           f"{path}.traceability")

    return FunctionSpec(
        quantity=quantity,
        func=default_func,
        connections=MappingProxyType(connections),
        modes=tuple(modes),
        ranges=tuple(ranges),
        apply=_parse_apply(doc, node.get("apply"), f"{path}.apply"),
        settle_s=_float(doc, node, "settle_s", path),
        volt_hertz_max=_float(doc, node, "volt_hertz_max", path),
        traceability=traceability,
        note=node.get("note"),
    )


_MODEL_KEYS = ("schema_version", "model_id", "display_name", "role", "driver",
               "adapter", "overload_sentinel", "interval_basis",
               "limits_authority", "intervals", "spec_confidence",
               "default_interval", "temperature", "functions", "notes",
               "options")
_DRIVER_KEYS = ("module", "cls", "func_enum")
_TEMPERATURE_KEYS = ("spec_window_c", "tempco_ref_c", "per_interval",
                     "relative_to", "half_width_c", "half_width_per_interval")

#: Keys a model file must state outright.  ``overload_sentinel`` and
#: ``interval_basis`` are here rather than defaulted because both defaults were
#: 34401A-shaped lies: a missing sentinel silently disables the +/-9.9e37 filter
#: this project's CLAUDE.md warns about, and a missing basis reports a 3458A's
#: ACAL-relative figures as "since calibration".  ``overload_sentinel: null`` is
#: allowed -- an explicit "this model has none" is the point, a silent one is not.
_REQUIRED_MODEL_KEYS = ("schema_version", "model_id", "display_name", "role",
                        "overload_sentinel", "interval_basis", "functions")


def _parse_window(doc: _Doc, node: Any, path: str) -> tuple[float, float]:
    seq = _sequence(doc, node, path)
    if len(seq) != 2:
        doc.fail(path, f"expected [low_c, high_c], got {len(seq)} element(s)")
    low = _float(doc, {"low_c": seq[0]}, "low_c", path)
    high = _float(doc, {"high_c": seq[1]}, "high_c", path)
    if high < low:
        doc.fail(path, f"high edge {high:g} C is below low edge {low:g} C")
    return (low, high)


def parse_model(data: Any, source: str = "<string>") -> ModelSpec:
    """Parse an already-loaded YAML document into a :class:`ModelSpec`."""
    doc = _Doc(source)
    root = _mapping(doc, data, "<root>")
    _check_keys(doc, root, "<root>", _MODEL_KEYS, required=_REQUIRED_MODEL_KEYS)

    version = _float(doc, root, "schema_version", "<root>")
    if version != SCHEMA_VERSION:
        doc.fail("<root>.schema_version",
                 f"unsupported schema_version {version:g}; "
                 f"this build understands {SCHEMA_VERSION}")

    try:
        role = Role(root["role"])
    except ValueError:
        doc.fail("<root>.role", f"unknown role {root['role']!r} "
                                f"(known: {', '.join(r.value for r in Role)})")

    driver = None
    if root.get("driver") is not None:
        node = _mapping(doc, root["driver"], "<root>.driver")
        _check_keys(doc, node, "<root>.driver", _DRIVER_KEYS,
                    required=("module", "cls"))
        driver = DriverRef(module=str(node["module"]), cls=str(node["cls"]),
                           func_enum=(str(node["func_enum"])
                                      if node.get("func_enum") else None))

    adapter = root.get("adapter")
    if adapter is not None:
        adapter = str(adapter)
        if not _ADAPTER_RE.match(adapter):
            doc.fail("<root>.adapter",
                     f"expected 'package.module:ClassName', got {adapter!r}")

    try:
        interval_basis = IntervalBasis(root["interval_basis"])
    except ValueError:
        doc.fail("<root>.interval_basis",
                 f"unknown interval_basis {root['interval_basis']!r} "
                 f"(known: {', '.join(b.value for b in IntervalBasis)})")

    limits_authority = LimitsAuthority.FILE
    if root.get("limits_authority") is not None:
        try:
            limits_authority = LimitsAuthority(root["limits_authority"])
        except ValueError:
            doc.fail("<root>.limits_authority",
                     f"unknown limits_authority {root['limits_authority']!r} "
                     f"(known: {', '.join(a.value for a in LimitsAuthority)})")

    intervals = tuple(str(i) for i in
                      _sequence(doc, root.get("intervals", []), "<root>.intervals"))
    if len(set(intervals)) != len(intervals):
        doc.fail("<root>.intervals", "duplicate interval name")

    spec_confidence = _float(doc, root, "spec_confidence", "<root>")
    if spec_confidence is not None and not 0.0 < spec_confidence < 1.0:
        doc.fail("<root>.spec_confidence",
                 f"a confidence is a fraction strictly between 0 and 1 "
                 f"(0.99, not 99), got {spec_confidence:g}")

    default_interval = root.get("default_interval")
    if default_interval is not None:
        default_interval = str(default_interval)
        if intervals and default_interval not in intervals:
            doc.fail("<root>.default_interval",
                     f"{default_interval!r} is not in intervals "
                     f"({', '.join(intervals)})")
    elif len(intervals) == 1:
        default_interval = intervals[0]

    temperature = TemperatureSpec()
    if root.get("temperature") is not None:
        node = _mapping(doc, root["temperature"], "<root>.temperature")
        _check_keys(doc, node, "<root>.temperature", _TEMPERATURE_KEYS)
        window = None
        if node.get("spec_window_c") is not None:
            window = _parse_window(doc, node["spec_window_c"],
                                   "<root>.temperature.spec_window_c")
        per_interval: dict[str, tuple[float, float]] = {}
        if node.get("per_interval") is not None:
            sub = _mapping(doc, node["per_interval"],
                           "<root>.temperature.per_interval")
            for interval, raw in sub.items():
                sub_path = f"<root>.temperature.per_interval.{interval}"
                if intervals and interval not in intervals:
                    doc.fail(sub_path,
                             f"unknown interval {interval!r} "
                             f"(declared intervals: {', '.join(intervals)})")
                per_interval[interval] = _parse_window(doc, raw, sub_path)

        relative_to = node.get("relative_to")
        half_width = _float(doc, node, "half_width_c", "<root>.temperature")
        if relative_to is not None:
            relative_to = str(relative_to)
            if relative_to not in TEMPERATURE_REFERENCES:
                doc.fail("<root>.temperature.relative_to",
                         f"unknown reference {relative_to!r} "
                         f"(known: {', '.join(TEMPERATURE_REFERENCES)})")
            if half_width is None:
                doc.fail("<root>.temperature",
                         f"'relative_to: {relative_to}' without 'half_width_c'; "
                         "a window centred on a run-time temperature still "
                         "needs its width")
        elif half_width is not None or node.get("half_width_per_interval"):
            doc.fail("<root>.temperature",
                     "'half_width_c' without 'relative_to'; a relative window "
                     "has to say what it is relative to "
                     f"({', '.join(TEMPERATURE_REFERENCES)})")
        if half_width is not None and half_width <= 0.0:
            doc.fail("<root>.temperature.half_width_c",
                     f"must be positive, got {half_width:g}")

        half_width_per_interval: dict[str, float] = {}
        if node.get("half_width_per_interval") is not None:
            sub = _mapping(doc, node["half_width_per_interval"],
                           "<root>.temperature.half_width_per_interval")
            for interval, raw in sub.items():
                sub_path = ("<root>.temperature.half_width_per_interval."
                            f"{interval}")
                if intervals and interval not in intervals:
                    doc.fail(sub_path,
                             f"unknown interval {interval!r} "
                             f"(declared intervals: {', '.join(intervals)})")
                value = _float(doc, sub, interval, sub_path)
                if value <= 0.0:
                    doc.fail(sub_path, f"must be positive, got {value:g}")
                half_width_per_interval[str(interval)] = value

        temperature = TemperatureSpec(
            spec_window_c=window,
            tempco_ref_c=_float(doc, node, "tempco_ref_c", "<root>.temperature"),
            per_interval=MappingProxyType(per_interval),
            relative_to=relative_to,
            half_width_c=half_width,
            half_width_per_interval=MappingProxyType(half_width_per_interval))

    functions: dict[Quantity, FunctionSpec] = {}
    for index, node in enumerate(_sequence(doc, root["functions"],
                                           "<root>.functions")):
        parsed = _parse_function(doc, node, f"functions[{index}]", intervals)
        if parsed.quantity in functions:
            doc.fail(f"functions[{index}]",
                     f"duplicate function for quantity {parsed.quantity.name}; "
                     "wiring variants belong in 'connections', not a second entry")
        functions[parsed.quantity] = parsed
    if not functions:
        doc.fail("<root>.functions", "at least one function is required")

    return ModelSpec(
        model_id=str(root["model_id"]),
        display_name=str(root["display_name"]),
        role=role,
        functions=MappingProxyType(functions),
        driver=driver,
        adapter=adapter,
        overload_sentinel=_float(doc, root, "overload_sentinel", "<root>"),
        interval_basis=interval_basis,
        limits_authority=limits_authority,
        intervals=intervals,
        default_interval=default_interval,
        temperature=temperature,
        spec_confidence=spec_confidence,
        notes=root.get("notes"),
        source=source,
        options=MappingProxyType({str(k): v for k, v
                                  in dict(root.get("options") or {}).items()}),
    )


def parse_model_text(text: str, source: str = "<string>") -> ModelSpec:
    """Parse YAML *text* into a :class:`ModelSpec`."""
    try:
        data = yaml.load(text, Loader=_SpecLoader)
    except yaml.YAMLError as exc:
        raise SpecError(f"{source}: not valid YAML: {exc}") from exc
    return parse_model(data, source)


#: File-name suffixes in the data package that are *not* model specifications.
#: ``fluke5700a_faults.yaml`` is a flat fault-code lookup that lives beside the
#: models because ``cal`` may not import a driver to get it.  It must not be
#: advertised by :func:`available_models`, because the obvious loop --
#: ``for m in available_models(): load_model(m)`` -- is exactly what a plan
#: generator, an analyzer and a ``--list-models`` flag all write, and it would
#: crash on a file that was never a model.
_NON_MODEL_SUFFIXES = ("_faults.yaml",)


def _is_model_file(name: str) -> bool:
    return (name.endswith(".yaml")
            and not any(name.endswith(s) for s in _NON_MODEL_SUFFIXES))


def available_models() -> list[str]:
    """Model ids of the specification files shipped with the package.

    Every id returned here loads: ``load_model`` on any of them succeeds.
    """
    try:
        root = resources.files(DATA_PACKAGE)
        return sorted(entry.name[:-5] for entry in root.iterdir()
                      if _is_model_file(entry.name))
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        return []


def load_model(model_id: str) -> ModelSpec:
    """Load a shipped specification by model id.  Cached; results are frozen.

    The id is normalised before the cache is consulted, so ``"HP34401A"`` and
    ``"hp34401a"`` are one entry and not two copies of the same file.
    """
    return _load_model_cached(str(model_id).strip().lower())


@lru_cache(maxsize=None)
def _load_model_cached(key: str) -> ModelSpec:
    filename = f"{key}.yaml"
    resource = resources.files(DATA_PACKAGE) / filename
    if not (key and _is_model_file(filename) and resource.is_file()):
        known = ", ".join(available_models()) or "(none installed)"
        raise SpecError(f"unknown model id {key!r}; "
                        f"available models: {known}")
    spec = parse_model_text(resource.read_text(encoding="utf-8"), filename)
    if spec.model_id != key:
        raise SpecError(f"{filename}: <root>.model_id: {spec.model_id!r} does "
                        f"not match the file name (expected {key!r})")
    return spec


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

def select_range(function: FunctionSpec, value: float,
                 range_fs: float | None = None, *,
                 for_meter_config: bool = False) -> RangeSpec | None:
    """The range to use for *value*, or ``None`` when nothing holds it.

    With no *range_fs* this picks the smallest range that can hold ``|value|``,
    which is what an operator would do by hand.

    ``for_meter_config=True`` asks a different question: which range should a
    *meter* be set to.  Meters specify an overrange -- "20% overrange on all
    ranges, except 1000 Vdc, 3 A range" (``34401a-user.txt:9110``, PDF p.216)
    -- so an 11 V point belongs on the 10 V range, with its finer resolution
    and its higher input impedance, rather than on the 100 V one.  Only ranges
    that declare :attr:`RangeSpec.overrange` stretch.

    The default stays strict, because the other caller is limit evaluation,
    and a range's specification is written for its nominal full scale.
    """
    if range_fs is not None:
        for candidate in function.ranges:
            if math.isclose(candidate.fs, float(range_fs), rel_tol=1e-9,
                            abs_tol=0.0):
                return candidate
        return None
    best: RangeSpec | None = None
    for candidate in function.ranges:
        if (candidate.holds(value, allow_overrange=for_meter_config)
                and (best is None or candidate.fs < best.fs)):
            best = candidate
    return best


def _band_limit(band: Band, value: float, fs: float,
                interval: str | None) -> float:
    """The band's limit at *value*, for comparing two bands at a shared edge.

    With no interval named -- or one this band does not carry -- the widest
    interval it does carry answers, so the comparison is defined for every
    band and does not depend on the caller having an interval in hand.
    """
    terms = band.limits.get(interval) if interval is not None else None
    if terms is not None:
        return terms.limit(value, fs)
    if not band.limits:
        return 0.0
    return max(t.limit(value, fs) for t in band.limits.values())


def select_band(range_spec: RangeSpec, freq_hz: float | None,
                mode_id: str | None = None, *,
                role: PairRole | str | None = None,
                value: float | None = None,
                interval: str | None = None) -> Band | None:
    """The band covering *freq_hz*, restricted to *mode_id* when given.

    Band endpoints are inclusive, so two adjacent rows can both cover an edge
    tone, and the datasheets print the edge in both without a tie-break.  Which
    row is the safe one depends on *role*:

    * :attr:`PairRole.DUT` -- the tighter row, which is what the
      manufacturers' verification tables use (see :class:`Band`).
    * :attr:`PairRole.REFERENCE` -- the looser row, because the figure is then
      an uncertainty rather than a tolerance.
    * ``None`` -- the historical rule: the higher-frequency band, ties broken
      by declaration order.  Callers that do not know which side of the
      comparison they are on keep the numbers they always had.

    *value* and *interval* say where to compare the two rows; they default to
    full scale and to the widest interval each row carries.

    A role picks a **row, never a mode**.  Bands sharing one span are a mode
    partition of a single datasheet row, so declaration order -- the author's
    deliberate best-first ordering -- decides between them whatever the role;
    and when no mode is pinned, the mode partition is settled by that same
    role-free rule *before* the role chooses among the spans that mode offers.
    Otherwise the two filters of a 34401A land on opposite sides of a shared
    edge and :func:`ambiguous_mode_siblings` reports a contest that the bands
    themselves do not have.
    """
    role = PairRole(role) if role is not None else None
    candidates = []
    for index, band in enumerate(range_spec.bands):
        if not band.covers(freq_hz):
            continue
        if mode_id is not None and band.modes and mode_id not in band.modes:
            continue
        candidates.append((index, band))
    if not candidates:
        return None

    def declaration_key(pair):
        return (-pair[1].freq_low_hz, pair[0])

    default = min(candidates, key=declaration_key)[1]
    if role is None or len(candidates) == 1:
        return default

    def usable(band: Band) -> bool:
        """True when this point can actually be evaluated on *band*.

        A row that cannot make this output (the 5700A's 1100 V ac range stops
        at 250 V below 50 Hz) or that carries no line for this interval is not
        a candidate for the tie-break: choosing it would turn a specified point
        into ``NO_SPEC`` for one side of the comparison only.
        """
        if (value is not None and band.max_out is not None
                and abs(value) > band.max_out):
            return False
        if interval is not None and band.limits and interval not in band.limits:
            return False
        return True

    if not usable(default):
        # With no role this point is unspecified here, and a role must not
        # rescue it any more than it may break it.
        return default
    candidates = [pair for pair in candidates if usable(pair[1])]

    # The role decides which *row* applies, never which *mode*.  The mode
    # partition is settled first, exactly as it is with no role at all, and the
    # role then chooses only among the spans that same mode offers.  Letting it
    # choose across modes sent the 34401A's medium filter to the row below the
    # slow filter's at 40 Hz and 100 Hz -- the two spans then differed, and
    # ambiguous_mode_siblings reported a contest that does not exist, turning
    # ordinary operator tones into a hard error on the reference side.
    if default.modes:
        shared = set(default.modes)
        candidates = [(index, band) for index, band in candidates
                      if not band.modes or shared & set(band.modes)]

    # One representative per distinct span: within a span the mode partition
    # rule applies, and only across spans is there an edge to resolve.
    spans: dict[tuple[float, float], tuple[int, Band]] = {}
    for index, band in candidates:
        span = (band.freq_low_hz, band.freq_high_hz)
        if span not in spans or index < spans[span][0]:
            spans[span] = (index, band)
    if len(spans) == 1:
        return default

    probe = range_spec.fs if value is None else abs(value)
    tighter = 1.0 if role is PairRole.DUT else -1.0
    return min(spans.values(),
               key=lambda pair: (tighter * _band_limit(pair[1], probe,
                                                       range_spec.fs, interval),
                                 *declaration_key(pair)))[1]


def ambiguous_mode_siblings(range_spec: RangeSpec, band: Band,
                            freq_hz: float | None) -> tuple[Band, ...]:
    """Bands that contest *band* at *freq_hz* and that no mode choice settles.

    :func:`select_band` breaks ties by highest ``freq_low_hz``, then by
    declaration order.  That is the right rule when a range's bands are a mode
    *partition* of one frequency span -- the 34401A writes ``[20, 40]`` twice,
    once for the slow filter and once for the medium one, best first, and
    declaration order is then the author's deliberate default.

    It is the wrong rule when the modes have genuinely different band
    geometry.  On the 3458A, SYNC's first ac band runs ``[1, 40]`` and ANA's
    runs ``[20, 40]``; at 30 Hz the highest-low tie-break hands the point to
    ANA, whose specification is roughly fifteen times looser, purely because
    of where the band edges fall.  A meter actually running SYNC would then be
    judged against ANA's limit and pass when it should fail.

    The contest is therefore decided **per mode, not per band**: for every mode
    the selected band does not name, ask which band *that* mode would itself
    land on here, and count it only when that band's span differs from the
    selected one's.

    Deciding it per sibling band instead over-fires on inclusive band edges.
    :meth:`Band.covers` includes both endpoints, so at exactly 40 Hz on the
    34401A the selected band is ``[40, 100] filt_slow`` while ``[20, 40]
    filt_medium`` is still covering -- disjoint modes, different span -- and a
    per-band test calls that a contest.  It is not one: ``filt_medium`` has its
    own ``[40, 100]`` band and would never be judged on ``[20, 40]`` here.
    Asking the mode where *it* would go answers the question that matters.

    There is deliberately no ``role`` here.  Whether a point is mode-ambiguous
    is a question about band *geometry*, and geometry does not depend on which
    side of a comparison the model is on: a role that could create or remove a
    contest would make the same point evaluate for the DUT and raise for the
    reference.  Callers pass the role-free selection.
    """
    if not band.modes:
        return ()
    selected = set(band.modes)
    span = (band.freq_low_hz, band.freq_high_hz)
    foreign = {m for other in range_spec.bands for m in other.modes} - selected
    contested: list[Band] = []
    for mode_id in sorted(foreign):
        peer = select_band(range_spec, freq_hz, mode_id)
        if peer is None or peer is band:
            continue
        if (peer.freq_low_hz, peer.freq_high_hz) == span:
            # A true mode partition of one span: declaration order is the
            # author's deliberate best-first ordering, so it decides.
            continue
        if not any(peer is already for already in contested):
            contested.append(peer)
    return tuple(contested)


def select_mode(function: FunctionSpec, band: Band | None,
                freq_hz: float | None) -> Mode | None:
    """The mode to use, preferring the band's own declaration order.

    Band ``modes:`` are written best-specification-first, so "the first mode
    that is valid at this frequency" is also "the best mode for this point".
    """
    if not function.modes:
        return None
    ids = band.modes if (band is not None and band.modes) else function.mode_ids
    for mode_id in ids:
        mode = function.mode(mode_id)
        if mode is not None and mode.covers(freq_hz):
            return mode
    return None


def evaluate_limit(spec: ModelSpec, quantity, value: float, *,
                   range_fs: float | None = None,
                   freq_hz: float | None = None,
                   connection: Connection | str | None = None,
                   interval: str | None = None,
                   mode: str | None = None,
                   role: PairRole | str | None = None) -> LimitResult | None:
    """Absolute tolerance at one test point, or ``None`` when unspecified.

    ``None`` means "this model has no specification row for this point" -- the
    caller renders that as ``NO_SPEC``.  It is never zero and never an
    exception, because a missing spec is a normal outcome (a 34401A has no
    100 kHz spec on its 750 V ac range) and must not be confused with a
    tolerance of nothing.

    Genuine caller mistakes -- an interval or mode the model does not declare,
    an ac point with no frequency -- do raise :class:`SpecError`, because those
    are typos, and a typo silently rendering as ``NO_SPEC`` is how a failing
    meter passes.

    *role* says which side of the comparison this model is on.  It changes
    exactly one thing: which row applies at a frequency two bands share
    (see :func:`select_band`).  Omitting it keeps the historical convention.
    The range is always chosen strictly -- an overrange is what a meter may be
    *configured* on, never what widens its tolerance.
    """
    quantity = Quantity(quantity)
    role = PairRole(role) if role is not None else None
    function = spec.function(quantity)
    if function is None:
        return None

    interval = interval or spec.default_interval
    if interval is None:
        raise SpecError(f"{spec.model_id}: no interval given and the model "
                        "declares no default_interval")
    if spec.intervals and interval not in spec.intervals:
        raise SpecError(f"{spec.model_id}: unknown interval {interval!r} "
                        f"(declared: {', '.join(spec.intervals)})")

    if connection is None:
        connection_spec = next(iter(function.connections.values()), None)
    else:
        connection = Connection(connection)
        connection_spec = function.connections.get(connection)
    if connection_spec is None:
        return None

    if mode is not None and function.mode(mode) is None:
        raise SpecError(
            f"{spec.model_id}: {quantity.name} has no mode {mode!r} "
            f"(declared: {', '.join(function.mode_ids) or '(none)'})")

    range_spec = select_range(function, value, range_fs)
    if range_spec is None:
        return None
    if not range_spec.allows(connection_spec.connection):
        # The function offers this wiring but this range does not -- the
        # 5700A has no 4-wire sense on 100 Mohm.
        return None
    min_reading_frac = range_spec.min_reading_frac_for(mode)
    if (min_reading_frac is not None
            and abs(value) < min_reading_frac * range_spec.fs):
        # Below the fraction of range the specification is written for: the
        # datasheet says nothing here, so neither do we.  A pinned mode gets
        # its own floor, because one per-range floor has to be the most
        # restrictive mode's and would otherwise delete points that the mode
        # actually running is specified for.
        return None
    if range_spec.min_out is not None and abs(value) < range_spec.min_out:
        # Below the smallest output the range can produce.
        return None
    if (function.volt_hertz_max is not None and freq_hz is not None
            and abs(value) * freq_hz > function.volt_hertz_max):
        return None

    band = None
    if range_spec.bands:
        if freq_hz is None:
            raise SpecError(
                f"{spec.model_id}: {quantity.name} on the {range_spec.fs:g} "
                "range is specified per frequency band; freq_hz is required")
        band = select_band(range_spec, freq_hz, mode, role=role, value=value,
                           interval=interval)
        if band is None:
            return None
        if band.max_out is not None and abs(value) > band.max_out:
            return None
        if mode is None:
            # Judged on the role-free selection, so that a role can move the
            # limit but never turn a specified point into an error.
            partition = (band if role is None
                         else select_band(range_spec, freq_hz))
            contested = ambiguous_mode_siblings(range_spec, partition, freq_hz)
            if contested:
                offered = sorted({m for b in (partition,) + contested
                                  for m in b.modes})
                raise SpecError(
                    f"{spec.model_id}: {quantity.name} at {freq_hz:g} Hz on the "
                    f"{range_spec.fs:g} range is specified separately for "
                    f"{', '.join(offered)}, and those bands do not line up, so "
                    "there is no defensible default; pass mode=")

    selected_mode = (function.mode(mode) if mode is not None
                     else select_mode(function, band, freq_hz))
    if band is not None and band.modes and selected_mode is None:
        return None

    limits = band.limits if band is not None else range_spec.limits
    base_terms = limits.get(interval)
    if base_terms is None:
        return None

    terms = base_terms
    # A range that names this connection replaces the function-level figure:
    # the 5700A's two-wire compensation adder is per range, and stacking it on
    # top of a per-connection worst case would count it twice.
    adder = range_spec.adder_for(connection_spec.connection)
    if adder is None:
        adder = connection_spec.adder_for(interval)
    if adder is not None:
        terms = terms + adder

    # A band that states its own below-fraction adder overrides the range's,
    # so a range can carry the general footnote and a band say "not here".
    below = range_spec.below_frac_adder
    if band is not None and band.below_frac_adder is not None:
        below = band.below_frac_adder
    if below is not None and below.applies(value, range_spec.fs):
        terms = terms + below.terms

    # The operating-regime adder is unconditional -- it is the penalty for the
    # conditions this code base actually measures in -- so it stacks on top of
    # everything above, in both roles, and rides in `terms` while `base_terms`
    # stays the data sheet's printed row.
    if range_spec.regime_adder is not None:
        terms = terms + range_spec.regime_adder.as_terms()

    # Most specific last: a mode is nested inside a band inside a range, so
    # it overrides the range, which overrides the function.  Same order as
    # ``adapters._merged_knobs``, which is what actually gets sent -- the two
    # disagreeing would make this record describe a configuration nobody used.
    applied = dict(function.apply)
    applied.update(range_spec.apply)
    if selected_mode is not None:
        applied.update(selected_mode.apply)

    return LimitResult(
        quantity=quantity,
        value=float(value),
        limit=terms.limit(value, range_spec.fs),
        terms=terms,
        interval=interval,
        connection=connection_spec.connection,
        range_spec=range_spec,
        func=connection_spec.func,
        band=band,
        mode=selected_mode,
        base_terms=base_terms,
        adder=adder,
        apply=MappingProxyType(applied),
        temperature_window_c=spec.temperature.window_for(interval),
    )

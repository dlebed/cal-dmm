"""The role seam: what the calibration engine is allowed to know about hardware.

Everything above this module talks to a **source** and a **meter**, never to a
driver.  A source produces a quantity and reports what it actually produced; a
meter is configured for a point and returns samples.  Swap the 5700A for a
Datron 4808 or the 3458A for a 1281 and only :mod:`caldmm.cal.adapters`
changes.

Why ``apply()`` returns a state object
--------------------------------------

:meth:`SourceRole.apply` returns a :class:`SourceState` carrying the value the
source *actually* sourced and **the source's own uncertainty for it**.  That is
what makes verification bidirectional without a second pair of interfaces: in
both directions the source is applied and the meter is read, and the only thing
that changes is *which side's specification supplies the tolerance limit*.

* Verifying a meter: the limit comes from the meter's ``ModelSpec``, and the
  source's uncertainty is a component of the budget.
* Verifying a calibrator against a golden DMM: the limit comes from the
  *source's* ``ModelSpec``, and the meter's specification becomes the reference
  component of the budget.

In resistance the reference is the *characterized* readback, never the nominal
that was requested — which is precisely why the state object carries
``actual_value`` rather than the engine assuming the request took effect.

Only the source has a lifecycle
-------------------------------

Standby/operate, an autonomous trip back to standby, a fault queue, warm-up and
"dc zeros are due" are all source-side.  There is no meter equivalent, so
:class:`MeterRole` has no ``health()``.  Inventing the symmetry would mean
inventing data.

Ambient temperature
-------------------

:func:`caldmm.cal.verdict.evaluate` **blocks a PASS** when a model declares a
temperature window and no ambient was recorded (``AMBIENT_UNKNOWN``), so
without an ambient reading every such point is ``INDETERMINATE`` by design.  The reading therefore has to
reach the verdict from somewhere, and that somewhere is an
:class:`EnvironmentProbe` — a one-method seam the runner satisfies with an
operator-entered number, a logged room sensor of your own, or the meter's own
internal sensor.  It is deliberately *not* solved by
loosening the temperature policy.

Import hygiene
--------------

This module imports :mod:`caldmm.cal.quantities` and the stdlib.  Nothing
else — no ``specs``, no ``yaml``, and above all no driver.  Only
:mod:`adapters` and :mod:`dmm_procedures` may touch :mod:`caldmm.drivers` at
all, and both do so lazily, inside a function — see :mod:`caldmm.cal` for why
the second one is sanctioned.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .quantities import Connection, Quantity

__all__ = [
    "Uncertainty",
    "SourceState",
    "SourceHealth",
    "Reading",
    "MeterConfig",
    "Environment",
    "UNKNOWN_ENVIRONMENT",
    "Point",
    "PointLike",
    "SourceRole",
    "MeterRole",
    "EnvironmentProbe",
]

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


def _not_finite(value: float) -> bool:
    """Default overload predicate: anything that is not a finite number."""
    try:
        return not math.isfinite(value)
    except TypeError:
        return True


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Uncertainty:
    """An instrument's own statement of its uncertainty for one output.

    The ``unit`` is carried because a calibrator does not have to answer in the
    quantity's unit: the 5700A's ``UNCERT?`` reports ``PPM``, ``PCT``, ``V``,
    ``A``, ``OHM`` "etc." and the list is explicitly open-ended.  Assuming
    absolute would be wrong by six orders of magnitude on a ppm answer, so the
    conversion is done here, once, by :meth:`absolute`.

    ``confidence`` is a fraction (``0.95``/``0.99``), not a percentage, and it
    is the level *the instrument* chose: on the 5700A ``CAL_CONF`` selects it
    and the same output legitimately reports two different numbers.  It feeds
    :func:`caldmm.cal.uncertainty.from_calibrator_uncert` directly, so a
    wrong or assumed level silently mis-scales the whole budget.
    """

    value: float
    unit: str = ""
    confidence: float | None = None
    interval_days: int | None = None

    @property
    def is_relative(self) -> bool:
        """True when :attr:`value` is a fraction of the reading, not absolute."""
        return self.unit.strip().upper() in ("PPM", "PCT", "%")

    def absolute(self, reading: float) -> float:
        """This uncertainty as an absolute quantity, for an output of *reading*.

        A relative figure is taken against ``|reading|``; an absolute one is
        returned unchanged.
        """
        unit = self.unit.strip().upper()
        if unit == "PPM":
            return abs(self.value) * abs(float(reading)) * 1e-6
        if unit in ("PCT", "%"):
            return abs(self.value) * abs(float(reading)) * 1e-2
        return abs(self.value)


@dataclass(frozen=True, slots=True)
class SourceState:
    """What a source is actually doing after :meth:`SourceRole.apply`.

    ``actual_value`` is the **readback**, not the request.  For the 5700A's
    resistance function the two differ by up to 500 ppm by design — the
    calibrator sources a characterized value near the nominal — and using the
    request as the reference folds the calibrator's ohms characterization into
    the UUT's error.

    ``settled`` and ``operate`` are read back from the instrument rather than
    inferred from "the command did not raise": ``OPER`` is *silently* inhibited
    above 22 V while a fault is queued, and several conditions trip the output
    back to standby on their own.  A meter reading 0 V then looks like a 100 %
    error rather than like an output that never came on.
    """

    actual_value: float
    unit: str
    frequency_hz: float = 0.0
    range_id: str | None = None
    uncertainty: Uncertainty | None = None
    settled: bool = False
    operate: bool = False
    #: Instrument status snapshot — the decoded ISR for the 5700A, or whatever
    #: the equivalent is elsewhere.  Free-form on purpose: it is provenance for
    #: the report, never something the engine branches on.
    raw: Mapping[str, Any] = field(default_factory=_empty_map)

    @property
    def expanded_uncertainty(self) -> float | None:
        """The source's own uncertainty, absolute, at its stated confidence."""
        if self.uncertainty is None:
            return None
        return self.uncertainty.absolute(self.actual_value)


@dataclass(frozen=True, slots=True)
class SourceHealth:
    """Whether a source is fit to be the reference right now.

    Every field is source-side lifecycle, and every one of them can invalidate
    a run that would otherwise look perfect:

    ``tripped``          the output dropped to standby by itself;
    ``faults``           the queue, ``(code, text)`` — non-empty silently
                         inhibits ``OPER`` above 22 V;
    ``warm_minutes``     minutes since power-up; the specs want twice the
                         off-time, to a 30-minute maximum;
    ``days_since_zero``  the 30-day dc-zeros gate;
    ``days_since_cal``   which interval column the figures may be taken from.
    """

    operate: bool = False
    tripped: bool = False
    faults: tuple[tuple[int, str], ...] = ()
    warm_minutes: int | None = None
    days_since_cal: int | None = None
    days_since_zero: int | None = None
    #: ``(code, substate)`` as reported, for the report.  Branch on the code
    #: only; the substate strings are not enumerated by any manual.
    state: tuple[int, str] | None = None
    raw: Mapping[str, Any] = field(default_factory=_empty_map)


@dataclass(frozen=True, slots=True)
class Reading:
    """The meter's answer at one point: the surviving samples and their stats.

    ``samples`` holds only the samples that count.  An invalid-reading sentinel
    (``9.9e37`` on HP/Agilent, ``1e35`` on the Wavetek 1281) is **excluded and
    counted** in ``overloads``, never averaged: one such sample moves a 10 V
    mean by 1e36 and takes the plot's autoscaling with it.  ``discarded``
    counts samples dropped for any other reason — typically the first reading
    or two after a range change.

    ``mean`` and ``stdev`` are ``nan`` when there is nothing to compute them
    from (``n < 1`` and ``n < 2`` respectively).  Zero would be a number the
    arithmetic downstream would happily consume.

    ``resolution`` is one least-significant digit at the range that was
    actually configured, so that
    :func:`caldmm.cal.uncertainty.resolution_component` can build the
    quantisation term without the engine having to look the range up again.
    """

    samples: tuple[float, ...]
    mean: float
    stdev: float
    n: int
    discarded: int = 0
    overloads: int = 0
    resolution: float | None = None
    unit: str = ""

    @classmethod
    def from_samples(cls, samples: Sequence[float], *,
                     overload=None,
                     discarded: int = 0,
                     resolution: float | None = None,
                     unit: str = "") -> "Reading":
        """Build a reading, splitting *samples* on the *overload* predicate.

        *overload* is a callable taking a value and returning True when it is
        an invalid reading — pass the model's own
        :meth:`caldmm.cal.specs.ModelSpec.is_overload`, because the sentinel
        differs per model and a value that is an overload on a 1281 is a
        perfectly ordinary (if enormous) number on a 3458A.
        """
        predicate = overload if overload is not None else _not_finite
        good: list[float] = []
        overloads = 0
        for value in samples:
            number = float(value)
            if predicate(number):
                overloads += 1
            else:
                good.append(number)
        n = len(good)
        mean = statistics.fmean(good) if n else math.nan
        stdev = statistics.stdev(good) if n > 1 else math.nan
        return cls(samples=tuple(good), mean=mean, stdev=stdev, n=n,
                   discarded=discarded, overloads=overloads,
                   resolution=resolution, unit=unit)

    @property
    def stdev_mean(self) -> float:
        """Standard deviation *of the mean*, ``s/sqrt(n)`` (GUM 4.2.3)."""
        if self.n < 2:
            return math.nan
        return self.stdev / math.sqrt(self.n)


@dataclass(frozen=True, slots=True)
class MeterConfig:
    """What :meth:`MeterRole.configure` actually applied.

    Recorded rather than assumed: the applied configuration is part of the
    result row, and "the plan asked for NPLC 100" is not evidence that the
    meter is integrating for 100 cycles.

    ``settle_s`` is what the *meter* needs after a step.  The engine waits the
    maximum of this and the source's own settling — the two are independent
    and the larger governs.

    ``range_fs`` is the full scale of the range the adapter **applied**, and
    ``applied["autorange"]`` is ``False`` when that range is fixed.  The engine
    compares ``range_fs`` with the instrument's own ``range_readback()`` before
    energizing anything above the hazard thresholds: what the adapter believes
    it sent and what the instrument is on are two different facts.
    """

    func: str | None = None          #: the driver's own function spelling
    range_arg: float | None = None   #: what the range knob was given; None = auto
    range_fs: float | None = None    #: full scale of the selected range
    nplc: float | None = None
    mode: str | None = None          #: spec mode id (ac technique or filter)
    ac_filter: str | None = None
    resolution: float | None = None  #: one LSD at this range
    settle_s: float = 0.0
    #: Every knob that was applied, by the closed ``APPLY_KNOBS`` vocabulary,
    #: plus what the adapter had to decide for itself and a reader would
    #: otherwise have to infer: ``autorange`` (always ``False`` -- these
    #: adapters never leave a meter ranging itself) and ``range`` beside it,
    #: and per meter ``resolution_pct`` / ``resolution_digits`` (3458A ac),
    #: ``lfilter`` / ``sync_source`` (its SYNC ac path), ``math_null``,
    #: ``range_held_s``, ``trigger_delay`` and ``ocomp_note``.  Free-form on
    #: purpose: it is provenance for the report, never something the engine
    #: branches on.
    applied: Mapping[str, Any] = field(default_factory=_empty_map)


@dataclass(frozen=True, slots=True)
class Environment:
    """Ambient conditions at a point, from wherever they were obtained.

    ``ambient_c`` is ``None`` when nobody recorded one.  That is a *distinct*
    state from "recorded and out of window" and the engine must keep it
    distinct: :func:`caldmm.cal.verdict.classify_temperature` turns the first
    into ``AMBIENT_UNKNOWN`` (which blocks a PASS) and the second into
    ``OUT_OF_WINDOW``.  A ``nan`` — the usual shape of a failed sensor read —
    is treated as unknown by :attr:`ambient_known`.
    """

    ambient_c: float | None = None
    humidity_pct: float | None = None
    timestamp: datetime | None = None
    #: Where it came from, e.g. ``"operator"``, ``"room-sensor"``, ``"3458a TEMP?"``.
    source: str = ""

    @property
    def ambient_known(self) -> bool:
        return self.ambient_c is not None and math.isfinite(self.ambient_c)

    @property
    def ambient_for_verdict(self) -> float | None:
        """The value to hand :func:`caldmm.cal.verdict.evaluate`."""
        return self.ambient_c if self.ambient_known else None


#: The honest default: nobody has said what the temperature is.
UNKNOWN_ENVIRONMENT = Environment()


@dataclass(frozen=True, slots=True)
class Point:
    """One test point, in the shared vocabulary.

    A concrete :class:`PointLike`.  :mod:`caldmm.cal.plan` may grow a richer
    point of its own; anything that carries these attributes works with these
    adapters, which is the whole reason the roles take a structural type.

    ``value`` is the **nominal** — what the plan asked for.  What was actually
    sourced comes back on :class:`SourceState`, and in resistance the two are
    not the same number.
    """

    quantity: Quantity
    value: float
    frequency_hz: float = 0.0
    connection: Connection = Connection.TWO_WIRE
    #: Source-side range identifier (the 5700A's §10 names) when one is pinned.
    range_id: str | None = None
    #: Force a meter range; ``None`` lets the model's spec pick one.
    range_arg: float | None = None
    #: Spec ``mode`` id — the ac technique or filter the limits assume.
    mode: str | None = None
    #: Which specification column this point is judged against.
    interval: str | None = None
    #: Overrides for the model's ``apply:`` knobs, same closed vocabulary.
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    #: Override the settling time; ``None`` takes it from the spec.
    settle_s: float | None = None
    label: str = ""

    @property
    def unit(self) -> str:
        """Canonical unit of the quantity — ``V``, ``A`` or ``OHM``."""
        return self.quantity.unit


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------

@runtime_checkable
class PointLike(Protocol):
    """The minimum a test point must carry for an adapter to act on it.

    Only the four attributes every adapter dereferences unconditionally.
    Everything else a point may have (``range_id``, ``mode``, ``apply``,
    ``interval``, ``settle_s``) is read with a default, so a plan-level point
    is free to omit it.
    """

    quantity: Quantity
    value: float
    frequency_hz: float
    connection: Connection


@runtime_checkable
class SourceRole(Protocol):
    """A calibrator, reference divider or anything else that produces a value.

    **Optional capabilities, deliberately not protocol members** (for the same
    reason as the meter's ``internal_temperature`` -- see :class:`MeterRole`):

    ``cal_temperature() -> float | None``
        The ambient the source's stored calibration constants assume (the
        5700A's ``CAL_TEMP?``; it has no live internal sensor), ``None`` where
        the instrument records none.  The engine reads it once per run with a
        guarded ``getattr``: the ambient-minus-``CAL_TEMP`` delta is what
        decides whether the source is inside the ±5 °C window every one of its
        specifications is written for, and the runner records it in the header.

    ``zero_output() -> None``
        Program the quantity's **zero** without energizing anything -- the
        instrument's own "0 V ENTER", which the 5700A's manual pairs with
        standby before any connection is touched (OM 1996 lines 4141-4146 and
        4311-4313).  ``standby()`` alone leaves the last setpoint selected, up
        to 1100 V, and one ``OPR`` press re-applies it.  The engine calls this
        immediately before ``standby()`` at every group boundary and on every
        exit path; a source that does not offer it is de-energized with
        ``standby()`` alone, and a call that raises is reported to the
        boundary callback rather than being allowed to stop a run.
    """

    model_id: str

    def validate(self, point: PointLike) -> list[str]:
        """Every reason this point cannot be sourced.  **Offline** — no I/O.

        Empty means "inside the envelope".  All violations are returned, not
        just the first, so a whole plan can be checked before a single cable is
        connected.
        """

    def apply(self, point: PointLike) -> SourceState:
        """Set, energize, settle and read back.  Returns what is really there."""

    def standby(self) -> None:
        """De-energize.  Safe to call redundantly and from a ``finally``."""

    def health(self) -> SourceHealth:
        """Warm-up, fault queue, trip state and calibration ages."""


@runtime_checkable
class MeterRole(Protocol):
    """A DMM being verified, or serving as the reference.

    **Optional capabilities, deliberately not protocol members.**  Each is read
    with a guarded ``getattr`` and each has a defined answer when it is absent,
    so a meter that offers none of them still runs.  They stay out of the
    protocol because a required member would make every meter without the
    hardware implement a stub, and ``isinstance`` checks would then certify a
    capability the instrument does not have.

    ``internal_temperature() -> float | None``
        The meter's own internal sensor (the 3458A's ``TEMP?``), ``None`` where
        the hardware has none.  The engine records it per point; it is
        provenance, never something the engine branches on.

    ``supports_manual_range: bool``
        Whether :meth:`configure` can put this meter on a **fixed** range with
        autorange off.  All four instruments this project drives can, so the
        engine's default when the attribute is absent is ``True``.  ``False``
        selects the fallback the manual-ranging rule defines: a
        10 %-of-target pre-step before any output above 22 V, which keeps the
        autoranger's step to one decade.

    ``range_readback() -> float | None``
        The full scale of the range the **instrument** reports it is actually
        on (3458A ``RANGE?``, SCPI ``...:RANGe?``), queried rather than
        remembered.  The engine compares it with
        :attr:`MeterConfig.range_fs` before energizing any point above 22 V or
        220 mA and aborts on a mismatch.  ``None`` -- and an absent method --
        mean "this instrument cannot say", which is *not* "the range is
        wrong": the check is skipped, not failed.
    """

    model_id: str

    def supports(self, quantity: Quantity, connection: Connection) -> bool:
        """True when this meter can measure *quantity* wired that way.

        Honesty here is load-bearing: a driver whose ``set_func`` ignores its
        argument (the 1281 is DCV-only) will silently measure dc volts when
        asked for ohms, and the plan generator would emit points that cannot
        run and results that look like a catastrophic failure.
        """

    def configure(self, point: PointLike) -> MeterConfig:
        """Apply function, range and the model's knobs for this point."""

    def read(self, n: int) -> Reading:
        """Take *n* samples; sentinels are excluded and counted, not averaged."""


@runtime_checkable
class EnvironmentProbe(Protocol):
    """Where the ambient temperature comes from.

    One method, because there is one question.  The runner picks the
    implementation — an operator-entered number, a logged room sensor, or a
    meter's internal sensor — and the engine passes
    :attr:`Environment.ambient_for_verdict` into
    :func:`caldmm.cal.verdict.evaluate`.

    .. note::
       ``read`` is also a :class:`MeterRole` member, so
       ``isinstance(meter, EnvironmentProbe)`` is ``True`` for every meter.
       Structural typing cannot tell two one-method interfaces apart by name
       alone.  Pass the probe explicitly; never *dispatch* on protocol
       membership.
    """

    def read(self) -> Environment:
        """Current conditions; ``ambient_c`` is ``None`` when unknown."""

"""The test plan: the ordered list of points a verification run executes.

A plan is written by hand or **generated**, and generation is an *intersection*:
the DUT's own :class:`~caldmm.cal.specs.ModelSpec` says which functions and
ranges exist -- that is the whole of "feature and range discovery", there is no
second capability object -- and the other instrument then says, point by point,
whether it can realise them.  What survives is the plan; what does not survives
in :attr:`TestPlan.skipped`, with reasons.

Why ``skipped`` is a first-class output
---------------------------------------

Quietly producing a shorter plan than the operator asked for is the failure mode
this module is written against.  An operator who asks for 1100 V and gets a plan
without it does not find out at the desk; they find out when the report is short,
three hours later, with the instrument warm.  Every dropped candidate therefore
carries the reason it was dropped, the list survives the YAML round trip, and a
report can print it.

``dut_role`` is a property of the plan, never of a point
--------------------------------------------------------

When the meter is under test its ranges are the axis, and the points are pinned
to them with ``range_arg``.  When the calibrator is under test the *calibrator's*
cardinal ranges are the axis -- because that is where ``UNCERT?`` and
``CAL_SHIFT?`` are anchored -- and the points carry ``range_id``.  A plan mixing
the two would be asking two different questions in one run, so the field lives on
the plan and every point inherits it.

A pinned range is a promise the run has to keep
-----------------------------------------------

``range_id`` on a source-DUT point is not a label: the calibrator autoranges to
the *lowest* range that holds the value ([OM 1996] p.4-24), so every 10 %-of-
range point and every zero above the bottom range is physically on the range
below unless something locks the range.  Fluke's own verification says so --
"Use Range Lock to obtain 1V on 11V and 10V on 22V range" ([SM 2007] Table 3-19
note 3) -- and :func:`needs_range_lock` / :func:`range_lock_anchor` are what the
adapter drives ``RANGELCK`` from.  A point whose range cannot be selected at all
is **not generated**: see :func:`range_lock_anchor`.

Which band judges a shared edge
-------------------------------

Band endpoints are inclusive, so a tone that lands exactly on one is covered by
two rows.  Every limit this module evaluates therefore names a
:class:`~caldmm.cal.specs.PairRole`: the **DUT** is judged on the tighter row
(it has to meet both, and that is Fluke's own stated practice for border
specifications), the **reference** contributes the looser one.  Generated centre
tones never land on an edge; an operator's ``frequencies=`` list and the
``"manufacturer"`` preset routinely do.

Resistance is a catalogue, not a ladder
---------------------------------------

The 5700A sources 17 resistances plus a short and faults on anything else, which
is why those ranges are ``discrete: true`` in the specification file.  Resistance
points are therefore generated **only** at catalogue values, in both directions,
and they carry ``range_id`` so that downstream resolves them by identifier.
Resolving by value is actively dangerous: the calibrator sources a
*characterized* value up to 500 ppm off nominal, and a 19 kohm readback of
19003.8 ohm falls through a value-ordered ladder into the 100 kohm entry, whose
specification is looser -- i.e. it passes a calibrator that should fail.

Import hygiene
--------------

``specs``, ``quantities``, ``roles``, the stdlib and ``yaml``.  Never
:mod:`caldmm.cal.adapters`, and no driver at module scope: the driver envelope
is imported lazily inside :func:`driver_envelope` so that a plan can be written,
generated, validated and saved on a laptop with no linux-gpib.
"""

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

import yaml

from .quantities import Connection, Quantity, Terminals
from .roles import Point
from .specs import (
    APPLY_KNOBS,
    FunctionSpec,
    LimitsAuthority,
    ModelSpec,
    PairRole,
    RangeSpec,
    Role,
    SpecError,
    evaluate_limit,
    load_model,
    select_band,
    select_mode,
    select_range,
)

__all__ = [
    "SCHEMA_VERSION",
    "PlanError",
    "DUT_ROLES",
    "TEMPERATURE_POLICIES",
    "DEFAULT_PERCENTAGES",
    "DEFAULT_SAMPLES",
    "DEFAULT_DISCARDS",
    "DEFAULT_CONFIDENCE",
    "DEFAULT_GUARD_FACTOR",
    "DEFAULT_TUR_FLOOR",
    "DEFAULT_REFERENCE_FREQUENCY_HZ",
    "FREQUENCY_PRESETS",
    "VERIFICATION_INTERVALS",
    "DIVIDER_VERIFIED_RANGES",
    "PREFERRED_FREQUENCIES",
    "POINT_SETS",
    "ManufacturerTest",
    "MANUFACTURER_TESTS",
    "manufacturer_tests",
    "manufacturer_test_for",
    "QUANTITY_ORDER",
    "CONNECTION_ORDER",
    "PlanPoint",
    "SkippedPoint",
    "PointGroup",
    "TestPlan",
    "break_point_frequencies",
    "driver_envelope",
    "needs_range_lock",
    "range_lock_anchor",
    "representative_frequencies",
    "generate_plan",
    "validate_plan",
    "point_problems",
    "plan_to_dict",
    "plan_from_dict",
    "load_plan",
    "save_plan",
]

SCHEMA_VERSION = 1


class PlanError(ValueError):
    """Any problem building, loading or validating a test plan."""


#: Which instrument is under test.  A plan-level choice; see the module
#: docstring.
DUT_ROLES = ("source", "meter")

#: Mirrors :data:`caldmm.cal.verdict.TEMPERATURE_POLICIES`.  Repeated rather
#: than imported because ``verdict`` pulls in ``uncertainty`` and therefore
#: SciPy, and a plan must be writable, loadable and printable without it.  A
#: test asserts the two stay equal.
TEMPERATURE_POLICIES = ("gate", "flag", "strict")

#: Percentages of each range's full scale, the classic 10/50/90/100 ladder.
DEFAULT_PERCENTAGES = (10.0, 50.0, 90.0, 100.0)

DEFAULT_SAMPLES = 10
DEFAULT_DISCARDS = 1

#: Mirrors the three ``verdict`` defaults, for the same reason as
#: :data:`TEMPERATURE_POLICIES`.
DEFAULT_CONFIDENCE = 0.95
DEFAULT_GUARD_FACTOR = 1.0
DEFAULT_TUR_FLOOR = 4.0

#: The tone an ac *amplitude* sweep is run at, when the range has a band that
#: covers it.  1 kHz is the conventional mid-band reference point.
DEFAULT_REFERENCE_FREQUENCY_HZ = 1000.0

#: Presets ``frequencies=`` accepts in place of a list of tones.
#:
#: ``"manufacturer"`` is the **specification break points** -- every band edge
#: the model file declares for the range -- because that is where the
#: manufacturers put their own verification points: the 3458A Cal Manual tests
#: the 10 V range at 20 Hz, 1 kHz, 20 kHz, 100 kHz and 1 MHz (Table 4-3,
#: ``3458a-cal-manual.txt`` line 1936), the 34401A Service Guide at 10 Hz and
#: 50 kHz (``34401a-service.txt`` lines 2905-2945), and Fluke's own 5700A test
#: record at 20 Hz, 40 Hz, 1 kHz, 20 kHz, 50 kHz, 100 kHz, 300 kHz, 500 kHz
#: and 1 MHz (SM 2007 Table 3-24).  It is **opt-in**: a band edge is where the
#: roll-off the specification exists to bound actually bites, and the
#: centre-tone ladder is what a routine verification runs.
FREQUENCY_PRESETS = ("manufacturer",)

#: The specification column a manufacturer's own verification sheet is
#: computed from, where it differs from the model file's ``default_interval``.
#:
#: Empty for the two shipped models: both are verified against their
#: ``default_interval``.  A model whose manufacturer sheet uses another column
#: (e.g. an "enhanced" or "24h" specification) belongs here, keyed by model id.
VERIFICATION_INTERVALS: Mapping[str, str] = MappingProxyType({})

#: Ranges the manufacturer verifies **through a divider** rather than directly
#: from a calibrator, as ``(model_id, quantity) -> full scales``.
#:
#: Such ranges are sourced from a calibrator only at a spec ratio far below 1
#: -- bench time that can only return INDETERMINATE -- so they are skipped by
#: default, with that reason, and ``include_divider_ranges=True`` plans them
#: anyway.  Empty for the two shipped models.
DIVIDER_VERIFIED_RANGES: Mapping[tuple[str, str], tuple[float, ...]] = \
    MappingProxyType({})

#: Mantissas a generated frequency may take, in order of how "round" they are.
#: The rank breaks ties between two candidates equally close to a band's
#: logarithmic centre, so that 5 kHz wins over 4 kHz rather than the choice
#: falling out of floating-point noise.
_FREQUENCY_MANTISSAS = (1.0, 2.0, 5.0, 3.0, 1.5, 7.0, 4.0)

#: How close, in decades, a candidate has to be to the best one to still be
#: considered for the roundness tie-break.
_FREQUENCY_TIE_DECADES = 0.03


def _build_frequency_ladder() -> tuple[tuple[float, ...], Mapping[float, int]]:
    values: dict[float, int] = {}
    for decade in range(0, 8):                       # 1 Hz .. 10 MHz
        for rank, mantissa in enumerate(_FREQUENCY_MANTISSAS):
            frequency = float(f"{mantissa * 10 ** decade:.12g}")
            if frequency > 1.0e7:
                continue
            values.setdefault(frequency, rank)
    return tuple(sorted(values)), MappingProxyType(values)


#: The ladder a generated frequency is chosen from, 1 Hz to 10 MHz.
PREFERRED_FREQUENCIES, _FREQUENCY_RANK = _build_frequency_ladder()

#: Order the functions of a run are executed in, chosen so that the operator
#: re-cables **once**.  One 4-wire cable set (calibrator OUTPUT and SENSE to the
#: meter's HI/LO and ohms-sense inputs, plus guard) carries dc volts, ac volts
#: and both resistance wirings, so those run back to back; then the HI lead
#: moves to the meter's current input for the two current functions -- the
#: only manual step of a full 3458A verification.  Wideband, on its own Type-N
#: output, goes last.  Within a quantity, :data:`CONNECTION_ORDER` decides.
#: See :class:`~caldmm.cal.quantities.Terminals` and :meth:`TestPlan.groups`.
QUANTITY_ORDER = (Quantity.DCV, Quantity.ACV, Quantity.RES,
                  Quantity.DCI, Quantity.ACI, Quantity.WBV)

#: Wiring order within a quantity, and the preference when the caller does not
#: name one: four-wire is the metrologically correct resistance connection, so
#: it is preferred where both instruments offer it.
CONNECTION_ORDER = (Connection.FOUR_WIRE, Connection.TWO_WIRE,
                    Connection.TWO_WIRE_RCOMP)

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


#: What :func:`generate_plan` builds, for a ``point_set=`` argument.
#:
#: ``"full"`` (the default) is the generated ladder -- every range of every
#: function the DUT and the reference share, at the 10/50/90/100 % points, both
#: polarities, one tone per band -- **plus** every row of the manufacturer's own
#: performance-verification tables (:data:`MANUFACTURER_TESTS`) and the
#: supplementary points that table needs to reach every range.  ``"ladder"`` is
#: the generated ladder alone; ``"manufacturer"`` is the manufacturer's rows
#: alone, without the supplements -- the short form of the verification.
POINT_SETS = ("full", "ladder", "manufacturer")


@dataclass(frozen=True, slots=True)
class ManufacturerTest:
    """One row of a manufacturer's performance-verification table.

    ``range_fs`` is the **meter's** range: these are tables for verifying a
    meter, written in the meter's terms.  ``card_limit`` is the 1-year limit
    printed on the manufacturer's test card for the standard instrument and
    ``card_limit_opt002`` the one for a high-stability unit, both as absolute
    values in the quantity's unit.  They are **reported beside** the verdict,
    never used as the tolerance: the card's "difference" is taken against a
    transfer standard in the manufacturer's own procedure, and its limits are
    not the data sheet's (see ``docs/3458a/cal-manual-verification.md``).

    ``supplementary`` marks a point that is not in the manufacturer's tables
    but is needed for every range of the instrument to be exercised, so a
    report never presents it as a manufacturer test.
    """

    test_id: str
    quantity: Quantity
    value: float
    range_fs: float
    frequency_hz: float = 0.0
    connection: Connection = Connection.TWO_WIRE
    mode: str | None = None
    apply: Mapping[str, Any] = field(default_factory=_empty_map)
    card_limit: float | None = None
    card_limit_opt002: float | None = None
    supplementary: bool = False
    note: str = ""

    @property
    def tag(self) -> str:
        """The label prefix a plan point carries, e.g. ``[CM 4-2 #3]``."""
        return f"[{self.test_id}]"


def _cm(table: str, number: str, quantity: Quantity, value: float,
        range_fs: float, card: float | None, card_002: float | None = None, *,
        frequency_hz: float = 0.0, connection: Connection = Connection.TWO_WIRE,
        mode: str | None = None, apply: Mapping[str, Any] | None = None,
        note: str = "") -> ManufacturerTest:
    return ManufacturerTest(
        test_id=f"CM {table} #{number}", quantity=quantity, value=value,
        range_fs=range_fs, frequency_hz=frequency_hz, connection=connection,
        mode=mode, apply=MappingProxyType(dict(apply or {})),
        card_limit=card, card_limit_opt002=(card if card_002 is None
                                            else card_002),
        note=note)


#: The 3458A's ACV verification configuration, from the Cal Manual's
#: "Configure the DMM under test" list (p.65-66): SETACV ANA, ACBAND 10,2E6.
#: NPLC 20 is not on that list; it is what keeps the data sheet's p.29
#: low-frequency adder at zero with ACBAND low at 10 Hz ("NPLC > 10").
_CM_ACV_APPLY = {"ac_band": (10.0, 2.0e6), "nplc": 20}

_V, _I, _R = Quantity.DCV, Quantity.DCI, Quantity.RES
_A = Quantity.ACV
_4W, _2W = Connection.FOUR_WIRE, Connection.TWO_WIRE

#: Keysight 3458A Calibration Manual (03458-90017, edition 11, 2023), Chapter
#: 4 "Performance Verification Tests", Tables 4-1 to 4-6, with the 1-year
#: limits of the PERFORMANCE TEST CARD at the end of that chapter.  The
#: frequency counter tests (1 Hz and 10 MHz from a synthesizer) are not here:
#: the 5700A is not a frequency standard (+/-0.01 % against a 500 ppm and a
#: 100 ppm limit) and cannot make 1 Hz or 10 MHz.
#:
#: Where the manual differs from what this toolkit does it says so in the
#: point's note: the offsets are taken against the calibrator's own 0 V / 0 A
#: / 0 ohm output rather than a copper short or an open circuit at the meter's
#: terminals, and only the **front** terminals are tested -- the rear terminal
#: offsets need the leads moved and the front-panel switch thrown.
_HP3458A_TESTS: tuple[ManufacturerTest, ...] = (
    # Table 4-1, DC voltage offset ("Short"; card limits are Std = Opt 002).
    *(_cm("4-1", str(n), _V, 0.0, fs, card, note=(
        "manual: low-thermal copper short at the front terminals; here the "
        "calibrator's 0 V output through the test leads"))
      for n, (fs, card) in enumerate(((0.1, 1.06e-6), (1.0, 1.06e-6),
                                      (10.0, 2.3e-6), (100.0, 36e-6),
                                      (1000.0, 100e-6)), start=1)),
    # Table 4-2, DC voltage gain.
    _cm("4-2", "1", _V, 0.1, 0.1, 2.12e-6, 1.88e-6),
    _cm("4-2", "2", _V, 1.0, 1.0, 9.98e-6, 7.40e-6),
    _cm("4-2", "3", _V, 1.0, 10.0, 11.1e-6, 8.5e-6),
    _cm("4-2", "4", _V, -1.0, 10.0, 11.1e-6, 8.5e-6),
    _cm("4-2", "5", _V, -10.0, 10.0, 89.2e-6, 62.4e-6),
    _cm("4-2", "6", _V, 10.0, 10.0, 89.2e-6, 62.4e-6),
    _cm("4-2", "7", _V, 100.0, 100.0, 1.114e-3, 0.853e-3),
    _cm("4-2", "8", _V, 1000.0, 1000.0, 23.96e-3, 19.34e-3),
    # Table 4-3, AC voltage, SETACV ANA (the manual's DUT configuration).
    _cm("4-3", "1", _A, 0.1, 0.1, 25.0e-6, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "2", _A, 1.0, 1.0, 250e-6, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "3", _A, 1.0, 10.0, 0.96e-3, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "4", _A, 10.0, 10.0, 13.38e-3, frequency_hz=20.0, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "5", _A, 10.0, 10.0, 2.50e-3, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "6", _A, 10.0, 10.0, 2.72e-3, frequency_hz=20e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "7", _A, 10.0, 10.0, 53.72e-3, frequency_hz=100e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "8", _A, 10.0, 10.0, 554.5e-3, frequency_hz=1e6, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "9", _A, 100.0, 100.0, 36.4e-3, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    _cm("4-3", "10", _A, 700.0, 1000.0, 0.544, frequency_hz=1e3, mode="ana",
        apply=_CM_ACV_APPLY),
    # Table 4-4, DC current offset ("Open").
    *(_cm("4-4", str(n), _I, 0.0, fs, card, note=(
        "manual: input terminals open; here the calibrator's 0 A output"))
      for n, (fs, card) in enumerate(((1e-4, 0.95e-9), (1e-3, 6.5e-9),
                                      (1e-2, 65e-9), (0.1, 650e-9),
                                      (1.0, 11.5e-6)), start=1)),
    # Table 4-5, DC current gain.
    *(_cm("4-5", str(n), _I, fs, fs, card)
      for n, (fs, card) in enumerate(((1e-4, 3.56e-9), (1e-3, 32.3e-9),
                                      (1e-2, 323e-9), (0.1, 4.89e-6),
                                      (1.0, 134.9e-6)), start=1)),
    # Ohms offsets (OHMS TESTS card), front terminals.
    _cm("ohms 2w offset", "1", _R, 0.0, 10.0, 0.25007, connection=_2W,
        note="manual: low-thermal short at the front terminals; here the "
             "calibrator's 2-wire short through the test leads, whose "
             "resistance reads as offset"),
    _cm("ohms 4w offset", "1", _R, 0.0, 10.0, 70e-6, connection=_4W,
        note="manual: low-thermal short; here the calibrator's 4-wire short"),
    # Table 4-6, OHMF gain.
    *(_cm("4-6", str(n), _R, fs, fs, card, connection=_4W)
      for n, (fs, card) in enumerate(((10.0, 280e-6), (100.0, 2.31e-3),
                                      (1e3, 14.2e-3), (1e4, 142e-3),
                                      (1e5, 1.42), (1e6, 20.9),
                                      (1e7, 703.0)), start=1)),
    # Supplementary: the ranges Chapter 4 never touches.
    ManufacturerTest(
        test_id="SUP ACV 10mV ana", quantity=_A, value=0.01, range_fs=0.01,
        frequency_hz=1e3, mode="ana",
        apply=MappingProxyType(dict(_CM_ACV_APPLY)), supplementary=True,
        note="not in the Cal Manual: the 10 mV ac range in the manual's own "
             "ANA configuration, so that technique is exercised on every "
             "range"),
    ManufacturerTest(
        test_id="SUP OHM 1G", quantity=_R, value=1e8, range_fs=1e9,
        connection=_2W, supplementary=True,
        note="not in the Cal Manual: the 1 Gohm range at 10 % of range, "
             "from the calibrator's 100 Mohm standard (2-wire only)"),
)

#: Manufacturer verification tables, keyed by the model id of the meter
#: they verify.
MANUFACTURER_TESTS: Mapping[str, tuple[ManufacturerTest, ...]] = \
    MappingProxyType({"hp3458a": _HP3458A_TESTS})


def manufacturer_tests(model_id: str) -> tuple[ManufacturerTest, ...]:
    """The manufacturer's verification rows for *model_id*, or ``()``."""
    return MANUFACTURER_TESTS.get(str(model_id).strip().lower(), ())


def manufacturer_test_for(model_id: str, point) -> ManufacturerTest | None:
    """The manufacturer row *point* realises, matched by setpoint.

    By what was measured -- quantity, nominal value, the meter's range, tone,
    wiring and ac technique -- rather than by the label, so a hand-written plan
    or a result file gets the same answer as a generated plan.  *point* is
    anything with those attributes: a :class:`PlanPoint` or a result row.
    """
    tests = manufacturer_tests(model_id)
    if not tests:
        return None
    try:
        quantity = Quantity(point.quantity)
        value = getattr(point, "value", None)
        value = float(point.nominal if value is None else value)
        range_arg = getattr(point, "range_arg", None)
        frequency = float(getattr(point, "frequency_hz", 0.0) or 0.0)
        connection = Connection(getattr(point, "connection",
                                        Connection.TWO_WIRE))
    except (TypeError, ValueError):
        return None
    mode = getattr(point, "mode", None) or None
    for test in tests:
        if test.quantity is not quantity or test.connection is not connection:
            continue
        if not math.isclose(test.value, value, rel_tol=1e-9, abs_tol=1e-15):
            continue
        if range_arg is None or not math.isclose(float(range_arg),
                                                 test.range_fs, rel_tol=1e-9):
            continue
        if not math.isclose(test.frequency_hz, frequency, rel_tol=1e-9,
                            abs_tol=1e-9):
            continue
        if (test.mode or None) != mode:
            continue
        return test
    return None


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PlanPoint(Point):
    """A :class:`~caldmm.cal.roles.Point` plus what a *run* needs to know.

    Deliberately a subclass rather than a second point type.  ``roles.Point`` is
    the executable shape -- it is what the adapters dereference and what
    ``dataclasses.replace(point, value=...)`` is applied to -- and everything
    here is scheduling: how many samples to take, how many to throw away first,
    and a free-form operator note.  ``replace`` on a :class:`PlanPoint` returns a
    :class:`PlanPoint`, so the engine's rewriting of ``value`` keeps the plan's
    own fields.

    ``samples`` and ``discards`` are ``None`` when the point does not override
    the plan's defaults; resolve them with :meth:`TestPlan.samples_for` and
    :meth:`TestPlan.discards_for` rather than reading them directly.
    """

    samples: int | None = None
    discards: int | None = None
    note: str = ""
    #: An adder on the **calibrator's** figure at this point, in the
    #: quantity's own unit, for a test condition its specification does not
    #: cover.  Today that is exactly one thing: a resistance read outside the
    #: 5700A's full-spec load-current window, derated by the manual's own
    #: ``K(IL - I)/(IL x I)`` (see :func:`_load_derating`).  It is not part of
    #: any tolerance the spec tables state, and :attr:`note` always says where
    #: it came from.
    #:
    #: **The engine consumes it, in both roles** (H06,
    #: :data:`~caldmm.cal.engine.FLAG_LOAD_DERATED`): with the calibrator as
    #: the reference it is an extra Type B term on the calibrator's side of
    #: the budget, treated at the same confidence as the figure it is added
    #: to; with the calibrator under test it widens the calibrator's own
    #: tolerance, because the manual's accuracy statement at that load is the
    #: stated figure plus the derating.  One place per role, never both.
    load_adder: float | None = None

    def describe(self) -> str:
        """A short human label, falling back to the numbers when unlabelled."""
        return self.label or _describe(self)


@dataclass(frozen=True, slots=True)
class SkippedPoint:
    """A candidate that was not planned, and every reason it was not.

    All the reasons, not the first: "the meter tops out at 1000 V" and "the
    calibrator's 1100 V range starts at 100 V" are different facts and an
    operator deciding what to do next wants both.
    """

    point: PlanPoint
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(str(r) for r in self.reasons))

    def __str__(self) -> str:
        return f"{self.point.describe()}: " + "; ".join(self.reasons)


@dataclass(frozen=True, slots=True)
class PointGroup:
    """A run of consecutive points sharing one physical wiring state.

    The boundary between two groups is where the operator re-cables, which is
    the only thing in a verification run that cannot be automated.  The runner
    prompts here, once, instead of per point.

    A group is keyed by the meter's **terminal set**
    (:class:`~caldmm.cal.quantities.Terminals`), not by the quantity: dc volts,
    ac volts and resistance all run through one 4-wire cable set, so they are
    one group with several *segments* -- consecutive runs of one
    ``(quantity, connection)`` -- and the calibrator re-configures itself
    between them without anyone touching a lead.  :attr:`quantity` and
    :attr:`connection` are the **first** segment's, for a caller that only
    needs one.
    """

    quantity: Quantity
    connection: Connection
    points: tuple[PlanPoint, ...]
    #: Index of the first point in :attr:`TestPlan.points`.
    start: int = 0
    terminals: Terminals | None = None

    def __post_init__(self) -> None:
        if self.terminals is None:
            object.__setattr__(self, "terminals", Terminals.of(self.quantity))

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[PlanPoint]:
        return iter(self.points)

    @property
    def segments(self) -> tuple[tuple[Quantity, Connection, int], ...]:
        """``(quantity, connection, count)`` per consecutive run, in order."""
        out: list[list] = []
        for point in self.points:
            key = (point.quantity, point.connection)
            if out and (out[-1][0], out[-1][1]) == key:
                out[-1][2] += 1
            else:
                out.append([point.quantity, point.connection, 1])
        return tuple((q, c, n) for q, c, n in out)

    @property
    def quantities(self) -> tuple[Quantity, ...]:
        """The quantities in this group, in run order, each once."""
        seen: list[Quantity] = []
        for quantity, _connection, _count in self.segments:
            if quantity not in seen:
                seen.append(quantity)
        return tuple(seen)

    @property
    def label(self) -> str:
        segments = self.segments
        if len(segments) <= 1:
            return f"{self.quantity.name} {self.connection.value}"
        return ", ".join(f"{q.name} {c.value}" for q, c, _n in segments)


@dataclass(frozen=True, slots=True)
class TestPlan:
    """An ordered list of points, and the conditions the run is judged under.

    ``interval`` names the DUT's specification column and ``confidence``,
    ``tur_floor``, ``guard_band_factor`` and ``temperature_policy`` are the
    decision-rule knobs :mod:`caldmm.cal.verdict` will be handed.  They live on
    the plan because they are properties of the *claim being tested*, not of any
    one point: a run that changed its decision rule half way through would not
    be reportable.
    """

    dut_role: str
    source_model_id: str
    meter_model_id: str
    points: tuple[PlanPoint, ...] = ()
    skipped: tuple[SkippedPoint, ...] = ()
    name: str = ""
    description: str = ""
    interval: str | None = None
    confidence: float = DEFAULT_CONFIDENCE
    tur_floor: float = DEFAULT_TUR_FLOOR
    guard_band_factor: float = DEFAULT_GUARD_FACTOR
    temperature_policy: str = "gate"
    samples: int = DEFAULT_SAMPLES
    discards: int = DEFAULT_DISCARDS

    #: Not a pytest test class.  The name is the domain's, and pytest collects
    #: anything called ``Test*`` that a test module imports.
    __test__ = False

    def __post_init__(self) -> None:
        role = str(self.dut_role).strip().lower()
        if role not in DUT_ROLES:
            raise PlanError(
                f"dut_role must be one of {', '.join(DUT_ROLES)}, got "
                f"{self.dut_role!r}; a plan tests one instrument, and a "
                f"per-point dut_role would be meaningless")
        policy = str(self.temperature_policy).strip().lower()
        if policy not in TEMPERATURE_POLICIES:
            raise PlanError(
                f"temperature_policy must be one of "
                f"{', '.join(TEMPERATURE_POLICIES)}, got "
                f"{self.temperature_policy!r}")
        object.__setattr__(self, "dut_role", role)
        object.__setattr__(self, "temperature_policy", policy)
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "skipped", tuple(self.skipped))

    # -- identity ---------------------------------------------------------

    @property
    def dut_is_meter(self) -> bool:
        return self.dut_role == "meter"

    @property
    def dut_model_id(self) -> str:
        """Model id of the instrument under test."""
        return self.meter_model_id if self.dut_is_meter else self.source_model_id

    @property
    def reference_model_id(self) -> str:
        """Model id of the instrument serving as the reference."""
        return self.source_model_id if self.dut_is_meter else self.meter_model_id

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[PlanPoint]:
        return iter(self.points)

    # -- scheduling -------------------------------------------------------

    def samples_for(self, point: PlanPoint) -> int:
        value = getattr(point, "samples", None)
        return self.samples if value is None else int(value)

    def discards_for(self, point: PlanPoint) -> int:
        value = getattr(point, "discards", None)
        return self.discards if value is None else int(value)

    def groups(self) -> tuple[PointGroup, ...]:
        """Runs of consecutive points sharing one terminal set.

        Keyed by :class:`~caldmm.cal.quantities.Terminals` -- the leads the
        operator physically moves -- so dc volts, ac volts and 2- and 4-wire
        resistance, which share one 4-wire cable set, are **one** group, and
        a full plan has one boundary before the current functions.

        Consecutive, not sorted: the plan's order is the run's order, and a
        hand-edited plan that alternates terminal sets gets the groups it asked
        for rather than a tidied-up lie about how often the operator rewires.
        """
        points = self.points
        out: list[PointGroup] = []
        start = 0
        for index in range(1, len(points) + 1):
            if index < len(points) \
                    and Terminals.of(points[index].quantity) \
                    == Terminals.of(points[start].quantity):
                continue
            out.append(PointGroup(points[start].quantity,
                                  points[start].connection,
                                  tuple(points[start:index]), start))
            start = index
        return tuple(out)

    def point_counts(self) -> Mapping[str, int]:
        """Planned points per quantity, in plan order -- the size of the run."""
        counts: dict[str, int] = {}
        for point in self.points:
            counts[point.quantity.name] = counts.get(point.quantity.name, 0) + 1
        return MappingProxyType(counts)

    def skipped_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for entry in self.skipped:
            name = entry.point.quantity.name
            counts[name] = counts.get(name, 0) + 1
        return MappingProxyType(counts)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _as_spec(spec: ModelSpec | str) -> ModelSpec:
    return load_model(spec) if isinstance(spec, str) else spec


def _g(value: float) -> str:
    return f"{value:g}"


def _describe(point: Point) -> str:
    parts = [point.quantity.name, f"{_g(point.value)} {point.unit}"]
    if point.frequency_hz:
        parts.append(f"@ {_g(point.frequency_hz)} Hz")
    if point.range_id:
        parts.append(f"[{point.range_id}]")
    elif point.range_arg is not None:
        parts.append(f"[{_g(point.range_arg)} range]")
    parts.append(point.connection.value)
    return " ".join(parts)


def _clean(value: float) -> float:
    """Strip binary-float dust from a derived setpoint.

    ``0.22 * 0.1`` is ``0.022000000000000002``, which is *above* the 5700A's
    22 mV minimum output by one ulp on a good day and below it on a bad one.
    Twelve significant digits is far beyond any setpoint's resolution and lands
    the 10 %-of-range points exactly on the stated minima.
    """
    return float(f"{value:.12g}")


def _range_by_id(function: FunctionSpec, range_id: str) -> RangeSpec | None:
    for candidate in function.ranges:
        if candidate.range_id == range_id:
            return candidate
    return None


def _range_by_arg(function: FunctionSpec, range_arg: float) -> RangeSpec | None:
    for candidate in function.ranges:
        arg = candidate.range_arg if candidate.range_arg is not None \
            else candidate.fs
        if math.isclose(arg, float(range_arg), rel_tol=1e-9, abs_tol=0.0):
            return candidate
    return None


def _identity(point: Point) -> tuple:
    """What makes two points the same *setpoint*, for cross-checking a plan.

    Labels, notes and sample counts are presentation and scheduling; they do not
    make two points different measurements.

    **The range is part of the identity, on whichever side pins it.**  A
    meter-DUT plan pins the meter's range with ``range_arg`` and leaves
    ``range_id`` ``None`` on every point, so identity without it collapses "10 V
    on the 10 V range" and "10 V on the 100 V range" into one point -- two
    genuinely different measurements of the same setpoint, and the whole axis a
    meter-DUT plan is built on.  With them merged, the 10 %-of-range ac point
    dropped by the 3458A's 12 %-of-range specification floor looked identical to
    the 100 %-of-range point one range down that was kept, and
    :func:`validate_plan` reported four "this point is also in points" problems
    for every meter-DUT plan with an ac function.
    """
    return (point.quantity, float(point.value), float(point.frequency_hz or 0.0),
            point.connection, point.range_id,
            None if point.range_arg is None else float(point.range_arg),
            point.mode)


def _range_label(range_spec: RangeSpec) -> str:
    return range_spec.range_id or f"{_g(range_spec.fs)} full-scale"


def _top_fs(function: FunctionSpec) -> float:
    return max(r.fs for r in function.ranges)


def _catalogue(function: FunctionSpec) -> tuple[float, ...]:
    return tuple(r.fs for r in function.ranges if r.discrete)


# --------------------------------------------------------------------------
# Envelope ceilings and the range lock
# --------------------------------------------------------------------------

def _range_cap(function: FunctionSpec, range_spec: RangeSpec,
               frequency_hz: float = 0.0) -> float | None:
    """The largest magnitude this range can handle at this frequency.

    Full scale, lowered by whatever the file says applies here: a band's own
    ``max_out`` (the 5700A's 1100 V ac range is limited to 250 V between 15 and
    50 Hz) and the function's volt-hertz product (2.2e7 V*Hz, which is what
    stops the 220 V ac range at 73.3 V by 300 kHz).  ``None`` when the range
    cannot make this frequency at all.

    This is an **envelope** question, not a tolerance one, so it takes no
    :class:`~caldmm.cal.specs.PairRole` and it obeys *every* row that covers
    the tone rather than the one row a tolerance lookup would select.  A
    generated point has to be one no applicable row says cannot be made; where
    two rows disagree, the lower ceiling is the one that can be run.
    """
    cap = float(range_spec.fs)
    if range_spec.bands and frequency_hz > 0.0:
        covering = [band for band in range_spec.bands
                    if band.covers(frequency_hz)]
        if not covering:
            return None
        cap = min([cap] + [float(band.max_out) for band in covering
                           if band.max_out is not None])
    if function.volt_hertz_max is not None and frequency_hz > 0.0:
        cap = min(cap, float(function.volt_hertz_max) / float(frequency_hz))
    return cap if cap > 0.0 else None


def _function_cap(function: FunctionSpec,
                  frequency_hz: float = 0.0) -> float | None:
    """The largest magnitude *function* can handle anywhere, at this frequency.

    The envelope the *other* instrument is capped by: 2.2 A of dc current from
    a 5700A, 700 V of ac from a 3458A, 220 V at 50 kHz from a 5700A (its 1100 V
    ac range stops at 1 kHz).  Discrete ranges are excluded -- a resistance
    catalogue has no ladder to clamp.
    """
    best: float | None = None
    for range_spec in function.ranges:
        if range_spec.discrete:
            continue
        cap = _range_cap(function, range_spec, frequency_hz)
        if cap is None:
            continue
        best = cap if best is None else max(best, cap)
    return best


def needs_range_lock(function: FunctionSpec, range_spec: RangeSpec,
                     value: float) -> bool:
    """Whether *value* reaches *range_spec* only with ``RANGELCK`` on.

    True exactly when autoranging -- "the lowest possible range" ([OM 1996]
    p.4-24) -- would select something else, which is the case for every
    10 %-of-range point and every zero above the bottom range.
    """
    if range_spec.discrete:
        return False
    natural = select_range(function, abs(float(value)))
    return natural is not None and natural is not range_spec


def range_lock_anchor(function: FunctionSpec, range_spec: RangeSpec, *,
                      frequency_hz: float = 0.0) -> float | None:
    """The output to program *before* ``RANGELCK ON`` to select *range_spec*.

    The 5700A has no command that names a range: a range is selected by
    outputting a value that autoranges onto it, and only then locked ([SM 2007]
    3-16: "Set the Calibrator output for 10V, then activate range lock for the
    11V range.  Now set the Calibrator to 0V").  The anchor is therefore the
    largest output the range can make here (:func:`_range_cap`) -- unambiguous,
    always inside the range, and always legal, where "one ulp above the range
    below" is neither.

    ``None`` means **no output selects this range at this frequency**, so the
    lock cannot be established and no point may be planned on it.  That happens
    when the range's ceiling here falls to or below the next range down, and it
    is why this is a plan-time check and not only an adapter-time one.
    """
    cap = _range_cap(function, range_spec, frequency_hz)
    if cap is None:
        return None
    anchor = _clean(cap)
    if range_spec.min_out is not None and anchor < range_spec.min_out:
        return None
    return anchor if select_range(function, anchor) is range_spec else None


def _range_lock_note(point: Point, spec: ModelSpec) -> str:
    """Say so on the point when the run has to lock the source's range."""
    if not getattr(point, "range_id", None):
        return ""
    function = spec.function(point.quantity)
    range_spec = None if function is None else _range_by_id(function,
                                                            point.range_id)
    if range_spec is None or not needs_range_lock(function, range_spec,
                                                  point.value):
        return ""
    anchor = range_lock_anchor(
        function, range_spec,
        frequency_hz=float(point.frequency_hz or 0.0))
    if anchor is None:                        # the point is dropped, not planned
        return ""
    return (f"sourced with the range lock on {_range_label(range_spec)} "
            f"(anchor {_g(anchor)} {point.unit} first, then the setpoint); "
            f"autoranging alone would select the range below")


def _join_notes(*parts: str) -> str:
    """The point's note, in the order a reader wants it.

    The spec ratio comes first and stays first: it is the number an operator
    scans a plan for, and the report and the tests both read it off the front.
    """
    return "; ".join(part for part in parts if part)


# --------------------------------------------------------------------------
# The driver envelope
# --------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _import_validate_output(module_name: str):
    """The driver's ``validate_output``, or ``None`` when it is not installed.

    Only :class:`ImportError` is swallowed -- "linux-gpib is not on this
    machine" is the expected answer and the file-side checks cover for it.
    Anything else is a bug in the driver, and because this result is cached a
    swallowed one would silently disable the cross-check for the rest of the
    process.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError:                    # pragma: no cover - laptop path
        return None
    return getattr(module, "validate_output", None)


def driver_envelope(spec: ModelSpec):
    """The source driver's own ``validate_output``, or ``None``.

    The 5700A's envelope is encoded twice on purpose and the two encodings check
    each other: the specification file carries ``min_out``, ``max_out``,
    ``volt_hertz_max`` and the discrete resistance catalogue, and the driver
    carries the same rules with their fault numbers.  This module runs both when
    it can, so a disagreement between them shows up as a skipped point with two
    reasons rather than as a fault at 1100 V.

    Returns ``None`` when the model declares no driver, or when the driver
    module cannot be imported, in which case the file-side checks stand alone.
    (``caldmm.drivers.fluke5700a`` defers its GPIB and serial imports, so it
    normally imports everywhere.)  The file-side checks cover every envelope rule in this file; what is
    lost is the fault-code wording and the 5725A amplifier corner, and the
    amplifier is excluded from the specification file anyway.
    """
    if spec.driver is None:
        return None
    return _import_validate_output(spec.driver.module)


# --------------------------------------------------------------------------
# Frequency selection
# --------------------------------------------------------------------------

def _band_frequency(range_spec: RangeSpec, band) -> float | None:
    """One frequency inside *band* that the spec layer resolves back to it.

    Strictly inside, never on an edge.  Band endpoints are inclusive, so a tone
    on one is covered by two rows and which of them judges it depends on the
    :class:`~caldmm.cal.specs.PairRole` of the instrument being asked -- the
    tighter row for a DUT, the looser for a reference.  A *representative* tone
    must not depend on who is asking: at 40 Hz "for the 20-40 Hz band" the DUT
    would be judged against one row and the reference against the other, and
    the band this function was called for might be neither.  Break-point tones
    are a deliberate, opt-in choice (:data:`FREQUENCY_PRESETS`), not something
    the default ladder should stumble into.

    The choice is then confirmed by asking ``select_band`` where the frequency
    lands **under one of the band's own modes** -- not under the default.  The
    default is the wrong question once the plan pins a mode per point: at 200 Hz
    on a 3458A the mode-blind default is ANA's 100 Hz - 20 kHz band, but a point
    pinned to SYNC lands squarely in SYNC's 40 Hz - 1 kHz band.  Asking the
    mode-blind question here used to leave a plan with no ac point anywhere
    between 30 Hz and 5 kHz -- 1 kHz, the one tone every ac verification has.

    Bands are logarithmic, so the target is the geometric centre and the
    candidates are the round numbers of :data:`PREFERRED_FREQUENCIES`.
    """
    low, high = float(band.freq_low_hz), float(band.freq_high_hz)
    if not (low > 0.0) or not (high > low):
        return None
    target = math.sqrt(low * high)
    inside = [f for f in PREFERRED_FREQUENCIES if low < f < high]
    if inside:
        best = min(abs(math.log10(f / target)) for f in inside)
        near = [f for f in inside
                if abs(math.log10(f / target)) <= best + _FREQUENCY_TIE_DECADES]
        ordered = sorted(near, key=lambda f: (_FREQUENCY_RANK[f],
                                              abs(math.log10(f / target))))
    else:
        candidate = _clean(float(f"{target:.3g}"))
        if not (low < candidate < high):
            candidate = target
        ordered = [candidate]
    modes = band.modes or (None,)
    for frequency in ordered:
        if any(select_band(range_spec, frequency, mode) is band
               for mode in modes):
            return frequency
    # Every candidate is shadowed even under this band's own modes, so another
    # band holds them and the frequency is not lost -- returning None lets the
    # caller move on rather than emitting a point judged by a row nobody chose.
    return None


def representative_frequencies(range_spec: RangeSpec) -> tuple[float, ...]:
    """One frequency per band of *range_spec*, deduplicated across modes.

    "At least one point per band" is the requirement, and a band is *covered*
    when a chosen frequency falls inside it -- not when a point was generated
    naming it.  That distinction is what makes an ac plan finite: the 3458A
    declares five ac techniques whose bands overlap heavily, and one 1 kHz tone
    sits inside the 40 Hz - 1 kHz SYNC band, the 100 Hz - 20 kHz ANA band and
    the 20 Hz - 100 kHz RNDM band at once.  Which of them supplies the tolerance
    is a *mode* decision, taken per point against the meter's own spec; running
    the same tone once per ac technique would be a mode study, not a
    verification.
    """
    chosen: list[float] = []
    for band in range_spec.bands:
        if any(band.covers(f) for f in chosen):
            continue
        frequency = _band_frequency(range_spec, band)
        if frequency is not None and frequency not in chosen:
            chosen.append(frequency)
    return tuple(sorted(chosen))


def _pick_mode(function: FunctionSpec, range_spec: RangeSpec, value: float,
               frequency_hz: float, interval: str | None,
               role: PairRole | None = None) -> str | None:
    """The ac technique or filter this point is run in, pinned into the plan.

    Chosen as the mode with the **tightest** limit here, ties broken by the
    function's declaration order (which the spec files write best-first).  The
    spec layer's own default -- highest ``freq_low_hz`` wins, then declaration
    order -- is not good enough to pin a plan with: on the 3458A, SYNC's bands
    and ANA's bands have different geometry, so at 500 kHz the default lands on
    ANA purely because its band edge happens to fall there, and ANA's
    specification is the looser one.  A plan that silently chose the looser
    technique would pass a meter that should fail, which is exactly what
    :func:`~caldmm.cal.specs.ambiguous_mode_siblings` exists to warn about.
    Pinning the choice also means the run is reproducible and the operator can
    override it by editing one field.
    """
    if not function.modes:
        return None
    best: tuple[float, int] | None = None
    best_id: str | None = None
    for order, mode in enumerate(function.modes):
        if not mode.covers(frequency_hz):
            continue
        band = select_band(range_spec, frequency_hz, mode.id, role=role,
                           value=value, interval=interval)
        if band is None:
            continue
        terms = band.limits.get(interval) if interval else None
        if terms is None and band.limits:
            # No row for this interval; fall back to whatever the band has, so
            # a mode is still chosen rather than the point losing its technique.
            terms = next(iter(band.limits.values()))
        if terms is None:
            continue
        key = (terms.limit(value, range_spec.fs), order)
        if best is None or key < best:
            best, best_id = key, mode.id
    if best_id is not None:
        return best_id
    mode = select_mode(function, select_band(range_spec, frequency_hz),
                       frequency_hz)
    return mode.id if mode is not None else None


# --------------------------------------------------------------------------
# Per-point checks -- shared by generation and validation
# --------------------------------------------------------------------------

def _source_problems(spec: ModelSpec, point: Point,
                     role: PairRole | None = None) -> list[str]:
    """Every reason this source cannot produce *point*.  **Offline.**"""
    problems: list[str] = []
    quantity = point.quantity
    unit = quantity.unit
    value = float(point.value)
    frequency = float(point.frequency_hz or 0.0)

    function = spec.function(quantity)
    if function is None:
        return [f"{spec.model_id} does not source {quantity.name}"]

    if function.connections and point.connection not in function.connections:
        problems.append(
            f"{spec.model_id} does not source {quantity.name} in "
            f"{point.connection.value}")

    if frequency and not quantity.frequency_dependent:
        problems.append(
            f"{quantity.name} is a dc quantity but the point states "
            f"{_g(frequency)} Hz")
    if quantity.frequency_dependent and not frequency > 0.0:
        problems.append(
            f"{quantity.name} needs a frequency; the point states "
            f"{_g(frequency)} Hz")
    if quantity.frequency_dependent and value <= 0.0:
        problems.append(
            f"an ac amplitude must be a positive rms value, got {_g(value)} "
            f"{unit}")
    if quantity is Quantity.RES and value < 0.0:
        problems.append(f"resistance must not be negative, got {_g(value)} {unit}")

    if point.range_id:
        range_spec = _range_by_id(function, point.range_id)
        if range_spec is None:
            ids = ", ".join(r.range_id for r in function.ranges if r.range_id)
            problems.append(
                f"{spec.model_id} has no {quantity.name} range "
                f"{point.range_id!r} (known: {ids or 'none'})")
            return problems
        if not range_spec.holds(value):
            problems.append(
                f"the {_range_label(range_spec)} range of {spec.model_id} does "
                f"not source {_g(value)} {unit}")
        elif needs_range_lock(function, range_spec, value) \
                and range_lock_anchor(function, range_spec,
                                      frequency_hz=frequency) is None:
            # The plan credits this range, so the run must be able to put the
            # output on it -- which means locking it, which means some output
            # has to select it first.  Where none does, the point would be
            # sourced on the range below and reported against this one.
            problems.append(
                f"{_g(value)} {unit} cannot be put on the "
                f"{_range_label(range_spec)} range of {spec.model_id}: it "
                f"autoranges to the range below and no output selects "
                f"{_range_label(range_spec)}"
                + (f" at {_g(frequency)} Hz" if frequency else "")
                + " for a range lock to hold")
    else:
        range_spec = select_range(function, value)
        if range_spec is None:
            catalogue = _catalogue(function)
            if catalogue:
                problems.append(
                    f"{_g(value)} {unit} is not one of the {len(catalogue)} "
                    f"values {spec.model_id} sources "
                    f"({', '.join(_g(v) for v in catalogue)}); resistance is a "
                    f"discrete catalogue, not a continuum")
            else:
                problems.append(
                    f"{spec.model_id} has no {quantity.name} range that can "
                    f"source {_g(value)} {unit} (top range "
                    f"{_g(_top_fs(function))} {unit})")
            return problems

    if not range_spec.allows(point.connection):
        problems.append(
            f"{point.connection.value} is not available on the "
            f"{_range_label(range_spec)} range of {spec.model_id}")
    if range_spec.min_out is not None and abs(value) < range_spec.min_out:
        problems.append(
            f"{_g(value)} {unit} is below the {_g(range_spec.min_out)} {unit} "
            f"minimum output of the {_range_label(range_spec)} range of "
            f"{spec.model_id}")

    if range_spec.bands and frequency > 0.0:
        band = select_band(range_spec, frequency, role=role, value=value)
        if band is None:
            spans = ", ".join(f"{_g(b.freq_low_hz)}-{_g(b.freq_high_hz)} Hz"
                              for b in range_spec.bands)
            problems.append(
                f"{spec.model_id} cannot source {_g(frequency)} Hz on the "
                f"{_range_label(range_spec)} range (bands: {spans})")
        elif band.max_out is not None and abs(value) > band.max_out:
            problems.append(
                f"{_g(value)} {unit} exceeds the {_g(band.max_out)} {unit} "
                f"limit of the {_range_label(range_spec)} range between "
                f"{_g(band.freq_low_hz)} and {_g(band.freq_high_hz)} Hz")

    if (function.volt_hertz_max is not None and frequency > 0.0
            and abs(value) * frequency > function.volt_hertz_max * (1 + 1e-9)):
        problems.append(
            f"{_g(value)} {unit} x {_g(frequency)} Hz exceeds the "
            f"{function.volt_hertz_max:.3E} {unit}*Hz envelope of "
            f"{spec.model_id}")

    validate_output = driver_envelope(spec)
    if validate_output is not None:
        problems.extend(validate_output(value, unit, frequency,
                                        range_id=point.range_id))
    return problems


def _meter_problems(spec: ModelSpec, point: Point,
                    role: PairRole | None = None) -> list[str]:
    """Every reason this meter cannot measure *point*.  **Offline.**"""
    problems: list[str] = []
    quantity = point.quantity
    unit = quantity.unit
    value = float(point.value)
    frequency = float(point.frequency_hz or 0.0)

    function = spec.function(quantity)
    if function is None:
        return [f"{spec.model_id} does not measure {quantity.name}"]
    if function.connections and point.connection not in function.connections:
        problems.append(
            f"{spec.model_id} does not measure {quantity.name} in "
            f"{point.connection.value}")

    if point.range_arg is not None:
        range_spec = _range_by_arg(function, point.range_arg)
        if range_spec is None:
            args = ", ".join(_g(r.range_arg if r.range_arg is not None else r.fs)
                             for r in function.ranges)
            problems.append(
                f"{spec.model_id} has no {quantity.name} range with range_arg "
                f"{_g(point.range_arg)} (known: {args})")
            return problems
        if not range_spec.holds(value):
            problems.append(
                f"the {_g(range_spec.fs)} {unit} range of {spec.model_id} does "
                f"not read {_g(value)} {unit}")
    else:
        range_spec = select_range(function, value)
        if range_spec is None:
            problems.append(
                f"{spec.model_id} has no {quantity.name} range that reads "
                f"{_g(value)} {unit} (top range {_g(_top_fs(function))} {unit})")
            return problems

    if not range_spec.allows(point.connection):
        problems.append(
            f"{point.connection.value} is not available on the "
            f"{_g(range_spec.fs)} {unit} range of {spec.model_id}")

    if point.mode is not None:
        mode = function.mode(point.mode)
        if mode is None:
            problems.append(
                f"{spec.model_id} has no {quantity.name} mode {point.mode!r} "
                f"(declared: {', '.join(function.mode_ids) or 'none'})")
        elif not mode.covers(frequency if quantity.frequency_dependent else None):
            problems.append(
                f"mode {point.mode!r} of {spec.model_id} is not valid at "
                f"{_g(frequency)} Hz")

    # The floor of the **pinned mode**, not the range-wide one (M69).  The
    # range-wide figure is the most restrictive mode's -- on the 3458A's 10 mV
    # ac range that is ANA's quarter of full scale -- and applying it to a
    # point the plan has pinned to SYNC, which is specified to a tenth of full
    # scale, removed the whole of the 5700A's 2.2 mV ac range for a rule that
    # does not apply to the technique in use.
    floor = range_spec.min_reading_frac_for(point.mode)
    if floor is not None and abs(value) < floor * range_spec.fs:
        # Message quality, not a filter.  Below this fraction of range the
        # datasheet states nothing, so `evaluate_limit` returns None and
        # `_limit_problems` would drop exactly the same points -- with a generic
        # "states no specification" that leaves the operator guessing.  Naming
        # the floor is the difference between "why is my 10 % ac point gone"
        # and "the 5700A's 10 %-of-range ladder lands under the 3458A's 12 %".
        problems.append(
            f"{_g(value)} {unit} is below the {_g(floor * 100)} % of range "
            f"floor of {spec.model_id}'s {_g(range_spec.fs)} {unit} "
            f"{quantity.name}"
            + (f" {point.mode}" if point.mode else "") + " specification")

    if range_spec.bands and quantity.frequency_dependent:
        band = select_band(range_spec, frequency, point.mode, role=role,
                           value=value)
        if band is None:
            spans = ", ".join(f"{_g(b.freq_low_hz)}-{_g(b.freq_high_hz)} Hz"
                              for b in range_spec.bands)
            problems.append(
                f"{spec.model_id} has no band covering {_g(frequency)} Hz on "
                f"its {_g(range_spec.fs)} {unit} range (bands: {spans})")
        elif band.max_out is not None and abs(value) > band.max_out:
            problems.append(
                f"{_g(value)} {unit} exceeds the {_g(band.max_out)} {unit} "
                f"limit of {spec.model_id}'s {_g(range_spec.fs)} {unit} range "
                f"between {_g(band.freq_low_hz)} and {_g(band.freq_high_hz)} Hz")
    return problems


def _limit_at(spec: ModelSpec, point: Point, interval: str | None,
              *, is_meter: bool, role: PairRole | None = None):
    """This model's absolute tolerance at *point*, or ``None``.

    Resolves the range the way the point pins it -- ``range_arg`` on the meter
    side, ``range_id`` on the source side -- so the limit comes from the range
    the plan actually named rather than from whatever autoranging would pick.
    Propagates :class:`~caldmm.cal.specs.SpecError`; the caller decides whether
    an ambiguous mode is a problem or merely a missing note.

    *role* decides which row owns a shared band edge (M67/H08): the tighter one
    for the instrument under test, the looser one for the reference.  It never
    changes the mode, and never turns a specified point into an unspecified
    one -- see :func:`~caldmm.cal.specs.select_band`.
    """
    function = spec.function(point.quantity)
    if function is None:
        return None
    range_fs: float | None = None
    if is_meter and point.range_arg is not None:
        found = _range_by_arg(function, point.range_arg)
        range_fs = found.fs if found is not None else None
    elif not is_meter and point.range_id:
        found = _range_by_id(function, point.range_id)
        range_fs = found.fs if found is not None else None
    mode = point.mode if (point.mode and function.mode(point.mode)) else None
    frequency = (float(point.frequency_hz)
                 if point.quantity.frequency_dependent else None)
    return evaluate_limit(spec, point.quantity, float(point.value),
                          range_fs=range_fs, freq_hz=frequency,
                          connection=point.connection,
                          interval=interval, mode=mode, role=role)


def _limit_problems(spec: ModelSpec, point: Point, interval: str | None,
                    *, is_meter: bool, role: PairRole) -> list[str]:
    """Whether *spec* actually states a tolerance at this point.

    Only asked of a model whose ``limits_authority`` is ``FILE``.  The 5700A
    computes its own uncertainty in firmware and its transcribed tables are a
    cross-check, so a gap in them is not a reason to drop a point -- ``UNCERT?``
    will answer at run time.
    """
    if spec.limits_authority is not LimitsAuthority.FILE:
        return []
    if spec.function(point.quantity) is None:
        return []                       # already reported by the capability check
    try:
        result = _limit_at(spec, point, interval, is_meter=is_meter, role=role)
    except SpecError as exc:
        return [str(exc)]
    if result is None:
        tail = ("so there is no tolerance to test" if role is PairRole.DUT
                else "so it cannot serve as the reference here")
        return [f"{spec.model_id} states no {interval or 'default'} "
                f"specification at this point, {tail}"]
    return []


def point_problems(point: Point, source_spec: ModelSpec, meter_spec: ModelSpec,
                   *, dut_role: str, interval: str | None = None) -> list[str]:
    """Every reason *point* cannot be run.  **Offline, no instrument.**

    The single check used by both :func:`generate_plan` and
    :func:`validate_plan`, so that a hand-edited plan is held to exactly the
    rules a generated one was built from.
    """
    source_spec = _as_spec(source_spec)
    meter_spec = _as_spec(meter_spec)
    dut_is_meter = str(dut_role).strip().lower() == "meter"
    meter_role = PairRole.DUT if dut_is_meter else PairRole.REFERENCE
    source_role = PairRole.REFERENCE if dut_is_meter else PairRole.DUT
    source_problems = _source_problems(source_spec, point, source_role)
    meter_problems = _meter_problems(meter_spec, point, meter_role)
    problems = source_problems + meter_problems
    dut_interval = interval or (meter_spec if dut_is_meter
                                else source_spec).default_interval
    # Only ask for a tolerance where the instrument was found capable.  A point
    # the meter cannot reach has no specification either, and reporting both
    # buries the fact that matters under a restatement of it.
    if not meter_problems:
        problems += _limit_problems(
            meter_spec, point,
            dut_interval if dut_is_meter else meter_spec.default_interval,
            is_meter=True, role=meter_role)
    if not source_problems:
        problems += _limit_problems(
            source_spec, point,
            source_spec.default_interval if dut_is_meter else dut_interval,
            is_meter=False, role=source_role)
    return problems


def spec_ratio_note(point: Point, source_spec: ModelSpec, meter_spec: ModelSpec,
                    *, dut_role: str, interval: str | None = None) -> str:
    """How much better the reference's specification is than the DUT's, here.

    ``DUT tolerance / reference tolerance``, both from the transcribed tables,
    written onto the point so a run is self-explaining before it starts.  A
    verification is only capable of a verdict when this is comfortably above
    one, and on the bottom of a 3458A's dc-current range against a 5700A it is
    **0.004**: the meter's 100 nA range is specified to 4.3e-11 A while the
    calibrator's own floor at that output is 1e-8 A.  Eighteen such points in a
    generated plan return ``INDETERMINATE`` no matter how well the instruments
    behave, and an operator who has not been told reads that as breakage.

    Deliberately **not** called a TUR and deliberately **not** a filter.  A TUR
    (Z540.3 3.11) divides by the expanded measurement uncertainty ``U`` at 95 %,
    which needs the budget :mod:`caldmm.cal.uncertainty` builds from real
    readings -- the reference's tolerance is only a planning stand-in for it.
    Whether a poor ratio should drop a point belongs to the runner, with the
    instrument's own ``UNCERT?`` in hand; this is a number on a note.

    Returns ``""`` when either side has no figure at this point.
    """
    source_spec = _as_spec(source_spec)
    meter_spec = _as_spec(meter_spec)
    dut_is_meter = str(dut_role).strip().lower() == "meter"
    dut_spec = meter_spec if dut_is_meter else source_spec
    ref_spec = source_spec if dut_is_meter else meter_spec
    dut_interval = interval or dut_spec.default_interval
    try:
        dut = _limit_at(dut_spec, point, dut_interval, is_meter=dut_is_meter,
                        role=PairRole.DUT)
        ref = _limit_at(ref_spec, point, ref_spec.default_interval,
                        is_meter=not dut_is_meter, role=PairRole.REFERENCE)
    except SpecError:
        return ""
    if dut is None or ref is None or not ref.limit > 0.0:
        return ""
    return (f"spec ratio {dut.limit / ref.limit:.3g} "
            f"({dut_spec.model_id} {dut.limit:.3g} / "
            f"{ref_spec.model_id} {ref.limit:.3g} {point.unit})")


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _resolve_quantities(quantities, source_spec: ModelSpec,
                        meter_spec: ModelSpec) -> tuple[Quantity, ...]:
    if quantities is None:
        # The union, not the DUT's own set: a quantity the reference offers and
        # the DUT does not is worth one line in `skipped` saying so.  That is
        # how an operator learns the 1281 is a dc-volts-only instrument from the
        # plan rather than from a short report.
        wanted = set(source_spec.functions) | set(meter_spec.functions)
    else:
        wanted = set()
        for item in quantities:
            try:
                wanted.add(Quantity(item))
            except ValueError as exc:
                raise PlanError(
                    f"unknown quantity {item!r} (known: "
                    f"{', '.join(q.name for q in Quantity)})") from exc
    ordered = [q for q in QUANTITY_ORDER if q in wanted]
    ordered += sorted(wanted - set(QUANTITY_ORDER), key=lambda q: q.name)
    return tuple(ordered)


def _resolve_connections(connections, quantity: Quantity,
                         source_function: FunctionSpec | None,
                         meter_function: FunctionSpec | None
                         ) -> tuple[list[Connection], list[Connection]]:
    """``(usable, requested_but_unavailable)`` for one quantity."""
    available = []
    for connection in CONNECTION_ORDER:
        if source_function is not None and source_function.connections \
                and connection not in source_function.connections:
            continue
        if meter_function is not None and meter_function.connections \
                and connection not in meter_function.connections:
            continue
        available.append(connection)
    if connections is None:
        # One connection per quantity by default: changing it means the operator
        # physically re-cables, and a plan that silently doubles the resistance
        # run to cover both wirings is not a kindness.
        return (available[:1], [])
    wanted = []
    for item in connections:
        try:
            wanted.append(Connection(item))
        except ValueError as exc:
            raise PlanError(
                f"unknown connection {item!r} (known: "
                f"{', '.join(c.value for c in Connection)})") from exc
    usable = [c for c in CONNECTION_ORDER if c in wanted and c in available]
    missing = [c for c in wanted if c not in available]
    return (usable, missing)


def _range_axis(function: FunctionSpec) -> tuple[RangeSpec, ...]:
    return tuple(sorted(function.ranges, key=lambda r: r.fs))


def _magnitudes(range_spec: RangeSpec, percentages: Sequence[float],
                include_zero: bool, quantity: Quantity) -> list[float]:
    values: list[float] = []
    if include_zero and not quantity.frequency_dependent:
        values.append(0.0)
    for percentage in percentages:
        magnitude = _clean(range_spec.fs * float(percentage) / 100.0)
        if magnitude == 0.0 or magnitude in values:
            continue
        values.append(magnitude)
    return values


def _signed(magnitude: float, include_negative: bool,
            quantity: Quantity) -> list[float]:
    if magnitude == 0.0:
        return [0.0]
    if not include_negative or quantity.frequency_dependent \
            or quantity is Quantity.RES:
        return [magnitude]
    return [magnitude, -magnitude]


def break_point_frequencies(range_spec: RangeSpec) -> tuple[float, ...]:
    """Every band edge this range declares -- the manufacturer's own points.

    A specification changes at a break point, so that is where a claim is
    weakest and where the manufacturers put their verification tones.  The
    edges are inclusive on both sides, which is exactly the case
    :class:`~caldmm.cal.specs.PairRole` exists to settle: the DUT is judged on
    the tighter of the two rows.
    """
    edges = {float(band.freq_low_hz) for band in range_spec.bands}
    edges |= {float(band.freq_high_hz) for band in range_spec.bands}
    return tuple(sorted(f for f in edges if f > 0.0))


def _frequency_axis(range_spec: RangeSpec,
                    frequencies: Sequence[float] | str | None
                    ) -> tuple[float, ...]:
    if frequencies is None:
        return representative_frequencies(range_spec)
    if isinstance(frequencies, str):
        return break_point_frequencies(range_spec)
    wanted = tuple(sorted({_clean(float(f)) for f in frequencies}))
    return tuple(f for f in wanted
                 if any(band.covers(f) for band in range_spec.bands))


def _nearest(values: Sequence[float], target: float) -> float:
    return min(values, key=lambda f: abs(math.log10(f / target)))


def _floor_sig(value: float, digits: int = 3) -> float:
    """*value* rounded **down** to *digits* significant figures.

    A substitute setpoint has to stay inside the envelope it was derived from,
    so it rounds down and never up; three figures keeps 220 V and 700 V exact
    while turning the volt-hertz ceiling at 300 kHz into 73.3 V rather than
    73.33333333 V.
    """
    if value <= 0.0:
        return value
    scale = 10.0 ** (digits - 1 - math.floor(math.log10(value)))
    return _clean(math.floor(value * scale) / scale)


def _reachable_max(dut_function: FunctionSpec, range_spec: RangeSpec,
                   partner_function: FunctionSpec | None,
                   frequency_hz: float = 0.0) -> float | None:
    """The largest magnitude **both** instruments can manage on this range.

    The DUT's range says what it can make or read here; the partner's whole
    function says what it can meet.  ``None`` when the range cannot make this
    frequency at all -- there is nothing to substitute then, and the candidate
    that names the frequency will be dropped with the reason.
    """
    here = _range_cap(dut_function, range_spec, frequency_hz)
    if here is None:
        return None
    there = (None if partner_function is None
             else _function_cap(partner_function, frequency_hz))
    return _floor_sig(here if there is None else min(here, there))


def _substitute(range_spec: RangeSpec, magnitude: float,
                cap: float | None) -> float | None:
    """The value that stands in for *magnitude* when the pair cannot reach it.

    ``None`` when nothing needs to stand in, or when the substitute would not
    be a legal output on this range either -- a candidate the plan should drop
    with its reason rather than replace with an illegal one.
    """
    if cap is None or magnitude <= cap or cap <= 0.0:
        return None
    if range_spec.min_out is not None and cap < range_spec.min_out:
        return None
    return cap if range_spec.holds(cap) else None


def _generate_range_points(quantity: Quantity, connection: Connection,
                           range_spec: RangeSpec, *, dut_is_meter: bool,
                           source_function: FunctionSpec | None,
                           meter_function: FunctionSpec | None,
                           meter_interval: str | None,
                           percentages, include_zero, include_negative,
                           frequencies, reference_frequency_hz,
                           frequency_sweep_pct) -> list[PlanPoint]:
    """Candidates on one range of the DUT, in the order an operator runs them.

    Where a percentage of full scale is beyond what the *pair* can manage, the
    largest value both instruments do reach is added beside it (M70/M71): the
    ladder point itself is still generated, and still lands in ``skipped`` with
    the reason it could not be made, so the plan says both what was wanted and
    what was run.  That one rule is what keeps the 34401A's 3 A ranges verified
    to 2.2 A rather than 1.5 A, puts the 3458A's 1000 V ac range at its
    specified 700 V, and re-homes a frequency sweep tone by tone instead of
    letting it vanish with the magnitude it was pinned to.
    """
    magnitudes = _magnitudes(range_spec, percentages, include_zero, quantity)
    range_id = range_spec.range_id if not dut_is_meter else None
    range_arg = (range_spec.range_arg if range_spec.range_arg is not None
                 else range_spec.fs) if dut_is_meter else None
    dut_function = meter_function if dut_is_meter else source_function
    partner_function = source_function if dut_is_meter else meter_function

    def _build(value: float, frequency: float, note: str = "") -> PlanPoint:
        mode = None
        if quantity.frequency_dependent and meter_function is not None:
            meter_range = (_range_by_arg(meter_function, range_arg)
                           if dut_is_meter and range_arg is not None
                           else select_range(meter_function, abs(value)))
            if meter_range is not None:
                mode = _pick_mode(meter_function, meter_range, abs(value),
                                  frequency, meter_interval,
                                  PairRole.DUT if dut_is_meter
                                  else PairRole.REFERENCE)
        return PlanPoint(quantity=quantity, value=value, frequency_hz=frequency,
                         connection=connection, range_id=range_id,
                         range_arg=range_arg, mode=mode, note=note)

    def _stand_in_note(magnitude: float, cap: float,
                       frequency: float) -> str:
        where = f" at {_g(frequency)} Hz" if frequency else ""
        return (f"{_g(cap)} {quantity.unit} is the largest value both "
                f"instruments reach on this range{where}; it stands in for "
                f"the {_g(magnitude)} {quantity.unit} point")

    points: list[PlanPoint] = []
    if not quantity.frequency_dependent:
        cap = _reachable_max(dut_function, range_spec, partner_function)
        stand_in = next((s for s in (_substitute(range_spec, m, cap)
                                     for m in magnitudes)
                         if s is not None and s not in magnitudes), None)
        for magnitude in magnitudes:
            for value in _signed(magnitude, include_negative, quantity):
                points.append(_build(value, 0.0))
        if stand_in is not None:
            note = _stand_in_note(max(magnitudes), stand_in, 0.0)
            for value in _signed(stand_in, include_negative, quantity):
                points.append(_build(value, 0.0, note))
        return points

    axis = _frequency_axis(range_spec, frequencies)
    if not axis:
        return []
    if frequencies is None and any(band.covers(reference_frequency_hz)
                                   for band in range_spec.bands):
        # 1 kHz wherever 1 kHz is legal: the amplitude sweep's tone should be
        # the same number on every range, not whatever the band geometry
        # happened to produce, or two ranges' ladders cannot be compared.
        if reference_frequency_hz not in axis:
            axis = tuple(sorted(axis + (reference_frequency_hz,)))
        reference = reference_frequency_hz
    else:
        reference = _nearest(axis, reference_frequency_hz)
    sweep_magnitude = _clean(range_spec.fs * float(frequency_sweep_pct) / 100.0)
    if sweep_magnitude not in magnitudes:
        # The frequency sweep must happen somewhere.  Asking for
        # percentages=(10, 50) with the sweep pinned at 100 % would otherwise
        # produce a plan with one tone per range and no note saying the sweep
        # was dropped.
        sweep_magnitude = max(magnitudes)
    seen: set[tuple[float, float]] = set()

    def _emit(magnitude: float, frequency: float, note: str = "") -> None:
        key = (magnitude, frequency)
        if key in seen:
            return
        seen.add(key)
        points.append(_build(magnitude, frequency, note))

    for magnitude in magnitudes:
        # The amplitude sweep runs at one tone, the frequency sweep at one
        # amplitude.  The full cross-product is len(percentages) x len(bands)
        # points per range -- an ac run nobody executes -- and it answers no
        # question the two sweeps do not.
        tones = [reference]
        if magnitude == sweep_magnitude:
            tones = list(axis)
        for frequency in tones:
            _emit(magnitude, frequency)
            # Per tone, because the ceiling moves with frequency: the 5700A's
            # 1100 V ac range stops at 1 kHz and is capped at 250 V below
            # 50 Hz, so a sweep pinned to one magnitude loses whole bands.
            stand_in = _substitute(
                range_spec, magnitude,
                _reachable_max(dut_function, range_spec, partner_function,
                               frequency))
            if stand_in is not None:
                _emit(stand_in, frequency,
                      _stand_in_note(magnitude, stand_in, frequency))
    points.sort(key=lambda p: (abs(p.value), p.frequency_hz))
    return points


#: How the manual's own load-current units read in amperes, so a note can
#: print "8-11 mA" the way [OM 1996] p.49 prints it rather than "0.008 A".
_LOAD_CURRENT_UNITS: Mapping[str, float] = MappingProxyType({"mA": 1e-3,
                                                             "uA": 1e-6})


def _current_text(value_a: float, unit: str | None) -> str:
    per = _LOAD_CURRENT_UNITS.get(unit or "", 0.0)
    if not per:
        return f"{_g(value_a)} A"
    return f"{_g(value_a / per)} {unit}"


def _window_text(load, unit: str | None) -> str:
    per = _LOAD_CURRENT_UNITS.get(unit or "", 0.0)
    if not per:
        return f"{_g(load.min_a)}-{_g(load.max_a)} A"
    return f"{_g(load.min_a / per)}-{_g(load.max_a / per)} {unit}"


def _load_derating(range_spec: RangeSpec, current_a: float | None,
                   connection: Connection) -> tuple[float | None, str]:
    """``(adder, note)`` for reading this resistance at *current_a*.

    The 5700A's resistance uncertainty is stated **only** inside the Full Spec
    Load Range IL-IU ([OM 1996] lines 1960-2016, PDF p.49); outside it the
    manual gives a derating to add to the uncertainty, ``K(IL - I)/(IL x I)``
    below IL and ``K(I^2 - IU^2)`` above IU (lines 2064-2085, p.50).  Nothing
    in the calibrator can know the meter's test current, so ``UNCERT?`` cannot
    include this and the plan has to.

    The adder comes back in the quantity's own unit -- the manual quotes ppm
    for every value but the short, whose constant is in milliohms because ppm
    of zero ohms is zero.  Where the manual's cell is blank (the 100 Mohm
    standard above its window) there is no number to add, and the note says so
    instead of inventing one.
    """
    load = range_spec.load_current
    if load is None or current_a is None:
        return (None, "")
    if load.in_window(current_a):
        return (None, "")
    two_wire = connection is Connection.TWO_WIRE_RCOMP
    under = load.under_error(current_a, two_wire=two_wire)
    over = load.over_error(current_a)
    side, unit = (("below", load.unit) if under is not None
                  else ("above", load.over_unit or load.unit))
    where = (f"read at {_current_text(current_a, unit)}, {side} the 5700A's "
             f"{_window_text(load, unit)} full-spec load window for "
             f"{_range_label(range_spec)}")
    error = under if under is not None else over
    if error is None:
        return (None, f"{where}; the manual states no derating constant there, "
                      f"so the calibrator's uncertainty is not claimed as "
                      f"stated")
    value, error_unit = error
    if error_unit == "ppm":
        adder = value * 1e-6 * abs(range_spec.fs)
        printed = f"{value:.3g} ppm ({_g(adder)} OHM)"
    else:                                     # the short: milliohms, absolute
        adder = value * 1e-3
        printed = f"{value:.3g} mOhm"
    return (adder, f"{where}: add {printed} per the OM current derating")


def _resistance_meter_range(meter_function: FunctionSpec, value: float,
                            range_spec: RangeSpec) -> RangeSpec | None:
    """The meter range this catalogue value is read on.

    The smallest range that holds it, as everywhere else -- **unless** that
    range's test current falls outside the calibrator's full-spec load window
    and another range's does not.  A larger meter range costs a little of the
    meter's own ppm-of-range term; reading a 5700A resistance below its minimum
    load costs the derating in :func:`_load_derating`, which at 19 ohm on a
    1 mA range is 14 ppm against a 31 ppm one-year figure.
    """
    default = select_range(meter_function, value)
    load = range_spec.load_current
    if default is None or load is None:
        return default
    if default.test_current_a is None or load.in_window(default.test_current_a):
        return default
    for candidate in sorted(meter_function.ranges, key=lambda r: r.fs):
        if candidate.test_current_a is None or not candidate.holds(value):
            continue
        if load.in_window(candidate.test_current_a):
            return candidate
    return default


def _meter_offers(meter_function: FunctionSpec | None,
                  connection: Connection, value: float) -> bool:
    """Whether the meter can be wired this way for this value.  Offline."""
    if meter_function is None:
        return True
    if meter_function.connections \
            and connection not in meter_function.connections:
        return False
    meter_range = select_range(meter_function, value)
    return meter_range is None or meter_range.allows(connection)


def _resistance_candidates(quantity: Quantity, connection: Connection,
                           source_function: FunctionSpec,
                           meter_function: FunctionSpec | None, *,
                           dut_is_meter: bool,
                           include_zero: bool,
                           two_wire_fallback: bool = False) -> list[PlanPoint]:
    """The catalogue, in ascending order, with the short first.

    Generated from the **source's** ranges in both directions, because the
    catalogue is a property of the calibrator's resistors: there is no such
    thing as "90 % of the 19 kohm range".  Each point carries ``range_id`` so
    that a characterized readback resolves by identifier rather than by falling
    through a value-ordered ladder into the next, looser, entry.

    The meter's range is chosen here in **both** roles even though only a
    meter-DUT plan pins it: the ohms test current is a property of the range
    the meter ends up on, and it is what decides whether the 5700A's
    specification applies as written (H06).

    *two_wire_fallback* keeps the one catalogue value that has no four-wire
    mode (L92).  "Four-wire sensing is available for all resistance values
    except 100 MOhm" ([OM 1996] p.102), and the default one-connection rule
    otherwise drops the 5700A's 100 Mohm standard from every plan -- and with
    it the 34401A's own 100 Mohm verification point.  It is the largest value,
    so it lands last, as a one-point group where the operator re-cables once.
    """
    points: list[PlanPoint] = []
    for range_spec in _range_axis(source_function):
        if range_spec.fs == 0.0 and not include_zero:
            continue
        wiring, wiring_note = connection, ""
        if two_wire_fallback and not range_spec.allows(connection) \
                and range_spec.allows(Connection.TWO_WIRE) \
                and _meter_offers(meter_function, Connection.TWO_WIRE,
                                  range_spec.fs):
            wiring = Connection.TWO_WIRE
            wiring_note = (
                f"{connection.value} is not available on the "
                f"{_range_label(range_spec)} range of the calibrator, so this "
                f"one point is run two-wire at the end of the resistance run")
        range_arg = None
        adder: float | None = None
        load_note = ""
        if meter_function is not None:
            meter_range = _resistance_meter_range(meter_function,
                                                  range_spec.fs, range_spec)
            if meter_range is not None:
                if dut_is_meter:
                    range_arg = (meter_range.range_arg
                                 if meter_range.range_arg is not None
                                 else meter_range.fs)
                adder, load_note = _load_derating(range_spec,
                                                  meter_range.test_current_a,
                                                  wiring)
        points.append(PlanPoint(quantity=quantity, value=range_spec.fs,
                                connection=wiring,
                                range_id=range_spec.range_id,
                                range_arg=range_arg, load_adder=adder,
                                note=_join_notes(wiring_note, load_note)))
    return points


def generate_plan(source_spec: ModelSpec | str, meter_spec: ModelSpec | str, *,
                  dut_role: str,
                  quantities: Sequence[Quantity | str] | None = None,
                  percentages: Sequence[float] = DEFAULT_PERCENTAGES,
                  include_negative: bool = True,
                  include_zero: bool = True,
                  frequencies: Sequence[float] | str | None = None,
                  connections: Sequence[Connection | str] | None = None,
                  interval: str | None = None,
                  include_divider_ranges: bool = False,
                  reference_frequency_hz: float = DEFAULT_REFERENCE_FREQUENCY_HZ,
                  frequency_sweep_pct: float = 100.0,
                  name: str = "", description: str = "",
                  samples: int = DEFAULT_SAMPLES,
                  discards: int = DEFAULT_DISCARDS,
                  confidence: float = DEFAULT_CONFIDENCE,
                  tur_floor: float = DEFAULT_TUR_FLOOR,
                  guard_band_factor: float = DEFAULT_GUARD_FACTOR,
                  temperature_policy: str = "gate",
                  point_set: str = "full") -> TestPlan:
    """Build a plan by intersecting what the source can produce with what the
    meter can measure.

    The DUT's ``ModelSpec`` supplies the axis -- its functions, its ranges, and
    for ac its frequency bands.  Every candidate is then put to both instruments
    offline; what fails lands in :attr:`TestPlan.skipped` with the reasons.

    ``percentages`` are of each range's full scale.  A 10 %-of-range ac point
    sits exactly on the 5700A's stated minimum output (and on the 9 uA floor of
    its ac 220 uA range, which is not 10 % of anything) and **is kept**: the
    minimum is a legal output, it is the most stressing low-end point the
    calibrator can make, and dropping it would leave the bottom decade of every
    ac range untested.  ``_clean`` exists so that the arithmetic lands on the
    stated figure rather than one ulp below it.

    ``include_zero`` adds a zero to every dc range and the short to the
    resistance catalogue, ordered first within its range while the leads are
    freshly settled.  On the calibrator's own 1100 V dc range -- the one range
    with a non-zero minimum output, 100 V -- the zero is generated and then
    skipped, saying so.

    ``frequencies`` overrides the per-band selection with an explicit list,
    filtered per range to the tones that range's bands actually cover, or with
    one of :data:`FREQUENCY_PRESETS` -- ``"manufacturer"`` being the break-point
    tones the manufacturers' own verification tables use (M68).  The default is
    unchanged: one round tone near the logarithmic centre of each band.

    ``include_divider_ranges`` plans the ranges in
    :data:`DIVIDER_VERIFIED_RANGES`, which are skipped by default because the
    manufacturer verifies them through a divider rather than from a calibrator.

    ``point_set`` is one of :data:`POINT_SETS`.  With the meter under test and
    a manufacturer table for it in :data:`MANUFACTURER_TESTS`, ``"full"`` adds
    every row of that table (and its supplementary points) to the generated
    ladder, each labelled with its test number -- a row the ladder already
    contains is labelled rather than duplicated -- and ``"manufacturer"`` plans
    the table alone.  Without such a table ``"full"`` is the ladder and
    ``"manufacturer"`` is an error.
    """
    source_spec = _as_spec(source_spec)
    meter_spec = _as_spec(meter_spec)
    role = str(dut_role).strip().lower()
    if role not in DUT_ROLES:
        raise PlanError(f"dut_role must be one of {', '.join(DUT_ROLES)}, "
                        f"got {dut_role!r}")
    if source_spec.role not in (Role.SOURCE, Role.BOTH):
        raise PlanError(f"{source_spec.model_id} is a {source_spec.role.value}, "
                        f"not a source")
    if meter_spec.role not in (Role.METER, Role.BOTH):
        raise PlanError(f"{meter_spec.model_id} is a {meter_spec.role.value}, "
                        f"not a meter")
    if not percentages:
        raise PlanError("percentages must not be empty")
    if isinstance(frequencies, str) \
            and frequencies.strip().lower() not in FREQUENCY_PRESETS:
        raise PlanError(
            f"unknown frequency preset {frequencies!r} (known: "
            f"{', '.join(FREQUENCY_PRESETS)}); a list of tones is the "
            f"alternative")
    if isinstance(frequencies, str):
        frequencies = frequencies.strip().lower()
    point_set = str(point_set).strip().lower()
    if point_set not in POINT_SETS:
        raise PlanError(f"point_set must be one of {', '.join(POINT_SETS)}, "
                        f"got {point_set!r}")

    dut_is_meter = role == "meter"
    table = manufacturer_tests(meter_spec.model_id) if dut_is_meter else ()
    if point_set == "manufacturer" and not table:
        raise PlanError(
            f"there is no manufacturer verification table for "
            f"{(meter_spec if dut_is_meter else source_spec).model_id} as the "
            f"instrument under test; use point_set 'full' or 'ladder'")
    dut_spec = meter_spec if dut_is_meter else source_spec
    if interval is None:
        # The manufacturer's own verification column where it differs from the
        # file's default (L93), so a plan is judged against the claim the
        # published sheet is computed from.
        wanted = VERIFICATION_INTERVALS.get(dut_spec.model_id)
        interval = (wanted if wanted and (not dut_spec.intervals
                                          or wanted in dut_spec.intervals)
                    else dut_spec.default_interval)
    if interval is not None and dut_spec.intervals \
            and interval not in dut_spec.intervals:
        raise PlanError(
            f"{dut_spec.model_id} has no {interval!r} interval "
            f"(declared: {', '.join(dut_spec.intervals)})")
    meter_interval = interval if dut_is_meter else meter_spec.default_interval

    points: list[PlanPoint] = []
    skipped: list[SkippedPoint] = []

    def _drop(candidate: PlanPoint, *reasons: str) -> None:
        skipped.append(SkippedPoint(candidate, tuple(reasons)))

    for quantity in _resolve_quantities(quantities, source_spec, meter_spec):
        source_function = source_spec.function(quantity)
        meter_function = meter_spec.function(quantity)
        placeholder = PlanPoint(quantity=quantity, value=0.0,
                                label=f"{quantity.name} (entire function)")
        if source_function is None:
            _drop(placeholder,
                  f"{source_spec.model_id} does not source {quantity.name}")
            continue
        if meter_function is None:
            _drop(placeholder,
                  f"{meter_spec.model_id} does not measure {quantity.name}")
            continue

        usable, missing = _resolve_connections(connections, quantity,
                                               source_function, meter_function)
        for connection in missing:
            _drop(replace(placeholder, connection=connection),
                  f"{quantity.name} in {connection.value} is not offered by "
                  f"both {source_spec.model_id} and {meter_spec.model_id}")
        if not usable:
            if not missing:
                _drop(placeholder,
                      f"{source_spec.model_id} and {meter_spec.model_id} share "
                      f"no wiring for {quantity.name}")
            continue

        if point_set == "manufacturer":
            continue
        for connection in usable:
            if quantity is Quantity.RES and any(r.discrete for r
                                                in source_function.ranges):
                candidates = _resistance_candidates(
                    quantity, connection, source_function, meter_function,
                    dut_is_meter=dut_is_meter, include_zero=include_zero,
                    two_wire_fallback=connections is None)
            else:
                dut_function = meter_function if dut_is_meter else source_function
                divider = (DIVIDER_VERIFIED_RANGES.get(
                    (dut_spec.model_id, quantity.name), ())
                    if not include_divider_ranges else ())
                candidates = []
                for range_spec in _range_axis(dut_function):
                    if any(math.isclose(range_spec.fs, fs, rel_tol=1e-9)
                           for fs in divider):
                        _drop(PlanPoint(
                            quantity=quantity, value=range_spec.fs,
                            connection=connection,
                            range_arg=(range_spec.range_arg
                                       if range_spec.range_arg is not None
                                       else range_spec.fs) if dut_is_meter
                            else None,
                            label=f"{quantity.name} {_g(range_spec.fs)} "
                                  f"{quantity.unit} range (entire range)"),
                            f"{dut_spec.model_id}'s {_g(range_spec.fs)} "
                            f"{quantity.unit} {quantity.name} range is verified "
                            f"through a voltage divider, not directly from a "
                            f"calibrator, and its manufacturer marks the test "
                            f"optional; pass include_divider_ranges to plan it "
                            f"anyway")
                        continue
                    candidates.extend(_generate_range_points(
                        quantity, connection, range_spec,
                        dut_is_meter=dut_is_meter,
                        source_function=source_function,
                        meter_function=meter_function,
                        meter_interval=meter_interval,
                        percentages=percentages, include_zero=include_zero,
                        include_negative=include_negative,
                        frequencies=frequencies,
                        reference_frequency_hz=reference_frequency_hz,
                        frequency_sweep_pct=frequency_sweep_pct))
            for candidate in candidates:
                candidate = replace(candidate, interval=interval,
                                    label=_describe(candidate))
                problems = point_problems(candidate, source_spec, meter_spec,
                                          dut_role=role, interval=interval)
                if problems:
                    _drop(candidate, *problems)
                else:
                    points.append(replace(candidate, note=_join_notes(
                        spec_ratio_note(candidate, source_spec, meter_spec,
                                        dut_role=role, interval=interval),
                        candidate.note,
                        _range_lock_note(candidate, source_spec))))

    if table and point_set in ("full", "manufacturer"):
        wanted = set(_resolve_quantities(quantities, source_spec, meter_spec))
        explicit = (None if connections is None else
                    {Connection(c) for c in connections})
        points = _merge_manufacturer(
            points, _drop, [t for t in table
                            if t.quantity in wanted
                            and (explicit is None or t.connection in explicit)
                            and (point_set == "full" or not t.supplementary)
                            and _row_is_wanted(t, include_negative,
                                               include_zero, frequencies)],
            source_spec=source_spec, meter_spec=meter_spec, role=role,
            interval=interval)

    return TestPlan(dut_role=role, source_model_id=source_spec.model_id,
                    meter_model_id=meter_spec.model_id,
                    points=tuple(points), skipped=tuple(skipped),
                    name=name, description=description, interval=interval,
                    confidence=confidence, tur_floor=tur_floor,
                    guard_band_factor=guard_band_factor,
                    temperature_policy=temperature_policy,
                    samples=samples, discards=discards)


def _row_is_wanted(test: ManufacturerTest, include_negative: bool,
                   include_zero: bool, frequencies) -> bool:
    """Whether a manufacturer row survives the caller's own narrowing.

    The rows supplement the ladder inside the same constraints: a plan asked
    for with no negative setpoints, no zeros or an explicit list of tones does
    not grow points outside them.
    """
    if not include_negative and test.value < 0.0:
        return False
    if not include_zero and test.value == 0.0:
        return False
    if test.quantity.frequency_dependent and frequencies is not None \
            and not isinstance(frequencies, str):
        return any(math.isclose(test.frequency_hz, float(f), rel_tol=1e-9)
                   for f in frequencies)
    return True


def _manufacturer_candidate(test: ManufacturerTest, source_spec: ModelSpec,
                            meter_spec: ModelSpec, interval: str | None
                            ) -> tuple[PlanPoint | None, str]:
    """The plan point realising *test*, or ``(None, why it cannot be)``."""
    quantity = test.quantity
    source_function = source_spec.function(quantity)
    meter_function = meter_spec.function(quantity)
    if source_function is None:
        return None, f"{source_spec.model_id} does not source {quantity.name}"
    if meter_function is None:
        return None, f"{meter_spec.model_id} does not measure {quantity.name}"
    meter_range = _range_by_arg(meter_function, test.range_fs)
    if meter_range is None:
        return None, (f"{meter_spec.model_id} has no {_g(test.range_fs)} "
                      f"{quantity.unit} {quantity.name} range")
    range_arg = (meter_range.range_arg if meter_range.range_arg is not None
                 else meter_range.fs)
    range_id, adder, load_note = None, None, ""
    if quantity is Quantity.RES and any(r.discrete
                                        for r in source_function.ranges):
        # The calibrator's resistors are a catalogue: the point has to name
        # the one it is sourced from (see _resistance_candidates).
        source_range = next(
            (r for r in source_function.ranges
             if math.isclose(r.fs, test.value, rel_tol=1e-9, abs_tol=0.0)),
            None)
        if source_range is None:
            return None, (f"{source_spec.model_id} has no "
                          f"{_g(test.value)} ohm standard")
        range_id = source_range.range_id
        adder, load_note = _load_derating(source_range,
                                          meter_range.test_current_a,
                                          test.connection)
    point = PlanPoint(quantity=quantity, value=float(test.value),
                      frequency_hz=float(test.frequency_hz),
                      connection=test.connection, range_id=range_id,
                      range_arg=range_arg, mode=test.mode, interval=interval,
                      apply=test.apply, load_adder=adder,
                      note=_join_notes(test.note, load_note))
    return point, ""


def _merge_manufacturer(points: list[PlanPoint], drop,
                        tests: Sequence[ManufacturerTest], *,
                        source_spec: ModelSpec, meter_spec: ModelSpec,
                        role: str, interval: str | None) -> list[PlanPoint]:
    """*points* with every manufacturer row in it, labelled, in run order.

    A row the generated ladder already holds -- the same setpoint on the same
    range, tone, wiring and technique -- labels that point instead of being
    measured twice.  A row that cannot be planned goes to ``skipped`` with
    its reasons, exactly like a generated candidate.  The result is
    re-ordered by quantity, wiring and range (stably, so each range keeps the
    ladder's own order and its zero first) so the manufacturer's extra points
    run with the rest of their range rather than in a second pass.
    """
    merged = list(points)
    index = {_identity(point): i for i, point in enumerate(merged)}
    for test in tests:
        candidate, why = _manufacturer_candidate(test, source_spec,
                                                 meter_spec, interval)
        label_point = candidate if candidate is not None else PlanPoint(
            quantity=test.quantity, value=test.value,
            frequency_hz=test.frequency_hz, connection=test.connection,
            range_arg=test.range_fs, mode=test.mode)
        label = f"{test.tag} {_describe(label_point)}"
        if candidate is None:
            drop(replace(label_point, label=label), why)
            continue
        existing = index.get(_identity(candidate))
        if existing is not None:
            point = merged[existing]
            merged[existing] = replace(
                point, label=f"{test.tag} {point.label or _describe(point)}",
                note=_join_notes(point.note, test.note))
            continue
        candidate = replace(candidate, label=label)
        problems = point_problems(candidate, source_spec, meter_spec,
                                  dut_role=role, interval=interval)
        if problems:
            drop(candidate, *problems)
            continue
        merged.append(replace(candidate, note=_join_notes(
            spec_ratio_note(candidate, source_spec, meter_spec,
                            dut_role=role, interval=interval),
            candidate.note,
            _range_lock_note(candidate, source_spec))))
        index[_identity(candidate)] = len(merged) - 1

    def order(point: PlanPoint) -> tuple:
        quantity_rank = (QUANTITY_ORDER.index(point.quantity)
                         if point.quantity in QUANTITY_ORDER
                         else len(QUANTITY_ORDER))
        connection_rank = (CONNECTION_ORDER.index(point.connection)
                           if point.connection in CONNECTION_ORDER
                           else len(CONNECTION_ORDER))
        scale = point.range_arg if point.range_arg is not None else 0.0
        return (quantity_rank, connection_rank, float(scale))

    return sorted(merged, key=order)


# --------------------------------------------------------------------------
# Validation of a hand-written or hand-edited plan
# --------------------------------------------------------------------------

def validate_plan(plan: TestPlan, source_spec: ModelSpec | str,
                  meter_spec: ModelSpec | str) -> list[str]:
    """Every problem with *plan*, offline.  Empty means it is runnable.

    The point of doing this at the desk is that the alternative is doing it
    three hours into a run with the instrument warm.  The per-point rules are
    the same ones :func:`generate_plan` built from, so a hand-edited plan is
    held to the same standard as a generated one.
    """
    source_spec = _as_spec(source_spec)
    meter_spec = _as_spec(meter_spec)
    problems: list[str] = []

    if plan.source_model_id != source_spec.model_id:
        problems.append(
            f"plan names source {plan.source_model_id!r} but the specification "
            f"given is {source_spec.model_id!r}")
    if plan.meter_model_id != meter_spec.model_id:
        problems.append(
            f"plan names meter {plan.meter_model_id!r} but the specification "
            f"given is {meter_spec.model_id!r}")
    if source_spec.role not in (Role.SOURCE, Role.BOTH):
        problems.append(f"{source_spec.model_id} is a {source_spec.role.value}, "
                        f"not a source")
    if meter_spec.role not in (Role.METER, Role.BOTH):
        problems.append(f"{meter_spec.model_id} is a {meter_spec.role.value}, "
                        f"not a meter")

    dut_spec = meter_spec if plan.dut_is_meter else source_spec
    if plan.interval is not None and dut_spec.intervals \
            and plan.interval not in dut_spec.intervals:
        problems.append(
            f"interval {plan.interval!r} is not declared by "
            f"{dut_spec.model_id} (declared: {', '.join(dut_spec.intervals)})")
    if not 0.0 < plan.confidence < 1.0:
        problems.append(
            f"confidence must be a fraction in (0, 1), got {plan.confidence!r}")
    if plan.tur_floor < 0.0:
        problems.append(f"tur_floor must not be negative, got {plan.tur_floor!r}")
    if plan.guard_band_factor < 0.0:
        problems.append(f"guard_band_factor must not be negative, got "
                        f"{plan.guard_band_factor!r}")
    if plan.samples < 1:
        problems.append(f"samples must be at least 1, got {plan.samples!r}")
    if plan.discards < 0:
        problems.append(f"discards must not be negative, got {plan.discards!r}")
    if not plan.points:
        problems.append("the plan has no points")

    for index, point in enumerate(plan.points):
        if plan.samples_for(point) < 1:
            problems.append(f"points[{index}] ({point.describe()}): samples "
                            f"must be at least 1")
        if plan.discards_for(point) < 0:
            problems.append(f"points[{index}] ({point.describe()}): discards "
                            f"must not be negative")
        for message in point_problems(point, source_spec, meter_spec,
                                      dut_role=plan.dut_role,
                                      interval=plan.interval):
            problems.append(f"points[{index}] ({point.describe()}): {message}")

    # `skipped` is what the report tells the operator was left out.  A hand edit
    # that moves a point back into `points` without deleting its skipped entry
    # produces a run that measures a point and a report that says it did not.
    planned = {_identity(p) for p in plan.points}
    for index, entry in enumerate(plan.skipped):
        if _identity(entry.point) in planned:
            problems.append(
                f"skipped[{index}] ({entry.point.describe()}): this point is "
                f"also in points; a plan cannot both run it and report it as "
                f"dropped")
    return problems


# --------------------------------------------------------------------------
# YAML round trip
# --------------------------------------------------------------------------

class _PlanLoader(yaml.SafeLoader):
    """``SafeLoader`` with a YAML 1.2 float resolver.

    Same fix, and for the same reason, as ``specs._SpecLoader``: PyYAML follows
    YAML 1.1, whose float resolver demands a signed exponent, so a hand-edited
    ``settle_s: 1e-3`` loads as the *string* ``"1e-3"`` and fails much later, or
    worse compares as a string.  A plan file is meant to be hand-edited, which
    makes this the likeliest single mistake a plan can contain.
    """


_YAML_12_FLOAT = re.compile(r"""^(?:
     [-+]?(?:\.[0-9]+|[0-9][0-9_]*(?:\.[0-9_]*)?)(?:[eE][-+]?[0-9]+)?
    |[-+]?\.(?:inf|Inf|INF)
    |\.(?:nan|NaN|NAN))$""", re.VERBOSE)

_PlanLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float", _YAML_12_FLOAT, list("-+0123456789."))


_POINT_KEYS = ("quantity", "value", "frequency_hz", "connection", "range_id",
               "range_arg", "mode", "interval", "apply", "settle_s", "label",
               "samples", "discards", "note", "load_adder")
_SKIPPED_KEYS = ("point", "reasons")
_PLAN_KEYS = ("schema_version", "name", "description", "dut_role",
              "source_model_id", "meter_model_id", "interval", "confidence",
              "tur_floor", "guard_band_factor", "temperature_policy",
              "samples", "discards", "points", "skipped")
_REQUIRED_PLAN_KEYS = ("dut_role", "source_model_id", "meter_model_id",
                       "points")


def _fail(source: str, path: str, message: str):
    raise PlanError(f"{source}: {path}: {message}")


def _mapping(source: str, node: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(node, Mapping):
        _fail(source, path, f"expected a mapping, got {type(node).__name__}")
    for key in node:
        if not isinstance(key, str):
            _fail(source, path, f"key {key!r} is not a string")
    return node


def _sequence(source: str, node: Any, path: str) -> Sequence[Any]:
    if isinstance(node, (str, bytes)) or not isinstance(node, Sequence):
        _fail(source, path, f"expected a list, got {type(node).__name__}")
    return node


def _check_keys(source: str, node: Mapping[str, Any], path: str,
                allowed: Sequence[str], required: Sequence[str] = ()) -> None:
    allowed_set = set(allowed)
    for key in node:
        if key not in allowed_set:
            _fail(source, path, f"unknown key {key!r} "
                                f"(allowed: {', '.join(sorted(allowed_set))})")
    for key in required:
        if key not in node:
            _fail(source, path, f"missing required key {key!r}")


def _require(source: str, node: Mapping[str, Any], key: str, path: str) -> None:
    """Reject an explicit ``null`` where a value is mandatory.

    ``_check_keys`` only tests that the key is *present*, so ``value: null``
    walks straight through it and lands as ``None`` on the point -- where
    ``float(None)`` raises a ``TypeError`` from inside ``validate_plan``,
    hundreds of lines from the line of YAML that caused it.  A plan file is
    meant to be hand-edited; the error has to name the field.
    """
    if key in node and node[key] is None:
        _fail(source, path, f"{key!r} must not be null")


def _number(source: str, node: Mapping[str, Any], key: str, path: str,
            default=None, *, required: bool = False):
    if required:
        _require(source, node, key, path)
    if key not in node or node[key] is None:
        return default
    raw = node[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        _fail(source, path, f"{key!r} must be a number, got "
                            f"{type(raw).__name__} ({raw!r})")
    return float(raw)


def _integer(source: str, node: Mapping[str, Any], key: str, path: str,
             default=None):
    if key not in node or node[key] is None:
        return default
    raw = node[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        _fail(source, path, f"{key!r} must be an integer, got "
                            f"{type(raw).__name__} ({raw!r})")
    return int(raw)


def _text(source: str, node: Mapping[str, Any], key: str, path: str,
          default=None, *, required: bool = False):
    if required:
        _require(source, node, key, path)
    if key not in node or node[key] is None:
        return default
    raw = node[key]
    if not isinstance(raw, str):
        _fail(source, path, f"{key!r} must be a string, got "
                            f"{type(raw).__name__} ({raw!r})")
    return raw


def _point_to_dict(point: PlanPoint) -> dict:
    out: dict[str, Any] = {"quantity": point.quantity.name,
                           "value": float(point.value)}
    if point.frequency_hz:
        out["frequency_hz"] = float(point.frequency_hz)
    out["connection"] = point.connection.value
    for key in ("range_id", "mode", "interval"):
        value = getattr(point, key)
        if value:
            out[key] = value
    if point.range_arg is not None:
        out["range_arg"] = float(point.range_arg)
    if point.apply:
        out["apply"] = dict(point.apply)
    if point.settle_s is not None:
        out["settle_s"] = float(point.settle_s)
    if point.label:
        out["label"] = point.label
    if point.samples is not None:
        out["samples"] = int(point.samples)
    if point.discards is not None:
        out["discards"] = int(point.discards)
    if point.note:
        out["note"] = point.note
    if point.load_adder is not None:
        out["load_adder"] = float(point.load_adder)
    return out


def _point_from_dict(source: str, node: Any, path: str) -> PlanPoint:
    node = _mapping(source, node, path)
    _check_keys(source, node, path, _POINT_KEYS, required=("quantity", "value"))
    try:
        quantity = Quantity(node["quantity"])
    except ValueError:
        _fail(source, f"{path}.quantity",
              f"unknown quantity {node['quantity']!r} "
              f"(known: {', '.join(q.name for q in Quantity)})")
    raw_connection = node.get("connection", Connection.TWO_WIRE.value)
    try:
        connection = Connection(raw_connection)
    except ValueError:
        _fail(source, f"{path}.connection",
              f"unknown connection {raw_connection!r} "
              f"(known: {', '.join(c.value for c in Connection)})")
    apply: Mapping[str, Any] = _EMPTY_MAP
    if node.get("apply") is not None:
        knobs = _mapping(source, node["apply"], f"{path}.apply")
        _check_keys(source, knobs, f"{path}.apply", tuple(APPLY_KNOBS))
        for key, value in knobs.items():
            types = APPLY_KNOBS[key]
            if bool not in types and isinstance(value, bool):
                _fail(source, f"{path}.apply", f"{key!r} must not be a boolean")
            if not isinstance(value, types):
                wanted = "/".join(t.__name__ for t in types)
                _fail(source, f"{path}.apply",
                      f"{key!r} must be {wanted}, got {type(value).__name__}")
        apply = MappingProxyType(dict(knobs))
    return PlanPoint(
        quantity=quantity,
        value=_number(source, node, "value", path, required=True),
        frequency_hz=_number(source, node, "frequency_hz", path, 0.0),
        connection=connection,
        range_id=_text(source, node, "range_id", path),
        range_arg=_number(source, node, "range_arg", path),
        mode=_text(source, node, "mode", path),
        interval=_text(source, node, "interval", path),
        apply=apply,
        settle_s=_number(source, node, "settle_s", path),
        label=_text(source, node, "label", path, ""),
        samples=_integer(source, node, "samples", path),
        discards=_integer(source, node, "discards", path),
        note=_text(source, node, "note", path, ""),
        load_adder=_number(source, node, "load_adder", path),
    )


def plan_to_dict(plan: TestPlan) -> dict:
    """A plain-data form of *plan*, ready for ``yaml.safe_dump``.

    ``skipped`` is included.  It is not derivable from the plan that was saved
    -- the candidates are gone by then -- and it is the record of what the
    operator asked for and did not get, so dropping it on the way to disk would
    reintroduce the silent-shortening this module exists to prevent.
    """
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    if plan.name:
        out["name"] = plan.name
    if plan.description:
        out["description"] = plan.description
    out["dut_role"] = plan.dut_role
    out["source_model_id"] = plan.source_model_id
    out["meter_model_id"] = plan.meter_model_id
    if plan.interval is not None:
        out["interval"] = plan.interval
    out["confidence"] = float(plan.confidence)
    out["tur_floor"] = float(plan.tur_floor)
    out["guard_band_factor"] = float(plan.guard_band_factor)
    out["temperature_policy"] = plan.temperature_policy
    out["samples"] = int(plan.samples)
    out["discards"] = int(plan.discards)
    out["points"] = [_point_to_dict(p) for p in plan.points]
    if plan.skipped:
        out["skipped"] = [{"point": _point_to_dict(s.point),
                           "reasons": list(s.reasons)} for s in plan.skipped]
    return out


def plan_from_dict(data: Any, source: str = "<dict>") -> TestPlan:
    """Parse a plain-data plan, strictly.

    Unknown keys are rejected by name and path.  A plan is meant to be
    hand-edited, and a typo that loads as "no override" is how a run silently
    does something other than what the file says.
    """
    root = _mapping(source, data, "<root>")
    _check_keys(source, root, "<root>", _PLAN_KEYS, required=_REQUIRED_PLAN_KEYS)

    version = _number(source, root, "schema_version", "<root>",
                      float(SCHEMA_VERSION))
    if version != SCHEMA_VERSION:
        _fail(source, "<root>.schema_version",
              f"unsupported schema_version {version:g}; this build understands "
              f"{SCHEMA_VERSION}")

    role = _text(source, root, "dut_role", "<root>", required=True)
    if role not in DUT_ROLES:
        _fail(source, "<root>.dut_role",
              f"must be one of {', '.join(DUT_ROLES)}, got {role!r}")
    policy = _text(source, root, "temperature_policy", "<root>", "gate")
    if policy not in TEMPERATURE_POLICIES:
        _fail(source, "<root>.temperature_policy",
              f"must be one of {', '.join(TEMPERATURE_POLICIES)}, got {policy!r}")

    points = [_point_from_dict(source, node, f"points[{index}]")
              for index, node in
              enumerate(_sequence(source, root["points"], "<root>.points"))]

    skipped: list[SkippedPoint] = []
    for index, node in enumerate(_sequence(source, root.get("skipped", []),
                                           "<root>.skipped")):
        path = f"skipped[{index}]"
        node = _mapping(source, node, path)
        _check_keys(source, node, path, _SKIPPED_KEYS, required=("point",))
        reasons = _sequence(source, node.get("reasons", []), f"{path}.reasons")
        for position, reason in enumerate(reasons):
            if not isinstance(reason, str):
                _fail(source, f"{path}.reasons[{position}]",
                      f"expected a string, got {type(reason).__name__}")
        skipped.append(SkippedPoint(
            _point_from_dict(source, node["point"], f"{path}.point"),
            tuple(reasons)))

    return TestPlan(
        dut_role=role,
        source_model_id=_text(source, root, "source_model_id", "<root>",
                              required=True),
        meter_model_id=_text(source, root, "meter_model_id", "<root>",
                             required=True),
        points=tuple(points),
        skipped=tuple(skipped),
        name=_text(source, root, "name", "<root>", ""),
        description=_text(source, root, "description", "<root>", ""),
        interval=_text(source, root, "interval", "<root>"),
        confidence=_number(source, root, "confidence", "<root>",
                           DEFAULT_CONFIDENCE),
        tur_floor=_number(source, root, "tur_floor", "<root>",
                          DEFAULT_TUR_FLOOR),
        guard_band_factor=_number(source, root, "guard_band_factor", "<root>",
                                  DEFAULT_GUARD_FACTOR),
        temperature_policy=policy,
        samples=_integer(source, root, "samples", "<root>", DEFAULT_SAMPLES),
        discards=_integer(source, root, "discards", "<root>", DEFAULT_DISCARDS),
    )


def load_plan(path) -> TestPlan:
    """Load a plan from a YAML file, strictly."""
    with open(path, "r", encoding="utf-8") as handle:
        try:
            data = yaml.load(handle, Loader=_PlanLoader)
        except yaml.YAMLError as exc:
            raise PlanError(f"{path}: not valid YAML: {exc}") from exc
    return plan_from_dict(data, str(path))


def save_plan(plan: TestPlan, path) -> None:
    """Write *plan* to a YAML file that loads back equal to it."""
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(plan_to_dict(plan), handle, sort_keys=False,
                       default_flow_style=False, allow_unicode=True)

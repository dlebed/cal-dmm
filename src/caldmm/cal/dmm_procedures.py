"""Guided *external adjustment* procedures for the bench DMMs.

Built on :mod:`caldmm.cal.procedure`, which owns the framework: the step
model, the commit gate, the three-valued NVRAM answer, the audit trail and the
runner.  **Nothing here re-implements any of it.**  This module adds one
procedure and registers it, exactly as the three Fluke 5700A procedures in that
module do.

``CAL`` is not ``ACAL``
----------------------

The distinction is the whole reason this module is worth writing carefully.

* **``ACAL``** is *auto-calibration*: the 3458A measuring its own internal 7 V
  and 40 kΩ references and recomputing ~197 of its 253 calibration constants
  from them (Calibration Manual, "Monitoring Calibration Constants", p.25).  It
  needs no external equipment, is run daily, and **cannot make the meter more
  accurate than its own internal references already are**.
* **``CAL``** is *external adjustment*: it retrains those internal references
  against a traceable standard physically on the terminals.  It is the only one
  of the two that can move the meter's absolute accuracy, and it writes
  non-volatile calibration memory irreversibly.

This module drives the second one.  Every prompt says so.

What gates it
-------------

Two things, and **neither can be queried remotely**:

1. the **security code** set with ``SECURE`` (Calibration Manual p.21).  The
   factory value is documented inconsistently -- ``3458`` in the Calibration
   Manual p.21, ``3458A`` in the User's Guide -- so **nothing is hard-coded**
   and the operator is asked;
2. internal jumper **J132** on the 03458-66505 (or -66515, option 001)
   assembly (Calibration Manual p.23, "Hardware lock-out of calibration").
   With J132 in the right position *neither* ``CAL`` nor ``ACAL`` will run even
   with the correct security code, and clearing it means **powering the meter
   down and removing the top cover**.

**The 3458A has no rear-panel calibration switch.**  The instrument in this
project that does have one is the Fluke 5700A.  ``hp3458a``'s
``CalibrationLockoutError`` already carries the whole J132 explanation, so this
module deliberately lets it propagate **unwrapped** -- see
:meth:`HP3458AExternalAdjust.adjust_step`.

Import hygiene
--------------

Stdlib and :mod:`caldmm.cal.procedure` only, at module scope.  The device is
duck-typed exactly as the framework's is, so this module imports on a machine
with no linux-gpib.

There is **one** exception, and it is the reason ``cal``'s import rule names
two modules rather than one: :meth:`HP3458AExternalAdjust._acal_ac_step`'s
action imports ``ACAL``, ``ERR_HARDWARE`` and the two error-bit decoders from
:mod:`caldmm.drivers.hp3458a` **inside the function**, the same pattern
:mod:`caldmm.cal.adapters` uses for its ``open`` methods.  That step is the
one place here that sends a command with ``cmd()`` instead of through a driver
method, so it has to decode ``ERR?``/``AUXERR?`` itself.  Everything needed
*before* I/O -- the ``CAL`` bands, the security-code shape -- is mirrored as a
constant below instead, so no guard ever depends on a driver being importable.
``tests/cal/test_dmm_procedures.py`` pins this with a ``sys.meta_path``
blocker: hoisting that import to module scope fails the suite.

Sourcing
--------

Every step names the section of the *Keysight 3458A Calibration Manual*
(03458-90017, edition 11) it comes from.  **Anything the manual does not state
is either left out or labelled as this project's judgement**, never presented
as documented: a guessed calibration step writes wrong constants into a meter
that cannot be un-adjusted.  The two deliberate omissions are recorded in
:data:`NOT_IMPLEMENTED_NOTES`.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator, Sequence

from . import procedure
from .procedure import (
    AdjustStep,
    ExternalStandard,
    ProcedureContext,
    ProcedureError,
    ProcedureResult,
    SafetyError,
    SetPoint,
    StandardSpec,
    StandardValueError,
    StepOrderError,
    StepOutcome,
    StepResult,
    ask,
    prompt_standards,
    register,
)
from .quantities import Connection, Quantity
from .roles import Point

__all__ = [
    "ACAL_AC_SECONDS",
    "ACAL_STALE_HOURS",
    "ADJUSTMENT_ORDER",
    "AMBIENT_MAX_C",
    "AMBIENT_MIN_C",
    "CAL_RESISTANCE_MAX",
    "CAL_RESISTANCE_MIN",
    "CAL_VOLTAGE_MAX",
    "CAL_VOLTAGE_MIN",
    "GAIN_CHECK_FRACTION",
    "HP3458A_STANDARDS",
    "HP3458AExternalAdjust",
    "NOT_IMPLEMENTED_NOTES",
    "OHMS_DELAY_LONG_S",
    "OHMS_DELAY_SHORT_S",
    "OHMS_DELAY_SIGNIFICANT_PPM",
    "PROCEDURE_ID",
    "ProcedureNotImplementedError",
    "SECURITY_CODE_RE",
    "SHORT_MAX_V",
    "SOURCED_POINT_10KOHM",
    "SOURCED_POINT_10V",
    "STANDARD_10KOHM",
    "STANDARD_10V",
    "WARMUP_HOURS",
    "WiringError",
    "parse_ambient_c",
    "parse_hours",
    "parse_security_code",
    "require_in_band",
    "require_not_freerunning",
    "require_valid_security_code",
    "require_write_enabled",
    "sourced_envelope_problems",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class WiringError(ProcedureError):
    """A trial reading says the wiring or the entered value is wrong.

    **Deliberately not a** :class:`~caldmm.cal.procedure.SafetyError`, even
    though it is a refusal, and the distinction is the whole point of this
    class.  ``SafetyError`` is in
    :data:`~caldmm.cal.procedure.UNSENT_GUARDS`, which is documented as
    "failures raised by guards that run *before* any I/O, **by
    construction**".  The checks that raise this one cannot: proving the short
    is fitted means selecting a function, a range and *taking a reading*
    first.  Filing those commands as ``skipped_commands`` would have the
    record assert the meter's function, range, ``OCOMP`` and ``DELAY`` were
    left alone when they had just been changed.

    Raising an unlisted type instead gives ``classify_failure`` the truth on
    both questions -- the commands went out, and (because every step that
    raises this has ``writes_nvram=False``) non-volatile memory is
    known-untouched.

    It is raised rather than returned as a ``FAILED``
    :class:`~caldmm.cal.procedure.StepResult` because
    :func:`~caldmm.cal.procedure.run_procedure` does not inspect a returned
    step's outcome: returning would let the run walk on to the ``CAL`` that
    this check exists to prevent, and ask the operator to type the commit
    confirmation for it first.
    """


class ProcedureNotImplementedError(ProcedureError):
    """A model is registered but its sequence has not been transcribed.

    A registry entry that raises this is **better than no entry at all**: an
    operator who asks for an adjustment learns *why* there is none and what
    would fix it, instead of "no procedure for model 'hp34401a'".
    """


# --------------------------------------------------------------------------
# The procedure id every model in this module registers under
# --------------------------------------------------------------------------

#: Deliberately not ``"adjust"`` or ``"cal"``.  ``external-adjust`` names the
#: thing that distinguishes it from ``ACAL``, and the distinction is the one a
#: reader most often gets wrong.
PROCEDURE_ID = "external-adjust"

#: The model file this procedure's points are read against.  §8's proof level
#: is raised to what *this* meter can read to +/-10 % (the controller's
#: amendment of 2026-08-22), so the rule needs a model id at the proof as well
#: as on the procedure class.
MODEL_ID = "hp3458a"


# --------------------------------------------------------------------------
# Constants mirrored from the driver
# --------------------------------------------------------------------------
#
# This module must not import ``caldmm.drivers.hp3458a`` at module scope (see
# the module docstring), so the numbers it has to guard on before any I/O are
# repeated here.  The driver remains the authority -- it enforces the same
# bands in ``adjustment_kind()`` -- but a *procedure* has to refuse first, and
# with a guard type ``classify_failure`` recognises as "never sent": a plain
# ``ValueError`` raised inside ``adjust()`` before transmission is recorded as
# though the command had gone out.

#: Legal ``CAL`` argument bands, Calibration Manual p.87-89 and ch.3:
#: ``CAL 0`` offset, ``CAL 1..12`` V dc gain, ``CAL 1e3..12e3`` Ω resistance
#: and dc current.  Mirrors ``hp3458a.CAL_VOLTAGE_MIN`` and friends.
CAL_VOLTAGE_MIN = 1.0
CAL_VOLTAGE_MAX = 12.0
CAL_RESISTANCE_MIN = 1.0e3
CAL_RESISTANCE_MAX = 12.0e3

#: What a ``SECURE`` code may contain.  Mirrors ``hp3458a._SECURITY_CODE_RE``.
#: Repeated here rather than imported because the driver's own guard only
#: covers commands the driver assembles, and the ``ACAL AC,<code>`` this module
#: sends with ``cmd()`` is not one of them -- see :func:`parse_security_code`.
SECURITY_CODE_RE = re.compile(r"^-?[A-Za-z0-9]+$")

#: What a Ctrl-C on a 3458A ``CAL``/``ACAL`` must say (M42).  There is no
#: documented remote abort and no front-panel STBY key, so the step is not
#: :attr:`~caldmm.cal.procedure.AdjustStep.abortable`; what the framework's
#: generic message must **not** do is send the operator to the line switch.
#: Calibration Manual p.87: "If power is turned off or the Reset button is
#: pushed during an ACAL, an error is generated. You must perform an ACAL ALL
#: to recalculate new calibration constants."
ABORT_HINT_3458A = (
    "A 3458A has no STBY key and no remote abort: let the command finish. It "
    "is written either way. Do NOT cut power or press Reset — Calibration "
    "Manual p.87: an interrupted autocal leaves the constants invalid and "
    "costs an ACAL ALL (16 min, plus a 30 min settle) to recover.")


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------

#: Calibration Manual ch.3, "Preliminary Adjustment Procedure" step 6: four
#: hours of warm-up **from the time power was applied**.  The 3458A has no
#: equivalent of the 5700A's ``ONTIME?``, so this cannot be verified over the
#: bus -- it is an operator acknowledgement in the ``brief`` step and nothing
#: else, and the report says so.
WARMUP_HOURS = 4.0

#: Calibration Manual ch.3, "Preliminary Adjustment Procedure" step 1: "The
#: temperature of the adjustment environment should be between 15 °C and
#: 30 °C."  Note this is **tighter than the 5700A's** 15-35 °C band, which is
#: why this module parses its own ambient rather than reusing
#: :func:`caldmm.cal.procedure.parse_temperature`.
AMBIENT_MIN_C = 15.0
AMBIENT_MAX_C = 30.0

#: Every 3458A accuracy column other than 24-hour is footnoted "within 24 hours
#: and ±1 °C of last ACAL" (data sheet; see ``cal/data/hp3458a.yaml``'s
#: ``interval_basis: since_acal``).  **Chapter 3 does not require a recent ACAL
#: before adjusting**, so this is a *warning* and never a blocking check: it
#: exists because the readings the setup steps take as wiring checks are only
#: as meaningful as the ACAL behind them.  The meter cannot report it, so the
#: operator is asked.
ACAL_STALE_HOURS = 24.0

#: Calibration Manual p.87: "The approximate time required to perform each
#: autocal routine … AC: 2 minutes and 45 seconds."  Mirrors
#: ``hp3458a.ACAL.AC.duration``; a test pins the two together, because the
#: ``ACAL AC`` step reads ``ERR?`` the moment the wait is over and waiting
#: short would query a meter that is still working.
#:
#: (p.45 step 7 of the adjustment procedure says "about 2 minutes" — the
#: manual contradicting itself.  p.87's table is the specific claim and the
#: longer one.)
ACAL_AC_SECONDS = 2 * 60 + 45


# --------------------------------------------------------------------------
# Gross-error thresholds -- THIS PROJECT'S JUDGEMENT, NOT THE MANUAL'S
# --------------------------------------------------------------------------
#
# The Calibration Manual states none of the three numbers below.  They exist
# for one purpose: catching a *wiring or typing* mistake before an irreversible
# command, in the same spirit as ``ExternalStandard``'s decade-error gate.
# None of them is a specification and none should ever be read as one.

#: A reading through the four-terminal short must be under this.  A missing or
#: badly made short reads orders of magnitude higher on the 100 mV range, and
#: ``CAL 0`` would then store that as the meter's offset.
SHORT_MAX_V = 1.0e-3

#: How far a trial reading may sit from the standard's certified value before
#: the procedure refuses the adjustment: 1 %.  Generous by four orders of
#: magnitude against any 3458A that is merely out of calibration, and tight
#: enough to catch the mistake that matters -- ``1.0000`` typed for a 10 V
#: standard, or ``1E3`` for a 10 kΩ one, both of which are **inside** the
#: driver's legal ``CAL`` bands and would be accepted by the meter.
GAIN_CHECK_FRACTION = 0.01

#: Calibration Manual p.44 NOTE: measure the resistance standard at the 50 ms
#: default delay and again "using a much longer delay (e.g., 1 second)"; if the
#: two differ significantly, adjust with the longer delay.  The manual does not
#: say what "significantly" is -- **10 ppm is this project's choice** -- but the
#: direction it points is unambiguous, so exceeding it selects the long delay.
OHMS_DELAY_SHORT_S = 0.050
OHMS_DELAY_LONG_S = 1.000
OHMS_DELAY_SIGNIFICANT_PPM = 10.0


# --------------------------------------------------------------------------
# The external standards
# --------------------------------------------------------------------------

#: Calibration Manual ch.3 "Required Equipment" (p.37): a 10 V dc standard
#: (Fluke 732A or equivalent) for the DC Gain Adjustment.
#:
#: ``max_deviation_ppm`` stays at the framework's 1000 ppm default and that is
#: load-bearing here: p.88 permits any value from 1 V to 12 V, so a ``1.0000``
#: typed for a ``10.0000`` standard is a **legal** ``CAL`` argument the meter
#: would accept, and the only thing that catches it is this gate.
STANDARD_10V = StandardSpec(
    key="10V", name="10 V dc reference", nominal=10.0, unit="V",
    recommended="Fluke 732A/732B", recommended_uncertainty_ppm=1.5)

#: Calibration Manual ch.3 "Required Equipment" (p.37): a 10 kΩ resistance
#: standard (Fluke 742A-10k or equivalent) for the Resistance and DC Current
#: Adjustment.  Declared in **ohms**, because that is what ``CAL`` takes
#: (p.89's example is ``CAL 10000.001``); a certificate written in kΩ is
#: accepted at the prompt as ``10.003 kohm``.
STANDARD_10KOHM = StandardSpec(
    key="10KOHM", name="10 kΩ resistance standard", nominal=10000.0,
    unit="OHM", recommended="Fluke 742A-10k",
    recommended_uncertainty_ppm=4.0)

HP3458A_STANDARDS: tuple[StandardSpec, ...] = (STANDARD_10V, STANDARD_10KOHM)


# --------------------------------------------------------------------------
# The sourced mode's two points
# --------------------------------------------------------------------------
#
# With ``--source`` a calibrator stands in for the two gain artefacts, and
# these are what it is asked to produce: the same 10 V and 10 kΩ chapter 3
# recommends.  The value each ``CAL`` receives is the calibrator's **readback**
# (:attr:`~caldmm.cal.roles.SourceState.actual_value`), never these nominals
# -- on the 5700A the resistance readback legitimately sits up to 500 ppm from
# the nominal, and sending the nominal would fold that characterization
# straight into the meter's gain constants.
#
# The offset adjustments have no sourced equivalent **on purpose**: chapter 3
# wants a four-terminal *low-thermal* short, and the calibrator's 0 Ω reached
# through two pairs of leads and its output relays is not one.  The short
# steps are identical in both modes.

#: The dc gain point.  Two-wire, like the Figure 3-2 artefact connection.
SOURCED_POINT_10V = Point(quantity=Quantity.DCV, value=10.0,
                          label="sourced 10 V dc gain point")

#: The resistance point.  **Four-wire**, so the source adapter asserts remote
#: sense (``EXTSENSE`` on a 5700A) -- the meter is on OHMF and a two-wire
#: sourcing would put the lead resistance into an irreversible ``CAL``.
SOURCED_POINT_10KOHM = Point(quantity=Quantity.RES, value=10000.0,
                             connection=Connection.FOUR_WIRE,
                             label="sourced 10 kΩ resistance point")


def sourced_envelope_problems(source: Any) -> list[str]:
    """Every reason the calibrator cannot stand in for the artefacts, offline.

    The 3458A counterpart of the 34401A module's ``envelope_problems``: run
    in preflight, before anything is opened or energized, because finding out
    at the resistance step that the calibrator cannot source 10 kΩ four-wire
    would leave a meter with its dc gain adjusted and its ohms not.
    """
    problems: list[str] = []
    for point in (SOURCED_POINT_10V, SOURCED_POINT_10KOHM):
        for reason in source.validate(point):
            problems.append(
                f"{point.label}: the calibrator refuses it — {reason}")
    return problems


# --------------------------------------------------------------------------
# The mandatory order
# --------------------------------------------------------------------------

#: Calibration Manual ch.3 Introduction: "You must perform the adjustments in
#: the order presented in this chapter", numbered 1-5; and p.43 states the
#: dependency explicitly -- "The DC Gain Adjustment must be performed prior to
#: this adjustment because this adjustment relies on the values calculated by
#: the DC Gain Adjustment."
#:
#: ``(step id, the ids that must have completed first)``.  Enforced **by the
#: procedure, before the wire**, because unlike the 5700A there is no
#: documented "steps out of order" fault to catch it afterwards: a 3458A asked
#: to adjust resistance without a DC gain adjustment simply computes and stores
#: constants from stale gains.
ADJUSTMENT_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("offset-front", ("short-front",)),
    ("offset-rear", ("offset-front", "short-rear")),
    ("dc-gain", ("offset-front", "offset-rear", "standard-10v")),
    ("resistance", ("dc-gain", "standard-10kohm")),
    ("acal-ac", ("resistance",)),
)

_ORDER_BY_ID = dict(ADJUSTMENT_ORDER)


#: What this procedure does **not** do, and why.  Reported rather than guessed.
NOT_IMPLEMENTED_NOTES: tuple[str, ...] = (
    "The AC Adjustment (Calibration Manual ch.3 item 5, p.46-50, the SCAL "
    "commands) is not implemented. It needs a synthesizer and three thermal "
    "converters, it is a manual characterization loop in which the operator "
    "reads voltages off the meter and writes them on a paper Adjustment "
    "Record, and the driver has no SCAL method. The manual requires it only "
    "once every 2 years or after the 03458-60502/03458-66502/03458-66503 "
    "assembly is repaired or replaced.",
    "The Adjustment Record form (p.51) carries a line 'Perform an ACAL OHMS' "
    "above numbered item 1, but ch.3's numbered procedure text does not "
    "mention it anywhere. That is ambiguous, so this procedure does NOT send "
    "it -- run it by hand first if your lab's practice requires it.",
)


# --------------------------------------------------------------------------
# Operator entry
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _split_number(text: str) -> tuple[float, str]:
    stripped = str(text).strip().replace(",", "")
    match = _NUMBER.match(stripped)
    if match is None:
        raise StandardValueError(f"{text!r} does not start with a number")
    return float(match.group()), stripped[match.end():].strip()


def parse_ambient_c(text: str) -> float:
    """Parse an ambient temperature and enforce the 3458A's 15-30 °C band.

    Separate from :func:`caldmm.cal.procedure.parse_temperature` on purpose:
    that one enforces the 5700A's 15-35 °C band and cites the calibrator's
    guide, and quoting the wrong instrument's band in a refusal is how an
    operator ends up adjusting a 3458A at 33 °C believing it was in
    specification.
    """
    value, unit = _split_number(text)
    token = unit.upper().lstrip("°").strip()
    if token not in ("", "C", "CELSIUS", "DEGC"):
        raise StandardValueError(
            f"ambient temperature {text!r}: give °C, not {unit!r}")
    if not AMBIENT_MIN_C <= value <= AMBIENT_MAX_C:
        raise StandardValueError(
            f"ambient {value:g} °C is outside the {AMBIENT_MIN_C:g}-"
            f"{AMBIENT_MAX_C:g} °C band the 3458A's adjustment is specified "
            f"over (Calibration Manual ch.3, Preliminary Adjustment Procedure "
            f"step 1). Constants computed outside it are not covered by any "
            f"specification, and they are written irreversibly.")
    return value


def parse_security_code(text: str) -> str | None:
    """Validate a typed ``SECURE`` code.  Blank means "security is disabled".

    Mirrors ``hp3458a._SECURITY_CODE_RE``, the same way the ``CAL`` value bands
    are mirrored above and for a stronger reason: **the driver's guard does not
    cover every command this module sends.**  ``ACAL AC,<code>`` is assembled
    here and written with ``cmd()``, which transmits verbatim, so an unvalidated
    code containing a newline or a semicolon would append a second command to
    an irreversible calibration write -- exactly the vector the driver's regex
    exists to close, on the one path it cannot see.

    Validating *at the prompt* also matters for a second reason.  If a bad code
    reached ``adjust()``, the driver would raise a plain ``ValueError`` before
    any I/O -- and ``ValueError`` is not in
    :data:`caldmm.cal.procedure.UNSENT_GUARDS`, so the run would be recorded
    as "``CAL`` sent, NVRAM state unknown" and the operator told to treat the
    meter as possibly adjusted, over a typo.
    """
    token = str(text).strip()
    if not token:
        return None
    if not SECURITY_CODE_RE.match(token):
        raise StandardValueError(
            "the calibration security code must be alphanumeric (optionally "
            "with a leading '-'), with no whitespace, commas or semicolons. "
            "The 3458A accepts several commands on one line, so a code "
            "carrying one of those could append a second command to the CAL "
            "that writes calibration memory. The offending value is "
            "deliberately not echoed here.")
    return token


def parse_hours(text: str) -> float | None:
    """Parse "how many hours ago" from the operator.  Blank means unknown."""
    stripped = str(text).strip()
    if not stripped:
        return None
    value, unit = _split_number(stripped)
    token = unit.upper().rstrip(".").strip()
    if token in ("", "H", "HR", "HRS", "HOUR", "HOURS"):
        hours = value
    elif token in ("M", "MIN", "MINS", "MINUTE", "MINUTES"):
        hours = value / 60.0
    elif token in ("D", "DAY", "DAYS"):
        hours = value * 24.0
    else:
        raise StandardValueError(
            f"{text!r}: give hours, minutes or days (e.g. '3 h', '90 min', "
            f"'2 d'), not {unit!r}")
    if not math.isfinite(hours) or hours < 0.0:
        raise StandardValueError(f"{text!r} is not a usable elapsed time")
    return hours


def _ask_until(console: Any, question: str, parse, *, attempts: int = 5):
    """Ask, parse, and loop on a rejected answer rather than aborting."""
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return parse(ask(console, question))
        except StandardValueError as exc:
            last = exc
            console.show("── rejected ──", str(exc), "Try again.")
    raise StandardValueError(
        f"no acceptable answer after {attempts} attempts") from last


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    """Local, timezone-aware -- the same clock as
    :func:`caldmm.cal.procedure._now` and :func:`caldmm.cal.engine._now`.

    One calibration record must not contain two clocks; see the note on
    ``procedure._now``.
    """
    return datetime.now().astimezone()


def _model_of(device: Any) -> str:
    """Whatever the meter answers to ``ID?``.  Never raises."""
    for name, args in (("idn", ()), ("query", ("ID?",))):
        method = getattr(device, name, None)
        if not callable(method):
            continue
        try:
            answer = method(*args)
        except Exception:                                # noqa: BLE001
            continue
        if answer:
            return str(answer).strip()
    return ""


def _serial_of(device: Any) -> str:
    identity = getattr(device, "identity", None)
    return str(getattr(identity, "serial", "") or "")


def _ppm(value: float, reference: float) -> float:
    return (value - reference) / abs(reference) * 1e6 if reference else 0.0


def _security_code_of(ctx: ProcedureContext):
    """The operator's ``SECURE`` code, or ``None``.

    Held as a plain attribute on the context and **nowhere else**: never on the
    procedure (which is a shared, stateless registered instance), never in a
    declared command, never in a :class:`StepResult`, never in the result's
    ``detail``.  A test asserts it appears in no rendered record.
    """
    code = getattr(ctx, "security_code", None)
    return None if code in (None, "") else code


def _sourced(ctx: ProcedureContext) -> bool:
    """Whether this run's gain artefacts are stood in for by a calibrator.

    Decided by one thing only: a source on the context.  The artefact mode --
    certified standards connected by hand -- stays the default, and nothing
    here ever opens or demands a source.
    """
    return getattr(ctx, "source", None) is not None


def _applied_line(point: Point, state: Any) -> str:
    """A record line for what the calibrator was actually put on."""
    actual = float(getattr(state, "actual_value", point.value))
    return f"OUT {actual:.8E} {point.unit}; OPER"


def _configure_dcv_range(ctx: ProcedureContext,
                         value: float) -> tuple[str, ...]:
    """Put the meter on a **fixed** dc-volts range for *value* (§9).

    ``ARANGE OFF`` first: autorange is the 3458A's power-on state (User's
    Guide p.240), it is one global setting rather than a per-function one,
    and a range chosen from a live output is a range chosen after the output
    has already met whatever the meter happened to be on.  ``DCV <value>``
    then selects the range by maximum input, exactly as the meter adapter
    does for a verification run.
    """
    ctx.device.cmd("ARANGE OFF")
    ctx.device.dcv(abs(float(value)))
    return ("ARANGE OFF", f"DCV {abs(float(value)):.2E}")


def _prove_dcv(ctx: ProcedureContext, source: Any,
               point: Point) -> tuple[str, ...]:
    """Global Constraints §8's wiring proof for a sourced dc-volts point.

    ``max(10 mV, 0.1 % of the point's full scale)`` on wiring nobody has
    proved yet, read back within 10 % before the standard's own level is
    applied.  The meter is **not** re-ranged for it: the level is a tenth of
    a percent of the range about to be adjusted, which reads perfectly well
    on it, and moving a 3458A off that range would prove a different path.
    """
    return tuple(procedure.prove_wiring(
        source, point, configure=None,
        read=lambda: float(ctx.device.read_single_sample()),
        unit=point.unit, full_scale=abs(float(point.value)),
        meter_model=MODEL_ID,
        note=lambda words: ctx.captures.setdefault(
            "wiring_proofs_skipped", []).append(words),
        what=f"the sourced {point.value:g} {point.unit} standard"))


def _source_standby(ctx: ProcedureContext, source: Any, step_id: str) -> None:
    """Standby from a ``finally``: never raises, always warns on failure.

    The same discipline as the 34401A's sourced steps -- the output must be
    dark on every exit path, and a standby that could not be confirmed is a
    warning the operator has to see before touching the terminals.
    """
    try:
        source.standby()
    except Exception as exc:                              # noqa: BLE001
        ctx.warn(f"{step_id}: the calibrator did not confirm standby "
                 f"({exc}) — check its front panel before touching the "
                 f"terminals.")


def _require_close_to_readback(reading: float, actual: float, what: str,
                               unit: str) -> None:
    """The sourced mode's gross-error gate, against the **readback**.

    The same :data:`GAIN_CHECK_FRACTION` gate as :func:`_require_close`, but
    the reference is what the calibrator reports it is really producing --
    which is the value ``CAL`` will receive -- not the nominal it was asked
    for.  A :class:`WiringError` for the same reason as there: the function
    and range were really selected and a reading really taken, so the record
    must say the commands went out.
    """
    if not math.isfinite(reading):
        raise WiringError(
            f"refusing to continue to {what}: the meter returned {reading!r} "
            f"against the calibrator's output. Nothing was sent to "
            f"calibration memory.")
    if not actual or not math.isfinite(actual):
        raise WiringError(
            f"refusing to continue to {what}: the calibrator reports "
            f"{actual!r} as its output, which cannot be adjusted against. "
            f"Nothing was sent to calibration memory.")
    if abs(reading - actual) > abs(actual) * GAIN_CHECK_FRACTION:
        raise WiringError(
            f"refusing to continue to {what}: the meter reads "
            f"{reading:.9g} {unit} but the calibrator reports "
            f"{actual:.9g} {unit} on its terminals — "
            f"{_ppm(reading, actual) / 1e4:+.2f} %, past the "
            f"{GAIN_CHECK_FRACTION * 100:g} % this procedure accepts. That "
            f"is a wiring fault or an un-settled output, not a meter out of "
            f"calibration. NOTHING WAS SENT TO CALIBRATION MEMORY; the "
            f"meter's function and range were changed by this check and the "
            f"calibrator was put back in standby. (The threshold is this "
            f"project's gross-error gate, not a Calibration Manual figure.)")


# --------------------------------------------------------------------------
# The guards -- every one of them raises a type ``classify_failure`` treats as
# "the command was never sent", and every one of them runs before any I/O.
# --------------------------------------------------------------------------

def require_write_enabled(ctx: ProcedureContext, what: str) -> None:
    """Refuse an adjustment on a driver that was not opted in to writing.

    The driver enforces this too (``allow_calibration_write``), and its own
    ``CalibrationWriteNotAllowed`` is also raised before any byte is
    transmitted -- but that type is not in
    :data:`caldmm.cal.procedure.UNSENT_GUARDS`, so a step that reached it
    would be recorded as though ``CAL`` had gone out and non-volatile memory
    were of unknown state.  Refusing here, with a listed guard, keeps the audit
    trail true.  **This is a second lock, not a replacement**: a procedure
    cannot bypass the driver's opt-in, and a test asserts that.
    """
    if not getattr(ctx.device, "allow_calibration_write", False):
        raise SafetyError(
            f"refusing to send {what}: this meter was opened without "
            f"allow_calibration_write=True, so the driver would refuse it "
            f"anyway. Nothing was sent. Open the instrument for a committing "
            f"run (caldmm-cal-verify --adjust --adjust-commit) if you really "
            f"mean to overwrite its calibration memory.")


def require_not_freerunning(ctx: ProcedureContext, what: str) -> None:
    """Refuse while the meter is streaming readings (``TARM AUTO``).

    ``adjust()`` reads ``ERR?``/``AUXERR?``/``CALNUM?`` straight after the
    command; a stream of readings in the output buffer would be parsed as those
    answers.  The driver refuses too, with a ``ValueError`` raised before
    transmission -- again a type the classifier would file as "sent".
    """
    if getattr(ctx.device, "freerunning", False):
        raise SafetyError(
            f"refusing to send {what}: the meter is free-running (TARM AUTO) "
            f"and its streaming readings would be mistaken for the ERR? "
            f"response. Nothing was sent; call stop() first.")


def require_valid_security_code(ctx: ProcedureContext, what: str):
    """Re-check the code before the wire, and return it.

    :func:`parse_security_code` already validated it at the prompt, but a
    context can be built by hand -- an app, a script, a test -- so the value is
    checked again here, where the guard type is one the classifier understands.
    Reaching ``adjust()`` with a bad code would raise a plain ``ValueError``
    from the driver: also before any I/O, but not in
    :data:`caldmm.cal.procedure.UNSENT_GUARDS`, so the run would be filed as
    "``CAL`` sent, NVRAM unknown" and the operator sent to check a meter
    nothing was ever transmitted to.
    """
    code = _security_code_of(ctx)
    if code is None:
        return None
    if not SECURITY_CODE_RE.match(str(code).strip()):
        raise SafetyError(
            f"refusing to send {what}: the calibration security code on this "
            f"context is not alphanumeric, and the 3458A accepts several "
            f"commands on one line — appending it could turn an irreversible "
            f"write into two commands. Nothing was sent. (The value is "
            f"deliberately not echoed.)")
    return str(code).strip()


def require_in_band(value: float, what: str) -> str:
    """Refuse a ``CAL`` argument outside the documented bands, and say which.

    Mirrors ``hp3458a.adjustment_kind`` (Calibration Manual p.87-89); returns
    the same ``offset`` / ``dc_gain`` / ``resistance`` label.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SafetyError(
            f"refusing to send {what}: {value!r} is not a number") from None
    if not math.isfinite(value):
        raise SafetyError(
            f"refusing to send {what}: {value!r} is not finite")
    if value == 0.0:
        return "offset"
    if CAL_VOLTAGE_MIN <= value <= CAL_VOLTAGE_MAX:
        return "dc_gain"
    if CAL_RESISTANCE_MIN <= value <= CAL_RESISTANCE_MAX:
        return "resistance"
    raise SafetyError(
        f"refusing to send {what}: {value:g} is outside every documented CAL "
        f"band. The Calibration Manual (p.87-89) allows CAL 0 (offset, with a "
        f"four-wire short), CAL {CAL_VOLTAGE_MIN:g}..{CAL_VOLTAGE_MAX:g} V (dc "
        f"gain, 10 V recommended) and CAL {CAL_RESISTANCE_MIN:g}.."
        f"{CAL_RESISTANCE_MAX:g} Ω (resistance and dc current, 10 kΩ "
        f"recommended). Nothing was sent.")


# --------------------------------------------------------------------------
# The 3458A procedure
# --------------------------------------------------------------------------

_SAFETY_BRIEF: tuple[str, ...] = (
    "THIS PROCEDURE OVERWRITES THE 3458A's NON-VOLATILE CALIBRATION MEMORY.",
    "The previous constants are not recoverable and there is no undo.",
    "",
    "This is CAL, the EXTERNAL adjustment. It is NOT ACAL:",
    "  ACAL retunes the meter against its OWN internal 7 V and 40 kΩ "
    "references",
    "       and can never make it more accurate than they already are;",
    "  CAL  retrains those references against the standard on the terminals,",
    "       and is the only one of the two that moves absolute accuracy.",
    "",
    f"The meter must have been ON for {WARMUP_HOURS:g} HOURS (Calibration "
    f"Manual ch.3,",
    "Preliminary Adjustment Procedure step 6). There is no ONTIME? on a "
    "3458A --",
    "nothing can check this for you, so confirm it yourself before "
    "continuing.",
    "",
    "Each CAL takes minutes (offset ~5, dc gain ~2, resistance ~12) and there "
    "is no",
    "documented remote abort. The meter is working, not hung.",
)


class HP3458AExternalAdjust:
    """The 3458A's external adjustment, Calibration Manual chapter 3.

    Four adjustments in a **mandatory order** -- front offset, rear offset, dc
    gain, resistance and dc current -- followed by the ``ACAL AC`` that ch.3
    p.45 step 7 requires because the AC Adjustment itself is a two-yearly job.

    Every irreversible step is preceded by a **non-committing setup step** that
    really runs: it selects the function and range, applies ``OCOMP``/``DELAY``
    where the manual calls for them, and takes a reading through the wiring the
    operator has just made.  That is what makes the default dry run useful
    rather than merely safe -- an operator can rehearse the whole sequence,
    including the wiring checks, and see the exact ``CAL`` commands that would
    have been sent.  It is also the only defence against a standard that is not
    actually connected: ``CAL`` believes whatever it is told.

    Two modes, decided by whether the context carries a source:

    * **Artefact mode (the default, and no source may be demanded)**: the two
      gain values come from certified standards the operator connects by hand
      and types in from their certificates.  This is the metrologically
      preferred mode -- a real 732B at 10 V beats the 5700A's 10 V output.
    * **Sourced mode** (``ctx.source`` is set): a calibrator stands in for
      the 10 V and 10 kΩ artefacts, and each ``CAL`` receives the
      calibrator's **readback**, never the nominal.  It exists for benches
      without discrete standards, and it changes only the two gain groups:
      the front/rear offset adjustments still use the hand-fitted
      four-terminal low-thermal short in both modes, because the
      calibrator's 0 Ω reached through leads and output relays is not a
      low-thermal short.

    Stateless, like every procedure: all run state is on the context.
    """

    model_id = MODEL_ID
    procedure_id = PROCEDURE_ID
    name = "HP 3458A external adjustment (CAL — not ACAL)"

    #: A calibrator is **accepted, never required** -- the three-way
    #: ``source_policy`` answer "optional".  Artefacts connected by hand
    #: remain the metrologically preferred default (a real 732B at 10 V beats
    #: the 5700A's 10 V); the sourced mode exists for benches without
    #: discrete standards.  Deliberately not ``needs_source``: that would
    #: refuse the hand-artefact run this procedure was written for.
    accepts_source = True

    #: What the object on the bus has to be able to do.  Duck-typed; this
    #: module never imports a driver at module scope.
    NEEDS = ("adjust", "calnum", "cal_string", "dcv", "ohmf", "set_ocomp",
             "read_single_sample", "get_temp", "cmd", "error_register",
             "aux_error_register")

    #: What a source has to be able to do, when one is supplied -- the
    #: ``SourceRole`` surface, same as the 34401A's.
    SOURCE_NEEDS = ("validate", "apply", "standby", "health")

    # -- operator data ---------------------------------------------------

    def prepare(self, ctx: ProcedureContext) -> None:
        """Collect the ambient, the ACAL age, the security code and the two
        standards.  Called by the runner before :meth:`preflight`.

        A caller that has already filled any of them in -- a script, a test,
        ``--ambient`` on the command line -- is left alone.  In sourced mode
        the two certificate prompts are skipped: the calibrator's readback
        replaces both certified values, so there is nothing to type in.
        Everything else -- ambient, ACAL age, security code -- is collected
        identically in both modes.
        """
        if ctx.ambient_c is None:
            ctx.console.show(
                "── ambient temperature ──",
                f"The 3458A's adjustment is specified over "
                f"{AMBIENT_MIN_C:g}-{AMBIENT_MAX_C:g} °C (Calibration Manual "
                f"ch.3, Preliminary step 1).",
                "The more thermally stable the environment, the better the "
                "adjustment.")
            ctx.ambient_c = _ask_until(
                ctx.console, "ambient temperature [°C]: ", parse_ambient_c)

        if not hasattr(ctx, "acal_age_h"):
            ctx.console.show(
                "── time since the last ACAL ──",
                "The 3458A cannot report this, and chapter 3 does not require "
                "a recent ACAL",
                "before adjusting — but every accuracy column except 24-hour "
                "is footnoted",
                "'within 24 hours and ±1 °C of last ACAL', so the trial "
                "readings this",
                "procedure takes are only as meaningful as the ACAL behind "
                "them.",
                "Leave it blank if you do not know.")
            ctx.acal_age_h = _ask_until(
                ctx.console, "hours since the last ACAL ALL (blank = "
                             "unknown): ", parse_hours)

        if not hasattr(ctx, "security_code"):
            ctx.console.show(
                "── calibration security code ──",
                "CAL is gated by the code set with SECURE (Calibration Manual "
                "p.21).",
                "The factory value is documented inconsistently — 3458 in the "
                "Calibration",
                "Manual, 3458A in the User's Guide — so nothing is assumed "
                "here. Leave it",
                "blank if security is disabled (code 0).",
                "",
                "It is asked for here and never taken from the command line: "
                "an argument",
                "vector is visible to every process on the machine. It is "
                "sent on the wire",
                "and stored nowhere — not in the record, not in the step log, "
                "not in any",
                "exception. NOTE: this prompt does echo what you type.")
            ctx.security_code = _ask_until(
                ctx.console,
                "calibration security code (blank if disabled): ",
                parse_security_code)

        if not _sourced(ctx):
            ctx.standards = prompt_standards(ctx.console, HP3458A_STANDARDS,
                                             ctx.standards)

    # -- preflight -------------------------------------------------------

    def precheck(self, ctx: ProcedureContext) -> list[str]:
        """The instrument-only half, run **before** :meth:`prepare`.

        Two calibration certificates are two certified values, two
        uncertainties and two serials to type.  Discovering afterwards that the
        thing on the bus is not a 3458A, and having the typing thrown away, is
        worth a separate pass.
        """
        problems = self._instrument_problems(ctx)
        if _sourced(ctx):
            problems += self._source_role_problems(ctx.source)
        return problems

    def preflight(self, ctx: ProcedureContext) -> list[str]:
        problems = self._instrument_problems(ctx)
        if problems:
            return problems

        if ctx.ambient_c is None:
            problems.append(
                f"no ambient temperature: the adjustment is specified only "
                f"over {AMBIENT_MIN_C:g}-{AMBIENT_MAX_C:g} °C (Calibration "
                f"Manual ch.3, Preliminary step 1)")
        else:
            try:
                parse_ambient_c(f"{float(ctx.ambient_c):g}")
            except StandardValueError as exc:
                problems.append(str(exc))

        if _sourced(ctx):
            # The calibrator replaces both certificates, so its own state is
            # what gets checked instead: the role surface, its health and its
            # envelope -- all offline, before anything is energized.
            role_problems = self._source_role_problems(ctx.source)
            problems += role_problems
            if not role_problems:
                problems += self._source_problems(ctx, ctx.source)
                problems += sourced_envelope_problems(ctx.source)
        else:
            for spec in HP3458A_STANDARDS:
                standard = ctx.standards.get(spec.key)
                if standard is None:
                    problems.append(
                        f"no certified value entered for the {spec.name} "
                        f"({spec.describe()})")
                elif (standard.uncertainty_ppm
                      > spec.recommended_uncertainty_ppm * 1.01):
                    ctx.warn(
                        f"{spec.name}: entered uncertainty "
                        f"{standard.uncertainty_ppm:.2f} ppm is worse than the "
                        f"±{spec.recommended_uncertainty_ppm:g} ppm of the "
                        f"{spec.recommended} chapter 3 specifies; the meter's "
                        f"accuracy after this adjustment inherits it")

        age = getattr(ctx, "acal_age_h", None)
        if age is None:
            ctx.warn(
                "time since the last ACAL is unknown; every 3458A accuracy "
                "column except 24-hour assumes an ACAL within 24 hours and "
                "±1 °C. Chapter 3 does not require one before adjusting, so "
                "this does not block.")
        elif age > ACAL_STALE_HOURS:
            ctx.warn(
                f"the last ACAL was {age:g} h ago, past the "
                f"{ACAL_STALE_HOURS:g} h every accuracy column except 24-hour "
                f"assumes. Chapter 3 does not require one before adjusting, "
                f"so this does not block — but the trial readings below are "
                f"only as good as that ACAL.")

        # Caught here as well as at the prompt and at each write, because a
        # context can be built by hand and because failing at step 1 of an
        # hour-long sequence over a typo is avoidable.
        try:
            require_valid_security_code(ctx, "CAL")
        except SafetyError as exc:
            problems.append(str(exc))

        if ctx.commit:
            if not getattr(ctx.device, "allow_calibration_write", False):
                problems.append(
                    "this is a committing run but the meter was opened "
                    "without allow_calibration_write=True, so every CAL would "
                    "be refused by the driver before transmission")
            ctx.warn(
                "commit=True: CAL will be sent and the 3458A's non-volatile "
                "calibration memory will be overwritten irreversibly")
        return problems

    def _instrument_problems(self, ctx: ProcedureContext) -> list[str]:
        problems: list[str] = []
        missing = [name for name in self.NEEDS
                   if not callable(getattr(ctx.device, name, None))]
        if missing:
            problems.append(
                f"the instrument object does not implement "
                f"{', '.join(missing)}; an external adjustment needs an "
                f"HP 3458A driver (caldmm.drivers.hp3458a.HP3458A)")
            return problems                # nothing below can be trusted

        model = _model_of(ctx.device).upper()
        if model and "3458" not in model:
            problems.append(
                f"the instrument answered {model!r}, which is not a 3458A. "
                f"Chapter 3's sequence is model-specific and would write "
                f"nonsense into anything else.")
        if getattr(ctx.device, "freerunning", False):
            problems.append(
                "the meter is free-running (TARM AUTO); its streaming "
                "readings would be mistaken for the ERR? response after a "
                "CAL. Call stop() first.")
        return problems

    def _source_role_problems(self, source: Any) -> list[str]:
        missing = [name for name in self.SOURCE_NEEDS
                   if not callable(getattr(source, name, None))]
        if missing:
            return [f"the source object cannot {', '.join(missing)} — it "
                    f"does not satisfy caldmm.cal.roles.SourceRole"]
        return []

    @staticmethod
    def _source_problems(ctx: ProcedureContext, source: Any) -> list[str]:
        """Whatever the calibrator says about itself.  Never fatal to read.

        The same checks, with the same thresholds and the same blocking
        force, as the 34401A's sourced adjustment: the calibrator's health
        gates the same thing on both meters -- whether its output is fit to
        be the reference a meter is irreversibly adjusted against.
        """
        problems: list[str] = []
        try:
            health = source.health()
        except Exception as exc:                          # noqa: BLE001
            return [f"the calibrator would not report its health: {exc}"]
        if getattr(health, "faults", ()):
            codes = ", ".join(f"{code} {text}"
                              for code, text in health.faults)
            problems.append(
                f"the calibrator has queued faults ({codes}). Clear them "
                f"first: a non-empty fault queue can inhibit OPER silently, "
                f"and a CAL taken against a dark output would train the "
                f"meter's gain on nothing.")
        warm = getattr(health, "warm_minutes", None)
        if warm is not None and warm < 30:
            problems.append(
                f"the calibrator has been warm for {warm} min; it needs at "
                f"least twice its time off, to a maximum of 30 min")
        days = getattr(health, "days_since_zero", None)
        if days is not None and days > 30:
            problems.append(
                f"the calibrator's dc zeros are {days} days old. They are "
                f"the reference this meter is about to be adjusted against.")
        return problems

    # -- the order guard -------------------------------------------------

    def require_order(self, ctx: ProcedureContext, step_id: str) -> None:
        """Refuse a step whose prerequisites did not complete.

        Public because it is the point of the class: an app offering a retry
        has to be able to ask.  Raises :class:`StepOrderError`, which
        :func:`~caldmm.cal.procedure.classify_failure` records as **never
        sent** -- and it really is, because this runs before any I/O.
        """
        if step_id not in _ORDER_BY_ID:
            raise ProcedureError(f"{step_id!r} is not an ordered CAL step")
        missing = [name for name in _ORDER_BY_ID[step_id]
                   if not ctx.completed(name)]
        if missing:
            raise StepOrderError(
                f"refusing to send {step_id}: {', '.join(missing)} did not "
                f"complete. Chapter 3 of the Calibration Manual states 'You "
                f"must perform the adjustments in the order presented in this "
                f"chapter', and p.43 says the resistance adjustment 'relies on "
                f"the values calculated by the DC Gain Adjustment'. Unlike the "
                f"5700A there is no documented out-of-order fault to catch "
                f"this afterwards — the meter would simply compute and store "
                f"constants from stale gains. Nothing was sent.")
        if ctx.completed(step_id):
            raise StepOrderError(
                f"refusing to send {step_id} twice: each adjustment is taken "
                f"once, in order. Restart the whole sequence if it has to be "
                f"redone.")

    # -- the irreversible step -------------------------------------------

    def adjust_step(self, ctx: ProcedureContext, step_id: str, value: float,
                    label: str) -> StepResult:
        """One ``CAL <value>``, fully guarded, order-checked before the wire.

        Every failure the driver can raise is allowed to propagate
        **unwrapped**.  That matters most for
        ``hp3458a.CalibrationLockoutError``, whose message is the entire user
        experience of a lockout: it already names jumper J132, the
        03458-66505/-66515 assembly, the top cover, the ``SECURE`` code, and
        the fact that the 3458A has no rear-panel calibration switch.  Wrapping
        it in something of this module's own would replace all of that with
        less.
        """
        self.require_order(ctx, step_id)
        command = f"CAL {value:.10G}"
        require_write_enabled(ctx, command)
        require_not_freerunning(ctx, command)
        kind = require_in_band(value, command)
        code = require_valid_security_code(ctx, command)

        started = _now()
        # The security code goes on the wire and into no record: the declared
        # command above -- which IS recorded -- deliberately omits it.
        calnum = ctx.device.adjust(value, security_code=code)
        return StepResult(
            step_id=step_id, outcome=StepOutcome.EXECUTED, started_at=started,
            finished_at=_now(), writes_nvram=True, commands=(command,),
            response=f"{label} adjustment stored; CALNUM? now {calnum}",
            operator="typed confirmation given",
            detail={"kind": kind, "value": value, "calnum": calnum})

    # -- steps -----------------------------------------------------------

    def steps(self, ctx: ProcedureContext) -> Iterator[AdjustStep]:
        sourced = _sourced(ctx)
        if not sourced:
            volts = self._standard(ctx, "10V")
            ohms = self._standard(ctx, "10KOHM")

        yield self._brief_step()
        yield self._capture_before_step()

        # 1. Front Terminal Offset Adjustment -- Calibration Manual p.40.
        yield self._short_step(
            "short-front", "FRONT",
            ("Connect the four-terminal low-thermal short across the FRONT "
             "panel HI and LO",
             "Input terminals AND the HI and LO Ω Sense terminals "
             "(Figure 3-1, p.39).",
             "Set the front-panel Terminals switch to FRONT.",
             "Then WAIT 5 MINUTES for thermal equilibrium and do not touch "
             "the short;",
             "cover the terminals if there are drafts (Calibration Manual "
             "p.40, note)."))
        yield self._cal_zero_step("offset-front", "FRONT", "p.40")

        # 2. Rear Terminal Offset Adjustment -- Calibration Manual p.41.
        yield self._short_step(
            "short-rear", "REAR",
            ("Move the four-terminal short to the REAR terminals.",
             "Set the front-panel Terminals switch to REAR.",
             "Then WAIT 5 MINUTES for thermal equilibrium (Calibration "
             "Manual p.41)."))
        yield self._cal_zero_step("offset-rear", "REAR", "p.41")

        # 3. DC Gain Adjustment -- Calibration Manual p.42.
        # 4. Resistance and DC current adjustment -- Calibration Manual
        #    p.43-45.
        # The only steps the sourced mode replaces.  The step ids are shared
        # so ADJUSTMENT_ORDER holds identically in both modes; the offset
        # steps above are the same hand-fitted low-thermal short either way
        # (the calibrator's 0 Ω through leads is not one).
        if sourced:
            yield self._sourced_dc_standard_step()
            yield self._sourced_dc_gain_step()
            yield self._sourced_ohms_standard_step()
            yield self._sourced_resistance_step()
        else:
            yield self._dc_standard_step(volts)
            yield self._dc_gain_step(volts)
            yield self._ohms_standard_step(ohms)
            yield self._resistance_step(ohms)

        # ch.3 p.45 step 7 and step 8.
        yield self._acal_ac_step()
        yield self._settle_step()

        yield self._capture_after_step()

    def _standard(self, ctx: ProcedureContext, key: str) -> ExternalStandard:
        standard = ctx.standards.get(key)
        if standard is None:
            raise ProcedureError(
                f"cannot build the sequence: no certified value for {key!r} "
                f"(preflight should have blocked this)")
        return standard

    # -- brief and captures ----------------------------------------------

    def _brief_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=_now(), finished_at=_now(),
                              operator="acknowledged the safety brief")

        return AdjustStep(
            id="brief",
            prompt="Read this before the first adjustment.",
            action=action, required=True,
            body=(*_SAFETY_BRIEF, "", *NOT_IMPLEMENTED_NOTES))

    def _capture_before_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            capture = _capture_identity(ctx, "before")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(), commands=step.commands,
                response=_describe_capture(capture), detail={"capture": capture})

        return AdjustStep(
            id="capture-before",
            prompt="Recording CALNUM?, CALSTR? and TEMP? as they stand.",
            action=action, requires_operator=False,
            commands=("CALNUM?", "CALSTR?", "TEMP?"),
            body=("The Adjustment Record (Calibration Manual p.51) asks for "
                  "the calibration",
                  "number BEFORE adjusting and again after; CALNUM? "
                  "advancing is the only",
                  "independent evidence that calibration memory really "
                  "changed."))

    def _capture_after_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            capture = _capture_identity(ctx, "after")
            before = (ctx.captures.get("before") or {}).get("calnum")
            after = capture.get("calnum")
            if ctx.commit and before is not None and after is not None:
                if after <= before:
                    ctx.warn(
                        f"CALNUM? did not advance ({before} -> {after}). On a "
                        f"committing run that means the meter did NOT accept "
                        f"an adjustment — treat this session as having "
                        f"changed nothing and check the security code and "
                        f"jumper J132 before trusting the meter.")
            # True after a committing run as well as a dry one -- the setup
            # steps run either way, and the meter is left on 4-wire ohms with
            # OCOMP ON and a DELAY chosen for a 10 kΩ standard regardless of
            # what happened to calibration memory.
            ctx.warn(
                ("dry run: no CAL was sent and calibration memory is "
                 "untouched — but the setup steps really did change the "
                 if ctx.dry_run else
                 "the setup steps changed the ") +
                "meter's function, range, OCOMP and DELAY, and this procedure "
                "does not put them back. Reconfigure it (or PRESET NORM) "
                "before using it to measure anything.")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(), commands=step.commands,
                response=_describe_capture(capture), detail={"capture": capture})

        return AdjustStep(
            id="capture-after",
            prompt="Recording CALNUM? and TEMP? after the sequence.",
            action=action, requires_operator=False,
            commands=("CALNUM?", "CALSTR?", "TEMP?"),
            body=("The Adjustment Record asks for the internal adjustment "
                  "temperature (TEMP?)",
                  "and the calibration number after adjusting."))

    # -- 1 and 2: the offset adjustments ---------------------------------

    def _short_step(self, step_id: str, terminals: str,
                    body: Sequence[str]) -> AdjustStep:
        """Non-committing: prove the four-terminal short is really there."""
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            ctx.device.dcv(0.1)                     # DCV, 100 mV range: p.38 §4
            reading = float(ctx.device.read_single_sample())
            if abs(reading) > SHORT_MAX_V:
                raise WiringError(
                    f"refusing to continue to CAL 0: the {terminals} "
                    f"terminals read {reading:.9g} V on the 100 mV range, "
                    f"past the {SHORT_MAX_V:g} V this procedure will accept "
                    f"as a four-terminal short. A missing, badly made or "
                    f"un-settled short reads like this, and CAL 0 would store "
                    f"it as the meter's offset — permanently. NOTHING WAS "
                    f"SENT TO CALIBRATION MEMORY; the meter was left on DCV, "
                    f"100 mV range by this check. (The threshold is this "
                    f"project's wiring check, not a Calibration Manual "
                    f"figure.)")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=step.commands,
                response=f"{terminals} short reads {reading * 1e6:+.3f} µV",
                detail={"reading_v": reading, "terminals": terminals})

        return AdjustStep(
            id=step_id,
            prompt=f"Fit the four-terminal short to the {terminals} "
                   f"terminals.",
            action=action, commands=("DCV 1.00E-01", "TARM SGL,1"),
            setpoint=SetPoint(0.0, "V", label="a four-terminal short"),
            body=(*body, "",
                  f"This step commits nothing. It selects DCV on the 100 mV "
                  f"range and takes one",
                  f"reading, which must be under {SHORT_MAX_V * 1e3:g} mV — "
                  f"the only check there is that",
                  f"the short is actually fitted before CAL 0 stores whatever "
                  f"it measures."))

    def _cal_zero_step(self, step_id: str, terminals: str,
                       page: str) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return self.adjust_step(ctx, step_id, 0.0,
                                    f"{terminals} terminal offset")

        return AdjustStep(
            id=step_id,
            prompt=f"Running CAL 0 — the {terminals} terminal offset "
                   f"adjustment. About 5 minutes.",
            action=action, writes_nvram=True, abort_hint=ABORT_HINT_3458A,
            commands=("CAL 0",),
            body=(f"Calibration Manual {page}: 'Execute the CAL 0 command. "
                  f"The multimeter",
                  f"automatically performs the {terminals.lower()} terminal "
                  f"offset adjustment.' It stores",
                  f"offset constants for DCV, DCI, OHM and OHMF for THIS "
                  f"Terminals switch position.",
                  f"Do not touch the short or the terminals while it runs.",
                  f"(If a security code is set it is appended on the wire; it "
                  f"is not recorded.)"),
            confirm=f"writing the {terminals} terminal offset constants to "
                    f"the 3458A's NON-VOLATILE calibration memory (CAL 0), "
                    f"overwriting the existing ones irreversibly",
            aftercare=(f"The four-terminal short is still on the {terminals} "
                       f"terminals and the",
                       f"Terminals switch is still at {terminals}. Remove the "
                       f"short before using",
                       f"the meter for anything else."),
            dry_run_note=(
                f"dry run: CAL 0 was NOT sent, so the {terminals} terminal "
                f"offset constants are unchanged."))

    # -- 3: the dc gain adjustment ---------------------------------------

    def _dc_standard_step(self, standard: ExternalStandard) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            # The certified value itself, not a headroom multiple: p.42 says
            # the meter measures the standard "using its 10 V range", and a
            # range argument of 12 sits exactly on the 10 V range's overrange
            # boundary, where which range is selected is not something to
            # guess at before an irreversible command.
            ctx.device.dcv(standard.certified)
            reading = float(ctx.device.read_single_sample())
            _require_close(reading, standard, "CAL", "V")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=step.commands,
                response=(f"reads {reading:.9g} V, "
                          f"{_ppm(reading, standard.certified):+.2f} ppm from "
                          f"the certified {standard.certified:.9g} V"),
                detail={"reading_v": reading, "standard": standard,
                        "deviation_ppm": _ppm(reading, standard.certified)})

        return AdjustStep(
            id="standard-10v",
            prompt="Connect the 10 V dc standard to the FRONT panel HI and LO "
                   "Input terminals.",
            action=action,
            commands=(f"DCV {standard.certified:.2E}", "TARM SGL,1"),
            setpoint=SetPoint(standard.certified, "V",
                              label=f"{standard.certified:.9g} V (the "
                                    f"standard, not a source setting)"),
            body=("Calibration Manual p.42, steps 1-3: select the dc voltage "
                  "function, set the",
                  "Terminals switch to FRONT, and connect the standard as "
                  "Figure 3-2 shows.",
                  "With a Guard wire, set the Guard switch to Open; without "
                  "one, to 'To LO'.",
                  "",
                  f"certified: {standard.describe()}",
                  "",
                  "This step commits nothing: it selects the function and "
                  "reads the standard,",
                  "so that a lead in the wrong jack is found now rather than "
                  "written into",
                  "calibration memory by the CAL that follows."))

    def _dc_gain_step(self, standard: ExternalStandard) -> AdjustStep:
        command = f"CAL {standard.certified:.10G}"

        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return self.adjust_step(ctx, "dc-gain", standard.certified,
                                    "dc gain")

        return AdjustStep(
            id="dc-gain",
            prompt="Running the dc gain adjustment. About 2 minutes.",
            action=action, writes_nvram=True, abort_hint=ABORT_HINT_3458A,
            commands=(command,),
            body=("Calibration Manual p.42, step 4: 'Execute the CAL command "
                  "specifying the",
                  "exact output voltage of the standard.' The meter measures "
                  "the standard on",
                  "its 10 V range, adjusts its gain to agree, then measures "
                  "and stores its own",
                  "internal 7 V reference against it. This also performs the "
                  "equivalent of an",
                  "ACAL DCV (p.88).",
                  "",
                  f"the value sent is the CERTIFIED value: "
                  f"{standard.certified:.9g} V"),
            confirm="writing the dc gain constant and the value of the "
                    "internal 7 V reference to the 3458A's NON-VOLATILE "
                    "calibration memory (CAL), overwriting the existing ones "
                    "irreversibly",
            aftercare=("The 10 V standard is still connected to the FRONT "
                       "Input terminals.",
                       "Disconnect it (Calibration Manual p.42, step 5)."),
            dry_run_note=(
                f"dry run: {command} was NOT sent, so the dc gain constant "
                f"and the stored value of the internal 7 V reference are "
                f"unchanged."))

    # -- 4: the resistance and dc current adjustment ---------------------

    def _ohms_standard_step(self, standard: ExternalStandard) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            # p.44 step 2: OCOMP ON, so thermal EMFs do not bias the
            # adjustment.  Through the driver's setter, which keeps the
            # cached state truthful.
            ctx.device.set_ocomp(True)
            ctx.device.ohmf(standard.certified)

            # p.44 NOTE: measure at the 50 ms default and again at ~1 s; a
            # significant difference means the standard or the cabling needs
            # the longer settling, and DELAY must be set before CAL.
            ctx.device.delay = OHMS_DELAY_SHORT_S
            fast = float(ctx.device.read_single_sample())
            ctx.device.delay = OHMS_DELAY_LONG_S
            slow = float(ctx.device.read_single_sample())
            spread_ppm = abs(_ppm(slow, fast)) if fast else float("inf")

            if spread_ppm > OHMS_DELAY_SIGNIFICANT_PPM:
                chosen = OHMS_DELAY_LONG_S
                ctx.warn(
                    f"the 10 kΩ standard reads {spread_ppm:.1f} ppm "
                    f"differently at {OHMS_DELAY_SHORT_S * 1e3:g} ms and "
                    f"{OHMS_DELAY_LONG_S:g} s of settling, so the adjustment "
                    f"will use DELAY {OHMS_DELAY_LONG_S:g} s (Calibration "
                    f"Manual p.44, note). The "
                    f"{OHMS_DELAY_SIGNIFICANT_PPM:g} ppm threshold is this "
                    f"project's; the manual says only 'significantly "
                    f"different'.")
            else:
                chosen = OHMS_DELAY_SHORT_S
            # Unconditional, so the command count does not depend on the
            # measurement: the value does, and the result records it below.
            ctx.device.delay = chosen
            reading = slow
            _require_close(reading, standard, "CAL", "Ω")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                # The declared tuple is the deterministic preview; what is
                # recorded is everything that really went out, including the
                # closing DELAY whose value only the readings decide.
                commands=(*step.commands, f"DELAY {chosen:.2E}"),
                response=(f"reads {reading:.9g} Ω, "
                          f"{_ppm(reading, standard.certified):+.2f} ppm from "
                          f"the certified {standard.certified:.9g} Ω; "
                          f"settling spread {spread_ppm:.1f} ppm; adjusting "
                          f"with DELAY {chosen:g} s"),
                detail={"reading_ohm": reading, "standard": standard,
                        "reading_fast_ohm": fast, "reading_slow_ohm": slow,
                        "settling_spread_ppm": spread_ppm,
                        "delay_s": chosen,
                        "deviation_ppm": _ppm(reading, standard.certified)})

        return AdjustStep(
            id="standard-10kohm",
            prompt="Disconnect the 10 V standard. Connect the 10 kΩ standard "
                   "4-wire to the FRONT terminals.",
            action=action,
            # Always followed by one more ``DELAY`` setting the value the
            # adjustment will use.  Its *value* is the one thing in this
            # procedure a measurement decides, so it is left out of the
            # preview rather than previewed wrongly -- and added to the
            # ``StepResult``'s own ``commands``, which is what the record and
            # the audit trail read.
            commands=("OCOMP ON", f"OHMF {standard.certified:.2E}",
                      f"DELAY {OHMS_DELAY_SHORT_S:.2E}", "TARM SGL,1",
                      f"DELAY {OHMS_DELAY_LONG_S:.2E}", "TARM SGL,1"),
            setpoint=SetPoint(standard.certified, "OHM",
                              label=f"{standard.certified:.9g} Ω (the "
                                    f"standard)"),
            body=("Calibration Manual p.44-45, steps 1-4: select 4-wire ohms, "
                  "execute OCOMP ON,",
                  "set the Terminals switch to FRONT, and connect the "
                  "standard to the HI and LO",
                  "Input AND HI and LO Sense terminals as Figure 3-3 shows.",
                  "",
                  f"certified: {standard.describe()}",
                  "",
                  "This step commits nothing. It also runs the manual's own "
                  "settling test (p.44,",
                  "note): the standard is measured at "
                  f"{OHMS_DELAY_SHORT_S * 1e3:g} ms and again at "
                  f"{OHMS_DELAY_LONG_S:g} s, and if the two",
                  "differ the adjustment is made with the longer DELAY."))

    def _resistance_step(self, standard: ExternalStandard) -> AdjustStep:
        command = f"CAL {standard.certified:.10G}"

        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return self.adjust_step(ctx, "resistance", standard.certified,
                                    "resistance and dc current")

        return AdjustStep(
            id="resistance",
            prompt="Running the resistance and dc current adjustment. About "
                   "12 minutes.",
            action=action, writes_nvram=True, abort_hint=ABORT_HINT_3458A,
            commands=(command,),
            body=("Calibration Manual p.45, step 5: 'Execute the CAL command "
                  "specifying the",
                  "exact value of the resistance standard.' This calculates "
                  "gain corrections for",
                  "the resistance AND dc current ranges, and p.43 states it "
                  "relies on the values",
                  "the dc gain adjustment computed — which is why that one "
                  "comes first.",
                  "",
                  f"the value sent is the CERTIFIED value: "
                  f"{standard.certified:.9g} Ω"),
            confirm="writing the resistance and dc current gain constants to "
                    "the 3458A's NON-VOLATILE calibration memory (CAL), "
                    "overwriting the existing ones irreversibly",
            aftercare=("The 10 kΩ standard is still connected. Disconnect it "
                       "(Calibration Manual",
                       "p.45, step 6)."),
            dry_run_note=(
                f"dry run: {command} was NOT sent, so the resistance and dc "
                f"current gain constants are unchanged."))

    # -- 3 and 4, sourced: the calibrator stands in for the artefacts ------
    #
    # Same step ids, same order guard, same gross-error gate -- but the value
    # every CAL receives is the calibrator's READBACK, never the nominal, and
    # the source goes to standby on every exit path of every step that
    # energized it (the same finally-standby discipline as the 34401A's
    # sourced groups).

    def _sourced_dc_standard_step(self) -> AdjustStep:
        point = SOURCED_POINT_10V

        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            source = ctx.require_source()
            sent: list[str] = []
            try:
                # §9: the meter goes on its fixed range, chosen from the
                # NOMINAL, before the calibrator is asked for anything.
                sent.extend(_configure_dcv_range(ctx, float(point.value)))
                # §8: a safe level first, on wiring nobody has proved yet.
                sent.extend(_prove_dcv(ctx, source, point))
                state = source.apply(point)
                actual = float(state.actual_value)
                sent.append(_applied_line(point, state))
                ctx.device.dcv(actual)
                sent.append(f"DCV {actual:.2E}")
                reading = float(ctx.device.read_single_sample())
                sent.append("TARM SGL,1")
                _require_close_to_readback(reading, actual, "CAL", "V")
            finally:
                _source_standby(ctx, source, step.id)
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=tuple(sent),
                response=(f"calibrator readback {actual:.9g} V; meter reads "
                          f"{reading:.9g} V, {_ppm(reading, actual):+.2f} ppm "
                          f"from it"),
                detail={"reading_v": reading, "source_actual_v": actual,
                        "source_uncertainty": state.uncertainty,
                        "source_range_id": getattr(state, "range_id", None),
                        "deviation_ppm": _ppm(reading, actual)})

        return AdjustStep(
            id="standard-10v",
            prompt="Connect the calibrator OUTPUT HI/LO to the FRONT panel "
                   "HI and LO Input terminals.",
            action=action,
            setpoint=SetPoint(point.value, "V",
                              label=f"{point.value:g} V dc from the "
                                    f"calibrator"),
            body=("Sourced mode: the calibrator stands in for the 10 V "
                  "artefact of Calibration",
                  "Manual p.42. Sense leads OFF — this is a two-terminal "
                  "connection, wired as",
                  "Figure 3-2 shows for the standard.",
                  "",
                  "The value the CAL that follows will receive is the "
                  "calibrator's READBACK,",
                  "with the calibrator's own uncertainty recorded beside it "
                  "— a certified 732B",
                  "connected by hand remains the better reference when there "
                  "is one.",
                  "",
                  "This step commits nothing: it sources the point, selects "
                  "the function and",
                  "reads it back, so a lead in the wrong jack is found now "
                  "rather than written",
                  "into calibration memory. The calibrator returns to "
                  "standby afterwards."))

    def _sourced_dc_gain_step(self) -> AdjustStep:
        return AdjustStep(
            id="dc-gain",
            prompt="Running the dc gain adjustment against the calibrator. "
                   "About 2 minutes.",
            action=self._sourced_adjust_action(
                step_id="dc-gain", point=SOURCED_POINT_10V, label="dc gain",
                unit="V", configure=self._configure_dcv),
            writes_nvram=True,
            commands=(f"OUT {SOURCED_POINT_10V.value:.8E} V; OPER",
                      f"CAL {SOURCED_POINT_10V.value:.10G}  (the "
                      f"calibrator's readback replaces this value)"),
            body=("Calibration Manual p.42, step 4, with the calibrator "
                  "standing in for the",
                  "standard: it is re-energized, read back, and the CAL is "
                  "sent with the exact",
                  "READBACK value — never the nominal. The calibrator stays "
                  "in OPERATE while",
                  "the meter measures it, and returns to standby when the "
                  "adjustment ends,",
                  "however it ends."),
            confirm="writing the dc gain constant and the value of the "
                    "internal 7 V reference to the 3458A's NON-VOLATILE "
                    "calibration memory (CAL), overwriting the existing ones "
                    "irreversibly — against the calibrator's 10 V, not a "
                    "certified artefact",
            aftercare=("The calibrator was put in standby but is still "
                       "connected to the FRONT",
                       "Input terminals. Check its front panel before "
                       "touching the leads."),
            dry_run_note=(
                "dry run: no CAL was sent, so the dc gain constant and the "
                "stored value of the internal 7 V reference are unchanged."))

    def _sourced_ohms_standard_step(self) -> AdjustStep:
        point = SOURCED_POINT_10KOHM

        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            source = ctx.require_source()
            sent: list[str] = []
            try:
                # FOUR_WIRE on the point makes the adapter assert remote
                # sense (EXTSENSE on a 5700A): the meter is on OHMF and a
                # two-wire sourcing would put the lead resistance into an
                # irreversible CAL.
                state = source.apply(point)
                actual = float(state.actual_value)
                sent.append(_applied_line(point, state))
                # p.44 step 2: OCOMP ON, exactly as in artefact mode.
                ctx.device.set_ocomp(True)
                sent.append("OCOMP ON")
                ctx.device.ohmf(actual)
                sent.append(f"OHMF {actual:.2E}")

                # The p.44 settling comparison is kept unchanged: the
                # calibrator's output plus the cabling settles no faster than
                # an artefact does.
                ctx.device.delay = OHMS_DELAY_SHORT_S
                sent.append(f"DELAY {OHMS_DELAY_SHORT_S:.2E}")
                fast = float(ctx.device.read_single_sample())
                sent.append("TARM SGL,1")
                ctx.device.delay = OHMS_DELAY_LONG_S
                sent.append(f"DELAY {OHMS_DELAY_LONG_S:.2E}")
                slow = float(ctx.device.read_single_sample())
                sent.append("TARM SGL,1")
                spread_ppm = abs(_ppm(slow, fast)) if fast else float("inf")

                if spread_ppm > OHMS_DELAY_SIGNIFICANT_PPM:
                    chosen = OHMS_DELAY_LONG_S
                    ctx.warn(
                        f"the sourced 10 kΩ reads {spread_ppm:.1f} ppm "
                        f"differently at {OHMS_DELAY_SHORT_S * 1e3:g} ms and "
                        f"{OHMS_DELAY_LONG_S:g} s of settling, so the "
                        f"adjustment will use DELAY {OHMS_DELAY_LONG_S:g} s "
                        f"(Calibration Manual p.44, note). The "
                        f"{OHMS_DELAY_SIGNIFICANT_PPM:g} ppm threshold is "
                        f"this project's; the manual says only 'significantly "
                        f"different'.")
                else:
                    chosen = OHMS_DELAY_SHORT_S
                ctx.device.delay = chosen
                sent.append(f"DELAY {chosen:.2E}")
                _require_close_to_readback(slow, actual, "CAL", "Ω")
            finally:
                _source_standby(ctx, source, step.id)
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=tuple(sent),
                response=(f"calibrator readback {actual:.9g} Ω; meter reads "
                          f"{slow:.9g} Ω, {_ppm(slow, actual):+.2f} ppm from "
                          f"it; settling spread {spread_ppm:.1f} ppm; "
                          f"adjusting with DELAY {chosen:g} s"),
                detail={"reading_ohm": slow, "source_actual_ohm": actual,
                        "source_uncertainty": state.uncertainty,
                        "source_range_id": getattr(state, "range_id", None),
                        "reading_fast_ohm": fast, "reading_slow_ohm": slow,
                        "settling_spread_ppm": spread_ppm,
                        "delay_s": chosen,
                        "deviation_ppm": _ppm(slow, actual)})

        return AdjustStep(
            id="standard-10kohm",
            prompt="Move to a 4-wire connection: calibrator OUTPUT HI/LO and "
                   "SENSE HI/LO to the FRONT panel Input HI/LO and Sense "
                   "HI/LO.",
            action=action,
            setpoint=SetPoint(point.value, "OHM",
                              label=f"{point.value:g} Ω from the calibrator, "
                                    f"4-wire"),
            body=("Sourced mode: the calibrator stands in for the 10 kΩ "
                  "artefact of Calibration",
                  "Manual p.44-45, wired 4-wire as Figure 3-3 shows for the "
                  "standard. The",
                  "connection is four-wire ON BOTH ENDS: the source asserts "
                  "remote sense, so a",
                  "missing sense pair is a fault, not a fallback.",
                  "",
                  "The calibrator's resistance is CHARACTERIZED, not "
                  "nominal: the value the CAL",
                  "that follows will receive is the readback, which can sit "
                  "hundreds of ppm from",
                  "the 10 kΩ that was asked for. Sending the nominal would "
                  "fold that difference",
                  "into the meter's resistance and dc current gains, "
                  "permanently.",
                  "",
                  "This step commits nothing. It also runs the manual's own "
                  "settling test (p.44,",
                  f"note): the output is measured at "
                  f"{OHMS_DELAY_SHORT_S * 1e3:g} ms and again at "
                  f"{OHMS_DELAY_LONG_S:g} s, and if the two",
                  "differ the adjustment is made with the longer DELAY."))

    def _sourced_resistance_step(self) -> AdjustStep:
        return AdjustStep(
            id="resistance",
            prompt="Running the resistance and dc current adjustment against "
                   "the calibrator. About 12 minutes.",
            action=self._sourced_adjust_action(
                step_id="resistance", point=SOURCED_POINT_10KOHM,
                label="resistance and dc current", unit="Ω",
                configure=self._configure_ohmf),
            writes_nvram=True,
            commands=(f"OUT {SOURCED_POINT_10KOHM.value:.8E} OHM; OPER",
                      f"CAL {SOURCED_POINT_10KOHM.value:.10G}  (the "
                      f"calibrator's readback replaces this value)"),
            body=("Calibration Manual p.45, step 5, with the calibrator "
                  "standing in for the",
                  "standard: it is re-energized 4-wire, read back, and the "
                  "CAL is sent with the",
                  "exact READBACK value — on a 5700A the ohms readback can "
                  "differ from the",
                  "nominal by up to 500 ppm, and the nominal would fold that "
                  "into the meter.",
                  "The DELAY chosen by the settling test stays in force. "
                  "This adjustment also",
                  "computes the dc current gains, and p.43 states it relies "
                  "on the dc gain",
                  "adjustment — which is why that one comes first."),
            confirm="writing the resistance and dc current gain constants to "
                    "the 3458A's NON-VOLATILE calibration memory (CAL), "
                    "overwriting the existing ones irreversibly — against "
                    "the calibrator's characterized 10 kΩ, not a certified "
                    "artefact",
            aftercare=("The calibrator was put in standby but is still "
                       "connected 4-wire. Check",
                       "its front panel before touching the leads."),
            dry_run_note=(
                "dry run: no CAL was sent, so the resistance and dc current "
                "gain constants are unchanged."))

    def _sourced_adjust_action(self, *, step_id: str, point: Point,
                               label: str, unit: str,
                               configure) -> "StepAction":
        """The shared shape of a sourced committing step.

        Pre-wire guards first -- so a refusal is recorded as "nothing was
        sent" and it is true of the calibrator as well as the meter -- then
        energize, re-check the wiring against the readback, send ``CAL`` with
        the readback, and go to standby from a ``finally`` whatever happened.
        """
        provisional = f"CAL <{label} readback>"

        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            # Every guard that can refuse without I/O runs before the
            # calibrator is energized.  adjust_step re-runs them harmlessly.
            self.require_order(ctx, step_id)
            require_write_enabled(ctx, provisional)
            require_not_freerunning(ctx, provisional)
            require_valid_security_code(ctx, provisional)

            source = ctx.require_source()
            sent: list[str] = []
            try:
                # §9: the meter is on its fixed range, chosen from the
                # NOMINAL, before the calibrator is asked for anything; the
                # readback re-targets it below, which is what the
                # characterized ohms need.
                sent.extend(configure(ctx, float(point.value)))
                if unit == "V":
                    # §8: the volts path is proved at a safe level first.
                    # The ohms path's own proof *is* its first reading of the
                    # standard, checked below against the readback to a far
                    # tighter window than §8's 10 %.
                    sent.extend(_prove_dcv(ctx, source, point))
                state = source.apply(point)
                actual = float(state.actual_value)
                sent.append(_applied_line(point, state))
                sent.extend(configure(ctx, actual))
                reading = float(ctx.device.read_single_sample())
                sent.append("TARM SGL,1")
                _require_close_to_readback(reading, actual, provisional, unit)
                result = self.adjust_step(ctx, step_id, actual, label)
            finally:
                # Standby on every exit path: success, a wiring refusal, a
                # driver failure, a lockout.  The output must not be live
                # while an operator investigates any of them.
                _source_standby(ctx, source, step.id)
            return replace(
                result,
                commands=(*sent, *result.commands),
                detail={**result.detail,
                        "source_actual": actual,
                        "source_uncertainty": state.uncertainty,
                        "source_range_id": getattr(state, "range_id", None),
                        "trial_reading": reading,
                        "trial_deviation_ppm": _ppm(reading, actual)})

        return action

    @staticmethod
    def _configure_dcv(ctx: ProcedureContext, value: float) -> tuple[str, ...]:
        return _configure_dcv_range(ctx, value)

    @staticmethod
    def _configure_ohmf(ctx: ProcedureContext, value: float) -> tuple[str, ...]:
        # OCOMP and the DELAY the settling test chose are still in force from
        # the setup step; only the range argument tracks the fresh readback.
        ctx.device.ohmf(value)
        return (f"OHMF {value:.2E}",)

    # -- ch.3 p.45 steps 7 and 8 -----------------------------------------

    def _acal_ac_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            self.require_order(ctx, "acal-ac")
            command = "ACAL AC"
            require_write_enabled(ctx, command)
            require_not_freerunning(ctx, command)
            # **The reason this module mirrors the driver's regex at all.**
            # The line below is assembled here and written with cmd(), which
            # transmits verbatim -- so this is the one calibration write the
            # driver's own _format_security_code never sees.
            code = require_valid_security_code(ctx, command)
            started = _now()

            # Sent with cmd() rather than the driver's acal(), for two
            # reasons: acal() takes no security code (and ACAL is secured from
            # the factory), and it does not read ERR?/AUXERR? afterwards, so
            # an ACAL that failed would be recorded as having succeeded.  The
            # write gate above is this module's, because cmd() has none --
            # which is exactly why it is checked before anything is sent.
            from caldmm.drivers.hp3458a import ACAL, _decode_error_bits, \
                _ERR_BIT_NAMES, _AUXERR_BIT_NAMES, ERR_HARDWARE

            ctx.device.error_register()
            ctx.device.aux_error_register()
            ctx.device.cmd(command if code is None else f"{command},{code}")
            time.sleep(ACAL.AC.duration)
            err = ctx.device.error_register()
            auxerr = ctx.device.aux_error_register() if err & ERR_HARDWARE else 0
            if err or auxerr:
                raise ProcedureError(
                    f"ACAL AC reported ERR?={err} "
                    f"[{_decode_error_bits(err, _ERR_BIT_NAMES)}], "
                    f"AUXERR?={auxerr} "
                    f"[{_decode_error_bits(auxerr, _AUXERR_BIT_NAMES)}]. "
                    f"Treating it as FAILED. If the meter refused outright, "
                    f"check the security code (ACAL is secured from the "
                    f"factory) and jumper J132 — with J132 in the right "
                    f"position neither CAL nor ACAL runs even with the "
                    f"correct code, and clearing it means powering the meter "
                    f"down and removing the top cover.")
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(), writes_nvram=True,
                commands=(command,),
                response="ac section autocalibrated",
                operator="typed confirmation given")

        return AdjustStep(
            id="acal-ac",
            prompt="Running ACAL AC. About 2½ minutes.",
            action=action, writes_nvram=True, abort_hint=ABORT_HINT_3458A,
            commands=("ACAL AC",),
            body=("Calibration Manual p.45, step 7: 'Execute the ACAL AC "
                  "command. This",
                  "autocalibrates the multimeter's AC section since the "
                  "following AC Adjustment",
                  "is normally performed only once every two years.'",
                  "",
                  "Disconnect everything from the terminals first: an input "
                  "signal left",
                  "connected adversely affects an autocal (p.87).",
                  "",
                  "This is ACAL, not CAL — it changes nothing about absolute "
                  "accuracy. It is",
                  "marked as writing non-volatile memory because it is: the "
                  "autocal constants",
                  "are stored in continuous memory and survive power-off "
                  "(p.87)."),
            confirm="running ACAL AC, which recomputes and stores the ac "
                    "section's autocal constants in the meter's continuous "
                    "memory",
            dry_run_note=(
                "dry run: ACAL AC was NOT sent, so the ac autocal constants "
                "are unchanged. Chapter 3 p.45 step 7 asks for it after the "
                "resistance adjustment — run it by hand, or re-run with "
                "commit."))

    def _settle_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=_now(), finished_at=_now(),
                operator="acknowledged the 15-minute relay settling")

        return AdjustStep(
            id="settle",
            prompt="Let the meter sit for 15 minutes before using it.",
            action=action,
            body=("Calibration Manual p.45, step 8: 'Following the autocal, "
                  "let the instrument",
                  "sit for 15 minutes for the relays to thermally "
                  "stabilize.'",
                  "",
                  "This procedure does not sleep through it — acknowledge the "
                  "step and let the",
                  "meter stand. Readings taken before it has are not of the "
                  "meter you just",
                  "adjusted."))

    # -- finalize --------------------------------------------------------

    def finalize(self, ctx: ProcedureContext) -> ProcedureResult:
        ordered = [spec.key for spec in HP3458A_STANDARDS
                   if spec.key in ctx.standards]
        ordered += [key for key in ctx.standards if key not in ordered]
        # NOTE: ``ctx.security_code`` is deliberately absent from ``detail``
        # and from everything reachable through it.
        detail = {
            "captures": dict(ctx.captures),
            "log": tuple(ctx.log),
            "acal_age_h": getattr(ctx, "acal_age_h", None),
            "not_implemented": NOT_IMPLEMENTED_NOTES,
            # None in artefact mode.  In sourced mode the record has to say
            # what stood in for the certificates it does not contain.
            "sourced_by": getattr(getattr(ctx, "source", None),
                                  "model_id", None),
        }
        return ProcedureResult(
            model_id=self.model_id, procedure_id=self.procedure_id,
            name=self.name,
            started_at=ctx.started_at or _now(),
            finished_at=ctx.finished_at or _now(),
            dry_run=ctx.dry_run,
            steps=tuple(ctx.results),
            standards=tuple(ctx.standards[key] for key in ordered),
            ambient_c=ctx.ambient_c,
            serial=_serial_of(ctx.device),
            operator=ctx.operator,
            notes=ctx.notes,
            warnings=tuple(ctx.warnings),
            unreached=tuple(ctx.unreached),
            interrupted=ctx.interrupted,
            error=ctx.error,
            detail=detail)


def _require_close(reading: float, standard: ExternalStandard, what: str,
                   unit: str) -> None:
    """Refuse when a trial reading is nowhere near the certified value.

    A :class:`WiringError`, **not** a
    :class:`~caldmm.cal.procedure.SafetyError`: getting this far meant
    selecting a function and a range and taking readings, so the commands
    really did go out and the record must say so.  See :class:`WiringError`.
    No ``CAL`` is ever reached -- this runs in the non-committing setup step,
    and raising stops the run before the irreversible one is offered.
    """
    certified = standard.certified
    if not math.isfinite(reading):
        raise WiringError(
            f"refusing to continue to {what}: the meter returned "
            f"{reading!r} for the {standard.name}. Nothing was sent to "
            f"calibration memory.")
    limit = abs(certified) * GAIN_CHECK_FRACTION
    if abs(reading - certified) > limit:
        raise WiringError(
            f"refusing to continue to {what}: the meter reads "
            f"{reading:.9g} {unit} but the {standard.name} is certified at "
            f"{certified:.9g} {unit} — {_ppm(reading, certified) / 1e4:+.2f} "
            f"%, past the {GAIN_CHECK_FRACTION * 100:g} % this procedure "
            f"accepts. That is a wiring or a typing mistake, not a meter out "
            f"of calibration: check the leads, the Terminals switch, the "
            f"function, and that the certified value was entered in the right "
            f"units and the right decade. NOTHING WAS SENT TO CALIBRATION "
            f"MEMORY; the function, range and (for the resistance standard) "
            f"OCOMP and DELAY were changed by this check. (The threshold is "
            f"this project's gross-error gate, not a Calibration Manual "
            f"figure.)")


def _capture_identity(ctx: ProcedureContext, when: str) -> dict:
    """``CALNUM?``, ``CALSTR?`` and ``TEMP?`` into the context's captures.

    Every failure here is a **warning**, never an error: losing the before/after
    calibration number is bad, but abandoning an adjustment because a query
    would not answer is worse.
    """
    capture: dict[str, Any] = {"when": when}
    for key, method in (("calnum", "calnum"), ("cal_string", "cal_string"),
                        ("temp_c", "get_temp")):
        try:
            capture[key] = getattr(ctx.device, method)()
        except Exception as exc:                          # noqa: BLE001
            ctx.warn(f"{when}: {method}() failed: {exc}")
    ctx.captures[when] = capture
    return capture


def _describe_capture(capture: dict) -> str:
    parts = []
    if capture.get("calnum") is not None:
        parts.append(f"CALNUM? {capture['calnum']}")
    if capture.get("temp_c") is not None:
        parts.append(f"TEMP? {float(capture['temp_c']):.2f} °C")
    if capture.get("cal_string"):
        parts.append(f"CALSTR? {capture['cal_string']!r}")
    return "; ".join(parts) or "nothing could be read"


# --------------------------------------------------------------------------
# Other meters
# --------------------------------------------------------------------------
#
# A meter adjusted by stepping a programmable calibrator through its points
# (rather than from certified artefacts connected by hand, as the 3458A is)
# belongs in a module of its own that registers itself on import -- and that
# module must then be imported by ``apps/cal_verify.py``, or ``--adjust``
# reports that the model has no procedure.  ``ProcedureNotImplementedError``
# is the right answer for a model registered before its procedure is written.


register(HP3458AExternalAdjust())

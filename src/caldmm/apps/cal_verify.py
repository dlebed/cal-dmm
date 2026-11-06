"""The verification runner: open the instruments, execute a plan, stream results.

This is the operator-facing end of :mod:`caldmm.cal`.  It resolves the two
instruments, generates or loads a :class:`~caldmm.cal.plan.TestPlan`, checks it
offline, drives :func:`caldmm.cal.engine.run_plan`, prompts the operator to
re-cable at every wiring boundary, writes each row as it is measured and prints
a summary at the end.  It decides nothing: every number it prints was produced
by a module whose job that number is.

**A run is around 194 points over 1.5 to 2.5 hours across four or five
operator-attended wiring groups.**  Everything below follows from that.

Streaming and resuming are not niceties
---------------------------------------

Rows go through :func:`caldmm.cal.result.open_results`, which flushes every
row as it is written -- never ``write_results``, which truncates the file it is
given.  A crash, a dropped ssh session or a Ctrl-C at point 180 therefore costs
the current point and nothing else, and ``--resume run.csv`` reopens the same
file with ``append=True`` and restarts at the first group the file does not
already cover.  A resume refuses to append rows from a *different* plan to the
same file: two plans in one result file is a record no report could untangle.

Ambient temperature is wired, not worked around
-----------------------------------------------

:func:`caldmm.cal.verdict.evaluate` blocks a PASS where the DUT's model
declares a temperature window and no ambient was recorded.  Without a reading
every such point is ``INDETERMINATE`` **by design**.  So the runner takes
``--ambient 23.4`` as :class:`~caldmm.cal.adapters.StaticEnvironment` (and an
explicit "unknown" probe without it), reads it once at startup for
the provenance block, and -- when the DUT's model declares a window and nothing
answered -- says so loudly and makes the operator decide.

``--temperature-policy`` deliberately still defaults to ``gate``.  Defaulting it
to ``flag`` would silence the 18-28 C windows, which any lab is trivially
inside, at the cost of also silencing the much tighter 22-24 C window a 24-hour
specification requires -- exactly the case where the temperature is the claim.

The spec-ratio census goes *before* the run, not into the report
---------------------------------------------------------------

Generated points carry a note like ``spec ratio 0.00403 (hp3458a 4.03e-11 /
fluke5700a 1e-08 A)`` -- the DUT's tolerance over the reference's.  Below 1 the
reference is no better than the instrument being verified, and on a
3458A-against-5700A meter-DUT run **137 of 194 points** are below 1, 18 of them
near 0.004 and arithmetically incapable of a verdict whatever the hardware does.
The census is printed before the first point is sourced, with the run time it
represents, so the operator decides to skip those points rather than discovering
them as a wall of ``INDETERMINATE`` two hours later.  ``--min-spec-ratio`` is
the filter and it defaults to **no filtering**: a poor ratio is a fact about the
pairing, and dropping points by default would quietly shorten the plan.

Narrowing a run, out loud
-------------------------

Three flags shape which points run, and none of them is allowed to do it
quietly.  ``--min-spec-ratio`` drops points the reference cannot judge.
``--max-voltage`` is the **ceiling** -- "run this plan, but stop at 100 V" --
and it is not ``--accept-max``, which only *states* the maximum a plan already
sources so ``--yes`` may run it unattended; stating a smaller number there
refuses the run rather than shrinking it.  The ceiling is imposed in
:func:`_phase_plan`, before the plan is validated and long before the
high-voltage confirmation, so every count, estimate and prompt downstream is
about the run that will actually happen.  ``--connections`` asks for wirings
the planner would otherwise pick one of, which is what it takes to exercise
both the two- and four-wire paths of the same resistance in one run.

Each prints what it did -- including "dropped 0" -- and each is recorded in the
result file's header.  A file whose plan names 1100 V and whose rows stop at
100 V has to say which of the two it is; nothing in the row data can.

Interruption
------------

:func:`caldmm.lib.signals.setup_graceful_shutdown` installs a SIGINT handler
that does one thing: put the source in **standby**, immediately.  The file needs
nothing from it -- :class:`~caldmm.cal.result.ResultWriter` flushes every row as
it is written, so what is on disk is already complete to the last measured
point.  Device handles are closed on the way out of :func:`run` instead of from
the signal handler, and the engine's generator is closed *before* they are: the
generator's own ``finally`` calls ``standby()`` one more time, and closing the
transport out from under it turns a clean Ctrl-C into an ``OSError`` raised
during unwinding that would *replace* the ``SystemExit`` and escape every
handler here -- no resume command, no fault line, a raw traceback.  Every
shutdown step is wrapped so one failure still attempts the rest, and the whole
shutdown is idempotent, because it runs from both paths.

``--adjust`` is a different run
------------------------------

It writes the DUT's calibration memory instead of measuring it, so it shares
almost nothing with the above: no plan, no points, no result CSV, no
calibrator.  It drives a procedure registered in
:mod:`caldmm.cal.dmm_procedures`, which owns the sequence, its mandatory order
and the operator prompts, and it does the two things a runner has to do that a
framework cannot -- put the record on disk (*before* anything else, and on
every exception path) and say in plain words what state the meter is now in.
It is a **dry run by default**; ``--adjust-commit`` is the second, explicit
flag, and each write still needs the framework's typed confirmation.

Adjusting and verifying **in one invocation is refused**.  It would be
convenient and it is exactly the check that means nothing: verifying a meter
against the reference that was just used to adjust it measures the reference
against itself.  The command to run the independent verification afterwards is
printed at the end.

Exit status
-----------

``0`` the run finished and nothing FAILed; ``1`` the run finished with at least
one FAIL; ``2`` a usage error, a plan that will not validate, a refusal to start
or an operator who declined; ``3`` the run was interrupted or abandoned -- rows
already measured are on disk and the printed ``--resume`` command continues it.

``--adjust`` adds ``4``, the state of non-volatile calibration memory is
**unknown** and has to be checked by hand, and ``5``, the session record is not
where it was asked to be -- which wins over every other status, because it is
the one condition whose evidence disappears when the terminal is closed.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Sequence

from caldmm.apps.cal_analyze import (
    SPEC_RATIO_WARN,
    find_counterpart,
    format_markdown,
    load_fault_table,
    output_paths as report_output_paths,
    point_key,
    render_pdf,
    spec_ratio,
)
from caldmm.cal.adapters import (
    CURRENT_POSTS,
    StaticEnvironment,
    open_meter,
    open_source,
)
from caldmm.cal.engine import (
    FLAG_GROSS_ERROR_ABORT,
    FLAG_METER_RANGE_MISMATCH,
    FLAG_PRESTEP_FAILED,
    FLAG_STANDBY_NOT_CONFIRMED,
    FLAG_WIRING_PROOF_FAILED,
    FLAG_WIRING_PROOF_UNREADABLE,
    WARMUP_MINUTES,
    ZERO_INTERVAL_DAYS,
    EngineError,
    GroupBoundary,
    SourceFaultError,
    run_plan,
)
# Imported for the registration side effect as much as for PROCEDURE_ID: an
# adjustment entry only exists in the registry once the module that registers
# it has been imported, and ``--adjust`` looks them up by model id.  Each
# meter's procedure lives in its own module, so every one is needed -- a
# missing import here does not fail loudly, it just makes ``--adjust`` report
# that the model has no procedure.  ``tests/apps/test_cal_verify.py`` pins
# the full set in a clean subprocess.
from caldmm.cal.dmm_procedures import PROCEDURE_ID as _PROCEDURE_ID
from caldmm.cal.plan import (
    DEFAULT_DISCARDS,
    DEFAULT_PERCENTAGES,
    DEFAULT_SAMPLES,
    FREQUENCY_PRESETS,
    POINT_SETS,
    TEMPERATURE_POLICIES,
    PlanError,
    TestPlan,
    generate_plan,
    load_plan,
    needs_range_lock,
    range_lock_anchor,
    validate_plan,
)
from caldmm.cal.procedure import (
    PreflightError,
    ProcedureContext,
    ProcedureError,
    ProcedureResult,
    StepOutcome,
    get_procedure,
    needs_source,
    source_policy,
    run_procedure,
)
from caldmm.cal.quantities import Connection, Quantity, Terminals
from caldmm.cal.result import (
    RUN_KINDS,
    ResultError,
    ResultRow,
    RunHeader,
    interval_days,
    load_results,
    open_results,
    summarize_run,
    caldmm_version,
)
from caldmm.cal.roles import UNKNOWN_ENVIRONMENT, Environment
from caldmm.cal.specs import IntervalBasis, SpecError, load_model
from caldmm.cal.verdict import (
    DEFAULT_CONFIDENCE,
    DEFAULT_GUARD_FACTOR,
    DEFAULT_TUR_FLOOR,
    DecisionRule,
    Verdict,
)
from caldmm.lib.console import OperatorConsole
from caldmm.lib.formatting import (
    duration as _duration,
    fmt_g as _g,
    is_blank as _is_none,
    plural as _plural,
    si_format,
    stamp as _stamp,
)
from caldmm.lib.signals import setup_graceful_shutdown

__all__ = [
    "VerifyError",
    "WriteFailed",
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_USAGE",
    "EXIT_ABORTED",
    "EXIT_UNKNOWN",
    "EXIT_NO_RECORD",
    "SECONDS_PER_POINT",
    "HIGH_VOLTAGE_V",
    "FAULTS_KEY",
    "ADJUST_PROCEDURE_ID",
    "RunContext",
    "parse_role_spec",
    "plan_point_key",
    "wiring_instructions",
    "resume_point",
    "filter_by_spec_ratio",
    "filter_by_max_voltage",
    "resume_command",
    "adjust_exit_status",
    "adjust_only_refusals",
    "adjust_record_base",
    "adjust_refusals",
    "adjust_sequence_complete",
    "linkage_refusals",
    "format_adjust_record",
    "build_parser",
    "run",
    "main",
]


class VerifyError(RuntimeError):
    """A run that must not start: bad arguments, a bad plan, a refusal."""


class WriteFailed(RuntimeError):
    """The result file could not be written mid-run.

    Its own type because it is the one failure that must end the run the way an
    interrupt does -- the rows already on disk are intact and the operator gets
    a resume command -- rather than as the usage error :class:`VerifyError`
    prints before anything was measured.
    """


#: The run finished and no point FAILed.
EXIT_OK = 0
#: The run finished and at least one point FAILed.  A verification that finds a
#: fault has done its job, so this is distinct from an error.
EXIT_FAIL = 1
#: Usage, a plan that will not validate, a pre-flight refusal, or an operator
#: who declined a confirmation.  Nothing was measured.
EXIT_USAGE = 2
#: Interrupted or abandoned mid-run.  Rows already measured are on disk and the
#: printed ``--resume`` command continues from the right group.
EXIT_ABORTED = 3

#: Wall-clock seconds per point, low and high, for the estimate printed before a
#: run.  Taken from the measured envelope of the run this subsystem was built
#: for -- 194 points in 1.5 to 2.5 hours -- rather than computed from settling
#: times and NPLC, which would be a precise-looking number built out of
#: assumptions about GPIB latency and operator pace.  It excludes re-cabling.
SECONDS_PER_POINT = (28.0, 46.0)

#: Above this the output is dangerous and the operator is asked to say so in
#: full.  It is also the 5700A's own threshold: a queued fault *silently*
#: inhibits ``OPER`` for outputs of 22 V and over.
HIGH_VOLTAGE_V = 22.0

#: The header key a run's fault queue is recorded under, read back by
#: ``apps.cal_analyze.parse_faults``.  **Bare codes, comma separated** -- the
#: analyzer decodes them from the offline table, so nothing has to survive the
#: file, and 21 of the 191 real fault messages contain a comma.
FAULTS_KEY = "faults"

#: The identity query per model.  Not a guess and not a default that happens to
#: work: the 3458A predates SCPI and answers ``ID?``, while ``*IDN?`` earns an
#: error beep and a timeout.  Keyed by *model id* -- never by the operator's
#: instrument name, which says nothing about the model.
_IDN_QUERY: dict[str, str] = {"hp3458a": "ID?"}
_DEFAULT_IDN_QUERY = "*IDN?"

#: How a wiring group is described to the operator, per connection.
_WIRING: dict[Connection, str] = {
    Connection.TWO_WIRE:
        "2-wire: calibrator OUTPUT HI/LO to the DUT terminals, sense off",
    Connection.FOUR_WIRE:
        "4-wire: calibrator OUTPUT *and* SENSE to the DUT's input and sense "
        "terminals (the calibrator asserts EXTSENSE)",
    Connection.TWO_WIRE_RCOMP:
        "2-wire with RCOMP: calibrator OUTPUT only; the calibrator nulls the "
        "lead resistance itself",
}

#: Which terminals a quantity uses, said out loud because it is the one thing
#: an operator can get wrong at a boundary without any instrument noticing.
#:
#: The two current quantities are **not** here: which binding post carries the
#: current is a non-volatile setting of the instrument (``CUR_POST``, retained
#: through power-off, [OM Table 5-2, p.176]) and not a property of the plan, so
#: their line is rendered from what the calibrator answers -- see
#: :func:`_current_terminals`.
_TERMINALS: dict[Quantity, str] = {
    Quantity.DCV: "voltage: the calibrator's OUTPUT terminals",
    Quantity.ACV: "voltage: the calibrator's OUTPUT terminals",
    Quantity.WBV: "wideband: the WIDEBAND Type-N output (option 5700A-03) "
                  "through its supplied cable, terminated in 50 ohm at the "
                  "DUT input (it is specified into 50 ohm at the end of that "
                  "cable)",
    Quantity.RES: "resistance: the calibrator's OUTPUT (and SENSE in 4-wire)",
}

#: The two current quantities, whose group prompt names the post in force.
_CURRENT_QUANTITIES = (Quantity.DCI, Quantity.ACI)

#: How a wiring group is named in the plan summary.
_TERMINAL_NAMES: dict[Terminals, str] = {
    Terminals.VOLTAGE: "voltage/ohms terminals",
    Terminals.CURRENT: "current terminals",
    Terminals.WIDEBAND: "wideband output",
}

#: The one cable set a voltage/ohms group runs through, said once at its
#: prompt.  dc volts, ac volts and both resistance wirings share it: the
#: calibrator switches EXTSENSE itself (on for 4-wire ohms, off otherwise) and
#: the meter's ohms-sense inputs are simply unused on the other functions.
_VOLTAGE_GROUP_WIRING = (
    "one cable set for the whole group: calibrator OUTPUT HI/LO to the "
    "meter's HI/LO inputs, calibrator SENSE HI/LO to the meter's ohms SENSE "
    "HI/LO inputs, calibrator GUARD to the meter's GUARD; nothing is touched "
    "again until the current group")
_VOLTAGE_GROUP_WIRING_2W = (
    "one cable set for the whole group: calibrator OUTPUT HI/LO to the "
    "meter's HI/LO inputs, calibrator GUARD to the meter's GUARD; nothing is "
    "touched again until the next group")

#: The one manual step of a full verification.
_CURRENT_GROUP_WIRING = (
    "move the lead on the meter's HI input to its current (I) input; LO, "
    "guard and the sense leads stay where they are.  Sense is off; the "
    "current returns through OUTPUT LO whichever post sources it")

#: What each ``CUR_POST`` token means at the binding posts, from
#: [OM Table 5-2, p.176] ("NORMAL selects the OUTPUT HI binding post; AUX
#: selects the AUX CURRENT OUTPUT binding post") and [OM §4-27, p.114].
_CURRENT_POST_TERMINALS: dict[str, str] = {
    "NORMAL": "the calibrator's OUTPUT HI and OUTPUT LO posts",
    "AUX": "the calibrator's AUX CURRENT OUTPUT post and OUTPUT LO",
    "IB5725": "the 5725A amplifier's own output posts",
}

#: Hours of warm-up each model's published specifications are stated after.
#: Every verdict is a statement about a specification whose stated conditions
#: were shown to hold, and this is one of them -- 3458A datasheet p.45
#: ("Warm-up time 4 hours to published specifications"), 34401A user's guide
#: p.216 ("Specifications are for 1-hour warm-up at 6 1/2 digits"), 34420A
#: user's guide p.5 ("after 2-hour warm-up"), 1281 user manual p.295
#: ("WARM UP 4 hours to full accuracy").  The meters cannot report their
#: power-on time, so this can only ever be an operator acknowledgement -- and
#: the record says which it was.
METER_WARMUP_HOURS: dict[str, float] = {
    "hp3458a": 4.0,
}

#: Half-width of the 5700A's own stated temperature window, in kelvin: every
#: one of its specification tables is headed "+/- 5 C from calibration
#: temperature" and its tempco "does not apply unless operating more than
#: +/- 5 C from calibration temperature" ([OM p.35], lines 1301-1302).  The
#: verdict side of this is the engine's; the runner reports the departure
#: before the run and records it.
SOURCE_TEMP_HALF_WIDTH_C = 5.0

#: Put on every row of a run whose meter warm-up the operator did not confirm.
#: The engine cannot know it -- no meter here reports its power-on time -- so
#: the runner, which asked, is what carries the answer onto the rows.
FLAG_METER_WARMUP_UNCONFIRMED = "meter-warmup-unconfirmed"

#: Put on every row of a run started with the meter's internal temperature
#: more than :data:`METER_TCAL_HALF_WIDTH_C` from its last adjustment
#: temperature.  "If you choose to verify performance when the temperature is
#: not within 5 C, you must recalculate all test limits" (3458A Cal Manual
#: p.56); the data sheet's figures are written for Tcal +/- 5 C.
FLAG_METER_OUTSIDE_TCAL = "meter-outside-tcal-window"

#: 3458A Cal Manual p.57 step 3: within 5 C of the adjustment temperature.
METER_TCAL_HALF_WIDTH_C = 5.0

#: Put on every row of a meter-under-test run whose reference calibrator is
#: past its own calibration interval (``CAL_DAYS?`` > ``CAL_INTV?``).  The
#: verdicts are still stated -- the operator's call -- but its ``UNCERT?``
#: is computed for an interval that has elapsed, so they carry this caveat.
FLAG_REFERENCE_CAL_OVERDUE = "reference-calibration-overdue"

#: The 3458A's 90-day to 2-year figures hold "within 24 hours and +/- 1 C of
#: last ACAL" (data sheet p.12, p.17, p.21, p.25, p.28, p.33).
ACAL_VALID_HOURS = 24.0
ACAL_HALF_WIDTH_C = 1.0
#: The unmet-condition code when that cannot be shown.
FLAG_ACAL_NOT_ESTABLISHED = "acal-condition-not-established"


def _wall_clock() -> datetime:
    """The engine's default clock: timezone-aware local time."""
    return datetime.now().astimezone()

#: Which ``CAL?`` adjustment temperature governs each quantity: ``CAL? 59``
#: (``CAL 10``) for the voltage functions, ``CAL? 60`` (``CAL 10K``) for ohms
#: and current -- the Cal Manual's DC VOLTAGE, OHMS and DC CURRENT test
#: cards say so ("CAL? 59", "CAL? 60"; p.60, p.68, p.72).
METER_TCAL_CONSTANT = {
    Quantity.DCV: "dcv", Quantity.ACV: "dcv",
    Quantity.RES: "ohms", Quantity.DCI: "ohms", Quantity.ACI: "ohms",
}

#: The Cal Manual's post-ACAL settling time for ``ACAL ALL`` (``ACAL``
#: reference: "settling time is required after each autocal routine: ALL: 30
#: minutes").
ACAL_SETTLE_MINUTES = 30.0


# --------------------------------------------------------------------------
# Small formatting helpers
# --------------------------------------------------------------------------

def _q(value, unit: str, digits: int, blank: str = "n/a") -> str:
    """A physical quantity with an SI prefix: ``8.5e-06`` V -> ``8.50000 uV``.

    ``digits`` has **no default**, deliberately.  ``apps.cal_analyze`` carries a
    twin of this function whose default is 4 while this one's was 6; every call
    site here passes ``digits`` explicitly, so the divergence was invisible and
    would have surfaced the first time someone added a call without it -- as two
    renderings of the same number in the runner and in the report of the run.
    """
    if _is_none(value):
        return blank
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return si_format(float(value), digits, unit)


def _opt(value, unit: str = "", fmt=str, blank: str = "unknown") -> str:
    """A value with its unit, or "unknown" -- **with the unit inside**.

    ``unknown d`` reads like a measurement whose number went missing.
    """
    return blank if _is_none(value) else f"{fmt(value)}{unit}"


def _rule(title: str = "", width: int = 78) -> str:
    if not title:
        return "-" * width
    head = f"--- {title} "
    return head + "-" * max(3, width - len(head))


@lru_cache(maxsize=1)
def _git_revision() -> str:
    """The working tree's revision, for the provenance block.

    Best effort and never fatal: a run started from an installed wheel with no
    ``.git`` at all must record an empty revision, not die at the desk.
    """
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5.0, check=False)
    except (OSError, subprocess.SubprocessError):   # pragma: no cover - no git
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _ask(device, command: str) -> str:
    """One query, or ``""``.  Provenance must never take a run down."""
    query = getattr(device, "query", None)
    if query is None:
        return ""
    try:
        return str(query(command)).strip()
    except Exception:                              # noqa: BLE001 - provenance
        return ""


def _call(obj, name: str, *args):
    """Call an optional capability, or return ``None``.

    The runner is driver-agnostic by construction -- everything it *needs* comes
    through the role protocols -- but a report is much better for a ``CALNUM``
    and an internal temperature, and those exist on some meters and not others.
    """
    method = getattr(obj, name, None)
    if method is None:
        return None
    try:
        return method(*args)
    except Exception:                              # noqa: BLE001 - provenance
        return None


# --------------------------------------------------------------------------
# Argument shapes
# --------------------------------------------------------------------------

def parse_role_spec(text: str, option: str) -> tuple[str, str]:
    """``MODEL[:NAME]:ADDRESS`` -> ``(model_id, "name:address")``.

    Two pieces of information travel here and they are **not** the same piece.
    The model id selects the specification file and the adapter; the name is an
    operator-chosen designator (``dmm1`` and ``dmm2`` distinguish two
    units of the same model, not two channels) and says nothing about the model.
    Inferring one from the other is the mistake
    :func:`caldmm.cal.adapters.open_meter`'s two-argument signature exists to
    make impossible, so the CLI carries both.

    ``fluke5700a:4`` is the two-field form: the model doubles as the name, which
    is right for the single 5700A on a bench.  ``hp3458a:dmm1:22`` names the
    unit as well.
    """
    parts = [part.strip() for part in str(text).split(":")]
    if len(parts) == 2 and all(parts):
        model, address = parts
        return model, f"{model}:{address}"
    if len(parts) == 3 and all(parts):
        model, name, address = parts
        return model, f"{name}:{address}"
    raise VerifyError(
        f"{option}: expected MODEL:ADDRESS or MODEL:NAME:ADDRESS, got "
        f"{text!r}.  The model id chooses the specification file and the "
        f"adapter; the name is your own designator for the unit and cannot be "
        f"used to infer one")


def _load_spec(model_id: str, option: str):
    try:
        return load_model(model_id)
    except (SpecError, FileNotFoundError, OSError) as exc:
        raise VerifyError(f"{option}: {exc}") from exc


def _floats(text: str | None, option: str) -> tuple[float, ...] | None:
    if text is None:
        return None
    values = []
    for item in str(text).replace(",", " ").split():
        try:
            values.append(float(item))
        except ValueError as exc:
            raise VerifyError(f"{option}: {item!r} is not a number") from exc
    if not values:
        raise VerifyError(f"{option}: no values given")
    return tuple(values)


def _frequencies(text: str | None):
    """``--frequencies``: a list of tones, or one of the plan's presets.

    ``manufacturer`` emits the specification break points the manufacturers'
    own verification tables use instead of the default centre-of-band ladder;
    everything else is read as a list of numbers, as before.
    """
    if text is not None and text.strip().lower() in FREQUENCY_PRESETS:
        return text.strip().lower()
    return _floats(text, "--frequencies")


def _connections(text: str | None) -> tuple[str, ...] | None:
    """``--connections``: the wirings to plan, or ``None`` for the default.

    Deliberately **not** validated here.  ``plan._resolve_connections`` already
    refuses an unknown name with a message that lists the ones the
    :class:`~caldmm.cal.quantities.Connection` enum defines, and that message
    reaches the operator through :func:`_generate`'s ``PlanError`` handler.  A
    second copy of the list here is a second thing to forget to update when a
    wiring is added.
    """
    if text is None:
        return None
    names = [item for item in str(text).replace(",", " ").split() if item]
    if not names:
        raise VerifyError("--connections: no connections given")
    return tuple(names)


def _quantities(text: str | None) -> tuple[str, ...] | None:
    if text is None:
        return None
    names = [item for item in str(text).replace(",", " ").split() if item]
    if not names:
        raise VerifyError("--quantities: no quantities given")
    for name in names:
        try:
            Quantity(name)
        except ValueError as exc:
            raise VerifyError(
                f"--quantities: unknown quantity {name!r} (known: "
                f"{', '.join(q.name for q in Quantity)})") from exc
    return tuple(names)


# --------------------------------------------------------------------------
# Plan identity, for the resume check
# --------------------------------------------------------------------------

def plan_point_key(point) -> tuple:
    """Identity of a *plan* point, in the same shape as a result row's.

    The counterpart of ``apps.cal_analyze.point_key``, which answers the same
    question about a :class:`~caldmm.cal.result.ResultRow`.  They have to agree
    exactly -- that is what lets a resume prove the rows already in a file were
    measured from the plan about to be continued -- and the only difference is
    the name of the setpoint field: a plan point calls it ``value``, a row calls
    it ``nominal`` (and keeps the *readback* in ``source_value``).

    ``range_arg`` is part of it, as it is in ``plan._identity`` and
    ``point_key``: a meter-DUT plan pins the meter's range with it and carries
    no ``range_id``, so without it every setpoint that appears on two ranges --
    all five ``DCV 0 V`` points of the shipped 3458A plan -- collapses into one.
    """
    return (point.quantity.name, repr(float(point.value)),
            repr(float(point.frequency_hz or 0.0)), point.connection.value,
            point.range_id or "",
            "" if point.range_arg is None else repr(float(point.range_arg)))


#: The label every safe-level wiring-proof row starts with
#: (:meth:`caldmm.cal.engine._Executor` writes one only when the proof failed,
#: could not be read, or its standby did not confirm).
PROOF_ROW_LABEL_PREFIX = "wiring proof ("


def is_proof_row(row: ResultRow) -> bool:
    """Whether *row* is a wiring proof rather than a measurement of its point.

    A proof row carries the ``point_index`` of the point it was guarding, but
    that point was **not** measured -- the run stopped before it.  Treating it
    as the point is how a resume skips that point (otherwise the proof before a
    ``DCI 0 A`` point would have hidden it) and how the
    identity check refuses a file its own plan wrote.
    """
    return str(row.label or "").startswith(PROOF_ROW_LABEL_PREFIX)


def resume_point(plan: TestPlan,
                 rows: Sequence[ResultRow]) -> tuple[int, int]:
    """``(group index, offset within it)`` of the first point not yet measured.

    **By point index, never by counting rows.**  Counting is what a run resumed
    twice inside one group defeats: a file that re-measured two points has more
    rows in that group than the group has points, the count reaches ``len(group)``
    while the group's tail was never taken, and the runner reports a complete
    run -- with a row count matching the plan exactly, so nothing downstream can
    tell.  Two points of a calibration certificate silently never measured is
    the worst thing this program could do.

    The offset is returned, not just the group, because
    :func:`caldmm.cal.engine.run_plan` takes ``start_point``: a partially
    measured group is *continued*, not restarted, so a resume adds no duplicate
    rows.

    The scan stops at the first **gap**, which is deliberately stricter than
    "the first incomplete group": rows are written in plan order, so a hole in
    the middle means something went wrong, and re-measuring the tail after it is
    the safe direction.
    """
    measured = {row.point_index for row in rows if not is_proof_row(row)}
    groups = plan.groups()
    for index, group in enumerate(groups):
        for offset in range(len(group)):
            if group.start + offset not in measured:
                return index, offset
    return len(groups), 0


def resume_command(csv_path: str, start_group: int,
                   argv: Sequence[str] | None = None) -> str:
    """The exact command line that continues this run.

    Built from the *real* ``sys.argv`` with ``--resume``, ``--start-group`` and
    ``-o`` removed, so the plan-shaping options an operator actually typed --
    ``--min-spec-ratio``, ``--quantities``, ``--percentages`` -- come back
    unchanged.  A resume that regenerated a different plan is precisely what the
    identity check refuses, and the operator should not have to reconstruct the
    flags from memory two hours in.

    The ``--start-group`` it prints is the group the file's own rows put the run
    in, and re-running the command **does not** re-measure that group's
    completed points: an explicit ``--start-group`` equal to the computed one
    keeps the computed offset within it (see :func:`_phase_resume`).  The
    program name is the console script's, not ``sys.argv[0]`` -- under
    ``python -m`` that is ``cal_verify.py``, which is not a command anyone can
    paste.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    dropped = {"--resume", "--start-group", "-o", "--csv"}
    kept: list[str] = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        head = item.split("=", 1)[0]
        if head in dropped:
            skip = "=" not in item
            continue
        kept.append(item)
    return " ".join(["caldmm-cal-verify", *kept, "--resume", csv_path,
                     "--start-group", str(start_group)])


# --------------------------------------------------------------------------
# The run's state
# --------------------------------------------------------------------------

@dataclass
class RunContext:
    """One run's mutable state, threaded through the phase functions."""

    args: argparse.Namespace
    console: OperatorConsole = field(default_factory=OperatorConsole)
    #: Injected for testing; a suite that actually slept for 200 points would
    #: not be run.
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] | None = None

    # -- offline
    source_model_id: str = ""
    meter_model_id: str = ""
    source_instrument: str = ""
    meter_instrument: str = ""
    source_spec: Any = None
    meter_spec: Any = None
    plan: TestPlan | None = None
    plan_name: str = ""
    #: How many points ``--min-spec-ratio`` removed; recorded in the header.
    dropped_by_ratio: int = 0
    #: How many points ``--max-voltage`` removed, and the largest magnitude
    #: among them.  Both recorded in the header: a file whose plan names 1100 V
    #: and whose rows stop at 100 V has to say which of the two it is.
    dropped_by_max_voltage: int = 0
    highest_dropped_v: float = 0.0
    csv_path: str = ""
    run_id: str = ""
    started: datetime | None = None
    start_group: int = 0
    #: Offset within :attr:`start_group` of the first point to measure.
    start_point: int = 0
    appending: bool = False
    #: Rows already in the file when a run was resumed.
    prior_rows: tuple[ResultRow, ...] = ()

    # -- hardware
    source: Any = None
    meter: Any = None
    #: The meter's identity string, read once by ``--adjust`` for the record.
    adjust_idn: str = ""
    probe: Any = None
    environment: Environment = UNKNOWN_ENVIRONMENT
    health: Any = None
    header: RunHeader | None = None

    # -- during the run
    writer: Any = None
    rows: Any = None
    measured: list[ResultRow] = field(default_factory=list)
    group_index: int = 0
    shut_down: bool = False
    #: What ``CUR_POST?`` answered after the post was set for a current group.
    current_post: str = ""
    #: Whether the operator confirmed the meter's warm-up.  ``None`` until
    #: they were asked; ``False`` is recorded on every row.
    meter_warm_confirmed: bool | None = None
    #: ``|ambient - CAL_TEMP?|`` where both are known and the source is the
    #: DUT, in kelvin.  Only set when it is outside the stated window.
    source_temp_departure_c: float | None = None
    #: The 3458A's ``TEMP?`` and its ``CAL? 58/59/60`` adjustment
    #: temperatures, read at the start of the run (``{}`` for a meter that
    #: cannot report them).
    meter_temperatures: dict = field(default_factory=dict)
    #: Departures of ``TEMP?`` from the adjustment temperatures that apply to
    #: this plan, where one exceeds the Cal Manual's 5 C -- set only when the
    #: operator forced the run anyway; every row then carries
    #: :data:`FLAG_METER_OUTSIDE_TCAL`.
    meter_temp_departures: dict = field(default_factory=dict)
    #: ``ACAL`` run by this session before the first point ("" for none), and
    #: the ``TEMP?`` read after its settling time.
    acal_done: str = ""
    acal_temp_c: float | None = None
    #: When this session's ACAL finished, on the run's own clock.
    acal_at: datetime | None = None
    #: The operator's answer to "was ACAL run within 24 h and 1 C?" when the
    #: run did not run one itself (``None``: not asked).
    acal_confirmed: bool | None = None
    #: Days past the reference calibrator's own CAL_INTV, when it is overdue.
    reference_overdue_days: int | None = None
    #: The entry-limit pairs this run armed, rendered, for the header.
    armed_limits: tuple[str, ...] = ()
    #: ``LIMIT?`` as it was before the run, restored at shutdown.
    previous_limits: tuple = None
    #: Signal dispositions displaced by the termination handlers.
    previous_signals: dict = field(default_factory=dict)

    @property
    def extra_row_flags(self) -> tuple[str, ...]:
        """Run-level flags the engine cannot know, added to every row.

        The engine sees the instruments; it does not see the operator's
        answer about when the meter was switched on.
        """
        flags: list[str] = []
        if self.meter_warm_confirmed is False:
            flags.append(FLAG_METER_WARMUP_UNCONFIRMED)
        if self.meter_temp_departures:
            flags.append(FLAG_METER_OUTSIDE_TCAL)
        if self.reference_overdue_days is not None:
            flags.append(FLAG_REFERENCE_CAL_OVERDUE)
        return tuple(flags)

    # -- identity ---------------------------------------------------------

    @property
    def dut_is_meter(self) -> bool:
        return self.plan is None or self.plan.dut_is_meter

    @property
    def dut_model_id(self) -> str:
        return self.meter_model_id if self.dut_is_meter else self.source_model_id

    @property
    def dut_spec(self):
        return self.meter_spec if self.dut_is_meter else self.source_spec

    @property
    def total_points(self) -> int:
        return 0 if self.plan is None else len(self.plan.points)

    def resume_hint(self) -> str:
        return resume_command(self.csv_path, self.group_index)


# --------------------------------------------------------------------------
# Phase 1 -- the plan, entirely offline
# --------------------------------------------------------------------------

def _phase_plan(ctx: RunContext) -> None:
    """Resolve the two models, build or load the plan, and validate it.

    Nothing here touches an instrument.  Doing it at the desk is the whole
    point: the alternative is finding out three hours in, with the calibrator
    warm and the leads connected.
    """
    args = ctx.args
    if not args.source:
        raise VerifyError(
            "--source is required for a verification run: it names the "
            "instrument that sources every point and supplies the reference "
            "half of the uncertainty budget.  (It is optional only because "
            "--adjust drives no source.)")
    ctx.source_model_id, ctx.source_instrument = parse_role_spec(args.source,
                                                                "--source")
    ctx.meter_model_id, ctx.meter_instrument = parse_role_spec(args.meter,
                                                               "--meter")
    ctx.source_spec = _load_spec(ctx.source_model_id, "--source")
    ctx.meter_spec = _meter_option(
        _load_spec(ctx.meter_model_id, "--meter"),
        getattr(ctx.args, "meter_option", None))

    if args.plan:
        _conflicting_generation_flags(args)
        try:
            ctx.plan = load_plan(args.plan)
        except (PlanError, OSError) as exc:
            raise VerifyError(f"--plan: {exc}") from exc
        ctx.plan_name = ctx.plan.name or os.path.basename(args.plan)
    else:
        ctx.plan = _generate(ctx)
        ctx.plan_name = ctx.plan.name

    _apply_max_voltage(ctx)

    problems = validate_plan(ctx.plan, ctx.source_spec, ctx.meter_spec)
    if problems:
        listed = "\n  ".join(problems[:20])
        more = ("" if len(problems) <= 20
                else f"\n  ... and {len(problems) - 20} more")
        raise VerifyError(
            f"the plan will not run against {ctx.source_model_id} and "
            f"{ctx.meter_model_id}:\n  {listed}{more}")


def _apply_max_voltage(ctx: RunContext) -> None:
    """Impose ``--max-voltage`` on the plan, before anything else sees it.

    Ordering is the whole point.  The ceiling runs **before**
    :func:`validate_plan`, so a point the pair cannot realise above the ceiling
    is dropped rather than refusing the run the operator narrowed to avoid it;
    and before :func:`_confirm_high_voltage`, so ``--accept-max`` and the typed
    confirmation are both asked about the reduced plan's maximum, not the
    original's.  Everything downstream -- the dry-run listing, the group
    census, the duration estimate, the resume identity check -- reads the
    filtered plan for free.

    The counts are kept on *ctx* rather than printed here: this phase is
    silent, and :func:`_phase_describe` is where a run says what it is about to
    do.  They are not optional -- a plan quietly shortened is exactly the
    failure this subsystem is written against.
    """
    ceiling = getattr(ctx.args, "max_voltage", None)
    if ceiling is None:
        return
    ceiling = float(ceiling)
    if not math.isfinite(ceiling) or ceiling < 0.0:
        raise VerifyError(
            f"--max-voltage {ceiling:g}: the ceiling is an output magnitude in "
            f"volts and must be a finite number that is not negative")
    before = _max_voltage(ctx.plan)
    ctx.plan, dropped = filter_by_max_voltage(ctx.plan, ceiling)
    ctx.dropped_by_max_voltage = len(dropped)
    ctx.highest_dropped_v = max((abs(float(p.value)) for p in dropped),
                                default=0.0)
    if not ctx.plan.points:
        raise VerifyError(
            f"--max-voltage {ceiling:g} left no points to run: every point of "
            f"this plan sources more than {ceiling:g} V (the plan reaches "
            f"{_q(before, 'V', 4)}).  Raise the ceiling, or choose a quantity "
            f"that is not volts")


def _meter_option(spec, option):
    """*spec* with ``--meter-option`` applied, or unchanged without one.

    The 3458A's Option 002 has tighter dc-volts figures than the standard
    instrument; judging such a unit at the standard ones is too lenient.
    """
    if not option:
        return spec
    try:
        return spec.with_option(option)
    except SpecError as exc:
        raise VerifyError(f"--meter-option {option}: {exc}") from exc


def _conflicting_generation_flags(args: argparse.Namespace) -> None:
    """Refuse ``--plan`` together with a flag that would be silently ignored.

    A plan file states its own interval, confidence, sample count and points.
    Accepting ``--samples 20`` alongside it and then not applying it is how a
    run quietly does something other than what the file says.
    """
    given = [name for name, value in (
        ("--quantities", args.quantities), ("--percentages", args.percentages),
        ("--frequencies", args.frequencies),
        ("--connections", args.connections),
        ("--include-negative/--no-negative", args.include_negative),
        ("--interval", args.interval), ("--samples", args.samples),
        ("--discards", args.discards), ("--confidence", args.confidence),
        ("--tur-floor", args.tur_floor), ("--guard-band", args.guard_band),
        ("--temperature-policy", args.temperature_policy),
        ("--points", getattr(args, "points", None)),
    ) if value is not None]
    if given:
        raise VerifyError(
            f"--plan carries its own points and decision rule, so "
            f"{', '.join(given)} would be ignored; edit the plan file instead")


def _generate(ctx: RunContext) -> TestPlan:
    args = ctx.args
    name = (f"{ctx.meter_model_id if args.dut == 'meter' else ctx.source_model_id}"
            f" verified against "
            f"{ctx.source_model_id if args.dut == 'meter' else ctx.meter_model_id}")
    try:
        return generate_plan(
            ctx.source_spec, ctx.meter_spec,
            dut_role=args.dut,
            quantities=_quantities(args.quantities),
            percentages=(_floats(args.percentages, "--percentages")
                         or DEFAULT_PERCENTAGES),
            include_negative=(True if args.include_negative is None
                              else bool(args.include_negative)),
            connections=_connections(args.connections),
            frequencies=_frequencies(args.frequencies),
            interval=args.interval,
            name=name,
            samples=DEFAULT_SAMPLES if args.samples is None else args.samples,
            discards=(DEFAULT_DISCARDS if args.discards is None
                      else args.discards),
            confidence=(DEFAULT_CONFIDENCE if args.confidence is None
                        else args.confidence),
            tur_floor=(DEFAULT_TUR_FLOOR if args.tur_floor is None
                       else args.tur_floor),
            guard_band_factor=(DEFAULT_GUARD_FACTOR if args.guard_band is None
                               else args.guard_band),
            temperature_policy=(args.temperature_policy or "gate"),
            point_set=(getattr(args, "points", None) or "full"))
    except (PlanError, SpecError) as exc:
        raise VerifyError(str(exc)) from exc


# --------------------------------------------------------------------------
# Phase 2 -- describe the run before it starts
# --------------------------------------------------------------------------

def filter_by_spec_ratio(plan: TestPlan, minimum: float
                         ) -> tuple[TestPlan, list]:
    """``(filtered plan, dropped points)`` -- points whose ratio is too low.

    **At or below**, matching the ``<=`` that ``cal_analyze.spec_ratio_summary``
    and the pre-run census use.  The two have to be the same comparison or the
    census says "no better than the DUT at 140 points, skip them with
    ``--min-spec-ratio 1``" and the filter then drops 137, leaving three points
    an operator was told would go.

    A point whose note carries **no** ratio is kept.  "Neither side states a
    tolerance here" is not the same finding as "the reference is worse than the
    DUT", and dropping the first because it cannot answer the second would
    silently shorten the plan for a reason the operator never asked for.
    """
    kept, dropped = [], []
    for point in plan.points:
        ratio = spec_ratio(point)
        if ratio is not None and ratio <= minimum:
            dropped.append((point, ratio))
        else:
            kept.append(point)
    return replace(plan, points=tuple(kept)), dropped


def filter_by_max_voltage(plan: TestPlan, ceiling: float
                          ) -> tuple[TestPlan, list]:
    """``(filtered plan, dropped points)`` -- voltage points above *ceiling*.

    A genuine **ceiling**, which ``--accept-max`` is not: that flag states the
    maximum a plan already sources so ``--yes`` may run it unattended, and
    stating a smaller number does not make the plan smaller, it makes the run
    refuse.  Wanting the shipped dc plan to stop at 100 V instead of 1000 V had
    no answer short of hand-authoring a plan file; ``--percentages`` picks
    fractions of each range, not an absolute limit, and a 10 % point on the
    1000 V range is still 100 V on the terminals.

    **Voltage only, by magnitude**, exactly like :func:`_max_voltage`: 1000 ohm
    is not a different kind of dangerous from 100 ohm, and both polarities of a
    dc setpoint are the same setpoint as far as a ceiling is concerned.  Points
    of every other quantity are kept -- an operator capping the volts has not
    asked to lose the resistance catalogue.

    **At** the ceiling is inside it, and a magnitude that only misses by
    floating-point noise (0.9 * 220 is not always 198.0) is treated as at it.
    A ceiling that excluded the number the operator typed would be a surprising
    reading of the word.
    """
    kept, dropped = [], []
    for point in plan.points:
        magnitude = abs(float(point.value))
        if (point.quantity.unit == "V" and magnitude > ceiling
                and not math.isclose(magnitude, ceiling, rel_tol=1e-9)):
            dropped.append(point)
        else:
            kept.append(point)
    return replace(plan, points=tuple(kept)), dropped


def _spec_ratio_census(ctx: RunContext) -> None:
    """Print how much of this run compares against a reference no better.

    Before the first point is sourced, because that is the only moment at which
    the operator can do anything about it.
    """
    plan = ctx.plan
    low = [(point, spec_ratio(point)) for point in plan.points]
    low = [(point, ratio) for point, ratio in low
           if ratio is not None and ratio <= SPEC_RATIO_WARN]
    unrated = sum(1 for point in plan.points if spec_ratio(point) is None)
    total = len(plan.points)
    reference = (ctx.source_model_id if plan.dut_is_meter
                 else ctx.meter_model_id)
    dut = plan.dut_model_id

    print()
    if not low:
        print(f"  spec ratio: every point compares against a reference better "
              f"than {dut}"
              + (f" ({unrated} of {total} state no ratio)" if unrated else ""))
        return

    worst_point, worst_ratio = min(low, key=lambda item: item[1])
    share = 100.0 * len(low) / max(total, 1)
    seconds = (len(low) * SECONDS_PER_POINT[0],
               len(low) * SECONDS_PER_POINT[1])
    counts = Counter(point.quantity.name for point, _ in low)
    print(f"  !! THE REFERENCE IS NO BETTER THAN THE DUT AT {len(low)} OF "
          f"{total} POINTS ({share:.0f} %)")
    print(f"     Their spec ratio (DUT tolerance / reference tolerance) is at "
          f"or below {SPEC_RATIO_WARN:g}, so at those points the comparison "
          f"mostly measures")
    print(f"     {reference} rather than {dut}.  Worst {worst_ratio:.3g} at "
          f"{worst_point.describe()}.")
    print("     By quantity: "
          + ", ".join(f"{name} {count}"
                      for name, count in sorted(counts.items())))
    print(f"     They are {_duration(seconds[0])} to {_duration(seconds[1])} "
          f"of this run.  Skip them with --min-spec-ratio 1.")
    if unrated:
        print(f"     ({unrated} further point(s) state no ratio at all -- one "
              f"side has no tolerance there; --min-spec-ratio keeps those.)")


def _phase_describe(ctx: RunContext) -> None:
    """Print what is about to happen: structure, census, filter, duration."""
    plan = ctx.plan
    role = "meter" if plan.dut_is_meter else "source"
    print()
    print(_rule("Plan"))
    print(f"  {ctx.plan_name}")
    print(f"  DUT       {plan.dut_model_id} (the {role})")
    print(f"  reference {plan.reference_model_id}")
    print(f"  rule      interval {plan.interval or 'model default'}, "
          f"{plan.confidence * 100:g} % confidence, TUR floor "
          f"{plan.tur_floor:g}:1, guard band g={plan.guard_band_factor:g}, "
          f"temperature policy {plan.temperature_policy}")
    print(f"  sampling  {plan.samples} samples per point, "
          f"{plan.discards} discarded first")
    min_samples = getattr(ctx.args, "min_samples", None)
    if min_samples is not None and min_samples < plan.samples:
        print(f"            early stop: from {max(1, min_samples)} samples "
              f"when spread and trend are small against u_B; otherwise all "
              f"{plan.samples}")
    if ctx.args.run_kind:
        line = f"  run kind  {ctx.args.run_kind}"
        if ctx.args.counterpart:
            line += f", counterpart {ctx.args.counterpart}"
        if ctx.args.adjust_log:
            line += f", adjustment record {ctx.args.adjust_log}"
        print(line)
        # A warning, not a refusal: the linkage is provenance recorded for
        # later, and an as-found counterpart may legitimately live on
        # another machine today.  But a typo'd path recorded silently would
        # surface as a WARNING from the analyzer weeks later, so say it now,
        # while the operator can still fix the flag.
        for label, path in (("counterpart", ctx.args.counterpart),
                            ("adjustment record", ctx.args.adjust_log)):
            if path and not os.path.exists(path):
                print(f"  !! the {label} {path} does not exist from here; "
                      f"recording it as given")

    if getattr(ctx.args, "max_voltage", None) is not None:
        # Printed before the census, because that is the order it happened in:
        # _phase_plan imposes the ceiling before the plan is validated, so
        # every count below is already a count of the reduced plan.  Said even
        # when it dropped nothing -- "the ceiling was in force and changed
        # nothing" and "no ceiling was applied" are different facts about a
        # run, and silence only prints one of them.
        ceiling = float(ctx.args.max_voltage)
        print()
        print(f"  --max-voltage {ceiling:g}: dropped "
              f"{_plural(ctx.dropped_by_max_voltage, 'point')} above "
              f"{_q(ceiling, 'V', 4)}, {len(plan.points)} remain")
        if ctx.dropped_by_max_voltage:
            print(f"     the highest dropped point was "
                  f"{_q(ctx.highest_dropped_v, 'V', 4)}; this run is NOT a "
                  f"verification of the ranges above the ceiling")

    _spec_ratio_census(ctx)

    if ctx.args.min_spec_ratio is not None:
        ctx.plan, dropped = filter_by_spec_ratio(plan,
                                                 ctx.args.min_spec_ratio)
        plan = ctx.plan
        ctx.dropped_by_ratio = len(dropped)
        print()
        print(f"  --min-spec-ratio {ctx.args.min_spec_ratio:g}: dropped "
              f"{_plural(len(dropped), 'point')}, {len(plan.points)} remain")
        if not plan.points:
            raise VerifyError(
                f"--min-spec-ratio {ctx.args.min_spec_ratio:g} left no points "
                f"to run; every point of this plan compares against a "
                f"reference below that ratio")

    groups = plan.groups()
    print()
    print(f"  {_plural(len(plan.points), 'point')} in "
          f"{_plural(len(groups), 'wiring group')}:")
    for index, group in enumerate(groups):
        print(f"    group {index + 1}/{len(groups)}  "
              f"{_TERMINAL_NAMES.get(group.terminals, group.label)}: "
              f"{len(group)} points from #{group.start}")
        offset = group.start
        for quantity, connection, count in group.segments:
            values = [abs(float(point.value))
                      for point in ctx.plan.points[offset:offset + count]]
            offset += count
            print(f"        {quantity.name} {connection.value:<4} {count:>4} "
                  f"points  {_q(min(values), quantity.unit, 3)} to "
                  f"{_q(max(values), quantity.unit, 3)}")
    low, high = (len(plan.points) * SECONDS_PER_POINT[0],
                 len(plan.points) * SECONDS_PER_POINT[1])
    print(f"  estimated {_duration(low)} to {_duration(high)} of instrument "
          f"time, plus {_plural(len(groups), 'wiring prompt')} (the first is "
          f"the initial connection)")

    _connections_census(ctx, plan)

    if plan.skipped:
        counts = plan.skipped_counts()
        print(f"  not planned: "
              f"{_plural(len(plan.skipped), 'candidate point')} ("
              + ", ".join(f"{name} {count}"
                          for name, count in sorted(counts.items()))
              + "); --dry-run lists them with reasons")
        if ctx.args.dry_run:
            for entry in plan.skipped:
                print(f"    - {entry}")

    maximum = _max_voltage(plan)
    if maximum >= HIGH_VOLTAGE_V:
        print(f"  !! this plan sources up to {_q(maximum, 'V', 4)} -- lethal, "
              f"and above the 22 V threshold at which a queued fault silently "
              f"inhibits the output")


def _connections_census(ctx: RunContext, plan: TestPlan) -> None:
    """Say which of the requested wirings actually reached the plan.

    Only when ``--connections`` was given: the default is one wiring per
    quantity, chosen by the planner, and the group listing above already names
    it.  Asking for a wiring that neither instrument offers on a quantity is
    not an error -- ``4w,2w`` across a run whose dc volts is two-wire only is a
    perfectly sensible thing to type -- but a request that reached **no** point
    at all must not pass in silence, because the operator asked for a physical
    path to be exercised and it was not.  ``plan.skipped`` already holds the
    per-quantity reason, from ``plan._resolve_connections``; this points at it.
    """
    requested = _connections(getattr(ctx.args, "connections", None))
    if not requested:
        return
    planned = {point.connection.value for point in plan.points}
    print(f"  wiring    requested {', '.join(requested)}; planned "
          + (", ".join(sorted(planned)) if planned else "none"))
    unreached = [name for name in requested
                 if str(name).strip().lower() not in planned]
    if unreached:
        print(f"  !! no point is wired {', '.join(unreached)}: neither "
              f"instrument offers it on any quantity in this run.  Nothing "
              f"was substituted; --dry-run lists the candidates it cost")


def _max_voltage(plan: TestPlan, start_group: int = 0) -> float:
    """The largest voltage setpoint *this run* will source, or ``0``.

    Voltage only.  1000 V is a different kind of number from 1000 ohm, and a
    confirmation prompt that fired on the resistance catalogue would be trained
    away within a week -- which is also why it is taken from *start_group*
    onwards: a run resumed into the resistance group will not put a volt on the
    terminals, and asking about the 1000 V the first session already did is the
    same prompt-fatigue in slower motion.
    """
    groups = plan.groups()[start_group:]
    values = [abs(float(point.value)) for group in groups
              for point in group.points if point.quantity.unit == "V"]
    return max(values, default=0.0)


# --------------------------------------------------------------------------
# Phase 3 -- resume
# --------------------------------------------------------------------------

def _phase_resume(ctx: RunContext) -> bool:
    """Set the output path, the run id and the starting group.

    Returns False when the file already covers every group, so the caller can
    stop rather than open two instruments and measure nothing.
    """
    args = ctx.args
    ctx.started = datetime.now().astimezone()

    if not args.resume:
        if not args.csv and not args.dry_run:
            raise VerifyError("-o/--csv is required (or --resume an existing "
                              "result file)")
        ctx.csv_path = args.csv or ""
        ctx.run_id = ctx.started.strftime("%Y%m%dT%H%M%S")
        ctx.start_group = int(args.start_group or 0)
        ctx.group_index = ctx.start_group
        if (not args.dry_run and os.path.exists(ctx.csv_path)
                and os.path.getsize(ctx.csv_path)):
            raise VerifyError(
                f"{ctx.csv_path} already exists; --resume it, or write to a "
                f"new file.  Truncating two hours of measurements to start a "
                f"new run is not something this tool will do for you")
        return True

    if not os.path.exists(args.resume):
        raise VerifyError(f"--resume: no such file: {args.resume}")
    try:
        header, rows = load_results(args.resume)
    except Exception as exc:                       # noqa: BLE001 - reported
        raise VerifyError(f"--resume: {args.resume}: {exc}") from exc

    _check_resume(ctx, header, rows)

    ctx.csv_path = args.resume
    ctx.appending = True
    ctx.prior_rows = tuple(rows)
    ctx.run_id = header.run_id or ctx.started.strftime("%Y%m%dT%H%M%S")
    groups = ctx.plan.groups()
    computed, offset = resume_point(ctx.plan, rows)

    # An explicit --start-group equal to the computed one keeps the computed
    # offset: that is exactly the command the interrupted run printed, and
    # re-measuring the group's completed points because the operator pasted it
    # back is the duplication this whole path exists to avoid.  A *different*
    # group is a deliberate override, and starts that group at its first point.
    if args.start_group is None or int(args.start_group) == computed:
        ctx.start_group, ctx.start_point = computed, offset
    else:
        ctx.start_group, ctx.start_point = int(args.start_group), 0
    ctx.group_index = ctx.start_group

    print()
    print(_rule("Resume"))
    print(f"  {args.resume}: {_plural(len(rows), 'row')} already measured, "
          f"run id {header.run_id or 'unknown'}, started {_stamp(header.started)}")
    if computed >= len(groups):
        print(f"  every one of the {len(groups)} groups is complete; there is "
              f"nothing left to measure")
        return False
    if offset:
        print(f"  group {computed + 1} is incomplete ({offset} of "
              f"{len(groups[computed])} points measured); it continues at its "
              f"point {offset}, so nothing is measured twice")
    if args.start_group is not None and int(args.start_group) != computed:
        print(f"  --start-group {args.start_group} overrides the computed "
              f"{computed}; that group starts from its first point")
    remaining = sum(len(g) for g in groups[ctx.start_group:]) - ctx.start_point
    print(f"  resuming at group {ctx.start_group + 1} of {len(groups)} "
          f"({groups[ctx.start_group].label}), "
          f"{_plural(remaining, 'point')} left")
    return True


def _check_unattended(ctx: RunContext) -> None:
    """Refuse ``--yes`` on a run with more than one wiring group left.

    ``--yes`` answers the group prompts, and a group boundary is where the
    operator *physically re-cables*.  Answering it for them does not skip the
    re-cabling; it measures the next group through the previous group's wiring.
    On the shipped plan that is 72 resistance points taken through 2-wire dc
    volts cabling and recorded as valid -- false FAILs on a good instrument,
    which is worse than no data -- and an unattended run that reaches 1000 V and
    then 1 A with nobody at the bench.

    One group is the case ``--yes`` is for, and it is also what makes ``--yes``
    accepting the high-voltage confirmation defensible: there is no boundary at
    which a live output could meet a pair of hands.
    """
    if not ctx.args.yes:
        return
    groups = ctx.plan.groups()[ctx.start_group:]
    if len(groups) <= 1:
        return
    listed = ", ".join(group.label for group in groups)
    raise VerifyError(
        f"--yes would run {_plural(len(groups), 'wiring group')} unattended "
        f"({listed}).  A group boundary is where the leads are physically "
        f"moved, so every group after the first would be measured through the "
        f"previous group's wiring and recorded as valid.  Narrow the run to "
        f"one group (--quantities, --min-spec-ratio or --start-group), or drop "
        f"--yes and answer the prompts")


def _check_resume(ctx: RunContext, header: RunHeader,
                  rows: Sequence[ResultRow]) -> None:
    """Refuse to append rows from a different plan to the same file.

    A result file's whole claim is that it is one run of one plan.  Two plans in
    one file is a record nothing downstream can untangle -- ``compare_runs``
    matches points by identity across files, not within one -- and the rows
    already in it are the ones that cost two hours.
    """
    plan = ctx.plan
    problems: list[str] = []
    for label, existing, wanted in (
            ("plan name", header.plan_name, ctx.plan_name),
            ("dut_role", header.dut_role, plan.dut_role),
            # run_kind is never blank -- "standalone" is a statement, and a
            # resume that quietly rewrote an as-found file's remaining rows
            # under a different kind would split one run across two cycles.
            ("run kind", header.run_kind,
             ctx.args.run_kind or "standalone"),
            ("source model", header.source_model, plan.source_model_id),
            ("meter model", header.meter_model, plan.meter_model_id),
            ("interval", header.interval, plan.interval),
            ("temperature policy", header.temperature_policy,
             plan.temperature_policy)):
        if existing and existing != wanted:
            problems.append(f"{label}: the file says {existing!r}, this run "
                            f"would be {wanted!r}")
    for label, existing, wanted in (
            ("confidence", header.confidence, plan.confidence),
            ("TUR floor", header.tur_floor, plan.tur_floor),
            ("guard-band factor", header.guard_band_factor,
             plan.guard_band_factor)):
        if not math.isclose(float(existing), float(wanted), rel_tol=1e-12):
            problems.append(f"{label}: the file says {existing:g}, this run "
                            f"would be {wanted:g}")

    for row in rows:
        if is_proof_row(row):
            continue                 # guards its point; not a measurement of it
        if row.point_index >= len(plan.points):
            problems.append(
                f"point {row.point_index} ({row.label or row.quantity.name}) "
                f"is in the file but this plan has only "
                f"{len(plan.points)} points")
            break
        planned = plan.points[row.point_index]
        if point_key(row) != plan_point_key(planned):
            problems.append(
                f"point {row.point_index}: the file measured "
                f"{row.label or row.quantity.name} but this plan has "
                f"{planned.describe()}")
            break

    if problems:
        raise VerifyError(
            "--resume: this is not the plan that wrote that file:\n  "
            + "\n  ".join(problems)
            + "\n  Re-run with the same options (or the same --plan), or "
              "write to a new file.")


# --------------------------------------------------------------------------
# Phase 4 -- the instruments
# --------------------------------------------------------------------------

#: The terminations that are not Ctrl-C: an ssh drop (SIGHUP), a
#: ``timeout``, a ``kill``, a systemd stop (SIGTERM).  ``setup_graceful_shutdown``
#: installs a SIGINT handler only, and the default disposition of these two
#: ends the interpreter without unwinding -- so ``run``'s ``finally``, which is
#: what puts the calibrator in standby, never happens (M12).  The 5700A service
#: console carries the same list under
#: :func:`~caldmm.apps.fluke5700a_console.install_termination_handlers`;
#: repeated rather than shared because the two apps have no other reason to
#: import each other.
TERMINATION_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP")
    if hasattr(signal, name))


def _install_termination_handlers() -> dict:
    """Turn SIGTERM/SIGHUP into an unwind, so the shutdown path runs.

    The handler **raises** rather than de-energizing itself: unwinding runs
    ``run``'s ``finally`` -- which closes the engine's generator (whose own
    ``finally`` puts the source in standby), then shuts the instruments down --
    on the main thread, instead of doing GPIB I/O from inside a signal handler
    while a read is in flight.  :func:`run` already treats ``SystemExit`` as an
    interruption, so the rows measured so far keep their resume command.

    Returns what was displaced, so :func:`_shutdown` can put it back: a
    handler that outlived its run would raise ``SystemExit`` inside whatever
    ran next on a signal that has nothing to do with this calibrator.
    """
    def handler(signum, frame):                    # pragma: no cover - signal
        name = getattr(signal.Signals(signum), "name", str(signum))
        print(f"\nCaught {name}, stopping and de-energizing...",
              file=sys.stderr, flush=True)
        raise SystemExit(EXIT_ABORTED)

    previous = {}
    for signum in TERMINATION_SIGNALS:
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):              # pragma: no cover - thread
            previous.pop(signum, None)
    return previous


def _restore_termination_handlers(previous: dict) -> None:
    """Put back what :func:`_install_termination_handlers` displaced."""
    for signum, disposition in (previous or {}).items():
        try:
            signal.signal(signum, disposition)
        except (ValueError, OSError, TypeError):   # pragma: no cover - thread
            pass


def _phase_open(ctx: RunContext) -> None:
    """Open both instruments, print their provenance, and refuse a bad start."""
    print()
    print(_rule("Instruments"))
    try:
        ctx.source = open_source(ctx.source_model_id, ctx.source_instrument)
    except Exception as exc:                       # noqa: BLE001 - reported
        raise VerifyError(
            f"cannot open the source {ctx.source_instrument} "
            f"({ctx.source_model_id}): {exc}") from exc
    try:
        ctx.meter = open_meter(ctx.meter_model_id, ctx.meter_instrument)
    except Exception as exc:                       # noqa: BLE001 - reported
        raise VerifyError(
            f"cannot open the meter {ctx.meter_instrument} "
            f"({ctx.meter_model_id}): {exc}") from exc

    # Installed as soon as there is something to de-energize, and before the
    # first health query: a Ctrl-C from here on must reach standby.
    setup_graceful_shutdown(lambda: _emergency(ctx))
    ctx.previous_signals = _install_termination_handlers()
    ctx.console = OperatorConsole.for_instrument(getattr(ctx.meter, "device",
                                                         None))

    ctx.health = ctx.source.health()
    _print_source(ctx)
    if _plan_sources_current(ctx):
        post = _apply_current_post(ctx)
        print(f"    CUR_POST? {post} — {_CURRENT_POST_TERMINALS.get(post, post)}"
              if ctx.current_post else
              f"    CUR_POST  {post} (this source cannot report it)")
    _print_meter(ctx)
    _gate_source(ctx)


def _print_source(ctx: RunContext) -> None:
    device = getattr(ctx.source, "device", None)
    health = ctx.health
    identity = getattr(device, "identity", None)
    options = list(getattr(device, "options", []) or [])
    interval = _call(device, "cal_intv")
    confidence = _call(device, "cal_conf")
    print(f"  source  {ctx.source_model_id}  {ctx.source_instrument}")
    print(f"    *IDN?     {getattr(identity, 'raw', '') or _ask(device, '*IDN?') or 'unknown'}")
    print(f"    *OPT?     {', '.join(options) if options else 'none'}")
    print(f"    ONTIME?   {_opt(health.warm_minutes, ' min')} warm "
          f"(specifications assume {WARMUP_MINUTES} min)")
    print(f"    CAL_DAYS? CAL {_opt(health.days_since_cal, ' d')}, "
          f"ZERO {_opt(health.days_since_zero, ' d')} "
          f"(zeros are due at {ZERO_INTERVAL_DAYS} d)")
    print(f"    CAL_INTV? {_opt(interval, ' d')}   CAL_CONF? "
          f"{confidence or 'unknown'}")
    cal_temp = _call(ctx.source, "cal_temperature")
    if cal_temp is not None:
        print(f"    CAL_TEMP? {cal_temp:.2f} C (the ambient its stored "
              f"constants assume)")
    if health.faults:
        print(f"    faults    {_plural(len(health.faults), 'queued fault')} "
              f"drained at startup: "
              + "; ".join(f"{code}: {text}" for code, text in health.faults))


def _print_meter(ctx: RunContext) -> None:
    device = getattr(ctx.meter, "device", None)
    query = _IDN_QUERY.get(ctx.meter_model_id, _DEFAULT_IDN_QUERY)
    idn = _call(device, "idn") or _ask(device, query)
    print(f"  meter   {ctx.meter_model_id}  {ctx.meter_instrument}")
    print(f"    {query:<9} {idn or 'unknown'}")
    calnum = _call(device, "calnum")
    if calnum is not None:
        print(f"    CALNUM?   {calnum}")
    if _call(device, "has_internal_temp"):
        temperature = _call(device, "get_temp")
        if temperature is not None:
            print(f"    TEMP?     {temperature:.2f} C internal")
    hours = meter_warmup_hours(ctx.meter_model_id)
    if hours is not None:
        print(f"    warm-up   {hours:g} h to its published specifications "
              f"(the meter cannot report its power-on time)")


def meter_warmup_hours(model_id: str) -> float | None:
    """The DUT meter's stated warm-up, or ``None`` for a model without one."""
    return METER_WARMUP_HOURS.get(str(model_id).strip().lower())


def _gate_source(ctx: RunContext) -> None:
    """Refuse a start that would make every dc number suspect."""
    health = ctx.health
    if health.tripped:
        raise VerifyError(
            "the calibrator is tripped to standby; clear the cause (check the "
            "leads and the fault queue) before starting.  Readings taken "
            "through a tripped output are not of the points that were asked "
            "for")
    zeros = health.days_since_zero
    if zeros is not None and zeros > ZERO_INTERVAL_DAYS:
        if not ctx.args.force:
            raise VerifyError(
                f"the calibrator's dc zeros are {zeros} days old and are due "
                f"every {ZERO_INTERVAL_DAYS}; every dc shift measured through "
                f"it would be suspect, and the unit is one output away from "
                f"fault 1303.  Run the zeros first, or --force")
        print(f"  !! --force: running with dc zeros {zeros} days old "
              f"(due at {ZERO_INTERVAL_DAYS}); every dc result is suspect")
    warm = health.warm_minutes
    if warm is not None and warm < WARMUP_MINUTES:
        print(f"  !! the calibrator has been warm for {warm} min; its "
              f"specifications assume {WARMUP_MINUTES}.  Every point will "
              f"carry source-not-warmed-up")
    interval = _call(getattr(ctx.source, "device", None), "cal_intv")
    if (interval and health.days_since_cal is not None
            and health.days_since_cal > interval):
        print(f"  !! the calibrator was last calibrated "
              f"{health.days_since_cal} days ago, past its own "
              f"{interval}-day CAL_INTV; its UNCERT? figures are outside the "
              f"interval they were computed for")
        if ctx.dut_is_meter:
            ctx.reference_overdue_days = int(health.days_since_cal
                                             - interval)
            print(f"     It is the reference: verdicts are still stated, and "
                  f"every row carries {FLAG_REFERENCE_CAL_OVERDUE}")
    _gate_claim_tested(ctx, interval)


def _gate_claim_tested(ctx: RunContext, interval) -> None:
    """M64: a ``--dut source`` run tests the claim the *instrument* states.

    With the calibrator under test the tolerance at every point where it
    states one is its own ``UNCERT?``, computed by its firmware at its stored
    ``CAL_INTV``/``CAL_CONF`` -- not at the interval the plan names, which
    still governs the header, the spec-table cross-check and the zero-point
    floor.  On the bench unit those are 1 day and CONF99, so a run headed
    "interval 1y" would test the 24-hour specification (3.5 ppm + 3 uV at
    10 V) at every non-zero point and the 1-year floor at its zeros: two
    claims in one certificate, which JCGM 106 5.1 says a statement of
    conformity may not have.

    Refused rather than warned, because the fix is one command on either side
    and the record is otherwise wrong in a way no reader can repair
    afterwards.  ``--force`` is the same escape it is for overdue zeros.
    """
    if ctx.dut_is_meter:
        return
    device = getattr(ctx.source, "device", None)
    confidence = str(_call(device, "cal_conf") or "")
    named = _plan_interval_days(ctx)
    claim = (f"UNCERT? at {int(interval)} d / {confidence or 'an unstated confidence'}"
             if interval else "")
    if claim:
        print(f"    claim tested: {claim} -- the tolerance every non-zero "
              f"point of this run is judged against")
    if not interval or named is None:
        return
    if math.isclose(float(named), float(interval), rel_tol=1e-9):
        return
    message = (
        f"the calibrator's own CAL_INTV is {int(interval)} d "
        f"({confidence or 'confidence unstated'}) but this plan states "
        f"interval {ctx.plan.interval}: UNCERT? would judge every non-zero "
        f"point against the {int(interval)}-day specification while the "
        f"header, the spec-table cross-check and the zero points state "
        f"{ctx.plan.interval}.  Set the instrument's interval to match "
        f"(caldmm-5700a-console ... calintv {int(named)}), or run with "
        f"--interval matching the instrument")
    if not ctx.args.force:
        raise VerifyError(message)
    print(f"  !! --force: {message}")


def _plan_interval_days(ctx: RunContext) -> float | None:
    """The plan's interval in days, or ``None`` if it names no column."""
    return interval_days(getattr(ctx.plan, "interval", "") or "")


# --------------------------------------------------------------------------
# Phase 5 -- ambient temperature
# --------------------------------------------------------------------------

def _phase_ambient(ctx: RunContext) -> None:
    """Wire the probe, read it once for the header, and say what it means."""
    args = ctx.args
    if args.ambient is not None:
        ctx.probe = StaticEnvironment(float(args.ambient), source="operator")
    else:
        ctx.probe = StaticEnvironment(None, source="none")

    try:
        ctx.environment = ctx.probe.read()
    except Exception as exc:                       # noqa: BLE001 - reported
        ctx.environment = UNKNOWN_ENVIRONMENT
        print(f"  ambient   probe failed: {type(exc).__name__}: {exc}")
    else:
        if ctx.environment.ambient_known:
            print(f"  ambient   {ctx.environment.ambient_c:.3f} C, "
                  f"{_opt(ctx.environment.humidity_pct, ' %RH', lambda v: f'{v:.1f}')}"
                  f" from {ctx.environment.source or 'unknown'} at "
                  f"{_stamp(ctx.environment.timestamp)}")
        else:
            print(f"  ambient   nothing recorded "
                  f"(source {ctx.environment.source or 'unknown'})")


def _ambient_is_required(ctx: RunContext) -> tuple[float, float] | None:
    """The DUT's temperature window for this run's interval, if it declares one.

    Only then is a missing ambient fatal to a PASS.  Neither the 3458A nor the
    5700A declares an absolute window -- their figures are relative to their own
    stored calibration temperature -- so warning about a missing ambient on that
    pairing as though every point were blocked would be crying wolf.
    """
    spec = ctx.dut_spec
    temperature = getattr(spec, "temperature", None)
    if temperature is None:
        return None
    return temperature.window_for(ctx.plan.interval or spec.default_interval)


# --------------------------------------------------------------------------
# Phase 6 -- the confirmations
# --------------------------------------------------------------------------

def _phase_confirm(ctx: RunContext) -> bool:
    """The things an operator must agree to.  False means "do not run"."""
    _check_source_temperature(ctx)
    _check_meter_temperature(ctx)
    _confirm_recent_acal(ctx)
    return (_confirm_ambient(ctx)
            and _confirm_meter_warmup(ctx)
            and _confirm_high_voltage(ctx))


def _check_source_temperature(ctx: RunContext) -> None:
    """M25: the 5700A's tables hold within +/-5 C of its own CAL_TEMP.

    Both numbers are already read -- ``CAL_TEMP?`` for the provenance block
    and the ambient for the verdict -- and nothing compared them, so a
    ``--dut source`` run at 29 C against constants stored at 22 C stated
    conformity to a specification whose tempco adder was excluded by its own
    stated condition ([OM p.35]: "Temperature coefficient is an adder to
    uncertainty specifications that does not apply unless operating more than
    +/-5 C from calibration temperature").

    Reported and recorded here; whether it makes a point INDETERMINATE is the
    engine's, from the window the model file declares.
    """
    if ctx.dut_is_meter or not ctx.environment.ambient_known:
        return
    cal_temp = _call(ctx.source, "cal_temperature")
    if cal_temp is None:
        return
    departure = abs(float(ctx.environment.ambient_c) - float(cal_temp))
    if departure <= SOURCE_TEMP_HALF_WIDTH_C:
        return
    ctx.source_temp_departure_c = departure
    print()
    print(f"  !! the bench is {departure:.1f} C from the calibrator's "
          f"CAL_TEMP? of {float(cal_temp):.2f} C, more than the "
          f"{SOURCE_TEMP_HALF_WIDTH_C:g} C every one of its specification "
          f"tables is headed with.")
    print(f"     Its UNCERT? figures carry no tempco adder, so they are "
          f"stated outside their own conditions here.")


def _acal_applies(ctx: RunContext) -> bool:
    """Whether the meter's figures are conditional on a recent ACAL.

    The model file says so (``interval_basis: since_acal``) for every column
    but 24 h, which is written for Tcal +/- 1 C instead.  As the reference
    the meter is judged at its own default column.
    """
    spec = ctx.meter_spec
    if spec is None or spec.interval_basis is not IntervalBasis.SINCE_ACAL:
        return False
    interval = (ctx.plan.interval if ctx.dut_is_meter
                else spec.default_interval)
    return interval != "24h"


def _confirm_recent_acal(ctx: RunContext) -> None:
    """Without ``--acal``, ``--acal-recent`` is the operator's statement.

    Not a prompt: a full run is meant to need no answers beyond the wiring.
    The statement (or its absence) is recorded and decides the condition on
    every row (see :func:`acal_conditions`): without one the rows are
    INDETERMINATE, because the figures they would be judged against are only
    stated after a recent ACAL.
    """
    if getattr(ctx.args, "acal", False) or not _acal_applies(ctx):
        return
    ctx.acal_confirmed = bool(getattr(ctx.args, "acal_recent", False))
    if not ctx.acal_confirmed:
        print(f"  !! no ACAL within {ACAL_VALID_HOURS:g} h and "
              f"{ACAL_HALF_WIDTH_C:g} C is established: every row will be "
              f"INDETERMINATE ({FLAG_ACAL_NOT_ESTABLISHED}).  Pass --acal to "
              f"run one first, or --acal-recent if one was")


def acal_conditions(ctx: RunContext, point, meter_temp, timestamp
                    ) -> list[tuple[str, str]]:
    """The engine's per-row hook: is the meter inside its ACAL conditions?"""
    if not _acal_applies(ctx):
        return []
    if ctx.acal_done and ctx.acal_at is not None:
        problems = []
        if timestamp is not None:
            age_h = (timestamp - ctx.acal_at).total_seconds() / 3600.0
            if age_h > ACAL_VALID_HOURS:
                problems.append(f"the ACAL is {age_h:.1f} h old")
        if meter_temp is not None and ctx.acal_temp_c is not None \
                and abs(float(meter_temp) - ctx.acal_temp_c) > ACAL_HALF_WIDTH_C:
            problems.append(f"TEMP? {float(meter_temp):.2f} C is "
                            f"{abs(float(meter_temp) - ctx.acal_temp_c):.2f} C "
                            f"from the ACAL's {ctx.acal_temp_c:.2f} C")
        if not problems:
            return []
        return [(FLAG_ACAL_NOT_ESTABLISHED,
                 "the meter is outside its ACAL conditions (within "
                 f"{ACAL_VALID_HOURS:g} h and {ACAL_HALF_WIDTH_C:g} C): "
                 + "; ".join(problems))]
    if ctx.acal_confirmed:
        return []
    return [(FLAG_ACAL_NOT_ESTABLISHED,
             f"no ACAL within {ACAL_VALID_HOURS:g} h and "
             f"{ACAL_HALF_WIDTH_C:g} C was run or confirmed, and the meter's "
             f"figures are stated only after one")]


def meter_temperature_windows(ctx: RunContext) -> dict:
    """The meter's internal-temperature windows, per quantity, for the engine.

    dc volts against ``CAL? 59``, ohms and current against ``CAL? 60``, at
    the spec file's half-width for the interval (5 C; 1 C at 24 h); ac against
    this session's ACAL temperature +/- 1 C, the datum the ac tables use.
    Empty for a meter that reports no adjustment temperatures.
    """
    temperatures = ctx.meter_temperatures or {}
    spec = ctx.meter_spec
    if spec is None or getattr(spec, "temperature", None) is None:
        return {}
    interval = ctx.plan.interval if ctx.dut_is_meter else spec.default_interval
    windows: dict = {}
    for quantity, name in METER_TCAL_CONSTANT.items():
        if Quantity(quantity).frequency_dependent:
            if ctx.acal_temp_c is not None:
                windows[quantity] = (ctx.acal_temp_c - ACAL_HALF_WIDTH_C,
                                     ctx.acal_temp_c + ACAL_HALF_WIDTH_C)
            continue
        centre = temperatures.get(name)
        if centre is None:
            continue
        window = spec.temperature.window_for(interval, reference_c=centre)
        if window is not None:
            windows[quantity] = window
    return windows


def _check_meter_temperature(ctx: RunContext) -> None:
    """The 3458A's own precondition: ``TEMP?`` within 5 C of its adjustment.

    Every performance test in the Cal Manual begins with it -- "The internal
    temperature of the 3458A under test must be within 5 degrees C of its
    temperature when last adjusted (CAL 0, CAL 10, and CAL 10K)" (p.57) --
    and the data sheet's 90-day, 1-year and 2-year columns are written for
    Tcal +/- 5 C.  The voltage functions are held to ``CAL? 59`` and ohms and
    current to ``CAL? 60``, as the manual's test cards do.

    Only for a meter under test (as a reference its window is its own
    budget's business), and only for a meter that can report both numbers.
    Outside the window the run is **refused**: the manual's own instruction
    is to recalculate every limit, which this toolkit does not do.  ``--force``
    runs it anyway with every row flagged :data:`FLAG_METER_OUTSIDE_TCAL`.
    """
    meter = ctx.meter
    reader = getattr(meter, "cal_temperatures", None)
    internal = getattr(meter, "internal_temperature", None)
    if not callable(reader) or not callable(internal):
        return
    try:
        temperatures = dict(reader())
        now = internal()
    except Exception as exc:                           # noqa: BLE001 - reported
        print(f"  -- the meter's adjustment temperatures could not be read "
              f"({type(exc).__name__}: {exc}); the 5 C precondition is not "
              f"checked")
        return
    if now is not None:
        temperatures["now"] = float(now)
    ctx.meter_temperatures = temperatures
    if now is None or not ctx.dut_is_meter:
        return
    wanted = {METER_TCAL_CONSTANT[Quantity(point.quantity)]
              for point in ctx.plan.points
              if Quantity(point.quantity) in METER_TCAL_CONSTANT}
    print(f"  meter temperature  TEMP? {float(now):.2f} C; adjusted at "
          + ", ".join(f"CAL? {HP_CAL_TEMP_NUMBERS[name]} {value:.2f} C"
                      for name, value in temperatures.items()
                      if name in HP_CAL_TEMP_NUMBERS))
    departures = {name: abs(float(now) - temperatures[name])
                  for name in sorted(wanted) if name in temperatures
                  and abs(float(now) - temperatures[name])
                  > METER_TCAL_HALF_WIDTH_C}
    if not departures:
        return
    listed = ", ".join(f"{d:.1f} C from CAL? {HP_CAL_TEMP_NUMBERS[name]}"
                       for name, d in departures.items())
    if not ctx.args.force:
        raise VerifyError(
            f"the meter's internal temperature is {listed} -- outside the "
            f"{METER_TCAL_HALF_WIDTH_C:g} C its specifications and the Cal "
            f"Manual's performance tests require (p.56-57: 'you must "
            f"recalculate all test limits').  Let it reach its adjustment "
            f"temperature, or pass --force to run with every row flagged "
            f"{FLAG_METER_OUTSIDE_TCAL}")
    ctx.meter_temp_departures = departures
    print(f"  !! the meter is {listed}; --force: every row will carry "
          f"{FLAG_METER_OUTSIDE_TCAL}")


#: ``cal_temperatures()`` key -> the ``CAL?`` constant it came from.
HP_CAL_TEMP_NUMBERS = {"cal0": 58, "dcv": 59, "ohms": 60}


def _phase_acal(ctx: RunContext) -> None:
    """``--acal``: autocalibrate the 3458A, then let it settle, unattended.

    The data sheet's 90-day to 2-year figures hold "within 24 hours and +/-1 C
    of last ACAL", and the Cal Manual runs an ACAL before every function's
    tests.  ``ACAL ALL`` covers every function at once (DCV first, then AC and
    OHMS, the order the manual requires), takes about 16 minutes, and wants 30
    minutes of settling (``--acal-settle``).  The calibrator is put in standby
    first: "Always disconnect any AC input signals before you perform an
    autocal".  Run before the first wiring prompt, so the operator can walk
    away for the whole of it.
    """
    if not getattr(ctx.args, "acal", False):
        return
    autocal = getattr(ctx.meter, "autocal", None)
    if not callable(autocal):
        raise VerifyError(
            f"--acal: {ctx.meter_model_id} has no autocal; drop --acal")
    settle = getattr(ctx.args, "acal_settle", None)
    settle = ACAL_SETTLE_MINUTES if settle is None else float(settle)
    if not math.isfinite(settle) or settle < 0.0:
        raise VerifyError(f"--acal-settle {settle:g}: minutes, not negative")
    try:
        ctx.source.standby()
    except Exception as exc:                           # noqa: BLE001 - fatal
        raise VerifyError(
            f"the calibrator would not confirm standby before ACAL ({exc}); "
            f"an autocal must not see a live input") from exc
    print()
    print(_rule("ACAL"))
    print(f"  ACAL ALL on {ctx.meter_instrument}: about 16 min, then "
          f"{settle:g} min of settling.  The calibrator is in standby.")
    autocal("ALL")
    remaining = settle * 60.0
    while remaining > 0.0:
        step = min(remaining, 300.0)
        print(f"  settling after ACAL: {remaining / 60.0:.0f} min left")
        ctx.sleep(step)
        remaining -= step
    ctx.acal_done = "ALL"
    ctx.acal_at = (ctx.clock or _wall_clock)()
    internal = getattr(ctx.meter, "internal_temperature", None)
    if callable(internal):
        try:
            value = internal()
        except Exception:                              # noqa: BLE001 - optional
            value = None
        if value is not None:
            ctx.acal_temp_c = float(value)
            print(f"  TEMP? after ACAL: {ctx.acal_temp_c:.2f} C (the datum of "
                  f"the ac temperature coefficients)")


def _confirm_ambient(ctx: RunContext) -> bool:
    window = _ambient_is_required(ctx)
    if ctx.environment.ambient_known:
        if window is not None and not (window[0] <= ctx.environment.ambient_c
                                       <= window[1]):
            print(f"  !! {ctx.environment.ambient_c:.2f} C is outside "
                  f"{ctx.dut_model_id}'s {window[0]:g}-{window[1]:g} C "
                  f"specification window; those points cannot PASS")
        return True
    if window is None:
        print(f"  -- no ambient temperature is available.  "
              f"{ctx.dut_model_id} declares no absolute window for this "
              f"interval, so no verdict is blocked by it, but the run will "
              f"record no conditions and no tempco term can be built")
        return True

    print()
    print(f"  !! NO AMBIENT TEMPERATURE IS AVAILABLE.")
    print(f"     {ctx.dut_model_id} specifies {window[0]:g} to {window[1]:g} C "
          f"for this interval, and a statement of conformity cannot be made "
          f"about a")
    print(f"     specification whose stated conditions were never shown to "
          f"hold.  Every one of these {len(ctx.plan.points)} points will "
          f"return INDETERMINATE.")
    print(f"     The fix is --ambient 23.4 (a measured room temperature), never "
          f"--temperature-policy flag: flag would also disable the tighter "
          f"window a")
    print(f"     24-hour specification depends on.")
    if ctx.args.yes:
        print(f"     --yes: running anyway.")
        return True
    # Enter is **not** yes here (M26): the hint says anything other than y
    # stops, and a run that cannot produce a single PASS must not be started
    # by the emptiest answer an operator can give.
    if ctx.console.confirm_ok(
            "running with no ambient temperature",
            hint="[y = run anyway / Enter or anything else = stop]",
            default=False):
        return True
    print("  stopped: nothing was measured.")
    return False


def _confirm_meter_warmup(ctx: RunContext) -> bool:
    """M24: the meter's specifications are stated after a warm-up.

    No meter here reports its power-on time, so the only evidence that ever
    exists is the operator's answer -- and a verdict against a 4-hour
    specification taken 30 minutes after switch-on is a statement about
    conditions the specification excludes.  Never a refusal: an unconfirmed
    warm-up is a *condition* of the claim, so it is asked, recorded in the
    header and carried onto every row, exactly as the calibrator's own
    warm-up is.
    """
    if not ctx.dut_is_meter:
        return True
    hours = meter_warmup_hours(ctx.meter_model_id)
    if hours is None:
        return True
    print()
    print(f"  {ctx.meter_model_id}'s specifications are stated after a "
          f"{hours:g} h warm-up, and the meter cannot report its own "
          f"power-on time.")
    if ctx.args.yes:
        # --yes answers prompts; it cannot know when the meter was switched
        # on, and recording an unasked question as "confirmed" would put a
        # condition in the record that nobody ever checked.
        ctx.meter_warm_confirmed = False
        print(f"     --yes: not confirmed.  Every row carries "
              f"{FLAG_METER_WARMUP_UNCONFIRMED}.")
        return True
    ctx.meter_warm_confirmed = ctx.console.confirm_ok(
        f"the meter having been powered for at least {hours:g} h",
        hint="[y = it has / Enter or anything else = it has not]",
        default=False)
    if not ctx.meter_warm_confirmed:
        print(f"     recorded as unconfirmed: every row carries "
              f"{FLAG_METER_WARMUP_UNCONFIRMED}.")
    return True


def _max_setpoint(plan: TestPlan, unit: str, start_group: int = 0) -> float:
    """The largest setpoint *this run* sources in *unit*, or ``0``.

    Same shape as :func:`_max_voltage` and for the same reason -- taken from
    *start_group* onwards, because a resumed run does not source what the
    first session already did.
    """
    groups = plan.groups()[start_group:]
    values = [abs(float(point.value)) for group in groups
              for point in group.points if point.quantity.unit == unit]
    return max(values, default=0.0)


def _max_entry(ctx: RunContext, unit: str) -> float:
    """The largest magnitude this run **enters** in *unit*, or ``0``.

    Not the same as the largest setpoint, and the difference is what ``LIMIT``
    sees.  A point that needs the 5700A's range lock is programmed to the
    range-lock *anchor* first -- the largest output the range can make, 1100 V
    for DC1100V and 2.2 A for DC2_2A ([SM 2007] 3-16, and
    :func:`~caldmm.cal.plan.range_lock_anchor`) -- and ``LIMIT`` refuses an
    entry whatever it was for (faults 815/856).  On the shipped ``--dut
    source`` plan every 10 %-of-range point is such a point, so a limit armed
    at the largest setpoint would fault the anchor of the very first one and
    fail it.

    The anchor is entered in **standby** and never energised; the operator's
    hazard acknowledgement stays :func:`_max_voltage`, which is what actually
    reaches the terminals.
    """
    maximum = _max_setpoint(ctx.plan, unit, ctx.start_group)
    spec = ctx.source_spec
    if spec is None:
        return maximum
    for group in ctx.plan.groups()[ctx.start_group:]:
        for point in group.points:
            if point.quantity.unit != unit:
                continue
            range_id = getattr(point, "range_id", None)
            if not range_id:
                continue
            function = spec.function(point.quantity)
            if function is None:
                continue
            range_spec = next((r for r in function.ranges
                               if r.range_id == range_id), None)
            if range_spec is None or not needs_range_lock(
                    function, range_spec, float(point.value)):
                continue
            anchor = range_lock_anchor(
                function, range_spec,
                frequency_hz=float(getattr(point, "frequency_hz", 0.0) or 0.0))
            if anchor is not None:
                maximum = max(maximum, abs(float(anchor)))
    return maximum


def _arm_output_limits(ctx: RunContext) -> None:
    """L87: arm the calibrator's own entry limits for the plan's maximum.

    ``LIMIT`` is the one interlock the instrument enforces whatever software
    asks (faults 815/856), and the runner never used it.  **Both pairs**: the
    voltage limit bounds dc and ac volts and the current limit bounds dc and
    ac amps, and they are separate commands ([OM §4-51]), so a current-only
    plan arms the pair that matters to it rather than nothing at all.

    The maximum is :func:`_max_entry`, not the largest setpoint: the range
    lock's anchor is an entry too, and arming below it would have the
    instrument refuse the plan's own points.  What was armed is printed and
    goes in the header, because it is not always a number the plan prints
    anywhere else.

    The limits in force are read first and restored at shutdown.  A power
    cycle would put the full envelope back by itself, but a run that ends
    normally leaves the instrument on the bench, and a 22 V entry limit left
    behind by a verification is a puzzle for whoever uses it next.  Best
    effort throughout: a calibrator that will not take a pair still runs the
    plan ``validate_output`` already checked offline.
    """
    device = getattr(ctx.source, "device", None)
    setter = getattr(device, "set_limits", None)
    if setter is None:
        return
    ctx.previous_limits = _call(device, "limits")
    armed = []
    for unit in ("V", "A"):
        maximum = _max_entry(ctx, unit)
        if not maximum or not math.isfinite(maximum):
            continue
        try:
            setter(maximum, -maximum, unit)
        except Exception as exc:                   # noqa: BLE001 - reported
            print(f"  !! the calibrator did not accept LIMIT "
                  f"{_q(maximum, unit, 4)} ({type(exc).__name__}: {exc}); "
                  f"its own entry interlock is not armed for {unit}")
            continue
        armed.append(f"+/-{_q(maximum, unit, 4)}")
    if not armed:
        return
    ctx.armed_limits = tuple(armed)
    print(f"  LIMIT {', '.join(armed)}: the calibrator itself now refuses "
          f"anything beyond this plan's largest entry (the range lock's "
          f"anchor included; nothing above the plan's setpoints is energized)")
    if ctx.previous_limits is None:
        print(f"  !! the previous LIMIT could not be read, so it will not be "
              f"restored at the end of the run")


def _restore_output_limits(ctx: RunContext) -> None:
    """Put the calibrator's entry limits back, on every exit path."""
    if not ctx.armed_limits or ctx.previous_limits is None:
        return
    device = getattr(ctx.source, "device", None)
    setter = getattr(device, "set_limits", None)
    if setter is None:
        return
    try:
        limits = tuple(ctx.previous_limits)
    except TypeError:
        # A driver whose ``limits()`` answered something that is not iterable
        # at all.  Unpacking it here would raise inside _shutdown and skip
        # closing the file and the instrument handles.
        print(f"  !! the previous LIMIT was {ctx.previous_limits!r}, which is "
              f"not the four fields LIMIT? answers; the entry limits were "
              f"left as this run armed them", file=sys.stderr)
        return
    if len(limits) != 4:
        # LIMIT? answers four fields; anything else is a driver this runner
        # does not know, and unpacking it here would raise inside _shutdown
        # and skip closing the file and the instruments.
        print(f"  !! the previous LIMIT was {limits!r}, not the four fields "
              f"LIMIT? answers; the entry limits were left as this run "
              f"armed them", file=sys.stderr)
        return
    positive_v, negative_v, positive_a, negative_a = limits
    for positive, negative, unit in ((positive_v, negative_v, "V"),
                                     (positive_a, negative_a, "A")):
        _guard(f"restoring the {unit} entry limits",
               lambda p=positive, n=negative, u=unit: setter(p, n, u))


def _confirm_high_voltage(ctx: RunContext) -> bool:
    maximum = _max_voltage(ctx.plan, ctx.start_group)
    if maximum < HIGH_VOLTAGE_V:
        return True
    what = (f"a run that sources up to {_q(maximum, 'V', 4)} on "
            f"{ctx.source_model_id}")
    print()
    print(f"  !! THIS PLAN SOURCES UP TO {_q(maximum, 'V', 4)}.")
    print(f"     Check the leads are rated for it, that nothing else is "
          f"connected to the calibrator's OUTPUT terminals, and that nobody "
          f"else is at the bench.")
    if ctx.args.yes:
        # L87: --yes may not accept a hazardous output on nobody's behalf.
        # It exists so a single-group run needs no operator at the boundary;
        # it was never meant to put 1000 V on a bench with nobody at it.
        # The unattended path is to *state the maximum*, which is the typed
        # acknowledgement the interactive path asks for.
        stated = getattr(ctx.args, "accept_max", None)
        if stated is None or not math.isclose(float(stated), maximum,
                                              rel_tol=1e-6):
            raise VerifyError(
                f"--yes will not accept a run that sources "
                f"{_q(maximum, 'V', 4)}, at or above the "
                f"{HIGH_VOLTAGE_V:g} V hazard threshold, with nobody at the "
                f"bench"
                + (f" (--accept-max {float(stated):g} does not match this "
                   f"plan's maximum of {maximum:g} V)" if stated is not None
                   else "")
                + f".  Answer the confirmation yourself, state the maximum "
                  f"with --accept-max {maximum:g}, or cap the run with "
                  f"--max-voltage (below {HIGH_VOLTAGE_V:g} V no "
                  f"acknowledgement is needed at all)")
        print(f"     --yes --accept-max {maximum:g}: accepted.  The output "
              f"will reach {_q(maximum, 'V', 4)} unattended.")
        return True
    if ctx.console.confirm_explicit(what):
        return True
    print("  stopped: nothing was measured, and no output was commanded.")
    return False


# --------------------------------------------------------------------------
# Phase 7 -- the run
# --------------------------------------------------------------------------

def _build_header(ctx: RunContext) -> RunHeader:
    """Provenance: what a report has to state to be defensible.

    Both instruments, not just the one under test.  A verification is a
    comparison, and a reader who cannot see the reference's calibration age, its
    interval and the confidence its own uncertainty figures were computed at has
    been given a number with no means to judge it.
    """
    plan = ctx.plan
    device = getattr(ctx.source, "device", None)
    meter_device = getattr(ctx.meter, "device", None)
    identity = getattr(device, "identity", None)
    health = ctx.health
    meter_query = _IDN_QUERY.get(ctx.meter_model_id, _DEFAULT_IDN_QUERY)

    return RunHeader(
        run_id=ctx.run_id,
        started=ctx.started,
        plan_name=ctx.plan_name,
        dut_role=plan.dut_role,
        run_kind=ctx.args.run_kind or "standalone",
        counterpart=ctx.args.counterpart,
        adjust_record=ctx.args.adjust_log,
        interval=plan.interval,
        confidence=plan.confidence,
        tur_floor=plan.tur_floor,
        guard_band_factor=plan.guard_band_factor,
        temperature_policy=plan.temperature_policy,

        source_model=plan.source_model_id,
        source_idn=(getattr(identity, "raw", "") or _ask(device, "*IDN?")),
        source_options=tuple(getattr(device, "options", []) or []),
        source_cal_days=health.days_since_cal,
        source_zero_days=health.days_since_zero,
        source_cal_interval_days=_call(device, "cal_intv"),
        source_cal_confidence=str(_call(device, "cal_conf") or ""),
        source_cal_temp_c=_call(ctx.source, "cal_temperature"),
        source_warm_minutes=health.warm_minutes,

        meter_model=plan.meter_model_id,
        meter_idn=(_call(meter_device, "idn")
                   or _ask(meter_device, meter_query)),
        meter_calnum=_call(meter_device, "calnum"),
        meter_temp_c=(_call(meter_device, "get_temp")
                      if _call(meter_device, "has_internal_temp") else None),

        ambient_c=ctx.environment.ambient_c,
        humidity_pct=ctx.environment.humidity_pct,
        ambient_timestamp=ctx.environment.timestamp,
        ambient_source=ctx.environment.source,

        caldmm_version=caldmm_version(),
        git_revision=_git_revision(),
        extra=_header_extra(ctx, health),
    )


def _header_extra(ctx: RunContext, health) -> dict[str, str]:
    """Header keys this build adds beyond ``RunHeader``'s own fields.

    Everything that makes the file **a different run from the plan it names**
    goes here.  A file written with ``--start-group 2`` holds no rows for the
    first two groups and a file written with ``--min-spec-ratio`` holds no rows
    for the points it dropped; without these keys a report generated from either
    reads as a complete run of a shorter plan, and nothing in the row data says
    otherwise.  ``load_results`` keeps unrecognised ``# key=value`` lines in
    :attr:`~caldmm.cal.result.RunHeader.extra`, so they survive to the report.
    """
    extra: dict[str, str] = {}
    if health.faults:
        extra[FAULTS_KEY] = ",".join(str(code) for code, _ in health.faults)
    if any(Quantity(point.quantity) in _CURRENT_QUANTITIES
           for point in ctx.plan.points):
        # Which binding post carried the current is provenance: a reader of
        # the file cannot otherwise tell which terminals the DUT was on.
        requested = str(getattr(ctx.args, "current_post", "normal")).upper()
        extra["current_post"] = (
            ctx.current_post if ctx.current_post
            else f"{requested}{_POST_UNCONFIRMED}")
    if ctx.meter_warm_confirmed is not None:
        hours = meter_warmup_hours(ctx.meter_model_id)
        extra["meter_warm_confirmed"] = ("yes" if ctx.meter_warm_confirmed
                                         else "no")
        if hours is not None:
            extra["meter_warm_hours_required"] = f"{hours:g}"
    if ctx.source_temp_departure_c is not None:
        extra["source_temp_departure_c"] = f"{ctx.source_temp_departure_c:.2f}"
    for name, value in ctx.meter_temperatures.items():
        key = ("meter_temp_c" if name == "now"
               else f"meter_cal{HP_CAL_TEMP_NUMBERS.get(name, name)}_temp_c")
        extra[key] = f"{float(value):.2f}"
    if ctx.meter_temp_departures:
        extra["meter_outside_tcal"] = ", ".join(
            f"CAL? {HP_CAL_TEMP_NUMBERS[name]} {d:.2f} C"
            for name, d in ctx.meter_temp_departures.items())
    if getattr(ctx.args, "meter_option", None):
        extra["meter_option"] = str(ctx.args.meter_option)
    if ctx.acal_confirmed is not None:
        extra["acal_confirmed"] = "yes" if ctx.acal_confirmed else "no"
    if ctx.reference_overdue_days is not None:
        extra["reference_overdue_days"] = str(ctx.reference_overdue_days)
    if getattr(ctx.args, "lead_resistance", None) is not None:
        extra["lead_resistance_ohm"] = f"{float(ctx.args.lead_resistance):g}"
    if ctx.acal_done:
        extra["acal"] = ctx.acal_done
        if ctx.acal_temp_c is not None:
            extra["acal_temp_c"] = f"{ctx.acal_temp_c:.2f}"
    if getattr(ctx.args, "points", None):
        extra["point_set"] = str(ctx.args.points)
    if ctx.armed_limits:
        extra["output_limits"] = ", ".join(ctx.armed_limits)
    if ctx.start_group or ctx.start_point:
        extra["start_group"] = str(ctx.start_group)
        extra["start_point"] = str(ctx.start_point)
    if ctx.args.min_spec_ratio is not None:
        extra["min_spec_ratio"] = f"{float(ctx.args.min_spec_ratio):g}"
        extra["points_dropped_by_spec_ratio"] = str(ctx.dropped_by_ratio)
    if getattr(ctx.args, "max_voltage", None) is not None:
        # Without these a file capped at 100 V reads as a complete run of a
        # plan that names 1000 V, and no row in it says otherwise.
        extra["max_voltage_v"] = f"{float(ctx.args.max_voltage):g}"
        extra["points_dropped_by_max_voltage"] = str(
            ctx.dropped_by_max_voltage)
    if getattr(ctx.args, "connections", None):
        extra["connections"] = ",".join(_connections(ctx.args.connections)
                                        or ())
    return extra


#: Appended to a post the instrument was never asked to confirm.
_POST_UNCONFIRMED = " (requested, not read back)"


def _current_terminals(post: str) -> str:
    """The current group's terminal line, for the post the unit reports.

    H02/H04/L58: the prompt used to name the AUX post unconditionally while
    the calibrator's default is ``CUR_POST NORMAL`` -- the OUTPUT HI binding
    post ([OM §4-26, p.105]: "If you do not select current output location,
    OUTPUT HI is active") -- and the same prompt's wiring line said OUTPUT
    HI/LO two lines further down.  Which post is live is a non-volatile
    setting nothing in the subsystem read, so the two lines could not agree
    except by luck.
    """
    token = (post or "").strip().upper()
    unconfirmed = _POST_UNCONFIRMED.upper() in token
    token = token.replace(_POST_UNCONFIRMED.upper(), "").strip()
    where = _CURRENT_POST_TERMINALS.get(
        token, f"the post CUR_POST {token or 'unknown'} selects")
    stated = f"CUR_POST {token or 'unknown'}"
    if unconfirmed:
        # Said out loud, because the operator is about to cable one post and
        # the instrument may be sourcing from the other.
        where = f"whichever post CUR_POST selects — assumed {where}"
        stated += _POST_UNCONFIRMED
    return f"current: {where} ({stated}), and the meter's current input"


def wiring_instructions(boundary: GroupBoundary, *,
                        current_post: str = "") -> list[str]:
    """What the operator has to physically do at this boundary.

    *current_post* is what the calibrator answered to ``CUR_POST?`` for a
    current group; it is what makes the terminal line a fact rather than an
    assumption.

    A group is one **terminal set**, so it may span several functions -- the
    voltage/ohms group of a full plan runs dc volts, ac volts and both
    resistance wirings through one cable set.  Such a group gets one line per
    function and the cable set's wiring once.
    """
    group = boundary.group
    count = _plural(boundary.points, 'point')
    if boundary.offset:
        count += (f", resuming at #{boundary.start + boundary.offset} "
                  f"({boundary.remaining} left)")
    segments = group.segments
    if len(segments) <= 1:
        quantity = boundary.quantity
        values = [abs(float(point.value)) for point in group.points]
        span = (f"{_q(min(values), quantity.unit, 3)} to "
                f"{_q(max(values), quantity.unit, 3)}")
        if quantity in _CURRENT_QUANTITIES:
            terminals = _current_terminals(current_post)
            # The generic 2-wire line names OUTPUT HI/LO, which is only right
            # for CUR_POST NORMAL; on a current group the line above has
            # already said where the current comes out, so this one says the
            # rest.
            wiring = ("2-wire: sense off; the current returns through OUTPUT "
                      "LO whichever post sources it")
        else:
            terminals = _TERMINALS.get(quantity, quantity.name)
            wiring = _WIRING.get(boundary.connection, boundary.connection.value)
        return [
            f"quantity : {quantity.name} -- {count}, {span}",
            f"terminals: {terminals}",
            f"wiring   : {wiring}",
            "",
            "The calibrator is in STANDBY.  It goes to operate as soon as this "
            "prompt is answered.",
        ]

    lines = [f"group    : {_TERMINAL_NAMES.get(group.terminals, group.label)}"
             f" -- {count}"]
    offset = 0
    for quantity, connection, n in segments:
        values = [abs(float(point.value))
                  for point in group.points[offset:offset + n]]
        offset += n
        lines.append(f"  {quantity.name} {connection.value:<4} "
                     f"{_plural(n, 'point'):>11}, "
                     f"{_q(min(values), quantity.unit, 3)} to "
                     f"{_q(max(values), quantity.unit, 3)}")
    if group.terminals is Terminals.CURRENT:
        lines += [f"terminals: {_current_terminals(current_post)}",
                  f"wiring   : {_CURRENT_GROUP_WIRING}"]
    elif group.terminals is Terminals.VOLTAGE:
        four_wire = any(c is Connection.FOUR_WIRE for _q2, c, _n in segments)
        lines += ["terminals: the calibrator's OUTPUT (and SENSE) terminals",
                  f"wiring   : "
                  f"{_VOLTAGE_GROUP_WIRING if four_wire else _VOLTAGE_GROUP_WIRING_2W}"]
    else:
        lines += [f"terminals: {_TERMINALS.get(group.quantity, group.label)}"]
    lines += ["",
              "The calibrator is in STANDBY.  It goes to operate as soon as "
              "this prompt is answered, and re-configures itself between the "
              "functions above."]
    return lines


def _select_current_post(ctx: RunContext, boundary: GroupBoundary) -> str:
    """Make the current post deterministic before a current group, or refuse.

    ``CUR_POST`` is retained through power-off and is not reset by ``*RST``
    ([OM Table 5-2, p.176]), so on any unit the post in force is whatever the
    last operator left behind.  The adapter writes it, drains the fault queue
    and reads ``CUR_POST?`` back; a unit that answers something else has not
    done what it was told, and sourcing a current into a post nobody has
    cabled is how a group of 72 points becomes a compliance trip.
    """
    if not any(quantity in _CURRENT_QUANTITIES
               for quantity in boundary.group.quantities):
        return ""
    return _apply_current_post(ctx)


def _plan_sources_current(ctx: RunContext) -> bool:
    return any(Quantity(point.quantity) in _CURRENT_QUANTITIES
               for point in ctx.plan.points)


def _apply_current_post(ctx: RunContext) -> str:
    """Set the post, read it back, and return what the instrument reports.

    Called once at open -- so the header records a post that was really read
    rather than one that was asked for, and so a unit that will not take the
    setting stops the run before an operator is told to cable anything -- and
    again at every current group, because the front panel can move it in
    between.
    """
    wanted = str(getattr(ctx.args, "current_post", "normal") or "normal").upper()
    # The post is a relay: it must never be switched under a live output.
    # At a group boundary the engine has already de-energized, so this is one
    # idempotent command; at open it is the only thing standing between a
    # calibrator a previous session left in OPERATE and a post switching
    # under it.  Put here rather than at the call sites so the invariant
    # belongs to the function that writes the post.
    try:
        ctx.source.standby()
    except Exception as exc:                           # noqa: BLE001 - fatal
        raise VerifyError(
            f"the calibrator would not confirm standby before setting "
            f"CUR_POST ({exc}); the post is a relay and must not be switched "
            f"under a live output") from exc
    setter = getattr(ctx.source, "set_current_post", None)
    if setter is None:
        # A source adapter that cannot set or read the post (an older build,
        # or a source that is not a 5700A).  The prompt then states a
        # REQUEST and says so: printing an unread setting as though the
        # instrument had confirmed it is the whole defect H02 is about.
        ctx.current_post = ""
        return f"{wanted}{_POST_UNCONFIRMED}"
    try:
        post = str(setter(wanted)).upper()
    except Exception as exc:                           # noqa: BLE001 - fatal
        raise VerifyError(
            f"the calibrator did not accept CUR_POST {wanted}: {exc}.  Which "
            f"binding post carries the current is a non-volatile setting, and "
            f"a current group cannot be wired against a post the instrument "
            f"does not confirm") from exc
    ctx.current_post = post
    return post


def _on_group(ctx: RunContext, boundary: GroupBoundary) -> None:
    """The engine's group callback: prompt the operator to re-cable.

    Fires with the source **already in standby** -- the engine guarantees it,
    and it is the only moment in the subsystem where a live output could reach a
    pair of hands.
    """
    ctx.group_index = boundary.index
    post = _select_current_post(ctx, boundary)
    banner = _rule(f"Group {boundary.index + 1} of {boundary.total}: "
                   f"{boundary.label}")
    panel = f"CAL grp {boundary.index + 1}/{boundary.total}"
    body = wiring_instructions(boundary, current_post=post)
    if ctx.args.yes:
        ctx.console.show(banner, *body[:-2],
                         "--yes: not waiting for an operator", panel_msg=panel)
        return
    ctx.console.prompt(banner, *body, panel_msg=panel,
                       action="press Enter when the wiring above is in place")


#: The engine's safety rules (Global Constraints §8 and §9), and what each one
#: means for the run the operator is watching.  Every one of them **stops the
#: run**, so the row that carries it is the last one, and the line under it is
#: the operator's whole explanation of why the file ends there.
SAFETY_FLAG_LINES: dict[str, str] = {
    FLAG_WIRING_PROOF_FAILED:
        "the safe-level wiring proof did not read back what was sourced -- "
        "the wiring is not what this plan assumes, and the planned level was "
        "never applied to it",
    FLAG_WIRING_PROOF_UNREADABLE:
        "the safe-level wiring proof could not be read from the meter -- the "
        "meter returned no value, so NOTHING IS IMPLIED ABOUT THE WIRING; "
        "the error is on the row's note, and the planned level was never "
        "applied",
    FLAG_GROSS_ERROR_ABORT:
        "the reading is off by more than ten times this point's tolerance -- "
        "that is a connection, not an instrument out of specification",
    FLAG_METER_RANGE_MISMATCH:
        "the meter's own range read-back disagreed with the range it was "
        "configured for, above the hazard threshold -- NOTHING WAS ENERGIZED",
    FLAG_PRESTEP_FAILED:
        "the 10 %-of-target pre-step did not read back the level that was "
        "sourced -- the full value was never applied",
    FLAG_STANDBY_NOT_CONFIRMED:
        "the calibrator did not confirm standby before the meter was to be "
        "re-ranged -- nothing was re-ranged and nothing further was sourced, "
        "but THE OUTPUT MAY STILL BE LIVE: press RESET and check the standby "
        "light before touching the terminals",
}


def safety_lines(row: ResultRow) -> list[str]:
    """What to print under a row that carries one of the engine's aborts.

    M58: the engine proves the wiring and abandons a run on a gross error;
    without this the operator sees a point line, a stack of rows stopping and
    a resume command, with the flag visible only in the CSV.
    """
    lines = []
    for flag, why in SAFETY_FLAG_LINES.items():
        if flag in row.flags:
            lines.append(f"           !! {flag}: {why}")
    return lines


def _with_run_flags(ctx: RunContext, row: ResultRow) -> ResultRow:
    """Add the run-level flags the engine cannot see (M24).

    The engine flags what the instruments told it.  Whether the meter had
    been powered for its stated warm-up is something only the operator knows,
    and it is a stated condition of every figure the row is judged against.
    """
    extra = tuple(flag for flag in ctx.extra_row_flags
                  if flag not in row.flags)
    if not extra:
        return row
    return replace(row, flags=tuple(row.flags) + extra)


def _point_line(ctx: RunContext, row: ResultRow) -> str:
    """One line per point: what was asked for, what came back, and the verdict."""
    unit = row.unit
    label = (row.label or row.quantity.name)[:24]
    if row.error_ppm is not None:
        error = f"{row.error_ppm:+10.3f} ppm"
    elif row.error is None:
        error = f"{'n/a':>14}"
    else:
        # The sign is carried explicitly: at a zero point there is no ppm
        # column, and "2 uV" without a direction is half a measurement.
        error = f"{'+' if row.error >= 0 else '-'}{_q(abs(row.error), unit, 4)}"
        error = f"{error:>14}"
    tur = "  TUR   ---" if row.tur is None else f"  TUR {row.tur:6.2f}"
    mark = "  <<< FAIL" if row.verdict is Verdict.FAIL else ""
    return (f"[{row.point_index + 1:>4}/{ctx.total_points}] "
            f"g{row.group_index + 1} {label:<24} "
            f"{_q(row.source_value, unit, 7):>15} -> "
            f"{_q(row.mean, unit, 7):>15}  err {error}{tur}  "
            f"{row.verdict.value.upper()}{mark}")


def _phase_execute(ctx: RunContext) -> None:
    """Stream the run: one row measured, written, flushed and printed at a time."""
    print()
    print(_rule("Run"))
    _arm_output_limits(ctx)
    ctx.header = _build_header(ctx)
    ctx.writer = open_results(ctx.csv_path, ctx.header, append=ctx.appending)
    print(f"  run id {ctx.run_id}, writing {ctx.csv_path}"
          + (" (appending)" if ctx.appending else ""))

    kwargs: dict[str, Any] = {}
    if ctx.clock is not None:
        kwargs["clock"] = ctx.clock
    if ctx.args.rule is not None:
        kwargs["rule"] = DecisionRule(ctx.args.rule)
    if ctx.args.thermal_emf is not None:
        kwargs["thermal_emf"] = ctx.args.thermal_emf
    if ctx.args.ref_tempco_ref_c is not None:
        kwargs["reference_tempco_ref_c"] = ctx.args.ref_tempco_ref_c
    if getattr(ctx.args, "min_samples", None) is not None:
        kwargs["min_samples"] = ctx.args.min_samples
    windows = meter_temperature_windows(ctx)
    if windows:
        kwargs["meter_temperature_windows"] = windows
    if getattr(ctx.args, "lead_resistance", None) is not None:
        kwargs["lead_resistance_ohm"] = float(ctx.args.lead_resistance)
    kwargs["extra_conditions"] = (
        lambda point, meter_temp, timestamp:
        acal_conditions(ctx, point, meter_temp, timestamp))
    ctx.rows = run_plan(
        ctx.plan, ctx.source, ctx.meter,
        environment=ctx.probe,
        source_spec=ctx.source_spec,
        meter_spec=ctx.meter_spec,
        run_id=ctx.run_id,
        start_group=ctx.start_group,
        start_point=ctx.start_point,
        on_group=lambda boundary: _on_group(ctx, boundary),
        sleep=ctx.sleep,
        **kwargs)

    for row in ctx.rows:
        row = _with_run_flags(ctx, row)
        # Written *before* it is printed: the file is the record, the console
        # is a courtesy, and a Ctrl-C between the two must not lose a point.
        try:
            ctx.writer.write(row)
        except (OSError, ResultError) as exc:
            # A full disk at point 150 is not a traceback -- it is a run that
            # has to end the way every other interrupted run ends, with the
            # rows so far intact and a command to continue it.
            raise WriteFailed(
                f"the result file could not be written at point "
                f"{row.point_index} ({row.label or row.quantity.name}): "
                f"{type(exc).__name__}: {exc}") from exc
        ctx.measured.append(row)
        print(_point_line(ctx, row), flush=True)
        if row.verdict is Verdict.FAIL and row.reason:
            print(f"           {row.reason}", flush=True)
        for line in safety_lines(row):
            print(line, flush=True)


# --------------------------------------------------------------------------
# Phase 8 -- finishing, and the two ways a run ends badly
# --------------------------------------------------------------------------

def _post_run_faults(ctx: RunContext) -> tuple[tuple[int, str], ...]:
    """The source's fault queue after the run, or the pre-run one on failure."""
    if ctx.source is None:
        return ()
    health = _call(ctx.source, "health")
    if health is None:
        return ()
    return tuple(getattr(health, "faults", ()) or ())


def _fault_lines(faults: Sequence[tuple[int, str]]) -> list[str]:
    """Queued faults, with the offline table filling in a missing message.

    The instrument's own ``EXPLAIN?`` text is used when the driver captured
    one; where it did not, the code is decoded from
    ``cal/data/fluke5700a_faults.yaml`` -- the same table the analyzer reads,
    and the only one to trust, because a unit sick enough to have a fault queue
    is the last unit whose ``EXPLAIN?`` should be believed.
    """
    table = load_fault_table()
    return [f"{code}: {text or table.get(int(code), 'unknown fault code')}"
            for code, text in faults]


def _append_faults(ctx: RunContext, faults: Sequence[tuple[int, str]]) -> None:
    """Record the fault queue as a trailing ``# faults=`` comment.

    A fault belongs to the *run*, not to a point, and the provenance block was
    written when the file was opened -- so a fault raised at point 150 can only
    be recorded as a trailing comment.  ``load_results`` collects ``#`` lines
    from anywhere in the file, so it lands in ``RunHeader.extra`` and
    ``cal_analyze.parse_faults`` decodes it from the offline table.

    **Bare codes**, comma separated: the analyzer has the text, and 21 of the
    191 real fault messages contain a comma.
    """
    if not faults or not ctx.csv_path or not os.path.exists(ctx.csv_path):
        return
    codes = ",".join(str(int(code)) for code, _ in faults)
    try:
        with open(ctx.csv_path, "a", encoding="utf-8") as handle:
            handle.write(f"# {FAULTS_KEY}={codes}\n")
    except OSError as exc:                         # pragma: no cover - disk
        print(f"  could not record the fault queue: {exc}", file=sys.stderr)


def _duplicate_points(rows: Sequence[ResultRow]) -> list[int]:
    """Plan indices measured more than once in this file.

    Should always be empty: a resume continues its group at the point it
    stopped at rather than restarting it.  A file written before that was true
    can still carry duplicates, and they **double-count in every verdict total**
    -- a duplicated FAIL is two failures on a certificate -- so the run says so
    rather than quietly de-duplicating.  De-duplicating is not this program's
    call to make: which of two readings of the same point is the real one is a
    question for whoever reads the file.
    """
    counts = Counter(row.point_index for row in rows)
    return sorted(index for index, count in counts.items() if count > 1)


def _summarize(ctx: RunContext) -> None:
    """The counts, over every row in the file -- not only this session's."""
    rows = list(ctx.prior_rows) + list(ctx.measured)
    if not rows:
        print("  no points were measured.")
        return
    summary = summarize_run(rows,
                            temperature_policy=ctx.plan.temperature_policy)
    print(f"  {summary.line()}")
    duplicates = _duplicate_points(rows)
    if duplicates:
        listed = ", ".join(f"#{index}" for index in duplicates[:8])
        more = "" if len(duplicates) <= 8 else f" and {len(duplicates) - 8} more"
        count = len(duplicates)
        print(f"  !! {_plural(count, 'point')} "
              f"{'appears' if count == 1 else 'appear'} more than "
              f"once in this file ({listed}{more}) -- an earlier resume "
              f"re-measured them.  Every count above includes each row, so a "
              f"duplicated FAIL is counted twice; decide which reading stands "
              f"before quoting these numbers.")
    if ctx.prior_rows:
        print(f"  ({len(ctx.measured)} measured in this session, "
              f"{len(ctx.prior_rows)} already in the file)")
    if summary[Verdict.FAIL]:
        print("  FAILED points:")
        for row in rows:
            if row.verdict is Verdict.FAIL:
                print(f"    #{row.point_index} "
                      f"{row.label or row.quantity.name}: "
                      f"error {_q(row.error, row.unit, 4)} against a limit of "
                      f"{_q(row.tolerance_limit, row.unit, 4)}")


def _phase_finish(ctx: RunContext) -> int:
    """Close the file, record the faults, summarize, and render the report.

    In that order: the faults comment must be on disk before the report is
    generated from the file, so the report shows them.
    """
    faults = _post_run_faults(ctx)
    _close_writer(ctx)
    _append_faults(ctx, faults)

    print()
    print(_rule("Summary"))
    _summarize(ctx)
    if faults:
        print(f"  !! the calibrator queued "
              f"{_plural(len(faults), 'fault')} during the run:")
        for line in _fault_lines(faults):
            print(f"       {line}")
    print(f"  file    {ctx.csv_path}")
    print(f"  analyse caldmm-cal-analyze {ctx.csv_path}")
    if not ctx.args.no_report:
        _write_report(ctx)

    rows = list(ctx.prior_rows) + list(ctx.measured)
    failed = any(row.verdict is Verdict.FAIL for row in rows)
    return EXIT_FAIL if failed else EXIT_OK


def _write_report(ctx: RunContext) -> None:
    """Render the analyzer's report next to the CSV, in-process.

    **The CSV is the record; the report is a rendering of it.**  So the rows
    are re-loaded from disk -- picking up the trailing ``# faults=`` comment
    :func:`_append_faults` just wrote -- rather than reused from memory, and
    a failure here changes nothing about the run's exit status: the error and
    the manual command are printed, and the operator still has everything.
    An as-left run that names its counterpart gets the before/after
    comparison, exactly as ``caldmm-cal-analyze`` would render it.
    """
    try:
        header, rows = load_results(ctx.csv_path)
        compare = None
        counterpart, warning = find_counterpart(ctx.csv_path, header)
        if warning:
            print(f"  !! {warning}")
        elif counterpart:
            _, before_rows = load_results(counterpart)
            compare = (counterpart, before_rows)
            print(f"  compare {counterpart} (the as-found counterpart)")
        pdf_path, md_path = report_output_paths(ctx.csv_path)
        text = format_markdown(ctx.csv_path, header, rows, compare=compare)
        with open(md_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        pages = render_pdf(ctx.csv_path, header, rows, pdf_path,
                           compare=compare)
        print(f"  report  {md_path}")
        print(f"  plots   {pdf_path} ({_plural(pages, 'page')})")
    except Exception as exc:                       # noqa: BLE001 - reported
        print(f"  !! the report could not be rendered "
              f"({type(exc).__name__}: {exc}).  The CSV above is the record "
              f"and is unaffected; generate the report by hand with:\n"
              f"     caldmm-cal-analyze {ctx.csv_path}", file=sys.stderr)


def _phase_abandoned(ctx: RunContext, message: str,
                     faults: Sequence[tuple[int, str]] | None = None) -> int:
    """A run that stopped early: say what was kept and exactly how to continue.

    *faults* is passed in by the source-fault path, which already has the queue
    that stopped the run: draining is how the 5700A's inhibit is cleared, so
    asking a second time answers "none" about the very faults being reported.
    """
    if faults is None:
        faults = _post_run_faults(ctx)
    _close_writer(ctx)
    _append_faults(ctx, faults)

    print()
    print(_rule("Interrupted"))
    print(f"  {message}")
    for line in _fault_lines(faults):
        print(f"  fault   {line}")
    if not ctx.csv_path or not os.path.exists(ctx.csv_path):
        print("  nothing was measured and no result file was written.")
        return EXIT_ABORTED
    print(f"  {_plural(len(ctx.measured), 'point')} measured in this session "
          f"{'is' if len(ctx.measured) == 1 else 'are'} on disk; every row was "
          f"flushed as it was taken.")
    print(f"  file    {ctx.csv_path}")
    print(f"  resume  {ctx.resume_hint()}")
    return EXIT_ABORTED


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------

def _guard(what: str, action: Callable[[], Any]) -> None:
    """Run one shutdown step; report a failure and carry on to the next.

    Every step is independent and the source's is the one that matters, so a
    meter that has already dropped off the bus must not be able to stop it.
    """
    try:
        action()
    except Exception as exc:                       # noqa: BLE001 - reported
        print(f"  shutdown: {what} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def _close_writer(ctx: RunContext) -> None:
    writer, ctx.writer = ctx.writer, None
    if writer is not None:
        _guard("closing the result file", writer.close)


def _emergency(ctx: RunContext) -> None:
    """The SIGINT handler's cleanup: de-energize, and nothing else.

    Deliberately **not** the full shutdown, and it does not touch the file --
    the writer has already flushed every row it wrote, so the record on disk is
    complete to the last measured point without any help here.  The device
    handles are closed on the way out of :func:`run`, after the engine's
    generator has been closed and its own ``finally`` has standbyed one last
    time; closing the transport from inside the signal handler would leave that
    ``finally`` writing to a closed descriptor, and the ``OSError`` it raised
    would replace the ``SystemExit`` and escape :func:`run`'s handlers entirely.
    """
    if ctx.source is not None:
        _guard("standby", ctx.source.standby)
        print("  source in standby.")


def _shutdown(ctx: RunContext) -> None:
    """Close the generator, de-energize, close the file, close the handles.

    In that order, and idempotently: it runs from the normal path and from every
    error path, and the generator's own ``finally`` calls ``standby()`` again --
    which is safe only while the transport is still open.
    """
    if ctx.shut_down:
        return
    ctx.shut_down = True
    rows, ctx.rows = ctx.rows, None
    if rows is not None:
        # ValueError("generator already executing") when a SIGINT landed inside
        # the engine; the SystemExit that follows runs its finally anyway.
        _guard("closing the engine", rows.close)
    if ctx.source is not None:
        _guard("standby", ctx.source.standby)
    # Guarded like every other step: restoring a courtesy setting must not be
    # able to stop the file and the handles from being closed.
    _guard("restoring the entry limits", lambda: _restore_output_limits(ctx))
    _close_writer(ctx)
    for label, role in (("the source", ctx.source), ("the meter", ctx.meter)):
        device = getattr(role, "device", None)
        close = getattr(device, "close", None)
        if close is not None:
            _guard(f"closing {label}", close)
    # Last, so a failure above cannot leave a handler that raises SystemExit
    # into whatever runs next.
    _restore_termination_handlers(ctx.previous_signals)


# --------------------------------------------------------------------------
# --adjust: the DUT's own calibration adjustment
# --------------------------------------------------------------------------
#
# A different kind of run entirely, and it shares almost nothing with the
# verification above except the two instrument-spec arguments.  It writes no
# result CSV, and every sequence lives in a ``cal.dmm_procedures*`` module.
#
# **Whether it drives a calibrator depends on the meter**, which is why the
# ``--source`` refusal is taken from the procedure's own ``needs_source``
# rather than hard-coded here.  The HP 3458A's standards are certified
# artefacts the operator connects by hand, so naming a calibrator would be
# misleading.  The HP 34401A's twenty-seven gain points are stepped by one, so
# not naming a calibrator is fatal.

#: The registered ``procedure_id`` ``--adjust`` looks up.
ADJUST_PROCEDURE_ID = _PROCEDURE_ID

#: **The non-volatile calibration state is unknown.**  A ``CAL`` reached the
#: meter and its outcome could not be read back.  Its own status because a
#: script that folded it into either 0 or 1 would be stating something about
#: the meter that nobody knows.  ``--adjust`` only.
EXIT_UNKNOWN = 4
#: **The session record is not where it was asked to be.**  Takes precedence
#: over every other status, including :data:`EXIT_UNKNOWN`: the unknown NVRAM
#: state can be resolved later with ``CALNUM?``, but the account of what was
#: done disappears when the terminal is closed.  ``--adjust`` only.
EXIT_NO_RECORD = 5

#: Verification-run options that have no meaning under ``--adjust``.  Refused
#: rather than ignored: silently dropping an argument on a run that overwrites
#: calibration memory is how an operator ends up believing something else
#: happened.
_ADJUST_CONFLICTS: tuple[tuple[str, str], ...] = (
    ("plan", "--plan"), ("resume", "--resume"), ("csv", "--csv"),
    ("start_group", "--start-group"), ("quantities", "--quantities"),
    ("percentages", "--percentages"), ("frequencies", "--frequencies"),
    ("connections", "--connections"),
    ("include_negative", "--include-negative/--no-negative"),
    ("interval", "--interval"), ("samples", "--samples"),
    ("discards", "--discards"), ("min_spec_ratio", "--min-spec-ratio"),
    ("max_voltage", "--max-voltage"),
    ("confidence", "--confidence"), ("tur_floor", "--tur-floor"),
    ("guard_band", "--guard-band"),
    ("temperature_policy", "--temperature-policy"),
    ("points", "--points"), ("acal", "--acal"),
    ("acal_settle", "--acal-settle"), ("acal_recent", "--acal-recent"),
    ("lead_resistance", "--lead-resistance"),
    ("run_kind", "--run-kind"), ("counterpart", "--counterpart"),
    ("adjust_log", "--adjust-log"),
)


def adjust_refusals(args: argparse.Namespace, *,
                    wants_source: bool = False,
                    policy: str | None = None) -> None:
    """Everything ``--adjust`` will not do, decided entirely offline.

    **An adjustment and a verification are refused in one invocation.**  It
    would be convenient -- the leads are connected, the operator is standing
    there -- and it is exactly the check that means nothing: verifying a meter
    against the same reference that was just used to adjust it measures the
    reference against itself and will report agreement whatever either
    instrument does.  So ``--adjust`` is its own invocation and the command to
    verify afterwards is printed at the end of it.

    *policy* comes from the procedure's own
    :func:`~caldmm.cal.procedure.source_policy` declaration, because the
    answer is not the same for every meter and hard-coding any one of them is
    wrong for the others.  The **34401A** steps a calibrator through
    twenty-seven points, so not naming one is fatal -- and that has to be
    found here, while it is still an argument mistake, rather than after a
    meter has been put into remote and an operator has typed a security code.
    The **3458A** takes its values from certified artefacts connected by hand
    *or* from a calibrator standing in for them, so a source is accepted but
    never required.  The 5700A's own procedures source internally, so naming
    one is refused as misleading.  (*wants_source* is the old boolean
    spelling, kept for callers that predate the three-way policy.)
    """
    if policy is None:
        policy = "required" if wants_source else "none"
    if not args.meter:
        raise VerifyError("--adjust needs --meter: it adjusts the DUT, and "
                          "the DUT is the meter")
    if args.dut != "meter":
        raise VerifyError(
            "--adjust --dut source is not this tool: the calibrator's own "
            "calibration is caldmm-5700a-selfcal, which drives CAL_REF / "
            "CAL_ADJ / CAL_STORE against three artefacts. --adjust here "
            "adjusts the meter")
    if policy == "required" and not args.source:
        raise VerifyError(
            "this meter's adjustment steps its applied values with a "
            "calibrator and --source names none. Nothing was opened. Add "
            "--source, e.g. --source fluke5700a:gpib0,4")
    if args.source and policy == "none":
        raise VerifyError(
            "this meter's adjustment does not drive a source, so naming one "
            "would be misleading. The 3458A adjustment is made against "
            "certified artefacts you connect by hand -- a 10 V dc standard "
            "and a 10 kΩ resistance standard -- and you enter each one's "
            "certified value and uncertainty at the prompt. Drop --source")
    if args.waive_as_found and args.as_found:
        raise VerifyError(
            "--waive-as-found and --as-found contradict each other. Pass the "
            "CSV, or waive it, not both")
    if args.yes:
        raise VerifyError(
            "--yes is refused with --adjust. This run overwrites calibration "
            "memory irreversibly and every one of its prompts asks the "
            "operator to move a short, a lead or the Terminals switch; "
            "answering them without being there would adjust the meter "
            "against whatever happened to be connected")
    if args.no_report:
        raise VerifyError(
            "--no-report has no meaning with --adjust: an adjustment writes "
            "a session record, not a verification report, and the record is "
            "never optional. Drop --no-report")
    if args.dry_run:
        raise VerifyError(
            "--dry-run is refused with --adjust, because the two mean "
            "different things and the difference matters. --dry-run touches "
            "no instrument at all; --adjust is ALREADY a dry run by default "
            "but is not a simulation -- it really selects functions and "
            "ranges and really reads the standards, and holds back only the "
            "CAL commands. Use --adjust alone to rehearse, and --adjust "
            "--adjust-commit to write")
    if args.force and not args.source:
        raise VerifyError(
            "--force has no meaning with --adjust for this meter: it "
            "overrides the calibrator's dc-zero interval, and this "
            "adjustment opens no calibrator")
    given = [flag for name, flag in _ADJUST_CONFLICTS
             if getattr(args, name, None) is not None]
    if given:
        raise VerifyError(
            f"{', '.join(given)} {'is' if len(given) == 1 else 'are'} part of "
            f"a verification run and --adjust is not one. Adjusting and then "
            f"verifying in a single invocation is refused on purpose: a "
            f"verification against the same reference that was just used to "
            f"adjust is not an independent check. Run --adjust first, then "
            f"run the verification as a separate command -- it is printed for "
            f"you at the end")


#: Options that only mean anything under ``--adjust``.  Silently ignoring one
#: in a module whose stated principle is "refused rather than ignored" is
#: worst of all for ``--adjust-commit``, whose whole content is consent to an
#: irreversible write.
_ADJUST_ONLY: tuple[tuple[str, str], ...] = (
    ("adjust_commit", "--adjust-commit"),
    ("adjust_record", "--adjust-record"),
    ("operator", "--operator"),
    ("notes", "--notes"),
    ("terminals", "--terminals"),
    ("as_found", "--as-found"),
    ("waive_as_found", "--waive-as-found"),
    ("adjust_groups", "--adjust-groups"),
    ("adjust_resume", "--adjust-resume"),
    ("adjust_resume_done", "--adjust-resume-done"),
)


def adjust_only_refusals(args: argparse.Namespace) -> None:
    """Refuse an adjustment-only option on a run that is not an adjustment."""
    given = [flag for name, flag in _ADJUST_ONLY if getattr(args, name, None)]
    if given:
        raise VerifyError(
            f"{', '.join(given)} {'has' if len(given) == 1 else 'have'} no "
            f"meaning without --adjust, and a verification run would silently "
            f"ignore {'it' if len(given) == 1 else 'them'}. That is a "
            f"particularly bad thing to ignore for --adjust-commit, whose "
            f"whole content is consent to overwriting calibration memory. Add "
            f"--adjust, or drop {'it' if len(given) == 1 else 'them'}")


def linkage_refusals(args: argparse.Namespace) -> None:
    """``--counterpart``/``--adjust-log`` without ``--run-kind`` are refused.

    Both flags say *where this run sits in a calibration cycle*, and a
    ``standalone`` header carrying a counterpart is a record that contradicts
    itself -- the analyzer would not know which way round to compare, so it
    would not compare at all, and the linkage would be dead weight nobody
    notices.  Refusing is the same principle as everywhere else in this
    module: an argument is honoured or refused, never recorded half-meant.
    """
    if args.run_kind:
        return
    given = [flag for name, flag in (("counterpart", "--counterpart"),
                                     ("adjust_log", "--adjust-log"))
             if getattr(args, name, None)]
    if given:
        raise VerifyError(
            f"{', '.join(given)} name{'s' if len(given) == 1 else ''} this "
            f"run's place in an as-found/as-left calibration cycle, and "
            f"--run-kind says which place that is. Without it the pairing is "
            f"ambiguous -- the analyzer compares an as-left run against its "
            f"counterpart, not the other way round. Add --run-kind as-found "
            f"or --run-kind as-left")


def adjust_record_base(args: argparse.Namespace, model_id: str,
                       now: datetime | None = None) -> str:
    """``3458a-adjust-20260802T143000`` -- where the record goes by default.

    ``--adjust-record`` is optional on purpose.  The account of an irreversible
    operation is not something to lose because a flag was forgotten, so a
    session always has somewhere to write and the path is printed before the
    first step.
    """
    if args.adjust_record:
        return str(args.adjust_record)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return f"{model_id}-adjust-{stamp}"


def _prepare_adjust_record(path: str) -> None:
    """Refuse to overwrite, then **prove the path can be written**, before the
    instrument is opened.

    Two different checks and only the second one is the interesting one: a
    typo'd or unmounted ``--adjust-record`` passes "does not already exist"
    trivially and is then discovered at the end of an hour, with the meter
    already adjusted and the only account of it on a terminal somebody is about
    to close.  ``apps/fluke5700a_selfcal._check_writable`` is the precedent.
    """
    if os.path.exists(path):
        raise VerifyError(
            f"{path} already exists and would be overwritten. Choose another "
            f"--adjust-record. It is the record of an adjustment that cannot "
            f"be repeated to reproduce it")
    directory = os.path.dirname(path) or "."
    # A missing parent is created rather than refused -- ``--adjust-record
    # runs/2026-08-02/adjust`` is a reasonable thing to type.  But it is
    # announced, because creating it silently is exactly what makes a typo'd
    # path succeed: the operator's only clue that ``--adjust-record
    # rusn/adjust`` went somewhere unintended is seeing the directory named.
    created = not os.path.isdir(directory)
    try:
        os.makedirs(directory, exist_ok=True)
        if created:
            print(f"  note: created the directory {directory!r} for the "
                  f"session record -- check that is where you meant it to go")
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        raise VerifyError(
            f"{path} cannot be written ({type(exc).__name__}: {exc}). This "
            f"session's record would have had nowhere to go, so nothing has "
            f"been sent to the meter. Fix --adjust-record and start "
            f"again") from exc
    try:
        os.unlink(path)
    except OSError:                                    # pragma: no cover - race
        pass


def adjust_sequence_complete(result: ProcedureResult) -> bool:
    """Did the whole sequence run, counting a dry run's held-back writes?

    **Not** ``not result.failed``.  A run in which the typed commit
    confirmation was declined has ``failed is False``, stored nothing, and must
    never read as a success.

    A dry run can never be ``ProcedureResult.complete`` -- its NVRAM steps are
    ``SKIPPED_DRY_RUN`` by design, which is the outcome the operator asked for
    -- so a held-back step counts as having run while everything that means the
    run stopped still says no.

    Mirrors ``apps.fluke5700a_selfcal.sequence_complete``.  Not imported from
    there: that module imports the Fluke 5700A driver at module scope, and this
    one must stay usable on a bench with only a DMM.
    """
    if not result.dry_run:
        return result.complete
    if result.interrupted or result.unreached or result.error:
        return False
    return all(step.outcome.ran
               or step.outcome is StepOutcome.SKIPPED_DRY_RUN
               for step in result.steps)


def adjust_exit_status(result: ProcedureResult) -> int:
    """The process status for a finished adjustment.

    Ordered so the three-valued NVRAM answer is decided first.  Branches on
    ``nvram_uncertain`` / ``committed`` / :func:`adjust_sequence_complete` and
    **never on** ``result.failed``.
    """
    if result.nvram_uncertain:
        return EXIT_UNKNOWN
    if result.committed:
        # Something was confirmed written.  That can be true of a run that did
        # not otherwise finish, and the calibration is stored either way; the
        # loose ends are printed loudly by _print_adjust_verdict.
        return EXIT_OK
    if adjust_sequence_complete(result):
        return EXIT_OK
    if result.interrupted:
        return EXIT_ABORTED
    return EXIT_FAIL


def _model_label(model_id: str | None) -> str:
    """``hp34401a`` -> ``34401A``, for operator-facing text."""
    text = str(model_id or "meter")
    return re.sub(r"^[a-z]+(?=\d)", "", text).upper() or text


def _point_rows_lines(captures: dict) -> list[str]:
    """The per-point readings a procedure captured, for the record.

    ``as_measured`` is what each check step read before anything was
    written (the as-found values at the adjustment points); ``adjusted`` is
    each point's trial reading just before its ``CAL?``, with the count it
    left.  Both were held in memory and never printed until 2026-11-02, when
    an evening of adjustment left no per-point record.
    """
    lines: list[str] = []
    for key, title in (("as_measured", "check steps (as found at each point)"),
                       ("adjusted", "adjust steps (trial reading, then CAL?)")):
        rows = captures.get(key) or {}
        if not rows:
            continue
        lines.append(f"  {title}:")
        for point_id, row in rows.items():
            reading = row.get("reading", row.get("reading_before"))
            dev = row.get("deviation_ppm")
            count = row.get("cal_count")
            lines.append(
                f"    {point_id:16s} applied {row.get('applied'):+.9g} "
                f"{row.get('unit', '')}  read {reading:+.9g}  "
                + (f"dev {dev:+.2f} ppm" if dev is not None else "")
                + (f"  CAL:COUN {count}" if count is not None else ""))
    return lines


def format_adjust_record(result: ProcedureResult, *, model_id: str,
                         instrument: str, idn: str = "",
                         argv: Sequence[str] | None = None) -> str:
    """The session record.  Contains no security code, by construction.

    The code the operator typed is held on the procedure context and never
    reaches a :class:`~caldmm.cal.procedure.StepResult`: every declared
    command is the bare ``CAL <value>`` / ``ACAL AC``, and the code is appended
    only on the wire.
    """
    lines = [
        "caldmm-cal-verify --adjust session record",
        _rule(),
        f"written    {datetime.now().astimezone().isoformat(' ', 'seconds')}",
        f"caldmm    {caldmm_version()}  ({_git_revision()})",
        f"command    {' '.join(argv or sys.argv)}",
        f"model      {model_id}",
        f"instrument {instrument}",
        f"*IDN?/ID?  {idn or 'unknown'}",
        f"serial     {result.serial or 'unknown'}",
        f"operator   {result.operator or 'not recorded'}",
        f"notes      {result.notes or '-'}",
        "",
        _rule("Result"),
        result.summary(),
        "",
        _rule("Step log"),
    ]
    lines.extend(f"  {line}"
                 for line in result.detail.get("log", ()) or ("-",))
    point_lines = _point_rows_lines(result.detail.get("captures", {}) or {})
    if point_lines:
        lines += ["", _rule("Per-point readings")] + point_lines
    notes = result.detail.get("not_implemented") or ()
    if notes:
        lines += ["", _rule("Not covered by this procedure")]
        lines += [f"  - {note}" for note in notes]
    lines.append("")
    return "\n".join(lines)


def _write_adjust_record(text: str, path: str) -> tuple[str, bool]:
    """Put the record on disk.  Returns ``(where, displaced)``.

    Called the instant a result exists -- before the summary is printed, before
    anything else that could raise -- and from every exception path including
    the interrupt.  An irreversible operation whose record was lost to a Ctrl-C
    is the failure this ordering exists to prevent.
    """
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path, False
    except OSError as exc:                             # noqa: BLE001 - reported
        print(f"  !! writing {path} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    try:
        handle_fd, fallback = tempfile.mkstemp(prefix="caldmm-cal-adjust-",
                                               suffix=".log")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return fallback, True
    except OSError as exc:                             # pragma: no cover - disk
        print(f"  !! the fallback copy failed too ({type(exc).__name__}: "
              f"{exc})", file=sys.stderr)
        return "", True


def _record_adjust(ctx: RunContext, result: ProcedureResult,
                   path: str) -> bool:
    """Render, write, then print.  **In that order, always.**"""
    try:
        text = format_adjust_record(
            result, model_id=ctx.meter_model_id,
            instrument=ctx.meter_instrument, idn=ctx.adjust_idn)
    except Exception as exc:                           # noqa: BLE001 - reported
        print(f"  !! the session record could not be formatted "
              f"({type(exc).__name__}: {exc}); writing the bare summary",
              file=sys.stderr)
        text = result.summary()
    where, displaced = _write_adjust_record(text, path)

    print()
    print(_rule("Result"))
    print(result.summary())
    print()
    print(_rule("Record"))
    if not displaced:
        print(f"  {where}")
    else:
        print(f"  !! THE SESSION RECORD COULD NOT BE WRITTEN TO {path}.")
        if where:
            print(f"     It was saved to {where} instead.  COPY IT SOMEWHERE "
                  f"PERMANENT NOW --")
            print("     that is a temporary file and the system may delete "
                  "it.")
        else:
            print("     It could not be saved anywhere.  THIS TERMINAL IS "
                  "THE ONLY COPY")
            print("     of what happened -- copy the text above by hand "
                  "before closing it.")
        print(f"     This session exits {EXIT_NO_RECORD}, not 0: the record "
              f"is not where it was asked to be.")
    return displaced


def _print_adjust_verdict(ctx: RunContext, result: ProcedureResult,
                          path: str) -> None:
    """What happened to non-volatile calibration memory, in words."""
    print()
    print(_rule("Calibration memory"))
    if result.nvram_uncertain:
        step = next((s for s in result.steps if s.nvram_uncertain), None)
        sent = "; ".join(step.commands) if step is not None else "CAL"
        print("  !! THE STATE OF NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN.")
        print(f"     {sent} reached the meter and the outcome could NOT be "
              f"read back.")
        print("     This run is NEITHER a success NOR a failure.  Treat the "
              "meter as")
        print("     POSSIBLY ADJUSTED until somebody checks, by hand:")
        print()
        print("         CALNUM?   the number of times it has been adjusted")
        print("         CALSTR?   the stored calibration message")
        print()
        before = (result.detail.get("captures", {}).get("before", {})
                  or {}).get("calnum")
        if before is not None:
            print(f"     It read {before} before this session.  A higher "
                  f"number means the")
            print("     adjustment went in; the same number means it did "
                  "not.")
        print("     Do not use this meter as a reference until you know "
              "which it is.")
        return

    if result.committed:
        print(f"  ** COMMITTED: new calibration constants are in the "
              f"{_model_label(ctx.meter_model_id)}'s non-volatile")
        print("     memory.  The previous ones are gone and are not "
              "recoverable.")
        if not adjust_sequence_complete(result):
            print("     The sequence did not otherwise finish -- read the "
                  "step list and the")
            print("     warnings above.")
    elif result.dry_run:
        print("  DRY RUN: nothing was written to calibration memory.")
        print("     Every CAL that would have been sent is listed above "
              "under")
        print("     'NOT committed'.  Re-run with --adjust-commit to write "
              "them.")
    else:
        print("  NOT COMMITTED: nothing was written to calibration memory.")
        print("     A step failed, was declined, or the run stopped -- see "
              "above.")

    for note in result.dry_run_notes:
        print(f"  {note}")
    print()
    print("  Verify it INDEPENDENTLY before quoting it -- against a "
          "calibrator, not")
    print("  against the artefacts it was just adjusted with:")
    # ``args.meter`` verbatim, not the parsed instrument: the parsed form has
    # the model id split off and would not round-trip back into --meter.
    print(f"    caldmm-cal-verify --source fluke5700a:<addr> "
          f"--meter {ctx.args.meter} --dut meter")
    print(f"  The session record is {path}")


def _phase_adjust(ctx: RunContext) -> int:
    """Run the DUT's registered adjustment procedure.  Its own kind of run."""
    args = ctx.args
    # --meter is parsed *before* the refusals so the procedure can be looked
    # up and asked whether it wants a source. Parsing a role spec opens
    # nothing; it splits a string.
    if not args.meter:
        raise VerifyError("--adjust needs --meter: it adjusts the DUT, and "
                          "the DUT is the meter")
    ctx.meter_model_id, ctx.meter_instrument = parse_role_spec(args.meter,
                                                               "--meter")
    ctx.meter_spec = _meter_option(
        _load_spec(ctx.meter_model_id, "--meter"),
        getattr(ctx.args, "meter_option", None))
    try:
        procedure = get_procedure(ctx.meter_model_id, ADJUST_PROCEDURE_ID)
    except ProcedureError as exc:
        raise VerifyError(str(exc)) from exc
    # Refused here, offline, rather than by the raising hooks a registry entry
    # may also have: an operator learns why before anything is opened.
    if not getattr(procedure, "implemented", True):
        raise VerifyError(getattr(procedure, "reason", str(procedure)))

    policy = source_policy(procedure)
    adjust_refusals(args, policy=policy)
    wants_source = policy == "required" or (policy == "optional"
                                            and bool(args.source))

    commit = bool(args.adjust_commit)
    path = adjust_record_base(args, ctx.meter_model_id) + ".log"
    _prepare_adjust_record(path)                   # before the instrument

    print()
    print(_rule("Adjustment"))
    print(f"  procedure      {procedure.name}")
    print(f"  meter          {ctx.meter_model_id}  {ctx.meter_instrument}")
    print(f"  mode           "
          + ("COMMITTING RUN -- CAL will be sent and calibration memory "
             "OVERWRITTEN"
             if commit else
             "dry run (the default) -- everything but the CAL commands really "
             "runs"))
    print(f"  session record {path}")
    if not commit:
        print("  Nothing will be written to calibration memory.  Pass "
              "--adjust-commit to write.")

    # allow_calibration_write is passed True *only* for a committing run, so a
    # dry run is locked at the driver as well as at the procedure: two
    # independent refusals, neither of which relies on the other.
    try:
        ctx.meter = open_meter(ctx.meter_model_id, ctx.meter_instrument,
                               allow_calibration_write=commit)
    except Exception as exc:                           # noqa: BLE001 - reported
        raise VerifyError(
            f"cannot open the meter {ctx.meter_instrument} "
            f"({ctx.meter_model_id}): {exc}") from exc

    device = getattr(ctx.meter, "device", None)
    query = _IDN_QUERY.get(ctx.meter_model_id, _DEFAULT_IDN_QUERY)
    ctx.adjust_idn = _call(device, "idn") or _ask(device, query)
    print(f"  {query:<14} {ctx.adjust_idn or 'unknown'}")
    ctx.console = OperatorConsole.for_instrument(device)

    # The calibrator, for procedures that step their own applied values.
    # Opened after the meter so a meter that cannot be reached fails before a
    # second instrument is put into remote.
    if wants_source:
        ctx.source_model_id, ctx.source_instrument = parse_role_spec(
            args.source, "--source")
        try:
            ctx.source = open_source(ctx.source_model_id,
                                     ctx.source_instrument)
        except Exception as exc:                       # noqa: BLE001
            raise VerifyError(
                f"cannot open the calibrator {ctx.source_instrument} "
                f"({ctx.source_model_id}): {exc}") from exc
        print(f"  calibrator     {ctx.source_model_id}  "
              f"{ctx.source_instrument}")

    pctx = ProcedureContext(
        device=device, console=ctx.console, commit=commit,
        source=ctx.source,
        ambient_c=args.ambient, operator=args.operator or "",
        notes=args.notes or "")
    if args.terminals is not None:
        pctx.terminals = args.terminals
    if args.as_found:
        pctx.as_found = args.as_found
    if args.waive_as_found:
        pctx.as_found_waived = True
    if args.adjust_groups:
        pctx.only_groups = tuple(g.strip() for g in args.adjust_groups.split(",")
                                 if g.strip())
    if bool(args.adjust_resume_done) != (args.adjust_resume is not None):
        raise VerifyError(
            "--adjust-resume-done and --adjust-resume go together: the steps "
            "an earlier session completed, and the CAL:COUN? it ended on, "
            "which is what proves nothing was written since")
    if args.adjust_resume_done:
        pctx.prior_steps = tuple(s.strip() for s in
                                 args.adjust_resume_done.split(",") if s.strip())
        pctx.resume_count = int(args.adjust_resume)

    # From here on there is one obligation: a result exists, and it reaches
    # disk before anything else happens to it.
    try:
        result = run_procedure(procedure, pctx)
    except PreflightError as exc:
        print()
        print(_rule("Refused"))
        print("  the meter is not in a state this procedure will start from, "
              "and NOTHING")
        print("  WAS SENT:")
        for problem in exc.problems:
            print(f"    - {problem}")
        return EXIT_USAGE
    except (KeyboardInterrupt, SystemExit):
        result = _finalize_adjust(procedure, pctx,
                                  "interrupted before the runner returned",
                                  interrupted=True)
        print()
        print(_rule("Interrupted"))
        if result is None:
            where, displaced = _write_adjust_record(
                _emergency_adjust_text(pctx), path)
            print(f"  the raw step log went to {where or 'nowhere'}")
            return EXIT_NO_RECORD if displaced else EXIT_ABORTED
        displaced = _record_adjust(ctx, result, path)
        _print_adjust_verdict(ctx, result, path)
        return EXIT_NO_RECORD if displaced else EXIT_ABORTED
    except ProcedureError as exc:
        result = _finalize_adjust(procedure, pctx, str(exc),
                                  interrupted=False)
        print()
        print(_rule("Refused"))
        print(f"  {exc}")
        if result is None:
            return EXIT_USAGE
        displaced = _record_adjust(ctx, result, path)
        _print_adjust_verdict(ctx, result, path)
        return EXIT_NO_RECORD if displaced else EXIT_USAGE

    displaced = _record_adjust(ctx, result, path)
    _print_adjust_verdict(ctx, result, path)
    status = adjust_exit_status(result)
    return EXIT_NO_RECORD if displaced else status


def _finalize_adjust(procedure: Any, pctx: ProcedureContext, error: str, *,
                     interrupted: bool) -> ProcedureResult | None:
    """Build the record after the runner did not return one.

    Through the procedure's own ``finalize`` -- the identical call
    ``run_procedure`` makes -- so this app never fabricates a result out of a
    state the framework cannot produce.
    """
    if interrupted:
        pctx.interrupted = True
    if not pctx.error:
        pctx.error = error
    try:
        return procedure.finalize(pctx)
    except Exception as exc:                           # noqa: BLE001 - reported
        print(f"  !! the run record could not be assembled: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _emergency_adjust_text(pctx: ProcedureContext) -> str:
    """Last resort: the raw step log, when ``finalize`` itself broke."""
    return "\n".join([
        "caldmm-cal-verify --adjust EMERGENCY session record",
        "the run result could not be assembled; this is the raw step log",
        f"written  {datetime.now().astimezone().isoformat(' ', 'seconds')}",
        f"error    {pctx.error or 'unknown'}",
        "",
        *pctx.log,
        "",
        *(f"warning: {warning}" for warning in pctx.warnings),
        "",
    ])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caldmm-cal-verify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a calibration verification: source a plan of points, "
                    "measure each one, decide conformity, stream the result "
                    "to CSV.",
        epilog="exit status: 0 finished with no FAIL, 1 finished with at "
               "least one FAIL, 2 usage or a refusal to start, 3 interrupted "
               "(the printed --resume command continues it).  --adjust adds "
               "two: 4 the state of the meter's calibration memory is UNKNOWN "
               "and must be checked by hand, 5 the session record is not "
               "where it was asked to be (which wins over all of the above).")

    instruments = parser.add_argument_group("instruments")
    # Not argparse-required, because --adjust drives no source and naming one
    # there is refused rather than ignored.  A verification run without it
    # still stops before anything is opened -- see _phase_plan.
    instruments.add_argument(
        "--source", default=None, metavar="MODEL[:NAME]:ADDR",
        help="the sourcing instrument, e.g. fluke5700a:4 or "
             "fluke5700a:cal1:4.  The model id chooses the specification "
             "and the adapter; the name is your own designator for the unit.  "
             "A GPIB ADDR may be 'bus,pad' (or 'gpib1,4') when the "
             "instruments sit on different USB-GPIB adapters; an ADDR "
             "containing a slash, or a Windows COMn port, is a serial port: "
             "'/dev/ttyUSB0', '/dev/ttyUSB0@19200', 'COM3@19200'.  "
             "Required for a verification run; refused with --adjust, which "
             "drives no source")
    instruments.add_argument(
        "--meter", required=True, metavar="MODEL[:NAME]:ADDR",
        help="the measuring instrument, e.g. hp3458a:22, "
             "hp3458a:dmm1:22 or hp3458a:dmm1:1,22 (GPIB bus 1)")
    instruments.add_argument(
        "--meter-option", default=None, metavar="OPT",
        help="a factory option of the meter that changes its specification, "
             "e.g. 002 for a 3458A with Option 002 (high stability): its "
             "tighter dc-volts figures are then the ones the meter is judged "
             "against (or, as the reference, budgeted with).  Recorded in "
             "the result file")
    instruments.add_argument(
        "--dut", choices=("source", "meter"), default="meter",
        help="which instrument is under test (default: meter).  It decides "
             "which side's specification supplies the tolerance limit and "
             "which becomes the reference component of the budget")

    plan_group = parser.add_argument_group(
        "the plan", "either --plan a file, or generate one from the models")
    plan_group.add_argument("--plan", metavar="PLAN.yaml", default=None,
                            help="run this plan instead of generating one")
    plan_group.add_argument("--quantities", default=None, metavar="LIST",
                            help="comma-separated: DCV,ACV,DCI,ACI,RES,WBV "
                                 "(default: everything both instruments do)")
    plan_group.add_argument("--percentages", default=None, metavar="LIST",
                            help="comma-separated percentages of each range's "
                                 "full scale (default: "
                                 + ",".join(f"{p:g}"
                                            for p in DEFAULT_PERCENTAGES) + ")")
    plan_group.add_argument("--frequencies", default=None, metavar="LIST",
                            help="comma-separated tones for the ac points, or "
                                 "'manufacturer' for the specification break "
                                 "points the manufacturers' own verification "
                                 "tables use (default: one tone per band of "
                                 "each range)")
    plan_group.add_argument(
        "--connections", default=None, metavar="LIST",
        help="comma-separated wirings to plan, e.g. '4w,2w'.  Default: one "
             "per quantity, the best both instruments offer -- changing the "
             "wiring means the operator physically re-cables, so a plan that "
             "silently doubled the resistance run to cover both would not be "
             "a kindness.  Ask for both when you want both physical paths "
             "exercised: a 4-wire-only resistance run never tests the 2-wire "
             "path.  Each wiring becomes its own group, with its own "
             "re-cabling stop.  A wiring neither instrument offers on a "
             "quantity is reported, not substituted")
    plan_group.add_argument(
        "--points", choices=POINT_SETS, default=None,
        help="which points to generate (default: full).  'full' is every "
             "range of every function both instruments share -- the "
             "10/50/90/100 %% ladder, both polarities, a tone per band -- plus "
             "every row of the manufacturer's own performance-verification "
             "tables (3458A Calibration Manual Tables 4-1 to 4-6, labelled "
             "[CM 4-x #n]) and the few supplementary points that reach the "
             "ranges those tables skip.  'manufacturer' is the Calibration "
             "Manual's rows alone (42 points on a 3458A); 'ladder' the generated "
             "ladder alone")
    plan_group.add_argument("--include-negative", dest="include_negative",
                            action="store_true", default=None,
                            help="measure each dc setpoint in both polarities "
                                 "(the default)")
    plan_group.add_argument("--no-negative", dest="include_negative",
                            action="store_false", default=None,
                            help="positive setpoints only -- halves a dc run")
    plan_group.add_argument("--interval", default=None, metavar="COLUMN",
                            help="the DUT's specification column to judge "
                                 "against, e.g. 24h, 90d, 1y")
    plan_group.add_argument("--samples", type=int, default=None,
                            help=f"readings averaged per point (default "
                                 f"{DEFAULT_SAMPLES})")
    plan_group.add_argument(
        "--min-samples", type=int, default=3, metavar="N",
        help="early termination: take N samples, then one at a time up to "
             "--samples, stopping as soon as the block's spread and its "
             "projected trend are small against the reference's own "
             "standard uncertainty (caldmm.cal.engine.sampling_converged); "
             "near a limit the test tightens.  A point that is still moving "
             "-- self-heating, slow settling -- gets the full --samples.  "
             "Default 3; set it to the --samples count to read every point "
             "in full")
    plan_group.add_argument("--discards", type=int, default=None,
                            help=f"readings thrown away after the range "
                                 f"change (default {DEFAULT_DISCARDS})")
    plan_group.add_argument(
        "--max-voltage", type=float, default=None, metavar="VOLTS",
        help="drop every point whose absolute source voltage is above this, "
             "before the plan is validated and before --accept-max is checked "
             "against it.  A real ceiling, unlike --accept-max, which only "
             "STATES the maximum a plan already sources.  Points of other "
             "quantities are untouched, and what it removed is printed and "
             "recorded in the result file's header.  Allowed alongside --plan: "
             "it narrows a plan rather than generating a different one",
    )
    plan_group.add_argument(
        "--min-spec-ratio", type=float, default=None, metavar="RATIO",
        help="skip points whose plan spec ratio (DUT tolerance / reference "
             "tolerance) is at or below this.  Default: no filtering.  The "
             "census is printed before every run either way; --min-spec-ratio "
             "1 drops exactly the points it counts -- the ones where the "
             "reference is no better than the DUT")

    rule = parser.add_argument_group("the decision rule")
    rule.add_argument("--confidence", type=float, default=None,
                      help=f"confidence level of the decision (default "
                           f"{DEFAULT_CONFIDENCE:g})")
    rule.add_argument("--tur-floor", type=float, default=None, metavar="RATIO",
                      help=f"test uncertainty ratio below which no statement "
                           f"is made (default {DEFAULT_TUR_FLOOR:g})")
    rule.add_argument("--guard-band", type=float, default=None, metavar="G",
                      help=f"guard-band factor g in w = g*U (default "
                           f"{DEFAULT_GUARD_FACTOR:g})")
    rule.add_argument("--temperature-policy", choices=TEMPERATURE_POLICIES,
                      default=None,
                      help="how the ambient enters the decision (default: "
                           "gate).  'flag' also disables the tight window a "
                           "24-hour specification needs -- pass an --ambient "
                           "instead")
    rule.add_argument("--rule", default=None,
                      choices=("simple", "guardbanded", "guardbanded-reject"),
                      help="the decision rule (default: guardbanded, JCGM 106 "
                           "8.3.3 guarded acceptance).  'guardbanded-reject' "
                           "(JCGM 106 8.3.4) also guard-bands the FAIL side: "
                           "at low TUR a marginal failure becomes "
                           "INDETERMINATE instead of an asserted "
                           "non-conformity.  A run-time choice, so it is "
                           "allowed alongside --plan")

    conditions = parser.add_argument_group("conditions")
    conditions.add_argument(
        "--ambient", type=float, default=None, metavar="CELSIUS",
        help="the measured ambient temperature.  "
             "Without it, points whose model declares a temperature "
             "window cannot PASS")
    conditions.add_argument(
        "--lead-resistance", type=float, default=None, metavar="OHMS",
        help="measured round-trip resistance of the HI and LO test leads, in "
             "ohms.  A 2-wire resistance reading includes it; with it stated "
             "the reading is corrected (with a 20 %% uncertainty term), "
             "without it the 2-wire points where up to 0.1 ohm of leads would "
             "matter -- the 10 ohm-range short -- are INDETERMINATE")
    conditions.add_argument(
        "--thermal-emf", type=float, default=None, metavar="VOLTS",
        help="bound on uncompensated thermal EMF in the measurement loop, as "
             "a half-width in volts -- ~1e-6 for clean copper-to-copper "
             "junctions, more for binding posts or dissimilar metals.  The "
             "dominant budget term at a short or zero point; without this "
             "flag that term is absent from every budget")
    conditions.add_argument(
        "--current-post", choices=tuple(p.lower() for p in CURRENT_POSTS),
        default="normal",
        help="which binding post the calibrator sources current from before a "
             "DCI/ACI group: 'normal' is the OUTPUT HI post and the "
             "instrument's own default, 'aux' the AUX CURRENT OUTPUT post.  "
             "It is set and read back at the group prompt, which then names "
             "the post the unit reports -- the setting is non-volatile and "
             "survives *RST, so it is never assumed")
    conditions.add_argument(
        "--ref-tempco-ref-c", type=float, default=None, metavar="CELSIUS",
        help="temperature the REFERENCE instrument's tempco figures are "
             "relative to, when its model file cannot state one.  For a "
             "3458A reference this is its last ACAL temperature (TEMP?); "
             "without it a --dut source run flags every point "
             "tempco-not-evaluated")

    output = parser.add_argument_group("output and resuming")
    output.add_argument("-o", "--csv", default=None, metavar="RUN.csv",
                        help="where to write the result file")
    output.add_argument("--resume", default=None, metavar="RUN.csv",
                        help="continue an interrupted run: append to this "
                             "file, starting at the first group it does not "
                             "already cover")
    output.add_argument("--start-group", type=int, default=None, metavar="N",
                        help="start at wiring group N (0-based), overriding "
                             "what --resume computes")
    output.add_argument("--run-kind",
                        choices=tuple(kind for kind in RUN_KINDS
                                      if kind != "standalone"),
                        default=None,
                        help="place this run in a calibration cycle: "
                             "'as-found' is measured before an adjustment, "
                             "'as-left' after it.  Recorded in the result "
                             "header (default: standalone).  An as-left file "
                             "that also names its --counterpart is compared "
                             "against it automatically by caldmm-cal-analyze")
    output.add_argument("--counterpart", default=None, metavar="RUN.csv",
                        help="the paired run of the cycle -- for an as-left "
                             "run, the as-found CSV measured before the "
                             "adjustment.  Recorded in the header; requires "
                             "--run-kind")
    output.add_argument("--adjust-log", default=None, metavar="RECORD.log",
                        help="the adjustment session record between the "
                             "as-found and as-left runs.  Recorded in the "
                             "header; requires --run-kind.  (Not "
                             "--adjust-record, which names where a --adjust "
                             "run WRITES its record)")
    output.add_argument("--no-report", action="store_true",
                        help="skip the automatic PDF+markdown report at the "
                             "end of the run.  The CSV is the record either "
                             "way; the caldmm-cal-analyze command is printed "
                             "for later")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--dry-run", action="store_true",
                           help="build and validate the plan, print it, and "
                                "touch no instrument")
    behaviour.add_argument("--yes", action="store_true",
                           help="answer every prompt, including the group "
                                "re-cabling stop and the high-voltage "
                                "confirmation.  **Refused when more than one "
                                "wiring group remains**: answering a re-cabling "
                                "prompt does not move the leads, so later "
                                "groups would be measured through the previous "
                                "group's wiring.  The output will reach its "
                                "maximum with nobody there")
    behaviour.add_argument("--accept-max", type=float, default=None,
                           metavar="VOLTS",
                           help="state this plan's maximum output voltage, so "
                                "--yes may run it unattended above the "
                                f"{HIGH_VOLTAGE_V:g} V hazard threshold.  It "
                                "must equal the maximum the plan actually "
                                "sources -- it is the typed acknowledgement "
                                "the interactive confirmation asks for, and a "
                                "number that does not match is a plan the "
                                "operator has not read.  It ACKNOWLEDGES a "
                                "maximum, it does not impose one; --max-voltage "
                                "is the ceiling, and this is then checked "
                                "against the reduced plan")
    behaviour.add_argument("--force", action="store_true",
                           help="start even though the calibrator's dc zeros "
                                "are overdue, or the meter under test is more "
                                "than 5 C from its adjustment temperature "
                                "(CAL? 59/60).  Every affected result will be "
                                "suspect, and flagged")
    behaviour.add_argument(
        "--acal", action="store_true", default=None,
        help="run ACAL ALL on the 3458A before the first point, with the "
             "calibrator in standby, then wait --acal-settle minutes.  The "
             "data sheet's figures hold within 24 h and 1 C of the last "
             "ACAL; this makes the run satisfy that by itself, unattended")
    behaviour.add_argument(
        "--acal-recent", action="store_true", default=None,
        help="state that ACAL ALL was run on the 3458A within the last 24 h "
             "and that its TEMP? is still within 1 C of the ACAL temperature. "
             "Recorded in the result file.  Without this or --acal, every "
             "row is INDETERMINATE: the data sheet states the 3458A's figures "
             "only after a recent ACAL")
    behaviour.add_argument(
        "--acal-settle", type=float, default=None, metavar="MIN",
        help=f"settling time after --acal, in minutes (default "
             f"{ACAL_SETTLE_MINUTES:g}, the Cal Manual's figure for ACAL ALL)")

    adjustment = parser.add_argument_group(
        "adjustment (a different run: it WRITES the DUT's calibration memory)",
        "Runs the DUT's registered adjustment procedure instead of a "
        "verification.  Refused in the same invocation as a verification run: "
        "verifying against the same reference that was just used to adjust is "
        "not an independent check.")
    adjustment.add_argument(
        "--adjust", action="store_true",
        help="adjust the meter against certified external standards you "
             "connect by hand.  DRY RUN BY DEFAULT: everything but the CAL "
             "commands really runs, and each one that would have been sent is "
             "printed.  Not available for every model -- an unimplemented one "
             "says why")
    adjustment.add_argument(
        "--adjust-commit", action="store_true",
        help="actually send the CAL commands, OVERWRITING the meter's "
             "non-volatile calibration memory irreversibly.  Each write still "
             "needs its own typed confirmation.  Without this flag the driver "
             "itself is opened without the write opt-in, so nothing can be "
             "written even by mistake")
    adjustment.add_argument(
        "--adjust-record", default=None, metavar="BASE",
        help="basename for the session record (default: "
             "MODEL-adjust-<timestamp>).  '.log' is appended.  The path is "
             "proved writable before the instrument is opened")
    adjustment.add_argument(
        "--operator", default=None, metavar="NAME",
        help="who performed the adjustment, for the record")
    adjustment.add_argument(
        "--notes", default=None, metavar="TEXT",
        help="free text for the adjustment record")
    adjustment.add_argument(
        "--adjust-groups", default=None, metavar="IDS",
        help="comma-separated wiring groups to adjust (34401A: dcv, ohms, "
             "dci, acv, freq); default all.  The procedure's own order and "
             "prerequisites still apply")
    adjustment.add_argument(
        "--adjust-resume-done", default=None, metavar="STEPS",
        help="step ids an EARLIER session completed (e.g. zero-front,"
             "adjust-dcv), counted as prerequisites only after CAL:COUN? "
             "is proved equal to --adjust-resume")
    adjustment.add_argument(
        "--adjust-resume", type=int, default=None, metavar="COUNT",
        help="the CAL:COUN? the earlier session ended on")
    adjustment.add_argument(
        "--terminals", default=None,
        choices=("front", "rear", "both", "none"),
        help="which terminal sets to zero-adjust (34401A only; default "
             "front).  The front and rear inputs carry SEPARATE offset "
             "constants and the switch that selects them cannot be read "
             "remotely, so you will be asked to move it")
    adjustment.add_argument(
        "--as-found", default=None, metavar="CSV",
        help="the verification results measured BEFORE this adjustment.  "
             "Required by procedures that ask for it: adjusting overwrites "
             "the only measurement of how far the meter drifted, which is "
             "what a calibration interval is justified from")
    adjustment.add_argument(
        "--waive-as-found", action="store_true",
        help="proceed without --as-found.  For a meter already known faulty, "
             "where there is no drift worth characterising")
    return parser


def run(args: argparse.Namespace, *, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None) -> int:
    """Execute one verification run.  Returns a process exit status.

    *sleep* and *clock* are injected for testing, exactly as
    :func:`caldmm.cal.engine.run_plan` takes them: a suite that really slept
    through 200 settling times would not be run.
    """
    ctx = RunContext(args=args, sleep=sleep, clock=clock)
    try:
        if getattr(args, "adjust", False):
            return _phase_adjust(ctx)
        adjust_only_refusals(args)
        linkage_refusals(args)
        _phase_plan(ctx)
        _phase_describe(ctx)
        if not _phase_resume(ctx):
            return EXIT_OK
        _check_unattended(ctx)
        if args.dry_run:
            print()
            print("  dry run: the plan will run, and no instrument was "
                  "opened.")
            return EXIT_OK

        _phase_open(ctx)
        _phase_ambient(ctx)
        if not _phase_confirm(ctx):
            return EXIT_USAGE

        _phase_acal(ctx)
        _phase_execute(ctx)
        return _phase_finish(ctx)
    except VerifyError as exc:
        if ctx.writer is not None:
            # A refusal raised *during* the run -- a current post the
            # calibrator will not confirm -- ends it the way an interrupt
            # does: the rows already measured are intact and the operator
            # gets a command to continue once the cause is fixed.
            return _phase_abandoned(ctx, f"the run was stopped: {exc}")
        print(f"\nerror: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SourceFaultError as exc:
        return _phase_abandoned(
            ctx, f"the calibrator stopped the run: {exc}",
            faults=tuple(getattr(exc.health, "faults", ()) or ()))
    except EngineError as exc:
        return _phase_abandoned(ctx, f"the run was abandoned: {exc}")
    except WriteFailed as exc:
        return _phase_abandoned(ctx, str(exc))
    except (KeyboardInterrupt, SystemExit):
        # setup_graceful_shutdown turns SIGINT into SystemExit *after* running
        # the cleanup, so the source is already in standby by the time we get
        # here; EOF on a prompt arrives as KeyboardInterrupt.
        return _phase_abandoned(ctx, "interrupted.")
    finally:
        _shutdown(ctx)


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

"""Adjustment procedures: ordered sequences that *mutate* calibration memory.

Verification (:mod:`caldmm.cal.engine`) is a generator over independent,
idempotent points.  **Adjustment is none of those things.**  It writes
non-volatile calibration memory, its steps have a mandatory order enforced by
the instrument with a fault, it cannot be restarted in the middle, it needs an
operator between steps, and every detail of it is model-specific.  Forcing that
through ``run_plan`` would be wrong, so this module is a separate and
deliberately small abstraction:

* :class:`AdjustStep` -- one operator-visible step, and the exact commands it
  will send,
* :class:`Procedure` -- ``preflight`` / ``steps`` / ``finalize``,
* :func:`run_procedure` -- the ~60-line runner that prompts, gates and records,
* :class:`ProcedureResult` -- the audit trail of an irreversible operation.

**Procedures are written in Python, one per model, never as a data-driven
DSL.**  They encode safety-critical ordering (and, for meters, secure access
codes).  A YAML dialect for irreversible instrument mutation would buy
configurability at the cost of the one property that matters.

Dry run is the default
----------------------

:attr:`ProcedureContext.commit` defaults to ``False``.  Every step that does
not write NVRAM is then **actually executed** -- the calibrator really does run
``CAL_REF`` and ``CAL_ADJ``, really does compute new constants and really does
print the report showing them -- and every step that *would* commit is skipped
and logged with the exact command it would have sent
(:attr:`ProcedureResult.would_commit`).  This is not a simulation mode bolted
on for testing: guide §14.6 says in as many words that *"omitting ``CAL_STORE``
collects performance data without committing constants"*, so the complete dry
run is a legitimate, useful outcome of a real artifact calibration -- the way
you find out what a calibration *would* do before you let it.

Import hygiene
--------------

Stdlib, :mod:`caldmm.cal.plan` and :mod:`caldmm.cal.calreport`.  **No
driver.**  Procedures are handed a driver *object* and duck-type it, so this
module imports on a machine with no linux-gpib, no pyserial and no pyvisa, and
so a fake instrument needs no patching to be substituted.  The handful of
driver constants a procedure has to know (the CALIBRATION-switch fault codes,
the 22 V long-term-command limit) are **mirrored** below with a comment saying
where the authority lives.
"""

from __future__ import annotations

import math
import re
import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import (Any, Callable, Iterable, Iterator, Mapping, Protocol,
                    Sequence, runtime_checkable)

from .calreport import CalReport, ReportDiff, diff_reports, parse_reports
# The verification engine owns the numbers of Global Constraints §8 and §9 --
# the safe level, the two probe tolerances, the hazard thresholds and the
# gross-error factor.  They are **imported**, not repeated: an adjustment
# procedure that proved its wiring to a different tolerance than the engine
# would be two rules wearing one name.  ``engine`` imports no driver, so the
# import invariant is untouched (and there is no cycle: nothing in the engine
# imports this module).
from .engine import (
    GROSS_ERROR_FACTOR,
    HAZARD_CURRENT_A,
    HAZARD_VOLTAGE_V,
    PRESTEP_FRACTION,
    PROBE_TOLERANCE,
    WIRING_PROOF_FRACTION,
    WIRING_PROOF_MIN_A,
    WIRING_PROOF_MIN_V,
    resolvable_proof_level,
)
from .plan import PlanPoint
from .specs import load_model

__all__ = [
    "AdjustStep", "ExternalStandard", "Fluke5700AArtifactCal",
    "Fluke5700ACalCheck", "Fluke5700ADcZeros", "Procedure",
    "ProcedureContext", "ProcedureError", "ProcedureResult", "SafetyError",
    "SetPoint", "StandardSpec", "StandardValueError", "StepOrderError",
    "StepOutcome", "StepResult", "CalibrationSwitchLockout",
    "NvramState", "PreflightError", "classify_failure",
    "REFUSED_BY_INSTRUMENT", "UNSENT_GUARDS",
    "ARTIFACT_STANDARDS", "CAL_REF_ORDER", "available_procedures",
    "NVRAM_VERIFY_QUERIES", "get_procedure", "interrupt_lines",
    "needs_source", "nvram_verify_hint",
    "parse_certified_value", "parse_temperature",
    "parse_uncertainty", "procedures_for", "prompt_standard",
    "prompt_standards", "register", "run_procedure",
    # Global Constraints §8 and §9, for the sourced adjustment procedures
    "GROSS_ERROR_FACTOR", "GrossErrorAbort", "HAZARD_CURRENT_A",
    "HAZARD_VOLTAGE_V", "PRESTEP_FRACTION", "PROBE_TOLERANCE",
    "WiringProofError", "check_gross_error", "hazardous_output",
    "pre_step", "prestep_needed", "probe_level_refusal", "probe_ok",
    "prove_wiring", "safe_probe_level", "verify_meter_range",
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


# --------------------------------------------------------------------------
# Constants mirrored from the driver
# --------------------------------------------------------------------------
#
# This module must not import ``caldmm.drivers.fluke5700a`` (see the module
# docstring), so the few numbers it has to branch on are repeated here.  The
# driver remains the authority; each one names its source.

#: Fault codes that mean "the rear-panel CALIBRATION switch is in NORMAL".
#: Mirrors ``fluke5700a.CALIBRATION_SWITCH_FAULT_CODES``.  Guide §7.6 / gap
#: G29: **the switch position cannot be queried remotely**, so a fault is the
#: only evidence that ever exists.  850/851 are the 5725A amplifier's own,
#: separate switch.
CALIBRATION_SWITCH_FAULT_CODES = frozenset({838, 839, 845, 850, 851, 855})

#: "Calibration Steps Out Of Order" -- what the *instrument* answers when the
#: §14.6 order is violated.  :class:`StepOrderError` exists so this fault is
#: never reached: the command does not leave the procedure.
FAULT_CAL_STEPS_OUT_OF_ORDER = 852

#: Guide §19.2: refuse to start a long-term command with an output at or above
#: this, because **there is no remote abort** -- ``STBY`` is itself a
#: state-changing command and is rejected with fault 2002 while one runs.
MAX_LIVE_OUTPUT_V = 22.0

#: Guide §11.6 warm-up: twice the off-time, to a maximum of 30 minutes.  The
#: off-duration is not queryable, so a procedure that cannot know it requires
#: the full 30.  Mirrors ``fluke5700a.WARMUP_MAX_MINUTES``.
WARMUP_MINUTES = 30

#: Guide §11.6: specifications require a dc zeros calibration every 30 days;
#: past that the calibrator logs fault 1303.
DC_ZERO_INTERVAL_DAYS = 30

#: Calibration is *specified* only over this band (guide §11.6).  The driver
#: separately refuses anything outside the 0-50 °C operating range.
CAL_TEMP_SPEC_MIN_C = 15.0
CAL_TEMP_SPEC_MAX_C = 35.0


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ProcedureError(Exception):
    """Base class for everything this module refuses to do."""


class StepOrderError(ProcedureError):
    """A step was attempted out of its mandatory order.

    Raised **before the command reaches the wire**.  The instrument would
    answer fault :data:`FAULT_CAL_STEPS_OUT_OF_ORDER`, but by then the
    calibrator has already spent a long-term command's worth of time on a
    sequence that cannot be completed, and the operator has to work out from a
    three-digit code what went wrong.
    """


class SafetyError(ProcedureError):
    """A precondition that exists to protect the operator or the instrument."""


class WiringProofError(ProcedureError):
    """A safe-level check did not read back what was sourced (§8, §9).

    Deliberately **not** a :class:`SafetyError`: by the time a proof can run,
    the meter's function and range have already been sent, and
    :data:`UNSENT_GUARDS` would have the record assert the instrument was left
    alone when it was not.  Nothing was written to calibration memory either
    way -- that is what stopping here buys.
    """


class GrossErrorAbort(ProcedureError):
    """A reading was off by ten tolerances, so the procedure stops (§8).

    A meter out of specification is what an adjustment exists to fix and is
    never this.  Ten times the step's tolerance is a lead on the wrong
    terminal, the wrong function or a sense pair still shorted from the last
    group -- and every later point would be adjusted against the same
    mistake, irreversibly.
    """


class StandardValueError(ProcedureError, ValueError):
    """A certified value or uncertainty that cannot be right.

    Also a :class:`ValueError`, so the ordinary ``except ValueError`` around an
    operator's typing still catches it.
    """


class CalibrationSwitchLockout(ProcedureError):
    """A commit was refused because the physical CALIBRATION switch is NORMAL.

    The whole user experience of this failure is this message, because there is
    no remote query for the switch (guide §7.6, gap G29) -- so it says which
    switch, where it is, and that nothing was stored.
    """

    def __init__(self, what: str, codes: Sequence[int] = (),
                 detail: str = "") -> None:
        self.what = what
        self.codes = tuple(int(c) for c in codes)
        self.detail = detail
        super().__init__(self._render())

    def _render(self) -> str:
        five725 = any(code in (850, 851) for code in self.codes)
        which = ("the 5725A amplifier's OWN rear-panel CALIBRATION switch"
                 if five725 else "the calibrator's rear-panel CALIBRATION "
                 "switch")
        codes = (f" (fault{'s' if len(self.codes) != 1 else ''} "
                 f"{', '.join(str(c) for c in self.codes)})"
                 if self.codes else "")
        detail = f"\n  instrument said: {self.detail}" if self.detail else ""
        return (
            f"{self.what} was REFUSED{codes}: {which} is in the NORMAL "
            f"position.\n"
            f"  NOTHING WAS STORED. The calibration constants computed by "
            f"CAL_ADJ are still in volatile memory only, and will be lost at "
            f"power-off.\n"
            f"  Go to the back of the instrument, set the CALIBRATION switch "
            f"to ENABLE, repeat this step, then set it back to NORMAL.\n"
            f"  Repeat means this step ALONE — the artefacts do not have to be "
            f"re-connected and the sequence does not have to be re-run. The "
            f"constants CAL_ADJ computed are still live, so "
            f"Fluke5700AArtifactCal.store_step(ctx) commits exactly the "
            f"calibration already performed. That holds only until the "
            f"calibrator is powered off.\n"
            f"  There is no remote query for that switch (guide §7.6, gap "
            f"G29), so this fault is the only way it can ever be "
            f"detected.{detail}")


class PreflightError(ProcedureError):
    """One or more blocking problems stopped the procedure before step 1."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(str(p) for p in problems)
        super().__init__("cannot start:\n" + "\n".join(
            f"  - {p}" for p in self.problems))


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SetPoint:
    """What will appear on the binding posts while a step runs.

    Not what the step *commands* -- ``CAL_REF`` drives the output itself, from
    the inside -- but what is physically there, which is what an operator with
    their hands near the terminals needs to know.  :attr:`hazardous` is the
    same 22 V line guide §19.2 draws.
    """

    value: float
    unit: str
    label: str = ""
    #: Non-zero for an ac point.  Carried because a hazard banner that says
    #: "750 V" without saying "at 900 Hz" describes a different risk from the
    #: one at the terminals, and because the record has to distinguish the two
    #: 100 V ac points of a 34401A adjustment, which differ only in frequency.
    freq_hz: float = 0.0

    @property
    def hazardous(self) -> bool:
        return (self.unit.upper().startswith("V")
                and abs(self.value) >= MAX_LIVE_OUTPUT_V)

    def describe(self) -> str:
        if self.label:
            return self.label
        text = f"{self.value:g} {self.unit}"
        if self.freq_hz:
            text += f" at {self.freq_hz:g} Hz"
        return text


class StepOutcome(Enum):
    """What actually happened to a step.  Recorded verbatim in the result."""

    EXECUTED = "executed"
    #: NVRAM step held back because this run is a dry run (the default).
    SKIPPED_DRY_RUN = "skipped-dry-run"
    #: The operator declined it at the prompt.
    SKIPPED_DECLINED = "skipped-declined"
    #: It raised.  An adjustment sequence does not resume, so the run stops.
    FAILED = "failed"

    @property
    def ran(self) -> bool:
        return self is StepOutcome.EXECUTED

    @property
    def held_back(self) -> bool:
        """True when the step did not run *and* nothing was attempted."""
        return self in (StepOutcome.SKIPPED_DRY_RUN,
                        StepOutcome.SKIPPED_DECLINED)


class NvramState(Enum):
    """What is known about non-volatile memory after a step.

    Three-valued on purpose.  A step whose commit command reached the wire and
    whose *outcome* could not be read back -- a GPIB read timeout on the
    ``FAULT?`` drain that follows ``CAL_STORE;*WAI``, say -- has almost
    certainly written the EEPROM, and recording that as "not committed" would
    make the audit trail state the opposite of what happened at the one
    irreversible step in the procedure.
    """

    #: Provably nothing was written: the step never ran, was held back, or
    #: failed in a way that proves the instrument refused it.
    UNTOUCHED = "untouched"
    #: The write completed and the instrument confirmed it.
    WRITTEN = "written"
    #: The command went out and the result could not be read back.
    UNKNOWN = "unknown"


#: Failures raised by guards that run *before* any I/O, by construction.  The
#: command **was never sent**, so it belongs in ``skipped_commands``.
UNSENT_GUARDS = (StepOrderError, SafetyError)

#: Failures where the command *was* sent and the instrument answered by
#: refusing it.  Non-volatile memory is known-untouched -- a queued fault 845
#: is the calibrator saying so -- but the command still belongs in
#: ``commands``: it went on the wire, and filing it as "not sent" would be the
#: same class of mislabelling as calling an unread outcome "not committed".
REFUSED_BY_INSTRUMENT = (CalibrationSwitchLockout,)


def classify_failure(exc: BaseException, commands: Sequence[str], *,
                     writes_nvram: bool
                     ) -> "tuple[NvramState, tuple[str, ...], tuple[str, ...]]":
    """How to record a step that raised: ``(nvram, commands, skipped)``.

    Public because it is not the runner's private business.  Any caller that
    drives a step outside :func:`run_procedure` -- an app offering the
    single-step ``CAL_STORE`` retry, say -- has to answer exactly these two
    questions, and answering them by copying the runner's tuples is how the
    two drift apart.  There is one implementation and this is it.

    Two **independent** questions, and conflating them is what makes an audit
    trail lie:

    *Did the command go out?*  Only a :data:`UNSENT_GUARDS` failure proves it
    did not.  Everything else either sent it or cannot be shown not to have,
    so it is reported as sent -- rendering "[-]" next to a failed
    ``CAL_STORE`` would say the opposite of what happened.

    *Was non-volatile memory written?*  Unknown for a commit that went out and
    was never answered; known-untouched when the instrument itself refused it
    (:data:`REFUSED_BY_INSTRUMENT`), because a queued fault 845 is the
    calibrator saying so.
    """
    sent = tuple(commands)
    guarded = isinstance(exc, UNSENT_GUARDS)
    refused = isinstance(exc, REFUSED_BY_INSTRUMENT)
    nvram = (NvramState.UNKNOWN
             if writes_nvram and not (guarded or refused)
             else NvramState.UNTOUCHED)
    return (nvram, () if guarded else sent, sent if guarded else ())


@dataclass(frozen=True, slots=True)
class StepResult:
    """One step's row of the audit trail.

    Complete enough to reconstruct the session: what was sent, what was
    deliberately *not* sent, what came back, whether NVRAM was touched and what
    the operator decided.
    """

    step_id: str
    outcome: StepOutcome
    started_at: datetime
    finished_at: datetime
    writes_nvram: bool = False
    #: Commands that really went to the instrument, in order.
    commands: tuple[str, ...] = ()
    #: Commands that were **not** sent, and would have been.  The point of a
    #: dry run.
    skipped_commands: tuple[str, ...] = ()
    #: Whatever the step read back, rendered for a human.
    response: str = ""
    #: The operator's decision, in their words where there was one.
    operator: str = ""
    error: str = ""
    #: What is known about non-volatile memory afterwards.  Defaulted from
    #: ``writes_nvram``/``outcome``; the runner overrides it on the failure
    #: path, which is the only place the three-valued answer can be decided.
    nvram: NvramState | None = None
    detail: Mapping[str, Any] = field(default_factory=_empty_map)

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "skipped_commands",
                           tuple(self.skipped_commands))
        object.__setattr__(self, "detail",
                           MappingProxyType(dict(self.detail)))
        if self.nvram is None:
            object.__setattr__(
                self, "nvram",
                NvramState.WRITTEN
                if self.writes_nvram and self.outcome is StepOutcome.EXECUTED
                else NvramState.UNTOUCHED)

    @property
    def committed(self) -> bool:
        """True only when this step really did write non-volatile memory."""
        return self.nvram is NvramState.WRITTEN

    @property
    def nvram_uncertain(self) -> bool:
        """True when a commit went out and its outcome is unknown."""
        return self.nvram is NvramState.UNKNOWN

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def describe(self) -> str:
        mark = {StepOutcome.EXECUTED: "ok",
                StepOutcome.SKIPPED_DRY_RUN: "DRY RUN - not sent",
                StepOutcome.SKIPPED_DECLINED: "declined",
                StepOutcome.FAILED: "FAILED"}[self.outcome]
        if self.nvram_uncertain:
            mark += " - NVRAM STATE UNKNOWN"
        sent = "; ".join(self.commands or self.skipped_commands) or "-"
        text = f"{self.step_id}: {mark} [{sent}]"
        if self.error:
            text += f" -- {self.error}"
        return text


#: The signature of :attr:`AdjustStep.action`.  It is handed the context and
#: its own step so a procedure can stay stateless -- all run state lives on the
#: context, which is also the audit trail.
StepAction = Callable[["ProcedureContext", "AdjustStep"], StepResult]


@dataclass(frozen=True, slots=True)
class AdjustStep:
    """One operator-visible step of an adjustment.

    ``action`` is third rather than fifth only because it has no default and
    the fields after it do.

    ``commands`` is the step's own declaration of the exact text it will send.
    It is what makes a dry run *informative* rather than merely safe: the
    runner can show and record what would have been committed without going
    anywhere near the instrument.  A test pins it against the transport log, so
    it cannot rot into a lie.
    """

    id: str
    prompt: str
    action: StepAction
    setpoint: SetPoint | None = None
    writes_nvram: bool = False
    verify_after: PlanPoint | None = None
    #: Exact commands this step sends, in order, for the dry-run preview.
    commands: tuple[str, ...] = ()
    #: Extra body lines shown under the prompt.
    body: tuple[str, ...] = ()
    #: What :meth:`ConsoleLike.confirm_explicit` asks before an NVRAM write.
    confirm: str = ""
    #: False for bookkeeping steps (captures, ``CAL_TEMP``) that need no
    #: physical action -- the runner shows them instead of blocking on Enter.
    requires_operator: bool = True
    #: **Declining this step stops the run.**  For a step whose whole content
    #: is an acknowledgement the rest of the sequence depends on -- the safety
    #: brief saying there is no remote abort and the front-panel STBY key must
    #: stay within reach -- "no" cannot mean "carry on anyway".
    required: bool = False
    #: Shown, and recorded as a warning, when this step does **not** complete
    #: after its prompt was already given.  A step that asked the operator to
    #: change the instrument's physical state must tell them how to put it
    #: back when the run stops right there.
    aftercare: tuple[str, ...] = ()
    #: Extra line shown, and lifted into the result's warnings, when this step
    #: is held back by a dry run.  For state a *non*-committing part of the
    #: sequence has already left behind in volatile memory.
    dry_run_note: str = ""
    #: The setpoint describes what the steps that FOLLOW will put on the
    #: terminals, not what this step does.  True on a wiring step, whose whole
    #: content is "make this connection": it has to carry the group's setpoint
    #: so the hazard banner is printed *before* the output rather than by the
    #: step that energizes, but "the output will be at 1 kV" would be a lie
    #: told while the calibrator is in standby with hands on the terminals.
    setpoint_upcoming: bool = False
    #: **The instrument documents a way to stop this step once it has started.**
    #: A declaration, not a mechanism: nothing here sends an abort.  It changes
    #: what the Ctrl-C handler tells the operator, because the default message
    #: is written for the Fluke 5700A, whose long-term commands have no remote
    #: abort at all (guide §19.2) and where the front-panel STBY key or the
    #: line switch is genuinely the only stop.
    #:
    #: On an HP 34401A that message is false: Service Guide p.76 documents that
    #: any front-panel key except Shift, a device clear, the LOCAL key, or the
    #: front/rear switch aborts a calibration in progress, and the meter reports
    #: error 705.  An operator told the 5700A's story would sit through a
    #: calibration they could have stopped, or reach for the line switch --
    #: which p.77 says can lose ALL calibration memory during a zero adjustment.
    #:
    #: Deliberately **not** wired to the SIGINT handler.  Sending a device clear
    #: from a signal handler means doing GPIB I/O re-entrantly against a
    #: transport that is mid-read, and the whole benefit would be ending a
    #: 20-second gain point early.  The operator's hand on the front panel is
    #: the documented abort; this flag makes sure they are told so.
    abortable: bool = False
    #: What to say about that stop, when there is more to say than "the front
    #: panel can stop this".  Shown by the interrupt handler.
    abort_hint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", tuple(self.commands))
        object.__setattr__(self, "body", tuple(self.body))
        object.__setattr__(self, "aftercare", tuple(self.aftercare))
        if self.writes_nvram and not self.confirm:
            object.__setattr__(
                self, "confirm",
                f"writing step {self.id!r} to NON-VOLATILE memory "
                f"({', '.join(self.commands) or 'no declared command'})")


@runtime_checkable
class ConsoleLike(Protocol):
    """The operator console, injected.

    Structurally satisfied by :class:`caldmm.lib.console.OperatorConsole`
    except for ``ask`` -- see :func:`ask` for how that is handled.
    """

    def show(self, banner: str, *body: str) -> None: ...

    def prompt(self, banner: str, *body: str) -> None: ...

    def confirm_ok(self, what: str = ..., *, hint: str | None = ...) -> bool: ...

    def confirm_explicit(self, what: str) -> bool: ...


@runtime_checkable
class Procedure(Protocol):
    """An adjustment procedure for one model.

    Implementations are **stateless**: every scrap of run state lives on the
    :class:`ProcedureContext`, which is also the audit trail.  That is what
    lets a single registered instance be shared, and what lets the order guard
    read the record of what actually completed rather than a private flag it
    might forget to clear.

    Optional members, all read through module-level helpers with a default, so
    that adding one never invalidates a procedure written before it:
    ``precheck``, ``prepare`` (see :func:`run_procedure`) and
    ``needs_source`` (see :func:`needs_source`).  They are deliberately **not**
    declared below: this Protocol is ``runtime_checkable``, so every name in
    its body becomes a ``hasattr`` requirement of ``isinstance``, and an
    optional member declared here would stop being optional.
    """

    model_id: str
    procedure_id: str
    name: str

    def preflight(self, ctx: "ProcedureContext") -> list[str]:
        """Every blocking problem.  Empty means ready.  **Read-only I/O.**"""

    def steps(self, ctx: "ProcedureContext") -> Iterable[AdjustStep]:
        """The ordered steps.  May query the instrument; must not change it."""

    def finalize(self, ctx: "ProcedureContext") -> "ProcedureResult":
        """Collect the audit trail, whether the run finished or not."""


#: How to read back, per model, whether non-volatile calibration memory was
#: really written.  Keyed by ``model_id``.
#:
#: This exists because the framework was written for the Fluke 5700A and used
#: to name ``CAL_DATE?`` / ``CAL_RPT?`` unconditionally -- on **every** path,
#: including the two meters that have neither command.  An operator reading a
#: session log after an ``nvram_uncertain`` result was told to send a command
#: their instrument would reject, at the one moment the audit trail is the
#: only thing standing between them and a meter of unknown calibration state.
NVRAM_VERIFY_QUERIES: Mapping[str, str] = MappingProxyType({
    "fluke5700a": "CAL_DATE? / CAL_RPT?",
    "fluke5720a": "CAL_DATE? / CAL_RPT?",
    "hp3458a": "CALNUM? / CALSTR?",
})

#: For a model nobody has taught this yet.  Says what to look for rather than
#: naming a command that might not exist -- a wrong command is worse than an
#: honest "you will have to look this up".
NVRAM_VERIFY_DEFAULT = ("the instrument's own calibration-count and "
                        "calibration-date queries")


def nvram_verify_hint(model_id: str) -> str:
    """What to send to find out whether calibration memory really changed."""
    return NVRAM_VERIFY_QUERIES.get(str(model_id).strip().lower(),
                                    NVRAM_VERIFY_DEFAULT)


def needs_source(procedure: Procedure) -> bool:
    """Does this procedure step its applied values with a programmable source?

    False for every procedure that does not say otherwise, which is the right
    default and the historical one: the three Fluke 5700A procedures source
    from inside the calibrator's own ``CAL_REF``, and the HP 3458A's takes its
    values from certified artefacts connected by hand.

    Callers should ask **before opening any instrument**.  An adjustment run
    started without the calibrator it needs should fail as an argument
    mistake, not two prompts and a security code later.
    """
    return bool(getattr(procedure, "needs_source", False))


def source_policy(procedure: Procedure) -> str:
    """``"required"``, ``"optional"`` or ``"none"`` — the source stance.

    Three answers because the meters genuinely differ three ways: the 34401A
    steps a calibrator through its points (*required*), the 5700A's own
    procedures source internally (*none*) — and the 3458A takes certified
    artefacts by hand **or** a calibrator standing in for them, per
    ``accepts_source`` (*optional*).  Collapsing optional into either
    neighbour re-creates the refusal bug this function replaces: a named
    source rejected as "misleading", or a hand-artefact run rejected for
    not naming one.
    """
    if needs_source(procedure):
        return "required"
    if bool(getattr(procedure, "accepts_source", False)):
        return "optional"
    return "none"


@dataclass
class ProcedureContext:
    """Everything a procedure is handed, and everything it accumulates.

    Mutable on purpose.  A frozen context would push the run record into
    globals or into the procedure, and the whole point of the record is that
    it survives a run that stopped half way.
    """

    #: The driver object.  Duck-typed -- this module never imports one.
    device: Any
    console: Any
    #: The programmable source stepping the applied values, when the procedure
    #: needs one.  ``None`` for every procedure written before the HP 34401A's:
    #: the three Fluke 5700A procedures drive the calibrator's own output from
    #: inside ``CAL_REF``, and the HP 3458A's takes its values from artefacts
    #: the operator connects by hand.  A meter whose adjustment is stepped by a
    #: calibrator is the first thing here that needs a second instrument on the
    #: bus.  Duck-typed against :class:`caldmm.cal.roles.SourceRole`; this
    #: module imports no driver and no role.
    source: Any = None
    #: **Opt in to writing NVRAM.**  False (the default) is a dry run.
    commit: bool = False
    ambient_c: float | None = None
    standards: dict[str, "ExternalStandard"] = field(default_factory=dict)
    operator: str = ""
    notes: str = ""
    #: §10 range identifiers to capture ``CAL_SHIFT?`` for, on top of the
    #: whole-instrument ``CAL_SLST?``.  Empty by default: ``CAL_SLST?`` already
    #: carries every range, and an unknown identifier is a hard driver error.
    shift_ranges: tuple[str, ...] = ()
    #: Optional post-step check, called with :attr:`AdjustStep.verify_after`.
    verifier: Callable[[PlanPoint], Any] | None = None

    # -- run state -------------------------------------------------------
    results: list[StepResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    captures: dict[str, Any] = field(default_factory=dict)
    unreached: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: Set by SIGINT.  Checked between steps; never raised from inside a
    #: long-term command, which cannot be aborted remotely.
    interrupted: bool = False
    error: str = ""
    #: The step the runner is currently inside, so the SIGINT handler can tell
    #: the operator the truth about whether the instrument can be stopped.
    #: Written by :func:`run_procedure` only.
    current_step: "AdjustStep | None" = None

    @property
    def dry_run(self) -> bool:
        return not self.commit

    def record(self, result: StepResult) -> StepResult:
        self.results.append(result)
        self.log.append(result.describe())
        return result

    def result_for(self, step_id: str) -> StepResult | None:
        for result in reversed(self.results):
            if result.step_id == step_id:
                return result
        return None

    def completed(self, step_id: str) -> bool:
        result = self.result_for(step_id)
        return bool(result is not None and result.outcome.ran)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        self.log.append(f"warning: {message}")

    def require_source(self) -> Any:
        """The source, or a clear refusal naming what should have supplied it.

        A procedure that declares ``needs_source`` and is handed a context
        without one has been mis-wired by its caller, not by the operator, so
        the message names the caller's job rather than asking for an action.
        """
        if self.source is None:
            raise ProcedureError(
                "this procedure steps its applied values with a programmable "
                "source, and ProcedureContext.source is None. The runner is "
                "supposed to open one and attach it — see "
                "Procedure.needs_source")
        return self.source

    def standard(self, key: str) -> "ExternalStandard":
        try:
            return self.standards[key]
        except KeyError:
            raise ProcedureError(
                f"no external standard registered for {key!r}; call the "
                f"procedure's prepare() or fill ProcedureContext.standards "
                f"before running") from None


@dataclass(frozen=True, slots=True)
class ProcedureResult:
    """The audit trail for an irreversible operation.

    Deliberately fat.  A calibration that has already happened cannot be
    re-observed, so anything not recorded here is gone.
    """

    model_id: str
    procedure_id: str
    name: str
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    steps: tuple[StepResult, ...] = ()
    standards: tuple["ExternalStandard", ...] = ()
    ambient_c: float | None = None
    serial: str = ""
    operator: str = ""
    notes: str = ""
    warnings: tuple[str, ...] = ()
    #: Step ids never reached, because the run stopped.
    unreached: tuple[str, ...] = ()
    before_report_text: str = ""
    after_report_text: str = ""
    before_report: CalReport | None = None
    after_report: CalReport | None = None
    report_diff: ReportDiff | None = None
    interrupted: bool = False
    error: str = ""
    detail: Mapping[str, Any] = field(default_factory=_empty_map)

    def __post_init__(self) -> None:
        for name in ("steps", "standards", "warnings", "unreached"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "detail",
                           MappingProxyType(dict(self.detail)))

    @property
    def committed(self) -> bool:
        """True when something was **confirmed** written to non-volatile
        memory.

        False does *not* mean "nothing was written" -- see
        :attr:`nvram_uncertain`, which is the case where the commit went out
        and the answer never came back.  Test both.
        """
        return any(step.committed for step in self.steps)

    @property
    def nvram_uncertain(self) -> bool:
        """True when a commit was sent and its outcome could not be read back.

        The instrument must be treated as **possibly adjusted**: confirm with
        the model's own readback queries -- see :func:`nvram_verify_hint` --
        before trusting it either way.
        """
        return any(step.nvram_uncertain for step in self.steps)

    @property
    def nvram_state(self) -> NvramState:
        """The run's overall answer to "was anything written?"."""
        if self.nvram_uncertain:
            return NvramState.UNKNOWN
        return NvramState.WRITTEN if self.committed else NvramState.UNTOUCHED

    @property
    def would_commit(self) -> tuple[str, ...]:
        """Every **commit** command that was held back, in order.

        Both reasons count -- the dry run that is the default, and an operator
        who declined the typed confirmation.  A command that did not go out did
        not go out, and a caller asking "what would this have written?" needs
        the same answer either way.

        Two exclusions, both so the name stays true.  Only ``writes_nvram``
        steps: a declined ``CAL_REF`` was held back too, but printing it under
        "would have sent" would read as though sourcing a reference were a
        commit -- it is in that step's own ``skipped_commands``.  And nothing
        from a *failed* step: those may well have reached the wire, and belong
        to :attr:`nvram_uncertain`.
        """
        return tuple(cmd for step in self.steps
                     if step.outcome.held_back and step.writes_nvram
                     for cmd in step.skipped_commands)

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(cmd for step in self.steps for cmd in step.commands)

    @property
    def dry_run_notes(self) -> tuple[str, ...]:
        """State a dry run left behind that the operator has to know about."""
        notes = []
        for step in self.steps:
            note = step.detail.get("dry_run_note")
            if note and note not in notes:
                notes.append(str(note))
        return tuple(notes)

    @property
    def failed(self) -> bool:
        """A step raised, or the run stopped on a refusal.

        **Not the success test.**  A run in which the operator declined the
        commit has ``failed is False`` and stored nothing.  Branch on
        :attr:`complete` (did the whole sequence run?) and :attr:`committed` /
        :attr:`nvram_uncertain` (what happened to non-volatile memory?).
        """
        return bool(self.error) or any(
            step.outcome is StepOutcome.FAILED for step in self.steps)

    @property
    def complete(self) -> bool:
        """Every step ran, nothing was declined, skipped or missed."""
        return (not self.failed and not self.unreached and not self.interrupted
                and all(step.outcome.ran for step in self.steps))

    def summary(self) -> str:
        head = (f"{self.name} on {self.model_id}"
                f"{' S/N ' + self.serial if self.serial else ''} -- "
                f"{'DRY RUN' if self.dry_run else 'COMMITTING RUN'}")
        lines = [head, f"started {self.started_at.isoformat(' ')}"]
        for standard in self.standards:
            lines.append(f"  standard: {standard.describe()}")
        if self.ambient_c is not None:
            lines.append(f"  ambient: {self.ambient_c:g} °C")
        lines.append("  steps:")
        lines.extend(f"    {step.describe()}" for step in self.steps)
        if self.would_commit:
            lines.append("  NOT committed — would have sent:")
            lines.extend(f"    {cmd}" for cmd in self.would_commit)
        # :attr:`dry_run_notes` are not printed here: the runner also files
        # each one as a warning, and they appear in that block below.
        if self.unreached:
            lines.append("  never reached: " + ", ".join(self.unreached))
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        if self.nvram_uncertain:
            lines.append(
                f"  committed to NVRAM: UNKNOWN — the commit command was "
                f"sent and the outcome could not be read back. Confirm with "
                f"{nvram_verify_hint(self.model_id)} before trusting the "
                f"instrument either way.")
        else:
            lines.append(
                f"  committed to NVRAM: {'yes' if self.committed else 'no'}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# External standards
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StandardSpec:
    """What a procedure needs an external standard to be.

    ``max_deviation_ppm`` is the gross-error gate: a 10 V standard entered as
    ``10000`` and a 1 Ω entered as ``1k`` are both decade errors and both land
    six orders of magnitude outside it.  It is not a tolerance on the
    standard's *quality* -- 1000 ppm is far looser than any usable artefact --
    only a refusal to send an obviously wrong number to a command that
    rewrites the calibrator's idea of a volt.
    """

    key: str
    name: str
    nominal: float
    #: ``CAL_REF`` unit token: ``V``, ``OHM`` or ``KOHM``.
    unit: str
    recommended: str = ""
    recommended_uncertainty_ppm: float = 0.0
    max_deviation_ppm: float = 1000.0
    max_uncertainty_ppm: float = 100.0

    @property
    def base_unit(self) -> str:
        return "OHM" if self.unit.upper().endswith("OHM") else "V"

    def describe(self) -> str:
        text = f"{self.name} (nominal {self.nominal:g} {self.unit})"
        if self.recommended:
            text += f", e.g. {self.recommended}"
            if self.recommended_uncertainty_ppm:
                text += f" at ±{self.recommended_uncertainty_ppm:g} ppm"
        return text


#: The three artefacts guide §14.6 / [MS9] require for a 5700A artifact
#: calibration.  Note there is no separate −10 V entry: the second ``CAL_REF``
#: step uses the *same* 10 V standard with the leads reversed, so its true
#: value is the negation of the certified one.  A separately certified negative
#: value has no meaning for a 732A/732B.
ARTIFACT_STANDARDS: tuple[StandardSpec, ...] = (
    StandardSpec("10V", "10 V dc reference", 10.0, "V",
                 "Fluke 732A/732B", 1.5),
    StandardSpec("10KOHM", "10 kΩ resistance standard", 10.0, "KOHM",
                 "Fluke 742A-10k", 4.0),
    StandardSpec("1OHM", "1 Ω resistance standard", 1.0, "OHM",
                 "Fluke 742A-1", 10.0),
)

STANDARDS_BY_KEY: Mapping[str, StandardSpec] = MappingProxyType(
    {spec.key: spec for spec in ARTIFACT_STANDARDS})


@dataclass(frozen=True, slots=True)
class ExternalStandard:
    """A certified artefact: what it really is, and how well that is known.

    The uncertainty is **not** metadata.  Everything the calibrator sources
    afterwards inherits it, so it belongs in the record of the calibration and
    in the uncertainty budget of every measurement made against the calibrator
    until the next one.  Constructing one validates it, so a fixture cannot
    build a standard the operator interface would have refused.
    """

    spec: StandardSpec
    #: Certified true value, in :attr:`StandardSpec.unit`.
    certified: float
    #: **Expanded** uncertainty at :attr:`coverage_k`, same unit, absolute.
    uncertainty: float
    coverage_k: float = 2.0
    serial: str = ""
    certificate: str = ""
    certified_on: str = ""

    def __post_init__(self) -> None:
        spec = self.spec
        value = float(self.certified)
        if not math.isfinite(value) or value == 0.0:
            raise StandardValueError(
                f"{spec.name}: certified value {self.certified!r} is not a "
                f"usable number")
        if (value > 0) != (spec.nominal > 0):
            raise StandardValueError(
                f"{spec.name}: certified value {value:g} {spec.unit} has the "
                f"wrong sign for a {spec.nominal:g} {spec.unit} standard. The "
                f"−10 V step reverses the *leads*, not the certificate.")
        deviation_ppm = (value - spec.nominal) / abs(spec.nominal) * 1e6
        if abs(deviation_ppm) > spec.max_deviation_ppm:
            raise StandardValueError(
                f"{spec.name}: certified value {value:g} {spec.unit} is "
                f"{deviation_ppm:+.0f} ppm from the {spec.nominal:g} "
                f"{spec.unit} nominal, past the {spec.max_deviation_ppm:g} "
                f"ppm sanity limit. That is a decade or units mistake, not a "
                f"standard — check the certificate and the unit "
                f"({spec.unit}). Refusing to send it to CAL_REF: it would be "
                f"written into the calibrator's constants.")
        u = float(self.uncertainty)
        if not math.isfinite(u) or u <= 0.0:
            raise StandardValueError(
                f"{spec.name}: an uncertainty is required and must be "
                f"positive, got {self.uncertainty!r}. It propagates into "
                f"everything the calibrator sources afterwards, so there is "
                f"no default for it.")
        u_ppm = u / abs(value) * 1e6
        if u_ppm > spec.max_uncertainty_ppm:
            hint = (f" ({spec.recommended} is specified at "
                    f"±{spec.recommended_uncertainty_ppm:g} ppm)"
                    if spec.recommended else "")
            raise StandardValueError(
                f"{spec.name}: uncertainty {u:g} {spec.unit} is {u_ppm:.0f} "
                f"ppm, past the {spec.max_uncertainty_ppm:g} ppm sanity "
                f"limit{hint}. A standard that poor cannot calibrate a "
                f"5700A — most likely the units were mistaken; enter e.g. "
                f"'1.5 ppm' or '15 uV'.")
        k = float(self.coverage_k)
        if not math.isfinite(k) or not 1.0 <= k <= 4.0:
            raise StandardValueError(
                f"{spec.name}: coverage factor {self.coverage_k!r} is not in "
                f"the plausible 1-4 range")
        object.__setattr__(self, "certified", value)
        object.__setattr__(self, "uncertainty", u)
        object.__setattr__(self, "coverage_k", k)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def unit(self) -> str:
        return self.spec.unit

    @property
    def nominal(self) -> float:
        return self.spec.nominal

    @property
    def deviation_ppm(self) -> float:
        return (self.certified - self.nominal) / abs(self.nominal) * 1e6

    @property
    def uncertainty_ppm(self) -> float:
        """Expanded uncertainty, relative, in ppm of the certified value."""
        return self.uncertainty / abs(self.certified) * 1e6

    @property
    def standard_uncertainty(self) -> float:
        """``u = U / k`` -- what a GUM budget actually combines."""
        return self.uncertainty / self.coverage_k

    @property
    def standard_uncertainty_ppm(self) -> float:
        return self.uncertainty_ppm / self.coverage_k

    def describe(self) -> str:
        text = (f"{self.name}: {self.certified:.9g} {self.unit} "
                f"({self.deviation_ppm:+.1f} ppm from nominal) "
                f"± {self.uncertainty:.3g} {self.unit} "
                f"({self.uncertainty_ppm:.2f} ppm, k={self.coverage_k:g})")
        for label, value in (("s/n", self.serial),
                             ("cert", self.certificate),
                             ("dated", self.certified_on)):
            if value:
                text += f", {label} {value}"
        return text


# -- operator entry --------------------------------------------------------

#: SI prefixes accepted on a typed value.  **Case matters**: ``m`` is milli and
#: ``M`` is mega, exactly as everywhere else in metrology.  ``K`` is allowed
#: for kilo because the SCPI token is ``KOHM``.
_PREFIXES: Mapping[str, float] = MappingProxyType({
    "p": 1e-12, "n": 1e-9, "N": 1e-9, "u": 1e-6, "U": 1e-6, "µ": 1e-6,
    "μ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6,
})

#: Looked up **upper-cased**, so only upper-case keys are reachable.
_UNIT_ALIASES: Mapping[str, str] = MappingProxyType({
    "V": "V", "VOLT": "V", "VOLTS": "V", "VDC": "V",
    "OHM": "OHM", "OHMS": "OHM", "R": "OHM", "Ω": "OHM",
})

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _split_number(text: str) -> tuple[float, str]:
    """``"9999.87 mohm"`` -> ``(9999.87, "mohm")``.  Raises on no number."""
    stripped = text.strip().replace(",", "")
    match = _NUMBER.match(stripped)
    if match is None:
        raise StandardValueError(
            f"{text!r} does not start with a number")
    return float(match.group()), stripped[match.end():].strip()


def _unit_scale(token: str, base_unit: str) -> float:
    """Scale factor from *token* to *base_unit*.  Raises when it does not fit.

    ``mV`` -> 1e-3 against ``V``; ``KOHM`` -> 1e3 against ``OHM``; ``mA``
    raises, because a current entered for a resistance standard is exactly the
    class of mistake worth stopping.
    """
    raw = token.strip()
    if not raw:
        raise StandardValueError("no unit given")
    for prefix, scale in (("", 1.0), *sorted(_PREFIXES.items())):
        if prefix and not raw.startswith(prefix):
            continue
        rest = raw[len(prefix):]
        canon = _UNIT_ALIASES.get(rest.upper())
        if canon == base_unit:
            return scale
    raise StandardValueError(
        f"unit {token!r} is not a {base_unit} unit; expected e.g. "
        f"{'V, mV, uV' if base_unit == 'V' else 'ohm, mohm, uohm, kohm'}")


def parse_certified_value(text: str, spec: StandardSpec) -> float:
    """Parse an operator's certified value into :attr:`StandardSpec.unit`.

    A bare number is taken to be in the standard's own unit -- which is how
    every calibration certificate is written -- and a unit may be given to say
    otherwise (``9999.87 ohm`` for the 10 kΩ standard).  The result is *not*
    range-checked here; :class:`ExternalStandard` does that, so the check
    cannot be bypassed by constructing one directly.
    """
    value, unit = _split_number(text)
    if not unit:
        return value
    scale = _unit_scale(unit, spec.base_unit)
    own = _unit_scale(spec.unit, spec.base_unit)
    return value * scale / own


def parse_uncertainty(text: str, spec: StandardSpec,
                      certified: float) -> float:
    """Parse an operator's uncertainty into an absolute value in the unit.

    **A unit is mandatory.**  ``1.5`` on a 10 V standard could mean 1.5 ppm
    (a 732B) or 1.5 V (nonsense) and there is no safe default, so a bare number
    is refused with an explanation rather than guessed at.  ``ppm``, ``ppb``
    and ``%`` are relative to *certified*; anything else is an absolute value
    in a compatible unit.
    """
    value, unit = _split_number(text)
    token = unit.upper()
    if not token:
        raise StandardValueError(
            f"uncertainty {text!r} has no unit. Give one — '1.5 ppm', "
            f"'15 uV', '0.0004 ohm' — because a bare number is ambiguous "
            f"(1.5 ppm and 1.5 {spec.unit} differ by six orders of "
            f"magnitude) and this number is written into the calibration.")
    if token in ("PPM", "PPM,", "PPM."):
        return abs(certified) * value * 1e-6
    if token == "PPB":
        return abs(certified) * value * 1e-9
    if token in ("%", "PCT", "PERCENT"):
        return abs(certified) * value * 1e-2
    scale = _unit_scale(unit, spec.base_unit)
    own = _unit_scale(spec.unit, spec.base_unit)
    return value * scale / own


def parse_temperature(text: str) -> float:
    """Parse an ambient temperature, enforcing the specified 15-35 °C band."""
    value, unit = _split_number(text)
    token = unit.upper().lstrip("°").strip()
    if token not in ("", "C", "CELSIUS", "DEGC"):
        raise StandardValueError(
            f"ambient temperature {text!r}: give °C, not {unit!r}")
    if not CAL_TEMP_SPEC_MIN_C <= value <= CAL_TEMP_SPEC_MAX_C:
        raise StandardValueError(
            f"ambient {value:g} °C is outside the "
            f"{CAL_TEMP_SPEC_MIN_C:g}-{CAL_TEMP_SPEC_MAX_C:g} °C band "
            f"calibration is specified over (guide §11.6). Calibrating "
            f"outside it produces constants whose accuracy is not covered by "
            f"any specification.")
    return value


def ask(console: Any, question: str) -> str:
    """One line of free text from the operator, through the injected console.

    Goes through ``console.ask`` and nowhere else -- there is deliberately no
    :func:`input` fallback.  A fallback would be a second, untested I/O path
    that the injected seam cannot reach, in a module whose whole point is that
    an irreversible sequence is driven through objects a test can substitute.
    A console without ``ask`` is a programming error, and says so.
    """
    asker = getattr(console, "ask", None)
    if not callable(asker):
        raise ProcedureError(
            f"the injected console ({type(console).__name__}) has no ask() "
            f"method; free-text entry is needed for the standards' certified "
            f"values and uncertainties. Use caldmm.lib.console."
            f"OperatorConsole, or supply ask(question) -> str.")
    return str(asker(question))


def prompt_standard(console: Any, spec: StandardSpec, *,
                    attempts: int = 5) -> ExternalStandard:
    """Ask the operator for one standard's certified value and uncertainty.

    Loops on a rejected entry rather than aborting: a mistyped certificate is
    the expected case, and the operator is standing at the instrument with the
    certificate in their hand.
    """
    console.show(
        f"── external standard: {spec.name} ──",
        spec.describe(),
        "Enter the values from this artefact's own calibration certificate.",
        "The uncertainty is not optional: it propagates into everything the",
        "calibrator sources until the next calibration.")
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            certified = parse_certified_value(
                ask(console,
                    f"certified value of the {spec.name} "
                    f"[{spec.unit}, e.g. {spec.nominal:g}]: "),
                spec)
            uncertainty = parse_uncertainty(
                ask(console,
                    "expanded uncertainty (k=2), WITH a unit "
                    "[e.g. '1.5 ppm' or '15 uV']: "),
                spec, certified)
            serial = ask(console,
                         "standard serial / certificate ref (optional): ")
            return ExternalStandard(spec=spec, certified=certified,
                                    uncertainty=uncertainty,
                                    serial=serial.strip())
        except StandardValueError as exc:
            last = exc
            console.show("── rejected ──", str(exc), "Try again.")
    raise StandardValueError(
        f"{spec.name}: no acceptable entry after {attempts} attempts"
    ) from last


def prompt_standards(console: Any,
                     specs: Sequence[StandardSpec] = ARTIFACT_STANDARDS,
                     existing: Mapping[str, ExternalStandard] | None = None,
                     ) -> dict[str, ExternalStandard]:
    """Ask for every standard not already present in *existing*."""
    collected = dict(existing or {})
    for spec in specs:
        if spec.key not in collected:
            collected[spec.key] = prompt_standard(console, spec)
    return collected


# --------------------------------------------------------------------------
# Shared instrument helpers (duck-typed -- no driver import)
# --------------------------------------------------------------------------

def _now() -> datetime:
    """Timezone-aware **local** wall clock, as every timestamp in this project
    is (see :func:`caldmm.cal.engine._now`).

    Not UTC.  This stamps ``started_at``/``finished_at`` on a
    :class:`ProcedureResult`, and the apps that render that record stamp their
    own "written" line with ``datetime.now().astimezone()``.  With a UTC clock
    here the permanent account of an irreversible adjustment read

        written 12:57:11-07:00 ... started 19:57:11+00:00

    -- the same instant, but printed so that the run appears to have started
    seven hours after it was written up.  Both stamps carry an offset and are
    therefore unambiguous to a machine; the reader they have to convince is a
    human auditor.
    """
    return datetime.now().astimezone()


def fault_codes_of(exc: BaseException) -> tuple[int, ...]:
    """Every fault code carried by a driver exception.

    Prefers the structured ``codes`` attribute the 5700A driver's
    ``CalibratorFault`` carries.  Falls back to scanning the rendered message
    for three- and four-digit numbers, so a driver that raises a plain error
    still gets the CALIBRATION-switch diagnosis rather than a bare traceback.
    """
    codes = getattr(exc, "codes", None)
    found: list[int] = []
    if isinstance(codes, (list, tuple)):
        for entry in codes:
            if isinstance(entry, (list, tuple)) and entry:
                entry = entry[0]
            try:
                found.append(int(entry))
            except (TypeError, ValueError):
                continue
    if found:
        return tuple(found)
    return tuple(int(m) for m in re.findall(r"\b(\d{3,4})\b", str(exc)))


def _translate(exc: BaseException, *, what: str) -> BaseException:
    """Map a driver exception onto this module's vocabulary where it helps."""
    codes = fault_codes_of(exc)
    switch = [c for c in codes if c in CALIBRATION_SWITCH_FAULT_CODES]
    if switch:
        return CalibrationSwitchLockout(what, switch, detail=str(exc))
    if FAULT_CAL_STEPS_OUT_OF_ORDER in codes:
        return StepOrderError(
            f"{what} was refused with fault "
            f"{FAULT_CAL_STEPS_OUT_OF_ORDER} (Calibration Steps Out Of "
            f"Order). The instrument disagrees with the procedure about where "
            f"in the §14.6 sequence it is — restart the whole artifact "
            f"calibration from CAL_TEMP; a partial sequence cannot be "
            f"resumed. Original: {exc}")
    return exc


def check_output_safe(device: Any, *, what: str) -> float:
    """Refuse to start a long-term command with a dangerous output set.

    Guide §19.2: there is **no remote abort** for ``CAL_REF``/``CAL_ADJ``/
    ``CAL_ZERO``/``CAL_CHK``; ``STBY`` during one is rejected with fault 2002.
    So the only stop is the front-panel key, and 1100 V standing on the posts
    for the length of a ``CAL_ADJ`` is not a risk to accept for convenience.

    A calibrator that cannot answer ``OUT?`` is *also* refused: "I could not
    check" must never read as "it is safe".
    """
    try:
        amplitude, unit, _freq = device.out_actual()
    except Exception as exc:
        raise SafetyError(
            f"refusing to start {what}: the output could not be read back "
            f"(OUT? failed: {exc}). There is no remote abort for a long-term "
            f"command (guide §19.2), so an unverifiable output state is not "
            f"an acceptable one to start from.") from exc
    if str(unit).upper().startswith("V") and abs(amplitude) >= MAX_LIVE_OUTPUT_V:
        raise SafetyError(
            f"refusing to start {what}: the output is set to "
            f"{amplitude:g} {unit}, at or above the {MAX_LIVE_OUTPUT_V:g} V "
            f"limit of guide §19.2. There is no remote abort for a long-term "
            f"command and STBY is rejected with fault 2002 while one runs, so "
            f"that voltage would stand until the command finished. Set the "
            f"output to 0 V and press STBY first.")
    return float(amplitude)


# --------------------------------------------------------------------------
# Global Constraints §8 (wiring proof, gross-error abort) and §9 (ranging)
# --------------------------------------------------------------------------
#
# The verification engine does all of this for a *measurement* run.  An
# adjustment run is the one that cannot be repaired afterwards -- CAL:VAL
# believes whatever it is told, and a constant written against a value that
# was never on the terminals is a meter that now reads wrong and says it is
# calibrated -- so the same rules apply, from the same constants.
#
# These are functions rather than a class because every procedure module has
# its own point table and its own way of configuring its meter; what they
# share is the arithmetic and the wording, and both belong in one place.

def safe_probe_level(unit: str, value: float, *,
                     full_scale: float | None = None,
                     meter_model: str | None = None,
                     quantity: Any = None,
                     frequency_hz: float = 0.0,
                     mode_id: str | None = None) -> float | None:
    """§8's safe level for a point: what to source *before* the real value.

    ``max(10 mV, 0.1 % of full scale)`` for volts and ``max(100 uA, 0.1 % of
    full scale)`` for amps -- low enough to be harmless into any mis-wiring,
    high enough to be unmistakable on any range of these meters -- **and never
    below what the meter can read to +/-10 %**, which is the controller's
    amendment of §8 of 2026-08-22.  For resistance the standard itself: an
    ohms output sources no hazard, and reading the standard *is* the proof.

    The amendment is applied by the verification engine's own
    :func:`~caldmm.cal.engine.resolvable_proof_level`, imported rather than
    repeated: a proof that meant one level in a verification and another in
    the adjustment that follows it would be two rules wearing one name.  Name
    the meter with *meter_model* to get it; without one the level is the §8
    figure alone, which is what a caller that does not know its meter can
    honestly compute.

    ``None`` where nothing can be proved -- a zero or a short, which reads
    near zero whether the leads are on the terminals or in the air, or a level
    the meter cannot read safely (:func:`probe_level_refusal` is what says
    which, and the caller reports the second).
    """
    level, _ = _probe_level_and_refusal(
        unit, value, full_scale=full_scale, meter_model=meter_model,
        quantity=quantity, frequency_hz=frequency_hz, mode_id=mode_id)
    return level


def probe_level_refusal(unit: str, value: float, *,
                        full_scale: float | None = None,
                        meter_model: str | None = None,
                        quantity: Any = None,
                        frequency_hz: float = 0.0,
                        mode_id: str | None = None) -> str:
    """Why §8's proof cannot be taken here, or ``""``.

    Empty for a level that can be proved **and** for one with nothing to prove
    (a zero or a short): only a level the meter cannot resolve gets words,
    because only that one has to be reported to the operator.
    """
    _, refusal = _probe_level_and_refusal(
        unit, value, full_scale=full_scale, meter_model=meter_model,
        quantity=quantity, frequency_hz=frequency_hz, mode_id=mode_id)
    return refusal


def _probe_level_and_refusal(unit: str, value: float, *,
                             full_scale: float | None,
                             meter_model: str | None,
                             quantity: Any,
                             frequency_hz: float,
                             mode_id: str | None
                             ) -> "tuple[float | None, str]":
    """The level and the refusal, computed once.  See :func:`safe_probe_level`."""
    token = str(unit).strip().upper()
    magnitude = abs(float(value))
    if token.startswith("OHM"):
        return (magnitude or None), ""
    fs = abs(float(full_scale)) if full_scale else magnitude
    if not fs:
        return None, ""
    if token == "V":
        base = max(WIRING_PROOF_MIN_V, WIRING_PROOF_FRACTION * fs)
    elif token == "A":
        base = max(WIRING_PROOF_MIN_A, WIRING_PROOF_FRACTION * fs)
    else:
        return None, ""
    if meter_model is None:
        return base, ""
    return resolvable_proof_level(
        base, token, meter_spec=load_model(meter_model), quantity=quantity,
        frequency_hz=float(frequency_hz or 0.0), mode_id=mode_id)


def probe_ok(reading: float, sourced: float,
             tolerance: float = PROBE_TOLERANCE) -> bool:
    """Whether a probe reading is what the calibrator says it is sourcing.

    Deliberately loose (:data:`PROBE_TOLERANCE`, 10 %): this is a wiring
    check, not a measurement, and it has to pass on the instrument that is
    about to be adjusted *because* it is out of specification.
    """
    try:
        reading = float(reading)
        sourced = float(sourced)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(reading) or not math.isfinite(sourced) or not sourced:
        return False
    return abs(reading - sourced) <= tolerance * abs(sourced)


def hazardous_output(unit: str, value: float,
                     full_scale: float | None = None) -> bool:
    """Whether this point (or the range it sits on) is above §9's thresholds.

    The range counts as well as the point: a reading taken on a range whose
    full scale is 1000 V is one keystroke away from 1000 V.
    """
    token = str(unit).strip().upper()
    if token not in ("V", "A"):
        return False
    threshold = HAZARD_VOLTAGE_V if token == "V" else HAZARD_CURRENT_A
    if abs(float(value)) > threshold:
        return True
    return bool(full_scale) and abs(float(full_scale)) > threshold


def prestep_needed(unit: str, value: float, *,
                   supports_manual_range: bool = True,
                   range_verified: bool = False) -> bool:
    """Whether §9's 10 %-of-target pre-step applies.

    **Always** above 220 mA -- manual ranging does not waive it, because what
    it catches there is a wrong shunt or a wrong terminal, not a wrong range,
    and at those currents that is a fire rather than a bad reading.  Above
    22 V whenever the meter's range was not **verified**: either it cannot be
    fixed at all, or it can be fixed but not read back.  The 1281 is the
    second case -- ``DCV <range>`` cancels autorange (UM p.5-30) and then
    nothing in its command set reports the range -- and it used to fall
    between the two rules and get neither guard at 1000 V.

    *range_verified* defaults to **False** on purpose: a caller that has not
    said its read-back agreed has not had one, and the safe reading of silence
    here is one extra decade of rehearsal, not none.
    """
    token = str(unit).strip().upper()
    magnitude = abs(float(value))
    if token == "A":
        return magnitude > HAZARD_CURRENT_A
    if token == "V" and magnitude > HAZARD_VOLTAGE_V:
        return not supports_manual_range or not range_verified
    return False


def _probe(source: Any, point: Any, level: float,
           configure: Callable[[float], Sequence[str]] | None,
           read: Callable[[], float]) -> tuple[float, float, list[str]]:
    """De-energize, configure the meter, source *level*, read it once.

    Returns ``(sourced, reading, commands)``.

    **The standby comes first, and it is not belt-and-braces.**  ``apply`` is
    the only thing in the loop that de-energizes (the adapter's ``set_output``
    emits ``STBY`` before the new value), so without this the meter would be
    re-ranged while the *previous* point is still live -- and a probe is by
    definition a step down.  On the 34401A's current table that is a
    ``CURR:DC:RANG`` for the 10 mA range, a 5 ohm shunt, while the calibrator
    is still sourcing 1 A: 5 V demanded against a 2 V compliance limit, fault
    1819, and a trip in the middle of an adjustment whose earlier points are
    already written.

    The meter is then configured **before** the source is applied, which is
    §9's rule and the reason this helper exists rather than each module doing
    it in its own order.  Callers range the meter from the *probe's own
    level*, not from the step's nominal: with the output already dark (the
    standby above), ranging down is safe, and a probe read on the range the
    planned point will use can sit below that range's own floor and abort a
    bench that was wired correctly.  The caller then ranges the meter back up
    to the step's nominal -- under the harmless probe level, before the
    planned value is applied -- so a range step up on the meter always
    precedes the amplitude step up on the source.
    """
    sent: list[str] = []
    source.standby()
    if configure is not None:
        sent.extend(configure(level))
    state = source.apply(replace(point, value=level))
    sourced = float(getattr(state, "actual_value", level))
    reading = float(read())
    return sourced, reading, sent


def prove_wiring(source: Any, point: Any, *,
                 configure: Callable[[float], Sequence[str]] | None,
                 read: Callable[[], float],
                 unit: str, full_scale: float | None = None,
                 meter_model: str | None = None,
                 note: Callable[[str], Any] | None = None,
                 what: str = "this point") -> list[str]:
    """Source a small safe signal and check it loosely, or refuse (§8).

    Called before the first sourced step of a procedure, at every function or
    connection change, and whenever the source changes range.  A lead in the
    wrong socket, an open sense pair or a current path through the volts
    terminals all read as "nothing" here, at 10 mV, instead of at 100 V with
    ``CAL:VAL`` about to believe it.

    *meter_model* names the model file the level must be readable on -- the
    controller's amendment of §8, 2026-08-22.  Where no safe level is readable
    the proof is **skipped** rather than faked or aborted on, and *note* (when
    given) is called with the plain-language reason so the step's record and
    the operator both have it.

    Returns the commands it sent, for the record.  Raises
    :class:`WiringProofError` on a reading that is not what was sourced.
    """
    level, refusal = _probe_level_and_refusal(
        unit, float(point.value), full_scale=full_scale,
        meter_model=meter_model, quantity=getattr(point, "quantity", None),
        frequency_hz=float(getattr(point, "frequency_hz", 0.0) or 0.0),
        mode_id=getattr(point, "mode", None))
    if level is None:
        if refusal and note is not None:
            note(f"no wiring proof was taken before {what}: {refusal}")
        return []
    # Labelled, so the record says which application was the proof and which
    # was the point -- and so a fake bench can tell them apart too.
    probe = replace(point, label=f"wiring proof before {what}")
    sourced, reading, sent = _probe(source, probe, level, configure, read)
    if probe_ok(reading, sourced):
        return sent
    raise WiringProofError(
        f"the wiring proof for {what} read {reading:.6g} {unit} where "
        f"{sourced:.6g} {unit} was sourced, more than "
        f"{PROBE_TOLERANCE * 100:g} % away. The wiring is not what this "
        f"procedure assumes, so {float(point.value):g} {unit} was NOT sourced "
        f"into it and NOTHING WAS WRITTEN. Check the leads, the sense "
        f"connections and the meter's function, then run this step again.")


def pre_step(source: Any, point: Any, *,
             configure: Callable[[float], Sequence[str]] | None,
             read: Callable[[], float], unit: str,
             what: str = "this point") -> list[str]:
    """Source a tenth of the target and prove the loop before the rest (§9).

    Unconditional above 220 mA: manual ranging does not catch a wrong shunt
    or a lead in the wrong jack, and at 1 A that is what it costs.
    """
    target = float(point.value)
    level = target * PRESTEP_FRACTION
    # Labelled like the proof, so a record (or a fake bench) can tell a
    # pre-step from the point it rehearses.
    rehearsal = replace(point, label=f"pre-step before {what}")
    sourced, reading, sent = _probe(source, rehearsal, level, configure, read)
    if probe_ok(reading, sourced):
        return sent
    raise WiringProofError(
        f"the 10 % pre-step for {what} read {reading:.6g} {unit} where "
        f"{sourced:.6g} {unit} was sourced, more than "
        f"{PROBE_TOLERANCE * 100:g} % away. {target:g} {unit} was NOT "
        f"sourced and NOTHING WAS WRITTEN.")


def verify_meter_range(readback: Callable[[], float | None] | None, *,
                       expected: float, unit: str, value: float,
                       what: str = "this point") -> bool:
    """Read the meter's range back and compare it, before the source operates.

    Only above the hazard thresholds, and only where the meter can answer: a
    ``None`` read-back means "this instrument cannot say", which is not the
    same as "the range is wrong" (the 1281 has no range query at all).  The
    case this exists to prevent is a meter left on 100 mV meeting 1000 V.

    Returns **whether the range was really verified**, which is what
    :func:`prestep_needed` needs: where it was not, the pre-step is the guard
    instead, and a caller that treated "could not ask" as "checked" would
    energise 1000 V with neither.
    """
    if readback is None or not hazardous_output(unit, value, expected):
        return False
    try:
        actual = readback()
    except Exception as exc:                       # noqa: BLE001 - fatal
        raise WiringProofError(
            f"the meter's range could not be read back before sourcing "
            f"{value:g} {unit} for {what} ({type(exc).__name__}: {exc}); "
            f"nothing was energized") from exc
    if actual is None:
        return False
    if math.isclose(float(actual), float(expected), rel_tol=1e-6, abs_tol=0.0):
        return True
    raise WiringProofError(
        f"the meter reports a range of {float(actual):g} {unit} where it was "
        f"configured for {float(expected):g} {unit}, and {what} sources "
        f"{value:g} {unit}. Nothing was energized and NOTHING WAS WRITTEN.")


def check_gross_error(reading: float, applied: float, tolerance: float, *,
                      unit: str, what: str = "this point") -> None:
    """Stop the procedure when a reading is off by ten tolerances (§8).

    *tolerance* is absolute and in the point's own unit: the DUT's
    specification there, or the procedure's own acceptance limit where it
    defines one.  A procedure whose acceptance limit is tighter than ten
    times itself will refuse first, and should -- this is the outer bound, not
    the working one.
    """
    if not tolerance or not math.isfinite(tolerance):
        return
    error = abs(float(reading) - float(applied))
    if not math.isfinite(error) or error <= GROSS_ERROR_FACTOR * abs(tolerance):
        return
    raise GrossErrorAbort(
        f"{what}: the meter reads {float(reading):.6g} {unit} where "
        f"{float(applied):.6g} {unit} is applied — off by {error:.6g} {unit}, "
        f"more than {GROSS_ERROR_FACTOR:g} times the "
        f"{abs(tolerance):.6g} {unit} tolerance of this step. That is a "
        f"connection, not an instrument out of specification. The calibrator "
        f"has been put in standby and NOTHING FURTHER WAS WRITTEN.")


def interrupt_lines(ctx: ProcedureContext, *, repeat: bool) -> list[str]:
    """What Ctrl-C says, given what the step in flight can actually be stopped.

    Split out of the signal handler so it can be tested: a handler is
    unreachable from a test that does not raise real signals, and this is the
    text an operator acts on with their hands near a live 750 V terminal.

    The default is the **model-neutral** truth: a long-term command has no
    remote abort, and cycling power is never the answer.  It used to be the
    Fluke 5700A's truth -- "use the front-panel STBY key or line power" -- and
    every 3458A step got it too (M42).  A 3458A has no STBY key, and line
    power is the one action its Calibration Manual forbids: "If power is
    turned off or the Reset button is pushed during an ACAL, an error is
    generated. You must perform an ACAL ALL to recalculate new calibration
    constants" (p.87); the 34401A's Service Guide p.77 says the same about a
    zero adjustment, only worse -- "this may cause ALL calibration memory to
    be lost".  The 5700A's STBY key belongs in the 5700A's own
    :attr:`AdjustStep.abort_hint`, which is printed for a non-abortable step
    as well.

    A step that declares :attr:`AdjustStep.abortable` gets the other message,
    because on an HP 34401A the default one is false in a way that costs
    something: Service Guide p.76 documents four ways to abort a calibration
    in progress, and an operator who believes there are none may reach for the
    line switch.
    """
    step = ctx.current_step
    abortable = bool(step is not None and step.abortable)
    hint = str(getattr(step, "abort_hint", "") or "")
    if repeat:
        if abortable:
            return ["\n  already stopping. To end the command now, use the "
                    "instrument's own abort — see above."]
        lines = ["\n  already stopping — the command in flight has no remote "
                 "abort. Let it finish; do NOT cycle power."]
        if hint:
            lines.append(f"  {hint}")
        return lines

    lines = ["\n  Ctrl-C noted: stopping at the end of this step."]
    if not abortable:
        lines.append("  The command in flight cannot be aborted remotely. Let "
                     "it finish; do NOT cycle power.")
        if hint:
            lines.append(f"  {hint}")
        return lines
    lines.append("  The command in flight CAN be stopped at the instrument, "
                 "and this run will not do it for you.")
    if step is not None and step.abort_hint:
        lines.append(f"  {step.abort_hint}")
    return lines


@contextmanager
def deferred_sigint(ctx: ProcedureContext) -> Iterator[None]:
    """Turn Ctrl-C into :attr:`ProcedureContext.interrupted`.

    A ``KeyboardInterrupt`` raised in the middle of a ``CAL_ADJ`` poll would
    unwind the driver with an ``*OPC`` still outstanding and the calibrator
    still running -- and there is no remote abort to clean up with (guide
    §19.2).  So SIGINT sets a flag, the poll finishes, and the runner stops at
    the next step boundary.  A second Ctrl-C says so rather than escalating.

    Silently does nothing off the main thread, where ``signal.signal`` is not
    available; the flag can still be set by hand.
    """
    def handler(signum, frame):                      # pragma: no cover - tty
        for line in interrupt_lines(ctx, repeat=ctx.interrupted):
            print(line, flush=True)
        ctx.interrupted = True

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):                    # pragma: no cover
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _progress_sink(ctx: ProcedureContext, label: str):
    """A ``progress_cb`` that reports only when the state actually changes.

    Long-term commands poll once a second for minutes; echoing every poll would
    bury the one line that matters.
    """
    last: list[Any] = [None]

    def report(code: int, substate: str) -> None:
        key = (code, substate)
        if key == last[0]:
            return
        last[0] = key
        text = f"{label}: state {code} {substate}".rstrip()
        ctx.log.append(text)
        ctx.console.show(f"  … {text}")

    return report


def _check(ctx: ProcedureContext, what: str) -> float:
    """:func:`check_output_safe` against the context's device."""
    return check_output_safe(ctx.device, what=what)


def _serial_of(device: Any) -> str:
    identity = getattr(device, "identity", None)
    return str(getattr(identity, "serial", "") or "")


def _model_of(device: Any) -> str:
    identity = getattr(device, "identity", None)
    return str(getattr(identity, "model", "") or "")


def _capture_reports(ctx: ProcedureContext, when: str, group: str) -> dict:
    """``CAL_RPT?`` + ``CAL_SLST?`` (+ optional ``CAL_SHIFT?``) into captures.

    Every failure here is a **warning**, never an error: losing the before/after
    report is bad, but aborting an artifact calibration because a report would
    not print is worse -- the calibrator would be left with computed-but-
    uncommitted constants and the operator with nothing to show for the hour.
    """
    capture: dict[str, Any] = {"group": group}
    try:
        text = ctx.device.cal_rpt(group)
        capture["text"] = text
        reports = parse_reports(text)
        capture["report"] = reports[0] if reports else None
        if not reports:
            ctx.warn(f"{when} CAL_RPT? {group} did not parse as a report; "
                     f"the raw text is kept on the result")
    except Exception as exc:
        ctx.warn(f"{when} CAL_RPT? {group} failed: {exc}")
    try:
        capture["shifts"] = ctx.device.cal_slst(group)
    except Exception as exc:
        ctx.warn(f"{when} CAL_SLST? {group} failed: {exc}")
    per_range = {}
    for range_id in ctx.shift_ranges:
        try:
            per_range[range_id] = ctx.device.cal_shift(group, range_id)
        except Exception as exc:
            ctx.warn(f"{when} CAL_SHIFT? {group}, {range_id} failed: {exc}")
    if per_range:
        capture["range_shifts"] = per_range
    ctx.captures[when] = capture
    return capture


def _capture_step(when: str, group: str, prompt: str) -> AdjustStep:
    """A non-committing bookkeeping step that snapshots the reports."""
    def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
        started = _now()
        capture = _capture_reports(ctx, when, group)
        report = capture.get("report")
        shifts = capture.get("shifts") or ()
        response = (f"CAL_RPT? {group}: "
                    f"{'parsed' if report is not None else 'unparsed'}; "
                    f"CAL_SLST? {group}: {len(shifts)} ranges")
        return StepResult(
            step_id=step.id, outcome=StepOutcome.EXECUTED, started_at=started,
            finished_at=_now(), commands=step.commands, response=response,
            detail={"capture": when})

    commands = (f"CAL_RPT? {group}", f"CAL_SLST? {group}")
    return AdjustStep(id=f"capture-{when}", prompt=prompt, action=action,
                      commands=commands, requires_operator=False)


def _common_preflight(ctx: ProcedureContext, *, needs: Sequence[str],
                      what: str, check_zeros: bool = True,
                      strict: bool = False) -> list[str]:
    """Checks every 5700A procedure shares.

    Warm-up is **blocking for a committing run and a warning for a dry run**.
    A rehearsal has to be possible at any time; the real thing must not
    calibrate a cold instrument, and guide §11.6's warm-up is a specification
    condition, not advice.

    *strict* makes the gate block whatever ``commit`` says.  It exists for
    ``CAL_CHK``, which never commits and yet is not a rehearsal: it replaces
    the stored CHECK performance history ([OM ch.6]), so a cold check
    destroys the previous baseline and puts an out-of-condition point in its
    place, and the app reports the run COMPLETE (M15).
    """
    problems: list[str] = []
    missing = [name for name in needs if not callable(getattr(ctx.device, name, None))]
    if missing:
        problems.append(
            f"the instrument object does not implement {', '.join(missing)}; "
            f"{what} needs a Fluke 5700A/5720A driver")
        return problems                    # nothing below can be trusted

    model = _model_of(ctx.device).upper()
    if model and model not in ("5700A", "5720A"):
        problems.append(f"instrument reports model {model!r}, not a 5700A/5720A")

    try:
        _check(ctx, what)
    except SafetyError as exc:
        problems.append(str(exc))

    # "I could not check" is not "it is safe" -- the same rule
    # ``check_output_safe`` applies to an unreadable ``OUT?``. On a committing
    # run an unverifiable specification condition blocks; a dry run may
    # rehearse against an instrument that will not answer.
    def gate(message: str) -> None:
        if ctx.commit or strict:
            problems.append(message)
        else:
            ctx.warn(message + " (dry run: not blocking)")

    try:
        minutes = int(ctx.device.ontime())
    except Exception as exc:
        gate(f"ONTIME? failed ({exc}), so the guide §11.6 warm-up could not "
             f"be verified")
    else:
        if minutes < WARMUP_MINUTES:
            gate(f"the calibrator has been on for {minutes} min; guide §11.6 "
                 f"requires twice the off-time up to {WARMUP_MINUTES} min of "
                 f"warm-up before calibration")

    # Overdue dc zeros **block** a committing run.  The artifact calibration
    # writes 2.2 V-range offsets into non-volatile memory permanently; stale
    # zeros are baked into them and cannot be re-measured away afterwards.
    # By irreversibility this has a stronger case for blocking than a
    # verification run, not a weaker one -- so it takes the same shape as the
    # warm-up gate above.
    if not check_zeros:
        # The dc-zeros procedure is the *fix* for overdue zeros; gating it on
        # them would be a deadlock.
        return problems
    try:
        days = int(ctx.device.cal_days("ZERO"))
    except Exception as exc:
        gate(f"CAL_DAYS? ZERO failed ({exc}), so the {DC_ZERO_INTERVAL_DAYS}-"
             f"day dc-zero interval could not be verified")
    else:
        if days > DC_ZERO_INTERVAL_DAYS:
            gate(f"the last dc zeros calibration was {days} days ago, past "
                 f"the {DC_ZERO_INTERVAL_DAYS}-day interval (fault 1303). Run "
                 f"the 'dc-zeros' procedure first — CAL_ZERO takes about 2½ "
                 f"minutes and removes 2.2 V-range offsets this calibration "
                 f"would otherwise bake into non-volatile memory")
    return problems


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------

def _preview(step: AdjustStep, ctx: ProcedureContext) -> list[str]:
    body = list(step.body)
    if step.setpoint is not None:
        line = (f"once this run continues, the output reaches "
                f"{step.setpoint.describe()}" if step.setpoint_upcoming
                else f"the output will be at {step.setpoint.describe()}")
        if step.setpoint.hazardous:
            line += "  *** HAZARDOUS VOLTAGE ***"
        body.append(line)
    if step.commands:
        verb = ("would send (NOT sent — dry run)"
                if step.writes_nvram and ctx.dry_run else "sends")
        body.append(f"{verb}: " + "  ".join(step.commands))
    if step.writes_nvram:
        body.append("THIS STEP WRITES NON-VOLATILE CALIBRATION MEMORY."
                    if ctx.commit else
                    "this step writes non-volatile memory and is being "
                    "SKIPPED because this is a dry run")
    return body


def _aftercare(ctx: ProcedureContext, step: AdjustStep) -> None:
    """Tell the operator how to undo a physical change when a step stops here.

    The ``store`` step sends them round the back to set the CALIBRATION switch
    to ENABLE.  Every exit from that step other than "it worked" -- declined,
    failed, Ctrl-C -- otherwise leaves them walking away with the calibrator
    write-enabled, because the step that says to put it back sits *after* the
    one that failed and is never reached.
    """
    if not step.aftercare:
        return
    ctx.console.show("── put it back ──", *step.aftercare)
    ctx.warn(f"{step.id} did not complete: " + " ".join(step.aftercare))


def run_procedure(procedure: Procedure, ctx: ProcedureContext,
                  ) -> ProcedureResult:
    """Prompt, gate, execute and record one procedure.

    Never raises on a step failure -- and **do not use
    :attr:`ProcedureResult.failed` as the success test**.  A run where the
    operator declined the commit has ``failed is False`` and stored nothing.
    Branch on :attr:`~ProcedureResult.complete` for "did the whole sequence
    run?" and on :attr:`~ProcedureResult.committed` /
    :attr:`~ProcedureResult.nvram_uncertain` for "what happened to
    non-volatile memory?".

    The whole framework:

    1. ``procedure.precheck(ctx)`` when it has one -- the instrument-only
       checks, run **before** any operator data is collected so nobody types
       three calibration certificates and is then told the thing on the bus is
       not a 5700A,
    2. ``procedure.prepare(ctx)`` when it has one (operator data collection),
    3. ``procedure.preflight(ctx)`` -- any blocking problem stops the run
       before step 1, and :class:`PreflightError` is the only thing this
       raises,
    4. for each step: show it, block on Enter when it needs a physical action,
       skip it when it writes NVRAM and this is a dry run, require a typed
       confirmation when it writes NVRAM and this is not, then run it,
    5. ``procedure.finalize(ctx)``.

    A step that raises stops the run. An adjustment sequence is **not**
    restartable in the middle -- resuming after a failed ``CAL_REF`` is exactly
    what fault 852 exists to catch -- so continuing would be worse than
    stopping.
    """
    ctx.started_at = ctx.started_at or _now()

    def stop(problems: Sequence[str]) -> None:
        ctx.error = "; ".join(problems)
        ctx.finished_at = _now()
        raise PreflightError(problems)

    precheck = getattr(procedure, "precheck", None)
    if callable(precheck):
        problems = precheck(ctx)
        if problems:
            stop(problems)

    prepare = getattr(procedure, "prepare", None)
    if callable(prepare):
        prepare(ctx)

    problems = procedure.preflight(ctx)
    if problems:
        stop(problems)

    steps = list(procedure.steps(ctx))
    ctx.console.show(
        f"── {procedure.name} ──",
        f"{len(steps)} steps, "
        f"{'DRY RUN — nothing will be committed' if ctx.dry_run else 'COMMITTING RUN'}",
        *(f"  {n}. {step.id}" for n, step in enumerate(steps, 1)))

    with deferred_sigint(ctx):
        for index, step in enumerate(steps):
            if ctx.interrupted:
                ctx.unreached.extend(s.id for s in steps[index:])
                break
            # Before the prompt, so a Ctrl-C anywhere in this step -- including
            # while the operator is being asked -- is answered with this
            # step's truth about whether the instrument can be stopped.
            ctx.current_step = step
            banner = f"── step {index + 1}/{len(steps)}: {step.id} ──"
            body = _preview(step, ctx)

            # The dry-run skip comes *before* the prompt on purpose: telling an
            # operator to walk round the back and flip the CALIBRATION switch
            # and then not storing anything would be a lie told in the right
            # order.  The step's prompt is *quoted*, not issued -- it is being
            # reported, not asked for.
            if step.writes_nvram and ctx.dry_run:
                ctx.console.show(
                    banner, "SKIPPED — dry run. This step would have asked:",
                    f"  {step.prompt}", *body,
                    *((step.dry_run_note,) if step.dry_run_note else ()))
                if step.dry_run_note:
                    ctx.warn(step.dry_run_note)
                ctx.record(StepResult(
                    step_id=step.id, outcome=StepOutcome.SKIPPED_DRY_RUN,
                    started_at=_now(), finished_at=_now(), writes_nvram=True,
                    skipped_commands=step.commands,
                    response="dry run: not committed",
                    operator="dry run (pass commit=True to write)",
                    detail={"dry_run_note": step.dry_run_note}))
                continue

            # One try for everything that talks to the operator.  The typed
            # confirmation is *inside* it: a Ctrl-C or a closed stdin at the
            # store prompt used to propagate straight out of run_procedure and
            # destroy the whole audit trail -- after CAL_TEMP, four CAL_REFs
            # and CAL_ADJ had already run.
            try:
                if step.requires_operator:
                    ctx.console.prompt(banner, step.prompt, *body)
                    declined = not ctx.console.confirm_ok(
                        f"step {step.id}",
                        hint="[Y = run it / anything else = skip it]")
                    reason = "declined at the prompt"
                else:
                    ctx.console.show(banner, step.prompt, *body)
                    declined = False
                    reason = ""
                if not declined and step.writes_nvram:
                    declined = not ctx.console.confirm_explicit(
                        step.confirm,
                        hint='[type "yes" to commit / anything else = SKIP '
                             'this step permanently]')
                    reason = "declined the typed confirmation"
            except KeyboardInterrupt:
                ctx.interrupted = True
                _aftercare(ctx, step)
                ctx.unreached.extend(s.id for s in steps[index:])
                break

            if declined:
                ctx.record(StepResult(
                    step_id=step.id, outcome=StepOutcome.SKIPPED_DECLINED,
                    started_at=_now(), finished_at=_now(),
                    writes_nvram=step.writes_nvram,
                    skipped_commands=step.commands, operator=reason))
                _aftercare(ctx, step)
                if step.required:
                    # Its whole content is an acknowledgement the rest of the
                    # sequence depends on.  "No" cannot mean "carry on anyway".
                    ctx.error = (
                        f"{step.id}: declined, and that step is required — "
                        f"{step.prompt}")
                    ctx.unreached.extend(s.id for s in steps[index + 1:])
                    break
                continue

            started = _now()
            try:
                result = step.action(ctx, step)
            except KeyboardInterrupt:
                ctx.interrupted = True
                _aftercare(ctx, step)
                ctx.unreached.extend(s.id for s in steps[index + 1:])
                break
            except Exception as exc:
                nvram, sent, held = classify_failure(
                    exc, step.commands, writes_nvram=step.writes_nvram)
                ctx.record(StepResult(
                    step_id=step.id, outcome=StepOutcome.FAILED,
                    started_at=started, finished_at=_now(),
                    writes_nvram=step.writes_nvram, nvram=nvram,
                    commands=sent, skipped_commands=held,
                    error=f"{exc.__class__.__name__}: {exc}",
                    detail={"declared_commands": step.commands}))
                ctx.error = f"{step.id}: {exc}"
                if nvram is NvramState.UNKNOWN:
                    ctx.warn(
                        f"{step.id}: {' '.join(step.commands)} was sent and "
                        f"its outcome could not be read back. Non-volatile "
                        f"memory may already have been written — confirm with "
                        f"{nvram_verify_hint(procedure.model_id)} before "
                        f"trusting the instrument.")
                _aftercare(ctx, step)
                ctx.unreached.extend(s.id for s in steps[index + 1:])
                break
            ctx.record(result)

            if step.verify_after is not None and ctx.verifier is not None:
                try:
                    ctx.captures.setdefault("verify", {})[step.id] = \
                        ctx.verifier(step.verify_after)
                except Exception as exc:
                    ctx.warn(f"verification after {step.id} failed: {exc}")

    ctx.current_step = None
    ctx.finished_at = _now()
    return procedure.finalize(ctx)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_REGISTRY: dict[tuple[str, str], Procedure] = {}


def register(procedure: Procedure) -> Procedure:
    """Register a stateless procedure instance under every model id it claims.

    ``model_ids`` (optional) lists aliases; ``model_id`` is the primary and is
    the spec-file id, so ``cal_verify --adjust`` can look one up from the same
    string it already uses to load a :class:`~caldmm.cal.specs.ModelSpec`.
    """
    ids = tuple(getattr(procedure, "model_ids", ()) or (procedure.model_id,))
    for model_id in ids:
        key = (str(model_id).strip().lower(),
               str(procedure.procedure_id).strip().lower())
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not procedure:
            raise ProcedureError(
                f"a different procedure is already registered for "
                f"{key[0]}/{key[1]}: {existing!r}")
        _REGISTRY[key] = procedure
    return procedure


def get_procedure(model_id: str, procedure_id: str) -> Procedure:
    key = (str(model_id).strip().lower(), str(procedure_id).strip().lower())
    try:
        return _REGISTRY[key]
    except KeyError:
        known = ", ".join(sorted(p for m, p in _REGISTRY if m == key[0]))
        raise ProcedureError(
            f"no procedure {procedure_id!r} for model {model_id!r}"
            + (f"; known: {known}" if known else
               "; no procedures are registered for that model")) from None


def procedures_for(model_id: str) -> tuple[Procedure, ...]:
    key = str(model_id).strip().lower()
    return tuple(proc for (model, _), proc in sorted(_REGISTRY.items())
                 if model == key)


def available_procedures() -> Mapping[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for model, procedure_id in sorted(_REGISTRY):
        out.setdefault(model, []).append(procedure_id)
    return MappingProxyType({k: tuple(v) for k, v in out.items()})


# --------------------------------------------------------------------------
# Fluke 5700A / 5720A procedures
# --------------------------------------------------------------------------

#: The mandatory ``CAL_REF`` order of guide §14.6, as
#: ``(step id, standard key, sign)``.  Out of order the instrument answers
#: fault 852 -- but only after burning a long-term command on it, which is why
#: :class:`Fluke5700AArtifactCal` refuses first.  The driver deliberately does
#: **not** enforce this: it exposes one step and knows nothing about which
#: artefact is on the posts.
CAL_REF_ORDER: tuple[tuple[str, str, int], ...] = (
    ("ref-10v-pos", "10V", +1),
    ("ref-10v-neg", "10V", -1),
    ("ref-10kohm", "10KOHM", +1),
    ("ref-1ohm", "1OHM", +1),
)

#: The 5700A's own stop, for the steps that have one.  It lives on the step
#: rather than in :func:`interrupt_lines`' default text (M42): the default is
#: printed for every instrument, and on a 3458A the front-panel STBY key does
#: not exist and the line switch is the one thing its Calibration Manual p.87
#: forbids.
ABORT_HINT_5700A = (
    "On this calibrator the front-panel STBY key is the stop — STBY sent over "
    "the bus while a long-term command runs is rejected with fault 2002 "
    "(guide §19.2). The constants this step computes are volatile until a "
    "later step stores them.")

_SAFETY_BRIEF = (
    "There is NO REMOTE ABORT for CAL_REF, CAL_ADJ, CAL_ZERO or CAL_CHK "
    "(guide §19.2).",
    "STBY sent while one runs is rejected with fault 2002 — the FRONT-PANEL "
    "STBY key",
    "and the line switch are the only ways to stop it. Keep one within reach.",
    "Each step below can take minutes; the calibrator is doing real work, not "
    "hung.",
)


def _cal_ref_command(value: float, unit: str) -> str:
    """The exact text the driver puts on the wire for one ``CAL_REF`` step.

    Mirrors ``Fluke5700A.cal_ref`` (``f"CAL_REF {magnitude:.10E} {unit}"``)
    plus the ``;*OPC`` its long-term runner appends.  A test asserts this
    equals what actually lands in the transport log, so the two cannot drift.
    """
    return f"CAL_REF {float(value):.10E} {unit.upper()};*OPC"


class Fluke5700AArtifactCal:
    """The 5700A/5720A main-output artifact calibration (guide §14.6).

    Four external-standard steps in a mandatory order, an internal adjustment,
    and a commit that needs a switch on the back of the instrument.

    The order is the whole safety story of this class.  ``CAL_REF`` is not
    idempotent and not re-orderable: the calibrator tracks which step it
    expects and answers fault 852 to anything else, *after* spending the
    long-term command.  So the procedure refuses first, from its own record of
    which steps actually completed — which means a declined or failed step
    blocks everything downstream of it, exactly as the instrument would.
    """

    model_id = "fluke5700a"
    procedure_id = "artifact-cal"
    name = "Fluke 5700A/5720A artifact calibration (main output)"

    # -- operator data ---------------------------------------------------

    def prepare(self, ctx: ProcedureContext) -> None:
        """Collect the ambient temperature and the three standards.

        Called by :func:`run_procedure` before :meth:`preflight`, so a caller
        that has already filled them in (a script, a test) is untouched.
        """
        if ctx.ambient_c is None:
            ctx.console.show(
                "── ambient temperature ──",
                "CAL_TEMP must be sent before the first CAL_REF; without it "
                "the calibrator",
                "assumes 23.0 °C, which is a real error source when the lab "
                "is not at 23 °C.")
            for _ in range(5):
                try:
                    ctx.ambient_c = parse_temperature(
                        ask(ctx.console, "ambient temperature [°C]: "))
                    break
                except StandardValueError as exc:
                    ctx.console.show("── rejected ──", str(exc))
        ctx.standards = prompt_standards(ctx.console, ARTIFACT_STANDARDS,
                                         ctx.standards)

    # -- preflight -------------------------------------------------------

    #: What :meth:`precheck` and :meth:`preflight` both require of the object
    #: on the bus.  Duck-typed: this module never imports a driver.
    NEEDS = ("cal_temp", "cal_ref", "cal_adj", "cal_store", "out_actual",
             "cal_rpt")

    def precheck(self, ctx: ProcedureContext) -> list[str]:
        """The instrument-only half, run **before** :meth:`prepare`.

        Three calibration certificates are three certified values, three
        uncertainties and three serials to type. Discovering afterwards that
        the thing on the bus is not a 5700A, or is stone cold, and having
        ``PreflightError`` throw the typing away, is a bad enough experience to
        be worth a separate pass.
        """
        return _common_preflight(ctx, what="an artifact calibration",
                                 needs=self.NEEDS)

    def preflight(self, ctx: ProcedureContext) -> list[str]:
        problems = _common_preflight(ctx, what="an artifact calibration",
                                     needs=self.NEEDS)
        if ctx.ambient_c is None:
            problems.append(
                "no ambient temperature: CAL_TEMP must precede the first "
                "CAL_REF (guide §14.6), and the calibrator's 23.0 °C default "
                "is a silent error source")
        else:
            try:
                parse_temperature(f"{ctx.ambient_c:g}")
            except StandardValueError as exc:
                problems.append(str(exc))
        for spec in ARTIFACT_STANDARDS:
            standard = ctx.standards.get(spec.key)
            if standard is None:
                problems.append(
                    f"no certified value entered for the {spec.name} "
                    f"({spec.describe()})")
            elif (standard.uncertainty_ppm
                  > spec.recommended_uncertainty_ppm * 1.01):
                # The 1 % margin is float noise, not tolerance: an entry that
                # round-trips to 1.5000000000000002 ppm is 1.5 ppm.
                ctx.warn(
                    f"{spec.name}: entered uncertainty "
                    f"{standard.uncertainty_ppm:.2f} ppm is worse than the "
                    f"±{spec.recommended_uncertainty_ppm:g} ppm of the "
                    f"{spec.recommended} the procedure specifies; every "
                    f"subsequent output inherits it")
        if ctx.commit:
            ctx.warn("commit=True: CAL_STORE will be offered, and the rear "
                     "CALIBRATION switch must be set to ENABLE for it")
        return problems

    # -- the order guard -------------------------------------------------

    def require_order(self, ctx: ProcedureContext, step_id: str) -> None:
        """Refuse a ``CAL_REF`` step whose predecessors have not completed.

        Public because it is the point of the class: an app that lets an
        operator retry or skip a step must be able to ask.
        """
        ids = [entry[0] for entry in CAL_REF_ORDER]
        if step_id not in ids:
            raise ProcedureError(f"{step_id!r} is not a CAL_REF step")
        index = ids.index(step_id)
        if not ctx.completed("cal-temp"):
            raise StepOrderError(
                f"refusing to send {step_id}: CAL_TEMP has not been sent. "
                f"Guide §14.6 requires it before the first CAL_REF; without "
                f"it the calibrator silently assumes 23.0 °C.")
        missing = [name for name in ids[:index] if not ctx.completed(name)]
        if missing:
            raise StepOrderError(
                f"refusing to send {step_id}: the mandatory CAL_REF order of "
                f"guide §14.6 is +10 V, −10 V, 10 kΩ, 1 Ω, and "
                f"{', '.join(missing)} did not complete. The instrument would "
                f"answer fault {FAULT_CAL_STEPS_OUT_OF_ORDER} (Calibration "
                f"Steps Out Of Order) — but only after spending a full "
                f"long-term command on it, and the sequence would then have "
                f"to be restarted from CAL_TEMP anyway. Nothing was sent.")
        if ctx.completed(step_id):
            raise StepOrderError(
                f"refusing to send {step_id} twice: each CAL_REF step is "
                f"taken once, in order. Restart from CAL_TEMP if the sequence "
                f"has to be redone.")

    def cal_ref_step(self, ctx: ProcedureContext, step_id: str) -> StepResult:
        """Run one ``CAL_REF`` step, order-checked before anything is sent."""
        self.require_order(ctx, step_id)
        entry = next(e for e in CAL_REF_ORDER if e[0] == step_id)
        _, key, sign = entry
        standard = ctx.standard(key)
        value = sign * standard.certified
        command = _cal_ref_command(value, standard.unit)
        started = _now()
        _check(ctx, command)
        try:
            ctx.device.cal_ref(value, standard.unit,
                               progress_cb=_progress_sink(ctx, step_id))
        except Exception as exc:
            raise _translate(exc, what=command) from exc
        return StepResult(
            step_id=step_id, outcome=StepOutcome.EXECUTED, started_at=started,
            finished_at=_now(), commands=(command,),
            response=f"accepted {value:.9g} {standard.unit}",
            detail={"standard": standard, "value": value,
                    "uncertainty": standard.uncertainty,
                    "uncertainty_ppm": standard.uncertainty_ppm})

    # -- steps -----------------------------------------------------------

    def steps(self, ctx: ProcedureContext) -> Iterator[AdjustStep]:
        yield self._brief_step()
        yield _capture_step("before", "CAL",
                            "Capturing the calibration report as it stands, "
                            "so the session can be diffed afterwards.")
        yield self._cal_temp_step(ctx)
        yield from self._ref_steps(ctx)
        yield self._adjust_step()
        yield _capture_step(
            "after", "CAL",
            "Capturing the report AFTER CAL_ADJ and BEFORE any commit — "
            "these are the constants a CAL_STORE would freeze.")
        yield self._store_step()
        # Only when something could actually have been stored: in a dry run the
        # switch is never touched, so telling the operator to put it back would
        # be one more instruction that does not match what happened.
        if ctx.commit:
            yield self._switch_back_step()

    def _brief_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=_now(), finished_at=_now(),
                              operator="acknowledged the safety brief")
        return AdjustStep(
            id="brief",
            prompt="Read this before the first long-term command.",
            action=action, body=_SAFETY_BRIEF, required=True)

    def _cal_temp_step(self, ctx: ProcedureContext) -> AdjustStep:
        ambient = float(ctx.ambient_c if ctx.ambient_c is not None else 23.0)
        command = f"CAL_TEMP {ambient:g}"

        def action(c: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            c.device.cal_temp(ambient)
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=started, finished_at=_now(),
                              commands=(command,),
                              response=f"ambient recorded as {ambient:g} °C",
                              detail={"ambient_c": ambient})

        return AdjustStep(
            id="cal-temp",
            prompt=f"Recording the ambient temperature as {ambient:g} °C.",
            action=action, commands=(command,), requires_operator=False)

    _REF_PROMPTS = {
        "ref-10v-pos": (
            "Connect the 10 V dc reference to the calibrator's OUTPUT "
            "terminals:",
            "  standard HI → OUTPUT HI, standard LO → OUTPUT LO.",
            "Step 1 of 4 of the mandatory order (+10 V, −10 V, 10 kΩ, 1 Ω)."),
        "ref-10v-neg": (
            "REVERSE THE LEADS on the same 10 V reference:",
            "  standard HI → OUTPUT LO, standard LO → OUTPUT HI.",
            "The certified value is unchanged; the procedure sends its "
            "negative.",
            "Step 2 of 4."),
        "ref-10kohm": (
            "Disconnect the 10 V reference. Connect the 10 kΩ standard to the "
            "OUTPUT",
            "and SENSE terminals (4-wire), observing the standard's own "
            "current direction.",
            "Step 3 of 4."),
        "ref-1ohm": (
            "Disconnect the 10 kΩ standard. Connect the 1 Ω standard 4-wire.",
            "Lead resistance matters here — use the standard's own potential "
            "terminals.",
            "Step 4 of 4."),
    }

    def _ref_steps(self, ctx: ProcedureContext) -> Iterator[AdjustStep]:
        for step_id, key, sign in CAL_REF_ORDER:
            standard = ctx.standards.get(key)
            if standard is None:
                raise ProcedureError(
                    f"cannot build the {step_id} step: no standard for {key!r}"
                    f" (preflight should have blocked this)")
            value = sign * standard.certified
            command = _cal_ref_command(value, standard.unit)
            prompt, *rest = self._REF_PROMPTS[step_id]

            def action(c: ProcedureContext, step: AdjustStep,
                       _id: str = step_id) -> StepResult:
                return self.cal_ref_step(c, _id)

            yield AdjustStep(
                id=step_id, prompt=prompt, action=action,
                setpoint=SetPoint(value, standard.unit,
                                  label=f"{value:.9g} {standard.unit} "
                                        f"(the standard, not a source "
                                        f"setting)"),
                commands=(command,), abort_hint=ABORT_HINT_5700A,
                body=(*rest,
                      f"certified: {standard.describe()}"))

    def _adjust_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            missing = [sid for sid, _, _ in CAL_REF_ORDER
                       if not ctx.completed(sid)]
            if missing:
                raise StepOrderError(
                    f"refusing to send CAL_ADJ: {', '.join(missing)} did not "
                    f"complete. CAL_ADJ computes the new constants from all "
                    f"four CAL_REF steps; running it on a partial set would "
                    f"produce constants derived from stale references. "
                    f"Nothing was sent.")
            started = _now()
            _check(ctx, "CAL_ADJ")
            try:
                ctx.device.cal_adj(progress_cb=_progress_sink(ctx, "CAL_ADJ"))
            except Exception as exc:
                raise _translate(exc, what="CAL_ADJ") from exc
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=("CAL_ADJ;*OPC",),
                response="new constants computed in volatile memory only")

        return AdjustStep(
            id="adjust",
            prompt="Running the internal adjustment (CAL_ADJ). This is the "
                   "longest step — minutes, with no remote abort.",
            action=action, commands=("CAL_ADJ;*OPC",),
            abort_hint=ABORT_HINT_5700A,
            body=("Nothing is committed by this step: CAL_ADJ computes the "
                  "new constants",
                  "into volatile memory. Omitting the CAL_STORE that follows "
                  "collects the",
                  "performance data without changing the instrument (guide "
                  "§14.6)."))

    #: What to tell the operator whenever the ``store`` step stops without
    #: succeeding.  The step sent them round the back of the instrument; the
    #: step that says to put the switch back sits *after* it and is never
    #: reached, so without this they walk away write-enabled.
    SWITCH_BACK = (
        "The rear CALIBRATION switch is still in ENABLE. SET IT BACK TO "
        "NORMAL.",
        "Left in ENABLE, any later CAL_STORE, CAL_RNG STORE or CAL_WBGAIN "
        "STORE",
        "can overwrite the calibration constants with no further protection.",
    )

    def store_step(self, ctx: ProcedureContext, *,
                   record: bool = True) -> StepResult:
        """``CAL_STORE`` on its own — the public single-step retry.

        This is the recovery the fault-845 message promises, and it is the
        metrologically right one: the constants ``CAL_ADJ`` computed are still
        live in volatile memory, so flipping the switch and resending
        ``CAL_STORE`` alone commits exactly the calibration that was already
        performed. The alternative — restarting the whole sequence — means
        re-connecting three artefacts for another hour to arrive at the same
        numbers.

        **Only valid in the same session**, and only while nothing has been
        power-cycled or re-adjusted since: the guard requires ``adjust`` to have
        completed *in this context*, which is exactly that condition.

        The result is appended to the context's audit trail by default, so a
        retry after a failed run shows up in ``finalize``'s record.  The
        in-runner action passes ``record=False``, because
        :func:`run_procedure` records what a step returns.
        """
        if not ctx.completed("adjust"):
            raise StepOrderError(
                "refusing to send CAL_STORE: CAL_ADJ did not complete in this "
                "session, so there are no new constants to commit — and "
                "CAL_ADJ's results do not survive a power cycle. Nothing was "
                "sent. Run the artifact calibration from CAL_TEMP.")
        started = _now()
        try:
            ctx.device.cal_store()
        except Exception as exc:
            raise _translate(exc, what="CAL_STORE") from exc
        result = StepResult(
            step_id="store", outcome=StepOutcome.EXECUTED,
            started_at=started, finished_at=_now(), writes_nvram=True,
            commands=("CAL_STORE;*WAI",),
            response="new calibration constants committed",
            operator="typed confirmation given")
        return ctx.record(result) if record else result

    def _store_step(self) -> AdjustStep:
        return AdjustStep(
            id="store",
            prompt="GO TO THE BACK OF THE INSTRUMENT and set the CALIBRATION "
                   "switch to ENABLE.",
            action=lambda ctx, step: self.store_step(ctx, record=False),
            writes_nvram=True, commands=("CAL_STORE;*WAI",),
            body=("There is no remote query for that switch (guide §7.6, gap "
                  "G29). If it is",
                  "left in NORMAL the calibrator answers fault 845 and stores "
                  "nothing.",
                  "This is the only irreversible step: it overwrites the "
                  "calibration constants",
                  "in non-volatile memory. The previous ones are not "
                  "recoverable."),
            confirm="committing the new calibration constants to the "
                    "calibrator's NON-VOLATILE memory (CAL_STORE), "
                    "overwriting the existing ones irreversibly",
            aftercare=self.SWITCH_BACK,
            dry_run_note=(
                "NOTE: CAL_ADJ really ran, so the calibrator is now sourcing "
                "from the NEW constants, in volatile memory. They were not "
                "committed and will be LOST at power-off, reverting to the "
                "stored ones. Power-cycle before using this calibrator for "
                "anything else, or re-run with commit to keep them."))

    def _switch_back_step(self) -> AdjustStep:
        def action(ctx: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=_now(), finished_at=_now(),
                              operator="confirmed the switch is back in NORMAL")
        return AdjustStep(
            id="switch-normal",
            prompt="Set the rear CALIBRATION switch back to NORMAL.",
            action=action, required=True, body=self.SWITCH_BACK[1:],
            aftercare=self.SWITCH_BACK)

    # -- finalize --------------------------------------------------------

    def finalize(self, ctx: ProcedureContext) -> ProcedureResult:
        return _finalize(self, ctx)


def _finalize(procedure: Any, ctx: ProcedureContext) -> ProcedureResult:
    """Build the result from whatever the context accumulated."""
    before = ctx.captures.get("before", {})
    after = ctx.captures.get("after", {})
    before_report = before.get("report")
    after_report = after.get("report")
    report_diff = None
    if isinstance(before_report, CalReport) and isinstance(after_report,
                                                           CalReport):
        try:
            report_diff = diff_reports(before_report, after_report)
        except Exception as exc:                     # pragma: no cover
            ctx.warn(f"report diff failed: {exc}")
    detail: dict[str, Any] = {"captures": ctx.captures, "log": tuple(ctx.log)}
    # Stable order: the artifact sequence first, then anything else present.
    ordered = [spec.key for spec in ARTIFACT_STANDARDS
               if spec.key in ctx.standards]
    ordered += [key for key in ctx.standards if key not in ordered]
    return ProcedureResult(
        model_id=procedure.model_id,
        procedure_id=procedure.procedure_id,
        name=procedure.name,
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
        before_report_text=before.get("text", ""),
        after_report_text=after.get("text", ""),
        before_report=before_report if isinstance(before_report, CalReport) else None,
        after_report=after_report if isinstance(after_report, CalReport) else None,
        report_diff=report_diff,
        interrupted=ctx.interrupted,
        error=ctx.error,
        detail=detail)


class Fluke5700ADcZeros:
    """``CAL_ZERO`` -- the internal dc zeros calibration (guide §9, §11.6).

    Required every 30 days; otherwise the calibrator logs fault 1303 and its
    specifications no longer apply.  It is the one calibration activity that
    does **not** need the rear CALIBRATION switch -- but it *does* write its
    results, so the step is marked ``writes_nvram`` and a dry run holds it
    back.  A dry run of this procedure therefore does very little, which is
    honest: there is nothing to rehearse here except the safety brief and the
    report capture.
    """

    model_id = "fluke5700a"
    procedure_id = "dc-zeros"
    name = "Fluke 5700A/5720A dc zeros calibration (CAL_ZERO)"

    NEEDS = ("cal_zero", "out_actual", "cal_rpt")

    def precheck(self, ctx: ProcedureContext) -> list[str]:
        return self.preflight(ctx)

    def preflight(self, ctx: ProcedureContext) -> list[str]:
        # check_zeros=False: this procedure *is* the fix for overdue zeros, so
        # gating it on them would be a deadlock.
        return _common_preflight(ctx, what="a dc zeros calibration",
                                 needs=self.NEEDS, check_zeros=False)

    def steps(self, ctx: ProcedureContext) -> Iterator[AdjustStep]:
        def brief(c: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=_now(), finished_at=_now(),
                              operator="acknowledged the safety brief")

        yield AdjustStep(
            id="brief",
            prompt="Disconnect everything from the OUTPUT and SENSE "
                   "terminals, then confirm.",
            action=brief, required=True,
            body=(*_SAFETY_BRIEF,
                  "CAL_ZERO takes about 2½ minutes (plus 30 s with a 5725A "
                  "attached)."))
        yield _capture_step("before", "CAL",
                            "Capturing the report before the zeros run.")

        def zeros(c: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            _check(c, "CAL_ZERO")
            try:
                c.device.cal_zero(progress_cb=_progress_sink(c, "CAL_ZERO"))
            except Exception as exc:
                raise _translate(exc, what="CAL_ZERO") from exc
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(), writes_nvram=True,
                commands=("CAL_ZERO;*OPC",),
                response="dc zeros stored; the 30-day clock restarts")

        yield AdjustStep(
            id="zeros",
            prompt="Running CAL_ZERO. About 2½ minutes, no remote abort.",
            action=zeros, writes_nvram=True, commands=("CAL_ZERO;*OPC",),
            abort_hint=ABORT_HINT_5700A,
            body=("The rear CALIBRATION switch is NOT needed for this "
                  "(guide §9 CAL_ZERO),",
                  "but the zeros themselves are written, so a dry run skips "
                  "it."),
            confirm="running CAL_ZERO, which writes new dc zero constants",
            dry_run_note=(
                "NOTE: nothing was changed. The 30-day dc-zero clock has NOT "
                "been restarted and the calibrator still logs fault 1303 if "
                "it was overdue. Re-run with commit to actually zero it."))
        yield _capture_step("after", "CAL",
                            "Capturing the report after the zeros run.")

    def finalize(self, ctx: ProcedureContext) -> ProcedureResult:
        return _finalize(self, ctx)


class Fluke5700ACalCheck:
    """``CAL_CHK`` -- the calibration check (guide §14.7).

    Compares against the *internal* standards and **makes no permanent change
    to the calibration constants** ([OM §6-6]), which is why its step is not
    marked ``writes_nvram``: a dry run of the check is a real check, and the
    check is the safe periodic-confidence operation that ought to be easy to
    run.  It does overwrite the CHECK group's stored report and date, so the
    previous one is captured first and kept on the result.
    """

    model_id = "fluke5700a"
    procedure_id = "cal-check"
    name = "Fluke 5700A/5720A calibration check (CAL_CHK)"

    NEEDS = ("cal_temp", "cal_chk", "out_actual", "cal_rpt")

    #: **The conditions block whatever ``commit`` says** (M15).  This
    #: procedure always runs with ``commit=False`` -- it changes no
    #: calibration constant -- so the shared warm-up and dc-zero gates only
    #: ever warned, and a cold ``CAL_CHK`` really ran: [OM §7-12] says "make
    #: sure the calibrator is powered on and has completed the appropriate
    #: warm-up period", [OM ch.6] says a later check replaces the stored one,
    #: and the app then labels the run COMPLETE.  A rehearsal of the artifact
    #: calibration keeps its relaxation; this is not a rehearsal of anything.
    blocking_preflight = True

    def precheck(self, ctx: ProcedureContext) -> list[str]:
        return _common_preflight(ctx, what="a calibration check",
                                 needs=self.NEEDS,
                                 strict=self.blocking_preflight)

    def preflight(self, ctx: ProcedureContext) -> list[str]:
        problems = _common_preflight(ctx, what="a calibration check",
                                     needs=self.NEEDS,
                                     strict=self.blocking_preflight)
        if ctx.ambient_c is None:
            problems.append(
                "no ambient temperature: CAL_TEMP must precede CAL_CHK "
                "(guide §14.7)")
        return problems

    def prepare(self, ctx: ProcedureContext) -> None:
        if ctx.ambient_c is None:
            for _ in range(5):
                try:
                    ctx.ambient_c = parse_temperature(
                        ask(ctx.console, "ambient temperature [°C]: "))
                    break
                except StandardValueError as exc:
                    ctx.console.show("── rejected ──", str(exc))

    def steps(self, ctx: ProcedureContext) -> Iterator[AdjustStep]:
        ambient = float(ctx.ambient_c if ctx.ambient_c is not None else 23.0)

        def brief(c: ProcedureContext, step: AdjustStep) -> StepResult:
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=_now(), finished_at=_now(),
                              operator="acknowledged the safety brief")

        yield AdjustStep(
            id="brief",
            prompt="Disconnect everything from the OUTPUT and SENSE "
                   "terminals, then confirm.",
            action=brief, required=True,
            body=(*_SAFETY_BRIEF,
                  "CAL_CHK changes no calibration constant — it measures "
                  "against the",
                  "internal standards — but it does overwrite the previous "
                  "CHECK report,",
                  "which is captured first."))
        yield _capture_step("before", "CHECK",
                            "Capturing the previous CHECK report before it is "
                            "overwritten.")

        def temp(c: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            c.device.cal_temp(ambient)
            return StepResult(step_id=step.id, outcome=StepOutcome.EXECUTED,
                              started_at=started, finished_at=_now(),
                              commands=(f"CAL_TEMP {ambient:g}",),
                              detail={"ambient_c": ambient})

        yield AdjustStep(
            id="cal-temp",
            prompt=f"Recording the ambient temperature as {ambient:g} °C.",
            action=temp, commands=(f"CAL_TEMP {ambient:g}",),
            requires_operator=False)

        def check(c: ProcedureContext, step: AdjustStep) -> StepResult:
            started = _now()
            _check(c, "CAL_CHK")
            try:
                c.device.cal_chk(progress_cb=_progress_sink(c, "CAL_CHK"))
            except Exception as exc:
                raise _translate(exc, what="CAL_CHK") from exc
            return StepResult(
                step_id=step.id, outcome=StepOutcome.EXECUTED,
                started_at=started, finished_at=_now(),
                commands=("CAL_CHK;*OPC",),
                response="check complete; results are in the CHECK group")

        yield AdjustStep(
            id="check",
            prompt="Running CAL_CHK. Minutes, no remote abort.",
            action=check, commands=("CAL_CHK;*OPC",),
            abort_hint=ABORT_HINT_5700A)
        yield _capture_step("after", "CHECK",
                            "Capturing the new CHECK report.")

    def finalize(self, ctx: ProcedureContext) -> ProcedureResult:
        return _finalize(self, ctx)


register(Fluke5700AArtifactCal())
register(Fluke5700ADcZeros())
register(Fluke5700ACalCheck())

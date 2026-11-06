"""The 5700A's own calibration: dc zeros, calibration check, artifact cal.

The operator shell over :mod:`caldmm.cal.procedure`.  That module does the
work -- it owns the mandatory ``CAL_REF`` order, the commit gate, the
three-valued NVRAM answer and the audit trail.  **Nothing here re-implements
any of it.**  This file resolves the CLI, opens the calibrator, hands
:func:`~caldmm.cal.procedure.run_procedure` a console and a context, and then
does the two things a runner has to do that a framework cannot: write the
record to disk, and tell the operator in plain words what state the instrument
is now in.

**This app writes irreversibly to the calibrator's non-volatile calibration
memory.**  Every design decision below follows from that.

The record is written before anything else
------------------------------------------

An artifact calibration cannot be re-observed.  Once ``CAL_STORE`` has gone
out, the previous constants are gone and the only evidence of what happened is
what was written down.  So :func:`_write_record` is called *the instant*
``run_procedure`` returns -- before the summary is printed, before the diff is
rendered, before anything that could itself fail -- **and on every exception
path, including a ``KeyboardInterrupt`` and the ``SystemExit`` the graceful
shutdown handler raises**.  On those paths the result is built by calling the
procedure's own ``finalize`` against the context that was accumulating all
along, which is the same call ``run_procedure`` makes; the app never fabricates
a result.

That is not enough on its own, because a record can also be lost by never
having had anywhere to go.  :func:`_check_writable` therefore **proves every
output path can be written before the instrument is opened** -- a typo'd or
unmounted ``-o`` otherwise passes the "do not overwrite" check trivially and is
discovered at the end of an hour, with the calibration already committed.  If
the write fails anyway (a disk that filled during the run),
:func:`_fallback_record` puts the session log in a temporary file and the
session exits :data:`EXIT_NO_RECORD` rather than ``0``: a wrapper script must
never read "fine" when the account of an irreversible operation is not where it
was asked to be.

Success is not ``result.failed``
--------------------------------

:attr:`~caldmm.cal.procedure.ProcedureResult.failed` answers "did a step
raise?", and a run in which the operator declined the typed commit
confirmation has ``failed is False`` and stored nothing.  Reading that as
success is precisely the bug this app must not have, so every verdict here is
decided by :func:`sequence_complete`, ``result.committed`` and
``result.nvram_uncertain``, and ``failed`` is used only to *describe* a
failure that has already been detected some other way.

Three outcomes, not two
-----------------------

``CAL_STORE`` reaching the wire without a readable answer is neither success
nor failure: the EEPROM has probably been written and there is no way to tell
from here.  That state gets its own exit status (:data:`EXIT_UNKNOWN`), its own
banner, and instructions naming ``CAL_DATE?`` and ``CAL_RPT?`` as the checks.
It is never folded into either of the other two.

A dry run is not a simulation
-----------------------------

``--artifact`` without ``--store`` really sends ``CAL_TEMP``, four ``CAL_REF``
steps and ``CAL_ADJ``.  The calibrator computes new constants and sources from
them; only the commit is held back.  So the calibrator is left running on
uncommitted constants that vanish at power-off, and **every non-committing run
says so at the end** -- see :func:`_uncommitted_note`.

Interruption
------------

There is no remote abort for ``CAL_REF``/``CAL_ADJ``/``CAL_ZERO``/``CAL_CHK``:
``STBY`` sent while one runs is rejected with fault 2002.  The front-panel STBY
key and the line switch are the only stops, and the safety brief says so before
the first long-term command.  Inside the step loop ``run_procedure``'s
``deferred_sigint`` turns Ctrl-C into a flag, the poll finishes and the run
stops at the next step boundary; outside it,
:func:`~caldmm.lib.signals.setup_graceful_shutdown` standbys and closes.
Either way the record is written.

Exit status
-----------

``0`` the sequence ran (a committing run committed; a dry run held back exactly
the commit); ``1`` it did not -- a step failed, a required step or the commit
was declined, and nothing was committed; ``2`` usage, or a pre-flight refusal
in which nothing was sent; ``3`` interrupted; ``4`` **the non-volatile state is
unknown** and has to be checked by hand; ``5`` **the session log is not where
it was asked to be** -- which wins over all of the above, because it is the one
state whose evidence disappears when the terminal is closed.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from caldmm.cal.calreport import (
    CSV_COLUMNS,
    CalReport,
    ReportDiff,
    diff_reports,
    format_summary,
    parse_reports,
    to_csv_rows,
)
from caldmm.cal.procedure import (
    ARTIFACT_STANDARDS,
    NvramState,
    PreflightError,
    ProcedureContext,
    ProcedureError,
    ProcedureResult,
    StandardValueError,
    StepOutcome,
    StepResult,
    classify_failure,
    get_procedure,
    parse_temperature,
    run_procedure,
)
from caldmm.cal.result import caldmm_version
from caldmm.drivers import fluke5700a as f57
from caldmm.drivers.fluke5700a import Fluke5700A
from caldmm.lib.console import OperatorConsole
from caldmm.lib.formatting import plural as _plural
from caldmm.lib.signals import setup_graceful_shutdown

__all__ = [
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_USAGE",
    "EXIT_ABORTED",
    "EXIT_UNKNOWN",
    "EXIT_NO_RECORD",
    "MODEL_ID",
    "MODES",
    "REPORT_GROUPS",
    "Paths",
    "RecordOutcome",
    "SelfCalError",
    "TranscriptConsole",
    "build_parser",
    "default_base",
    "diff_csv_rows",
    "exit_status",
    "format_diff",
    "format_outcome",
    "format_record",
    "main",
    "output_paths",
    "run",
    "sequence_complete",
]


class SelfCalError(RuntimeError):
    """A run that must not start: bad arguments, or a refusal before step 1."""


#: The sequence ran end to end.  For a committing run that means the constants
#: are in non-volatile memory; for a dry run it means everything but the commit
#: really happened, which is the dry run's whole purpose.
EXIT_OK = 0
#: The sequence did not run to the end and **nothing was committed**: a step
#: failed, a required step was declined, or the typed commit confirmation was
#: refused.  Not an error in the program -- an outcome the operator chose or the
#: instrument imposed -- but never a success.
EXIT_FAIL = 1
#: Usage, or a pre-flight refusal.  Nothing was sent to the calibrator.
EXIT_USAGE = 2
#: Interrupted.  Whatever completed before the interrupt is in the record.
EXIT_ABORTED = 3
#: **The non-volatile state is unknown.**  ``CAL_STORE`` reached the instrument
#: and its outcome could not be read back.  Deliberately its own status: a
#: script that folded this into either 0 or 1 would be stating something about
#: the calibrator that nobody knows.
EXIT_UNKNOWN = 4
#: **The session log is not where it was asked to be.**  The run happened --
#: possibly including the commit -- and the only complete account of it is the
#: terminal, plus whatever :func:`_fallback_record` managed to save.
#:
#: It takes precedence over every other status, including
#: :data:`EXIT_UNKNOWN`, because it is the one condition whose evidence
#: disappears when the window is closed: a caller that sees this must read the
#: console output before doing anything else, and the banner there still says
#: what happened to non-volatile memory.
EXIT_NO_RECORD = 5

#: The spec-file model id every procedure here is registered under.  The
#: procedures run on a 5720A too (same remote surface), but the registry
#: only advertises ids ``load_model()`` accepts, and there is no
#: ``fluke5720a.yaml``.
MODEL_ID = "fluke5700a"

#: ``--flag`` -> the registered ``procedure_id``.
MODES: dict[str, str] = {
    "zeros": "dc-zeros",
    "check": "cal-check",
    "artifact": "artifact-cal",
}

#: ``CAL_RPT?`` groups the instrument accepts.
REPORT_GROUPS = ("CAL", "CHECK", "RAW")

#: The step id of ``Fluke5700AArtifactCal``'s ``CAL_ADJ`` step.  Named here
#: because the "you are running on uncommitted constants" warning is true only
#: when *that* step executed -- it is the one non-NVRAM step that changes what
#: the calibrator sources.
ADJUST_STEP_ID = "adjust"

#: The step id of the ``CAL_STORE`` step, for the single-step retry offer.
STORE_STEP_ID = "store"

#: How many times the single-step ``CAL_STORE`` retry is offered.  Bounded so a
#: rejected commit cannot turn into an infinite prompt loop, and small because
#: the only failure it can fix -- the rear switch in NORMAL -- is fixed in one
#: walk to the back of the instrument.
STORE_RETRY_ATTEMPTS = 3


# --------------------------------------------------------------------------
# The operator console, with a transcript
# --------------------------------------------------------------------------

class TranscriptConsole(OperatorConsole):
    """:class:`~caldmm.lib.console.OperatorConsole` that remembers the session.

    A real subclass, not a stand-in: every prompt an operator sees goes through
    the same code the rest of caldmm uses, and the transcript is captured on
    the way past.  That matters because the record has to state *what was
    asked and what was answered* -- a calibration certificate that says the
    10 V standard was entered as 10.000131 V is only defensible if the entry
    itself was recorded, and re-deriving it from the result afterwards would
    record what the framework kept rather than what the person typed.
    """

    def __init__(self, *, panel: Callable[[str], None] | None = None) -> None:
        super().__init__(panel=panel)
        #: Every line shown and every answer given, in order.
        self.transcript: list[str] = []

    # -- OperatorConsole surface, each recording then delegating ----------

    def show(self, banner: str, *body: str, panel_msg: str | None = None) -> None:
        self._note(banner, body)
        super().show(banner, *body, panel_msg=panel_msg)

    def prompt(self, banner: str, *body: str, panel_msg: str | None = None,
               action: str = 'press Enter when the action above is complete',
               ) -> None:
        # No _note here: OperatorConsole.prompt renders through ``self.show``,
        # which is the override above, so the banner and body are already in
        # the transcript by the time this returns.  Recording them again put
        # every operator-attended step in the record twice.
        super().prompt(banner, *body, panel_msg=panel_msg, action=action)
        self.transcript.append(f"    [Enter] {action}")

    def confirm_ok(self, what: str = 'this measurement', *,
                   hint: str | None = None) -> bool:
        answer = super().confirm_ok(what, hint=hint)
        self.transcript.append(
            f"    [ask] OK with {what}? -> {'yes' if answer else 'NO'}")
        return answer

    def confirm_explicit(self, what: str, *, hint: str | None = None) -> bool:
        answer = super().confirm_explicit(what, hint=hint)
        self.transcript.append(
            f"    [ask] override and accept {what}? -> "
            f"{'YES (typed)' if answer else 'NO'}")
        return answer

    def ask(self, question: str, *, panel_msg: str | None = None) -> str:
        answer = super().ask(question, panel_msg=panel_msg)
        self.transcript.append(f"    [ask] {question.strip()} -> {answer!r}")
        return answer

    def _note(self, banner: str, body: Sequence[str]) -> None:
        self.transcript.append(banner)
        self.transcript.extend(f"    {line}" for line in body if line)


# --------------------------------------------------------------------------
# Where the record goes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    """The files one session writes, all derived from a single basename."""

    base: str

    @property
    def log(self) -> str:
        return f"{self.base}.log"

    @property
    def before(self) -> str:
        return f"{self.base}-before.txt"

    @property
    def after(self) -> str:
        return f"{self.base}-after.txt"

    @property
    def diff_csv(self) -> str:
        return f"{self.base}-diff.csv"

    @property
    def report(self) -> str:
        return f"{self.base}.txt"

    @property
    def report_csv(self) -> str:
        return f"{self.base}.csv"

    def for_procedure(self) -> tuple[str, ...]:
        """What a procedure run writes."""
        return (self.log, self.before, self.after, self.diff_csv)

    def for_report(self) -> tuple[str, ...]:
        """What a ``--report`` capture writes."""
        return (self.report, self.report_csv)


def default_base(what: str, now: datetime | None = None) -> str:
    """``5700a-artifact-cal-20260801T143000`` -- the fallback basename.

    ``-o`` is optional on purpose.  The record of an irreversible operation is
    not something to lose because a flag was forgotten, so a session always has
    somewhere to write; the paths are printed before the first step so the
    operator knows where.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    return f"5700a-{what}-{stamp}"


def output_paths(base: str | None, what: str) -> Paths:
    return Paths(base or default_base(what))


def _refuse_existing(candidates: Sequence[str], force: bool) -> None:
    """Refuse to overwrite an existing record unless ``--force``.

    The same rule ``cal_verify`` applies to a result file, for a stronger
    reason: these files are the only account of a calibration that cannot be
    performed again to produce them.
    """
    if force:
        return
    existing = [path for path in candidates if os.path.exists(path)]
    if existing:
        raise SelfCalError(
            f"these files already exist and would be overwritten: "
            f"{', '.join(existing)}.  Choose another -o, or pass --force.  "
            f"They are the record of a calibration that cannot be repeated to "
            f"reproduce them.")


def _check_writable(candidates: Sequence[str]) -> None:
    """Prove every output path can actually be written, before step 1.

    Refusing to *overwrite* a record is not the same check as being able to
    *write* one, and only the second one matters here.  A typo'd or unmounted
    ``-o`` passes the first check trivially -- nothing exists at that path --
    and then the whole irreversible sequence runs, ``CAL_STORE`` goes out, and
    the write fails with the calibration already committed and its only account
    on a terminal somebody is about to close.

    So each path is created and removed again now, at the desk, where the cost
    of being wrong is a re-typed command.  A missing parent directory is
    created rather than refused: ``-o runs/2026-08-02/artifact`` is a
    reasonable thing to type and the alternative is an error message about a
    directory the operator obviously meant to have.
    """
    for path in candidates:
        directory = os.path.dirname(path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise SelfCalError(
                f"the output directory {directory!r} cannot be created "
                f"({exc.__class__.__name__}: {exc}).  Nothing has been sent to "
                f"the calibrator; fix -o and start again.") from exc
        existed = os.path.exists(path)
        try:
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise SelfCalError(
                f"{path} cannot be written ({exc.__class__.__name__}: {exc}).  "
                f"This session's record would have had nowhere to go, so "
                f"nothing has been sent to the calibrator.  Fix -o and start "
                f"again.") from exc
        if not existed:
            try:
                os.unlink(path)
            except OSError:                           # pragma: no cover - race
                pass


def _prepare_outputs(candidates: Sequence[str], force: bool) -> None:
    """Both output guards, in the order that makes each one meaningful.

    ``_refuse_existing`` first: the probe below creates and removes files, and
    running it first on a path that already holds somebody's calibration record
    would be touching that file to find out whether it may be touched.
    """
    _refuse_existing(candidates, force)
    _check_writable(candidates)


# --------------------------------------------------------------------------
# Small formatting helpers
# --------------------------------------------------------------------------

def _rule(title: str = "", width: int = 78) -> str:
    if not title:
        return "-" * width
    head = f"--- {title} "
    return head + "-" * max(3, width - len(head))


def _stamp(when: datetime | None) -> str:
    return "unknown" if when is None else when.isoformat(" ", "seconds")


def _identity_lines(dev: Any) -> list[str]:
    identity = getattr(dev, "identity", None)
    options = list(getattr(dev, "options", []) or [])
    return [
        f"*IDN?              {getattr(identity, 'raw', '') or 'unknown'}",
        f"model / serial     {getattr(identity, 'model', '?')} / "
        f"{getattr(identity, 'serial', '?')}",
        f"*OPT?              {', '.join(options) if options else 'none'}",
    ]


# --------------------------------------------------------------------------
# Report diffing and its CSV
# --------------------------------------------------------------------------

def format_diff(diff: ReportDiff, count: int = 15) -> str:
    """The before/after table an operator actually reads.

    The headline column is the shift **as a percentage of that range's
    specification**: a point at 90 % of spec still passes and is about to stop
    passing, which is the signal a calibration is run to find.
    """
    lines = [
        f"before printed {_stamp(diff.before_printed_at)}",
        f"after  printed {_stamp(diff.after_printed_at)}",
    ]
    if not diff.deltas:
        lines.append("no points matched between the two reports -- nothing to "
                     "compare (a truncated capture, or two different "
                     "instruments?)")
    else:
        # Three-way, and none of it by subtraction: most of a calibration's
        # points usually do not move at all, and counting those as regressions
        # turns a dc-zero-only run into "259 further away" when 253 of them
        # were untouched.
        counts = diff.counts()
        lines.append(
            f"{_plural(len(diff.deltas), 'point')} compared: "
            f"{counts.improved} moved closer to nominal, "
            f"{counts.worsened} further away, "
            f"{counts.unchanged} unchanged")
        lines.append("")
        lines.append(f"  {'function':<14}{'range':>10} {'point':<10}"
                     f"{'before %':>10}{'after %':>10}{'change':>10}")
        for delta in diff.worst(count):
            lines.append(
                f"  {delta.function:<14}{delta.range_label:>10} "
                f"{delta.point:<10}{delta.before_percent:>10.2f}"
                f"{delta.after_percent:>10.2f}{delta.delta:>+10.2f}")
        if len(diff.deltas) > count:
            lines.append(f"  ... and {len(diff.deltas) - count} more, all in "
                         f"the CSV")
    for label, rows in (("only in the after report", diff.added),
                        ("only in the before report", diff.removed)):
        if rows:
            lines.append(f"{_plural(len(rows), 'point')} {label}: "
                         + ", ".join(f"{r.function} {r.range_label} {r.point}"
                                     for r in rows[:6])
                         + (" ..." if len(rows) > 6 else ""))
    return "\n".join(lines)


def _diff_title(result: ProcedureResult) -> str:
    """What the two captures bracket, which is not the same in every mode."""
    if result.procedure_id == MODES["artifact"]:
        return "Report diff (before -> after CAL_ADJ, before any commit)"
    return "Report diff (before -> after)"


def _rows_by_key(report: CalReport) -> dict[tuple, dict[str, str]]:
    """:func:`to_csv_rows`' string rows, keyed by :attr:`ShiftRow.key`.

    ``to_csv_rows`` iterates ``report.shifts`` in order, so zipping restores
    the key its rows do not themselves carry.  It has to be that key and not
    ``(function, range, point)``: a resistance row's ``point`` is the
    artefact's certified value, which moves by a few ppm between two reports of
    the same instrument, so joining on it would match nothing in exactly the
    table where the artifacts live.
    """
    return {row.key: csv_row
            for row, csv_row in zip(report.shifts, to_csv_rows(report))}


#: Columns of ``<base>-diff.csv``.  Both sides of every numeric field, because
#: the point of the file is that somebody else can check the arithmetic.
DIFF_CSV_COLUMNS = [
    "status", "function", "range", "point", "frequency_hz",
    "before_percent_of_spec", "after_percent_of_spec", "delta_percent_of_spec",
    "before_shift_value", "after_shift_value", "shift_unit",
    "before_full_scale_shift", "after_full_scale_shift", "base_unit",
    "spec_limit", "spec_unit",
]


def diff_csv_rows(before: CalReport, after: CalReport) -> list[dict[str, str]]:
    """One row per point, with both reports' numbers side by side.

    Keyed the same way :func:`~caldmm.cal.calreport.diff_reports` keys its
    deltas, so the CSV and the printed table cannot disagree about which points
    matched.  Points present in only one report are kept with a ``status`` of
    ``added``/``removed`` rather than dropped: a missing table is itself a
    finding.
    """
    before_rows = _rows_by_key(before)
    after_rows = _rows_by_key(after)
    keys = list(before_rows) + [k for k in after_rows if k not in before_rows]

    rows: list[dict[str, str]] = []
    for key in keys:
        b = before_rows.get(key)
        a = after_rows.get(key)
        source = b or a
        assert source is not None
        status = "both" if b and a else ("removed" if b else "added")
        delta = ""
        if b and a:
            try:
                delta = f"{float(a['percent_of_spec']) - float(b['percent_of_spec']):.12g}"
            except ValueError:                       # pragma: no cover - guard
                delta = ""
        rows.append({
            "status": status,
            "function": source["function"],
            "range": source["range"],
            "point": source["point"],
            "frequency_hz": source["frequency_hz"],
            "before_percent_of_spec": b["percent_of_spec"] if b else "",
            "after_percent_of_spec": a["percent_of_spec"] if a else "",
            "delta_percent_of_spec": delta,
            "before_shift_value": b["shift_value"] if b else "",
            "after_shift_value": a["shift_value"] if a else "",
            "shift_unit": source["shift_unit"],
            "before_full_scale_shift": b["full_scale_shift"] if b else "",
            "after_full_scale_shift": a["full_scale_shift"] if a else "",
            "base_unit": source["base_unit"],
            "spec_limit": source["spec_limit"],
            "spec_unit": source["spec_unit"],
        })
    return rows


def _write_csv(path: str, columns: Sequence[str],
               rows: Sequence[dict[str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# Reading the result
# --------------------------------------------------------------------------

def sequence_complete(result: ProcedureResult) -> bool:
    """Did the whole sequence run, counting a dry run's held-back commit?

    For a committing run this is exactly
    :attr:`~caldmm.cal.procedure.ProcedureResult.complete`.

    A **dry run can never be** ``complete``: its NVRAM steps are
    ``SKIPPED_DRY_RUN`` by design, and that is the outcome the operator asked
    for.  So a held-back step counts as having run, while everything that means
    the run stopped -- an interrupt, unreached steps, a recorded error, a
    failed step, a step the operator declined -- still says no.

    Note what this is *not*: ``not result.failed``.  A run in which the typed
    commit confirmation was declined has ``failed is False``, stored nothing,
    and must not read as a success.
    """
    if not result.dry_run:
        return result.complete
    if result.interrupted or result.unreached or result.error:
        return False
    return all(step.outcome.ran
               or step.outcome is StepOutcome.SKIPPED_DRY_RUN
               for step in result.steps)


def exit_status(result: ProcedureResult) -> int:
    """The process status for a finished run.

    Ordered so the three-valued NVRAM answer is decided first: a commit whose
    outcome could not be read back is neither of the other two, whatever else
    the run did.
    """
    if result.nvram_uncertain:
        return EXIT_UNKNOWN
    if result.committed:
        # Something was confirmed written.  That can be true of a run that did
        # not otherwise finish -- a single-step CAL_STORE retry, or an operator
        # who walked off before confirming the switch was set back -- and the
        # calibration is stored either way.  The loose ends are printed, loudly.
        return EXIT_OK
    if sequence_complete(result):
        return EXIT_OK
    if result.interrupted:
        return EXIT_ABORTED
    return EXIT_FAIL


def _step(result: ProcedureResult, step_id: str) -> StepResult | None:
    for step in reversed(result.steps):
        if step.step_id == step_id:
            return step
    return None


def _uncommitted_note(result: ProcedureResult) -> list[str]:
    """What a run that committed nothing has left behind in volatile memory.

    ``CAL_ADJ`` is not a dry-run no-op: it really runs, really computes new
    constants, and the calibrator really sources from them until it is powered
    off.  A rehearsal that ended with the operator believing the instrument was
    untouched would be the worst possible outcome of a "safe" mode.

    **Silent when the NVRAM state is unknown.**  This note asserts that nothing
    was stored, and the whole content of :func:`_nvram_unknown_note` is that
    nobody knows whether anything was stored; printing both would answer the
    question the loud one exists to leave open.
    """
    if result.nvram_uncertain:
        return []
    adjust = _step(result, ADJUST_STEP_ID)
    if adjust is None or not adjust.outcome.ran:
        return []
    return [
        "!! THE CALIBRATOR IS NOW RUNNING ON UNCOMMITTED CONSTANTS.",
        "   CAL_ADJ really ran and really computed new calibration constants.",
        "   They live in VOLATILE memory only: nothing was stored, and they "
        "will be",
        "   LOST at power-off, reverting to the constants that are still in "
        "NVRAM.",
        "   Until then this calibrator is sourcing from numbers that are in no "
        "record.",
        "   POWER-CYCLE it before using it for anything else, or re-run with "
        "--store.",
    ]


def _nvram_unknown_note(result: ProcedureResult, paths: Paths) -> list[str]:
    """The loud middle answer.  Never rendered as success or as failure."""
    step = next((s for s in result.steps if s.nvram_uncertain), None)
    sent = "; ".join(step.commands) if step is not None else "CAL_STORE;*WAI"
    return [
        "!! THE STATE OF NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN.",
        f"   {sent} reached the instrument and the outcome could NOT be read "
        f"back.",
        "   This run is NEITHER a success NOR a failure. The calibrator must "
        "be treated",
        "   as POSSIBLY ADJUSTED until somebody checks, by hand:",
        "",
        "       CAL_DATE? CAL     the date of the stored main-output "
        "calibration",
        "       CAL_RPT?  CAL     the stored report",
        "",
        "   If CAL_DATE? shows today, the store went through and this "
        "calibration is",
        "   committed. If it shows the previous date, nothing was stored and "
        "the",
        "   CAL_ADJ constants are still live in volatile memory only, and will "
        "be lost",
        "   at power-off.",
        f"   Compare CAL_RPT? CAL against {paths.before} and {paths.after}.",
        "   Do not use this calibrator as a reference until you know which it "
        "is.",
        "   caldmm-5700a-selfcal --report CAL captures the stored report and "
        "sends no",
        "   calibration command (opening the session does *RST/STBY, so a live "
        "output",
        "   is dropped).",
    ]


def format_outcome(result: ProcedureResult, paths: Paths) -> list[str]:
    """The verdict block: what happened to non-volatile memory, in words."""
    lines: list[str] = []
    if result.nvram_uncertain:
        return _nvram_unknown_note(result, paths)

    if result.committed:
        lines.append("** COMMITTED: new calibration constants are in the "
                     "calibrator's non-volatile memory.")
        lines.append("   The previous constants are gone and are not "
                     "recoverable.")
        if not sequence_complete(result):
            lines.append("   The sequence did not otherwise finish -- read the "
                         "step list and the warnings above.")
        lines.append("   Verify the result before quoting it: "
                     "caldmm-cal-verify --source fluke5700a:<addr> ...")
        return lines

    if sequence_complete(result):
        if result.dry_run:
            lines.append("** DRY RUN COMPLETE: every step ran except the ones "
                         "that write non-volatile memory.")
            for command in result.would_commit:
                lines.append(f"   NOT sent: {command}")
        else:
            lines.append("** COMPLETE: every step ran. Nothing in this "
                         "procedure writes calibration memory.")
    elif result.interrupted:
        lines.append("** INTERRUPTED: the run stopped at a step boundary and "
                     "NOTHING WAS COMMITTED.")
        if result.unreached:
            lines.append("   never reached: " + ", ".join(result.unreached))
    else:
        lines.append("** NOT COMMITTED: the sequence did not run to the end "
                     "and nothing was written.")
        if result.error:
            lines.append(f"   {result.error}")
        for step in result.steps:
            if step.outcome is StepOutcome.SKIPPED_DECLINED:
                lines.append(f"   declined: {step.step_id} "
                             f"({step.operator or 'no reason recorded'})")
        if result.unreached:
            lines.append("   never reached: " + ", ".join(result.unreached))
    return lines


# --------------------------------------------------------------------------
# The session record
# --------------------------------------------------------------------------

def format_record(result: ProcedureResult, *, dev: Any, paths: Paths,
                  console: TranscriptConsole | None = None,
                  argv: Sequence[str] | None = None) -> str:
    """The human-readable session log: asked, answered, sent, returned, stored.

    Deliberately fat, and deliberately plain text.  It is the only account of
    an operation that cannot be repeated to produce another one.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    head = [
        "caldmm-5700a-selfcal session record",
        "=" * 78,
        f"written            {datetime.now().astimezone().isoformat(' ', 'seconds')}",
        f"command            caldmm-5700a-selfcal {' '.join(argv)}",
        f"caldmm            {caldmm_version()}",
        *_identity_lines(dev),
        f"procedure          {result.procedure_id} -- {result.name}",
        f"mode               "
        f"{'DRY RUN (nothing written to NVRAM)' if result.dry_run else 'COMMITTING RUN'}",
        f"started / finished {_stamp(result.started_at)} / "
        f"{_stamp(result.finished_at)}",
    ]
    if result.operator:
        head.append(f"operator           {result.operator}")
    if result.notes:
        head.append(f"notes              {result.notes}")
    head.append(f"non-volatile       {result.nvram_state.value.upper()}")

    body = ["", _rule("What happened"), result.summary()]

    body += ["", _rule("Outcome")]
    body += format_outcome(result, paths)
    body += _uncommitted_note(result) if not result.committed else []

    if result.standards:
        body += ["", _rule("External standards, as entered")]
        body += [f"  {standard.describe()}" for standard in result.standards]
        body += ["  Every output this calibrator sources until its next "
                 "calibration inherits",
                 "  these uncertainties. A certificate that does not state "
                 "them is not defensible."]

    log = tuple(result.detail.get("log", ()))
    if log:
        body += ["", _rule("Step log"), *(f"  {line}" for line in log)]

    if console is not None and console.transcript:
        body += ["", _rule("Operator transcript"),
                 *(f"  {line}" for line in console.transcript)]

    body += ["", _rule("Files"), f"  session log   {paths.log}"]
    if result.before_report_text:
        body.append(f"  before report {paths.before}")
    if result.after_report_text:
        body.append(f"  after report  {paths.after}")
    if result.report_diff is not None:
        body.append(f"  diff CSV      {paths.diff_csv}")

    if result.report_diff is not None:
        body += ["", _rule(_diff_title(result)),
                 format_diff(result.report_diff, 40)]

    return "\n".join([*head, *body, ""])


def _guard(what: str, action: Callable[[], Any]) -> bool:
    """Run one step of the write-out; report a failure and carry on.

    Every file is independent, and the session log is the one that matters, so
    a full disk on the diff CSV must not be able to take it down with it.
    """
    try:
        action()
    except Exception as exc:                          # noqa: BLE001 - reported
        print(f"  !! {what} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False
    return True


@dataclass(frozen=True)
class RecordOutcome:
    """Where the audit trail actually ended up.

    Not a bool, because "the diff CSV failed" and "the session log is not where
    it was asked to be" are different sizes of problem and only the second one
    changes the exit status.
    """

    #: Files that really were written, in order.
    written: tuple[str, ...] = ()
    #: Where the session log went when it could not be written to its own path.
    #: Empty when it did not have to go anywhere else -- or when even the
    #: fallback failed, in which case :attr:`displaced` is still True and the
    #: terminal is the only copy.
    fallback: str = ""
    #: True when the session log is **not** at ``paths.log``.
    displaced: bool = False


def _fallback_record(text: str) -> str:
    """Last resort: put the session log anywhere at all, and say where.

    Reached only when the requested path could not be written despite
    :func:`_check_writable` having proved it could -- a disk that filled during
    the run, a network mount that went away.  A temporary file is not a good
    home for the record of an irreversible operation, but it is the difference
    between the operator copying a path and the operator re-typing an hour of
    calibration from a scrollback buffer.
    """
    try:
        handle_fd, path = tempfile.mkstemp(prefix="caldmm-5700a-selfcal-",
                                           suffix=".log")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:                            # pragma: no cover - disk
        print(f"  !! the fallback copy failed too "
              f"({exc.__class__.__name__}: {exc})", file=sys.stderr)
        return ""
    return path


def _write_record(result: ProcedureResult, *, dev: Any, paths: Paths,
                  console: TranscriptConsole | None = None,
                  argv: Sequence[str] | None = None) -> RecordOutcome:
    """Put the audit trail on disk.  Called the instant a result exists.

    Called from the normal path *and* from every exception path, including the
    interrupt.  An irreversible operation whose record was lost to a Ctrl-C is
    the failure this ordering exists to prevent, so nothing that could raise --
    printing a summary, rendering a diff -- comes first.

    Re-callable: a single-step ``CAL_STORE`` retry produces a second, later
    result for the same session and it overwrites the first.
    """
    written: list[str] = []

    def write_text(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        written.append(path)

    # Rendered before it is written so the fallback below has something to
    # write without re-running a formatter that has already been shown to work.
    # A formatter that raises must still leave a record: the summary is the
    # irreducible part of it.
    try:
        text = format_record(result, dev=dev, paths=paths, console=console,
                             argv=argv)
    except Exception as exc:                          # noqa: BLE001 - reported
        print(f"  !! the session record could not be formatted "
              f"({exc.__class__.__name__}: {exc}); writing the bare summary",
              file=sys.stderr)
        text = result.summary()
    displaced = not _guard(f"writing {paths.log}",
                           lambda: write_text(paths.log, text))
    fallback = _fallback_record(text) if displaced else ""

    if result.before_report_text:
        _guard(f"writing {paths.before}",
               lambda: write_text(paths.before, result.before_report_text))
    if result.after_report_text:
        _guard(f"writing {paths.after}",
               lambda: write_text(paths.after, result.after_report_text))
    if result.before_report is not None and result.after_report is not None:
        def write_diff() -> None:
            _write_csv(paths.diff_csv, DIFF_CSV_COLUMNS,
                       diff_csv_rows(result.before_report,
                                     result.after_report))
            written.append(paths.diff_csv)
        _guard(f"writing {paths.diff_csv}", write_diff)
    return RecordOutcome(written=tuple(written), fallback=fallback,
                         displaced=displaced)


# --------------------------------------------------------------------------
# The CAL_STORE single-step retry
# --------------------------------------------------------------------------

#: Why the retry exists, in the words the operator needs at that moment.
_RETRY_BRIEF = (
    "The constants CAL_ADJ computed are still live in volatile memory, so "
    "CAL_STORE on its",
    "own commits exactly the calibration that was already performed -- the "
    "artefacts do",
    "not have to be re-connected and the four CAL_REF steps do not have to be "
    "re-run.",
    "That holds only until the calibrator is powered off.",
    "",
    "Set the rear-panel CALIBRATION switch to ENABLE before answering.",
)


def _offer_store_retry(procedure: Any, pctx: ProcedureContext,
                       console: TranscriptConsole,
                       result: ProcedureResult) -> ProcedureResult | None:
    """Offer ``CAL_STORE`` alone after a failure that provably stored nothing.

    Only offered when the failure is one that **proves nothing was written** --
    in practice fault 845, the rear CALIBRATION switch left in NORMAL, which is
    the case the driver's own error message promises this recovery for.  When
    the NVRAM state is *unknown* the retry is deliberately not offered: the
    right next action there is to read ``CAL_DATE?``, not to send another
    irreversible command on top of one whose outcome nobody knows.

    Returns the re-finalized result when a retry was attempted, else ``None``.
    """
    if not pctx.commit or result.nvram_uncertain:
        return None
    step = _step(result, STORE_STEP_ID)
    if (step is None or step.outcome is not StepOutcome.FAILED
            or step.nvram is not NvramState.UNTOUCHED):
        return None
    if not pctx.completed(ADJUST_STEP_ID):
        return None

    attempted = False
    try:
        for attempt in range(1, STORE_RETRY_ATTEMPTS + 1):
            console.show(
                f"── CAL_STORE can be retried on its own "
                f"({attempt}/{STORE_RETRY_ATTEMPTS}) ──", *_RETRY_BRIEF)
            if not console.confirm_ok(
                    "retrying CAL_STORE now",
                    hint="[Y = retry the commit / anything else = give up]"):
                break
            if not console.confirm_explicit(
                    "committing the new calibration constants to the "
                    "calibrator's NON-VOLATILE memory (CAL_STORE), overwriting "
                    "the existing ones irreversibly",
                    hint='[type "yes" to commit / anything else = give up]'):
                break
            attempted = True
            try:
                procedure.store_step(pctx)
            except KeyboardInterrupt:
                # The command was already on its way out when the Ctrl-C
                # landed, so this is the unknown state, not a cancellation:
                # recording it as "the operator stopped it" would claim to
                # know something nobody knows.
                _record_failed_retry(
                    pctx, KeyboardInterrupt(
                        "interrupted while CAL_STORE was in flight"))
                pctx.interrupted = True
                break
            except Exception as exc:                  # noqa: BLE001 - recorded
                _record_failed_retry(pctx, exc)
                print(f"\n  CAL_STORE was refused again:\n  {exc}\n")
                continue
            pctx.error = ""        # the retry is the answer to what failed
            console.show("── stored ──",
                         "CAL_STORE succeeded. Set the rear CALIBRATION switch "
                         "back to NORMAL.")
            break
    except KeyboardInterrupt:
        # At a prompt, before anything was sent: the same as declining, and
        # the run's own verdict already says nothing was committed.
        print("\n  retry abandoned; nothing further was sent.")

    if not attempted:
        return None
    pctx.finished_at = None        # finalize re-stamps it
    return procedure.finalize(pctx)


def _record_failed_retry(pctx: ProcedureContext, exc: BaseException) -> None:
    """Put a failed retry into the audit trail.

    ``store_step`` records only on success, so without this a retry that
    reached the wire and could not be read back would leave no trace at all --
    the one thing the record exists to make impossible.

    :func:`~caldmm.cal.procedure.classify_failure` decides *both* of the
    questions that answer needs -- whether the command went out, and what is
    known about non-volatile memory -- and it is public precisely so this
    caller does not copy the runner's tuples and drift from them.  It is the
    same call ``run_procedure`` makes for a step that raised, so a
    ``store-retry`` refused by fault 845 is filed as a command that **was** on
    the wire, which is what happened.
    """
    now = datetime.now()
    nvram, sent, held = classify_failure(exc, ("CAL_STORE;*WAI",),
                                         writes_nvram=True)
    pctx.record(StepResult(
        step_id="store-retry", outcome=StepOutcome.FAILED,
        started_at=now, finished_at=now, writes_nvram=True,
        nvram=nvram, commands=sent, skipped_commands=held,
        error=f"{exc.__class__.__name__}: {exc}"))
    if nvram is NvramState.UNKNOWN:
        pctx.warn(
            "store-retry: CAL_STORE;*WAI was sent and its outcome could not "
            "be read back. Non-volatile memory may already have been "
            "written -- confirm with CAL_DATE? / CAL_RPT? before trusting the "
            "instrument.")


# --------------------------------------------------------------------------
# Mode: --compare, entirely offline
# --------------------------------------------------------------------------

def _load_report(path: str) -> CalReport:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise SelfCalError(f"--compare: {path}: {exc}") from exc
    reports = parse_reports(text)
    if not reports:
        raise SelfCalError(
            f"--compare: {path} holds no 5700A calibration report.  A capture "
            f"starts with a 'FLUKE CORPORATION S/N ... OUTPUT CALIBRATION' "
            f"header line; --report CAL writes one.")
    if len(reports) > 1:
        print(f"  {path}: {_plural(len(reports), 'report')} in the file; "
              f"comparing the first")
    return reports[0]


def _run_compare(args: argparse.Namespace) -> int:
    """Diff two saved captures.  No instrument, no changes, no record."""
    before = _load_report(args.compare[0])
    after = _load_report(args.compare[1])
    print()
    print(_rule("Compare"))
    print(f"  before  {args.compare[0]}  ({before.model} S/N {before.serial}, "
          f"{before.kind.value})")
    print(f"  after   {args.compare[1]}  ({after.model} S/N {after.serial}, "
          f"{after.kind.value})")
    if before.serial and after.serial and before.serial != after.serial:
        print(f"  !! these are two DIFFERENT instruments "
              f"(S/N {before.serial} and {after.serial}); the comparison "
              f"below is meaningless")
    print()
    print(format_diff(diff_reports(before, after), 40))

    if args.output:
        paths = output_paths(args.output, "compare")
        _prepare_outputs((paths.diff_csv,), args.force)
        if _guard(f"writing {paths.diff_csv}",
                  lambda: _write_csv(paths.diff_csv, DIFF_CSV_COLUMNS,
                                     diff_csv_rows(before, after))):
            print(f"\n  diff CSV  {paths.diff_csv}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Mode: --report, a capture that changes nothing
# --------------------------------------------------------------------------

def _run_report(dev: Any, group: str, paths: Paths) -> int:
    """Capture ``CAL_RPT?`` verbatim.  Sends no calibration command.

    Not the same claim as "changes nothing": opening the session runs the guide
    §14.1 sequence, which includes ``*RST`` and ``STBY``, so **a live output is
    dropped**.  No calibration constant is read back into, nothing is written,
    and no ``CAL_`` command other than the query goes out -- but an operator
    with something energized deserves to be told which of those two statements
    is the true one.

    The raw text is written before it is parsed, and kept even when it does not
    parse: a capture from a sick instrument is exactly the one worth having,
    and the parser is not the arbiter of whether the bytes were worth keeping.
    """
    print()
    print(_rule(f"CAL_RPT? {group}"))
    for line in _identity_lines(dev):
        print(f"  {line}")
    text = dev.cal_rpt(group)

    if _guard(f"writing {paths.report}",
              lambda: _write_file(paths.report, text)):
        print(f"  captured      {paths.report}")
    reports = parse_reports(text)
    if reports and _guard(
            f"writing {paths.report_csv}",
            lambda: _write_csv(paths.report_csv, CSV_COLUMNS,
                               to_csv_rows(reports[0]))):
        print(f"  shift table   {paths.report_csv}")

    print()
    if not reports:
        print("  the capture did not parse as a calibration report; the raw "
              "text was kept verbatim:")
        print(text)
        return EXIT_FAIL
    print(format_summary(reports[0]))
    print()
    print(f"  --compare {paths.report} <another capture> diffs two of these.")
    return EXIT_OK


def _write_file(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


# --------------------------------------------------------------------------
# Mode: a procedure
# --------------------------------------------------------------------------

#: What has to be on the bench before an artifact calibration starts, and in
#: what order it gets connected.  ``run_procedure`` prints each step's own
#: instructions as it reaches it; this is the version the operator reads
#: *before* committing an hour to the sequence.
def _artifact_briefing(commit: bool) -> list[str]:
    lines = [
        "Three external standards are needed, connected one at a time, in "
        "this order:",
        "",
        "  1. +10 V   10 V dc reference to OUTPUT HI/LO "
        "(standard HI → HI, LO → LO)",
        "  2. −10 V   the SAME reference with the LEADS REVERSED "
        "(HI → LO, LO → HI)",
        "  3. 10 kΩ   10 kΩ standard, 4-wire across OUTPUT and SENSE",
        "  4.  1 Ω    1 Ω standard, 4-wire on its own potential terminals",
        "",
        "then CAL_ADJ computes the new constants from all four.",
        "",
        "The order is mandatory (guide §14.6). Out of order the calibrator "
        "answers fault 852",
        "and the whole sequence has to restart from CAL_TEMP -- so this "
        "program refuses",
        "an out-of-order step before it reaches the wire.",
        "",
    ]
    for spec in ARTIFACT_STANDARDS:
        lines.append(f"  {spec.describe()}")
    lines += [
        "",
        "You will be asked for each standard's CERTIFIED VALUE and its "
        "UNCERTAINTY, from",
        "that artefact's own calibration certificate. The uncertainty is not "
        "optional: it",
        "propagates into everything this calibrator sources until its next "
        "calibration.",
        "",
        "Budget an hour. Each CAL_REF and the CAL_ADJ take minutes and CANNOT "
        "be aborted",
        "remotely -- the front-panel STBY key and the line switch are the only "
        "stops.",
    ]
    if commit:
        lines += [
            "",
            "COMMITTING RUN: CAL_STORE will be offered at the end. It needs "
            "the rear-panel",
            "CALIBRATION switch in ENABLE and a typed confirmation, and it "
            "overwrites the",
            "existing calibration constants IRREVERSIBLY.",
        ]
    else:
        lines += [
            "",
            "DRY RUN: CAL_TEMP, all four CAL_REF steps and CAL_ADJ WILL "
            "really run -- only",
            "CAL_STORE is held back. The calibrator ends up sourcing from the "
            "new constants",
            "in volatile memory until it is powered off. Add --store to "
            "commit them.",
        ]
    return lines


#: What "mode" means in each procedure.  A single "DRY RUN / COMMITTING RUN"
#: line would say the wrong thing twice: a dry run of the artifact cal still
#: runs CAL_ADJ, and a calibration check has nothing to commit in any mode, so
#: calling it a dry run implies a committing one exists.
#:
#: Keyed on the mode alone.  ``--artifact`` is the only one whose ``commit`` an
#: operator chooses -- ``--zeros`` always commits and ``--check`` never can --
#: so it is the only one that needs two lines, and :func:`_mode_line` special-
#: cases exactly that.
_MODE_LINES: dict[str, str] = {
    "artifact":
        "COMMITTING RUN -- CAL_STORE will be offered at the end",
    "zeros":
        "COMMITTING RUN -- CAL_ZERO writes the new dc zeros",
    "check":
        "CAL_CHK changes no calibration constant; nothing can be committed",
}

_ARTIFACT_DRY_RUN_LINE = ("DRY RUN -- CAL_TEMP, the CAL_REFs and CAL_ADJ really "
                          "run; CAL_STORE is held back")


def _mode_line(mode: str, commit: bool) -> str:
    if mode == "artifact" and not commit:
        return _ARTIFACT_DRY_RUN_LINE
    return _MODE_LINES[mode]


def _parse_ambient(args: argparse.Namespace) -> float | None:
    """``--ambient`` as a number, or ``None``.

    Called from :func:`run` **before the instrument is opened**, so a typo
    outside the 15-35 °C band calibration is specified over costs nothing, and
    again from :func:`_make_context`, which is the one that feeds it to
    ``CAL_TEMP``.  Idempotent, so the second call is free.
    """
    if args.ambient is None:
        return None
    try:
        return parse_temperature(str(args.ambient))
    except StandardValueError as exc:
        raise SelfCalError(f"--ambient: {exc}") from exc


def _make_context(dev: Any, console: TranscriptConsole,
                  args: argparse.Namespace, *, commit: bool
                  ) -> ProcedureContext:
    ambient = _parse_ambient(args)
    return ProcedureContext(
        device=dev, console=console, commit=commit, ambient_c=ambient,
        operator=args.operator or "", notes=args.notes or "")


def _finalize_after_exception(procedure: Any, pctx: ProcedureContext,
                              error: str, *, interrupted: bool
                              ) -> ProcedureResult | None:
    """Build the record from the context after the runner did not return one.

    Through the procedure's own ``finalize`` -- the identical call
    ``run_procedure`` makes -- so the app never fabricates a result out of a
    state the framework cannot produce.

    *interrupted* is the operator's Ctrl-C, not any other failure: recording a
    refused certificate entry as an interrupt would put a wrong word in the
    only account of the session there is.
    """
    if interrupted:
        pctx.interrupted = True
    if not pctx.error:
        pctx.error = error
    try:
        return procedure.finalize(pctx)
    except Exception as exc:                          # noqa: BLE001 - reported
        print(f"  !! the run record could not be assembled: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _emergency_text(pctx: ProcedureContext, paths: Paths) -> str:
    """The last-resort record: the raw step log, when ``finalize`` itself broke."""
    return "\n".join([
        "caldmm-5700a-selfcal EMERGENCY session record",
        "the run result could not be assembled; this is the raw step log",
        f"written  {datetime.now().astimezone().isoformat(' ', 'seconds')}",
        f"error    {pctx.error or 'unknown'}",
        "",
        *pctx.log,
        "",
        *(f"warning: {warning}" for warning in pctx.warnings),
        "",
    ])


def _run_procedure_mode(dev: Any, args: argparse.Namespace, mode: str,
                        console: TranscriptConsole, paths: Paths) -> int:
    """Drive one registered procedure, and make sure it leaves a record."""
    procedure_id = MODES[mode]
    procedure = get_procedure(MODEL_ID, procedure_id)

    # CAL_ZERO writes its own results, so it needs commit=True to do anything
    # at all; it needs no external standard and no rear-panel switch, and a
    # calibrator whose zeros are overdue is out of specification. Defaulting it
    # to a run that does nothing would make the flag a trap. It still asks for
    # a typed confirmation, because it still writes.
    # CAL_CHK changes no calibration constant, so it never commits.
    commit = {"artifact": bool(args.store), "zeros": True, "check": False}[mode]

    print()
    print(_rule(procedure.name))
    for line in _identity_lines(dev):
        print(f"  {line}")
    print(f"  procedure          {procedure_id}")
    print(f"  mode               {_mode_line(mode, commit)}")
    print(f"  session log        {paths.log}")
    startup_faults = list(getattr(dev, "startup_faults", ()) or ())
    if startup_faults:
        print(f"  !! {_plural(len(startup_faults), 'fault')} were queued at "
              f"startup and have been drained:")
        for code, text in startup_faults:
            print(f"       {code}: {text}")

    if mode == "artifact":
        console.show("── before you start ──", *_artifact_briefing(commit))

    pctx = _make_context(dev, console, args, commit=commit)

    # Everything from here on has one obligation: a result exists, and it is
    # written to disk before anything else happens to it.
    try:
        result = run_procedure(procedure, pctx)
    except PreflightError as exc:
        print()
        print(_rule("Refused"))
        print("  the calibrator is not in a state this procedure will start "
              "from, and")
        print("  NOTHING WAS SENT:")
        for problem in exc.problems:
            print(f"    - {problem}")
        print("  --force does not override these: they are the conditions the "
              "calibration's")
        print("  own specifications assume. Fix the cause (warm it up, run "
              "--zeros, set the")
        print("  output to 0 V and press STBY) and start again.")
        return EXIT_USAGE
    except (KeyboardInterrupt, SystemExit):
        result = _finalize_after_exception(
            procedure, pctx, "interrupted before the runner returned",
            interrupted=True)
        print()
        print(_rule("Interrupted"))
        if result is None:
            emergency = _emergency_text(pctx, paths)
            if not _guard(f"writing {paths.log}",
                          lambda: _write_file(paths.log, emergency)):
                where = _fallback_record(emergency)
                print(f"  !! the raw step log went to {where or 'nowhere'}, "
                      f"not {paths.log}")
                return EXIT_NO_RECORD
            print(f"  the raw step log was written to {paths.log}")
            return EXIT_ABORTED
        record = _report(result, dev=dev, paths=paths, console=console)
        _print_verdict(result, paths)
        return _with_record(EXIT_ABORTED, record)
    except ProcedureError as exc:
        # A standards entry that could not be made sane, an order violation
        # raised outside a step -- refused before anything was sent, but the
        # context may already hold completed steps, so it still gets a record.
        result = _finalize_after_exception(procedure, pctx, str(exc),
                                           interrupted=False)
        print()
        print(_rule("Refused"))
        print(f"  {exc}")
        if result is None:
            return EXIT_USAGE
        record = _report(result, dev=dev, paths=paths, console=console)
        _print_verdict(result, paths)
        return _with_record(EXIT_USAGE, record)

    # The record first, unconditionally, before anything that could raise.
    record = _report(result, dev=dev, paths=paths, console=console)

    retried = _offer_store_retry(procedure, pctx, console, result)
    if retried is not None:
        print()
        print(_rule("After the CAL_STORE retry"))
        record = _report(retried, dev=dev, paths=paths, console=console)
        result = retried

    _print_verdict(result, paths)
    return _with_record(exit_status(result), record)


def _with_record(status: int, record: RecordOutcome) -> int:
    """:data:`EXIT_NO_RECORD` wins over whatever the run itself concluded.

    Including over :data:`EXIT_UNKNOWN`.  Both say "go and look" -- but the
    unknown NVRAM state can be resolved at any time with ``CAL_DATE?``, while
    the account of what was done is gone the moment the terminal is closed.
    The banner for the other state is still on the screen either way.
    """
    return EXIT_NO_RECORD if record.displaced else status


def _report(result: ProcedureResult, *, dev: Any, paths: Paths,
            console: TranscriptConsole) -> RecordOutcome:
    """Write the record, then print it.  **In that order, always.**

    The verdict is deliberately *not* printed here -- see
    :func:`_print_verdict`.  A ``CAL_STORE`` retry can follow this call and
    change the answer, and printing "NOT COMMITTED, power-cycle it" and then
    committing thirty seconds later would leave two contradictory verdicts on
    the operator's screen with nothing to say which one stands.
    """
    record = _write_record(result, dev=dev, paths=paths, console=console)

    print()
    print(_rule("Result"))
    print(result.summary())
    if result.report_diff is not None:
        print()
        print(_rule(_diff_title(result)))
        print(format_diff(result.report_diff))

    print()
    print(_rule("Record"))
    for path in record.written:
        print(f"  {path}")
    if record.displaced:
        print(f"  !! THE SESSION LOG COULD NOT BE WRITTEN TO {paths.log}.")
        if record.fallback:
            print(f"     It was saved to {record.fallback} instead. COPY IT "
                  f"SOMEWHERE PERMANENT NOW --")
            print(f"     that is a temporary file and the system may delete "
                  f"it.")
        else:
            print("     It could not be saved anywhere. THIS TERMINAL IS THE "
                  "ONLY COPY of what")
            print("     happened -- copy the text above by hand before closing "
                  "it.")
        print(f"     This session exits {EXIT_NO_RECORD}, not 0: the record is "
              f"not where it was asked to be.")
    return record


def _print_verdict(result: ProcedureResult, paths: Paths) -> None:
    """The last thing on the screen, once, from the run's final result."""
    print()
    for line in format_outcome(result, paths):
        print(line)
    if not result.committed:
        for line in _uncommitted_note(result):
            print(line)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caldmm-5700a-selfcal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="The Fluke 5700A/5720A's own calibration: dc zeros, "
                    "calibration check, and a guided artifact calibration "
                    "against external standards.",
        epilog="""\
exactly one mode is required.

  --zeros      CAL_ZERO, about 2.5 min.  Needs nothing connected and no rear
               switch, but it does write the zeros, so it asks for a typed
               confirmation.  Due every 30 days (fault 1303).
  --check      CAL_CHK against the internal standards.  Changes no calibration
               constant; it does overwrite the CHECK report, which is captured
               first.
  --artifact   the full main-output artifact calibration.  **A DRY RUN unless
               --store**: CAL_TEMP, four CAL_REF steps and CAL_ADJ really run
               either way, and the calibrator is left sourcing from the new,
               uncommitted constants until it is power-cycled.
  --report     capture CAL_RPT? to a file.  Sends no calibration command --
               but opening the session does *RST/STBY, so a live output is
               dropped.
  --compare    diff two saved captures.  Nothing is contacted (the driver
               module is still imported, so linux-gpib must be installed).

exit status: 0 the sequence ran, 1 it did not and nothing was committed,
2 usage or a pre-flight refusal, 3 interrupted, 4 THE NON-VOLATILE STATE IS
UNKNOWN and has to be checked by hand with CAL_DATE? / CAL_RPT?, 5 THE SESSION
LOG IS NOT WHERE IT WAS ASKED TO BE -- read the console output before closing
the window.  5 takes precedence over the others.""")

    modes = parser.add_argument_group("mode (exactly one)")
    modes.add_argument("--zeros", action="store_true",
                       help="run the dc zeros calibration (CAL_ZERO)")
    modes.add_argument("--check", action="store_true",
                       help="run the calibration check (CAL_CHK)")
    modes.add_argument("--artifact", action="store_true",
                       help="run the artifact calibration; a dry run unless "
                            "--store")
    modes.add_argument("--report", nargs="?", const="CAL", metavar="GROUP",
                       help=f"capture CAL_RPT? GROUP "
                            f"({'/'.join(REPORT_GROUPS)}, default CAL).  Sends "
                            f"no calibration command; opening the session does "
                            f"*RST/STBY, so a live output is dropped")
    modes.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                       help="diff two saved captures; no instrument is "
                            "contacted")

    commit = parser.add_argument_group("committing")
    commit.add_argument("--store", action="store_true",
                        help="allow CAL_STORE, which overwrites the "
                             "calibration constants in NON-VOLATILE memory "
                             "irreversibly.  It still needs the rear-panel "
                             "CALIBRATION switch in ENABLE and a typed "
                             "confirmation")

    instrument = parser.add_argument_group("instrument")
    link = instrument.add_mutually_exclusive_group()
    # default=None, resolved in main(); see the same note in
    # fluke5700a_console.build_parser() -- argparse before 3.12 lets
    # `--address 4 --port DEV` through when 4 is the declared default.
    link.add_argument("--address", type=int, default=None,
                      help=f"GPIB address (default "
                           f"{f57.DEFAULT_GPIB_ADDRESS})")
    link.add_argument("--port", metavar="DEV",
                      help="use the rear-panel RS-232C port at this device "
                           "path (e.g. /dev/ttyUSB0 or COM3) instead of GPIB")
    instrument.add_argument("--bus", type=int, default=f57.DEFAULT_GPIB_BUS,
                            help="GPIB bus/board index (default %(default)s); "
                                 "GPIB only")
    instrument.add_argument("--baud", type=int,
                            default=f57.DEFAULT_SERIAL_BAUD,
                            choices=f57.SERIAL_BAUD_RATES,
                            help="serial baud rate (default %(default)s); "
                                 "only with --port")
    instrument.add_argument("--timeout", type=float,
                            default=f57.DEFAULT_TIMEOUT_SEC,
                            help="session timeout in seconds "
                                 "(default %(default)s)")

    conditions = parser.add_argument_group("conditions and provenance")
    conditions.add_argument("--ambient", default=None, metavar="CELSIUS",
                            help="ambient temperature for CAL_TEMP, e.g. 23.4. "
                                 "Asked for interactively when omitted; "
                                 "without it the calibrator silently assumes "
                                 "23.0 C")
    conditions.add_argument("--operator", default=None, metavar="NAME",
                            help="who ran it, recorded in the session log")
    conditions.add_argument("--notes", default=None, metavar="TEXT",
                            help="free text recorded in the session log")

    output = parser.add_argument_group("output")
    output.add_argument("-o", "--output", default=None, metavar="BASENAME",
                        help="basename for the record: BASENAME.log, "
                             "BASENAME-before.txt, BASENAME-after.txt, "
                             "BASENAME-diff.csv.  Defaults to "
                             "5700a-<procedure>-<timestamp>")
    output.add_argument("--force", action="store_true",
                        help="overwrite existing output files, and skip the "
                             "app's own refusals.  It does NOT override the "
                             "procedure's pre-flight (warm-up, dc-zero "
                             "interval, a live output at or above 22 V, the "
                             "model check) -- those are the conditions the "
                             "specifications assume")
    return parser


def _select_mode(args: argparse.Namespace) -> str:
    """The one mode this invocation asks for.  Raises on none or several."""
    chosen = [name for name in ("zeros", "check", "artifact")
              if getattr(args, name)]
    if args.report is not None:
        chosen.append("report")
    if args.compare is not None:
        chosen.append("compare")
    if not chosen:
        raise SelfCalError(
            "one of --zeros, --check, --artifact, --report or --compare is "
            "required.  --artifact is a dry run unless --store; --help "
            "describes what each one sends.")
    if len(chosen) > 1:
        raise SelfCalError(
            f"these modes are mutually exclusive, and "
            f"{', '.join('--' + name for name in chosen)} were all given.  "
            f"An adjustment sequence is not restartable in the middle, so "
            f"they are never chained in one invocation.")
    mode = chosen[0]

    if args.store and mode != "artifact":
        # Not fatal: it asks for something the mode already does or can never
        # do, and refusing the whole run over it would be pedantry at the
        # bench.
        print(f"  note: --store applies to --artifact.  "
              + ("--zeros always commits (CAL_ZERO writes the zeros; there is "
                 "nothing to hold back)."
                 if mode == "zeros" else
                 "--check writes no calibration constant, so there is nothing "
                 "to commit."))
    if args.report is not None and str(args.report).upper() not in REPORT_GROUPS:
        raise SelfCalError(
            f"--report {args.report!r}: expected one of "
            f"{', '.join(REPORT_GROUPS)}")
    return mode


def run(args: argparse.Namespace, *, dev: Any = None) -> int:
    """Execute one session.  Returns a process exit status.

    *dev* is injected by the tests; when it is ``None`` this opens the
    calibrator itself and owns closing it.
    """
    try:
        mode = _select_mode(args)
        _parse_ambient(args)       # refuse a bad one before opening anything
    except SelfCalError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if mode == "compare":
        try:
            return _run_compare(args)
        except SelfCalError as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
            return EXIT_USAGE

    group = str(args.report).upper() if mode == "report" else ""
    what = f"report-{group.lower()}" if mode == "report" else MODES[mode]
    paths = output_paths(args.output, what)
    try:
        # Before the instrument is opened: a run refused for a name clash, or
        # for a path it could not have written its record to, must not have
        # touched anything.
        _prepare_outputs(paths.for_report() if mode == "report"
                         else paths.for_procedure(), args.force)
    except SelfCalError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return EXIT_USAGE

    owns_device = dev is None
    if owns_device:
        port = getattr(args, "port", None)
        address = (f57.DEFAULT_GPIB_ADDRESS if args.address is None
                   else args.address)
        where = (f"serial {port} at {args.baud} baud" if port
                 else f"GPIB {args.bus}:{address}")
        try:
            if port:
                dev = Fluke5700A(serial_port=port, serial_baud=args.baud,
                                 timeout=args.timeout)
            else:
                dev = Fluke5700A(address, args.bus, timeout=args.timeout)
        except Exception as exc:                      # noqa: BLE001 - reported
            print(f"cannot open the calibrator at {where}: {exc}",
                  file=sys.stderr)
            return EXIT_USAGE

    console = TranscriptConsole(panel=getattr(dev, "display_message", None))

    closed = {"done": False}

    def cleanup() -> None:
        if closed["done"]:
            return
        closed["done"] = True
        # Each step wrapped, and standby first: a meter that has dropped off
        # the bus must not be able to stop the output coming off.  Note that
        # while a long-term command runs this handler is not installed --
        # run_procedure's deferred_sigint owns SIGINT there, because STBY
        # during CAL_ADJ is rejected with fault 2002 and there is no remote
        # abort to clean up with.
        try:
            dev.standby()
        except Exception:                             # noqa: BLE001 - cleanup
            pass
        if owns_device:
            try:
                dev.close()
            except Exception:                         # noqa: BLE001 - cleanup
                pass

    setup_graceful_shutdown(cleanup)

    try:
        if mode == "report":
            return _run_report(dev, group, paths)
        return _run_procedure_mode(dev, args, mode, console, paths)
    except SelfCalError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (KeyboardInterrupt, SystemExit):
        # Only reachable outside _run_procedure_mode's own handling -- a
        # --report capture, or the open above.
        print("\ninterrupted.", file=sys.stderr)
        return EXIT_ABORTED
    finally:
        cleanup()


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

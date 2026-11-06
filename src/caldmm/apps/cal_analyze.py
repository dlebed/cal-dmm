"""Offline PDF and markdown reports from a verification result CSV.

Fully offline: no instrument, no database, no network.  It reads one result
file written by :mod:`caldmm.cal.result` and writes a multi-page PDF next to
it plus a markdown report that can be pasted into a unit log.

A second mode, ``--adjust-log``, renders a **calibration cycle**: the
adjustment session record verbatim plus -- when the paired as-found/as-left
verification CSVs are given, or the as-left names its counterpart -- the
before/after comparison, into a PDF and markdown next to the log.  A log
alone yields a text-only report that says so; this module does not invent
plots out of a file that carries no measurements.

**This app is the proof of the subsystem's premise.**  ``cal/result.py`` claims
that a calibration report must be re-derivable from its own data file; this
module is what re-derives it.  Every uncertainty budget printed here is
reconstructed component by component from the ``Budget`` cell of the row it
belongs to, and ``u_c``, ``nu_eff``, ``k`` and ``U`` are recomputed from those
components -- nothing derived is read back out of the file.  If the report and
the file ever disagreed, the file would not be evidence.

Two things it refuses to blur
-----------------------------

**The two reasons a point is INDETERMINATE are different findings.**  "The TUR
floor blocked it" says the *reference* is not good enough for that measurement
and no amount of re-running will change it; "the temperature gate blocked it"
says the specification's stated conditions were not shown to hold, which is a
procedural fix that costs nothing.  A third case, "no uncertainty budget at
all", is neither.  :func:`indeterminate_causes` keeps them apart, and the
causes are reported as overlapping counts rather than a partition because a
point can legitimately be blocked by two of them at once.

**A reference no better than the DUT is a headline, not a footnote.**  Plan
points carry a note like ``spec ratio 0.00403 (hp3458a 4.03e-11 / fluke5700a
1e-08 A)`` -- the DUT's limit over the reference's.  Below 1.0 the comparison
is mostly measuring the calibrator rather than the meter, and for the shipped
3458A-against-5700A pairing that is most of the run.  The warning goes at the
top of the report, before the tables, so no reader has to infer it from a wall
of INDETERMINATE rows.

Exit status is about the *report*, not about the verdicts
---------------------------------------------------------

``0`` means the report was written; ``2`` means a file named on the command
line does not exist.  A file whose every row is a FAIL still exits ``0``, and
that is **not** the same contract ``apps.cal_verify`` publishes, where ``1``
means "the run finished and something FAILed".

The two answer different questions, and the divergence is deliberate.  The
runner is the thing that performed the comparison, so "did it find a fault?"
is its result to report.  This module renders a file somebody else produced,
and it is run repeatedly over the same file -- with ``--compare``, with
``--full-budgets``, from a report-regeneration script -- where a non-zero
status would mean "the report failed" to every wrapper that checks one.  The
verdict counts are on the first line of stdout and in the report; the exit
status says whether there is a report.  See :func:`run`.

Reading the file
----------------

``lib.csv_io.load_csv`` **cannot read a result file** -- it is pandas without a
``comment=`` argument, so the leading ``#`` provenance block parses as data and
raises ``ParserError``.  :func:`caldmm.cal.result.load_results` is the reader,
and it returns the :class:`~caldmm.cal.result.RunHeader` alongside the rows.

Faults
------

Result rows have no fault column: a fault belongs to the *run*, not to a point,
and a per-point column would repeat one queue across 194 rows while implying an
attribution the instrument never gives.  A runner records the queue as a
``# key=value`` comment::

    # faults=3111,4021

**Bare codes**, decoded here from ``cal/data/fluke5700a_faults.yaml`` -- never
from the instrument, because a unit sick enough to be worth diagnosing is
exactly the unit whose ``EXPLAIN?`` cannot be trusted, and bench-side analysis
often runs with nothing attached.  Recorded text, if it must be kept, goes in a
**JSON list** (``faults=[[3122, "A14/A15: HV AC 1KHZ, Preamp Fault (lo)"]]``):
21 of the 191 real fault messages contain a comma, so a comma-separated
``code: text`` form truncates them silently.

``ResultWriter`` writes the provenance block once, when the file is opened, so
the header can only carry the **pre-run** queue.  ``load_results`` collects
``#`` lines from anywhere in the file, so a fault raised at point 150 is
recorded by writing the key a second time as a trailing comment at close; this
module reads whichever ``faults=`` line the loader kept.

Faults are grouped by the assembly their text names (``3111: A15: HV +DC High
Voltage Output Fault`` -> ``A15``), because several codes pointing at one
assembly is one repair, not several -- and a code naming two (``A14/A15``, 23
of the 191, the whole HV preamp block) is listed under both.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Iterable, Mapping, Sequence

from caldmm.cal.calreport import fault_assemblies, strip_assembly_prefix
from caldmm.cal.result import (
    LIMIT_DISAGREEMENT_FACTOR,
    TEMPERATURE_FLAGS,
    ResultRow,
    RunHeader,
    RunSummary,
    load_results,
    summarize_run,
)
from caldmm.cal.plan import manufacturer_test_for, manufacturer_tests
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.specs import SpecError, load_model
from caldmm.cal.uncertainty import format_budget
from caldmm.cal.verdict import (
    FLAG_AMBIENT_UNKNOWN,
    FLAG_CONDITIONS_NOT_MET,
    FLAG_EMPTY_BUDGET,
    FLAG_NO_TEMPERATURE_WINDOW,
    FLAG_TEMPERATURE_OUT_OF_WINDOW,
    FLAG_TUR_BELOW_FLOOR,
    FLAG_ZERO_UNCERTAINTY,
    TemperatureStatus,
    Verdict,
    VerdictError,
    gates_for,
)
from caldmm.lib.formatting import (
    duration,
    fmt_g,
    is_blank,
    plural,
    si_format,
    stamp,
)

__all__ = [
    "SPEC_RATIO_WARN",
    "FAULTS_KEY",
    "Fault",
    "SpecRatios",
    "PointDelta",
    "spec_ratio",
    "spec_ratio_summary",
    "indeterminate_causes",
    "temperature_breakdown",
    "policy_of",
    "dominant_contribution",
    "load_fault_table",
    "parse_faults",
    "group_faults",
    "point_key",
    "compare_runs",
    "find_counterpart",
    "COMPARE_CSV_COLUMNS",
    "compare_csv_rows",
    "compare_diff_path",
    "write_compare_csv",
    "format_markdown",
    "render_pdf",
    "output_paths",
    "adjust_output_paths",
    "format_adjust_markdown",
    "render_adjust_pdf",
    "build_parser",
    "run",
    "main",
]

#: Spec ratio (DUT limit / reference limit) at or below which the reference is
#: not meaningfully better than the instrument being verified.  1.0 is the
#: point where the two specifications are equal; a calibration convention would
#: want 4.
SPEC_RATIO_WARN = 1.0

#: ``(figsize, characters per line, lines per page)`` for a PDF text page, at
#: the 6.5 pt monospace used below.  The per-point table is 150 characters wide
#: and is unreadable folded, so it goes on landscape pages; prose and budgets
#: stay portrait.
_PDF_PORTRAIT = ((8.27, 11.69), 130, 78)
_PDF_LANDSCAPE = ((11.69, 8.27), 190, 54)

_VERDICT_ORDER = (Verdict.PASS, Verdict.FAIL, Verdict.INDETERMINATE,
                  Verdict.NO_SPEC)

_VERDICT_COLOURS = {
    Verdict.PASS: "tab:green",
    Verdict.FAIL: "tab:red",
    Verdict.INDETERMINATE: "tab:orange",
    Verdict.NO_SPEC: "tab:gray",
}

#: ``spec ratio 0.00403 (hp3458a 4.03e-11 / fluke5700a 1e-08 A)``
_SPEC_RATIO_RE = re.compile(r"spec\s+ratio\s+([-+0-9.eE]+)")

#: The one header key faults are recorded under.  See :func:`parse_faults`.
FAULTS_KEY = "faults"


# --------------------------------------------------------------------------
# Small formatting helpers
# --------------------------------------------------------------------------

#: Shared with ``apps.cal_verify`` through ``lib.formatting`` -- see the note
#: there.  Kept under their local names so every call site in this module reads
#: as it always has.
_is_none = is_blank
_g = fmt_g
_plural = plural
_duration = duration
_stamp = stamp


def _q(value, unit: str, digits: int = 4, blank: str = "n/a") -> str:
    """A physical quantity with an SI prefix: ``8.5e-06`` V -> ``8.5000μV``."""
    if _is_none(value):
        return blank
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return si_format(float(value), digits, unit)


def _ppm(value, blank: str = "") -> str:
    """ppm, **blank at a zero point** -- ``None`` means "there is no ppm here".

    ``%g``, not a fixed number of places: a 0.004 ppm error on a 1000 V point
    printed as ``+0.00`` would lose the only information in the cell.
    """
    return blank if _is_none(value) else f"{float(value):+.4g}"


def _pct(value, blank: str = "n/a") -> str:
    return blank if _is_none(value) else f"{float(value) * 100:+.1f} %"


def _freq(value) -> str:
    return "dc" if not value else f"{float(value):g} Hz"


def _cell(text) -> str:
    """Make *text* safe inside a markdown table cell."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _prose(text) -> str:
    """One-line text outside a table, where ``|`` needs no escaping.

    ``verdict.evaluate``'s reasons open with ``|error| = ...``; escaping the
    bars there would put backslashes in the middle of every sentence a reader
    is meant to read.
    """
    return str(text).replace("\n", " ")


def _verb(count: int) -> str:
    """``carries``/``carry`` -- one report line is read far more often than it
    is written, and "1 point carry" is the kind of thing a reader stops on."""
    return "carries" if count == 1 else "carry"


def _opt(value, unit: str = "", fmt=str, blank: str = "unknown") -> str:
    """A value with its unit, or "unknown" -- **with the unit inside**.

    ``unknown d`` and ``unknown °C`` read like a measurement whose number went
    missing; the unit belongs to the number, so it only appears when there is
    one.
    """
    return blank if _is_none(value) else f"{fmt(value)}{unit}"


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def spec_ratio(row: ResultRow) -> float | None:
    """The plan's ``spec ratio`` for this point, from its note.

    DUT limit over reference limit: below 1 the reference is no better than the
    instrument being verified.  ``None`` when the plan recorded no ratio -- for
    a point where either side has no specification there is nothing to compare.
    """
    match = _SPEC_RATIO_RE.search(row.note or "")
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return None if math.isnan(value) else value


@dataclass(frozen=True)
class SpecRatios:
    """How much of a run compares against a reference worse than the DUT."""

    threshold: float
    #: ``(row, ratio)`` for the points whose ratio is at or below
    #: :attr:`threshold` -- the ones whose reference is no better than the DUT.
    low: tuple[tuple[ResultRow, float], ...] = ()
    #: Every point in the run, whether or not its note carried a ratio.
    total: int = 0

    @property
    def fraction(self) -> float:
        """Fraction of *all* points whose reference is not better than the DUT."""
        return 0.0 if not self.total else len(self.low) / self.total

    @property
    def worst(self) -> tuple[ResultRow, float] | None:
        return min(self.low, key=lambda item: item[1], default=None)

    @property
    def quantities(self) -> "Counter[str]":
        return Counter(row.quantity.name for row, _ in self.low)


def spec_ratio_summary(rows: Sequence[ResultRow],
                       threshold: float = SPEC_RATIO_WARN) -> SpecRatios:
    """Split a run by whether its reference is better than its DUT."""
    low = []
    for row in rows:
        ratio = spec_ratio(row)
        if ratio is not None and ratio <= threshold:
            low.append((row, ratio))
    return SpecRatios(threshold=float(threshold), low=tuple(low),
                      total=len(rows))


#: The row flags that name one unmet condition, and the words a report uses
#: for each.  ``conditions-not-met`` itself is the marker, not a condition, so
#: it is deliberately absent: a bullet reading "conditions not met" would say
#: only what the heading already says.
CONDITION_WORDS: Mapping[str, str] = {
    "source-not-warmed-up":
        "the reference had not finished warming up (its specifications are "
        "valid after a warm-up of twice its off time, to a 30-minute maximum)",
    "source-zeros-due":
        "the reference's dc zeros were overdue (its specifications require "
        "them at least every 30 days)",
    "temperature-out-of-window":
        "the ambient was outside the window the figures are written for",
    "reference-temperature-out-of-window":
        "the **reference** was outside the temperature window its own figures "
        "are written for — for a calibrator, its stored CAL_TEMP ± 5 °C",
    "ambient-temperature-unknown":
        "the ambient temperature was never recorded, so the window could not "
        "be shown to hold",
    "no-temperature-window":
        "the model states no absolute temperature window (its figures are "
        "relative to its own stored calibration temperature)",
}


def unmet_conditions(rows: Sequence[ResultRow]) -> dict[str, int]:
    """How many ``INDETERMINATE`` rows each unmet condition accounts for.

    Ordered as :data:`CONDITION_WORDS` is, so two runs of the same shape
    produce the same report.  Rows blocked by more than one condition are
    counted under each: they are independent facts, and clearing one would not
    clear the other.
    """
    counts: dict[str, int] = {}
    for row in rows:
        if row.verdict is not Verdict.INDETERMINATE:
            continue
        if FLAG_CONDITIONS_NOT_MET not in row.flags:
            continue
        for code in CONDITION_WORDS:
            if code in row.flags:
                counts[code] = counts.get(code, 0) + 1
    return counts


def indeterminate_causes(summary: RunSummary) -> dict[str, int]:
    """Why the unclear points are unclear -- **as five separate findings**.

    Straight off the :class:`~caldmm.cal.result.RunSummary`, which is where
    the arithmetic belongs: the same four numbers end a run on the console and
    open this report, and two implementations of "why is this point unclear"
    is one too many.  Pass a summary built with the run's own
    ``temperature_policy`` (see :func:`policy_of`) or the temperature and
    guard-band counts will be answers to a question the run did not ask.

    The counts **overlap**: a point whose TUR is below the floor *and* whose
    ambient was never recorded is blocked twice and is counted twice, because
    fixing one of them would not clear it.  They are not a partition of the
    ``INDETERMINATE`` total and must never be presented as one.
    """
    return {
        "tur_floor": summary.indeterminate_tur,
        "temperature": summary.indeterminate_temperature,
        "conditions": summary.indeterminate_conditions,
        "no_budget": summary.indeterminate_no_budget,
        "guard_band": summary.indeterminate_guard_band,
    }


def policy_of(header: RunHeader) -> tuple[str, bool]:
    """``(policy to compute with, whether the file's policy was recognised)``.

    A result file can name a policy this build does not know -- it was written
    by a later version, or hand-edited.  Crashing on it would lose the whole
    report, and silently substituting ``gate`` while the provenance block
    prints the file's own name would be a quiet lie, so the caller gets both
    and the report says which it used.
    """
    policy = header.temperature_policy or "gate"
    try:
        gates_for(policy)
    except VerdictError:
        return "gate", False
    return policy, True


_TEMPERATURE_WORDS: Mapping[TemperatureStatus, str] = {
    TemperatureStatus.OUT_OF_WINDOW:
        "ambient recorded and outside the specification window",
    TemperatureStatus.AMBIENT_UNKNOWN:
        "no ambient was recorded and a window exists",
    TemperatureStatus.NO_WINDOW:
        "the model declares no absolute window (its figures are relative to "
        "its own stored calibration temperature)",
    TemperatureStatus.UNKNOWN:
        "neither an ambient nor a window is known",
    TemperatureStatus.IN_WINDOW: "ambient inside the window",
}


def temperature_breakdown(rows: Sequence[ResultRow], policy: str = "gate"
                          ) -> tuple["Counter[TemperatureStatus]", int]:
    """``(counts by status, how many *policy* actually blocked)``.

    An ``INDETERMINATE`` point may merely *carry* a temperature condition -- on
    the 3458A-against-5700A pairing every row carries ``no-temperature-window``
    because neither model declares an absolute window, and the default ``gate``
    policy blocks on none of them.  The counts are what the report needs to say
    which is which; ``blocked`` is the same number
    :attr:`RunSummary.indeterminate_temperature` carries.
    """
    gates = gates_for(policy)
    counts: "Counter[TemperatureStatus]" = Counter()
    blocked = 0
    for row in rows:
        if not row.carries_temperature_condition:
            continue
        counts[row.temperature_status] += 1
        if row.temperature_status in gates:
            blocked += 1
    return counts, blocked


def dominant_contribution(row: ResultRow) -> tuple[str, float] | None:
    """``(label, percent of variance)`` of the term that dominates the budget.

    The actionable half of a budget: it says what to change to get a better
    measurement.  Repeatability at 88 % means average more; the calibrator at
    88 % means nothing done at the bench will help.
    """
    contributions = row.budget.contributions()
    if not contributions:
        return None
    label, _, percent = contributions[0]
    return (label, percent)


def _error_ratio(row: ResultRow) -> float | None:
    """``error / TL`` -- 1.0 is the tolerance limit whatever the unit."""
    if row.error is None or row.tolerance_limit is None:
        return None
    if not math.isfinite(row.tolerance_limit) or row.tolerance_limit <= 0.0:
        return None
    return row.error / row.tolerance_limit


def _acceptance_ratio(row: ResultRow) -> float | None:
    if row.acceptance_limit is None or row.tolerance_limit is None:
        return None
    if not math.isfinite(row.tolerance_limit) or row.tolerance_limit <= 0.0:
        return None
    return row.acceptance_limit / row.tolerance_limit


def _sorted_rows(rows: Iterable[ResultRow]) -> list[ResultRow]:
    return sorted(rows, key=lambda row: (row.group_index, row.point_index))


def _run_span(header: RunHeader,
              rows: Sequence[ResultRow]) -> tuple[datetime | None, datetime | None]:
    stamps = [row.timestamp for row in rows if row.timestamp is not None]
    first = header.started or (min(stamps) if stamps else None)
    last = max(stamps) if stamps else None
    return first, last


def _run_seconds(header: RunHeader, rows: Sequence[ResultRow]) -> float | None:
    first, last = _run_span(header, rows)
    if first is None or last is None:
        return None
    return (last - first).total_seconds()


def _dut_label(header: RunHeader) -> str:
    role = (header.dut_role or "").strip().lower()
    if role == "meter":
        return header.meter_model or "the meter"
    if role == "source":
        return header.source_model or "the source"
    return header.dut_role or "unknown"


def _reference_label(header: RunHeader) -> str:
    role = (header.dut_role or "").strip().lower()
    if role == "meter":
        return header.source_model or "the source"
    if role == "source":
        return header.meter_model or "the meter"
    return "unknown"


# --------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Fault:
    """One queued instrument fault, decoded offline.

    ``assemblies`` is a *tuple* because 23 of the 191 shipped codes name two --
    ``3117: A14/A15: HV +DC Current Error Amp Fault`` implicates both boards
    and the report has to be findable under either.
    """

    #: ``None`` only for an entry this build could not parse a code out of.
    code: int | None
    #: The message with any assembly prefix removed.
    text: str
    assemblies: tuple[str, ...] = ()

    @property
    def assembly(self) -> str | None:
        """The first assembly named, or ``None``."""
        return self.assemblies[0] if self.assemblies else None

    @property
    def prefix(self) -> str:
        """``A14/A15`` as the fault table spells it, or ``""``."""
        return "/".join(self.assemblies)

    def line(self) -> str:
        code = "?" if self.code is None else str(self.code)
        prefix = f"{self.prefix}: " if self.assemblies else ""
        return f"{code}: {prefix}{self.text}"

    def sort_key(self) -> tuple[int, int]:
        """Unparsed entries last; everything else by code."""
        return (1, 0) if self.code is None else (0, self.code)


@lru_cache(maxsize=1)
def load_fault_table() -> Mapping[int, str]:
    """The offline 5700A/5720A fault-code table shipped with ``cal/data``.

    Read from the package data rather than from an instrument: bench-side
    analysis routinely runs with nothing attached, and a unit sick enough to
    have a fault queue is the last unit whose ``EXPLAIN?`` should be trusted.
    """
    import yaml

    resource = resources.files("caldmm.cal.data") / "fluke5700a_faults.yaml"
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):        # pragma: no cover - packaging
        return {}
    data = yaml.safe_load(text) or {}
    faults = data.get("faults") or {}
    table: dict[int, str] = {}
    for code, message in faults.items():
        try:
            table[int(code)] = str(message)
        except (TypeError, ValueError):          # pragma: no cover - bad data
            continue
    return table


def _fault_from(code: int | None, recorded: str, table: Mapping[int, str]
                ) -> Fault:
    text = recorded.strip()
    if not text and code is not None:
        text = table.get(code, "")
    if not text:
        text = "unknown fault code (not in the offline table)"
    # One implementation of "which board is this", shared with the operator
    # console: a second copy here was prefix-only, and prefix-only loses
    # 3135 "Assembly A14 Not Responding" -- an A14 fault, in the block this
    # bench's unit fails in, that says so in prose instead of as a prefix.
    return Fault(code=code, text=strip_assembly_prefix(text),
                 assemblies=fault_assemblies(text))


def parse_faults(header: RunHeader) -> list[Fault]:
    """Faults the run recorded, decoded offline and split by assembly.

    **The contract, which the runner has to write to.**  One header key,
    :data:`FAULTS_KEY`, in either of two forms:

    ``faults=3111,3104,808``
        **Bare codes only.**  The text comes from the shipped table, so nothing
        has to survive the file.  This is the form to use.

    ``faults=[[3122, "A14/A15: HV AC 1KHZ, Preamp Fault (lo)"], 3111]``
        A JSON list, when the recorded text itself must be kept.  It is the
        only safe way to carry text: **21 of the 191 real fault messages
        contain a comma**, so a comma-separated ``code: text`` form truncates
        at the first one -- silently, because the tail then fails to parse as a
        code and is dropped.  The file already carries JSON in ``Budget`` and
        ``Mtr_Applied``; this is the same trick.

    An entry no code can be read from is still listed, saying so, rather than
    dropped: a fault this build cannot parse is exactly the one worth seeing.
    A bare-code entry with a trailing ``: text`` is tolerated for older files --
    the code is taken and the text is read from the table.
    """
    raw = str(header.extra.get(FAULTS_KEY) or "").strip()
    if not raw:
        return []

    table = load_fault_table()
    faults: list[Fault] = []

    if raw.startswith("["):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return [Fault(code=None,
                          text=f"unparsable fault record: {raw!r}")]
        for item in items if isinstance(items, list) else [items]:
            code, recorded = None, ""
            if isinstance(item, bool):
                pass
            elif isinstance(item, int):
                code = item
            elif isinstance(item, (list, tuple)) and item:
                code = item[0] if isinstance(item[0], int) else None
                recorded = str(item[1]) if len(item) > 1 else ""
            elif isinstance(item, Mapping):
                raw_code = item.get("code")
                code = raw_code if isinstance(raw_code, int) else None
                recorded = str(item.get("text") or "")
            if code is None and not recorded:
                faults.append(Fault(code=None,
                                    text=f"unparsable fault entry: {item!r}"))
                continue
            faults.append(_fault_from(code, recorded, table))
        return faults

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # Take the code off the front and let the table supply the text: the
        # only thing after the colon in the legacy form is a copy of what the
        # table already has, and a comma inside it has already split the entry
        # by the time we get here.
        code_text = item.partition(":")[0].strip()
        try:
            code = int(code_text)
        except ValueError:
            faults.append(Fault(code=None,
                                text=f"unparsable fault entry: {item!r}"))
            continue
        faults.append(_fault_from(code, "", table))
    return faults


def group_faults(faults: Sequence[Fault]) -> list[tuple[str | None, list[Fault]]]:
    """Faults by assembly, named assemblies first, then the ones naming none.

    A fault naming two assemblies is listed under **both**: someone tracing an
    A14 problem must find every code that implicates A14, and the printed line
    keeps the full ``A14/A15`` prefix so the duplication is self-evident rather
    than looking like two faults.
    """
    groups: dict[str | None, list[Fault]] = {}
    for fault in faults:
        for name in fault.assemblies or (None,):
            groups.setdefault(name, []).append(fault)
    named = sorted((key for key in groups if key is not None))
    ordered: list[tuple[str | None, list[Fault]]] = [
        (key, groups[key]) for key in named]
    if None in groups:
        ordered.append((None, groups[None]))
    return ordered


# --------------------------------------------------------------------------
# Comparing two runs
# --------------------------------------------------------------------------

def point_key(row: ResultRow) -> tuple:
    """Identity of a point across runs of the same plan.

    Deliberately *not* the point index: a resumed or re-ordered run renumbers
    its points, and matching on the index would silently pair a 10 V dc point
    with a 100 kohm one.

    **The range belongs in the key.**  A meter-DUT run pins the meter's range
    with ``range_arg`` and carries no ``range_id`` at all, so a key without it
    aliases every setpoint that appears on more than one range -- on the shipped
    3458A plan that is all five ``DCV 0 V`` points (the 0.1, 1, 10, 100 and
    1000 V ranges), which :func:`compare_runs` would then match as one point and
    report four legitimate measurements as missing.  Kept in step with
    ``plan._identity`` and ``apps.cal_verify.plan_point_key``, which answer the
    same question about a plan point.
    """
    return (row.quantity.name, repr(float(row.nominal)),
            repr(float(row.frequency_hz)), row.connection.value,
            row.range_id or "",
            "" if row.range_arg is None else repr(float(row.range_arg)))


@dataclass(frozen=True)
class PointDelta:
    """One point measured in two runs."""

    before: ResultRow
    after: ResultRow

    @property
    def label(self) -> str:
        return self.after.label or self.after.quantity.name

    @property
    def unit(self) -> str:
        return self.after.unit

    @property
    def error_delta(self) -> float | None:
        if self.before.error is None or self.after.error is None:
            return None
        return self.after.error - self.before.error

    @property
    def ratio_delta(self) -> float | None:
        before = _error_ratio(self.before)
        after = _error_ratio(self.after)
        if before is None or after is None:
            return None
        return after - before

    @property
    def changed(self) -> bool:
        return self.before.verdict is not self.after.verdict

    @property
    def transition(self) -> str:
        return (f"{self.before.verdict.value.upper()} -> "
                f"{self.after.verdict.value.upper()}")

    @property
    def improved(self) -> bool:
        return (self.before.verdict is Verdict.FAIL
                and self.after.verdict is not Verdict.FAIL)

    @property
    def regressed(self) -> bool:
        return (self.before.verdict is not Verdict.FAIL
                and self.after.verdict is Verdict.FAIL)


def compare_runs(before: Sequence[ResultRow], after: Sequence[ResultRow]
                 ) -> tuple[list[PointDelta], list[ResultRow], list[ResultRow]]:
    """``(matched, only_before, only_after)`` for two runs of the same plan.

    Unmatched points are returned rather than dropped: a point that one run has
    and the other does not is itself a finding -- a plan was changed, or a run
    stopped early.
    """
    before_map: dict[tuple, ResultRow] = {}
    for row in before:
        before_map.setdefault(point_key(row), row)
    matched: list[PointDelta] = []
    only_after: list[ResultRow] = []
    seen: set[tuple] = set()
    for row in _sorted_rows(after):
        key = point_key(row)
        previous = before_map.get(key)
        if previous is None:
            only_after.append(row)
            continue
        seen.add(key)
        matched.append(PointDelta(before=previous, after=row))
    only_before = [row for key, row in before_map.items() if key not in seen]
    return matched, only_before, _sorted_rows(only_after)


def find_counterpart(csv_path: str,
                     header: RunHeader) -> tuple[str | None, str | None]:
    """``(path, warning)`` -- the as-found run this file's header names.

    Only an **as-left** run is auto-compared: its counterpart is the as-found
    measured *before* the adjustment, so ``before -> after`` reads the right
    way round.  An as-found run naming a counterpart is left alone -- swapping
    it in as "before" would invert every delta in the report.

    The stored path is tried as written, then by basename next to *csv_path*:
    a pair of runs recorded on the bench and analysed from another directory
    is the normal case, and the two files travel together.  A counterpart
    that is named but nowhere to be found is a *warning*, never a crash --
    the run's own report must not be hostage to its sibling's whereabouts.
    """
    name = (header.counterpart or "").strip()
    if not name or header.run_kind != "as-left":
        return None, None
    sibling = os.path.join(os.path.dirname(os.path.abspath(csv_path)),
                           os.path.basename(name))
    for candidate in (name, sibling):
        if os.path.exists(candidate):
            return candidate, None
    return None, (f"the header names the as-found counterpart {name!r} but "
                  f"no such file exists (also tried {sibling}); the "
                  f"before/after comparison is skipped")


#: Columns of the machine-readable ``--compare`` diff, one row per point.
#: Both sides of every numeric field, because the point of the file is that
#: somebody else can check the arithmetic; floats are written with ``repr``
#: so they are the loaded values, not a rounding of them.
COMPARE_CSV_COLUMNS = [
    "status", "quantity", "nominal", "frequency_hz", "connection",
    "range_id", "range_arg", "label", "unit",
    "before_error", "after_error", "delta_error",
    "before_error_over_tl", "after_error_over_tl", "delta_error_over_tl",
    "before_verdict", "after_verdict", "verdict_changed",
]


def _diff_cell(value) -> str:
    return "" if value is None else repr(float(value))


def _diff_identity(row: ResultRow) -> dict[str, str]:
    return {
        "quantity": row.quantity.name,
        "nominal": repr(float(row.nominal)),
        "frequency_hz": repr(float(row.frequency_hz)),
        "connection": row.connection.value,
        "range_id": row.range_id or "",
        "range_arg": ("" if row.range_arg is None
                      else repr(float(row.range_arg))),
        "label": row.label or "",
        "unit": row.unit,
    }


def compare_csv_rows(matched: Sequence[PointDelta],
                     only_before: Sequence[ResultRow],
                     only_after: Sequence[ResultRow]) -> list[dict[str, str]]:
    """The ``--compare`` result as machine-readable rows.

    Keyed and ordered exactly as :func:`compare_runs` returned them, so the
    CSV and the printed table cannot disagree about which points matched.
    Points present in only one run are kept with a ``status`` of
    ``added``/``removed`` rather than dropped -- a missing point is itself a
    finding.  Pattern: ``apps.fluke5700a_selfcal.diff_csv_rows``.
    """
    rows: list[dict[str, str]] = []
    blank = {column: "" for column in COMPARE_CSV_COLUMNS}
    for delta in matched:
        row = dict(blank)
        row.update(_diff_identity(delta.after))
        row.update({
            "status": "both",
            "before_error": _diff_cell(delta.before.error),
            "after_error": _diff_cell(delta.after.error),
            "delta_error": _diff_cell(delta.error_delta),
            "before_error_over_tl": _diff_cell(_error_ratio(delta.before)),
            "after_error_over_tl": _diff_cell(_error_ratio(delta.after)),
            "delta_error_over_tl": _diff_cell(delta.ratio_delta),
            "before_verdict": delta.before.verdict.value,
            "after_verdict": delta.after.verdict.value,
            "verdict_changed": "1" if delta.changed else "0",
        })
        rows.append(row)
    for status, side, error_key, ratio_key, verdict_key in (
            ("removed", only_before, "before_error", "before_error_over_tl",
             "before_verdict"),
            ("added", only_after, "after_error", "after_error_over_tl",
             "after_verdict")):
        for point in side:
            row = dict(blank)
            row.update(_diff_identity(point))
            row.update({
                "status": status,
                error_key: _diff_cell(point.error),
                ratio_key: _diff_cell(_error_ratio(point)),
                verdict_key: point.verdict.value,
            })
            rows.append(row)
    return rows


def compare_diff_path(base_path: str) -> str:
    """``<base stem>-diff.csv`` -- where the machine-readable diff goes."""
    return os.path.splitext(base_path)[0] + "-diff.csv"


def write_compare_csv(path: str, matched: Sequence[PointDelta],
                      only_before: Sequence[ResultRow],
                      only_after: Sequence[ResultRow]) -> str:
    """Write the diff CSV and return *path*.  Plain ``csv``, not the result
    schema: this file is derived, regenerable from the two runs at any time,
    and meant for a spreadsheet -- none of which is true of a result file."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(compare_csv_rows(matched, only_before, only_after))
    return path


# --------------------------------------------------------------------------
# Markdown sections
# --------------------------------------------------------------------------

def _md_title(path: str, header: RunHeader, rows: Sequence[ResultRow],
              summary: RunSummary) -> list[str]:
    name = header.plan_name or os.path.basename(path)
    first, _ = _run_span(header, rows)
    lines = [f"# Verification report — {name}", ""]
    lines.append(f"- source file: `{os.path.basename(path)}`")
    lines.append(f"- run id: `{header.run_id or 'unknown'}`")
    lines.append(f"- started: {_stamp(first)}, "
                 f"duration {_duration(_run_seconds(header, rows))}")
    lines.append(f"- {summary.line()}")
    policy, known = policy_of(header)
    statuses, blocked = temperature_breakdown(rows, policy)
    carried = sum(statuses.values())
    if carried > blocked:
        rest = carried - blocked
        if blocked:
            lines.append(
                f"- note: the {blocked} counted as temperature-gated above "
                f"are the ones the `{policy}` policy actually blocked; "
                f"{_plural(rest, 'further point')} {_verb(rest)} a "
                f"temperature condition it does not block on — see Verdicts "
                f"below")
        else:
            lines.append(
                f"- note: {_plural(rest, 'point')} {_verb(rest)} a "
                f"temperature condition the `{policy}` policy does not block "
                f"on, so none is counted as temperature-gated above; they are "
                f"unclear for another reason — see Verdicts below")
    if not known:
        lines.append(f"- note: this file names an unrecognised temperature "
                     f"policy `{header.temperature_policy}`; the counts here "
                     f"are taken under `gate`")
    lines.append("")
    return lines


#: A run that stopped for a safety rule, and what a reader has to be told.
#: Both are abandonments, not findings: the rows after them do not exist, and
#: reading such a file as a verification is reading a run that never finished.
SAFETY_ABORTS: Mapping[str, str] = {
    # First, always: it is the only one of these whose consequence is on the
    # bench rather than in the record.
    "standby-not-confirmed":
        "the source did not confirm standby before the meter was to be "
        "re-ranged, so THE OUTPUT MAY STILL BE LIVE — nothing was re-ranged "
        "and nothing further was sourced, but press RESET and verify the "
        "standby light before touching the terminals",
    "wiring-proof-failed":
        "the safe-level wiring proof did not read back what was sourced, so "
        "the planned level was never applied to that wiring",
    "wiring-proof-unreadable":
        "the safe-level wiring proof could not be read from the meter, which "
        "says nothing about the wiring -- the meter returned no value at all; "
        "the point's note names the error, and the planned level was never "
        "applied",
    "gross-error-abort":
        "a reading was off by more than 10x the point's tolerance, which is a "
        "connection and not an instrument out of specification",
    "meter-range-mismatch":
        "the meter's own range read-back disagreed with the range that was "
        "configured, above the hazard thresholds; nothing was energized",
    "prestep-failed":
        "the 10 %-of-target pre-step did not read back the level that was "
        "sourced, so the full value was never applied",
}


def _md_warnings(header: RunHeader, rows: Sequence[ResultRow],
                 summary: RunSummary, ratios: SpecRatios,
                 faults: Sequence[Fault]) -> list[str]:
    """The things a reader must see before any table."""
    bullets: list[str] = []

    # First, and before anything about verdicts: a run that was stopped is
    # not a run whose remaining points passed.
    for flag, why in SAFETY_ABORTS.items():
        stopped = [row for row in rows if flag in row.flags]
        if not stopped:
            continue
        where = stopped[0]
        bullets.append(
            f"**THIS RUN WAS STOPPED — `{flag}`.** At point "
            f"{where.point_index} ({where.label or where.quantity.name}), "
            f"{why}. Everything after that point was never measured, so this "
            f"file is **not a complete verification**: fix what the message "
            f"names, then re-run from the group it stopped in.")

    if ratios.low:
        worst = ratios.worst
        share = f"{ratios.fraction * 100:.0f} %"
        bullet = (f"**The reference is no better than the instrument under "
                  f"test at {len(ratios.low)} of {ratios.total} points "
                  f"({share}).** Their plan spec ratio (DUT limit / reference "
                  f"limit) is at or below {ratios.threshold:g}, so at those "
                  f"points the comparison is mostly measuring "
                  f"{_reference_label(header)} rather than "
                  f"{_dut_label(header)}. Read their verdicts as a statement "
                  f"about the pairing, not about the instrument.")
        if worst is not None:
            row, ratio = worst
            bullet += (f" Worst: {ratio:.3g} at "
                       f"{row.label or row.quantity.name}.")
        counts = ", ".join(f"{name} {count}"
                           for name, count in sorted(ratios.quantities.items()))
        if counts:
            bullet += f" By quantity: {counts}."
        bullets.append(bullet)

    fails = summary[Verdict.FAIL]
    if fails:
        worst_fail = min(
            (row for row in rows if row.verdict is Verdict.FAIL),
            key=lambda row: (row.relative_margin
                             if row.relative_margin is not None else 0.0),
            default=None)
        bullet = f"**{_plural(fails, 'point')} FAILED.**"
        if worst_fail is not None:
            bullet += (f" Worst: {worst_fail.label or worst_fail.quantity.name}"
                       f", error {_q(worst_fail.error, worst_fail.unit)} "
                       f"against a limit of "
                       f"{_q(worst_fail.tolerance_limit, worst_fail.unit)} "
                       f"({_pct(worst_fail.relative_margin)} of TL).")
        bullets.append(bullet)

    causes = indeterminate_causes(summary)
    if causes["tur_floor"]:
        bullets.append(
            f"**{_plural(causes['tur_floor'], 'point')} could not be judged "
            f"because the test uncertainty ratio was below the "
            f"{header.tur_floor:g}:1 floor** — the reference is not good "
            f"enough there. This is not a statement about the DUT.")
    if causes["temperature"]:
        bullets.append(
            f"**{_plural(causes['temperature'], 'point')} could not be judged "
            f"because the specification's temperature conditions were not "
            f"shown to hold** — a different finding from the TUR floor, and "
            f"usually a procedural fix.")
    per_condition = unmet_conditions(rows)
    others = {code: count for code, count in per_condition.items()
              if code not in TEMPERATURE_FLAGS}
    if others:
        bullets.append(
            f"**{_plural(causes['conditions'], 'point')} carries a stated "
            f"condition of the specification that was not shown to hold**, so "
            f"it is INDETERMINATE and **not counted as PASS or FAIL** — a "
            f"specification holds under its stated conditions, and where they "
            f"do not hold there is neither conformity nor non-conformity to "
            f"report.")
        for code, count in others.items():
            bullets.append(
                f"— {_plural(count, 'point')}: {CONDITION_WORDS[code]} "
                f"(`{code}`).")
    if causes["guard_band"]:
        bullets.append(
            f"**{_plural(causes['guard_band'], 'point')} landed inside the "
            f"guard band** — the reference was good enough and the conditions "
            f"held; the DUT itself is too close to its limit to state either "
            f"conformity or non-conformity at this confidence.")
    if causes["no_budget"]:
        bullets.append(
            f"**{_plural(causes['no_budget'], 'point')} carries no "
            f"uncertainty budget at all**, so nothing about it can be "
            f"defended.")

    # Gate on the rows too: a run whose header carries no ambient but whose
    # every point does is not a run where "the ambient was never recorded",
    # and printing that two paragraphs above the per-point ambient range
    # contradicts the report's own provenance block.
    if not header.ambient_known and not any(row.ambient_c is not None
                                            for row in rows):
        bullets.append(
            "**The ambient temperature was never recorded for this run.** "
            "Every temperature-dependent claim below rests on an assumption.")

    if header.claim_disagrees_with_interval:
        bullets.append(
            f"**The claim tested is not the interval this run is headed "
            f"with.** The tolerance at every point where the calibrator "
            f"states one is its own `UNCERT?`, computed by its firmware at "
            f"its stored CAL_INTV / CAL_CONF — here **"
            f"{header.claim_tested}** — while the header says "
            f"`interval={header.interval or 'unknown'}`. Read the verdicts as "
            f"a statement about the specification the instrument computed, "
            f"and set CAL_INTV to the interval you mean to certify before the "
            f"next run.")

    disagreeing = [row for row in rows if row.limit_disagree]
    if disagreeing:
        names = sorted({row.label or row.quantity.name for row in disagreeing})
        listed = ", ".join(names[:6]) + (", …" if len(names) > 6 else "")
        bullets.append(
            f"**{_plural(len(disagreeing), 'point')} where the spec table and "
            f"the instrument's own UNCERT? disagree by more than "
            f"{LIMIT_DISAGREEMENT_FACTOR:g}x** — check the calibrator's "
            f"CAL_INTV / CAL_CONF and its stored constants: {listed}.")

    unproved = [row for row in rows
                if "wiring-proof-not-resolvable" in row.flags]
    if unproved:
        bullets.append(
            f"**{_plural(len(unproved), 'point')} was measured with no "
            f"safe-level wiring proof before it** — no level this meter can "
            f"read to +/-10 % is also below the hazard thresholds, so §8's "
            f"proof could not be taken there and was recorded as absent "
            f"rather than faked. Each row's note says why; the readings "
            f"themselves are ordinary. {_prose(unproved[0].note)}")

    if summary.overloads:
        bullets.append(
            f"**{_plural(summary.overloads, 'sample')} were invalid-reading "
            f"sentinels** and were excluded from the means.")

    if faults:
        bullets.append(
            f"**The instrument reported {_plural(len(faults), 'fault')}** — "
            "see the fault section below.")

    if not bullets:
        return []
    lines = ["## Read this first", ""]
    lines += [f"- {bullet}" for bullet in bullets]
    lines.append("")
    return lines


def _md_provenance(path: str, header: RunHeader,
                   rows: Sequence[ResultRow]) -> list[str]:
    first, last = _run_span(header, rows)
    role = (header.dut_role or "unknown").strip().lower()

    lines = ["## Provenance", ""]
    lines.append(f"Device under test: **{_dut_label(header)}** "
                 f"(`dut_role={header.dut_role or 'unknown'}`); "
                 f"reference: **{_reference_label(header)}**.")
    lines.append("")

    lines.append("### Instruments")
    lines.append("")
    lines.append("| | source | meter |")
    lines.append("|---|---|---|")
    lines.append(f"| role | {'reference' if role == 'meter' else 'DUT'} "
                 f"| {'DUT' if role == 'meter' else 'reference'} |")
    lines.append(f"| model | {_cell(header.source_model or 'unknown')} "
                 f"| {_cell(header.meter_model or 'unknown')} |")
    lines.append(f"| \\*IDN? | `{_cell(header.source_idn or 'unknown')}` "
                 f"| `{_cell(header.meter_idn or 'unknown')}` |")
    lines.append(f"| options | "
                 f"{_cell(', '.join(header.source_options) or 'none')} | — |")
    lines.append(f"| since CAL (CAL_DAYS?) | "
                 f"{_opt(header.source_cal_days, ' d')} | — |")
    lines.append(f"| since ZERO | "
                 f"{_opt(header.source_zero_days, ' d')} | — |")
    lines.append(f"| cal interval / confidence | "
                 f"{_opt(header.source_cal_interval_days, ' d')}, "
                 f"{_cell(header.source_cal_confidence or 'unknown')} | — |")
    if header.claim_tested:
        lines.append(f"| claim tested | {_cell(header.claim_tested)} | — |")
    lines.append(f"| cal temperature (CAL_TEMP?) | "
                 f"{_opt(header.source_cal_temp_c, ' °C', _g)} | — |")
    lines.append(f"| warm-up | "
                 f"{_opt(header.source_warm_minutes, ' min')} | — |")
    lines.append(f"| CALNUM | — | {_opt(header.meter_calnum)} |")
    lines.append(f"| ACAL age | — | "
                 f"{_opt(header.meter_acal_age_h, ' h', _g)} |")
    lines.append(f"| internal temperature | — | "
                 f"{_opt(header.meter_temp_c, ' °C', _g)} |")
    lines.append("")

    lines.append("### Conditions")
    lines.append("")
    if header.ambient_known:
        lines.append(f"- ambient: {_g(header.ambient_c)} °C, humidity "
                     f"{_g(header.humidity_pct)} %, read "
                     f"{_stamp(header.ambient_timestamp)} "
                     f"from `{header.ambient_source or 'unknown source'}`")
    else:
        lines.append("- ambient: **unknown** — no ambient temperature was "
                     "recorded for this run")
    ambients = [row.ambient_c for row in rows if row.ambient_c is not None]
    if ambients:
        env_stamps = [row.env_timestamp for row in rows
                      if row.env_timestamp is not None]
        lines.append(f"- per-point ambient: {min(ambients):.3f} to "
                     f"{max(ambients):.3f} °C over {len(ambients)} of "
                     f"{len(rows)} points"
                     + (f", last read {_stamp(max(env_stamps))}"
                        if env_stamps else ""))
    else:
        lines.append(f"- per-point ambient: **unknown** at all "
                     f"{len(rows)} points")
    meter_temps = [row.meter_temp_c for row in rows
                   if row.meter_temp_c is not None]
    if meter_temps:
        lines.append(f"- meter internal temperature: {min(meter_temps):.3f} "
                     f"to {max(meter_temps):.3f} °C over {len(meter_temps)} "
                     f"of {len(rows)} points")
    if header.source_cal_temp_c is not None:
        lines.append(f"- source cal temperature (CAL_TEMP?): "
                     f"{_g(header.source_cal_temp_c)} °C — the ambient its "
                     f"stored constants assume; the departure from it drives "
                     f"its tempco")
    lines.append("")

    lines.append("### Decision rule")
    lines.append("")
    lines.append(f"- interval: {header.interval or 'model default'}")
    lines.append(f"- confidence: {header.confidence * 100:g} %")
    lines.append(f"- TUR floor: {header.tur_floor:g}:1")
    lines.append(f"- guard-band factor g: {header.guard_band_factor:g} "
                 f"(w = g·U)")
    policy, known = policy_of(header)
    lines.append(f"- temperature policy: {header.temperature_policy}"
                 + ("" if known else
                    f" — **unrecognised by this build**; every count in this "
                    f"report is taken under `{policy}`"))
    rules = sorted({row.rule for row in rows if row.rule})
    for rule in rules:
        lines.append(f"- rule as applied: `{_cell(rule)}`")
    lines.append("")

    lines.append("### Run")
    lines.append("")
    lines.append(f"- file: `{os.path.abspath(path)}`")
    lines.append(f"- points: {len(rows)}")
    lines.append(f"- started: {_stamp(first)}; last point: {_stamp(last)}; "
                 f"duration {_duration(_run_seconds(header, rows))}")
    if header.run_kind and header.run_kind != "standalone":
        lines.append(f"- run kind: **{_cell(header.run_kind)}** — "
                     + ("measured before an adjustment"
                        if header.run_kind == "as-found"
                        else "measured after an adjustment"
                        if header.run_kind == "as-left"
                        else "as recorded"))
    if header.counterpart:
        lines.append(f"- counterpart run: `{_cell(header.counterpart)}`")
    if header.adjust_record:
        lines.append(f"- adjustment record: `{_cell(header.adjust_record)}`")
    lines.append(f"- caldmm {header.caldmm_version or 'unknown'}, git "
                 f"{header.git_revision or 'unknown'}, result schema "
                 f"v{header.schema_version}")
    extra = {key: value for key, value in header.extra.items()
             if key != FAULTS_KEY}
    for key in sorted(extra):
        lines.append(f"- {_cell(key)}: {_cell(extra[key])}")
    lines.append("")
    return lines


def _md_verdicts(rows: Sequence[ResultRow], summary: RunSummary,
                 header: RunHeader) -> list[str]:
    lines = ["## Verdicts", ""]
    lines.append("| verdict | points | share |")
    lines.append("|---|---:|---:|")
    for verdict in _VERDICT_ORDER:
        count = summary[verdict]
        share = 0.0 if not summary.total else 100.0 * count / summary.total
        lines.append(f"| {verdict.value.upper()} | {count} | {share:.0f} % |")
    lines.append(f"| **total** | **{summary.total}** | |")
    lines.append("")

    causes = indeterminate_causes(summary)
    policy, _ = policy_of(header)
    if summary[Verdict.INDETERMINATE]:
        lines.append("### Why the indeterminate points are indeterminate")
        lines.append("")
        lines.append("| cause | points | what it means |")
        lines.append("|---|---:|---|")
        lines.append(f"| TUR below the {header.tur_floor:g}:1 floor "
                     f"| {causes['tur_floor']} | the **reference** is not good "
                     f"enough at that point; re-running will not change it |")
        lines.append(f"| temperature conditions not shown "
                     f"| {causes['temperature']} "
                     f"| the **specification's stated conditions** were not "
                     f"demonstrated; record the ambient and repeat |")
        lines.append(f"| stated conditions not shown (any) "
                     f"| {causes['conditions']} "
                     f"| the temperature above **plus** warm-up and overdue "
                     f"zeros: the specification was not in force, so neither "
                     f"conformity nor non-conformity is stated |")
        lines.append(f"| error inside the guard band | {causes['guard_band']} "
                     f"| the **DUT** is close enough to its limit that neither "
                     f"statement can be made at this confidence |")
        lines.append(f"| no uncertainty budget | {causes['no_budget']} | "
                     f"nothing was supplied to support any statement |")
        lines.append("")
        lines.append("A point blocked by two causes is counted under both; "
                     "these are independent blockers, not a partition of the "
                     "indeterminate total.")
        lines.append("")
        statuses, blocked = temperature_breakdown(rows, policy)
        if statuses:
            lines.append(f"Of the "
                         f"{_plural(sum(statuses.values()), 'point')} carrying "
                         f"a temperature condition, **{blocked}** blocked a "
                         f"statement under the `{policy}` policy:")
            lines.append("")
            gates = gates_for(policy)
            for status, count in sorted(statuses.items(),
                                        key=lambda item: -item[1]):
                lines.append(
                    f"- {count} × `{status.value}` — "
                    f"{_TEMPERATURE_WORDS.get(status, status.value)}"
                    + ("; this blocks a PASS" if status in gates else
                       "; this alone does **not** block a PASS under "
                       "this policy, so those points are indeterminate for "
                       "another reason and are flagged only so the report "
                       "cannot omit the condition"))
            lines.append("")

    if summary.worst is not None:
        worst = summary.worst
        lines.append(f"Closest to its limit: **{worst.label or worst.quantity.name}** "
                     f"— margin {_q(worst.margin, worst.unit)} "
                     f"({_pct(summary.worst_relative_margin)} of TL), "
                     f"verdict {worst.verdict.value.upper()}.")
        lines.append("")
    if summary.overloads or summary.discarded:
        lines.append(f"Samples excluded across the run: "
                     f"{summary.overloads} invalid-reading sentinels, "
                     f"{summary.discarded} discarded settling samples.")
        lines.append("")

    by_quantity: dict[str, Counter] = {}
    for row in rows:
        by_quantity.setdefault(row.quantity.name, Counter())[row.verdict] += 1
    if by_quantity:
        lines.append("### By quantity")
        lines.append("")
        lines.append("| quantity | " + " | ".join(v.value.upper()
                                                  for v in _VERDICT_ORDER)
                     + " | total |")
        lines.append("|---|" + "---:|" * (len(_VERDICT_ORDER) + 1))
        for name in sorted(by_quantity):
            counts = by_quantity[name]
            total = sum(counts.values())
            lines.append(f"| {name} | "
                         + " | ".join(str(counts[v]) for v in _VERDICT_ORDER)
                         + f" | {total} |")
        lines.append("")
    return lines


#: Label prefix of a row that is a wiring proof, not its point's measurement
#: (``caldmm.apps.cal_verify.PROOF_ROW_LABEL_PREFIX``; not imported, because
#: this module must not pull in the runner).
_PROOF_PREFIX = "wiring proof ("


def _measured(rows: Iterable[ResultRow]) -> list[ResultRow]:
    return [row for row in rows
            if not str(row.label or "").startswith(_PROOF_PREFIX)]


def _dut_meter_model(header: RunHeader):
    """The meter's model spec when the meter was under test, else ``None``."""
    if str(header.dut_role or "").lower() != "meter" or not header.meter_model:
        return None
    try:
        return load_model(header.meter_model)
    except (SpecError, OSError, ValueError, KeyError):
        return None


def _row_range(row: ResultRow) -> float | None:
    for value in (row.range_arg, row.meter_range_fs):
        if value is not None:
            return float(value)
    return None


def range_coverage(header: RunHeader, rows: Sequence[ResultRow]
                   ) -> list[tuple[Quantity, Connection, float, Counter]]:
    """Every range of the DUT meter, with the verdicts measured on it.

    ``(quantity, wiring, range full scale, Counter of verdict values)`` in the
    model file's order; an empty counter is a range the run never touched.
    Only for a meter under test, whose ranges are the plan's axis.
    """
    spec = _dut_meter_model(header)
    if spec is None:
        return []
    by_key: dict[tuple, Counter] = {}
    for row in _measured(rows):
        scale = _row_range(row)
        if scale is None:
            continue
        key = (row.quantity, row.connection, round(scale, 15))
        by_key.setdefault(key, Counter())[row.verdict.value.upper()] += 1
    out = []
    for quantity, function in spec.functions.items():
        wirings = tuple(function.connections) or (Connection.TWO_WIRE,)
        for connection in wirings:
            for range_spec in sorted(function.ranges, key=lambda r: r.fs):
                scale = (range_spec.range_arg if range_spec.range_arg
                         is not None else range_spec.fs)
                out.append((quantity, connection, range_spec.fs,
                            by_key.get((quantity, connection,
                                        round(float(scale), 15)), Counter())))
    return out


def _md_coverage(header: RunHeader, rows: Sequence[ResultRow]) -> list[str]:
    coverage = range_coverage(header, rows)
    if not coverage:
        return []
    lines = ["## Range coverage", "",
             f"Every range of {header.meter_model} the specification file "
             f"declares, and what this run measured on it.  A range with no "
             f"points was not exercised at all.", "",
             "| function | wiring | range | points | PASS | FAIL | "
             "INDETERMINATE | other |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    untouched = []
    for quantity, connection, fs, counts in coverage:
        total = sum(counts.values())
        known = counts["PASS"] + counts["FAIL"] + counts["INDETERMINATE"]
        if not total:
            untouched.append(f"{quantity.name} {connection.value} "
                             f"{_q(fs, quantity.unit, 3)}")
        lines.append(
            f"| {quantity.name} | {connection.value} "
            f"| {_q(fs, quantity.unit, 3)} | {total or '—'} "
            f"| {counts['PASS'] or ''} | {counts['FAIL'] or ''} "
            f"| {counts['INDETERMINATE'] or ''} "
            f"| {(total - known) or ''} |")
    lines.append("")
    if untouched:
        lines.append(f"Not exercised: {', '.join(untouched)}.")
        lines.append("")
    return lines


def test_card_rows(header: RunHeader, rows: Sequence[ResultRow]):
    """``(manufacturer test, row or None)`` for every row of the DUT's table."""
    spec = _dut_meter_model(header)
    if spec is None:
        return []
    tests = manufacturer_tests(spec.model_id)
    if not tests:
        return []
    found: dict[str, ResultRow] = {}
    for row in _measured(rows):
        test = manufacturer_test_for(spec.model_id, row)
        if test is not None:
            found[test.test_id] = row
    return [(test, found.get(test.test_id)) for test in tests]


def _md_test_card(header: RunHeader, rows: Sequence[ResultRow]) -> list[str]:
    pairs = test_card_rows(header, rows)
    if not pairs or not any(row is not None for _test, row in pairs):
        return []
    lines = ["## Manufacturer test card", "",
             "The rows of the manufacturer's own performance-verification "
             "tables (3458A Calibration Manual Tables 4-1 to 4-6) that this "
             "run measured, with |error| against the 1-year limits printed on "
             "its test card.  **The verdict column is this run's own** "
             "(data-sheet tolerance, GUM budget, guard band); the card "
             "columns are a cross-check against the manufacturer's acceptance "
             "limits, which are stated for a transfer-standard comparison and "
             "differ from the data sheet in places "
             "(docs/3458a/cal-manual-verification.md).  `SUP` rows are not in "
             "the manual: they reach the ranges it skips.", "",
             "| test | setpoint | range | freq | |error| | card (Std) | "
             "card (Opt 002) | within card | TL (data sheet) | verdict |",
             "|---|---:|---:|---:|---:|---:|---:|---|---:|---|"]
    missing = []
    for test, row in pairs:
        unit = test.quantity.unit
        if row is None:
            missing.append(test.test_id)
            continue
        error = None if row.error is None else abs(float(row.error))
        if error is None or test.card_limit is None:
            within = "—"
        elif error <= test.card_limit_opt002:
            within = "yes"
        elif error <= test.card_limit:
            within = "Std only"
        else:
            within = "**no**"
        mode = f" {test.mode.upper()}" if test.mode else ""
        lines.append(
            f"| {test.test_id} | {_q(test.value, unit)}{mode} "
            f"| {_q(test.range_fs, unit, 3)} | {_freq(test.frequency_hz)} "
            f"| {_q(error, unit)} | {_q(test.card_limit, unit, blank='—')} "
            f"| {_q(test.card_limit_opt002, unit, blank='—')} | {within} "
            f"| {_q(row.tolerance_limit, unit)} "
            f"| {row.verdict.value.upper()} |")
    lines.append("")
    if missing:
        lines.append(f"Not measured in this run: {', '.join(missing)}.")
        lines.append("")
    return lines


def _md_points(rows: Sequence[ResultRow]) -> list[str]:
    lines = ["## Points", ""]
    if not rows:
        lines.append("The run recorded no points.")
        lines.append("")
        return lines
    lines.append("Error is measured − reference; ppm is of the reference and "
                 "is **blank at a zero point**, where \"ppm of reading\" means "
                 "nothing. U is at the decision confidence and k is Student-t "
                 "at ν_eff, not 2.")
    lines.append("")
    lines.append("| # | grp | label | quantity | setpoint | freq | conn | "
                 "reference | mean | error | error ppm | TL | TL ppm | U | k | "
                 "TUR | margin | verdict |")
    lines.append("|---:|---:|---|---|---:|---:|---|---:|---:|---:|---:|---:|"
                 "---:|---:|---:|---:|---:|---|")
    for row in _sorted_rows(rows):
        unit = row.unit
        lines.append(
            f"| {row.point_index} | {row.group_index} "
            f"| {_cell(row.label or '')} | {row.quantity.name} "
            f"| {_q(row.nominal, unit)} | {_freq(row.frequency_hz)} "
            f"| {row.connection.value} "
            f"| {_q(row.reference_value, unit)} | {_q(row.mean, unit)} "
            f"| {_q(row.error, unit)} | {_ppm(row.error_ppm)} "
            f"| {_q(row.tolerance_limit, unit)} | {_ppm(row.tolerance_ppm)} "
            f"| {_q(row.expanded_uncertainty, unit)} "
            f"| {_g(row.coverage_factor, 3)} | {_g(row.tur, 3)} "
            f"| {_pct(row.relative_margin)} "
            f"| {row.verdict.value.upper()} |")
    lines.append("")

    lines += _md_flags(rows)
    return lines


#: What a flag means, for the one-line summary of a flag every point carries.
_FLAG_WORDS: Mapping[str, str] = {
    FLAG_NO_TEMPERATURE_WINDOW:
        "neither model declares an absolute temperature window, so the "
        "governing effect is the tempco relative to each instrument's own "
        "stored calibration temperature — which is in the budget, not in the "
        "decision",
    FLAG_AMBIENT_UNKNOWN: "no ambient temperature was recorded",
    "reference-calibration-overdue":
        "the reference calibrator was past its own calibration interval; its "
        "UNCERT? is computed for an interval that has elapsed, so every "
        "verdict carries that caveat",
    "acal-condition-not-established":
        "the 3458A's figures hold only within 24 h and 1 C of its last ACAL, "
        "and that was not established for this point (run with --acal or "
        "--acal-recent)",
    "dut-typical-only":
        "the DUT's figure here is typical performance, not a specification, "
        "so no conformity statement is made",
    "lead-resistance-unknown":
        "the 2-wire reading includes test leads of unmeasured resistance, "
        "material against the tolerance (state --lead-resistance)",
    "lead-resistance-corrected":
        "the 2-wire reading was corrected by the stated test-lead resistance",
    FLAG_TEMPERATURE_OUT_OF_WINDOW:
        "the ambient was outside the specification window",
    FLAG_TUR_BELOW_FLOOR: "the reference was not good enough",
    FLAG_EMPTY_BUDGET: "no uncertainty components were supplied",
    FLAG_ZERO_UNCERTAINTY: "the budget combines to zero",
    FLAG_CONDITIONS_NOT_MET:
        "a stated condition of the specification was not shown to hold, so "
        "neither conformity nor non-conformity is stated",
    "source-not-warmed-up": "the reference had not finished warming up",
    "source-zeros-due": "the reference's dc zeros were overdue",
    "reference-temperature-out-of-window":
        "the reference was outside the temperature window its own figures are "
        "written for",
    "gross-check-not-applicable":
        "the point has neither a tolerance nor a reference uncertainty, so "
        "the gross-error check had no scale to work against",
    "source-tripped-mid-point":
        "the source tripped or faulted while the point was being measured",
    "meter-range-mismatch":
        "the meter's own range read-back disagreed with the range that was "
        "configured, above the hazard thresholds; nothing was energized",
    "prestep-failed":
        "the 10 % pre-step did not read back the level that was sourced",
    "wiring-proof-failed":
        "the safe-level wiring proof did not read back what was sourced",
    "wiring-proof-unreadable":
        "the safe-level wiring proof could not be read from the meter, so "
        "nothing was established about the wiring either way",
    "standby-not-confirmed":
        "the source did not confirm standby before the meter was to be "
        "re-ranged, so the output may still have been live when the run "
        "stopped",
    "gross-error-abort":
        "the reading was off by more than 10x the point's tolerance",
    "load-current-derated":
        "the point was read outside the calibrator's full-spec load-current "
        "window, so the manual's K-derating was applied to its figure — as an "
        "extra term on its uncertainty where the calibrator is the reference, "
        "and as a wider tolerance where the calibrator is the instrument "
        "under test; each point's plan note carries the arithmetic",
    "meter-warmup-unconfirmed":
        "the operator did not confirm the meter had been powered long enough "
        "for its own published specifications",
    "wiring-proof-not-resolvable":
        "no safe-level wiring proof was taken before this point: no level "
        "the meter can read to +/-10 % is also below the hazard thresholds, "
        "so a proof would have been either unjudgeable or itself the hazard "
        "it exists to prevent.  The point's note says which; the points "
        "themselves were measured normally",
    "thermal-emf-default":
        "no thermal-EMF bound was declared for this run, so the budget "
        "carries the default 1 uV rectangular bound at every zero and short "
        "-- a declared figure, not a measured one.  --thermal-emf overrides "
        "it, and 0 disables it",
    "thermal-emf-not-converted":
        "the thermal-EMF bound is in volts and this point is not, so it "
        "could not be converted into the point's own unit and was left out "
        "of the budget",
    "loading-corrected":
        "the reading was corrected for the divider the source's output "
        "impedance makes with the meter's input impedance; the correction "
        "and its own uncertainty are in the budget",
    "loading-uncorrected-above-100-khz":
        "the loading correction was not applied: above 100 kHz the reactive "
        "part dominates and the two resistances alone do not describe it",
    "tempco-not-evaluated":
        "a temperature coefficient is declared but the ambient or the "
        "instrument's own calibration temperature was unknown, so no tempco "
        "term could be computed",
    "reference-confidence-unknown":
        "the reference's uncertainty came without a stated confidence, so it "
        "was taken at the model's declared default rather than converted",
    "tolerance-limit-from-spec-table":
        "the tolerance judged against came from this project's specification "
        "table rather than the instrument's own UNCERT?",
    "source-range-mismatch":
        "the calibrator reported a different range from the one the plan "
        "pinned; the figures follow the readback and the flag says the plan "
        "disagreed",
    "no-repeat-uncertainty":
        "only one sample survived, so there is no Type A (repeatability) "
        "term in the budget",
}


def _md_flags(rows: Sequence[ResultRow]) -> list[str]:
    """Flags, with the ones every point carries collapsed to a single line.

    On the pairing this project exists to run, *every* row carries
    ``no-temperature-window``.  A 194-row table repeating it buries the three
    flags that actually distinguish a point, which is the only reason to print
    a flag table at all.
    """
    counts = Counter(flag for row in rows for flag in row.flags)
    universal = sorted(flag for flag, count in counts.items()
                       if count == len(rows) and len(rows) > 1)
    per_point = [(row, tuple(f for f in row.flags if f not in universal))
                 for row in _sorted_rows(rows)]
    per_point = [(row, flags) for row, flags in per_point if flags]
    if not counts:
        return []

    lines = ["### Flags", ""]
    for flag in universal:
        lines.append(f"- `{flag}`: **all {len(rows)} points** — "
                     f"{_FLAG_WORDS.get(flag, 'carried by every point')}")
    if universal:
        lines.append("")
    if per_point:
        lines.append("| # | label | flags |")
        lines.append("|---:|---|---|")
        for row, flags in per_point:
            lines.append(f"| {row.point_index} "
                         f"| {_cell(row.label or row.quantity.name)} "
                         f"| {_cell(', '.join(flags))} |")
        lines.append("")
    elif universal:
        lines.append("No point carries a flag the others do not.")
        lines.append("")
    return lines


def _md_faults(faults: Sequence[Fault]) -> list[str]:
    if not faults:
        return []
    lines = ["## Instrument faults", ""]
    lines.append("Decoded offline from `cal/data/fluke5700a_faults.yaml`. "
                 "Codes are grouped by the assembly their text names — several "
                 "codes against one assembly is one repair, not several. A "
                 "code naming two (the `A14/A15` HV preamp block) is listed "
                 "under both, so either assembly can be traced from its own "
                 "heading.")
    lines.append("")
    for assembly, group in group_faults(faults):
        title = assembly or "no assembly named"
        lines.append(f"### {title} ({_plural(len(group), 'fault')})")
        lines.append("")
        for fault in sorted(group, key=Fault.sort_key):
            code = "?" if fault.code is None else str(fault.code)
            others = [name for name in fault.assemblies if name != assembly]
            also = (f" *(also names {', '.join(others)})*" if others else "")
            lines.append(f"- `{code}` — {_prose(fault.text)}{also}")
        lines.append("")
    return lines


def _md_budgets(rows: Sequence[ResultRow], *, title: str = "Uncertainty budgets",
                note: str | None = None) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("No budgets to show.")
        lines.append("")
        return lines
    lines.append("Every budget below is **reconstructed from the `Budget` cell "
                 "of its own row**; u_c, ν_eff, k and U are recomputed from "
                 "those components, never read back from the file.")
    if note:
        lines.append("")
        lines.append(note)
    lines.append("")
    for row in _sorted_rows(rows):
        name = row.label or f"{row.quantity.name} {_q(row.nominal, row.unit)}"
        lines.append(f"### Point {row.point_index} — {_cell(name)} "
                     f"[{row.verdict.value.upper()}]")
        lines.append("")
        dominant = dominant_contribution(row)
        if dominant is not None:
            lines.append(f"Dominated by: **{_cell(dominant[0])}** "
                         f"({dominant[1]:.0f} % of the variance).")
        else:
            lines.append("**No uncertainty components were supplied for this "
                         "point.**")
        lines.append("")
        lines.append("```")
        lines.append(format_budget(row.budget, row.unit,
                                   confidence=row.confidence))
        lines.append("```")
        lines.append("")
        if row.note:
            lines.append(f"Plan note: {_prose(row.note)}")
            lines.append("")
        if row.reason:
            lines.append(f"Decision: {_prose(row.reason)}")
            lines.append("")
    return lines


def _md_compare(before_path: str, matched: Sequence[PointDelta],
                only_before: Sequence[ResultRow],
                only_after: Sequence[ResultRow]) -> list[str]:
    lines = [f"## Comparison with `{os.path.basename(before_path)}`", ""]
    improved = [d for d in matched if d.improved]
    regressed = [d for d in matched if d.regressed]
    changed = [d for d in matched if d.changed]
    lines.append(f"{len(matched)} points matched by identity "
                 f"(quantity, setpoint, frequency, connection, range); "
                 f"{len(changed)} changed verdict "
                 f"({len(improved)} no longer FAIL, {len(regressed)} newly "
                 f"FAIL).")
    if only_before or only_after:
        lines.append("")
        lines.append(f"{len(only_before)} point(s) only in the earlier run, "
                     f"{len(only_after)} only in this one — the two runs are "
                     f"not the same plan.")
    lines.append("")
    if matched:
        lines.append("| # | label | error before | error after | Δerror | "
                     "Δ(error/TL) | verdict |")
        lines.append("|---:|---|---:|---:|---:|---:|---|")
        for delta in matched:
            unit = delta.unit
            transition = (delta.transition if delta.changed
                          else delta.after.verdict.value.upper())
            lines.append(
                f"| {delta.after.point_index} | {_cell(delta.label)} "
                f"| {_q(delta.before.error, unit)} "
                f"| {_q(delta.after.error, unit)} "
                f"| {_q(delta.error_delta, unit)} "
                f"| {_g(delta.ratio_delta, 3)} | {transition} |")
        lines.append("")
    for row in only_before:
        lines.append(f"- only in the earlier run: point {row.point_index} "
                     f"{_cell(row.label or row.quantity.name)}")
    for row in only_after:
        lines.append(f"- only in this run: point {row.point_index} "
                     f"{_cell(row.label or row.quantity.name)}")
    if only_before or only_after:
        lines.append("")
    return lines


def format_markdown(path: str, header: RunHeader, rows: Sequence[ResultRow],
                    *, compare: tuple[str, Sequence[ResultRow]] | None = None,
                    spec_ratio_warn: float = SPEC_RATIO_WARN) -> str:
    """The whole markdown report, as one string."""
    summary = summarize_run(rows,
                            temperature_policy=policy_of(header)[0])
    ratios = spec_ratio_summary(rows, threshold=spec_ratio_warn)
    faults = parse_faults(header)

    lines: list[str] = []
    lines += _md_title(path, header, rows, summary)
    lines += _md_warnings(header, rows, summary, ratios, faults)
    lines += _md_provenance(path, header, rows)
    lines += _md_verdicts(rows, summary, header)
    lines += _md_coverage(header, rows)
    lines += _md_test_card(header, rows)
    if compare is not None:
        before_path, before_rows = compare
        matched, only_before, only_after = compare_runs(before_rows, rows)
        lines += _md_compare(before_path, matched, only_before, only_after)
    lines += _md_points(rows)
    lines += _md_faults(faults)
    lines += _md_budgets(rows)
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# Markdown -> aligned plain text, for the PDF's text pages
# --------------------------------------------------------------------------

def _align_tables(lines: Sequence[str]) -> list[str]:
    """Turn markdown pipe tables into column-aligned plain text.

    The PDF renders the same report the markdown file carries; pipe tables read
    badly in a monospace block unless their columns line up, and aligning them
    here keeps one report renderer rather than two.
    """
    # A report "line" may itself be a multi-line block -- ``format_budget``
    # returns its whole table as one string -- and folding that at the page
    # width instead of at its own newlines is how a budget turns into
    # confetti.  Expand first, then look for tables.
    lines = [part for raw in lines for part in str(raw).split("\n")]
    out: list[str] = []
    block: list[list[str]] = []

    def flush() -> None:
        if not block:
            return
        width = max(len(row) for row in block)
        rows = [row + [""] * (width - len(row)) for row in block]
        widths = [max(len(row[i]) for row in rows) for i in range(width)]
        for row in rows:
            out.append("  ".join(cell.ljust(widths[i])
                                 for i, cell in enumerate(row)).rstrip())
        block.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip().replace("\\|", "|").replace("**", "")
                     .replace("`", "")
                     for c in stripped.strip("|").split("|")]
            if all(set(cell) <= set("-: ") and cell for cell in cells):
                continue                      # the |---|---| separator row
            block.append(cells)
            continue
        flush()
        if stripped == "```":
            continue
        out.append(line.replace("**", "").replace("`", ""))
    flush()
    return out


def _wrap(lines: Sequence[str], width: int) -> list[str]:
    out: list[str] = []
    for raw in lines:
        for line in str(raw).split("\n"):
            while len(line) > width:
                out.append(line[:width])
                line = "    " + line[width:]
            out.append(line)
    return out


def _text_pages(pdf, plt, lines: Sequence[str], *,
                landscape: bool = False) -> int:
    """Render *lines* as monospace pages.  Returns the number of pages."""
    figsize, width, per_page = (_PDF_LANDSCAPE if landscape
                                else _PDF_PORTRAIT)
    prepared = _wrap([str(line) for line in lines], width)
    pages = 0
    for start in range(0, max(len(prepared), 1), per_page):
        chunk = prepared[start:start + per_page]
        fig = plt.figure(figsize=figsize)
        fig.text(0.03, 0.975, "\n".join(chunk), va="top", ha="left",
                 family="monospace", fontsize=6.5, linespacing=1.35)
        pdf.savefig(fig)
        plt.close(fig)
        pages += 1
    return pages


# --------------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------------

def _verdict_legend(ax, present: Iterable[Verdict]) -> None:
    """Add one legend entry per verdict actually plotted, keeping the others."""
    from matplotlib.lines import Line2D

    seen = set(present)                 # materialised: *present* is a generator
    handles = list(ax.get_legend_handles_labels()[0])
    handles += [Line2D([], [], marker="o", linestyle="none",
                       color=_VERDICT_COLOURS[v], label=v.value.upper())
                for v in _VERDICT_ORDER if v in seen]
    if handles:
        ax.legend(handles=handles, fontsize=8, loc="best")


def _symlog(ax, values: Sequence[float]) -> None:
    """Symmetric-log y axis, so a 1 uV point and a 1 mV point share a page."""
    finite = [abs(v) for v in values if v is not None and math.isfinite(v)
              and v != 0.0]
    if not finite:
        return
    ax.set_yscale("symlog", linthresh=min(finite))


def _plot_error_vs_index(pdf, plt, rows: Sequence[ResultRow]) -> int:
    """Error against the tolerance and acceptance bands, one axes per unit."""
    units: dict[str, list[ResultRow]] = {}
    for row in _sorted_rows(rows):
        if row.error is not None:
            units.setdefault(row.unit, []).append(row)
    if not units:
        return 0

    fig, axes = plt.subplots(len(units), 1, figsize=(11, 3.2 * len(units) + 1),
                             squeeze=False)
    for axis, (unit, group) in zip(axes[:, 0], sorted(units.items())):
        x = [row.point_index for row in group]
        errors = [row.error for row in group]
        limits = [row.tolerance_limit for row in group]
        accepts = [row.acceptance_limit for row in group]
        if any(limit is not None for limit in limits):
            axis.step(x, [limit if limit is not None else math.nan
                          for limit in limits], where="mid", color="tab:red",
                      lw=0.9, label="±TL")
            axis.step(x, [-limit if limit is not None else math.nan
                          for limit in limits], where="mid", color="tab:red",
                      lw=0.9)
        if any(a is not None for a in accepts):
            axis.step(x, [a if a is not None else math.nan for a in accepts],
                      where="mid", color="tab:blue", lw=0.8, ls="--",
                      label="±AL = TL − w")
            axis.step(x, [-a if a is not None else math.nan for a in accepts],
                      where="mid", color="tab:blue", lw=0.8, ls="--")
        for verdict in _VERDICT_ORDER:
            xs = [row.point_index for row in group if row.verdict is verdict]
            ys = [row.error for row in group if row.verdict is verdict]
            if xs:
                axis.plot(xs, ys, "o", ms=4,
                          color=_VERDICT_COLOURS[verdict])
        axis.axhline(0, color="k", lw=0.5)
        _symlog(axis, errors + [limit for limit in limits
                                if limit is not None])
        axis.set_ylabel(f"error ({unit})")
        axis.set_xlabel("point")
        axis.grid(True, which="both", alpha=0.3)
        axis.set_title(f"error vs point — {unit}", fontsize=10)
        _verdict_legend(axis, (row.verdict for row in group))
    fig.suptitle("Error against the tolerance and acceptance limits")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_error_ratio(pdf, plt, rows: Sequence[ResultRow]) -> int:
    """error / TL: 1.0 is the edge whatever the range, the unit or the point."""
    usable = [row for row in _sorted_rows(rows)
              if _error_ratio(row) is not None]
    if not usable:
        return 0
    fig, ax = plt.subplots(figsize=(11, 6))
    x = [row.point_index for row in usable]
    ax.step(x, [_acceptance_ratio(row) or math.nan for row in usable],
            where="mid", color="tab:blue", lw=0.8, ls="--",
            label="acceptance limit AL/TL")
    ax.step(x, [-(_acceptance_ratio(row) or math.nan) for row in usable],
            where="mid", color="tab:blue", lw=0.8, ls="--")
    ax.axhline(1.0, color="tab:red", lw=1.0, label="tolerance limit")
    ax.axhline(-1.0, color="tab:red", lw=1.0)
    ax.axhline(0.0, color="k", lw=0.5)
    for verdict in _VERDICT_ORDER:
        xs = [row.point_index for row in usable if row.verdict is verdict]
        ys = [_error_ratio(row) for row in usable if row.verdict is verdict]
        if xs:
            ax.plot(xs, ys, "o", ms=5, color=_VERDICT_COLOURS[verdict])
    ax.set_yscale("symlog", linthresh=1.0)
    ax.set_xlabel("point")
    ax.set_ylabel("error / tolerance limit")
    ax.set_title("Error as a fraction of its own limit — 1.0 is the edge, "
                 "for every range and unit")
    ax.grid(True, which="both", alpha=0.3)
    _verdict_legend(ax, (row.verdict for row in usable))
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_tur(pdf, plt, rows: Sequence[ResultRow], floor: float) -> int:
    usable = [row for row in _sorted_rows(rows)
              if row.tur is not None and row.tur > 0.0
              and math.isfinite(row.tur)]
    if not usable:
        return 0
    fig, ax = plt.subplots(figsize=(11, 6))
    for verdict in _VERDICT_ORDER:
        xs = [row.point_index for row in usable if row.verdict is verdict]
        ys = [row.tur for row in usable if row.verdict is verdict]
        if xs:
            ax.semilogy(xs, ys, "o", ms=5, color=_VERDICT_COLOURS[verdict])
    if floor > 0:
        ax.axhline(floor, color="tab:red", lw=1.2,
                   label=f"TUR floor {floor:g}:1")
        ax.axhspan(min(row.tur for row in usable) / 2.0, floor,
                   color="tab:red", alpha=0.06)
    ax.axhline(1.0, color="k", lw=0.6, ls=":",
               label="1:1 — reference as bad as the DUT")
    ax.set_xlabel("point")
    ax.set_ylabel("TUR = TL / U")
    ax.set_title("Test uncertainty ratio per point")
    ax.grid(True, which="both", alpha=0.3)
    _verdict_legend(ax, (row.verdict for row in usable))
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_frequency(pdf, plt, rows: Sequence[ResultRow]) -> int:
    ac = [row for row in _sorted_rows(rows)
          if row.frequency_hz and row.frequency_hz > 0.0
          and _error_ratio(row) is not None]
    if not ac:
        return 0
    fig, ax = plt.subplots(figsize=(11, 6))
    by_quantity: dict[str, list[ResultRow]] = {}
    for row in ac:
        by_quantity.setdefault(row.quantity.name, []).append(row)
    for name, group in sorted(by_quantity.items()):
        group = sorted(group, key=lambda row: row.frequency_hz)
        ax.semilogx([row.frequency_hz for row in group],
                    [_error_ratio(row) for row in group], "o-", ms=4, lw=0.8,
                    alpha=0.6, label=name)
    for verdict in (Verdict.FAIL, Verdict.INDETERMINATE):
        xs = [row.frequency_hz for row in ac if row.verdict is verdict]
        ys = [_error_ratio(row) for row in ac if row.verdict is verdict]
        if xs:
            ax.semilogx(xs, ys, "o", ms=8, mfc="none",
                        color=_VERDICT_COLOURS[verdict],
                        label=verdict.value.upper())
    ax.axhline(1.0, color="tab:red", lw=1.0)
    ax.axhline(-1.0, color="tab:red", lw=1.0)
    ax.axhline(0.0, color="k", lw=0.5)
    ax.set_xlabel("frequency (Hz)")
    ax.set_ylabel("error / tolerance limit")
    ax.set_title("Frequency response of the error, as a fraction of the limit")
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(fontsize=8, loc="best")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_function_summary(pdf, plt, rows: Sequence[ResultRow]) -> int:
    if not rows:
        return 0
    by_quantity: dict[str, Counter] = {}
    for row in rows:
        by_quantity.setdefault(row.quantity.name, Counter())[row.verdict] += 1
    names = sorted(by_quantity)
    fig, axes = plt.subplots(1, 2, figsize=(12, 1.0 + 0.6 * len(names) + 2))

    ax = axes[0]
    bottom = [0.0] * len(names)
    for verdict in _VERDICT_ORDER:
        values = [by_quantity[name][verdict] for name in names]
        ax.barh(names, values, left=bottom, color=_VERDICT_COLOURS[verdict],
                label=verdict.value.upper())
        bottom = [b + v for b, v in zip(bottom, values)]
    ax.set_xlabel("points")
    ax.set_title("verdicts per quantity")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, axis="x", alpha=0.3)

    ax = axes[1]
    worst = []
    for name in names:
        margins = [row.relative_margin for row in rows
                   if row.quantity.name == name
                   and row.relative_margin is not None]
        worst.append(min(margins) if margins else math.nan)
    ax.barh(names, worst, color="tab:blue")
    ax.axvline(0.0, color="tab:red", lw=1.0)
    ax.set_xlabel("worst margin / TL (0 = on the limit)")
    ax.set_title("closest approach to the limit per quantity")
    ax.grid(True, axis="x", alpha=0.3)

    fig.suptitle("Per-function summary")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_temperature(pdf, plt, header: RunHeader,
                      rows: Sequence[ResultRow]) -> int:
    """The run's thermal picture: ambient, RH, meter internals, source Tcal.

    Skipped -- honestly, not with an empty axes -- when no per-point series
    exists: the source's ``CAL_TEMP?`` alone is one number, and a page with a
    single horizontal line on it would be decoration.
    """
    ordered = _sorted_rows(rows)
    ambient = [(row.point_index, row.ambient_c) for row in ordered
               if row.ambient_c is not None]
    meter = [(row.point_index, row.meter_temp_c) for row in ordered
             if row.meter_temp_c is not None]
    humidity = [(row.point_index, row.humidity_pct) for row in ordered
                if row.humidity_pct is not None]
    if not ambient and not meter:
        return 0

    fig, ax = plt.subplots(figsize=(11, 6))
    if ambient:
        ax.plot([x for x, _ in ambient], [y for _, y in ambient], "o-",
                ms=3, lw=0.8, color="tab:blue", label="ambient (°C)")
    if meter:
        ax.plot([x for x, _ in meter], [y for _, y in meter], "s-",
                ms=3, lw=0.8, color="tab:orange",
                label="meter internal (°C)")
    if header.source_cal_temp_c is not None:
        ax.axhline(header.source_cal_temp_c, color="tab:red", lw=1.0,
                   ls="--",
                   label=f"source CAL_TEMP? "
                         f"{_g(header.source_cal_temp_c)} °C")
    ax.set_xlabel("point")
    ax.set_ylabel("temperature (°C)")
    ax.grid(True, alpha=0.3)

    handles, labels = ax.get_legend_handles_labels()
    if humidity:
        twin = ax.twinx()
        twin.plot([x for x, _ in humidity], [y for _, y in humidity], "^-",
                  ms=3, lw=0.8, color="tab:green", alpha=0.6,
                  label="humidity (%RH)")
        twin.set_ylabel("relative humidity (%)")
        more, more_labels = twin.get_legend_handles_labels()
        handles += more
        labels += more_labels
    ax.legend(handles, labels, fontsize=8, loc="best")
    ax.set_title("Temperature over the run — ambient, meter internal, and "
                 "the source's assumed cal temperature")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


def _plot_compare(pdf, plt, matched: Sequence[PointDelta],
                  before_path: str) -> int:
    usable = [d for d in matched if d.ratio_delta is not None]
    if not usable:
        return 0
    fig, ax = plt.subplots(figsize=(11, 6))
    x = [d.after.point_index for d in usable]
    colours = ["tab:red" if d.regressed else
               "tab:green" if d.improved else
               "tab:orange" if d.changed else "tab:blue" for d in usable]
    ax.bar(x, [d.ratio_delta for d in usable], color=colours)
    ax.axhline(0.0, color="k", lw=0.6)
    ax.set_xlabel("point")
    ax.set_ylabel("Δ(error / TL), this run − earlier run")
    ax.set_title(f"Change since {os.path.basename(before_path)} "
                 f"(green: no longer FAIL, red: newly FAIL, "
                 f"orange: other verdict change)")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)
    return 1


# --------------------------------------------------------------------------
# The PDF
# --------------------------------------------------------------------------

def render_pdf(path: str, header: RunHeader, rows: Sequence[ResultRow],
               out_pdf: str, *, full_budgets: bool = False,
               compare: tuple[str, Sequence[ResultRow]] | None = None,
               spec_ratio_warn: float = SPEC_RATIO_WARN) -> int:
    """Write the PDF report.  Returns the number of pages written.

    Headless by construction: the Agg backend is selected here, so importing
    this module never touches a display and the test suite never opens a
    window.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    summary = summarize_run(rows,
                            temperature_policy=policy_of(header)[0])
    ratios = spec_ratio_summary(rows, threshold=spec_ratio_warn)
    faults = parse_faults(header)

    front: list[str] = []
    front += _md_title(path, header, rows, summary)
    front += _md_warnings(header, rows, summary, ratios, faults)
    front += _md_provenance(path, header, rows)
    front += _md_verdicts(rows, summary, header)
    front += _md_coverage(header, rows)
    front += _md_test_card(header, rows)
    front += _md_faults(faults)

    if full_budgets:
        budget_rows = list(rows)
        note = None
    else:
        budget_rows = [row for row in rows
                       if row.verdict in (Verdict.FAIL, Verdict.INDETERMINATE)]
        note = ("Only the FAIL and INDETERMINATE points are shown here; "
                "the markdown report carries every budget, and `--full-budgets` "
                "puts them all in the PDF.")

    pages = 0
    with PdfPages(out_pdf) as pdf:
        pages += _text_pages(pdf, plt, _align_tables(front))
        pages += _plot_error_vs_index(pdf, plt, rows)
        pages += _plot_error_ratio(pdf, plt, rows)
        pages += _plot_tur(pdf, plt, rows, header.tur_floor)
        pages += _plot_frequency(pdf, plt, rows)
        pages += _plot_function_summary(pdf, plt, rows)
        pages += _plot_temperature(pdf, plt, header, rows)
        if compare is not None:
            before_path, before_rows = compare
            matched, only_before, only_after = compare_runs(before_rows, rows)
            pages += _plot_compare(pdf, plt, matched, before_path)
            pages += _text_pages(pdf, plt, _align_tables(
                _md_compare(before_path, matched, only_before, only_after)),
                landscape=True)
        pages += _text_pages(pdf, plt, _align_tables(_md_points(rows)),
                             landscape=True)
        pages += _text_pages(pdf, plt, _align_tables(
            _md_budgets(budget_rows, note=note)))
    return pages


# --------------------------------------------------------------------------
# The calibration-cycle report (--adjust-log)
# --------------------------------------------------------------------------
#
# An adjustment session writes a text record; the verification runs around it
# write result CSVs.  This mode binds the three into one document: the session
# record verbatim, and -- when the as-found and as-left runs are given or
# discoverable from the as-left header -- the before/after comparison.  It is
# kept honest: a log alone yields a text-only report that says so, rather than
# inventing plots out of a file that carries no measurements.

def adjust_output_paths(log_path: str, output: str | None = None,
                        markdown: str | None = None) -> tuple[str, str]:
    """``(pdf, markdown)`` next to the adjustment log, unless overridden."""
    stem = os.path.splitext(log_path)[0]
    return (output or stem + ".pdf", markdown or stem + "-report.md")


def _run_line(label: str, run: tuple | None, absent: str) -> list[str]:
    if run is None:
        return [f"- {label}: {absent}"]
    path, header, rows = run
    summary = summarize_run(rows, temperature_policy=policy_of(header)[0])
    return [f"- {label}: `{os.path.basename(path)}` "
            f"(run id `{header.run_id or 'unknown'}`, "
            f"started {_stamp(header.started)})",
            f"  - {summary.line()}"]


def _adjust_front(log_path: str, log_text: str, as_found: tuple | None,
                  as_left: tuple | None) -> list[str]:
    """Title, linkage and the session record -- shared by markdown and PDF."""
    lines = [f"# Calibration cycle — {os.path.basename(log_path)}", ""]
    lines.append(f"- adjustment record: `{os.path.abspath(log_path)}`")
    lines += _run_line("as-found verification", as_found,
                       "**not given** — how far the instrument had drifted "
                       "is not part of this report")
    lines += _run_line("as-left verification", as_left,
                       "**not given** — the state the instrument was "
                       "released in is not part of this report")
    if as_found is None or as_left is None:
        lines.append("")
        lines.append("**This is a text-only record of the adjustment "
                     "session.** A before/after comparison needs both the "
                     "as-found and the as-left verification CSVs; pass them "
                     "with `--as-found`/`--as-left` (an as-left file that "
                     "names its counterpart is found on its own).")
    lines.append("")
    lines.append("## Adjustment session record")
    lines.append("")
    lines.append("```")
    lines.append(log_text.rstrip("\n"))
    lines.append("```")
    lines.append("")
    return lines


def format_adjust_markdown(log_path: str, log_text: str, *,
                           as_found: tuple | None = None,
                           as_left: tuple | None = None) -> str:
    """The calibration-cycle report as markdown.

    *as_found* / *as_left* are ``(path, header, rows)`` triples or ``None``.
    The comparison is rendered by the same :func:`_md_compare` a verification
    report uses, so the two documents cannot disagree about a delta.
    """
    lines = _adjust_front(log_path, log_text, as_found, as_left)
    if as_found is not None and as_left is not None:
        before_path, _, before_rows = as_found
        _, _, after_rows = as_left
        matched, only_before, only_after = compare_runs(before_rows,
                                                        after_rows)
        lines.append("## Before/after")
        lines.append("")
        lines.append("Errors are `after − before` per matched point; a "
                     "negative Δ(error/TL) moved the point away from its "
                     "limit.")
        lines.append("")
        lines += _md_compare(before_path, matched, only_before, only_after)
    return "\n".join(lines).rstrip() + "\n"


def render_adjust_pdf(log_path: str, log_text: str, out_pdf: str, *,
                      as_found: tuple | None = None,
                      as_left: tuple | None = None) -> int:
    """Write the calibration-cycle PDF.  Returns the number of pages.

    A log alone yields text pages only -- the front matter says so in words.
    With both runs, the comparison plot and table are appended, reusing the
    verification report's own scaffolding.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    pages = 0
    with PdfPages(out_pdf) as pdf:
        pages += _text_pages(pdf, plt, _align_tables(
            _adjust_front(log_path, log_text, as_found, as_left)))
        if as_found is not None and as_left is not None:
            before_path, _, before_rows = as_found
            _, _, after_rows = as_left
            matched, only_before, only_after = compare_runs(before_rows,
                                                            after_rows)
            pages += _plot_compare(pdf, plt, matched, before_path)
            pages += _text_pages(pdf, plt, _align_tables(
                _md_compare(before_path, matched, only_before, only_after)),
                landscape=True)
    return pages


def _load_named_run(path: str, option: str) -> tuple:
    """``(path, header, rows)`` for a run named on the command line."""
    header, rows = load_results(path)
    expected = {"--as-found": "as-left", "--as-left": "as-found"}.get(option)
    if expected is not None and header.run_kind == expected:
        print(f"  WARNING: {path} records run_kind={header.run_kind!r} but "
              f"was passed as {option}; the comparison direction follows "
              f"the flags, not the file", file=sys.stderr)
    return path, header, rows


def _run_adjust_log(args: argparse.Namespace) -> int:
    """The ``--adjust-log`` mode of :func:`run`."""
    if args.csv:
        print("--adjust-log is its own mode: name the verification runs "
              "with --as-found/--as-left, not as a positional CSV",
              file=sys.stderr)
        return 2
    if args.compare:
        print("--compare belongs to the verification report; with "
              "--adjust-log the pair is --as-found/--as-left",
              file=sys.stderr)
        return 2
    for path in (args.adjust_log, args.as_found, args.as_left):
        if path and not os.path.exists(path):
            print(f"no such file: {path}", file=sys.stderr)
            return 2

    with open(args.adjust_log, "r", encoding="utf-8") as handle:
        log_text = handle.read()

    as_found = (_load_named_run(args.as_found, "--as-found")
                if args.as_found else None)
    as_left = (_load_named_run(args.as_left, "--as-left")
               if args.as_left else None)
    # An as-left run knows its as-found counterpart; the reverse direction is
    # not discoverable (the as-found was written before the pair existed).
    if as_left is not None and as_found is None:
        counterpart, warning = find_counterpart(as_left[0], as_left[1])
        if warning:
            print(f"  WARNING: {warning}", file=sys.stderr)
        elif counterpart:
            header, rows = load_results(counterpart)
            as_found = (counterpart, header, rows)
            print(f"  as-found: {counterpart} (named by the as-left header)")

    pdf_path, md_path = adjust_output_paths(args.adjust_log, args.output,
                                            args.markdown)
    text = format_adjust_markdown(args.adjust_log, log_text,
                                  as_found=as_found, as_left=as_left)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    if as_found is not None and as_left is not None:
        matched, only_before, only_after = compare_runs(as_found[2],
                                                        as_left[2])
        improved = sum(1 for d in matched if d.improved)
        regressed = sum(1 for d in matched if d.regressed)
        print(f"{os.path.basename(args.adjust_log)}: {len(matched)} points "
              f"compared, {improved} no longer FAIL, {regressed} newly FAIL")
        diff_path = compare_diff_path(args.adjust_log)
        write_compare_csv(diff_path, matched, only_before, only_after)
        print(f"diff  : {diff_path}")
    else:
        print(f"{os.path.basename(args.adjust_log)}: text-only record — no "
              f"paired verification runs to compare")
    if args.print_report:
        print()
        print(text)
    print(f"report: {md_path}")
    if not args.no_pdf:
        pages = render_adjust_pdf(args.adjust_log, log_text, pdf_path,
                                  as_found=as_found, as_left=as_left)
        print(f"plots : {pdf_path} ({_plural(pages, 'page')})")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def output_paths(csv_path: str, output: str | None = None,
                 markdown: str | None = None) -> tuple[str, str]:
    """``(pdf, markdown)``, derived from the CSV basename unless overridden."""
    stem = os.path.splitext(csv_path)[0]
    return (output or stem + ".pdf", markdown or stem + "-report.md")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caldmm-cal-analyze",
        description="Offline PDF and markdown reports from a caldmm "
                    "verification result CSV. Reads nothing but the file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit status: 0 the report was written, 2 a named file does "
               "not exist.\n"
               "A file full of FAILs still exits 0 -- this reports on a run, "
               "it does not\n"
               "perform one, and it is re-run over the same file routinely. "
               "The verdict\n"
               "counts are the first line of stdout. caldmm-cal-verify, "
               "which does perform\n"
               "the run, exits 1 when it finds a FAIL.",
    )
    parser.add_argument("csv", nargs="?", default=None,
                        help="result CSV written by the verification runner "
                             "(omitted only with --adjust-log)")
    parser.add_argument("-o", "--output", default=None,
                        help="output PDF (default: <csv>.pdf)")
    parser.add_argument("--markdown", default=None,
                        help="output markdown (default: <csv>-report.md)")
    parser.add_argument("--no-pdf", action="store_true",
                        help="write only the markdown report")
    parser.add_argument("--full-budgets", action="store_true",
                        help="put every point's uncertainty budget in the PDF, "
                             "not only the FAIL and INDETERMINATE ones "
                             "(the markdown always carries them all)")
    parser.add_argument("--compare", metavar="BEFORE.csv", default=None,
                        help="an earlier run of the same plan; adds per-point "
                             "error deltas and verdict transitions.  Without "
                             "it, an as-left file that names its as-found "
                             "counterpart is compared against it "
                             "automatically (a named-but-missing counterpart "
                             "is a warning, not an error)")
    cycle = parser.add_argument_group(
        "calibration cycle (--adjust-log)",
        "Render an adjustment session record — with the before/after "
        "comparison when the paired verification runs are given or "
        "discoverable — into a PDF and markdown next to the log.  A log "
        "alone yields a text-only report that says so.")
    cycle.add_argument("--adjust-log", metavar="RECORD.log", default=None,
                       help="the session record written by caldmm-cal-verify "
                            "--adjust (or caldmm-5700a-selfcal)")
    cycle.add_argument("--as-found", metavar="BEFORE.csv", default=None,
                       help="the verification run measured before the "
                            "adjustment")
    cycle.add_argument("--as-left", metavar="AFTER.csv", default=None,
                       help="the verification run measured after the "
                            "adjustment; if it names its as-found "
                            "counterpart in its header, --as-found may be "
                            "omitted")
    parser.add_argument("--spec-ratio-warn", type=float,
                        default=SPEC_RATIO_WARN, metavar="RATIO",
                        help="warn about points whose plan spec ratio (DUT "
                             "limit / reference limit) is at or below this "
                             f"(default {SPEC_RATIO_WARN:g}: the reference is "
                             "no better than the DUT)")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="also print the whole markdown report to stdout")
    return parser


def run(args: argparse.Namespace) -> int:
    """Analyze one result file (or one adjustment log).  Returns an exit status."""
    if getattr(args, "adjust_log", None):
        return _run_adjust_log(args)
    if getattr(args, "as_found", None) or getattr(args, "as_left", None):
        print("--as-found/--as-left belong to --adjust-log; a verification "
              "report takes --compare", file=sys.stderr)
        return 2
    if not args.csv:
        print("a result CSV is required (or --adjust-log RECORD.log)",
              file=sys.stderr)
        return 2
    if not os.path.exists(args.csv):
        print(f"no such file: {args.csv}", file=sys.stderr)
        return 2

    header, rows = load_results(args.csv)
    summary = summarize_run(rows,
                            temperature_policy=policy_of(header)[0])
    ratios = spec_ratio_summary(rows, threshold=args.spec_ratio_warn)
    faults = parse_faults(header)

    compare = None
    if args.compare:
        if not os.path.exists(args.compare):
            print(f"no such file: {args.compare}", file=sys.stderr)
            return 2
        _, before_rows = load_results(args.compare)
        compare = (args.compare, before_rows)
    else:
        # An as-left run that names its as-found counterpart is compared
        # against it without being asked; an explicit --compare always wins.
        counterpart, warning = find_counterpart(args.csv, header)
        if warning:
            print(f"  WARNING: {warning}", file=sys.stderr)
        elif counterpart:
            _, before_rows = load_results(counterpart)
            compare = (counterpart, before_rows)
            print(f"  comparing against the as-found counterpart "
                  f"{counterpart} (named by the header; --compare overrides)")

    pdf_path, md_path = output_paths(args.csv, args.output, args.markdown)

    text = format_markdown(args.csv, header, rows, compare=compare,
                           spec_ratio_warn=args.spec_ratio_warn)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    # -- what the operator sees without opening anything
    print(f"{os.path.basename(args.csv)}: {summary.line()}")
    causes = indeterminate_causes(summary)
    policy, policy_known = policy_of(header)
    if summary[Verdict.INDETERMINATE]:
        statuses, blocked = temperature_breakdown(rows, policy)
        carried_only = sum(statuses.values()) - blocked
        print(f"  indeterminate: {causes['tur_floor']} below the TUR floor "
              f"(reference not good enough), {causes['conditions']} outside "
              f"the specification's stated conditions "
              f"({causes['temperature']} of them temperature), "
              f"{causes['guard_band']} in the guard band, "
              f"{causes['no_budget']} with no budget "
              f"— causes overlap")
        if carried_only:
            print(f"  ({carried_only} more {_verb(carried_only)} a "
                  f"temperature condition the {policy!r} policy does not "
                  f"block on; see the report)")
    if not policy_known:
        print(f"  WARNING: unrecognised temperature policy "
              f"{header.temperature_policy!r}; counts taken under {policy!r}")
    if ratios.low:
        print(f"  WARNING: the reference is no better than the DUT at "
              f"{len(ratios.low)} of {ratios.total} points "
              f"({ratios.fraction * 100:.0f} %); those points measure "
              f"{_reference_label(header)}, not {_dut_label(header)}")
    if not header.ambient_known and not any(row.ambient_c is not None
                                            for row in rows):
        print("  WARNING: no ambient temperature was recorded for this run")
    for assembly, group in group_faults(faults):
        print(f"  fault: {assembly or 'no assembly named'}: "
              + ", ".join(f.line() for f in sorted(group, key=Fault.sort_key)))

    if args.print_report:
        print()
        print(text)

    if compare is not None:
        before_path, before_rows = compare
        matched, only_before, only_after = compare_runs(before_rows, rows)
        diff_path = compare_diff_path(args.csv)
        write_compare_csv(diff_path, matched, only_before, only_after)
        print(f"diff  : {diff_path} (vs {os.path.basename(before_path)})")
    print(f"report: {md_path}")
    if not args.no_pdf:
        pages = render_pdf(args.csv, header, rows, pdf_path,
                           full_budgets=args.full_budgets, compare=compare,
                           spec_ratio_warn=args.spec_ratio_warn)
        print(f"plots : {pdf_path} ({_plural(pages, 'page')})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())

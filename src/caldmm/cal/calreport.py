"""Fluke 5700A calibration-report parser: raw printed text -> structured data.

The 5700A's ``CAL_RPT? CAL|CHECK|RAW`` query (and the serial-printed ``CAL_PR``)
emit a human-formatted, page-broken calibration report.  It is the single
richest health record the instrument produces: for every range it states how
far each calibration constant moved since the previous calibration, both in
absolute terms and -- the headline number -- **as a percentage of that range's
specification**.  A range sitting at 90 % of spec still passes, and is about to
stop passing; that is exactly the signal a bench engineer wants before, and
after, swapping a board.

Scope
-----

Pure text in, dataclasses out.  This module performs **no I/O** and imports no
driver: ``caldmm.cal`` stays driver-free at run time by design (see the
package docstring), so the driver's :meth:`cal_rpt` hands its raw string here.

The wire format
---------------

Three properties of the real bytes shape everything below.

**1. Transport strips leading whitespace.**  ``GpibTransport.read()`` strips
every line, so a report captured live has *no* leading indentation, while the
same report captured from the serial printer port has four columns of it.  The
two must parse identically, therefore this module **never** uses fixed column
positions and **never** keys off indentation.  Every line is
``str.strip()``-ed and then split on whitespace, and the fields are located by
counting tokens **from the right**, where the layout is rigid:

===============================  ==========================================
``... <spec> <unit> <percent>``  the last three tokens, always
``... <shift> <unit>``           the two before those
``... <full scale shift> <unit>``  the two before those
``... <zero shift> <unit>``      two more, on the dc-type tables only
===============================  ==========================================

whatever is left over is the head: the range label (one *or two* tokens --
``220 mV`` but ``2.2V``) plus the point.  ``NO SPEC`` conveniently occupies the
same two tokens a numeric spec would, so the right-anchored count is uniform.

**2. Shift tables use blank-first-field continuation rows.**  A range is
printed once and its remaining points follow with the range column blank::

    220 mV +FS     0.00003 mV    -0.00041 mV  -1.87   ppm   9.91 ppm  -18.88
           -FS     0.00003 mV     0.00048 mV   2.16   ppm   9.91 ppm   21.79

After stripping, the second line simply begins with ``-FS``.  The head-token
analysis above resolves it: a head that is *only* a point (``+FS``/``-FS``/
``FS``) or *only* a frequency carries the previous range forward.

**3. Faults, prompts and several reports share one capture.**  A session log
interleaves ``Fault: <code>: <text>`` lines and ``1>`` prompts with any number
of reports.  :func:`parse_reports` returns them all, in order;
:func:`parse_faults` picks out the faults independently.

Three table shapes
------------------

*dc-type* (``DC VOLTAGE``, ``DC CURRENT``)
    range, ``+FS``/``-FS``, zero shift, full-scale shift, shift, spec, % spec.
*ac-type* (``AC VOLTAGE``, ``AC CURRENT``, ``WIDEBAND``)
    range, ``FS`` or a frequency, full-scale shift, shift, spec, % spec.  The
    ``FS`` row is the range's primary point; the rows under it are the same
    range at other frequencies.
*resistance*
    no range column at all: the certified value stands in for it, and the
    spec on the 0 Ω short is absolute (``Ohm``) rather than relative.

Because the resistance value differs slightly between two reports of the same
instrument, resistance rows are keyed for diffing by a **nominal** label
derived to two significant figures (``0.9999976 Ohm`` and ``0.9999856 Ohm``
both key as ``1.0 Ohm``).

Robustness
----------

This parser exists to be pointed at a *sick* instrument.  Malformed or
truncated input must never throw away what was already parsed: every line is
dispatched inside a guard, anything that looks like data but does not parse is
appended to :attr:`CalReport.warnings` with the offending text, and a report
that ends before its tables do is still returned.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Sequence

__all__ = [
    "CSV_COLUMNS",
    "NO_ASSEMBLY",
    "CalDate",
    "CalReport",
    "DeltaCounts",
    "InternalReference",
    "ReportDiff",
    "ReportKind",
    "ShiftDelta",
    "ShiftRow",
    "Standard",
    "ZERO_CAL_INTERVAL_DAYS",
    "diff_reports",
    "fault_assemblies",
    "format_summary",
    "normalize_text",
    "parse_faults",
    "parse_report",
    "parse_reports",
    "strip_assembly_prefix",
    "to_csv_rows",
]


# --------------------------------------------------------------- unit tables

#: Multiplier and base unit for every magnitude unit the report prints.  These
#: are the units actually observed; an unlisted one becomes a warning rather
#: than a silently mis-scaled value, which is the behaviour worth having.
VALUE_UNITS: dict[str, tuple[float, str]] = {
    "uV": (1e-6, "V"), "mV": (1e-3, "V"), "V": (1.0, "V"),
    "uA": (1e-6, "A"), "mA": (1e-3, "A"), "A": (1.0, "A"),
    "Ohm": (1.0, "Ohm"), "kOhm": (1e3, "Ohm"), "MOhm": (1e6, "Ohm"),
}

#: Multiplier to hertz for the frequency column of the ac-type tables.
FREQUENCY_UNITS: dict[str, float] = {"kHz": 1e3, "MHz": 1e6}

#: Units the *shift* and *spec* columns may carry.  ``Ohm`` appears only on the
#: 0 Ω short, whose specification is absolute rather than relative.
RATIO_UNITS = frozenset({"ppm", "%", "Ohm"})

#: The point labels that mark a dc-type or ac-type primary row.
POINT_LABELS = frozenset({"+FS", "-FS", "FS"})

#: A dc zero older than this trips 5700A fault 1303, "DC Zero Is Needed".
ZERO_CAL_INTERVAL_DAYS = 30

#: ``(has_range_column, has_zero_column)`` per table, used only when the
#: column-header line is missing (a truncated or re-flowed capture).
_TABLE_SHAPES: dict[str, tuple[bool, bool]] = {
    "DC VOLTAGE": (True, True),
    "DC CURRENT": (True, True),
    "AC VOLTAGE": (True, False),
    "AC CURRENT": (True, False),
    "WIDEBAND": (True, False),
    "RESISTANCE": (False, False),
}


# ------------------------------------------------------------------- regexes

_RE_HEADER = re.compile(
    r"FLUKE\s+CORPORATION\s+S/N\s+(?P<serial>\S+)\s+"
    r"(?P<model>\S+)\s+(?P<kind>OUTPUT\s+CALIBRATION|CALIBRATION\s+CHECK)\s*$",
    re.IGNORECASE)
_RE_PRINTED = re.compile(
    r"PRINTED\s+ON\s+(?P<d>\d+/\d+/\d+)\s+AT\s+(?P<t>\d+:\d+:\d+)",
    re.IGNORECASE)
_RE_FAULT = re.compile(r"^Fault:\s*(?P<code>\d+)\s*:\s*(?P<text>.*?)\s*$")
_RE_SHIFT_TITLE = re.compile(r"^(?P<function>.+?)\s+OUTPUT\s+SHIFTS$", re.IGNORECASE)
_RE_INTERVAL = re.compile(
    r"Specification\s+Interval:\s*(?P<interval>\S+)"
    r"(?:\s+Confidence:\s*(?P<confidence>\S+))?",
    re.IGNORECASE)
_RE_CAL_DATE = re.compile(r"^(?P<name>[^()]*)\(\s*(?P<field>[^)]*?)\s*\)\s*(?P<rest>.*)$")
_RE_DATE_TEMP = re.compile(
    r"^(?P<date>\d+/\d+/\d+)\s*,?\s*(?:(?P<temp>[-+]?[\d.]+)\s*Degrees\s*C)?\s*$",
    re.IGNORECASE)
_RE_PROMPT = re.compile(r"^\d+>\s*$")
_RE_DIGIT = re.compile(r"\d")

_SECTION_MODULES = "MODULES PRESENT"
_SECTION_DATES = "ARTIFACT CALIBRATION DATES AND TEMPERATURES"
_SECTION_STANDARDS = "STANDARDS USED"
_SECTION_REFERENCES = "EXTERNALLY CALIBRATED INTERNAL REFERENCES"


# ---------------------------------------------------------------- primitives

def normalize_text(text: str) -> str:
    """Strip the printer artefacts a raw capture carries.

    Carriage returns, form feeds (page breaks) and NUL padding all appear in a
    serial-port capture and none of them mean anything to the parser.  Live
    GPIB text has none of them, which is the point: after this call the two
    sources differ only by leading indentation, and nothing downstream looks at
    that.
    """
    return (text.replace("\r\n", "\n").replace("\r", "\n")
                .replace("\x0c", "").replace("\x00", ""))


def _collapse(line: str) -> str:
    """``line`` with runs of whitespace collapsed to one space, ends trimmed."""
    return " ".join(line.split())


def _is_separator(line: str) -> bool:
    return len(line) >= 4 and set(line) == {"-"}


def _try_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def _parse_date(token: str) -> date | None:
    """``m/d/yy`` as printed by the 5700A.  ``None`` when it will not parse."""
    try:
        return datetime.strptime(token.strip(), "%m/%d/%y").date()
    except ValueError:
        try:
            return datetime.strptime(token.strip(), "%m/%d/%Y").date()
        except ValueError:
            return None


def _si(value: float, unit: str) -> tuple[float, str]:
    """Scale *value* in *unit* to its SI base unit.  Raises on unknown units."""
    try:
        mult, base = VALUE_UNITS[unit]
    except KeyError:
        raise ValueError(f"unknown magnitude unit {unit!r}") from None
    return value * mult, base


def _nominal_label(value: float, unit: str) -> str:
    """A two-significant-figure label, stable across reports.

    The resistance table prints the certified value of each artifact, which
    moves by a few ppm between calibrations.  Diffing needs a key that does
    not, so ``0.9999976 Ohm`` and ``0.9999856 Ohm`` both become ``1.0 Ohm``,
    and ``18.999227 Ohm`` becomes ``19 Ohm``.
    """
    if value == 0.0:
        return f"0 {unit}"
    digits = -int(math.floor(math.log10(abs(value)))) + 1
    rounded = round(value, digits)
    if abs(rounded) >= 10:
        return f"{rounded:.0f} {unit}"
    return f"{rounded:.1f} {unit}"


# ------------------------------------------------------------------- results

class ReportKind(Enum):
    """Which of the two 5700A reports this is."""

    OUTPUT_CALIBRATION = "OUTPUT CALIBRATION"
    CALIBRATION_CHECK = "CALIBRATION CHECK"

    @classmethod
    def from_text(cls, text: str) -> "ReportKind":
        key = _collapse(text).upper()
        for member in cls:
            if member.value == key:
                return member
        raise ValueError(f"unknown report kind {text!r}")

    @property
    def is_check(self) -> bool:
        """True for a calibration *check*, which adjusts nothing."""
        return self is ReportKind.CALIBRATION_CHECK


@dataclass(frozen=True, slots=True)
class CalDate:
    """One row of ARTIFACT CALIBRATION DATES AND TEMPERATURES.

    ``name`` is the section as printed -- ``MAIN OUTPUT``, ``ZERO``,
    ``WIDEBAND GAIN``, ``WIDEBAND FLAT``.  ``ZERO`` has no temperature and no
    previous entry; ``CalCheck`` appears only in a calibration-check report.
    """

    name: str
    most_recent: date | None = None
    most_recent_temp_c: float | None = None
    previous: date | None = None
    previous_temp_c: float | None = None
    cal_check: date | None = None
    cal_check_temp_c: float | None = None
    days_since_cal: int | None = None

    @property
    def overdue(self) -> bool:
        """True when this artifact is past the 30-day dc-zero interval.

        Meaningful for ``ZERO``: beyond 30 days the instrument raises fault
        1303 and its dc specifications no longer apply.
        """
        return (self.days_since_cal is not None
                and self.days_since_cal > ZERO_CAL_INTERVAL_DAYS)


@dataclass(frozen=True, slots=True)
class Standard:
    """One row of STANDARDS USED -- a certified value entered at cal time."""

    name: str
    value: float | None = None
    unit: str | None = None
    number: str | None = None
    cert_date: date | None = None

    @property
    def value_si(self) -> float | None:
        """``value`` scaled to volts or ohms, or ``None`` if not given."""
        if self.value is None or self.unit is None:
            return None
        return _si(self.value, self.unit)[0]


@dataclass(frozen=True, slots=True)
class InternalReference:
    """One row of EXTERNALLY CALIBRATED INTERNAL REFERENCES.

    The 6.5 V and 13 V zeners and the 1 Ω/1.9 Ω/10 kΩ artifacts, with the ppm
    shift between the previous certified value and the current one.  These are
    the instrument's own long-term drift, independent of any range.
    """

    name: str
    most_recent: float
    previous: float
    shift_ppm: float


@dataclass(frozen=True, slots=True)
class ShiftRow:
    """One measured point from an OUTPUT SHIFTS table.

    Magnitudes (``zero_shift``, ``full_scale_shift``) are normalised to SI base
    units -- volts, amps, ohms -- with the printed text kept alongside; the
    report mixes ``mV``/``V``, ``uA``/``mA``/``A`` and ``Ohm``/``kOhm``/``MOhm``
    *within* a single column.  ``shift_value``/``shift_unit`` and
    ``spec_limit``/``spec_unit`` are left in their printed unit because ``ppm``
    and ``%`` are ratios, not magnitudes; use :attr:`shift_ppm` and
    :attr:`spec_ppm` for a common scale.

    :attr:`percent_of_spec` is the headline: the shift as a percentage of the
    range's specification.  Sort on :attr:`abs_percent_of_spec` to find what is
    about to drift out of tolerance.
    """

    function: str
    range_label: str
    point: str
    point_key: str
    frequency_hz: float | None
    zero_shift: float | None
    zero_shift_text: str | None
    full_scale_shift: float
    full_scale_shift_text: str
    base_unit: str
    shift_value: float
    shift_unit: str
    spec_limit: float | None
    spec_unit: str | None
    percent_of_spec: float
    point_value: float | None
    raw_line: str

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity of this point, stable across reports.  Used by diffing."""
        return (self.function, self.range_label, self.point_key)

    @property
    def abs_percent_of_spec(self) -> float:
        return abs(self.percent_of_spec)

    @property
    def has_spec(self) -> bool:
        """False on the ``NO SPEC`` rows above the ac ranges' rated band."""
        return self.spec_limit is not None

    @property
    def shift_ppm(self) -> float | None:
        """The shift in ppm, or ``None`` when it is an absolute quantity."""
        return _to_ppm(self.shift_value, self.shift_unit)

    @property
    def spec_ppm(self) -> float | None:
        """The specification in ppm, or ``None`` when absolute or absent."""
        if self.spec_limit is None or self.spec_unit is None:
            return None
        return _to_ppm(self.spec_limit, self.spec_unit)


def _to_ppm(value: float, unit: str) -> float | None:
    if unit == "ppm":
        return value
    if unit == "%":
        return value * 1e4
    return None


@dataclass(frozen=True, slots=True)
class CalReport:
    """One complete 5700A calibration report."""

    serial: str
    kind: ReportKind
    model: str = "5700A"
    printed_at: datetime | None = None
    software_revision: str | None = None
    modules: tuple[str, ...] = ()
    specification_interval: str | None = None
    confidence: str | None = None
    cal_dates: tuple[CalDate, ...] = ()
    standards: tuple[Standard, ...] = ()
    references: tuple[InternalReference, ...] = ()
    shifts: tuple[ShiftRow, ...] = ()
    warnings: tuple[str, ...] = ()

    # ------------------------------------------------------------- lookups

    def cal_date(self, name: str) -> CalDate | None:
        """The named artifact row, matched case- and spacing-insensitively."""
        key = _collapse(name).upper()
        for entry in self.cal_dates:
            if _collapse(entry.name).upper() == key:
                return entry
        return None

    @property
    def days_since_zero(self) -> int | None:
        """Days since the last dc zero, the most actionable number in here."""
        entry = self.cal_date("ZERO")
        return None if entry is None else entry.days_since_cal

    @property
    def zero_overdue(self) -> bool:
        """True when the dc zero is past its 30-day interval (fault 1303)."""
        entry = self.cal_date("ZERO")
        return bool(entry is not None and entry.overdue)

    def standard(self, name: str) -> Standard | None:
        key = _collapse(name).upper()
        for entry in self.standards:
            if _collapse(entry.name).upper() == key:
                return entry
        return None

    def reference(self, name: str) -> InternalReference | None:
        key = _collapse(name).upper()
        for entry in self.references:
            if _collapse(entry.name).upper() == key:
                return entry
        return None

    @property
    def confidence_percent(self) -> float | None:
        """``confidence`` as a number, e.g. ``99.0`` for ``99%``."""
        if not self.confidence:
            return None
        return _try_float(self.confidence.rstrip("%"))

    # -------------------------------------------------------------- tables

    @property
    def functions(self) -> tuple[str, ...]:
        """The OUTPUT SHIFTS tables present, in printed order."""
        seen: list[str] = []
        for row in self.shifts:
            if row.function not in seen:
                seen.append(row.function)
        return tuple(seen)

    def rows_for(self, function: str) -> tuple[ShiftRow, ...]:
        key = _collapse(function).upper()
        return tuple(r for r in self.shifts if _collapse(r.function).upper() == key)

    def worst_rows(self, count: int = 10) -> tuple[ShiftRow, ...]:
        """The *count* rows furthest along toward their specification limit."""
        ordered = sorted(self.shifts, key=lambda r: -r.abs_percent_of_spec)
        return tuple(ordered[:count])


# ------------------------------------------------------------------ diffing

@dataclass(frozen=True, slots=True)
class ShiftDelta:
    """How one point's percentage-of-spec moved between two reports."""

    function: str
    range_label: str
    point: str
    before_percent: float
    after_percent: float

    @property
    def delta(self) -> float:
        return self.after_percent - self.before_percent

    @property
    def abs_delta(self) -> float:
        return abs(self.delta)

    @property
    def moved(self) -> bool:
        """True when this point's percentage of spec changed at all.

        The printed figures are two decimal places of a percentage, so exact
        equality is a real statement about the instrument and not a
        floating-point accident: a constant that was not touched prints the
        same number twice.
        """
        return self.after_percent != self.before_percent

    @property
    def improved(self) -> bool:
        """True when the point moved closer to nominal."""
        return abs(self.after_percent) < abs(self.before_percent)

    @property
    def unchanged(self) -> bool:
        """True when the point did not move.

        The distinction that matters in the headline: a dc-zero-only
        calibration moves the dc tables and leaves ac, resistance and wideband
        **untouched**, and "did not move" is the correct, reassuring reading of
        those rows -- not "moved further away".
        """
        return not self.moved

    @property
    def worsened(self) -> bool:
        """True when the point moved and did not get closer to nominal.

        Defined as the complement of the other two so that the three are an
        exhaustive partition and a summary built from them always adds up.  The
        one case that lands here on a technicality is a sign flip of exactly
        equal magnitude (``+18.88`` to ``-18.88``): it did move, and it did not
        get closer, so counting it as movement is the honest of the two errors
        -- calling a real change "unchanged" is precisely the bug this
        classification exists to prevent.
        """
        return self.moved and not self.improved


@dataclass(frozen=True, slots=True)
class DeltaCounts:
    """How a whole diff came out: closer, further, and not moved at all.

    A three-way count, because the two-way one it replaces was wrong in the
    only line most readers read.  Deriving "further away" as
    ``total - improved`` reports every *untouched* point as a regression: on a
    real dc-zero-only calibration that is ``11 moved closer, 259 further away``
    when the truth is 11 closer, 6 worse and 253 unchanged -- i.e. it says a
    repair made 259 ranges worse when it changed nothing at 253 of them.
    """

    improved: int = 0
    worsened: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.improved + self.worsened + self.unchanged

    @property
    def moved(self) -> int:
        """Points that changed at all, in either direction."""
        return self.improved + self.worsened


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """The per-range answer to "did the board swap fix it?"."""

    before_printed_at: datetime | None
    after_printed_at: datetime | None
    deltas: tuple[ShiftDelta, ...] = ()
    added: tuple[ShiftRow, ...] = ()
    removed: tuple[ShiftRow, ...] = ()

    def worst(self, count: int = 10) -> tuple[ShiftDelta, ...]:
        """The points that moved the most, largest absolute change first."""
        return tuple(sorted(self.deltas, key=lambda d: -d.abs_delta)[:count])

    def counts(self) -> DeltaCounts:
        """The three-way classification of every matched point.

        Provided here rather than left to each caller: the summary line is the
        one line that answers "did it help?", and computing two of the three
        numbers by subtraction is how it came to claim 259 regressions from a
        calibration that moved 17 points.
        """
        return DeltaCounts(
            improved=sum(1 for d in self.deltas if d.improved),
            worsened=sum(1 for d in self.deltas if d.worsened),
            unchanged=sum(1 for d in self.deltas if d.unchanged),
        )


def diff_reports(before: CalReport, after: CalReport) -> ReportDiff:
    """Per-point change in :attr:`ShiftRow.percent_of_spec` between two reports.

    Points are matched on :attr:`ShiftRow.key`, so resistance artifacts line up
    on their nominal value rather than on the certified value that moved.
    Points present in only one report are reported separately rather than
    silently dropped -- a missing table is itself a finding (a calibration
    check has no WIDEBAND table, and a sick instrument may be unable to print
    one at all).
    """
    before_map = {row.key: row for row in before.shifts}
    after_map = {row.key: row for row in after.shifts}

    deltas = []
    for key, b_row in before_map.items():
        a_row = after_map.get(key)
        if a_row is None:
            continue
        deltas.append(ShiftDelta(
            function=b_row.function,
            range_label=b_row.range_label,
            point=b_row.point if b_row.point_key else b_row.range_label,
            before_percent=b_row.percent_of_spec,
            after_percent=a_row.percent_of_spec,
        ))

    removed = tuple(r for k, r in before_map.items() if k not in after_map)
    added = tuple(r for k, r in after_map.items() if k not in before_map)
    return ReportDiff(
        before_printed_at=before.printed_at,
        after_printed_at=after.printed_at,
        deltas=tuple(deltas),
        added=added,
        removed=removed,
    )


# ---------------------------------------------------------------------- CSV

#: Column order for :func:`to_csv_rows`, suitable for ``lib.csv_io.CsvWriter``
#: (which prepends its own ``Time`` column).
CSV_COLUMNS = [
    "serial", "model", "kind", "printed_at", "function", "range", "point",
    "frequency_hz", "zero_shift", "full_scale_shift", "base_unit",
    "shift_value", "shift_unit", "spec_limit", "spec_unit", "percent_of_spec",
]


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def to_csv_rows(report: CalReport) -> list[dict[str, str]]:
    """Flatten a report's shift tables into ``CSV_COLUMNS``-keyed string rows.

    Strings, not floats, because the consumer is
    :class:`caldmm.lib.csv_io.CsvWriter`; ``%.12g`` keeps every digit the
    instrument printed.  Two captures written this way diff row for row.
    """
    printed = report.printed_at.isoformat() if report.printed_at else ""
    rows: list[dict[str, str]] = []
    for row in report.shifts:
        rows.append({
            "serial": report.serial,
            "model": report.model,
            "kind": report.kind.value,
            "printed_at": printed,
            "function": row.function,
            "range": row.range_label,
            "point": row.point,
            "frequency_hz": _fmt(row.frequency_hz),
            "zero_shift": _fmt(row.zero_shift),
            "full_scale_shift": _fmt(row.full_scale_shift),
            "base_unit": row.base_unit,
            "shift_value": _fmt(row.shift_value),
            "shift_unit": row.shift_unit,
            "spec_limit": _fmt(row.spec_limit),
            "spec_unit": _fmt(row.spec_unit),
            "percent_of_spec": _fmt(row.percent_of_spec),
        })
    return rows


# -------------------------------------------------------------------- faults

#: ``A15:`` or ``A14/A15:`` at the head of a fault message.
_RE_ASSEMBLY_PREFIX = re.compile(r"^\s*(A\d+(?:\s*/\s*A\d+)*)\s*:")
#: An assembly named anywhere in the prose, e.g. ``Assembly A14 Not Responding``.
_RE_ASSEMBLY_ANY = re.compile(r"\bA\d{1,3}\b")

#: Group key for faults whose text names no assembly.
NO_ASSEMBLY = "(no assembly named)"


def fault_assemblies(text: str) -> tuple[str, ...]:
    """Every assembly a fault's text names, prefix **or** prose.

    ``"A15: HV +DC High Voltage Output Fault"`` -> ``("A15",)`` and
    ``"A14/A15: HV +DC Current Error Amp Fault"`` -> ``("A14", "A15")``.

    The prose fallback is not a nicety: of the 191 codes in
    ``data/fluke5700a_faults.yaml``, **3135 is ``"Assembly A14 Not
    Responding"``** -- it names its board in the middle of the sentence rather
    than as a prefix, and a prefix-only pattern drops the one A14 fault that
    says so in plain English into "no assembly named".  A fault naming none
    (``"Unknown Command"``) yields ``()``.

    Lives here, in the module that already parses the instrument's fault
    lines, because both the operator console and the offline report group
    faults by board and two implementations of "which board is this" is how
    they end up disagreeing -- which is exactly what happened.
    """
    if not text:
        return ()
    match = _RE_ASSEMBLY_PREFIX.match(text)
    if match:
        return tuple(part.strip().upper()
                     for part in match.group(1).split("/"))
    seen: list[str] = []
    for item in _RE_ASSEMBLY_ANY.findall(text):
        if item.upper() not in seen:
            seen.append(item.upper())
    return tuple(seen)


def strip_assembly_prefix(text: str) -> str:
    """*text* with a leading ``A15:`` / ``A14/A15:`` removed, if it has one.

    Only the *prefix* form is stripped: a prose mention is part of the
    sentence (``"Assembly A14 Not Responding"`` reads as nothing at all with
    the board cut out of it), so it stays.
    """
    if not text:
        return ""
    match = _RE_ASSEMBLY_PREFIX.match(text)
    return text[match.end():].strip() if match else text.strip()


def parse_faults(text: str) -> list[tuple[int, str]]:
    """Every ``Fault: <code>: <text>`` line, in order, duplicates included.

    A captured service session interleaves faults with reports, and the
    repetition is information: four A15 faults in a row is the high-voltage
    section failing every retry, not one event logged four times.
    """
    faults: list[tuple[int, str]] = []
    for line in normalize_text(text).split("\n"):
        match = _RE_FAULT.match(line.strip())
        if match:
            faults.append((int(match.group("code")), match.group("text")))
    return faults


# -------------------------------------------------------------- the parser

class _ReportBuilder:
    """Mutable accumulator for one report; frozen into a :class:`CalReport`."""

    def __init__(self, serial: str, kind: ReportKind, model: str = "5700A"):
        self.serial = serial
        self.kind = kind
        self.model = model
        self.printed_at: datetime | None = None
        self.software_revision: str | None = None
        self.modules: list[str] = []
        self.specification_interval: str | None = None
        self.confidence: str | None = None
        self.cal_dates: dict[str, dict] = {}
        self.cal_date_order: list[str] = []
        self.current_cal_date: str | None = None
        self.standards: list[Standard] = []
        self.references: list[InternalReference] = []
        self.shifts: list[ShiftRow] = []
        self.warnings: list[str] = []

        self.section: str | None = None
        self.function: str | None = None
        self.has_range = True
        self.has_zero = False
        self.current_range: str | None = None

    # -- helpers ---------------------------------------------------------

    def warn(self, message: str, line: str) -> None:
        self.warnings.append(f"{message}: {line}")

    def cal_date_entry(self, name: str) -> dict:
        if name not in self.cal_dates:
            self.cal_dates[name] = {"name": name}
            self.cal_date_order.append(name)
        return self.cal_dates[name]

    def finish(self) -> CalReport:
        if not self.shifts:
            self.warnings.append(
                "report contains no output-shift rows (truncated capture?)")
        return CalReport(
            serial=self.serial,
            kind=self.kind,
            model=self.model,
            printed_at=self.printed_at,
            software_revision=self.software_revision,
            modules=tuple(self.modules),
            specification_interval=self.specification_interval,
            confidence=self.confidence,
            cal_dates=tuple(CalDate(**self.cal_dates[n]) for n in self.cal_date_order),
            standards=tuple(self.standards),
            references=tuple(self.references),
            shifts=tuple(self.shifts),
            warnings=tuple(self.warnings),
        )

    # -- section dispatch ------------------------------------------------

    def feed(self, line: str) -> None:
        """Hand one stripped, non-empty, non-separator line to its section."""
        collapsed = _collapse(line)

        if _RE_PRINTED.search(collapsed):
            self._printed_on(collapsed)
            return

        title = self._section_title(collapsed)
        if title is not None:
            self.section = title
            return

        if self.section == _SECTION_MODULES:
            self._modules(line)
        elif self.section == _SECTION_DATES:
            self._cal_dates(collapsed)
        elif self.section == _SECTION_STANDARDS:
            self._standards(collapsed)
        elif self.section == _SECTION_REFERENCES:
            self._references(collapsed)
        elif self.section == "SHIFTS":
            self._shifts(collapsed)
        # Anything before the first section title is banner text; ignore it.

    def _section_title(self, collapsed: str) -> str | None:
        upper = collapsed.upper()
        if upper in (_SECTION_MODULES, _SECTION_DATES,
                     _SECTION_STANDARDS, _SECTION_REFERENCES):
            return upper
        match = _RE_SHIFT_TITLE.match(collapsed)
        if match:
            self.function = _collapse(match.group("function")).upper()
            self.has_range, self.has_zero = _TABLE_SHAPES.get(
                self.function, (True, False))
            self.current_range = None
            return "SHIFTS"
        return None

    def _printed_on(self, collapsed: str) -> None:
        match = _RE_PRINTED.search(collapsed)
        assert match is not None
        day = _parse_date(match.group("d"))
        if day is None:
            self.warn("unparsed print date", collapsed)
            return
        try:
            clock = datetime.strptime(match.group("t"), "%H:%M:%S").time()
        except ValueError:
            self.warn("unparsed print time", collapsed)
            return
        self.printed_at = datetime.combine(day, clock)

    # -- sections --------------------------------------------------------

    def _modules(self, line: str) -> None:
        text = line.strip()
        if text.upper().startswith("SOFTWARE REVISION"):
            self.software_revision = text.split(None, 2)[2] if len(
                text.split(None, 2)) > 2 else None
            return
        self.modules.append(text)

    def _cal_dates(self, collapsed: str) -> None:
        interval = _RE_INTERVAL.search(collapsed)
        if interval:
            self.specification_interval = interval.group("interval")
            confidence = interval.group("confidence")
            if confidence:
                # The firmware prints a stray back-tick after the value.
                self.confidence = confidence.rstrip("`'\"")
            return

        match = _RE_CAL_DATE.match(collapsed)
        if not match:
            if _RE_DIGIT.search(collapsed):
                self.warn("unparsed calibration-date line", collapsed)
            return

        name = match.group("name").strip().rstrip(":").strip()
        field_name = _collapse(match.group("field")).lower()
        rest = match.group("rest").strip()

        if name:
            self.current_cal_date = name
        if self.current_cal_date is None:
            self.warn("calibration-date line before any artifact name", collapsed)
            return
        entry = self.cal_date_entry(self.current_cal_date)

        if field_name == "days since last cal":
            value = _try_float(rest.split()[0]) if rest.split() else None
            if value is None:
                self.warn("unparsed days-since-cal", collapsed)
                return
            entry["days_since_cal"] = int(value)
            return

        prefix = {"most recent": "most_recent",
                  "previous": "previous",
                  "calcheck": "cal_check"}.get(field_name)
        if prefix is None:
            self.warn("unknown calibration-date field", collapsed)
            return

        parsed = _RE_DATE_TEMP.match(rest)
        if not parsed:
            self.warn("unparsed calibration date/temperature", collapsed)
            return
        entry[prefix] = _parse_date(parsed.group("date"))
        temp = parsed.group("temp")
        if temp is not None:
            entry[f"{prefix}_temp_c"] = _try_float(temp)

    def _standards(self, collapsed: str) -> None:
        tokens = collapsed.split()
        blanks = [i for i, t in enumerate(tokens) if set(t) == {"_"}]
        if not blanks:
            # The blank-field runs are what locate the name and the value, so a
            # form with its NUMBER and CERT. DATE filled in cannot be split
            # here.  The fixture has no such row, so the layout is unknown --
            # but the row must not vanish: record it verbatim.  (The real
            # column header, "STANDARD NUMBER CERT. DATE", ends in no number
            # and so stays silent.)
            if _try_float(tokens[-1]) is not None or (
                    len(tokens) > 1 and _try_float(tokens[-2]) is not None):
                self.warn("standards row with no blank fields", collapsed)
            return
        name_tokens = tokens[:blanks[0]]
        value_tokens = tokens[blanks[-1] + 1:]
        if not name_tokens:
            return  # the spare unnamed OTHER row
        name = " ".join(name_tokens)
        if not value_tokens:
            self.standards.append(Standard(name=name))
            return
        value = _try_float(value_tokens[0])
        if value is None:
            self.warn("unparsed standard value", collapsed)
            self.standards.append(Standard(name=name))
            return
        unit = value_tokens[1] if len(value_tokens) > 1 else None
        if unit is not None and unit not in VALUE_UNITS:
            self.warn("unknown standard unit", collapsed)
            unit = None
        self.standards.append(Standard(name=name, value=value, unit=unit))

    def _references(self, collapsed: str) -> None:
        tokens = collapsed.split()
        if len(tokens) < 4:
            return
        numbers = [_try_float(t) for t in tokens[-3:]]
        if any(n is None for n in numbers):
            # Column header, or the check report's "USED AS CHECK STANDARDS"
            # banner -- neither has a number anywhere near its right edge.  A
            # row where *some* of the three parse is a garbled data row, and
            # which one broke must not decide whether it is reported.
            if _RE_DIGIT.search(collapsed) and any(n is not None for n in numbers):
                self.warn("unparsed internal-reference row", collapsed)
            return
        name = " ".join(tokens[:-3])
        if not name:
            self.warn("internal-reference row with no name", collapsed)
            return
        self.references.append(InternalReference(
            name=name,
            most_recent=numbers[0],
            previous=numbers[1],
            shift_ppm=numbers[2],
        ))

    # -- shift tables ----------------------------------------------------

    def _shifts(self, collapsed: str) -> None:
        upper = collapsed.upper()
        if "FULL SCALE SHIFT" in upper:
            # The column header settles the table's shape without relying on
            # the function name -- and without looking at column positions.
            self.has_range = upper.startswith("RANGE")
            self.has_zero = "ZERO SHIFT" in upper
            return
        if not _RE_DIGIT.search(collapsed):
            return  # the "(+/-) (% spec)" second header line, or banner text

        row = self._shift_row(collapsed)
        if row is not None:
            self.shifts.append(row)

    def _shift_row(self, collapsed: str) -> ShiftRow | None:
        tokens = collapsed.split()
        n_tail = 9 if self.has_zero else 7
        if len(tokens) < n_tail + 1:
            self.warn("short shift row", collapsed)
            return None

        tail = tokens[-n_tail:]
        head = tokens[:-n_tail]

        # The head is resolved *first*, deliberately: it is what carries the
        # range forward to the continuation rows below.  A garbled magnitude
        # further right must cost one row, not the rest of the range's block.
        parsed_head = self._shift_head(head, collapsed)

        try:
            percent = float(tail[-1])
            spec_limit, spec_unit = _parse_spec(tail[-3], tail[-2])
            shift_value = float(tail[-5])
            shift_unit = tail[-4]
            full_scale, full_scale_unit = float(tail[-7]), tail[-6]
            if self.has_zero:
                zero, zero_unit = float(tail[-9]), tail[-8]
            else:
                zero, zero_unit = None, None
        except ValueError:
            self.warn("unparsed shift row", collapsed)
            return None

        if shift_unit not in RATIO_UNITS:
            self.warn("unknown shift unit", collapsed)
            return None
        if spec_unit is not None and spec_unit not in RATIO_UNITS:
            self.warn("unknown spec unit", collapsed)
            return None

        try:
            full_scale_si, base_unit = _si(full_scale, full_scale_unit)
            zero_si = None if zero is None else _si(zero, zero_unit)[0]
        except ValueError:
            self.warn("unknown magnitude unit", collapsed)
            return None

        if parsed_head is None:
            return None
        range_label, point, point_key, frequency_hz, point_value = parsed_head

        return ShiftRow(
            function=self.function or "",
            range_label=range_label,
            point=point,
            point_key=point_key,
            frequency_hz=frequency_hz,
            zero_shift=zero_si,
            zero_shift_text=None if zero is None else f"{tail[-9]} {zero_unit}",
            full_scale_shift=full_scale_si,
            full_scale_shift_text=f"{tail[-7]} {full_scale_unit}",
            base_unit=base_unit,
            shift_value=shift_value,
            shift_unit=shift_unit,
            spec_limit=spec_limit,
            spec_unit=spec_unit,
            percent_of_spec=percent,
            point_value=point_value,
            raw_line=collapsed,
        )

    def _shift_head(
        self, head: Sequence[str], collapsed: str,
    ) -> tuple[str, str, str, float | None, float | None] | None:
        """Resolve the leading tokens into ``(range, point, key, f, value)``.

        This is where continuation rows are handled: a head that carries no
        range tokens inherits the range of the row above it.  The
        discrimination is purely by token content, never by indentation --
        which is gone by the time the text reaches here on the GPIB path.
        """
        if not self.has_range:
            # Resistance: the certified value stands in for the range column.
            if len(head) != 2:
                self.warn("unparsed resistance point", collapsed)
                return None
            value = _try_float(head[0])
            if value is None or head[1] not in VALUE_UNITS:
                self.warn("unparsed resistance point", collapsed)
                return None
            value_si = _si(value, head[1])[0]
            return (_nominal_label(value, head[1]),
                    f"{head[0]} {head[1]}", "", None, value_si)

        if head and head[-1] in POINT_LABELS:
            point = head[-1]
            range_tokens = head[:-1]
            frequency_hz = None
        elif len(head) >= 2 and head[-1] in FREQUENCY_UNITS:
            magnitude = _try_float(head[-2])
            if magnitude is None:
                self.warn("unparsed frequency point", collapsed)
                return None
            point = f"{head[-2]} {head[-1]}"
            frequency_hz = magnitude * FREQUENCY_UNITS[head[-1]]
            range_tokens = head[:-2]
        else:
            self.warn("unparsed shift-row point", collapsed)
            return None

        if range_tokens:
            self.current_range = " ".join(range_tokens)
        if self.current_range is None:
            self.warn("continuation row before any range", collapsed)
            return None
        return (self.current_range, point, point, frequency_hz, None)


def _parse_spec(value_token: str, unit_token: str) -> tuple[float | None, str | None]:
    """``("85.00", "ppm")`` or the two-token ``("NO", "SPEC")`` placeholder.

    ``NO SPEC`` occupies exactly the width a numeric specification would, which
    is what keeps the right-anchored token count uniform across every table.
    """
    if value_token.upper() == "NO" and unit_token.upper() == "SPEC":
        return None, None
    return float(value_token), unit_token


def parse_reports(text: str) -> list[CalReport]:
    """Every report in *text*, in order.

    A captured service session holds several: an output calibration, a
    calibration check, more of both, with faults and ``1>`` prompts between
    them.  Faults and prompts close the report in progress -- they are printed
    by the remote interface, never inside a report body.

    Never raises on malformed input: a line that cannot be parsed becomes an
    entry in the report's :attr:`~CalReport.warnings` and parsing continues, so
    a truncated final table still yields every table before it.
    """
    builder: _ReportBuilder | None = None
    reports: list[CalReport] = []

    for raw_line in normalize_text(text).split("\n"):
        line = raw_line.strip()
        if not line or _is_separator(line):
            continue

        header = _RE_HEADER.search(line)
        if header:
            if builder is not None:
                reports.append(builder.finish())
            try:
                kind = ReportKind.from_text(header.group("kind"))
            except ValueError:
                builder = None
                continue
            builder = _ReportBuilder(header.group("serial"), kind,
                                     header.group("model"))
            continue

        if _RE_FAULT.match(line) or _RE_PROMPT.match(line) \
                or line.upper().endswith("REMOTE INTERFACE"):
            if builder is not None:
                # A fault landing *inside* a printed report ends it here, and
                # everything below the fault is orphaned.  That is precisely
                # the case this module was written for -- a sick instrument
                # faulting mid-print -- so it must leave a record.  The prompt
                # and banner lines legitimately follow a finished report, and
                # stay silent.
                if builder.shifts and _RE_FAULT.match(line):
                    builder.warn(
                        "report closed early by an interleaved fault line", line)
                reports.append(builder.finish())
                builder = None
            continue

        if builder is None:
            continue

        try:
            builder.feed(line)
        except Exception as exc:  # never lose what is already parsed
            builder.warn(f"parser error ({exc.__class__.__name__}: {exc})", line)

    if builder is not None:
        reports.append(builder.finish())
    return reports


def parse_report(text: str) -> CalReport:
    """The first report in *text*.  Raises ``ValueError`` when there is none."""
    reports = parse_reports(text)
    if not reports:
        raise ValueError("no 5700A calibration report found in the text")
    return reports[0]


def format_summary(report: CalReport, count: int = 10) -> str:
    """A short human summary: identity, zero age, and the worst *count* rows."""
    lines = [
        f"{report.model} S/N {report.serial} -- {report.kind.value}",
        f"printed {report.printed_at.isoformat(' ') if report.printed_at else '?'}"
        f"  firmware {report.software_revision or '?'}",
        f"spec interval {report.specification_interval or '?'}"
        f" at {report.confidence or '?'} confidence",
    ]
    days = report.days_since_zero
    if days is not None:
        flag = "  *** OVERDUE (fault 1303) ***" if report.zero_overdue else ""
        lines.append(f"days since dc zero: {days}{flag}")
    if report.shifts:
        lines.append(f"worst {count} points, as a percentage of spec:")
        for row in report.worst_rows(count):
            lines.append(
                f"  {row.percent_of_spec:8.2f} %  {row.function:<12}"
                f" {row.range_label:>8} {row.point}")
    for warning in report.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)

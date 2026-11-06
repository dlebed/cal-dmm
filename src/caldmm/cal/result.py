"""What a verification run records: the result row, its CSV schema and the header.

**The CSV is the source of truth.**  A calibration report that cannot be
re-derived from its own data file is not evidence, so everything a report needs
travels in the file: the full uncertainty budget component by component, the
provenance of *both* instruments, the conditions the decision was taken under,
and the decision itself with the rule that produced it.  Nothing here talks to
an instrument, and nothing here decides anything -- :mod:`caldmm.cal.verdict`
has already done that by the time a row is built.

Why a file and not a database
-----------------------------

A generic time-series table (time, instrument, value, unit) cannot express a
verification row -- there is nowhere to put a setpoint, an actual, a limit, an
uncertainty, a TUR or a verdict -- and such stores often carry retention
policies, which is exactly wrong for a record that has to outlive the next
calibration interval.  So the CSV is authoritative and this module does no
database I/O at all.

The three things that are easy to get wrong
-------------------------------------------

**The source's value is the readback, not the request.**  :attr:`ResultRow.
source_value` is :attr:`~caldmm.cal.roles.SourceState.actual_value` -- for
resistance the 5700A sources a *characterized* value up to 500 ppm off the
nominal it was asked for, and recording the nominal folds the calibrator's ohms
characterization straight into the DUT's error.  The nominal is kept too, in
:attr:`ResultRow.nominal`, because the two being different is itself worth
seeing.

**A tolerance is stored as an absolute in the quantity's unit**, and ppm is
*derived* from it against :attr:`ResultRow.ppm_base`.  Storing ppm alone makes a
zero or short point meaningless -- there is no "ppm of reading" when the reading
is zero -- and the ppm columns are therefore empty at such a point while the
absolute limit is still there.  See :attr:`ResultRow.is_zero_point` for what
counts as zero, which is not merely ``== 0``.

**Derived numbers are derived, never stored twice.**  ``U``, ``k``, ``nu_eff``,
``TUR``, the acceptance limit and the margin are recomputed from the budget and
the stored inputs by :meth:`ResultRow.from_cells`; the ppm columns, the relative
margin, the source's absolute ``UNCERT?`` and the limit-disagreement flag are
properties.  All of them are *written* to the CSV so a human or a spreadsheet
can read them, and none of them is read back.  A stored derived value that
disagrees with its own inputs is the one failure mode a self-contained evidence
file must not have -- and the written cells are rounded for legibility, so it
would happen in the twelfth digit of every row.

The exception, stated because it is the only one: ``Guard_Band``.  ``w`` follows
from the *decision rule* (zero, ``+g*U`` or ``-g*U``), which the budget does not
know, so it is stored as an input and the acceptance limit is derived from it.

Layout of a file
----------------

Leading ``# key=value`` comment lines carry the :class:`RunHeader`, then the
ordinary caldmm CSV: semicolon-delimited, ISO-8601 ``Time`` first column, one
row per executed point.  One file, because a sidecar can be separated from the
data it describes and then neither half is evidence.  The cost is that
``lib.csv_io.load_csv`` (pandas, no ``comment=`` argument) cannot read a result
file directly; :func:`load_results` is the reader, and it returns the header
alongside the rows.

The budget is a **JSON list in one cell** (``Budget``), exactly the rows
:func:`~caldmm.cal.uncertainty.budget_to_rows` produces.  A budget has a
variable number of components -- two at a plain dc point, six or seven once
loading, tempco and thermal EMF are carried -- and the alternatives are worse:
fixed ``U1..Un`` columns silently truncate the eighth component, one CSV row
per component makes the file no longer one-row-per-point (and every existing
tool that reads caldmm CSVs assumes that), and a sidecar can be separated from
the data.  ``budget_to_rows`` already writes each float with ``repr``, so the
budget round-trips **bit-exactly** through JSON while the measurement columns
keep the project's human-readable exponential format.

The raw samples travel the same way (``Samples``, schema v2): the reading's
*surviving* samples as one JSON list, floats serialised by ``json`` with
``repr`` precision so they reload bit-exactly.  They are what makes the Type A
term auditable -- ``Stdev`` in the row is an 11-digit rendering, but the exact
standard deviation is recomputable from the cell, sample by sample.  The
excluded sentinels are *not* in the list; they were never part of any
statistic, and ``Overloads``/``Discarded`` carry their counts.  A file written
before schema v2 has no ``Samples`` column and loads with an empty tuple:
"not recorded" is a state, and it is not fabricated.

Schema v2 also adds ``Meter_Temp_C`` -- the meter's own internal temperature
at each point, where the instrument offers one (the 3458A's ``TEMP?``) --
and four header keys: ``run_kind`` (``as-found`` | ``as-left`` |
``standalone``), ``counterpart`` (the paired run's file), ``adjust_record``
(the adjustment session log between the pair), and ``source_cal_temp_c``
(the calibrator's ``CAL_TEMP?`` -- the ambient its stored constants assume;
the departure from it is what drives its tempco).

Delimiter safety: ``lib.csv_io.CsvWriter`` joins cells with ``;`` and does no
quoting, and this schema stores free text (``Reason`` runs to a sentence with
semicolons in it) and JSON.  Every text cell is therefore backslash-escaped on
the way out (:func:`_escape`) and unescaped on the way in, so a cell can never
contain a raw delimiter or newline.

Durability
----------

:class:`ResultWriter` flushes **every row as it is written**.  A run is around
200 points over two hours, so a buffered writer would hand a crash, a dropped
ssh session or a Ctrl-C at point 180 the power to destroy the whole record.
The file on disk is always a loadable record of every point completed so far,
and ``append=True`` reopens it to resume at a group boundary.

Import hygiene
--------------

Model and serialisation only.  Imports are the stdlib, ``lib.csv_io`` and the
pure-model half of ``cal`` (``quantities``, ``roles``, ``uncertainty``,
``verdict``).  Never :mod:`caldmm.cal.adapters`, never a driver.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from ..lib.csv_io import CsvWriter
from .quantities import Connection, Quantity
from .roles import (
    UNKNOWN_ENVIRONMENT,
    Environment,
    MeterConfig,
    Reading,
    SourceState,
    Uncertainty,
)
from .uncertainty import UncertaintyBudget, budget_from_rows, budget_to_rows
from .verdict import (
    DEFAULT_CONFIDENCE,
    DEFAULT_GUARD_FACTOR,
    DEFAULT_TUR_FLOOR,
    FLAG_AMBIENT_UNKNOWN,
    FLAG_CONDITIONS_NOT_MET,
    FLAG_EMPTY_BUDGET,
    FLAG_NO_TEMPERATURE_WINDOW,
    FLAG_TEMPERATURE_OUT_OF_WINDOW,
    FLAG_TUR_BELOW_FLOOR,
    FLAG_ZERO_UNCERTAINTY,
    ConformityResult,
    TemperatureStatus,
    Verdict,
    gates_for,
)

__all__ = [
    "ResultError",
    "SCHEMA_VERSION",
    "RUN_KINDS",
    "CSV_COLUMNS",
    "LIMIT_DISAGREEMENT_FACTOR",
    "TEMPERATURE_FLAGS",
    "interval_days",
    "RunHeader",
    "ResultRow",
    "RunSummary",
    "ResultWriter",
    "header_to_lines",
    "header_from_lines",
    "open_results",
    "write_results",
    "load_results",
    "summarize_run",
    "caldmm_version",
]


class ResultError(ValueError):
    """A malformed result row, header or result file."""


#: Bumped when the column set or the header vocabulary changes incompatibly.
#: v2 added the ``Samples`` and ``Meter_Temp_C`` columns and the ``run_kind``
#: / ``counterpart`` / ``adjust_record`` / ``source_cal_temp_c`` header keys.
SCHEMA_VERSION = 2

#: What :attr:`RunHeader.run_kind` may say.  ``as-found`` is the verification
#: measured *before* an adjustment, ``as-left`` the one measured *after* it;
#: ``standalone`` is a run that is part of no such pair, and the default so
#: that a file which never says is not mistaken for half of one.
RUN_KINDS = ("as-found", "as-left", "standalone")

#: How far the spec-table limit and the instrument's own ``UNCERT?``-derived
#: limit may differ before :attr:`ResultRow.limit_disagree` fires.  A factor of
#: two: the two figures are computed differently (a printed table against the
#: unit's own stored constants, ``CAL_INTV`` and ``CAL_CONF``) and will never
#: agree exactly, but a *unit* whose calibration constants are corrupt, or
#: whose interval or confidence has been changed behind the operator's back,
#: misses by far more than that.
LIMIT_DISAGREEMENT_FACTOR = 2.0

#: Every verdict flag that says something about the temperature.  Note what
#: this is **not**: ``no-temperature-window`` is in here and the default
#: ``gate`` policy does not block on it, so membership means "a report must
#: mention the temperature", never "the temperature blocked this point".
#: :meth:`ResultRow.blocked_by_temperature` answers the second question, and it
#: needs the run's policy to do it.
TEMPERATURE_FLAGS = (FLAG_TEMPERATURE_OUT_OF_WINDOW, FLAG_AMBIENT_UNKNOWN,
                     FLAG_NO_TEMPERATURE_WINDOW)

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
# The columns
# --------------------------------------------------------------------------

#: Column order of a result file, after the ``Time`` column that
#: :class:`~caldmm.lib.csv_io.CsvWriter` prepends.
CSV_COLUMNS: list[str] = [
    # -- identity and place in the run
    "Run_Id", "Point", "Group", "Label", "Note",
    # -- the point, as planned
    "Quantity", "Nominal", "Freq_Hz", "Connection",
    "Range_Arg", "Range_Id", "Mode", "Interval",
    # -- the source: what it actually sourced, and its own figure for it
    "Src_Model", "Src_Value", "Src_Unit", "Src_Freq_Hz", "Src_Range_Id",
    "Src_Uncert", "Src_Uncert_Unit", "Src_Uncert_Conf", "Src_Uncert_Days",
    "Src_Settled", "Src_Operate",
    # -- the meter: the statistics and the configuration actually applied
    "Mtr_Model", "Mean", "Stdev", "N", "Discarded", "Overloads", "Resolution",
    "Mtr_Func", "Mtr_Range_Arg", "Mtr_Range_FS", "Mtr_NPLC", "Mtr_Mode",
    "Mtr_AC_Filter", "Mtr_Settle_s", "Mtr_Applied",
    # -- conditions at the point
    "Ambient_C", "Humidity_Pct", "Env_Time", "Env_Source", "Meter_Temp_C",
    # -- the comparison
    "Reference", "Error", "Error_ppm", "Tol_Limit", "Tol_Limit_ppm",
    # -- cross-check: the spec table against the instrument's own UNCERT?
    "Spec_Limit", "Uncert_Limit", "Limit_Disagree",
    # -- the decision
    "Verdict", "U", "k", "Nu_Eff", "TUR", "Guard_Band", "Accept_Limit",
    "Margin", "Rel_Margin", "Confidence", "Temp_Status", "Flags", "Rule",
    "Reason",
    # -- the budget, component by component
    "Budget",
    # -- the raw evidence: the surviving samples behind Mean and Stdev
    "Samples",
]

_INT_COLUMNS = frozenset({
    "Point", "Group", "N", "Discarded", "Overloads", "Src_Uncert_Days",
})
_BOOL_COLUMNS = frozenset({"Src_Settled", "Src_Operate", "Limit_Disagree"})
_PPM_COLUMNS = frozenset({"Error_ppm", "Tol_Limit_ppm"})
#: Dimensionless and much more readable as plain decimals -- but ``%g``, not a
#: fixed number of places: a TUR of 2.4e-4 (a 5700A ac point against a meter
#: barely better than it) written as ``0.000240`` would be down to two
#: significant figures, and "how bad is the ratio" is exactly what that column
#: is read for.
_RATIO_COLUMNS = frozenset({"k", "TUR", "Confidence"})
_SIGNED_RATIO_COLUMNS = frozenset({"Rel_Margin"})
#: ISO-8601 timestamp columns.  Not escaped: an ISO timestamp cannot contain
#: the delimiter, and leaving it raw keeps the cell greppable.
_TIME_COLUMNS = frozenset({"Env_Time"})
#: May legitimately be ``inf`` (a budget of pure Type B bounds).
_DOF_COLUMNS = frozenset({"Nu_Eff"})
_TEXT_COLUMNS = frozenset({
    "Run_Id", "Label", "Note", "Quantity", "Connection", "Range_Id", "Mode",
    "Interval", "Src_Model", "Src_Unit", "Src_Range_Id", "Src_Uncert_Unit",
    "Mtr_Model", "Mtr_Func", "Mtr_Mode", "Mtr_AC_Filter", "Env_Source",
    "Verdict", "Temp_Status", "Flags", "Rule", "Reason",
})
#: JSON-encoded columns.  ``Budget`` is the whole itemised budget as
#: :func:`~caldmm.cal.uncertainty.budget_to_rows` produced it -- see the
#: module docstring for why it travels in the row rather than a sidecar.
#: ``Samples`` is the reading's surviving samples, floats at ``repr``
#: precision, so the Type A term is recomputable from the file alone.
_JSON_COLUMNS = ("Mtr_Applied", "Budget", "Samples")


# --------------------------------------------------------------------------
# Cell escaping and formatting
# --------------------------------------------------------------------------

#: The delimiter is written as a hex escape, **not** as ``\;``: the reader
#: splits the line on ``;`` before it has a chance to honour any escaping, so
#: an escape that still contains the delimiter character does not escape
#: anything.  ``\x3B`` removes it from the byte stream entirely.
_SEMICOLON_ESCAPE = "\\x3B"

_ESCAPES = {"\\": "\\\\", ";": _SEMICOLON_ESCAPE, "\n": "\\n", "\r": "\\r"}


def _escape(text: str) -> str:
    """Make *text* safe for a naively ``;``-joined, newline-terminated cell.

    ``lib.csv_io.CsvWriter`` does no quoting, and this schema stores a
    ``Reason`` sentence full of semicolons next to a JSON budget.  Escaping
    here is what keeps one row on one line with one cell per column.
    """
    return "".join(_ESCAPES.get(char, char) for char in text)


_UNESCAPES = {"\\": "\\", "n": "\n", "r": "\r"}


def _unescape(text: str) -> str:
    """Inverse of :func:`_escape`.  Unknown escapes are left verbatim."""
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            nxt = text[index + 1]
            if nxt in _UNESCAPES:
                out.append(_UNESCAPES[nxt])
                index += 2
                continue
            if nxt == "x" and index + 3 < length:
                try:
                    out.append(chr(int(text[index + 2:index + 4], 16)))
                except ValueError:
                    out.append("\\x")
                index += 4
                continue
            out.append("\\" + nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _json_cell(value) -> str:
    """A mapping or list as one escaped JSON cell; ``None``/empty -> ``''``.

    ``default=str`` because ``MeterConfig.applied`` carries whatever the
    model's knob vocabulary put there; a knob whose value is an enum or a
    ``Decimal`` must not take the whole file down.
    """
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)) and len(value) == 0:
        return ""
    if isinstance(value, Mapping):
        value = dict(value)
    return _escape(json.dumps(value, separators=(",", ":"), default=str,
                              sort_keys=isinstance(value, dict)))


def _fmt_cell(col: str, v) -> str:
    """Format one cell according to its column's convention.

    ``None`` and ``nan`` render as the empty string and reload as ``None``:
    "there is no number here" is a state this schema has to be able to say --
    a ``NO_SPEC`` point has no tolerance limit, a zero point has no ppm, and a
    single-sample reading has no standard deviation.  Zero would be a number
    the arithmetic downstream would happily consume.
    """
    if v is None:
        return ""
    if isinstance(v, Quantity):
        v = v.name
    elif isinstance(v, (Connection, Verdict, TemperatureStatus)):
        v = v.value
    if col == "Flags" and isinstance(v, (tuple, list)):
        v = ",".join(str(f) for f in v)
    if col in _JSON_COLUMNS:
        return _json_cell(v)
    if isinstance(v, float) and math.isnan(v):
        return ""
    if col in _BOOL_COLUMNS:
        return "1" if v else "0"
    if col in _INT_COLUMNS:
        return str(int(v))
    if col in _DOF_COLUMNS:
        value = float(v)
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return f"{value:+.10E}"
    if col in _TIME_COLUMNS:
        return v.isoformat() if isinstance(v, datetime) else str(v)
    if col in _PPM_COLUMNS:
        return f"{float(v):+.6f}"
    if col in _SIGNED_RATIO_COLUMNS:
        return f"{float(v):+.12g}"
    if col in _RATIO_COLUMNS:
        return f"{float(v):.12g}"
    if col in _TEXT_COLUMNS:
        return _escape(str(v))
    if isinstance(v, float):
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return f"{v:+.10E}"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return _escape(str(v))


# ---- parsing back

def _cell(cells: Mapping[str, str], col: str) -> str:
    value = cells.get(col)
    return "" if value is None else str(value).strip()


def _text_of(cells: Mapping[str, str], col: str) -> str:
    return _unescape(_cell(cells, col))


def _opt_text(cells: Mapping[str, str], col: str) -> str | None:
    text = _text_of(cells, col)
    return text or None


def _float_of(cells: Mapping[str, str], col: str) -> float | None:
    text = _cell(cells, col)
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ResultError(f"{col}: not a number: {text!r}") from exc
    return None if math.isnan(value) else value


def _int_of(cells: Mapping[str, str], col: str) -> int | None:
    text = _cell(cells, col)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError as exc:
        raise ResultError(f"{col}: not an integer: {text!r}") from exc


def _bool_of(cells: Mapping[str, str], col: str) -> bool:
    return _cell(cells, col) in ("1", "true", "True", "yes")


def _datetime_of(cells: Mapping[str, str], col: str) -> "datetime | None":
    text = _cell(cells, col)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ResultError(
            f"{col}: not an ISO-8601 timestamp: {text!r}") from exc


def _enum_of(kind, cells: Mapping[str, str], col: str, default=None):
    """Parse an enum cell, reporting the column rather than a bare ValueError.

    Every other parse failure in this module names the column it came from; an
    unknown ``Quantity`` spelling in row 140 of a two-hour run should not be
    the one that says only ``'DVC' is not a valid Quantity``.
    """
    text = _text_of(cells, col) or default
    try:
        return kind(text)
    except ValueError as exc:
        raise ResultError(
            f"{col}: unknown {kind.__name__} {text!r}") from exc


def _json_of(cells: Mapping[str, str], col: str):
    text = _text_of(cells, col)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResultError(f"{col}: malformed JSON: {exc}") from exc


#: Days per unit suffix of an interval id (``24h``, ``90d``, ``1y``).
_INTERVAL_UNIT_DAYS: Mapping[str, float] = MappingProxyType({
    "h": 1.0 / 24.0, "d": 1.0, "w": 7.0, "m": 30.0, "y": 365.0,
})


def interval_days(interval: str | None) -> float | None:
    """How many days an interval id names, or ``None`` if it names none.

    ``24h`` -> 1, ``90d`` -> 90, ``1y`` -> 365.  Ids like the 1281's
    ``enhanced`` name a *column*, not a period, and answer ``None`` -- as does
    an absent interval.  Lives here because both this module (comparing the
    header's interval with the instrument's own) and the engine (choosing the
    column a zero-point fallback is read from) need the same reading of the
    same vocabulary, and two readings of it would eventually disagree.
    """
    text = str(interval or "").strip().lower()
    if len(text) < 2:
        return None
    days = _INTERVAL_UNIT_DAYS.get(text[-1])
    if days is None:
        return None
    try:
        return float(text[:-1]) * days
    except ValueError:
        return None


def _nan_to_none(value) -> float | None:
    """``nan`` means "no number" everywhere in ``cal``; store it as ``None``.

    :class:`~caldmm.cal.verdict.ConformityResult` uses ``nan`` for the
    limit-derived fields of a ``NO_SPEC`` point and
    :class:`~caldmm.cal.roles.Reading` uses it for the statistics of an empty
    sample set.  Normalising on the way in is what makes the round trip exact:
    ``nan`` is written as an empty cell, and an empty cell reads back as
    ``None``, not as a ``nan`` that would compare unequal to itself.
    """
    if value is None:
        return None
    value = float(value)
    return None if math.isnan(value) else value


# --------------------------------------------------------------------------
# The run header
# --------------------------------------------------------------------------

def caldmm_version() -> str:
    """The installed package version, for the header's provenance block."""
    from .. import __version__
    return str(__version__)


_HEADER_BANNER = "# caldmm calibration results"

#: ``(key, kind)`` for every header field, in the order they are written.
#: ``kind`` drives both directions of the conversion, so a field cannot be
#: written one way and parsed another.
_HEADER_FIELDS: tuple[tuple[str, str], ...] = (
    ("schema_version", "int"),
    ("run_id", "str"),
    ("started", "datetime"),
    ("plan_name", "str"),
    ("dut_role", "str"),
    ("run_kind", "str"),
    ("counterpart", "optstr"),
    ("adjust_record", "optstr"),
    ("interval", "optstr"),
    ("confidence", "float"),
    ("tur_floor", "float"),
    ("guard_band_factor", "float"),
    ("temperature_policy", "str"),
    ("source_model", "str"),
    ("source_idn", "str"),
    ("source_options", "tuple"),
    ("source_cal_days", "int"),
    ("source_zero_days", "int"),
    ("source_cal_interval_days", "int"),
    ("source_cal_confidence", "str"),
    ("source_cal_temp_c", "float"),
    ("source_warm_minutes", "int"),
    ("meter_model", "str"),
    ("meter_idn", "str"),
    ("meter_calnum", "int"),
    ("meter_acal_age_h", "float"),
    ("meter_temp_c", "float"),
    ("ambient_c", "float"),
    ("humidity_pct", "float"),
    ("ambient_timestamp", "datetime"),
    ("ambient_source", "str"),
    ("caldmm_version", "str"),
    ("git_revision", "str"),
)


@dataclass(frozen=True, slots=True)
class RunHeader:
    """Provenance: what a report has to state to be defensible.

    Both instruments, not just the one under test.  A verification is a
    *comparison*, and a reader who cannot see the reference's calibration age,
    its interval, the confidence level its own uncertainty figures were
    computed at and how long it had been warm has been given a number without
    the means to judge it.

    ``ambient_c`` is ``None`` when nobody recorded one -- a distinct state from
    "recorded and out of window", and the one that
    :func:`caldmm.cal.verdict.evaluate` turns into ``AMBIENT_UNKNOWN`` and
    blocks a ``PASS`` on.  :attr:`ambient_known` says which it was, and the
    written header carries it explicitly so nobody has to infer it from a blank.

    Unrecognised ``# key=value`` lines are kept in :attr:`extra` rather than
    dropped: a file written by a later version must not lose provenance on the
    way through an older reader.

    ``run_kind`` places the file in a calibration cycle: an ``as-found`` run
    is the last measurement of how far the instrument drifted before an
    adjustment overwrote it, an ``as-left`` run is the state it was released
    in, and ``standalone`` -- the default -- is a run that is part of no such
    pair.  ``counterpart`` names the paired run's file and ``adjust_record``
    the adjustment session log between them, so the three files of a cycle
    can find each other from any one of them.  ``source_cal_temp_c`` is the
    calibrator's ``CAL_TEMP?`` -- the ambient its stored constants assume (it
    has no live internal sensor); the ambient-minus-``CAL_TEMP`` delta is the
    quantity that drives its temperature coefficient.
    """

    run_id: str = ""
    started: datetime | None = None
    plan_name: str = ""
    dut_role: str = ""
    run_kind: str = "standalone"
    counterpart: str | None = None
    adjust_record: str | None = None
    interval: str | None = None
    confidence: float = DEFAULT_CONFIDENCE
    tur_floor: float = DEFAULT_TUR_FLOOR
    guard_band_factor: float = DEFAULT_GUARD_FACTOR
    temperature_policy: str = "gate"

    # -- the source (5700A: *IDN?, *OPT?, CAL_DAYS?, CAL_INTV?, CAL_CONF?,
    #    ONTIME?)
    source_model: str = ""
    source_idn: str = ""
    source_options: tuple[str, ...] = ()
    source_cal_days: int | None = None
    source_zero_days: int | None = None
    source_cal_interval_days: int | None = None
    source_cal_confidence: str = ""
    source_cal_temp_c: float | None = None
    source_warm_minutes: int | None = None

    # -- the meter (3458A: calnum, ACAL age, internal temperature)
    meter_model: str = ""
    meter_idn: str = ""
    meter_calnum: int | None = None
    meter_acal_age_h: float | None = None
    meter_temp_c: float | None = None

    # -- conditions
    ambient_c: float | None = None
    humidity_pct: float | None = None
    ambient_timestamp: datetime | None = None
    ambient_source: str = ""

    # -- build provenance
    caldmm_version: str = ""
    git_revision: str = ""
    schema_version: int = SCHEMA_VERSION
    #: Header keys this build does not know about, preserved verbatim.
    extra: Mapping[str, str] = field(default_factory=_empty_map)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_options",
                           tuple(str(o) for o in self.source_options))
        object.__setattr__(self, "extra",
                           MappingProxyType(dict(self.extra)))
        object.__setattr__(self, "ambient_c", _nan_to_none(self.ambient_c))
        object.__setattr__(self, "humidity_pct",
                           _nan_to_none(self.humidity_pct))
        object.__setattr__(self, "source_cal_temp_c",
                           _nan_to_none(self.source_cal_temp_c))
        # An empty run_kind (a pre-v2 file, or a blank cell) is a run that
        # never said -- which is exactly what "standalone" means.
        object.__setattr__(self, "run_kind",
                           str(self.run_kind or "standalone"))

    @property
    def ambient_known(self) -> bool:
        """Whether an ambient temperature was recorded at all."""
        return self.ambient_c is not None and math.isfinite(self.ambient_c)

    @property
    def claim_tested(self) -> str:
        """The claim a ``--dut source`` run actually tested, in one phrase.

        With the calibrator under test the tolerance at every point where it
        states one is its own ``UNCERT?`` -- **computed by its firmware at its
        own stored ``CAL_INTV`` and ``CAL_CONF``**, not at the interval the
        plan names.  On the bench unit those are 1 day and CONF99, so a run
        headed "interval 1y" tests the 24-hour specification (3.5 ppm + 3 uV
        at 10 V) at every non-zero point.  Both numbers were already in the
        header; nothing said what they meant together, and JCGM 106 5.1 wants
        the interval a certificate states to be unambiguous.

        Empty when the meter is the DUT (no such claim is being tested) or
        when the instrument did not report an interval.
        """
        if (self.dut_role or "").strip().lower() != "source":
            return ""
        if self.source_cal_interval_days is None:
            return ""
        conf = self.source_cal_confidence or "an unstated confidence"
        return f"UNCERT? at {int(self.source_cal_interval_days)} d / {conf}"

    @property
    def claim_disagrees_with_interval(self) -> bool:
        """Whether the tested claim's interval is not the one in the header.

        True is not an error -- it is the state of the bench unit -- but it is
        the thing a reader of the certificate has to be told.
        """
        if not self.claim_tested:
            return False
        named = interval_days(self.interval)
        if named is None:
            return False
        return not math.isclose(named, float(self.source_cal_interval_days),
                                rel_tol=1e-9, abs_tol=1e-9)

    def to_lines(self) -> list[str]:
        return header_to_lines(self)


def _fmt_header_value(kind: str, value) -> str:
    if value is None:
        return ""
    if kind == "datetime":
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if kind == "tuple":
        return ",".join(str(v) for v in value)
    if kind == "int":
        return str(int(value))
    if kind == "float":
        number = float(value)
        # ``repr``, not ``%g``: a real sensor reading is 23.147125 C and ``%g``
        # would store it as 23.1471, so a header would not compare equal to
        # itself after a round trip.  A provenance block that quietly rounds
        # the conditions the run was taken under is not provenance.
        return "" if math.isnan(number) else repr(number)
    return str(value)


def header_to_lines(header: RunHeader) -> list[str]:
    """The provenance block, as ``# key=value`` comment lines.

    Values are escaped for newlines, so one field is always one line -- a
    ``*IDN?`` string that arrived with a stray CR cannot split the block.
    """
    lines = [_HEADER_BANNER]
    for key, kind in _HEADER_FIELDS:
        text = _fmt_header_value(kind, getattr(header, key))
        lines.append(f"# {key}={_escape(text)}")
    # Derived, and written out because a reader should not have to infer
    # "nobody measured it" from a blank cell.
    lines.append(f"# ambient_known={'1' if header.ambient_known else '0'}")
    for key in sorted(header.extra):
        lines.append(f"# {key}={_escape(str(header.extra[key]))}")
    return lines


def _parse_header_value(kind: str, text: str, key: str):
    if kind in ("int", "float", "datetime", "optstr") and not text:
        return None
    if kind == "str":
        return text
    if kind == "optstr":
        return text or None
    if kind == "tuple":
        return tuple(part for part in text.split(",") if part)
    if kind == "int":
        try:
            return int(float(text))
        except ValueError as exc:
            raise ResultError(f"header {key}: not an integer: {text!r}") from exc
    if kind == "float":
        try:
            return float(text)
        except ValueError as exc:
            raise ResultError(f"header {key}: not a number: {text!r}") from exc
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ResultError(
            f"header {key}: not an ISO-8601 timestamp: {text!r}") from exc


def header_from_lines(lines: Iterable[str]) -> RunHeader:
    """Parse a provenance block back into a :class:`RunHeader`.

    Comment lines without an ``=`` (the banner) are ignored; keys this build
    does not know are kept in :attr:`RunHeader.extra`.  ``ambient_known`` is
    dropped on the way in -- it is derived from ``ambient_c``, and honouring a
    stored copy that disagreed with it would be worse than recomputing.
    """
    kinds = dict(_HEADER_FIELDS)
    values: dict[str, Any] = {}
    extra: dict[str, str] = {}
    for raw in lines:
        text = raw.strip()
        if not text.startswith("#"):
            continue
        text = text.lstrip("#").strip()
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        value = _unescape(value.strip())
        if key == "ambient_known":
            continue
        if key in kinds:
            parsed = _parse_header_value(kinds[key], value, key)
            if parsed is not None:
                values[key] = parsed
        else:
            extra[key] = value
    if extra:
        values["extra"] = extra
    return RunHeader(**values)


# --------------------------------------------------------------------------
# The result row
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResultRow:
    """One executed point, with everything needed to re-derive its verdict.

    Every stored field is an *input*.  ``u_c``, ``nu_eff``, ``k`` and ``U`` come
    back out of :attr:`budget`; :attr:`error_ppm`, :attr:`tolerance_ppm`,
    :attr:`relative_margin`, :attr:`source_uncert_limit` and
    :attr:`limit_disagree` are properties.  The CSV carries them as columns for
    legibility, and :meth:`from_cells` ignores those columns.
    """

    quantity: Quantity
    nominal: float
    timestamp: datetime | None = None
    run_id: str = ""
    point_index: int = 0
    group_index: int = 0
    label: str = ""
    #: The plan point's note -- carries the spec ratio that justified the point.
    note: str = ""
    frequency_hz: float = 0.0
    connection: Connection = Connection.TWO_WIRE
    range_arg: float | None = None
    range_id: str | None = None
    mode: str | None = None
    interval: str | None = None

    # -- the source
    source_model: str = ""
    #: The **readback**, not the request.  For resistance this is the
    #: characterized ``OUT?`` value; the whole error computation hangs on it.
    source_value: float | None = None
    source_unit: str = ""
    source_frequency_hz: float = 0.0
    source_range_id: str | None = None
    #: The source's own figure for this output, as it reported it -- value,
    #: unit (``PPM``/``PCT``/absolute), confidence level and interval.
    source_uncertainty: Uncertainty | None = None
    source_settled: bool = False
    source_operate: bool = False

    # -- the meter
    meter_model: str = ""
    mean: float | None = None
    stdev: float | None = None
    n: int = 0
    discarded: int = 0
    #: Invalid-reading sentinels excluded from the mean.  Non-zero here with a
    #: plausible-looking mean is the shape of a point that half happened.
    overloads: int = 0
    resolution: float | None = None
    meter_func: str | None = None
    meter_range_arg: float | None = None
    meter_range_fs: float | None = None
    meter_nplc: float | None = None
    meter_mode: str | None = None
    meter_ac_filter: str | None = None
    meter_settle_s: float = 0.0
    applied: Mapping[str, Any] = field(default_factory=_empty_map)

    # -- conditions at this point
    ambient_c: float | None = None
    humidity_pct: float | None = None
    #: When the ambient was *read*, which is not when this point was measured.
    #: A run polls the ambient probe once a group and takes points for two hours; a row
    #: that carried only the value would make every ambient look
    #: contemporaneous, and the staleness is exactly what a reader has to judge.
    env_timestamp: datetime | None = None
    env_source: str = ""
    #: The meter's own internal temperature at this point, where the
    #: instrument offers one (the 3458A's ``TEMP?``).  Distinct from the
    #: ambient: it is what the meter's electronics are actually at, and the
    #: two drifting apart over a run is itself worth seeing.
    meter_temp_c: float | None = None

    # -- the comparison
    #: The value the error is measured *against*: the source's actual value
    #: when the meter is the DUT, the meter's mean when the source is.  It is
    #: also the base of every ppm column, which is why it is stored rather than
    #: left implicit in ``dut_role``.
    reference_value: float | None = None
    error: float | None = None
    #: The DUT's tolerance, **absolute, in the quantity's unit**.
    tolerance_limit: float | None = None
    #: The *source's* limit from the spec table, for the cross-check below.
    spec_limit: float | None = None

    # -- the decision
    verdict: Verdict = Verdict.NO_SPEC
    expanded_uncertainty: float | None = None
    coverage_factor: float | None = None
    effective_dof: float | None = None
    tur: float | None = None
    guard_band: float | None = None
    acceptance_limit: float | None = None
    margin: float | None = None
    confidence: float = DEFAULT_CONFIDENCE
    temperature_status: TemperatureStatus = TemperatureStatus.UNKNOWN
    flags: tuple[str, ...] = ()
    rule: str = ""
    reason: str = ""

    # -- the evidence
    budget: UncertaintyBudget = field(default_factory=UncertaintyBudget)
    #: The surviving samples behind ``mean`` and ``stdev``, exactly as
    #: :attr:`~caldmm.cal.roles.Reading.samples` carried them -- sentinels
    #: already excluded and counted in ``overloads``.  Empty when a file
    #: predates schema v2, which is "not recorded", never "no samples".
    samples: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantity", Quantity(self.quantity))
        object.__setattr__(self, "connection", Connection(self.connection))
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        object.__setattr__(self, "temperature_status",
                           TemperatureStatus(self.temperature_status))
        object.__setattr__(self, "flags", tuple(str(f) for f in self.flags))
        object.__setattr__(self, "applied",
                           MappingProxyType(dict(self.applied)))
        object.__setattr__(self, "samples",
                           tuple(float(s) for s in self.samples))
        if not isinstance(self.budget, UncertaintyBudget):
            raise ResultError(
                "budget must be an UncertaintyBudget, got "
                f"{type(self.budget).__name__}; a row without an itemised "
                "budget cannot support the verdict it carries")
        for name in ("nominal", "source_value", "mean", "stdev", "resolution",
                     "reference_value", "error", "tolerance_limit",
                     "spec_limit", "expanded_uncertainty", "coverage_factor",
                     "effective_dof", "tur", "guard_band", "acceptance_limit",
                     "margin", "ambient_c", "humidity_pct", "meter_temp_c",
                     "range_arg", "meter_range_arg", "meter_range_fs",
                     "meter_nplc"):
            object.__setattr__(self, name, _nan_to_none(getattr(self, name)))
        if self.nominal is None:
            raise ResultError("nominal is required and may not be nan")

    # ---- derived: units and ppm

    @property
    def unit(self) -> str:
        """Canonical unit of the quantity -- ``V``, ``A`` or ``OHM``."""
        return self.quantity.unit

    @property
    def ppm_base(self) -> float | None:
        """Magnitude the ppm columns are taken against: ``|reference|``."""
        if self.reference_value is None:
            return None
        return abs(self.reference_value)

    @property
    def is_zero_point(self) -> bool:
        """Whether "ppm of reading" means anything here.

        **The setpoint decides first.**  A point planned at zero is a zero
        point whatever the meter read: in a source-DUT run the reference is
        the meter's own mean, so a 0 V point whose meter sat 2 uV off its zero
        was not a zero point by the reference rule below and reported an error
        of +1,000,000 ppm and a tolerance of 400,000 ppm -- ppm of the meter's
        offset, which is arithmetic and not information.  Eleven of the 156
        points of the shipped source-DUT plan are zero setpoints.

        A point is *also* a zero point when its reference is zero, and when
        the reference is smaller than its own tolerance -- a short measured as
        120 nV against a 4 uV limit is not a 120 nV *setpoint*, and reporting
        an error as 30000 ppm of it would be arithmetic too.  That is the
        unit-free way to say "approximately zero": the only scale available is
        the point's own limit.
        """
        if self.nominal == 0.0:
            return True
        base = self.ppm_base
        if base is None or base == 0.0:
            return True
        if self.tolerance_limit is not None and base < self.tolerance_limit:
            return True
        return False

    def _ppm(self, value: float | None) -> float | None:
        if value is None or self.is_zero_point:
            return None
        return value / self.ppm_base * 1e6

    @property
    def error_ppm(self) -> float | None:
        """The error as ppm of the reference; ``None`` at a zero point."""
        return self._ppm(self.error)

    @property
    def tolerance_ppm(self) -> float | None:
        """The tolerance as ppm of the reference; ``None`` at a zero point.

        The absolute limit is always present -- storing ppm alone is what makes
        a zero or short row meaningless.
        """
        return self._ppm(self.tolerance_limit)

    # ---- derived: the cross-check

    @property
    def source_uncert_limit(self) -> float | None:
        """The source's own ``UNCERT?`` as an absolute, at what it sourced."""
        if self.source_uncertainty is None:
            return None
        base = self.source_value if self.source_value is not None else 0.0
        return self.source_uncertainty.absolute(base)

    @property
    def limit_disagree(self) -> bool:
        """The spec table and the instrument's own figure do not agree.

        Both must exist and be positive; the flag fires when they differ by
        more than :data:`LIMIT_DISAGREEMENT_FACTOR`.  It costs one division and
        it is what catches a unit whose stored calibration constants are
        corrupt, or whose ``CAL_INTV``/``CAL_CONF`` is not what the report
        assumes -- neither of which shows up anywhere else in a run that
        otherwise looks perfect.
        """
        spec = self.spec_limit
        own = self.source_uncert_limit
        if spec is None or own is None or spec <= 0.0 or own <= 0.0:
            return False
        ratio = spec / own
        return ratio > LIMIT_DISAGREEMENT_FACTOR \
            or ratio < 1.0 / LIMIT_DISAGREEMENT_FACTOR

    # ---- derived: the decision

    @property
    def relative_margin(self) -> float | None:
        """``margin/TL`` -- the only sound way to rank points against each other.

        ``None`` when there is no usable limit, which keeps such points out of
        a run's "worst" ranking instead of letting a ``nan`` win it.
        """
        if self.margin is None or self.tolerance_limit is None:
            return None
        if not math.isfinite(self.tolerance_limit) or self.tolerance_limit <= 0:
            return None
        return self.margin / self.tolerance_limit

    @property
    def indeterminate_by_tur(self) -> bool:
        """``INDETERMINATE`` because the reference was not good enough."""
        return (self.verdict is Verdict.INDETERMINATE
                and FLAG_TUR_BELOW_FLOOR in self.flags)

    @property
    def carries_temperature_condition(self) -> bool:
        """``INDETERMINATE`` **and** carrying a temperature flag of any kind.

        Named for what it actually tests.  It is *not* "the temperature blocked
        this point": :data:`TEMPERATURE_FLAGS` includes
        ``no-temperature-window``, which the default ``gate`` policy does not
        block on at all -- and both the 3458A and the 5700A declare no absolute
        window, so on the pairing this project exists to run every row carries
        it.  Reading this as the block turned "182 points the reference was too
        coarse for" into "182 temperature-gated" in a report.  Use
        :meth:`blocked_by_temperature` for the decision; this one is for "does
        a report have to mention the temperature here at all".
        """
        return (self.verdict is Verdict.INDETERMINATE
                and any(f in self.flags for f in TEMPERATURE_FLAGS))

    def blocked_by_temperature(self, policy: str = "gate") -> bool:
        """``INDETERMINATE`` because *policy* blocks on this temperature status.

        The honest question, and it cannot be answered without the policy: the
        same ``NO_WINDOW`` row is blocked under ``strict`` and merely flagged
        under ``gate``.  The policy is a property of the *run* -- it is in
        :attr:`RunHeader.temperature_policy` -- and must be passed in; it is
        deliberately not recovered from :attr:`rule`, which only spells the
        policy out when it is not the default.
        """
        return (self.verdict is Verdict.INDETERMINATE
                and self.temperature_status in gates_for(policy))

    # ---- construction from the measurement objects

    @classmethod
    def from_measurement(
        cls,
        *,
        point,
        conformity: ConformityResult,
        budget: UncertaintyBudget,
        state: SourceState | None = None,
        reading: Reading | None = None,
        config: MeterConfig | None = None,
        environment: Environment = UNKNOWN_ENVIRONMENT,
        source_model: str = "",
        meter_model: str = "",
        run_id: str = "",
        point_index: int = 0,
        group_index: int = 0,
        reference_value: float | None = None,
        spec_limit: float | None = None,
        timestamp: datetime | None = None,
        meter_temp_c: float | None = None,
    ) -> "ResultRow":
        """Assemble a row from the objects an execution step produces.

        *point* is any :class:`~caldmm.cal.roles.PointLike` -- everything past
        the four attributes an adapter dereferences is read with a default, so
        a plain ``Point`` and a ``PlanPoint`` both work and this module never
        has to import :mod:`caldmm.cal.plan`.

        *reference_value* defaults to what the source actually sourced, which
        is right when the **meter** is the DUT.  When the *source* is the DUT
        the reference is the meter's mean and the caller must say so: the
        default cannot know, and a ppm base taken from the wrong side of the
        comparison would be wrong by the very error being measured.

        The error, the limit and every decision field are taken from
        *conformity* verbatim rather than recomputed, so the row records the
        numbers the verdict was actually reached with.
        """
        applied = getattr(config, "applied", None) or {}
        return cls(
            quantity=point.quantity,
            nominal=float(point.value),
            timestamp=timestamp,
            run_id=run_id,
            point_index=int(point_index),
            group_index=int(group_index),
            label=str(getattr(point, "label", "") or ""),
            note=str(getattr(point, "note", "") or ""),
            frequency_hz=float(getattr(point, "frequency_hz", 0.0) or 0.0),
            connection=point.connection,
            range_arg=getattr(point, "range_arg", None),
            range_id=getattr(point, "range_id", None),
            mode=getattr(point, "mode", None),
            interval=getattr(point, "interval", None),

            source_model=source_model,
            source_value=None if state is None else state.actual_value,
            source_unit="" if state is None else state.unit,
            source_frequency_hz=0.0 if state is None else state.frequency_hz,
            source_range_id=None if state is None else state.range_id,
            source_uncertainty=None if state is None else state.uncertainty,
            source_settled=bool(getattr(state, "settled", False)),
            source_operate=bool(getattr(state, "operate", False)),

            meter_model=meter_model,
            mean=None if reading is None else reading.mean,
            stdev=None if reading is None else reading.stdev,
            n=0 if reading is None else int(reading.n),
            discarded=0 if reading is None else int(reading.discarded),
            overloads=0 if reading is None else int(reading.overloads),
            resolution=None if reading is None else reading.resolution,
            meter_func=getattr(config, "func", None),
            meter_range_arg=getattr(config, "range_arg", None),
            meter_range_fs=getattr(config, "range_fs", None),
            meter_nplc=getattr(config, "nplc", None),
            meter_mode=getattr(config, "mode", None),
            meter_ac_filter=getattr(config, "ac_filter", None),
            meter_settle_s=float(getattr(config, "settle_s", 0.0) or 0.0),
            applied=applied,

            ambient_c=environment.ambient_c,
            humidity_pct=environment.humidity_pct,
            env_timestamp=environment.timestamp,
            env_source=environment.source,
            meter_temp_c=meter_temp_c,

            reference_value=(reference_value if reference_value is not None
                             else (None if state is None
                                   else state.actual_value)),
            error=conformity.error,
            tolerance_limit=conformity.tolerance_limit,
            spec_limit=spec_limit,

            verdict=conformity.verdict,
            expanded_uncertainty=conformity.expanded_uncertainty,
            coverage_factor=conformity.coverage_factor,
            effective_dof=conformity.effective_dof,
            tur=conformity.tur,
            guard_band=conformity.guard_band,
            acceptance_limit=conformity.acceptance_limit,
            margin=conformity.margin,
            confidence=conformity.confidence,
            temperature_status=conformity.temperature_status,
            flags=conformity.flags,
            rule=conformity.rule,
            reason=conformity.reason,
            budget=budget,
            samples=() if reading is None else tuple(reading.samples),
        )

    # ---- serialisation

    def values(self) -> dict[str, Any]:
        """The raw (unformatted) value behind every column."""
        uncert = self.source_uncertainty
        return {
            "Run_Id": self.run_id,
            "Point": self.point_index,
            "Group": self.group_index,
            "Label": self.label,
            "Note": self.note,

            "Quantity": self.quantity,
            "Nominal": self.nominal,
            "Freq_Hz": self.frequency_hz,
            "Connection": self.connection,
            "Range_Arg": self.range_arg,
            "Range_Id": self.range_id,
            "Mode": self.mode,
            "Interval": self.interval,

            "Src_Model": self.source_model,
            "Src_Value": self.source_value,
            "Src_Unit": self.source_unit,
            "Src_Freq_Hz": self.source_frequency_hz,
            "Src_Range_Id": self.source_range_id,
            "Src_Uncert": None if uncert is None else uncert.value,
            "Src_Uncert_Unit": None if uncert is None else uncert.unit,
            "Src_Uncert_Conf": None if uncert is None else uncert.confidence,
            "Src_Uncert_Days": None if uncert is None else uncert.interval_days,
            "Src_Settled": self.source_settled,
            "Src_Operate": self.source_operate,

            "Mtr_Model": self.meter_model,
            "Mean": self.mean,
            "Stdev": self.stdev,
            "N": self.n,
            "Discarded": self.discarded,
            "Overloads": self.overloads,
            "Resolution": self.resolution,
            "Mtr_Func": self.meter_func,
            "Mtr_Range_Arg": self.meter_range_arg,
            "Mtr_Range_FS": self.meter_range_fs,
            "Mtr_NPLC": self.meter_nplc,
            "Mtr_Mode": self.meter_mode,
            "Mtr_AC_Filter": self.meter_ac_filter,
            "Mtr_Settle_s": self.meter_settle_s,
            "Mtr_Applied": dict(self.applied),

            "Ambient_C": self.ambient_c,
            "Humidity_Pct": self.humidity_pct,
            "Env_Time": self.env_timestamp,
            "Env_Source": self.env_source,
            "Meter_Temp_C": self.meter_temp_c,

            "Reference": self.reference_value,
            "Error": self.error,
            "Error_ppm": self.error_ppm,
            "Tol_Limit": self.tolerance_limit,
            "Tol_Limit_ppm": self.tolerance_ppm,

            "Spec_Limit": self.spec_limit,
            "Uncert_Limit": self.source_uncert_limit,
            "Limit_Disagree": self.limit_disagree,

            "Verdict": self.verdict,
            "U": self.expanded_uncertainty,
            "k": self.coverage_factor,
            "Nu_Eff": self.effective_dof,
            "TUR": self.tur,
            "Guard_Band": self.guard_band,
            "Accept_Limit": self.acceptance_limit,
            "Margin": self.margin,
            "Rel_Margin": self.relative_margin,
            "Confidence": self.confidence,
            "Temp_Status": self.temperature_status,
            "Flags": self.flags,
            "Rule": self.rule,
            "Reason": self.reason,

            "Budget": budget_to_rows(self.budget),
            "Samples": list(self.samples),
        }

    def to_cells(self) -> dict[str, str]:
        """Formatted cells, keyed by column.

        ``Time`` is not included: :class:`~caldmm.lib.csv_io.CsvWriter`
        prepends it from the timestamp passed to ``write_row``.

        Indexes :meth:`values` rather than ``.get``: a column added to
        :data:`CSV_COLUMNS` and forgotten in :meth:`values` would otherwise
        write a blank in every row of every run until someone noticed the
        column was always empty.
        """
        values = self.values()
        try:
            return {col: _fmt_cell(col, values[col]) for col in CSV_COLUMNS}
        except KeyError as exc:
            raise ResultError(
                f"column {exc.args[0]!r} is in CSV_COLUMNS but ResultRow."
                f"values() does not produce it") from exc

    @classmethod
    def from_cells(cls, cells: Mapping[str, str]) -> "ResultRow":
        """Rebuild a row from one CSV line's cells (including ``Time``).

        **Nothing derived is read back.**  Both ppm columns, ``Rel_Margin``,
        ``Uncert_Limit`` and ``Limit_Disagree`` are properties; ``U``, ``k``,
        ``Nu_Eff``, ``TUR``, ``Accept_Limit`` and ``Margin`` are recomputed
        here from the budget and the stored inputs.  The written cells are
        11-digit decimal, so reading them back would leave a loaded row
        carrying a ``U`` that differs in the twelfth digit from the ``U`` its
        own budget gives -- harmless numerically and fatal to the premise, in a
        file whose entire claim is that it is self-consistent.

        ``Guard_Band`` is the one decision number that *is* read: ``w`` follows
        from the rule (zero for simple acceptance, ``+g*U`` for guarded
        acceptance, ``-g*U`` for guarded rejection) and the budget does not
        know which rule was applied.  Recovering it by parsing the ``Rule``
        string would make this reader depend on
        :mod:`caldmm.cal.verdict`'s private formatting.
        """
        timestamp = _datetime_of(cells, "Time")

        uncert_value = _float_of(cells, "Src_Uncert")
        uncertainty = None
        if uncert_value is not None:
            uncertainty = Uncertainty(
                value=uncert_value,
                unit=_text_of(cells, "Src_Uncert_Unit"),
                confidence=_float_of(cells, "Src_Uncert_Conf"),
                interval_days=_int_of(cells, "Src_Uncert_Days"),
            )

        nominal = _float_of(cells, "Nominal")
        if nominal is None:
            raise ResultError("Nominal is required")
        budget_rows = _json_of(cells, "Budget") or []
        applied = _json_of(cells, "Mtr_Applied") or {}
        # A file written before schema v2 has no Samples column at all; an
        # empty tuple says "not recorded", which the loader must not dress up.
        sample_values = _json_of(cells, "Samples") or []
        try:
            samples = tuple(float(v) for v in sample_values)
        except (TypeError, ValueError) as exc:
            raise ResultError(
                f"Samples: not a list of numbers: {sample_values!r}") from exc
        confidence = _float_of(cells, "Confidence")
        if confidence is None:
            confidence = DEFAULT_CONFIDENCE
        flags_text = _text_of(cells, "Flags")

        # -- the decision, re-derived rather than re-read.  k is taken once and
        # U built from it, exactly as verdict.evaluate does, so the k a
        # regenerated report prints is provably the k behind its own U.
        budget = budget_from_rows(budget_rows)
        coverage_factor = budget.coverage_factor(confidence)
        effective_dof = budget.effective_dof()
        expanded = coverage_factor * budget.combined()
        error = _float_of(cells, "Error")
        tolerance_limit = _float_of(cells, "Tol_Limit")
        guard_band = _float_of(cells, "Guard_Band")
        tur = (None if tolerance_limit is None or expanded == 0.0
               else tolerance_limit / expanded)
        acceptance_limit = (None if tolerance_limit is None
                            or guard_band is None
                            else tolerance_limit - guard_band)
        margin = (None if tolerance_limit is None or error is None
                  else tolerance_limit - abs(error))

        return cls(
            quantity=_enum_of(Quantity, cells, "Quantity"),
            nominal=nominal,
            timestamp=timestamp,
            run_id=_text_of(cells, "Run_Id"),
            point_index=_int_of(cells, "Point") or 0,
            group_index=_int_of(cells, "Group") or 0,
            label=_text_of(cells, "Label"),
            note=_text_of(cells, "Note"),
            frequency_hz=_float_of(cells, "Freq_Hz") or 0.0,
            connection=_enum_of(Connection, cells, "Connection"),
            range_arg=_float_of(cells, "Range_Arg"),
            range_id=_opt_text(cells, "Range_Id"),
            mode=_opt_text(cells, "Mode"),
            interval=_opt_text(cells, "Interval"),

            source_model=_text_of(cells, "Src_Model"),
            source_value=_float_of(cells, "Src_Value"),
            source_unit=_text_of(cells, "Src_Unit"),
            source_frequency_hz=_float_of(cells, "Src_Freq_Hz") or 0.0,
            source_range_id=_opt_text(cells, "Src_Range_Id"),
            source_uncertainty=uncertainty,
            source_settled=_bool_of(cells, "Src_Settled"),
            source_operate=_bool_of(cells, "Src_Operate"),

            meter_model=_text_of(cells, "Mtr_Model"),
            mean=_float_of(cells, "Mean"),
            stdev=_float_of(cells, "Stdev"),
            n=_int_of(cells, "N") or 0,
            discarded=_int_of(cells, "Discarded") or 0,
            overloads=_int_of(cells, "Overloads") or 0,
            resolution=_float_of(cells, "Resolution"),
            meter_func=_opt_text(cells, "Mtr_Func"),
            meter_range_arg=_float_of(cells, "Mtr_Range_Arg"),
            meter_range_fs=_float_of(cells, "Mtr_Range_FS"),
            meter_nplc=_float_of(cells, "Mtr_NPLC"),
            meter_mode=_opt_text(cells, "Mtr_Mode"),
            meter_ac_filter=_opt_text(cells, "Mtr_AC_Filter"),
            meter_settle_s=_float_of(cells, "Mtr_Settle_s") or 0.0,
            applied=applied,

            ambient_c=_float_of(cells, "Ambient_C"),
            humidity_pct=_float_of(cells, "Humidity_Pct"),
            env_timestamp=_datetime_of(cells, "Env_Time"),
            env_source=_text_of(cells, "Env_Source"),
            meter_temp_c=_float_of(cells, "Meter_Temp_C"),

            reference_value=_float_of(cells, "Reference"),
            error=error,
            tolerance_limit=tolerance_limit,
            spec_limit=_float_of(cells, "Spec_Limit"),

            verdict=_enum_of(Verdict, cells, "Verdict", "no_spec"),
            expanded_uncertainty=expanded,
            coverage_factor=coverage_factor,
            effective_dof=effective_dof,
            tur=tur,
            guard_band=guard_band,
            acceptance_limit=acceptance_limit,
            margin=margin,
            confidence=confidence,
            temperature_status=_enum_of(TemperatureStatus, cells,
                                        "Temp_Status", "unknown"),
            flags=tuple(f for f in flags_text.split(",") if f),
            rule=_text_of(cells, "Rule"),
            reason=_text_of(cells, "Reason"),
            budget=budget,
            samples=samples,
        )


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------

_COLUMN_LINE = "Time;" + ";".join(CSV_COLUMNS)


class ResultWriter:
    """A result file open for writing, one **flushed** line per point.

    Why streaming is not a nicety here: a verification run is on the order of
    200 points over two hours, operator-attended, across several rewiring
    groups.  A writer that buffered the run and wrote it at the end would lose
    everything to a crash, a dropped ssh session or a Ctrl-C at point 180 --
    which is precisely the failure this module exists to prevent, and it would
    also leave the runner nothing to resume from.  Every row is therefore
    written and flushed as it is measured, so the file on disk is always a
    valid, loadable record of every point completed so far.

    ``append=True`` reopens an existing run instead of truncating it, for
    resuming at a group boundary.  The existing file's column line is checked
    against this build's :data:`CSV_COLUMNS` first: appending rows of a
    different shape would produce a file that neither schema can read, and the
    rows already in it are the ones that cost two hours.
    """

    def __init__(self, path: str, header: RunHeader, *,
                 append: bool = False) -> None:
        self._path = path
        resuming = (append and os.path.exists(path)
                    and os.path.getsize(path) > 0)
        if resuming:
            _check_columns(path)
        else:
            # The provenance block has to lead the file, and CsvWriter
            # suppresses its own column line when appending to a non-empty
            # file, so the block and that one line are written here -- in
            # exactly CsvWriter's format.  Every data row still goes through
            # CsvWriter, so a result file is laid out like every other CSV
            # this project writes.
            with open(path, "w", encoding="utf-8") as handle:
                for line in header_to_lines(header):
                    handle.write(line + "\n")
                handle.write(_COLUMN_LINE + "\n")
        self._writer = CsvWriter(path, CSV_COLUMNS, append=True)
        self._rows_written = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def rows_written(self) -> int:
        """Rows written by *this* writer (not rows already in the file)."""
        return self._rows_written

    def write(self, row: ResultRow) -> None:
        """Append one row and flush it to the operating system.

        A row with no ``timestamp`` is **rejected**.  ``CsvWriter.write_row``
        would happily stamp it with ``datetime.now()`` -- the file-write wall
        clock, not the measurement time -- and it would then load back looking
        like a measurement that was taken at a moment nobody measured
        anything.  A fabricated time in a calibration record is worse than a
        missing one, and it is the only field that would silently fail to
        round-trip.
        """
        if not isinstance(row, ResultRow):
            raise ResultError(f"ResultWriter takes ResultRow, got {row!r}")
        if row.timestamp is None:
            raise ResultError(
                f"point {row.point_index} ({row.label or row.quantity.name}) "
                f"has no timestamp; a result row must carry the time it was "
                f"measured, and this writer will not substitute the "
                f"file-write clock")
        self._writer.write_row(row.to_cells(), ts=row.timestamp)
        self._writer.flush()
        self._rows_written += 1

    def close(self) -> None:
        self._writer.close()

    def __enter__(self) -> "ResultWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _check_columns(path: str) -> None:
    """Verify an existing file's column line matches this build's schema."""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line != _COLUMN_LINE:
                existing = line.split(";")
                raise ResultError(
                    f"{path}: cannot append -- the file has "
                    f"{len(existing)} columns and this build writes "
                    f"{len(CSV_COLUMNS) + 1}; refusing to mix two schemas in "
                    f"one result file")
            return
    raise ResultError(f"{path}: cannot append -- no column header line")


def open_results(path: str, header: RunHeader, *,
                 append: bool = False) -> ResultWriter:
    """Open a result file for streaming writes; use as a context manager.

    The interface the runner wants::

        with open_results(path, header) as out:
            for point in plan:
                out.write(measure(point))       # flushed as it goes
    """
    return ResultWriter(path, header, append=append)


def write_results(path: str, header: RunHeader,
                  rows: Iterable[ResultRow]) -> str:
    """Write a complete result file: provenance block, columns, rows.

    Convenience over :class:`ResultWriter` for a run that is already in
    memory; it streams and flushes just the same, so passing a generator is
    safe and the file grows as the generator yields.  Returns *path*.
    """
    with ResultWriter(path, header) as writer:
        for row in rows:
            writer.write(row)
    return path


def load_results(path: str) -> tuple[RunHeader, list[ResultRow]]:
    """Read a result file back as ``(header, rows)``.

    The reader, because ``lib.csv_io.load_csv`` is pandas without a ``comment``
    argument and would take the provenance block for data.  Everything the
    verdict was reached with is reconstructed, including the itemised budget,
    so a report can be regenerated from the file alone.
    """
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    comments: list[str] = []
    columns: list[str] | None = None
    rows: list[ResultRow] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            comments.append(line)
            continue
        cells = line.split(";")
        if columns is None:
            columns = [c.strip() for c in cells]
            continue
        if len(cells) != len(columns):
            raise ResultError(
                f"{path}:{number}: {len(cells)} cells for {len(columns)} "
                f"columns -- the file is truncated or a cell was written with "
                f"an unescaped delimiter")
        rows.append(ResultRow.from_cells(dict(zip(columns, cells))))
    if columns is None:
        raise ResultError(f"{path}: no column header line")
    return header_from_lines(comments), rows


# --------------------------------------------------------------------------
# Run summary
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RunSummary:
    """Counts per verdict, the worst point, and *why* the unclear ones are unclear.

    The ``indeterminate_*`` counts are the point of this class.  "17
    indeterminate" tells an operator nothing; "12 of them because the TUR floor
    was not met" says the *reference* is not good enough for those points and
    no amount of re-running will change it, while "5 because the ambient was
    never recorded" is a procedural fix that costs nothing.  A point blocked by
    both is counted in both -- they are independent causes, not a partition.

    They are only meaningful against the run's **temperature policy**, which is
    why :func:`summarize_run` takes it: a point whose model declares no
    absolute window is blocked under ``strict`` and merely flagged under
    ``gate``, and counting the flag instead of the block reported every point
    of a 3458A-against-5700A run as temperature-gated while leaving
    :attr:`indeterminate_guard_band` at zero.
    """

    counts: Mapping[Verdict, int] = field(
        default_factory=lambda: MappingProxyType({v: 0 for v in Verdict}))
    total: int = 0
    #: Ranked on the dimensionless margin/TL; ``None`` when no row had a limit.
    worst_relative_margin: float | None = None
    worst: ResultRow | None = None
    #: Rows carrying at least one flag a report must print.
    flagged: int = 0
    #: ``INDETERMINATE`` because ``TUR`` was below the floor -- "your reference
    #: is not good enough here", as opposed to "your DUT is marginal".
    indeterminate_tur: int = 0
    #: ``INDETERMINATE`` because the run's temperature *policy* blocked it --
    #: not merely because the row carries a temperature flag.  A **subset** of
    #: :attr:`indeterminate_conditions`: the temperature is one of the stated
    #: conditions, kept separately because it is the one with a policy.
    indeterminate_temperature: int = 0
    #: ``INDETERMINATE`` because a stated condition of the specification was
    #: not shown to hold -- the source was not warm, its dc zeros are overdue,
    #: the ambient was outside the window or was never recorded.  These are
    #: **not** statements about the DUT and are never counted as PASS or FAIL:
    #: the specification is simply not in force at such a point.
    indeterminate_conditions: int = 0
    #: ``INDETERMINATE`` with none of the other causes: the error itself landed
    #: in the guard band, which is the one cause that is about the **DUT**.
    indeterminate_guard_band: int = 0
    #: ``INDETERMINATE`` because no uncertainty components were supplied.  Kept
    #: out of :attr:`indeterminate_guard_band`: such a point has ``U = 0``, so
    #: its guard band is zero and its error is nowhere near a limit -- it is
    #: unclear because nothing was measured *about* the measurement.
    indeterminate_no_budget: int = 0
    #: Invalid-reading sentinels excluded across the whole run.
    overloads: int = 0
    discarded: int = 0

    def __getitem__(self, verdict) -> int:
        return self.counts[Verdict(verdict)]

    def line(self) -> str:
        """One line for the end of a run."""
        order = (Verdict.PASS, Verdict.FAIL, Verdict.INDETERMINATE,
                 Verdict.NO_SPEC)
        parts = ", ".join(f"{self.counts[v]} {v.value.replace('_', ' ')}"
                          for v in order)
        text = f"{self.total} point{'' if self.total == 1 else 's'}: {parts}"
        causes = []
        if self.indeterminate_tur:
            causes.append(f"{self.indeterminate_tur} below the TUR floor")
        if self.indeterminate_temperature:
            causes.append(
                f"{self.indeterminate_temperature} temperature-gated")
        others = self.indeterminate_conditions - self.indeterminate_temperature
        if others > 0:
            causes.append(f"{others} outside other stated conditions")
        if self.indeterminate_guard_band:
            causes.append(f"{self.indeterminate_guard_band} in the guard band")
        if self.indeterminate_no_budget:
            causes.append(f"{self.indeterminate_no_budget} with no budget")
        if causes:
            text += " (" + ", ".join(causes) + ")"
        if self.worst is not None and self.worst_relative_margin is not None:
            text += (f"; worst margin {self.worst_relative_margin * 100:+.1f} %"
                     f" of TL at {self.worst.label or self.worst.quantity.name}")
        if self.overloads:
            text += f"; {self.overloads} overload samples"
        if self.flagged:
            text += f"; {self.flagged} flagged"
        return text

    def __str__(self) -> str:
        return self.line()


def summarize_run(rows: Sequence[ResultRow], *,
                  temperature_policy: str = "gate") -> RunSummary:
    """Counts per verdict, the worst relative margin, and the TUR-floor subset.

    The ranking is on :attr:`ResultRow.relative_margin` -- dimensionless,
    because a run spans volts, ohms and amps and ranking raw margins would
    nominate whichever quantity carries the biggest numbers rather than the
    point closest to failing.

    *temperature_policy* is the run's own policy, from
    :attr:`RunHeader.temperature_policy`, and it is **required to get the
    causes right**: only the statuses that policy blocks on count towards
    :attr:`RunSummary.indeterminate_temperature`, and everything else unclaimed
    by the TUR floor falls to :attr:`RunSummary.indeterminate_guard_band`.
    Counting the *flag* instead put every row of a 3458A-against-5700A run --
    which carries ``no-temperature-window`` on all of them, because neither
    model declares an absolute window -- into the temperature bucket, and left
    the guard-band bucket reading zero on every real run.

    An ``INDETERMINATE`` point with **no budget at all** -- or a budget whose
    components combine to ``U = 0`` -- is counted in
    :attr:`RunSummary.indeterminate_no_budget` and kept out of the guard-band
    bucket: with ``U = 0`` the guard band is zero, so such a point never landed
    in one, and calling it a guard-band case would report "the DUT is close to
    its limit" about a point whose error may be nowhere near it.
    """
    counts = {v: 0 for v in Verdict}
    worst: ResultRow | None = None
    worst_relative: float | None = None
    flagged = tur_blocked = temp_blocked = band = no_budget = 0
    conditions = 0
    overloads = discarded = total = 0
    for row in rows:
        if not isinstance(row, ResultRow):
            raise ResultError(f"summarize_run() takes ResultRow, got {row!r}")
        total += 1
        counts[row.verdict] += 1
        overloads += row.overloads
        discarded += row.discarded
        if row.flags:
            flagged += 1
        if row.verdict is Verdict.INDETERMINATE:
            by_tur = row.indeterminate_by_tur
            by_temp = row.blocked_by_temperature(temperature_policy)
            unmet = FLAG_CONDITIONS_NOT_MET in row.flags
            empty = (FLAG_EMPTY_BUDGET in row.flags
                     or FLAG_ZERO_UNCERTAINTY in row.flags)
            tur_blocked += int(by_tur)
            temp_blocked += int(by_temp)
            conditions += int(unmet)
            no_budget += int(empty)
            band += int(not by_tur and not by_temp and not unmet
                        and not empty)
        relative = row.relative_margin
        if relative is None or math.isnan(relative):
            continue
        if worst_relative is None or relative < worst_relative:
            worst, worst_relative = row, relative
    return RunSummary(
        counts=MappingProxyType(counts),
        total=total,
        worst_relative_margin=worst_relative,
        worst=worst,
        flagged=flagged,
        indeterminate_tur=tur_blocked,
        indeterminate_temperature=temp_blocked,
        indeterminate_guard_band=band,
        indeterminate_conditions=conditions,
        indeterminate_no_budget=no_budget,
        overloads=overloads,
        discarded=discarded,
    )

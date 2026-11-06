"""SI-prefix formatting helpers for measurement values."""

import math
from datetime import datetime

from prefixed import Float


def si_format(value: float, precision: int = 9, unit: str = '') -> str:
    """Format a value with SI prefix notation.

    >>> si_format(0.001234, precision=3, unit='V')
    '1.23 mV'
    """
    return f"{Float(value):.{precision}h}{unit}"


# --------------------------------------------------------------------------
# Report prose
#
# Shared by ``apps/cal_analyze`` and ``apps/cal_verify``, which print the same
# numbers about the same run -- one from the file and one as it is measured.
# Two copies drifted apart is how a run summary and its own report end up
# disagreeing about how long the run took.
# --------------------------------------------------------------------------

def is_blank(value) -> bool:
    """Whether *value* is "there is no number here".

    ``nan`` counts: it is what ``cal`` uses throughout for a quantity that was
    never computed, and formatting it as ``nan`` in a report would put a number
    where the honest answer is a blank.
    """
    return value is None or (isinstance(value, float) and math.isnan(value))


def fmt_g(value, digits: int = 4, blank: str = "n/a") -> str:
    """A plain number to *digits* significant figures, or *blank*."""
    if is_blank(value):
        return blank
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return f"{float(value):.{digits}g}"


def plural(count: int, noun: str) -> str:
    """``1 point`` / ``2 points`` -- naive, and right for every noun here."""
    return f"{count} {noun}" + ("" if count == 1 else "s")


def duration(seconds: float | None) -> str:
    """A number of seconds as ``2 h 05 min``, ``7 min 30 s`` or ``9 s``."""
    if seconds is None:
        return "unknown"
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def stamp(value) -> str:
    """A timestamp to the second, or ``unknown``."""
    if value is None:
        return "unknown"
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def ppm(value: float, reference: float) -> float:
    """Convert an absolute value to PPM relative to a reference."""
    if reference == 0:
        return 0.0
    return (value - reference) / abs(reference) * 1e6


def ppm_array(values, reference: float):
    """Convert an array of values to PPM relative to a reference.

    Works with numpy arrays and plain lists.
    """
    import numpy as np
    arr = np.asarray(values, dtype=np.float64)
    if reference == 0:
        return np.zeros_like(arr)
    return (arr - reference) / abs(reference) * 1e6
